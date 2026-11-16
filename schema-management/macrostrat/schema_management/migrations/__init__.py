"""Condition-checked migrations, applied in dependency order.

A migration names what must hold before it can run (preconditions) and what
holds once it has (postconditions), so whether it has applied is read from the
database rather than from a ledger. The runner is handed everything specific to
an application: the database, the migrations, the class of environment, the role
that owns what migrations create, and what to do once they have applied.
"""

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
from .loader import load_migrations
from .migration import (
    INHERIT,
    ApplicationStatus,
    Migration,
    ReadinessState,
    concrete_migrations,
    order_migrations,
    source_location,
)
from .runner import MigrationResult, MigrationRunner, MigrationState
