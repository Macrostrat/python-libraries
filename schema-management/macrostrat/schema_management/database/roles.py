"""Applying DDL as a chosen role, so objects are born with the right owner.

A declarative chunk, a migration and a diff plan are each applied as the
application's owning role rather than as whoever is connected. Statements the
role is refused (an extension, a shared schema) are re-run as the connector:
privilege is what separates the two cases and Postgres already reports it, so
this reacts to `42501` rather than classifying statements up front.
"""

from contextlib import contextmanager
from typing import Optional
from weakref import WeakKeyDictionary

from psycopg.sql import Identifier
from sqlalchemy import event

from macrostrat.database import Database
from macrostrat.utils import get_logger

log = get_logger(__name__)

# The role each engine's connections are pinned to, per engine. See `pin_role`.
_role_pins: WeakKeyDictionary = WeakKeyDictionary()


class _RolePin:
    """Checkout/checkin listeners holding one engine's connections to a role."""

    def __init__(self, engine, role: str):
        self.role = role
        event.listen(engine, "checkout", self.checkout)
        event.listen(engine, "checkin", self.checkin)

    def remove(self, engine):
        event.remove(engine, "checkout", self.checkout)
        event.remove(engine, "checkin", self.checkin)

    def checkout(self, dbapi_connection, connection_record, connection_proxy):
        _set_session_role(dbapi_connection, self.role)

    def checkin(self, dbapi_connection, connection_record):
        # None for a connection that was invalidated, and so is going away anyway.
        if dbapi_connection is None:
            return
        try:
            _set_session_role(dbapi_connection, None)
        except Exception:  # noqa: BLE001 -- a broken connection is discarded later
            log.debug("Could not reset the role of a returned connection")


def _set_session_role(dbapi_connection, role: str | None):
    if role is None:
        statement = "RESET ROLE"
    else:
        statement = 'SET ROLE "' + role.replace('"', '""') + '"'
    with dbapi_connection.cursor() as cursor:
        cursor.execute(statement)
    # Committed, so the role outlives the transaction SQLAlchemy opens next.
    dbapi_connection.commit()


def pin_role(engine, role: str | None) -> None:
    """Have every connection `engine` hands out act as `role`; `None` stops.

    `SET ROLE` belongs to one connection, and most code here commits per
    statement, which hands the session's connection back to the pool. Once the
    pool holds a second connection -- anything that called `engine.connect()`,
    `db.transaction()` or a library that opens its own -- the next statement can
    land on a connection that never took the role, and whatever it creates is
    owned by the connector. So the role is pinned to the engine instead: set on
    every checkout, and reset on checkin so that no pooled connection outlives
    the pin still acting as the role. A connection the caller already holds is
    not reached; set its role directly.
    """
    pin = _role_pins.get(engine)
    if role is None:
        if pin is not None:
            pin.remove(engine)
            del _role_pins[engine]
        return
    if pin is None:
        _role_pins[engine] = _RolePin(engine, role)
    else:
        pin.role = role


def set_applying_role(db: Database, owner: Optional[str]) -> None:
    """Establish the role that applies the next chunk.

    Shared with the ``sync`` rebuild passes (``rebuild.role_switcher``), which
    re-apply the same chunk content and must use the same role, so an object they
    recreate is born with the owner the declarative build would have given it.

    Re-established at the *top of every chunk* (not once for the whole build) so a
    mid-chunk failure can't silently carry a role forward into the next chunk.
    ``owner=None`` runs as the connector (superuser) for foundational DDL.

    ``SET ROLE`` belongs to one connection, but ``run_fixtures`` commits per
    statement and so may be handed a different pooled connection for the next
    one; the role is therefore also pinned to the engine (``pin_role``), so every
    connection it hands out takes it. The direct ``SET ROLE`` covers the
    connection the session may already hold, or is bound to in a transaction.
    """
    if not owner:
        pin_role(db.engine, None)
        db.run_sql("RESET ROLE", raise_errors=True)
        return
    db.run_sql("SET ROLE {role}", dict(role=Identifier(owner)), raise_errors=True)
    # After the direct `SET ROLE`, so a role the connector can't take is refused
    # before anything is pinned.
    pin_role(db.engine, owner)


# SQLSTATE 42501 (insufficient_privilege) — the application role can't do this, so
# the statement belongs to the connector.
_INSUFFICIENT_PRIVILEGE = "42501"


def _is_privilege_error(err: Exception) -> bool:
    """Whether ``err`` is Postgres refusing the statement to the current role.

    Read from the driver exception SQLAlchemy wraps (``.orig``); psycopg 3 spells
    the code ``sqlstate`` and psycopg 2 ``pgcode``, and both drivers are installed.
    """
    orig = getattr(err, "orig", err)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code == _INSUFFICIENT_PRIVILEGE


@contextmanager
def applied_as_owner(db: Database, owner: str):
    """Run a block's DDL as ``owner``, yielding an ``on_error`` hook to pass on.

    The hook re-runs a statement the application role was refused as the
    connector, then restores the role for the rest of the plan. It runs the
    statement itself rather than returning ``RESET ROLE`` / statement / ``SET
    ROLE`` directives: each directive commits on its own, and the role is pinned
    to every connection the engine hands out (``set_applying_role``), so a reset
    issued as a separate statement would not carry over to the next one.

    Yields ``None`` -- leaving the session as the connector, i.e. the previous
    behaviour -- when the connector isn't a member of ``owner`` and so can't
    take the role at all.
    """
    try:
        set_applying_role(db, owner)
    except Exception as err:  # noqa: BLE001 — degrade to the connector, don't fail
        log.warning(
            "Could not apply as %s (%s); running as the connector instead.",
            owner,
            err,
        )
        yield None
        return

    def escalate(ctx, err, connectable):
        if not _is_privilege_error(err):
            return None  # not ours to handle — fall through to normal handling
        log.info("Statement needs the connector's privileges: %s", ctx.sql_text[:100])
        set_applying_role(db, None)
        try:
            db.run_sql(ctx.query, ctx.params, raise_errors=True)
        except Exception as retry_err:  # noqa: BLE001 — reported as the original
            log.error("Statement failed as the connector too: %s", retry_err)
            return None
        finally:
            set_applying_role(db, owner)
        return []  # handled: nothing further to run

    try:
        yield escalate
    finally:
        pin_role(db.engine, None)
        db.run_sql("RESET ROLE", raise_errors=False)
