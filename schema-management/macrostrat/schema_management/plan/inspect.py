"""Reading a database's structure, limited to the schemas being compared."""

from results.schemainspect.pg import PostgreSQL
from results.schemainspect.pg.obj import PROPS
from sqlalchemy import TextClause, text

from macrostrat.database import Database

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
