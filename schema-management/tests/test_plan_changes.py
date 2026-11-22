"""Planning inspects each database through its own engine's connection settings."""

from contextlib import ExitStack

from pytest import fixture
from sqlalchemy.engine import make_url

from macrostrat.database import Database
from macrostrat.database.utils import temporary_database
from macrostrat.schema_management.plan import plan_changes


@fixture
def urls(database_url):
    names = ["plan_changes_empty", "plan_changes_populated"]
    with ExitStack() as stack:
        engines = [
            stack.enter_context(
                temporary_database(
                    make_url(str(database_url)).set(database=name), force_drop=True
                )
            )
            for name in names
        ]
        Database(engines[1]).run_sql(
            "CREATE SCHEMA s; CREATE TABLE s.t (id integer)", raise_errors=True
        )
        yield [e.url for e in engines]


def _plan(current_url, **target_kwargs):
    current = Database(current_url)
    target = Database(**target_kwargs)
    try:
        return plan_changes(current, target)
    finally:
        current.engine.dispose()
        target.engine.dispose()


def _creates_table(statements):
    return any('create table "s"."t"' in s for s in statements)


def test_inspection_uses_connect_args(urls):
    empty, populated = urls
    # The URL names the empty database; only connect_args reach the populated one.
    statements = _plan(
        empty,
        db_conn=empty,
        connect_args={"dbname": populated.database, "options": "-c search_path=s"},
    )
    assert _creates_table(statements)


def test_inspection_accepts_url_options_with_spaces(urls):
    empty, populated = urls
    url = populated.update_query_dict({"options": "-c search_path=s"})
    assert _creates_table(_plan(empty, db_conn=url))
