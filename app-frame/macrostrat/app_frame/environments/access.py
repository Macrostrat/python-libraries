"""Confirming access to an environment before it happens.

Two properties do the real work, and both are about what an *agent* can do.
The stricter levels read from a terminal, and an agent has none, so no flag or
variable satisfies them. And ``reauthorize`` hands control to the application,
which can put a secret manager's own approval prompt in the path.
"""

import sys
from typing import Callable, Optional

from click.exceptions import ClickException

from macrostrat.utils import ApplicationError, get_logger

from .policy import AccessScope, EnvironmentPolicy, WriteGate, format_duration
from .state import EnvironmentState

log = get_logger(__name__)


class Refusal(ApplicationError, ClickException):
    """Something was not authorized. Click renders it as an error, exit 1."""

    def __init__(self, message: str, details: Optional[str] = None):
        ApplicationError.__init__(self, message, details)
        self.exit_code = 1

    def format_message(self) -> str:
        if self.details:
            return f"{self.message}\n{self.details}"
        return self.message


class AccessRefused(Refusal):
    """An access was not authorized."""


class StaleEnvironment(Refusal):
    """A lapsed environment was not confirmed for use."""


class UnknownEnvironment(Refusal):
    """An environment the config file does not define."""


def is_interactive() -> bool:
    """Whether a person is plausibly at a terminal: agents and CI have none."""
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def prompt(message: str) -> str:
    # stderr, so a prompt never lands in a command's piped stdout.
    print(message, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip()


def require_environment(
    state: EnvironmentState,
    policy: EnvironmentPolicy,
    *,
    command_name: str,
    action: Optional[str] = None,
) -> None:
    """Confirm a *lapsed* environment before it is used, or raise.

    A fresh, explicit or absent environment passes at once. A lapsed one asks
    once at a terminal (yes renews a remembered environment's time-to-live) and
    is refused without one.
    """
    active = state.active
    if active is None or not active.needs_approval:
        return None

    env = active.name
    klass = policy.env_class.value
    lapsed_for = "a while"
    if active.remaining:
        lapsed_for = format_duration(-active.remaining)
    what = ""
    if action:
        what = f" before {action}"

    if not is_interactive():
        raise StaleEnvironment(
            f"The {active.source} environment {env} ({klass}) lapsed {lapsed_for} ago",
            details=(
                f"There is no terminal to confirm it on{what}. Pass --env {env} "
                f"to use it for this command, or run `{command_name} env {env}` "
                "to activate it again."
            ),
        )

    if active.source == "remembered":
        grant = format_duration(policy.ttl)
        answer = prompt(
            f"Remembered environment {env} ({klass}) lapsed {lapsed_for} ago. "
            f"Keep using it for another {grant}? [y/N] "
        )
    else:
        answer = prompt(
            f"Shell environment {env} ({klass}) lapsed {lapsed_for} ago. "
            "Use it for this command? [y/N] "
        )
    if answer.lower() not in ("y", "yes"):
        raise StaleEnvironment(
            f"Declined to keep using {env}",
            details=f"Run `{command_name} env <name>` to choose an environment.",
        )

    if active.source == "remembered":
        state.renew(policy.ttl)
    else:
        active.approved = True
    return None


def require_access(
    scope,
    policy: EnvironmentPolicy,
    *,
    assume_yes: bool = False,
    action: Optional[str] = None,
    reauthorize: Optional[Callable[[AccessScope], None]] = None,
) -> None:
    """Authorize access of *scope* under *policy*, or raise `AccessRefused`.

    Returns None when it may proceed; never a boolean, so the answer cannot be
    ignored. *assume_yes* (a per-invocation ``--yes``) satisfies only the
    ``prompt`` level. *reauthorize* is called for the ``reauthorize`` level,
    after the name has been typed; without it that level cannot be satisfied.
    """
    scope = AccessScope(scope)
    gate = policy.gate_for(scope)
    env = policy.name or "<no environment>"
    klass = policy.env_class.value
    if action is not None:
        what = action
    elif scope == AccessScope.Read:
        what = "read"
    else:
        what = f"{scope.value} write"

    inferred = ""
    if policy.inferred:
        inferred = (
            " This environment declares no class, so it is treated as "
            f"{klass} ({policy.reason})."
        )

    if gate == WriteGate.NoGate:
        return None

    if gate == WriteGate.Confirm:
        if assume_yes:
            log.warning("Proceeding with %s in %s (%s): --yes.", what, env, klass)
            return None
        if not is_interactive():
            bypass = ", or pass --yes"
            if scope == AccessScope.Read:
                bypass = ""
            raise AccessRefused(
                f"Refusing {what} in {env} ({klass})",
                details=(
                    "This needs confirmation and there is no terminal to ask "
                    f"on. Re-run it interactively{bypass}.{inferred}"
                ),
            )
        answer = prompt(f"{what.capitalize()} in {env}. Proceed? [y/N] ")
        if answer.lower() not in ("y", "yes"):
            raise AccessRefused(f"{what.capitalize()} in {env} was declined")
        return None

    if gate == WriteGate.Escalate and reauthorize is None:
        raise AccessRefused(
            f"Refusing {what} in {env} ({klass})",
            details=(
                f"The {gate.value!r} level needs something to re-authorize "
                "against, and this application provides nothing. Lower the "
                f"level for this environment.{inferred}"
            ),
        )

    if not is_interactive():
        raise AccessRefused(
            f"Refusing {what} in {env} ({klass})",
            details=(
                f"The {gate.value!r} level guarding {scope.value} access here "
                "requires an interactive terminal. No flag or environment "
                f"variable satisfies it.{inferred}"
            ),
        )

    typed = prompt(
        f"{what.capitalize()} in {env} ({klass}). "
        "Type the environment name to proceed: "
    )
    if typed != env:
        raise AccessRefused(
            f"Refusing {what} in {env}", details=f"Expected {env!r}, got {typed!r}."
        )

    if gate == WriteGate.Escalate:
        reauthorize(scope)
    return None
