"""Condition-checked migrations, applied in dependency order.

A migration names what must hold before it can run (preconditions) and what
holds once it has (postconditions), so whether it has applied is read from the
database rather than from a ledger. The runner is handed everything specific to
an application: the database, the migrations, the class of environment, the role
that owns what migrations create, and what to do once they have applied.
"""

import inspect
import warnings
from dataclasses import dataclass, field
from enum import Enum
from functools import total_ordering
from graphlib import TopologicalSorter
from pathlib import Path
from time import time
from typing import Callable, Iterable, Optional, Union

from pydantic import BaseModel
from rich import print
from rich.markup import escape

from macrostrat.database import Database
from macrostrat.database.query import OutputMode

from .conditions import DBCallable, DbEvaluator, describe_condition
from .definition import SchemaDefinition
from .environment import EnvironmentClass, environment_class

_MIN_READINESS = {
    EnvironmentClass.Local: "alpha",
    EnvironmentClass.Development: "alpha",
    EnvironmentClass.Staging: "beta",
    EnvironmentClass.Production: "ga",
}
_READINESS_ORDER = {"alpha": 0, "beta": 1, "ga": 2}

# For `Migration.owner`: apply as the role the runner was given.
INHERIT = "<runner owner>"


@total_ordering
class ReadinessState(Enum):
    ALPHA = "alpha"
    BETA = "beta"
    GA = "ga"

    def __gt__(self, other):
        if not isinstance(other, ReadinessState):
            return NotImplemented
        return _READINESS_ORDER[self.value] > _READINESS_ORDER[other.value]

    def __eq__(self, other):
        if not isinstance(other, ReadinessState):
            return NotImplemented
        return self.value == other.value

    def __hash__(self):
        return hash(self.value)


def _env_allows_migration(
    migration_readiness: Union[str, ReadinessState], env_class: EnvironmentClass
) -> bool:
    if isinstance(migration_readiness, ReadinessState):
        migration_readiness = migration_readiness.value
    migr_ready = (migration_readiness or "alpha").lower()
    min_ready = _MIN_READINESS[environment_class(env_class)]
    return _READINESS_ORDER[migr_ready] >= _READINESS_ORDER[min_ready]


class ApplicationStatus(Enum):
    """Enum for the possible"""

    # The preconditions for this migration aren't met, so it can't be applied
    CANT_APPLY = "cant_apply"

    # The preconditions for this migration are met but the postconditions aren't met, so it can be applied
    CAN_APPLY = "can_apply"

    # The postconditions for this migration are met, so it doesn't need to be applied
    APPLIED = "applied"


class Migration:
    """Class defining a set of sql changes to be applied to the database, as well as checks for
    whether the migration can be applied to the current state of the database
    """

    # Unique name for the migration
    name: str

    # Short description for the migration, printed beneath it when listed
    description: Optional[str] = None

    # Portion of the database to which this migration applies
    subsystem: str

    # List of migration names that must be run before this migration
    depends_on: list[str] = []

    # List of checks on the database that must all evaluate to true before the migration can be run
    preconditions: list[DbEvaluator] = []

    # List of checks on the database that should all evaluate to true after the migration has run successfully
    postconditions: list[DbEvaluator] = []

    # Should sql be loaded from the directory of the migration class
    load_sql_files: bool | Path = True

    # Fixtures to run after loaded sql
    fixtures: list[Path | DBCallable] = []

    # Schema chunks to re-sync once this migration has applied: their views,
    # functions, seed data and grants are re-applied, as `schema sync
    # --target <chunk> --no-dependents` would. For a migration that changes the
    # structure a chunk's code objects are defined against, so it need not
    # re-run the chunk's files itself. Runs before the postconditions are checked.
    sync_chunks: list[str] = []

    # Flag for whether running this migration will cause data changes in the database in addition to
    # schema changes
    destructive: bool = False

    # Role the migration is applied as, so what it creates is born owned the way
    # the declarative build owns it rather than by the connector. `INHERIT` takes
    # the runner's owner. Statements from the migration's SQL files that the role
    # is refused (an extension, `public`) are retried as the connector. `None`
    # applies it as the connector: for a migration whose own `apply` needs more
    # than the application role.
    owner: Optional[str] = INHERIT

    # Set by the runner while the migration applies: retries a statement the
    # owner is refused as the connector. See `roles.applied_as_owner`.
    on_error = None

    # Flag for whether this migration only contains views/functions that don't modify the broader schema
    always_apply: bool = False

    # Flags for whether the migration can be applied in the given environment
    readiness_state: str = "alpha"

    output_mode: OutputMode = OutputMode.SUMMARY

    def __init__(self):
        pass

    def should_apply(self, database: Database) -> ApplicationStatus:
        """Determine whether this migration can run, or has already run.

        Only called once every dependency has applied, so a condition may read
        what a dependency creates."""
        if self.always_apply:
            return ApplicationStatus.CAN_APPLY
        # If all post-conditions are met, the migration is already applied
        if all([cond(database) for cond in self.postconditions]):
            return ApplicationStatus.APPLIED
        # Else if all pre-conditions are met, the migration can be applied
        elif all([cond(database) for cond in self.preconditions]):
            return ApplicationStatus.CAN_APPLY
        # Else, can't apply
        else:
            return ApplicationStatus.CANT_APPLY

    def explain(self, database: Database) -> list[str]:
        """Why this migration cannot apply, one line per reason.

        Postconditions are reported only when some already hold, which marks a
        partial application. A migration whose `should_apply` decides on other
        grounds can override this to say what they are."""
        lines = [
            f"precondition not met: {describe_condition(cond)}"
            for cond in self.preconditions
            if not cond(database)
        ]
        explained = len(lines) > 0
        unmet = [cond for cond in self.postconditions if not cond(database)]
        n_held = len(self.postconditions) - len(unmet)
        if n_held and unmet:
            lines.append(
                f"partially applied: {n_held} of {len(self.postconditions)} postconditions met"
            )
            lines.extend(
                f"postcondition not met: {describe_condition(cond)}" for cond in unmet
            )
        if not explained:
            lines.append(f"decided by {source_location(type(self).should_apply)}")
        return lines

    def apply(self, database: Database) -> ApplicationStatus:
        """Apply the migrations defined by this class. By default, run every sql file
        in the same directory as the class definition."""
        if len(self.fixtures) == 0 and self.load_sql_files:
            # Automatically load fixtures from the same directory as the migration
            sql_dir = Path(inspect.getfile(self.__class__)).parent
            database.run_fixtures(
                sql_dir, output_mode=self.output_mode, on_error=self.on_error
            )
            return

        for fixture in self.fixtures:
            if callable(fixture):
                fixture(database)
            elif isinstance(fixture, Path):
                database.run_fixtures(
                    fixture, output_mode=self.output_mode, on_error=self.on_error
                )
            else:
                raise ValueError(f"Fixture {fixture} should be a callable or a Path")


def source_location(fn, root: Optional[Path] = None) -> str:
    try:
        path = Path(inspect.getsourcefile(fn))
        line = inspect.getsourcelines(fn)[1]
    except (OSError, TypeError):
        return fn.__qualname__
    if root is not None and path.is_relative_to(root):
        path = path.relative_to(root)
    return f"{fn.__qualname__} ({path}:{line})"


def _print_explanation(db: Database, migration: Migration, style: str, root=None):
    lines, error = _guarded(db, lambda: migration.explain(db))
    if error is not None:
        lines = _error_lines(db, migration, error, root)
    _print_lines(lines, style)


def _print_lines(lines: list[str], style: str):
    for line in lines:
        print(f"    [{style}]{escape(line)}[/]")


class MigrationState(Enum):
    """Enum for the possible states of a migration before application"""

    COMPLETE = "complete"
    UNMET_DEPENDENCIES = "unmet_dependencies"
    CANNOT_APPLY = "cannot_apply"
    SHOULD_APPLY = "should_apply"
    DISALLOWED = "disallowed"
    # The migration always applies, regardless of the state of the database
    ALWAYS_APPLY = "always_apply"
    NOT_ENV_READY = "not_env_ready"
    # Evaluating the migration's conditions raised an error
    EVALUATION_ERROR = "evaluation_error"


class MigrationResult(BaseModel):
    n_migrations: int
    n_remaining: int
    duration: float


def concrete_migrations(base: type = Migration) -> list[type[Migration]]:
    """Every concrete migration class (one with a `name`) descending from `base`."""
    found = []
    pending = list(base.__subclasses__())
    while pending:
        cls = pending.pop(0)
        if hasattr(cls, "name"):
            found.append(cls)
            continue
        subclasses = cls.__subclasses__()
        if len(subclasses) == 0:
            warnings.warn(
                f"No subclasses or concrete implementation found for migration class {cls}"
            )
        pending.extend(subclasses)
    return found


def order_migrations(instances: list[Migration]) -> list[Migration]:
    """Sort migrations so each follows what it depends on."""
    graph = {inst.name: inst.depends_on for inst in instances}
    order = list(TopologicalSorter(graph).static_order())
    return sorted(instances, key=lambda i: order.index(i.name))


def _undefined_dependencies(migration: Migration, defined: set[str]) -> list[str]:
    """Dependencies that name no migration at all, legacy ones included."""
    return [d for d in migration.depends_on if d not in defined]


def _dependencies_met(
    migration: Migration, completed: set[str] | list[str], present: set[str]
) -> bool:
    """Whether every dependency in this run has applied.

    A dependency that is not in the run counts as met. Migrations are removed
    once they hold in every environment, and what depended on one still stands;
    the listing names any that no longer exist, so a typo does not pass silently.
    """
    return all(d in completed for d in migration.depends_on if d in present)


def _with_dependencies(instances: list[Migration], name: str) -> list[Migration]:
    """The named migration and everything it depends on, in dependency order."""
    by_name = {m.name: m for m in instances}
    if name not in by_name:
        raise ValueError(f"No migration named {name}")
    needed = set()
    pending = [name]
    while pending:
        _name = pending.pop()
        if _name in needed or _name not in by_name:
            continue
        needed.add(_name)
        pending.extend(by_name[_name].depends_on)
    return [m for m in instances if m.name in needed]


def _guarded(db: Database, fn):
    """Run `fn`, returning its result and any error, so one migration's broken
    condition does not stop the others being listed or applied."""
    try:
        return fn(), None
    except Exception as err:
        # Otherwise every later query fails with "transaction is aborted"
        db.session.rollback()
        return None, err


def _error_lines(
    db: Database, migration: Migration, err: Exception, root=None
) -> list[str]:
    """The error a migration's conditions raised, and which condition raised it."""
    message = str(getattr(err, "orig", None) or err).strip()
    lines = [message.splitlines()[0] if message else type(err).__name__]
    # Retried one at a time: closures from one factory share a code object, so
    # the traceback cannot say which of them raised.
    for kind, conditions in (
        ("postcondition", migration.postconditions),
        ("precondition", migration.preconditions),
    ):
        for cond in conditions:
            if _guarded(db, lambda: cond(db))[1] is not None:
                return lines + [f"while checking {kind} {describe_condition(cond)}"]
    location = source_location(type(migration).should_apply, root)
    return lines + [f"raised in {location}"]


def _evaluate(db: Database, instances: list[Migration]):
    """Yield each migration with its `ApplicationStatus` and any error raised
    evaluating it, in dependency order.

    A migration whose dependencies have not applied is not evaluated, and yields
    None: its conditions may read what a dependency creates.
    """
    present = {m.name for m in instances}
    completed = set()
    for migration in instances:
        status, error = None, None
        if _dependencies_met(migration, completed, present):
            status, error = _guarded(db, lambda: migration.should_apply(db))
            if status == ApplicationStatus.APPLIED:
                completed.add(migration.name)
        yield migration, status, error


@dataclass
class MigrationRunner:
    """Lists and applies a set of migrations against one database.

    Everything an application decides is given here rather than read from global
    configuration, so one process can manage several databases.
    """

    db: Database
    # Migration classes or instances; see `loader.load_migrations`.
    migrations: Iterable[Union[type[Migration], Migration]] = ()
    # Decides which readiness states may apply. Unknown fails closed to production.
    env_class: EnvironmentClass = EnvironmentClass.Production
    # Role that `Migration.owner = INHERIT` migrations are applied as.
    owner: Optional[str] = None
    # The schema chunks, for migrations that name `sync_chunks`.
    chunks: Optional[Callable[[], list[SchemaDefinition]]] = None
    # Called once migrations have applied (e.g. to reload an API's schema cache).
    after_apply: Optional[Callable[[Database], None]] = None
    # Source paths in explanations are printed relative to this.
    source_root: Optional[Path] = None
    _instances: Optional[list[Migration]] = field(default=None, repr=False)

    def __post_init__(self):
        self.env_class = environment_class(self.env_class)

    # -- the migration set ------------------------------------------------

    def all_instances(self) -> list[Migration]:
        if self._instances is None:
            self._instances = [
                m() if isinstance(m, type) else m
                for m in self.migrations
                if hasattr(m, "name")
            ]
        return self._instances

    def instances(
        self, *, legacy: bool = False, readiness_level: ReadinessState = None
    ) -> list[Migration]:
        """Migrations in dependency order, optionally at or above a readiness."""
        instances = [
            m for m in self.all_instances() if legacy or not getattr(m, "legacy", False)
        ]
        if readiness_level is not None:
            expected = ReadinessState(readiness_level)
            instances = [
                m
                for m in instances
                if ReadinessState(getattr(m, "readiness_state", "alpha")) >= expected
            ]
        return order_migrations(instances)

    # -- applying ---------------------------------------------------------

    def _owner_of(self, migration: Migration) -> Optional[str]:
        if migration.owner == INHERIT:
            return self.owner
        return migration.owner

    def _apply_as_owner(self, migration: Migration):
        owner = self._owner_of(migration)
        if owner is None:
            migration.apply(self.db)
            return
        from .roles import applied_as_owner

        with applied_as_owner(self.db, owner) as escalate:
            migration.on_error = escalate
            try:
                migration.apply(self.db)
            finally:
                migration.on_error = None

    def _sync_after(self, migration: Migration):
        """Re-sync the chunks a migration names, once it has applied."""
        if not migration.sync_chunks:
            return
        from .sync import print_failures, sync_schema_chunks

        available = self.chunks() if self.chunks is not None else []
        chunks = [c for c in available if c.name in migration.sync_chunks]
        unknown = set(migration.sync_chunks) - {c.name for c in chunks}
        if unknown:
            print(
                f"[yellow]{migration.name} syncs {', '.join(sorted(unknown))}, which is"
                " not a schema chunk in this environment[/]"
            )
        if not chunks:
            return
        print(f"[dim]Syncing {', '.join(c.name for c in chunks)}[/]")
        report = sync_schema_chunks(self.db, chunks)
        print_failures(report.failures)

    def run(
        self,
        apply: bool = False,
        name: str = None,
        force: bool = False,
        data_changes: bool = False,
        subsystem: str = None,
        verbose: bool = True,
        legacy: bool = False,
        reapply: bool = False,
        show_applied: bool = False,
    ) -> tuple[Optional[int], set[str]]:
        """List migrations and, with `apply`, apply those that can.

        Returns the number applied (None when only listing) and the names that
        hold afterwards.
        """
        db = self.db
        root = self.source_root
        t_start = time()

        if force and not name:
            raise ValueError("--force can only be applied with --name")

        instances = self.instances(legacy=legacy)
        if name is not None:
            instances = _with_dependencies(instances, name)

        output_mode = OutputMode.SUMMARY if verbose else OutputMode.NONE

        completed_migrations = []
        failed_migrations = []

        name_max_width = max((len(m.name) for m in instances), default=0)

        print("Migrations:")
        if not instances:
            print("[dim]  none defined[/]")

        migrations_to_run = []
        listed = []
        errors = {}
        present = {m.name for m in instances}
        defined = present | {m.name for m in self.all_instances()}

        # Dependencies of a migration named with --name are evaluated, though not
        # listed, so that it sees whether they have applied.
        for _migration, apply_status, error in _evaluate(db, instances):
            _name = _migration.name
            _subsystem = getattr(_migration, "subsystem", None)

            if apply_status == ApplicationStatus.APPLIED:
                completed_migrations.append(_name)
            if error is not None:
                errors[_name] = error

            if name is not None and name != _name:
                continue

            if subsystem is not None and subsystem != _subsystem:
                continue

            _status = _get_status(
                _migration,
                completed_migrations,
                apply_status,
                data_changes=data_changes,
                env_class=self.env_class,
                error=error,
            )

            listed.append((_migration, _status))
            migrations_to_run.append(_migration)

        # Applied migrations last, so what needs attention is at the top, and only
        # on request (or when named). Printing only: `migrations_to_run` keeps
        # dependency order for application.
        listed.sort(key=lambda row: row[1] == MigrationState.COMPLETE)
        n_hidden = 0
        for _migration, _status in listed:
            if _status == MigrationState.COMPLETE and not (show_applied or name):
                n_hidden += 1
                continue
            _print_status(
                _migration.name,
                _status,
                name_max_width=name_max_width,
                env_class=self.env_class,
            )
            if _status == MigrationState.CANNOT_APPLY:
                _print_explanation(db, _migration, "red", root)
            if _status == MigrationState.EVALUATION_ERROR:
                lines = _error_lines(db, _migration, errors[_migration.name], root)
                _print_lines(lines, "red")
            undefined = _undefined_dependencies(_migration, defined)
            if undefined:
                print(
                    f"    [yellow]depends on {', '.join(undefined)}, which no longer"
                    " exist -- assumed met[/]"
                )
            if _migration.description:
                print(f"    [dim]{_migration.description}[/]")
            if _migration.sync_chunks:
                print(f"    [dim]then syncs {', '.join(_migration.sync_chunks)}[/]")
        if n_hidden:
            print(f"[dim]{n_hidden} already applied (--show-applied to list them)[/]")

        if not apply:
            print("\n[dim]To apply the migrations, run with --apply")
            return (None, set())

        run_counter = 0

        for _migration in migrations_to_run:
            _name = _migration.name
            if not _env_allows_migration(
                getattr(_migration, "readiness_state", "alpha"), self.env_class
            ):
                continue
            dependencies_met = _dependencies_met(
                _migration, completed_migrations, present
            )

            if not force:
                if not dependencies_met:
                    continue

                if (
                    _name in completed_migrations
                    and not reapply
                    and not _migration.always_apply
                ):
                    continue

                if _migration.destructive and not data_changes:
                    continue

                # Evaluated here rather than trusted from the listing: a dependency
                # applied earlier in this run can be what makes the conditions
                # hold, or evaluable at all.
                apply_status, error = _guarded(db, lambda: _migration.should_apply(db))
                if error is not None:
                    print(
                        f"\n[dim]Skipping [cyan]{_name}[/]: could not be evaluated[/]"
                    )
                    _print_lines(_error_lines(db, _migration, error, root), "dim")
                    continue
                if apply_status == ApplicationStatus.CANT_APPLY:
                    print(f"\n[dim]Skipping [cyan]{_name}[/]: preconditions not met[/]")
                    _print_explanation(db, _migration, "dim", root)
                    continue
                if apply_status == ApplicationStatus.APPLIED and not reapply:
                    completed_migrations.append(_name)
                    continue

            _migration.output_mode = output_mode

            print(f"\nApplying migration [bold cyan]{_name}[/]...")
            self._apply_as_owner(_migration)
            self._sync_after(_migration)
            run_counter += 1
            # Reload the reflected schema, then confirm the migration now holds.
            db.refresh_schema()

            if (
                _migration.should_apply(db) != ApplicationStatus.APPLIED
                and not _migration.always_apply
            ):
                failed_migrations.append(_migration.name)
                continue

            completed_migrations.append(_migration.name)

        if self.after_apply is not None:
            self.after_apply(db)

        t_delta = time() - t_start

        print(f"\nApplied {run_counter} migrations in {t_delta:.2f} seconds")
        print(f"Completed: {completed_migrations}")
        if failed_migrations:
            print(f"Failed: {failed_migrations}")

        return run_counter, set(completed_migrations)

    def applyable(
        self, *, allow_destructive=False, legacy=False, readiness_level=None
    ) -> set[str]:
        """Names of the migrations that can apply now."""
        _res = set()
        migrations = self.instances(legacy=legacy, readiness_level=readiness_level)
        for _migration, apply_status, _ in _evaluate(self.db, migrations):
            if _migration.destructive and not allow_destructive:
                continue
            if apply_status == ApplicationStatus.CAN_APPLY:
                _res.add(_migration.name)
        return _res

    def run_to_completion(
        self, *, legacy=False, raise_errors=False, readiness_level=None
    ) -> MigrationResult:
        """Apply repeatedly until nothing more can apply; for a scratch database.

        With `readiness_level`, only migrations at or above it count as remaining.
        """
        t_start = time()
        n_total = 0
        completed: set[str] = set()

        def applyable():
            return self.applyable(
                allow_destructive=True, legacy=legacy, readiness_level=readiness_level
            )

        remaining = applyable()
        while remaining:
            n_applied, done = self.run(apply=True, data_changes=True, legacy=legacy)
            n_total += n_applied
            completed |= done
            following = applyable() - completed
            if following == remaining:
                print("No changes in applyable migrations, exiting")
                if raise_errors:
                    raise ValueError(f"Migrations did not apply: {sorted(remaining)}")
                break
            remaining = following
        return MigrationResult(
            n_migrations=n_total,
            n_remaining=len(remaining),
            duration=time() - t_start,
        )

    def has_been_run(self, *names: str) -> bool:
        """Whether every named migration's postconditions hold."""
        by_name = {m.name: m for m in self.all_instances()}
        missing = set(names) - set(by_name)
        if missing:
            raise ValueError(f"Unknown migrations: {missing}")
        return all(
            by_name[n].should_apply(self.db) is ApplicationStatus.APPLIED for n in names
        )


def _get_status(
    _migration: Migration,
    completed_migrations: set[str],
    apply_status: Optional[ApplicationStatus],
    data_changes: bool = False,
    env_class: EnvironmentClass = EnvironmentClass.Production,
    error: Optional[Exception] = None,
) -> MigrationState:
    """Get the status of a migration. `apply_status` is None for one that was not
    evaluated because a dependency has not applied, or whose evaluation raised
    `error`."""
    name = _migration.name

    if name in completed_migrations:
        return MigrationState.COMPLETE

    if not _env_allows_migration(
        getattr(_migration, "readiness_state", "alpha"), env_class
    ):
        return MigrationState.NOT_ENV_READY

    if error is not None:
        return MigrationState.EVALUATION_ERROR

    # By default, don't run migrations that depend on other non-applied migrations
    if apply_status is None:
        return MigrationState.UNMET_DEPENDENCIES

    if _migration.always_apply:
        return MigrationState.ALWAYS_APPLY

    if apply_status == ApplicationStatus.CANT_APPLY:
        return MigrationState.CANNOT_APPLY

    if _migration.destructive and not data_changes:
        return MigrationState.DISALLOWED
    return MigrationState.SHOULD_APPLY


def _print_status(name, status: MigrationState, *, name_max_width=40, env_class=None):
    padding = " " * (name_max_width - len(name))
    print(f"- [bold cyan]{name}[/]: " + padding, end="")
    if status == MigrationState.COMPLETE:
        print("[green]already applied[/green]")
    elif status == MigrationState.UNMET_DEPENDENCIES:
        print("[orange]has unmet dependencies[/orange]")
    elif status == MigrationState.CANNOT_APPLY:
        print("[red]cannot be applied[/red]")
    elif status == MigrationState.EVALUATION_ERROR:
        print("[red]could not be evaluated[/red]")
    elif status == MigrationState.SHOULD_APPLY:
        print("[orange]should be applied[/orange]")
    elif status == MigrationState.ALWAYS_APPLY:
        print("[yellow] always applied[/yellow]")
    elif status == MigrationState.DISALLOWED:
        print("[red]cannot be applied without --force or --data[/red]")
    elif status == MigrationState.NOT_ENV_READY:
        print(f"[red]not {environment_class(env_class).value} ready[/red]")
    else:
        raise ValueError(f"Unknown migration status: {status}")
