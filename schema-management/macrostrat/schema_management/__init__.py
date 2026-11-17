"""Declarative schemas, schema diffs and condition-checked migrations.

The approach Macrostrat's database is managed with, free of Macrostrat's own
configuration so that other applications can adopt it. An application describes
itself with a :class:`SchemaApp`; `create_schema_cli` turns that into commands.

The parts, each a subpackage:

- ``chunks``: the schema as it should be, in dependency-ordered pieces
- ``plan``: the difference between that and a live database
- ``sync``: re-applying what a diff cannot manage
- ``migrations``: changes that neither can express
- ``database``: roles, environment classes and throwaway clusters
- ``testing``: helpers for an application's tests
"""

from .app import SchemaApp, reload_postgrest_schema, scratch_database
from .chunks import (
    SchemaDefinition,
    apply_sql_path,
    build_schema,
    dependency_closure,
    discover_chunks,
    order_chunks,
    parse_frontmatter,
    select_chunks,
    sql_files,
)
from .database import (
    EnvironmentClass,
    applied_as_owner,
    classes_up_to,
    environment_class,
    pin_role,
    set_applying_role,
)
from .migrations import (
    INHERIT,
    ApplicationStatus,
    DBCallable,
    DbEvaluator,
    Migration,
    MigrationResult,
    MigrationRunner,
    MigrationState,
    ReadinessState,
    _any,
    _not,
    column_type_is,
    concrete_migrations,
    custom_type_exists,
    describe_condition,
    exists,
    has_columns,
    has_fks,
    has_schema,
    has_table_privilege,
    load_migrations,
    not_exists,
    schema_exists,
    view_exists,
)

any_of = _any
negate = _not
