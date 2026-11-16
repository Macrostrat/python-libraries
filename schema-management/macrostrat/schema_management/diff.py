"""Diff a database against its declarative schema, and apply the difference.

The declarative schema is built into a scratch database and compared with the
live one; the statements that would bring the live database into line are the
*plan*. Changes that could lose data are counted apart and skipped by default.
"""

import re
from typing import Callable, Optional

from results import db as results_db
from results.dbdiff import Migration as DiffMigration
from results.dbdiff.statements import check_for_drop
from results.schemainspect.pg import PostgreSQL
from results.schemainspect.pg.obj import PROPS
from rich import print
from sqlalchemy import TextClause, text
from sqlparse import format as format_sql

from macrostrat.database import Database
from macrostrat.database.transfer.utils import raw_database_url


def is_unsafe_statement(s: str) -> bool:
    """Check if a SQL statement is unsafe (i.e., contains DROP)"""
    is_drop = check_for_drop(s)
    if not is_drop:
        return False
    allowed_drops = [
        "drop view",
        "drop index",
        "drop function",
        "drop trigger",
    ]
    s_lower = s.lower()
    for allowed in allowed_drops:
        if s_lower.startswith(allowed):
            return False
    return True


# Each of these catalog queries skips system schemas with a `not in (…)` list
# holding this pair, which other schemas can be added to.
SYSTEM_SCHEMAS = "'information_schema', 'pg_toast'"
SCHEMA_FILTERED_QUERIES = (
    "ALL_RELATIONS_QUERY",
    "COLLATIONS_QUERY",
    "CONSTRAINTS_QUERY",
    # Both ends of a dependency, so none names an object left unloaded.
    "DEPS_QUERY",
    "ENUMS_QUERY",
    "FUNCTIONS_QUERY",
    "INDEXES_QUERY",
    "PRIVILEGES_QUERY",
    "SCHEMAS_QUERY",
    "SEQUENCES_QUERY",
)


# schemainspect finds the owner in `pg_tables`, which lacks views, so view grants vanished.
PRIVILEGES_QUERY = f"""
select
  g.table_schema as schema,
  g.table_name as name,
  'table' as object_type,
  g.grantee as user,
  g.privilege_type as privilege
from information_schema.role_table_grants g
join pg_catalog.pg_namespace n on n.nspname = g.table_schema
join pg_catalog.pg_class c on c.relnamespace = n.oid and c.relname = g.table_name
where g.grantee <> pg_catalog.pg_get_userbyid(c.relowner)
  and g.table_schema not in ('pg_internal', 'pg_catalog', {SYSTEM_SCHEMAS})
  and g.table_schema not like 'pg_temp_%' and g.table_schema not like 'pg_toast_temp_%'
order by schema, name, user
"""


def _add_to_system_schemas(query, names: str):
    # Over a SQLAlchemy connection, the inspector's queries are `text()` clauses.
    if isinstance(query, TextClause):
        return text(_add_to_system_schemas(query.text, names))
    return query.replace(SYSTEM_SCHEMAS, f"{SYSTEM_SCHEMAS}, {names}")


# The stock query assumes types are on the search path: it drops the rest as not
# "visible", and `format_type` would schema-qualify their names a second time.
TYPES_QUERY_FIXES = {
    "AND pg_catalog.pg_type_is_visible ( t.oid )": "",
    "pg_catalog.format_type (t.oid, NULL) AS name": "t.typname AS name",
}


def _types_off_search_path(query):
    if isinstance(query, TextClause):
        return text(_types_off_search_path(query.text))
    for old, new in TYPES_QUERY_FIXES.items():
        query = query.replace(old, new)
    return query


class SchemaInspector(PostgreSQL):
    included_schemas = ["public"]

    def __init__(self, conn, schemas=None):
        # Read by `load_all`, which the parent constructor calls.
        if schemas is not None:
            self.included_schemas = schemas
        super().__init__(conn)

    def load_all(self):
        # Large unmanaged schemas can dominate the catalog; never read them.
        query = (
            "SELECT nspname FROM pg_catalog.pg_namespace"
            " WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema'"
        )
        if self.connection_type == "sqlalchemy":
            query = text(query)
        present = [r.nspname for r in self.execute(query)]
        self.PRIVILEGES_QUERY = PRIVILEGES_QUERY
        if self.connection_type == "sqlalchemy":
            self.PRIVILEGES_QUERY = text(PRIVILEGES_QUERY)
        self.TYPES_QUERY = _types_off_search_path(self.TYPES_QUERY)
        self.exclude_schemas([s for s in present if s not in self.included_schemas])
        super().load_all()

    def exclude_schemas(self, schemas):
        if not schemas:
            return
        names = ", ".join("'" + s.replace("'", "''") + "'" for s in schemas)
        for attr in SCHEMA_FILTERED_QUERIES:
            setattr(self, attr, _add_to_system_schemas(getattr(self, attr), names))

    def filter_schema(self, schema=None, exclude_schema=None):
        # Types are not in PROPS; the visibility filter used to bound them instead.
        for prop in PROPS.split() + ["types"]:
            att = getattr(self, prop)
            filtered = {
                k: v for k, v in att.items() if v.schema in self.included_schemas
            }
            setattr(self, prop, filtered)


def get_inspector(x, schemas=None):
    if hasattr(x, "url") and hasattr(x, "URL"):
        with x.t() as t:
            inspected = SchemaInspector(t.c, schemas)
    else:
        try:
            inspected = SchemaInspector(x.c, schemas)
        except AttributeError:
            inspected = SchemaInspector(x, schemas)
    inspected.filter_schema()
    return inspected


def get_all_schemas(db: Database, excluded_schemas=None):
    _excluded = [
        "information_schema",
        "pg_catalog",
        "pg_toast",
    ]
    if excluded_schemas is not None:
        _excluded.extend(excluded_schemas)
    return (
        db.run_query(
            """
        SELECT nspname AS schema_name FROM pg_catalog.pg_namespace
        WHERE NOT nspname = ANY(:excluded_schemas)
        AND nspname NOT LIKE 'pg_temp_%'
        AND nspname NOT LIKE 'pg_toast_%'
        """,
            {"excluded_schemas": _excluded},
        )
        .scalars()
        .all()
    )


_ROLE_STATEMENT = re.compile(r"\s*(SET|RESET)\s+ROLE\b", re.IGNORECASE)


class StatementCounter:
    def __init__(self, safe: bool = True):
        self.total = 0
        self.unsafe = 0
        self.safe = safe
        self.statements = []

    def filter(self, s, params):
        # The plan's own `SET ROLE` / `RESET ROLE`, for applying it by hand: `apply`
        # manages the role itself (`applied_as_owner`), and they are not changes.
        if _ROLE_STATEMENT.match(s):
            return False
        self.total += 1
        if is_unsafe_statement(s):
            self.unsafe += 1
            if self.safe:
                return False
        self.statements.append((s, params))
        return True

    def print_report(self, file=None, prefix=""):
        applied_count = self.total
        _unsafe_action = "applied"
        unsafe_suffix = ""
        if self.safe:
            applied_count -= self.unsafe
            _unsafe_action = "skipped"
        elif self.unsafe > 0:
            unsafe_suffix = f" ([red bold]{self.unsafe} unsafe[/])"

        n_text = _count(applied_count, "change")

        print(
            prefix + f"[dim]{n_text} applied" + unsafe_suffix,
            file=file,
        )
        if self.unsafe > 0 and self.safe:
            n_text = _count(self.unsafe, "unsafe change")
            print(
                prefix + f"[red bold]{n_text} skipped",
                file=file,
            )

    def schema_log_entries(self):
        return [s for s, p in self.statements if should_log_statement(s)]


def _count(count, label):
    plural = "" if count == 1 else "s"
    return f"{count} {label}{plural}"


def should_log_statement(s):
    """Filter out uninteresting statements for schema change logging"""
    s_lower = s.lower()
    uninteresting_starts = [
        "set",
        "notify",
        "alter role",
        "comment on",
        "create index",
        "drop index",
        "create view",
        "drop view",
        "create or replace view",
        "create or replace function",
        "create function",
        "drop function",
        "grant",
        "revoke",
    ]
    for start in uninteresting_starts:
        if s_lower.startswith(start + " "):
            return False
    return True


def _results_db(db: Database) -> results_db:
    # `results` does not tolerate a driver suffix such as +psycopg.
    url = db.engine.url.set(drivername="postgresql")
    return results_db(raw_database_url(url))


def plan_changes(
    current: Database,
    target: Database,
    *,
    excluded_schemas: Optional[list[str]] = None,
    prepare: Optional[Callable] = None,
    privileges: bool = True,
) -> list[str]:
    """Statements that bring ``current`` into line with ``target``.

    Every schema of ``target`` outside ``excluded_schemas`` is compared.
    ``prepare`` receives the diff's ``changes`` before statements are generated,
    to drop objects that cannot be diffed meaningfully. Statements are returned
    unfiltered; see :func:`is_unsafe_statement`.
    """
    schemas = get_all_schemas(target, excluded_schemas=excluded_schemas)
    m = DiffMigration(
        get_inspector(_results_db(current), schemas),
        get_inspector(_results_db(target), schemas),
    )
    m.changes.ignore_extension_versions = True
    if prepare is not None:
        prepare(m.changes)
    m.set_safety(False)
    m.add_all_changes(privileges=privileges)
    return list(m.statements)


def format_plan(statements: list[str], *, owner: Optional[str] = None) -> str:
    """A plan file's text. With ``owner``, it names the role it is applied as."""
    formatted = [
        format_sql(s, reindent_aligned=True, compact=True, keyword_case="upper")
        for s in statements
    ]
    if formatted and owner is not None:
        # No colons: SQLAlchemy reads one before a word as a bind parameter.
        header = (
            f"-- Applied as {owner}, so what it creates is owned as the declarative\n"
            "-- build owns it. Applied by hand, re-run any statement refused here as\n"
            "-- the connecting role.\n"
            f"SET ROLE {owner};\n"
        )
        formatted = [header, *formatted, "RESET ROLE;"]
    out = ""
    for statement in formatted:
        out += statement + "\n"
        if "\n" in statement:
            out += "\n"
    return out
