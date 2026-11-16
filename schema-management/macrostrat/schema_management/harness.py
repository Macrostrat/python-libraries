"""Build a test database progressively, chunk by chunk."""

from typing import Optional

from macrostrat.database import Database
from macrostrat.database.query import StatementContext, StatementResult

from .composer import dependency_closure, order_chunks
from .definition import SchemaDefinition
from .roles import set_applying_role


def optimize_transform(ctx: StatementContext) -> Optional[list[StatementResult]]:
    """Drop statements most tests do not need, for a faster build.

    Returning ``[]`` skips the statement; ``None`` applies it unchanged.
    """
    stmt = ctx.sql_text.strip().lower()
    if (
        stmt.startswith("create index")
        or stmt.startswith("create unique index")
        or stmt.startswith("alter index")
        or (stmt.startswith("alter table") and "owner to" in stmt)
    ):
        return []
    return None


class DatabaseTestHarness:
    """Applies chunks to one database, remembering which are already there."""

    def __init__(
        self,
        database: Database,
        chunks: list[SchemaDefinition],
        *,
        optimize: bool = True,
    ):
        self.db = database
        self._chunks = chunks
        self.optimize = optimize
        self._applied_chunks: set[str] = set()

    @property
    def transform_statement(self):
        if self.optimize:
            return optimize_transform
        return None

    def chunks(self) -> list[SchemaDefinition]:
        return order_chunks(self._chunks)

    def load_schema(self, *, target: Optional[str] = None) -> Database:
        """Apply ``target`` and its dependencies, or every chunk.

        Chunks applied by an earlier call are skipped, so a minimal build can be
        grown into a fuller one in place.
        """
        chunks = self.chunks()
        if target is not None:
            keep = dependency_closure(chunks, target)
            chunks = [c for c in chunks if c.name in keep]
        try:
            for chunk in chunks:
                if chunk.name in self._applied_chunks:
                    continue
                # As its owner, so sync's rebuild passes find the owners they expect.
                set_applying_role(self.db, chunk.owner)
                chunk.apply(self.db, transform_statement=self.transform_statement)
                self._applied_chunks.add(chunk.name)
        finally:
            set_applying_role(self.db, None)
        return self.db
