"""Procedure rebuild: statement parsing, and recreating what CREATE OR REPLACE refuses."""

from pytest import fixture, mark

from macrostrat.schema_management import SchemaDefinition
from macrostrat.schema_management.sync.procedures import (
    _as_create_or_replace,
    procedure_statements_in,
    rebuild_procedures,
)


def test_procedure_statements_in_filters():
    sql = """
    CREATE TABLE s.t (id int);
    CREATE FUNCTION s.f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;
    CREATE OR REPLACE PROCEDURE s.p() LANGUAGE sql AS $$ SELECT 1 $$;
    CREATE VIEW s.v AS SELECT 1;
    GRANT SELECT ON s.v TO web_anon;
    """
    found = list(procedure_statements_in(sql))
    assert len(found) == 2
    assert found[0].upper().startswith("CREATE FUNCTION")
    assert found[1].upper().startswith("CREATE OR REPLACE PROCEDURE")


@mark.parametrize(
    "stmt, expected_prefix",
    [
        ("CREATE FUNCTION s.f() RETURNS int AS $$x$$", "CREATE OR REPLACE FUNCTION"),
        ("create   procedure s.p() AS $$x$$", "CREATE OR REPLACE PROCEDURE"),
        ("CREATE OR REPLACE FUNCTION s.f() AS $$x$$", "CREATE OR REPLACE FUNCTION"),
    ],
)
def test_as_create_or_replace(stmt, expected_prefix):
    out = _as_create_or_replace(stmt)
    assert out.upper().startswith(expected_prefix)
    # Idempotent — never doubles the clause, body preserved.
    assert out.upper().count("OR REPLACE") == 1
    assert out.rstrip().endswith("$$x$$")


# --- drop-and-recreate (database) -----------------------------------------

SCHEMA = "sync_recreate_test"


def _sync(db, tmp_path, sql: str):
    (tmp_path / "f.sql").write_text(sql)
    chunk = SchemaDefinition(name="t", provides=[tmp_path])
    return rebuild_procedures(db, [chunk])


def _scalar(db, sql: str):
    return db.run_query(sql).scalar()
