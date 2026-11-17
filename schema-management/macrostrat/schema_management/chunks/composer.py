"""Dependency-graph composer for a declarative schema.

The schema is assembled from *chunks* ordered by a declared dependency graph.
Each chunk contributes one or more providers — a directory of ``.sql`` files or
a callable that applies its own statements. Both are idempotent and buildable
from zero, so they take part in the graph identically.
"""

from graphlib import TopologicalSorter
from typing import Optional

from macrostrat.database import Database

from ..database.roles import set_applying_role
from .definition import SchemaDefinition


def order_chunks(chunks: list[SchemaDefinition]) -> list[SchemaDefinition]:
    """Topologically sort chunks by ``depends_on``.

    Dependencies pointing outside the given set (e.g. env-filtered out) are
    tolerated: they order the graph but are skipped in the result.
    """
    databases = {c.database for c in chunks}
    if len(databases) != 1:
        raise ValueError(f"Chunks must all be in the same database, got {databases}")

    by_name = {c.name: c for c in chunks}
    graph = {c.name: c.depends_on for c in chunks}
    order = list(TopologicalSorter(graph).static_order())
    return [by_name[n] for n in order if n in by_name]


def dependency_closure(chunks: list[SchemaDefinition], target: str) -> set[str]:
    """Names of ``target`` plus all of its transitive dependencies.

    ``target`` is a subsystem (chunk) name. Building the closure yields exactly
    the chunks needed to stand up that subsystem.
    """
    by_name = {c.name: c for c in chunks}
    if target not in by_name:
        raise ValueError(
            f"Unknown schema target {target!r}. Known chunks: {sorted(by_name)}"
        )

    seen: set[str] = set()
    stack = [target]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        chunk = by_name.get(name)
        if chunk is not None:
            stack.extend(chunk.depends_on)
    return seen


def select_chunks(
    chunks: list[SchemaDefinition],
    *,
    target: Optional[str] = None,
    no_dependents: bool = False,
) -> list[SchemaDefinition]:
    """Order ``chunks``, optionally narrowed to a ``target`` subsystem.

    - ``target=None`` → every chunk.
    - ``target`` set → the target chunk plus the chunks it depends on.
    - ``target`` set, ``no_dependents=True`` → only the target chunk.
    """
    ordered = order_chunks(chunks)
    if target is None:
        return ordered
    keep = dependency_closure(ordered, target)
    if no_dependents:
        keep = {target}
    return [c for c in ordered if c.name in keep]


def build_schema(
    db: Database,
    chunks: list[SchemaDefinition],
    *,
    transform_statement=None,
    statement_filter=None,
    target: Optional[str] = None,
) -> Database:
    """Build the declarative schema by applying ``chunks`` in graph order.

    ``transform_statement`` / ``statement_filter`` are forwarded to SQL file
    application. With ``target``, only that chunk and its dependencies are built.
    """
    ordered = select_chunks(chunks, target=target)
    try:
        for chunk in ordered:
            # Per chunk, so a failure cannot carry a role into the next one.
            set_applying_role(db, chunk.owner)
            chunk.apply(
                db,
                transform_statement=transform_statement,
                statement_filter=statement_filter,
            )
    finally:
        set_applying_role(db, None)

    return db
