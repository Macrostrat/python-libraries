"""Mocked (no-database) tests for the schema composer.

These use a fake ``Database`` that just records which fixture paths would be
applied, so chunk ordering and target-closure selection can be verified quickly
without Docker.
"""

from pathlib import Path

from pytest import raises
from sqlalchemy import create_engine

from macrostrat.schema_management import (
    EnvironmentClass,
    SchemaApp,
    SchemaDefinition,
    build_schema,
    classes_up_to,
    dependency_closure,
    select_chunks,
)
from macrostrat.schema_management.roles import _role_pins


def _chunks(root: Path) -> list[SchemaDefinition]:
    """public -> core -> api, plus a development-only layer, as files on disk."""
    files = {
        "public": ["0001-public.sql"],
        "core": ["01-main.sql", "02-more.sql"],
        "api": ["views.sql"],
        "dev": ["seed.sql"],
    }
    for name, names in files.items():
        (root / name).mkdir()
        for f in names:
            (root / name / f).write_text("SELECT 1;")
    return [
        SchemaDefinition(name="api", depends_on=["core"], provides=[root / "api"]),
        SchemaDefinition(name="public", provides=[root / "public"]),
        SchemaDefinition(name="core", depends_on=["public"], provides=[root / "core"]),
        SchemaDefinition(
            name="dev",
            depends_on=["api"],
            provides=[root / "dev"],
            environments=classes_up_to(EnvironmentClass.Development),
        ),
    ]


class FakeDB:
    """Records applied fixture paths and role changes instead of touching a database."""

    def __init__(self):
        self.applied: list[Path] = []
        # Sequence of role directives issued by build_schema, as raw SQL strings.
        self.roles: list[str] = []
        # Never connects; only carries the pool listeners `pin_role` installs.
        self.engine = create_engine("postgresql+psycopg://")

    def run_fixtures(self, fixtures, **kwargs):
        self.applied.extend(fixtures)

    def run_sql(self, sql, params=None, **kwargs):
        # build_schema only issues SET ROLE / RESET ROLE through this path.
        self.roles.append(sql)


def _names(db: FakeDB) -> list[str]:
    return [p.name for p in db.applied]


def test_target_closure_builds_only_subsystem_and_deps(tmp_path):
    db = FakeDB()
    build_schema(db, _chunks(tmp_path), target="core")
    assert _names(db) == ["0001-public.sql", "01-main.sql", "02-more.sql"]


def test_application_order_follows_dependency_chain(tmp_path):
    db = FakeDB()
    build_schema(db, _chunks(tmp_path))
    names = _names(db)
    assert names.index("0001-public.sql") < names.index("01-main.sql")
    assert names.index("02-more.sql") < names.index("views.sql")
    assert names[-1] == "seed.sql"


def test_select_chunks_no_dependents(tmp_path):
    chunks = _chunks(tmp_path)
    assert [c.name for c in select_chunks(chunks, target="api")] == [
        "public",
        "core",
        "api",
    ]
    only = select_chunks(chunks, target="api", no_dependents=True)
    assert [c.name for c in only] == ["api"]


def test_schema_app_filters_chunks_by_environment_class(tmp_path):
    chunks = _chunks(tmp_path)
    app = SchemaApp(name="t", database=FakeDB, chunks=chunks, env_class="production")
    assert "dev" not in [c.name for c in app.environment_chunks()]
    app = SchemaApp(name="t", database=FakeDB, chunks=chunks, env_class="local")
    assert "dev" in [c.name for c in app.environment_chunks()]
    # An unrecognized class fails closed to production.
    app = SchemaApp(name="t", database=FakeDB, chunks=chunks, env_class="nonsense")
    assert app.env_class == EnvironmentClass.Production


def test_build_schema_sets_role_per_chunk():
    """Each chunk applies under its owner (SET ROLE); a foundational chunk applies
    as the connector (RESET ROLE); and the session is always reset at the end."""
    chunks = [
        SchemaDefinition(name="public", owner=None),
        SchemaDefinition(name="app", depends_on=["public"], owner="macrostrat"),
    ]
    db = FakeDB()
    build_schema(db, chunks)
    # public (owner=None) → RESET, app (owner=macrostrat) → SET ROLE, final RESET.
    assert db.roles == ["RESET ROLE", "SET ROLE {role}", "RESET ROLE"]


def test_build_schema_pins_the_role_to_the_engine_while_a_chunk_applies():
    """Every pooled connection takes the chunk's role, not just the one in hand,
    and no pin outlives the build."""
    seen = []
    chunks = [
        SchemaDefinition(
            name="app",
            owner="macrostrat",
            provides=[lambda db: seen.append(_role_pins[db.engine].role)],
        )
    ]
    db = FakeDB()
    build_schema(db, chunks)
    assert seen == ["macrostrat"]
    assert db.engine not in _role_pins


def test_build_schema_resets_role_even_when_a_chunk_fails():
    """A mid-build failure still resets the session (the finally clause)."""

    def boom(_db):
        raise RuntimeError("chunk exploded")

    chunks = [SchemaDefinition(name="app", owner="macrostrat", provides=[boom])]
    db = FakeDB()
    with raises(RuntimeError, match="exploded"):
        build_schema(db, chunks)
    assert db.roles == ["SET ROLE {role}", "RESET ROLE"]
    assert db.engine not in _role_pins


def test_dependency_closure(tmp_path):
    chunks = _chunks(tmp_path)
    assert dependency_closure(chunks, "core") == {"public", "core"}


def test_dependency_closure_unknown_target_raises(tmp_path):
    with raises(ValueError):
        dependency_closure(_chunks(tmp_path), "does-not-exist")
