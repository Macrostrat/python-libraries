"""A small application's schema, taken through build, plan, apply, sync and migrate.

Runs against a real PostgreSQL (the repository's `database_url` fixture).
"""

from contextlib import contextmanager

from pytest import fixture
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from macrostrat.database import Database
from macrostrat.database.utils import temporary_database as temp_database
from macrostrat.schema_management import (
    Migration,
    SchemaApp,
    SchemaDefinition,
    exists,
    has_columns,
)
from macrostrat.schema_management.cli import create_schema_cli

TABLES = """
CREATE SCHEMA IF NOT EXISTS field;
CREATE TABLE IF NOT EXISTS field.checkin (
  id serial PRIMARY KEY,
  notes text
);
"""

VIEWS = """
-- @subsystem: api
-- @depends-on: tables
CREATE OR REPLACE VIEW field.checkin_summary AS
SELECT id, notes FROM field.checkin;
"""


class LegacyCheckins(Migration):
    """The table as an older deployment has it, lacking `rating`."""

    name = "checkin-rating"
    subsystem = "field"
    readiness_state = "ga"
    owner = None
    preconditions = [exists("field", "checkin")]
    postconditions = [has_columns("field", "checkin", "rating")]

    def apply(self, db):
        db.run_sql("ALTER TABLE field.checkin ADD COLUMN rating integer")


class NotReady(Migration):
    name = "experimental"
    subsystem = "field"
    readiness_state = "alpha"
    owner = None
    preconditions = [exists("field", "checkin")]
    postconditions = [exists("field", "experiment")]

    def apply(self, db):
        db.run_sql("CREATE TABLE field.experiment ()")


def _url(database_url, name):
    return make_url(str(database_url)).set(database=name)


@fixture
def app(database_url, tmp_path):
    definitions = tmp_path / "definitions"
    definitions.mkdir()
    (definitions / "tables.sql").write_text(TABLES)
    (definitions / "views.sql").write_text(VIEWS)

    chunks = [
        SchemaDefinition(name="tables", provides=[definitions / "tables.sql"]),
        SchemaDefinition(
            name="api", depends_on=["tables"], provides=[definitions / "views.sql"]
        ),
    ]

    @contextmanager
    def planning_database():
        with temp_database(
            _url(database_url, "schema_management_plan"), force_drop=True
        ) as engine:
            db = Database(engine.url)
            yield db
            db.engine.dispose()

    with temp_database(
        _url(database_url, "schema_management_app"), force_drop=True
    ) as engine:
        db = Database(engine.url)
        yield SchemaApp(
            name="fieldapp",
            database=lambda: db,
            chunks=chunks,
            schema_dir=tmp_path,
            migrations=[LegacyCheckins, NotReady],
            environment="testing",
            env_class="production",
            planning_database=planning_database,
            source_root=tmp_path,
        )
        db.engine.dispose()


def test_plan_apply_brings_an_empty_database_into_line(app):
    db = app.database()
    assert not db.inspector.has_table("checkin", schema="field")

    plan_file, statements = app.write_plan()
    assert any("checkin" in s for s in statements)
    assert plan_file.name == "testing.plan.sql"

    counter = app.apply_plan()
    assert counter.unsafe == 0
    db.refresh_schema()
    assert db.inspector.has_table("checkin", schema="field")
    assert "checkin_summary" in db.inspector.get_view_names("field")

    # Nothing left to do.
    assert app.plan() == []


def test_unsafe_statements_are_skipped_unless_asked(app):
    db = app.database()
    app.provision()
    db.run_sql("CREATE TABLE field.stray (id integer)")

    _, statements = app.write_plan()
    assert any("stray" in s.lower() for s in statements)

    counter = app.apply_plan()
    assert counter.unsafe == 1
    db.refresh_schema()
    assert db.inspector.has_table("stray", schema="field")

    app.apply_plan(safe=False)
    db.refresh_schema()
    assert not db.inspector.has_table("stray", schema="field")


def test_sync_restores_a_changed_view(app):
    db = app.database()
    app.provision()
    db.run_sql(
        "CREATE OR REPLACE VIEW field.checkin_summary AS SELECT 1 AS id, 'x'::text AS notes"
    )
    report = app.sync()
    assert report.views.replaced >= 1
    definition = db.run_query(
        "SELECT pg_get_viewdef('field.checkin_summary'::regclass)"
    ).scalar()
    assert "checkin" in definition


def test_migrations_apply_by_condition_and_readiness(app):
    db = app.database()
    runner = app.runner()

    # Preconditions unmet on an empty database.
    assert runner.applyable() == set()

    app.provision()
    db.refresh_schema()
    assert runner.applyable() == {"checkin-rating", "experimental"}

    n_applied, completed = runner.run(apply=True)
    # `alpha` does not apply in a production-class environment.
    assert n_applied == 1
    assert completed == {"checkin-rating"}
    assert runner.has_been_run("checkin-rating")
    assert not runner.has_been_run("experimental")

    # The declarative schema lacks the column, so the diff would drop it.
    assert any("rating" in s for s in app.plan())


def test_cli_lists_migrations_and_graph(app):
    cli = create_schema_cli(lambda: app)
    runner = CliRunner()

    res = runner.invoke(cli, ["graph"])
    assert res.exit_code == 0, res.output
    assert "tables" in res.output and "api" in res.output

    res = runner.invoke(cli, ["migrate"])
    assert res.exit_code == 0, res.output
    assert "checkin-rating" in res.output
    assert "run with --apply" in res.output

    res = runner.invoke(cli, ["provision"])
    assert res.exit_code == 0, res.output
    res = runner.invoke(cli, ["migrate", "--apply"])
    assert res.exit_code == 0, res.output
    assert app.runner().has_been_run("checkin-rating")


def test_authorize_can_refuse_writes(app):
    def refuse(action, assume_yes):
        raise PermissionError(action)

    app.authorize = refuse
    try:
        app.provision()
    except PermissionError as err:
        assert "provisioning" in str(err)
    else:
        raise AssertionError("write was not refused")
    # Listing migrations needs no authorization.
    app.runner().run()
