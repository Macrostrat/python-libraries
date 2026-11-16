"""What an application tells the schema library about itself."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, ContextManager, Iterable, Optional, Union

from macrostrat.database import Database

from .chunks.composer import build_schema, select_chunks
from .chunks.definition import SchemaDefinition
from .database.environment import EnvironmentClass, environment_class
from .database.roles import applied_as_owner
from .migrations import Migration, MigrationRunner
from .migrations.loader import load_migrations
from .plan import StatementCounter, format_plan, plan_changes
from .sync import SyncReport, sync_schema_chunks
from .sync.selection import ObjectSelector

ChunkSource = Union[
    list[SchemaDefinition], Callable[[EnvironmentClass], list[SchemaDefinition]]
]


@contextmanager
def scratch_database(image: str = "postgis/postgis:16-3.4", **kwargs):
    """An empty, throwaway database in a container, to build a schema into."""
    from .database.cluster import database_cluster

    kwargs.setdefault("optimize_for_testing", True)
    with database_cluster(image, **kwargs) as db:
        yield db


def reload_postgrest_schema(db: Database):
    """An `after_change` hook for databases served by PostgREST."""
    db.run_sql("NOTIFY pgrst, 'reload schema';")


@dataclass
class SchemaApp:
    """One application's database schema, and how to manage it.

    The schema library hold no configuration of their own: an application builds
    one of these (per command, if its configuration can change) and the same
    object backs its command-line tools, its tests and its services.
    """

    # Names the application in messages, e.g. "rockd".
    name: str
    # Connects to the database being managed.
    database: Callable[[], Database]
    # The declarative schema: the chunks, or a function of the environment class.
    chunks: ChunkSource = field(default_factory=list)
    # Where plans and the applied-changes log are written.
    schema_dir: Optional[Path] = None
    # A directory of migration modules, and any classes given directly.
    migrations_dir: Optional[Path] = None
    migrations: Iterable[type[Migration]] = ()
    # Names the environment in plan files; its class decides layers and readiness.
    environment: str = "local"
    env_class: EnvironmentClass = EnvironmentClass.Production
    # Role that owns what the tools create. None applies as the connecting role.
    owner: Optional[str] = None
    # Yields an empty database the declarative schema is built into for a diff.
    planning_database: Callable[[], ContextManager[Database]] = scratch_database
    # Schemas a diff never compares (bulk data, extension-owned).
    excluded_schemas: list[str] = field(default_factory=list)
    # Receives a diff's `changes` to drop what cannot be diffed meaningfully.
    prepare_diff: Optional[Callable] = None
    # Called before any write with a description and whether `--yes` was given;
    # raises to refuse it.
    authorize: Optional[Callable[[str, bool], None]] = None
    # Called after the schema has changed.
    after_change: Optional[Callable[[Database], None]] = None
    # Paths in listings are printed relative to this.
    source_root: Optional[Path] = None

    def __post_init__(self):
        self.env_class = environment_class(self.env_class)

    # -- chunks -----------------------------------------------------------

    def all_chunks(self) -> list[SchemaDefinition]:
        """Every chunk, whatever environment it applies in."""
        if callable(self.chunks):
            # A function decides for itself; ask for each class and merge.
            seen: dict[str, SchemaDefinition] = {}
            for klass in EnvironmentClass:
                for chunk in self.chunks(klass):
                    seen.setdefault(chunk.name, chunk)
            return list(seen.values())
        return list(self.chunks)

    def environment_chunks(self) -> list[SchemaDefinition]:
        """The chunks that apply in this environment."""
        if callable(self.chunks):
            chunks = self.chunks(self.env_class)
        else:
            chunks = self.chunks
        return [c for c in chunks if c.applies_to(self.env_class)]

    def selected_chunks(
        self, *, target: Optional[str] = None, no_dependents: bool = False
    ) -> list[SchemaDefinition]:
        return select_chunks(
            self.environment_chunks(), target=target, no_dependents=no_dependents
        )

    # -- writes -----------------------------------------------------------

    def require_write(self, action: str, assume_yes: bool = False):
        if self.authorize is not None:
            self.authorize(action, assume_yes)

    def changed(self, db: Database):
        # Conditions read a cached inspector, stale once the schema has changed.
        db.refresh_schema()
        if self.after_change is not None:
            self.after_change(db)

    # -- declarative schema -----------------------------------------------

    def build(self, db: Database, *, target: Optional[str] = None, **kwargs):
        """Build the declarative schema into ``db`` (which may be a scratch one)."""
        chunks = self.selected_chunks(target=target)
        return build_schema(db, chunks, **kwargs)

    def provision(
        self,
        *,
        target: Optional[str] = None,
        no_dependents: bool = False,
        assume_yes: bool = False,
    ) -> StatementCounter:
        """Apply the declarative schema's statements to the managed database."""
        self.require_write("schema provisioning", assume_yes)
        db = self.database()
        counter = StatementCounter(safe=True)
        chunks = self.selected_chunks(target=target, no_dependents=no_dependents)
        build_schema(db, chunks, statement_filter=counter.filter)
        self.changed(db)
        return counter

    def sync(
        self,
        *,
        target: Optional[str] = None,
        no_dependents: bool = False,
        only: Optional[ObjectSelector] = None,
        assume_yes: bool = False,
        **parts,
    ) -> SyncReport:
        """Re-apply views, procedures, seed data and permissions."""
        self.require_write("schema sync", assume_yes)
        db = self.database()
        chunks = self.selected_chunks(target=target, no_dependents=no_dependents)
        report = sync_schema_chunks(db, chunks, only=only, **parts)
        self.changed(db)
        return report

    # -- diff -------------------------------------------------------------

    @property
    def plan_file(self) -> Path:
        if self.schema_dir is None:
            raise ValueError(f"{self.name} has no schema_dir to keep plans in")
        return self.schema_dir / f"{self.environment}.plan.sql"

    def plan(self) -> list[str]:
        """Statements that bring the managed database into line with the schema."""
        db = self.database()
        with self.planning_database() as plan_db:
            self.build(plan_db)
            return plan_changes(
                db,
                plan_db,
                excluded_schemas=self.excluded_schemas,
                prepare=self.prepare_diff,
            )

    def write_plan(self) -> tuple[Path, list[str]]:
        statements = self.plan()
        self.plan_file.parent.mkdir(parents=True, exist_ok=True)
        self.plan_file.write_text(format_plan(statements, owner=self.owner))
        return self.plan_file, statements

    def apply_plan(
        self,
        plan_file: Optional[Path] = None,
        *,
        safe: bool = True,
        console=None,
        assume_yes: bool = False,
    ) -> StatementCounter:
        """Apply a plan file; with ``safe``, statements that drop data are skipped."""
        self.require_write("schema application", assume_yes)
        plan_file = plan_file or self.plan_file
        db = self.database()
        counter = StatementCounter(safe=safe)
        kwargs = dict(statement_filter=counter.filter)
        if console is not None:
            kwargs["console"] = console
        if self.owner is None:
            db.run_fixtures(plan_file, **kwargs)
        else:
            # A plan has no chunks to take owners from, so one role applies it.
            with applied_as_owner(db, self.owner) as escalate:
                db.run_fixtures(plan_file, on_error=escalate, **kwargs)
        self.changed(db)
        return counter

    def log_applied(self, counter: StatementCounter) -> Optional[Path]:
        """Append the applied structural statements to the schema changelog."""
        statements = counter.schema_log_entries()
        if not statements:
            return None
        applied_dir = self.schema_dir / "_changelog"
        applied_dir.mkdir(exist_ok=True)
        now = datetime.now()
        applied_file = applied_dir / f"{now:%Y-%m-%d}-{self.environment}.applied.sql"
        n_ignored = len(counter.statements) - len(statements)
        with applied_file.open("a") as f:
            f.write(f"\n-- {now:%Y-%m-%d %H:%M:%S}\n")
            f.write(f"-- Environment: {self.environment}\n")
            counter.print_report(file=f, prefix="-- ")
            if n_ignored > 0:
                f.write(f"-- {n_ignored} statements were not logged\n")
            f.write("\n" + "\n".join(statements) + "\n\n")
        return applied_file

    # -- migrations -------------------------------------------------------

    def migration_classes(self) -> list[type[Migration]]:
        classes = list(self.migrations)
        if self.migrations_dir is not None and self.migrations_dir.is_dir():
            classes += [
                c for c in load_migrations(self.migrations_dir) if c not in classes
            ]
        return classes

    def runner(self, db: Optional[Database] = None) -> MigrationRunner:
        """A migration runner for the managed database, or for ``db``."""
        return MigrationRunner(
            db=db or self.database(),
            migrations=self.migration_classes(),
            env_class=self.env_class,
            owner=self.owner,
            chunks=self.environment_chunks,
            after_apply=self.after_change,
            source_root=self.source_root,
        )
