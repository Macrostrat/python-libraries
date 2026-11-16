"""Tests for seed-data detection and re-application."""

from pytest import raises
from sqlalchemy.exc import IntegrityError

from macrostrat.schema_management.sync.seed_data import (
    _is_non_idempotent_insert,
    data_statements_in,
    rebuild_seed_data,
)


def test_seed_statements_in_detects_data_dml_only():
    sql = """
    CREATE TABLE s.t (id int);
    INSERT INTO s.t (id) VALUES (1) ON CONFLICT DO NOTHING;
    UPDATE s.t SET x = 1 WHERE id = 1;
    WITH cte AS (SELECT 1 AS id)
        INSERT INTO s.t (id) SELECT id FROM cte ON CONFLICT (id) DO NOTHING;
    WITH cte AS (SELECT 1) SELECT * FROM cte;   -- read-only, not seed
    DELETE FROM s.t WHERE id = 99;
    CREATE VIEW s.v AS SELECT 1;
    GRANT SELECT ON s.t TO web_anon;
    """
    found = list(data_statements_in(sql))
    # INSERT, UPDATE, WITH…INSERT, DELETE — but not WITH…SELECT, CREATE*, GRANT.
    assert len(found) == 4
    assert any(f.upper().startswith("WITH") for f in found)  # WITH … INSERT swept in
    assert not any(f.upper().startswith(("CREATE", "GRANT")) for f in found)


def test_setval_selects_are_swept_in_but_plain_selects_are_not():
    """`SELECT setval(…)` writes, even though sqlparse types it as a SELECT."""
    sql = """
    INSERT INTO s.t (id) VALUES (1) ON CONFLICT DO NOTHING;
    SELECT setval('s.t_id_seq', (SELECT max(id) FROM s.t));
    SELECT pg_catalog.setval('s.other_seq', 10);
    SELECT max(id) FROM s.t;   -- read-only, not seed
    """
    found = list(data_statements_in(sql))
    assert len(found) == 3
    assert sum("setval" in f for f in found) == 2
    assert not any(f.strip().upper().startswith("SELECT MAX") for f in found)


def test_non_idempotent_insert_detection():
    assert _is_non_idempotent_insert("INSERT INTO s.t (id) VALUES (1)") is True
    assert (
        _is_non_idempotent_insert(
            "INSERT INTO s.t (id) VALUES (1) ON CONFLICT DO NOTHING"
        )
        is False
    )
    # WITH … INSERT without ON CONFLICT is still flagged.
    assert (
        _is_non_idempotent_insert("WITH c AS (SELECT 1) INSERT INTO s.t SELECT 1")
        is True
    )
    # Non-INSERT DML isn't an ON CONFLICT concern.
    assert _is_non_idempotent_insert("UPDATE s.t SET x = 1") is False
