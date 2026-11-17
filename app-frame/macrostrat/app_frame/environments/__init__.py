"""Named environments for an application: settings, selection and confirmation.

An application keeps a TOML file with a table per environment. One is selected
per invocation (``--env``), per shell, or remembered for a while, and its class
decides how much confirmation a write asks for.

.. code-block:: python

    environments = Environments("rockd")
    environments.settings.get("database")
    environments.require_access(AccessScope.Schema, action="schema migration")
    cli.add_typer(environments.cli(), name="env")
"""

import sys
from datetime import datetime, timezone
from os import environ
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from ..secrets import Secret, SecretResolutionError, as_secret, reveal
from .access import (
    AccessRefused,
    Refusal,
    StaleEnvironment,
    UnknownEnvironment,
    is_interactive,
    require_access,
    require_environment,
)
from .config import (
    Settings,
    deep_merge,
    environment_overrides,
    environments_in,
    find_config,
    load_settings,
    merged_environment,
    read_config,
)
from .policy import (
    DEFAULT_ENV,
    DEFAULT_GATES,
    DEFAULT_TTL,
    WRITE_SCOPES,
    AccessScope,
    EnvironmentClass,
    EnvironmentPolicy,
    InvalidDuration,
    WriteGate,
    classes_up_to,
    format_duration,
    parse_duration,
    parse_gate,
    policy_from_table,
)
from .state import ActiveEnvironment, EnvironmentState, extract_env_from_argv


class Environments:
    """One application's environments, resolved for this invocation.

    Construct it once, before the command-line parser runs: ``--env`` is taken
    from ``sys.argv`` here so that settings can be loaded straight away.
    """

    def __init__(
        self,
        app_name: str,
        *,
        command_name: Optional[str] = None,
        config_name: Optional[str] = None,
        env_prefix: Optional[str] = None,
        search: Iterable[Path] = (),
        defaults: Optional[Mapping] = None,
        state_dir: Optional[Path] = None,
        credentials: Iterable[str] = (),
        reauthorize: Optional[Callable[[AccessScope], None]] = None,
        read_argv: bool = True,
    ):
        self.app_name = app_name
        self.command_name = command_name or app_name
        self.config_name = config_name or f"{app_name}.toml"
        self.env_prefix = env_prefix or app_name.upper().replace("-", "_") + "_"
        self.env_var = self.env_prefix + "ENV"
        self.expires_var = self.env_prefix + "ENV_EXPIRES"
        self.config_var = self.env_prefix + "CONFIG"
        self._search = list(search)
        self._defaults = defaults
        # Settings whose secrets are fetched afresh at the `reauthorize` level.
        self._credentials = list(credentials)
        self._reauthorize = reauthorize
        self.state = EnvironmentState(
            app_name,
            env_var=self.env_var,
            expires_var=self.expires_var,
            state_dir=state_dir,
        )

        if read_argv:
            env = extract_env_from_argv()
            if env is not None:
                environ[self.env_var] = env
                # --env is per invocation, so a shell session's expiry is moot.
                environ.pop(self.expires_var, None)
        self.reload()

    @classmethod
    def for_command_line(cls, *args, **kwargs) -> "Environments":
        """Construct, reporting a bad environment as a message and exit code 1.

        Settings load before any command-line parser runs, so nothing else
        would render the error.
        """
        try:
            return cls(*args, **kwargs)
        except Refusal as err:
            print(f"Error: {err.format_message()}", file=sys.stderr)
            raise SystemExit(1)

    def reload(self):
        """Find the config file, resolve the environment and load its settings."""
        self.config_file = find_config(
            self.config_name,
            env_var=self.config_var,
            app_name=self.app_name,
            search=self._search,
        )
        self.env = self.state.resolve(self.config_file)
        available = self.available()
        # With no file, a named environment is whatever the variables describe.
        if self.env is not None and available and self.env not in available:
            raise UnknownEnvironment(
                f"Environment {self.env} is not defined in {self.config_file}",
                details="Available: " + ", ".join(available),
            )
        self.settings: Settings = load_settings(
            self.config_file,
            self.env,
            prefix=self.env_prefix,
            control_variables=("ENV", "ENV_EXPIRES", "CONFIG"),
            defaults=self._defaults,
        )
        self.policy: EnvironmentPolicy = policy_from_table(self.env, self.settings)

    # -- description ------------------------------------------------------

    @property
    def active(self) -> Optional[ActiveEnvironment]:
        return self.state.active

    @property
    def env_class(self) -> EnvironmentClass:
        return self.policy.env_class

    def available(self) -> list[str]:
        return environments_in(self.config_file)

    def policy_for(self, env: str) -> EnvironmentPolicy:
        """The policy *env* declares, without selecting it."""
        table = merged_environment(read_config(self.config_file), env)
        return policy_from_table(env, table)

    # -- confirmation -----------------------------------------------------

    def require_environment(self, action: Optional[str] = None) -> None:
        """Confirm a lapsed environment before it is used. Idempotent."""
        require_environment(
            self.state, self.policy, command_name=self.command_name, action=action
        )

    def require_access(
        self, scope, *, assume_yes: bool = False, action: Optional[str] = None
    ) -> None:
        """Authorize access of *scope* in the active environment, or raise."""
        self.require_environment(action)
        reauthorize = self._reauthorize
        if reauthorize is None and self._credentials:
            reauthorize = self._refetch_credentials
        require_access(
            scope,
            self.policy,
            assume_yes=assume_yes,
            action=action,
            reauthorize=reauthorize,
        )

    def _refetch_credentials(self, scope: AccessScope) -> None:
        """Fetch each credential again, so the secret manager asks a person.

        A literal in the config file cannot be re-authorized, so it fails closed.
        """
        env = self.env or "<no environment>"
        for key in self._credentials:
            value = self.settings.get(key)
            if not isinstance(value, Secret):
                raise AccessRefused(
                    f"Cannot reauthorize in {env}: `{key}` is not a secret reference",
                    details=(
                        "The reauthorize level fetches the credential again so the "
                        "secret manager can require approval. Make it a reference "
                        f'({key} = "op://…") or lower the level for this environment.'
                    ),
                )
            value.forget()
            value.get()

    # -- selection --------------------------------------------------------

    def select(self, env: str) -> Optional[datetime]:
        """Remember *env* for its time-to-live. Returns when it lapses."""
        available = self.available()
        if env not in available:
            raise UnknownEnvironment(
                f"Environment {env} is not defined in {self.config_file}",
                details="Available: " + (", ".join(available) or "none"),
            )
        policy = self.policy_for(env)
        expires = self.state.remember(
            env, expires_in=policy.ttl, config_file=self.config_file
        )
        environ[self.env_var] = env
        return expires

    def forget(self):
        self.state.remember(None)

    def shell_exports(self, env: str) -> list[str]:
        """Lines for `eval`, giving a shell an environment that lapses too."""
        if env not in self.available():
            raise UnknownEnvironment(f"Environment {env} is not defined")
        policy = self.policy_for(env)
        lines = [f"export {self.env_var}={env}"]
        if policy.ttl is not None:
            expires = datetime.now(timezone.utc) + policy.ttl
            lines.append(f"export {self.expires_var}={expires.isoformat()}")
        return lines

    def cli(self):
        """A Typer app: show, select, forget and list environments."""
        from .cli import create_env_cli

        return create_env_cli(self)
