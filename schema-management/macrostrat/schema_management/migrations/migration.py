"""A migration: what must hold before it runs, and what holds once it has."""

import inspect
import warnings
from enum import Enum
from functools import total_ordering
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Optional, Union

from macrostrat.database import Database
from macrostrat.database.query import OutputMode

from ..database.environment import EnvironmentClass, environment_class
from .conditions import DBCallable, DbEvaluator, describe_condition

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


def env_allows_migration(
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
