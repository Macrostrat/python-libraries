"""The schema as it should be: named, dependency-ordered chunks of SQL."""

from .composer import build_schema, dependency_closure, order_chunks, select_chunks
from .definition import SchemaDefinition, apply_sql_path, sql_files
from .discovery import discover_chunks, parse_frontmatter
