"""The statements that bring a database into line with its declarative schema."""

from typing import Callable, Optional

from results.dbdiff import Migration as DiffMigration
from sqlparse import format as format_sql

from macrostrat.database import Database

from .inspect import SchemaInspector, get_all_schemas, get_inspector


def _inspect(db: Database, schemas: list[str]) -> SchemaInspector:
    # Connect through the engine so its connect_args and connect events apply.
    conn = db.engine.raw_connection()
    try:
        return get_inspector(conn.cursor(), schemas)
    finally:
        conn.rollback()
        conn.close()


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
        _inspect(current, schemas),
        _inspect(target, schemas),
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
