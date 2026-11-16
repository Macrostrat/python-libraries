"""Declarative schemas, schema diffs and condition-checked migrations.

The approach Macrostrat's database is managed with, free of Macrostrat's own
configuration so that other applications can adopt it. An application describes
itself with a :class:`SchemaApp`; `create_schema_cli` turns that into commands.
"""

from .app import SchemaApp, reload_postgrest_schema, scratch_database
from .composer import build_schema, dependency_closure, order_chunks, select_chunks
from .conditions import (
    DBCallable,
    DbEvaluator,
    _any,
    _not,
    column_type_is,
    custom_type_exists,
    describe_condition,
    exists,
    has_columns,
    has_fks,
    has_schema,
    has_table_privilege,
    not_exists,
    schema_exists,
    view_exists,
)
from .definition import SchemaDefinition, apply_sql_path, sql_files
from .discovery import discover_chunks, parse_frontmatter
from .environment import EnvironmentClass, classes_up_to, environment_class
from .loader import load_migrations
from .migrations import (
    INHERIT,
    ApplicationStatus,
    Migration,
    MigrationResult,
    MigrationRunner,
    MigrationState,
    ReadinessState,
    concrete_migrations,
)
from .roles import applied_as_owner, pin_role, set_applying_role

any_of = _any
negate = _not
