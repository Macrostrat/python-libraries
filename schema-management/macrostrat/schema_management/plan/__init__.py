"""Diff a database against its declarative schema, and apply the difference.

The declarative schema is built into a scratch database and compared with the
live one; the statements that would bring the live database into line are the
*plan*. Changes that could lose data are counted apart and skipped by default.
"""

from .changes import format_plan, plan_changes
from .inspect import SchemaInspector, get_all_schemas, get_inspector
from .statements import StatementCounter, is_unsafe_statement, should_log_statement

__all__ = [
    "SchemaInspector",
    "StatementCounter",
    "format_plan",
    "get_all_schemas",
    "get_inspector",
    "is_unsafe_statement",
    "plan_changes",
    "should_log_statement",
]
