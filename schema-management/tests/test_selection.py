"""Object selection for `schema sync --object`: what each statement names, and matching."""

from pytest import fixture, mark

from macrostrat.schema_management import SchemaDefinition
from macrostrat.schema_management.grants import grant_objects
from macrostrat.schema_management.procedures import procedure_objects
from macrostrat.schema_management.seed_data import data_objects
from macrostrat.schema_management.selection import ObjectName, ObjectSelector
from macrostrat.schema_management.views import rebuild_views, view_objects


@mark.parametrize(
    "patterns, objects, expected",
    [
        (["maps"], [ObjectName("maps", "sources")], True),
        (["maps"], [ObjectName("maps")], True),
        (["maps"], [ObjectName("carto", "sources")], False),
        (["maps.sources"], [ObjectName("maps", "sources")], True),
        (["Maps.Sources"], [ObjectName("maps", "sources")], True),
        (["maps.legend_*"], [ObjectName("maps", "legend_lookup")], True),
        (["map*.sources"], [ObjectName("maps", "sources")], True),
        (["maps.sources"], [ObjectName("maps")], False),
        (["carto", "maps.sources"], [ObjectName("maps", "sources")], True),
        (["maps"], [], False),
    ],
)
def test_selector_matches(patterns, objects, expected):
    assert ObjectSelector.parse(patterns).matches(objects) is expected


def test_procedure_and_view_objects():
    assert procedure_objects(
        'CREATE FUNCTION "maps".f(_a integer) RETURNS int AS $$x$$'
    ) == [ObjectName("maps", "f")]
    assert view_objects("CREATE OR REPLACE VIEW macrostrat_api.cols AS SELECT 1") == [
        ObjectName("macrostrat_api", "cols")
    ]


@mark.parametrize(
    "stmt, expected",
    [
        (
            "INSERT INTO maps.sources (slug) VALUES ('a')"
            " ON CONFLICT (slug) DO UPDATE SET name = excluded.name",
            [ObjectName("maps", "sources")],
        ),
        (
            "UPDATE maps.sources s SET scale = v.scale FROM (VALUES ('a', 'b')) v",
            [ObjectName("maps", "sources")],
        ),
        (
            "WITH x AS (SELECT 1) INSERT INTO map_bounds.compilation (source_id)"
            " SELECT source_id FROM maps.sources",
            [ObjectName("map_bounds", "compilation")],
        ),
        ("DELETE FROM ONLY a.b WHERE true", [ObjectName("a", "b")]),
        (
            "SELECT setval('user_features.location_tags_id_seq',"
            " (SELECT max(id) FROM user_features.location_tags))",
            [
                ObjectName("user_features", "location_tags_id_seq"),
                ObjectName("user_features", "location_tags"),
            ],
        ),
    ],
)
def test_data_objects(stmt, expected):
    assert data_objects(stmt) == expected


@mark.parametrize(
    "stmt, expected",
    [
        (
            "GRANT SELECT ON TABLE macrostrat_api.cols TO web_anon",
            [ObjectName("macrostrat_api", "cols")],
        ),
        (
            "GRANT SELECT, UPDATE ON a.b, a.c TO web_user",
            [ObjectName("a", "b"), ObjectName("a", "c")],
        ),
        (
            "GRANT EXECUTE ON FUNCTION public.f(integer, text) TO web_anon",
            [ObjectName("public", "f")],
        ),
        ("GRANT SELECT (a, b) ON a.t TO web_anon", [ObjectName("a", "t")]),
        (
            "GRANT SELECT ON spatial_ref_sys TO web_anon",
            [ObjectName("public", "spatial_ref_sys")],
        ),
        ("REVOKE ALL ON a.t FROM PUBLIC", [ObjectName("a", "t")]),
        (
            "GRANT USAGE ON SCHEMA maps, carto TO web_anon",
            [ObjectName("maps"), ObjectName("carto")],
        ),
        ("GRANT SELECT ON ALL TABLES IN SCHEMA maps TO web_anon", [ObjectName("maps")]),
        (
            "ALTER DEFAULT PRIVILEGES FOR ROLE macrostrat IN SCHEMA maps"
            " GRANT SELECT ON TABLES TO web_anon",
            [ObjectName("maps")],
        ),
        ("ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO web_anon", []),
        ("GRANT CONNECT ON DATABASE macrostrat TO web_anon", []),
        ("GRANT web_anon TO web_user", []),
        ("CREATE ROLE web_anon NOLOGIN", []),
    ],
)
def test_grant_objects(stmt, expected):
    assert grant_objects(stmt) == expected


# --- a selected view's recreate cascades past the selection (database) ----

SCHEMA = "sync_selection_test"
