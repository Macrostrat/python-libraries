"""Which environment an invocation uses, and how long a remembered one lasts.

An environment comes from, in order: ``--env`` on the command line, the
application's ``<PREFIX>ENV`` variable, or a pointer remembered in the user's
application directory. The remembered pointer applies only to the config file
it was set against, and lapses after the environment's time-to-live. A lapsed
pointer is kept, and whoever is about to use it asks first.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from os import environ
from pathlib import Path
from sys import argv
from typing import Optional

from click import get_app_dir

from macrostrat.utils import get_logger

from .config import NO_VALUE, environments_in

log = get_logger(__name__)

ACTIVE_ENV_KEY = "active_env"
ACTIVE_ENV_EXPIRES_KEY = "active_env_expires"
ACTIVE_ENV_CONFIG_KEY = "active_env_config"


def extract_env_from_argv(args=None) -> Optional[str]:
    """Pull `--env`/`-e` out of *args*, removing both tokens. Mutates in place.

    Settings are usually loaded while an application is constructed, which is
    before its command-line parser runs; so the flag is read here instead.
    """
    argv_list = argv if args is None else args
    for i, arg in enumerate(argv_list):
        if arg in ("--env", "-e") and i + 1 < len(argv_list):
            value = argv_list[i + 1]
            argv_list.pop(i + 1)
            argv_list.pop(i)
            return value
        if arg.startswith("--env="):
            argv_list.pop(i)
            return arg.split("=", 1)[1]
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(raw) -> Optional[datetime]:
    """An aware UTC datetime, or `datetime.min` if corrupt.

    A corrupt timestamp means *already lapsed*, never *never lapses*.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        value = raw
    else:
        try:
            value = datetime.fromisoformat(str(raw))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc)


@dataclass
class ActiveEnvironment:
    """How this invocation came to have an environment, and whether it is fresh."""

    name: str
    #: ``explicit`` (`--env` or the variable), ``shell`` (`env --shell`), or
    #: ``remembered`` (`env <name>`).
    source: str
    #: When it lapses, or None if it does not.
    expires: Optional[datetime] = None
    #: The config file a remembered environment was set against.
    config_file: Optional[Path] = None
    #: Set once a lapsed environment has been confirmed for this invocation.
    approved: bool = False

    @property
    def remaining(self) -> Optional[timedelta]:
        if self.expires is None:
            return None
        return self.expires - _now()

    @property
    def lapsed(self) -> bool:
        remaining = self.remaining
        return remaining is not None and remaining <= timedelta(0)

    @property
    def needs_approval(self) -> bool:
        return self.lapsed and not self.approved


def _same_file(a: Optional[Path], b: Optional[Path]) -> bool:
    if a is None or b is None:
        return True
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


class EnvironmentState:
    """One application's remembered environment, kept in its user directory."""

    def __init__(
        self,
        app_name: str,
        *,
        env_var: str,
        expires_var: str,
        state_dir: Optional[Path] = None,
    ):
        self.app_name = app_name
        self.env_var = env_var
        self.expires_var = expires_var
        self.state_dir = Path(state_dir or get_app_dir(app_name))
        self.active: Optional[ActiveEnvironment] = None

    # -- the state file ---------------------------------------------------

    @property
    def state_file(self) -> Path:
        return self.state_dir / "environment.json"

    def _read(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, state: dict):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2))

    # -- the remembered pointer -------------------------------------------

    def remembered(self) -> Optional[ActiveEnvironment]:
        """The pointer as written, whether or not it applies here."""
        state = self._read()
        env = state.get(ACTIVE_ENV_KEY)
        if env is None:
            return None
        config = state.get(ACTIVE_ENV_CONFIG_KEY)
        config_file = None
        if config:
            config_file = Path(config)
        return ActiveEnvironment(
            name=str(env),
            source="remembered",
            expires=parse_timestamp(state.get(ACTIVE_ENV_EXPIRES_KEY)),
            config_file=config_file,
        )

    def remember(
        self,
        env: Optional[str],
        *,
        expires_in: Optional[timedelta] = None,
        config_file: Optional[Path] = None,
    ) -> Optional[datetime]:
        """Remember *env* for *config_file*; None forgets. Returns the expiry."""
        if env is None:
            self._write({})
            self.active = None
            return None
        state = {ACTIVE_ENV_KEY: env}
        if config_file is not None:
            state[ACTIVE_ENV_CONFIG_KEY] = str(Path(config_file).resolve())
        expires = None
        if expires_in is not None:
            expires = _now() + expires_in
            state[ACTIVE_ENV_EXPIRES_KEY] = expires.isoformat()
        self._write(state)
        self.active = ActiveEnvironment(
            name=env, source="remembered", expires=expires, config_file=config_file
        )
        return expires

    def renew(self, expires_in: Optional[timedelta]) -> Optional[datetime]:
        """Re-stamp the active environment's expiry from now, and approve it."""
        active = self.active
        if active is None:
            return None
        expires = None
        if expires_in is not None:
            expires = _now() + expires_in
        if active.source == "remembered":
            state = self._read()
            state.pop(ACTIVE_ENV_EXPIRES_KEY, None)
            if expires is not None:
                state[ACTIVE_ENV_EXPIRES_KEY] = expires.isoformat()
            self._write(state)
        active.expires = expires
        active.approved = True
        return expires

    # -- resolution -------------------------------------------------------

    def resolve(self, config_file: Optional[Path]) -> Optional[str]:
        """The environment for this invocation, or None. Writes nothing.

        The variable (which `--env` is folded into) always wins. A remembered
        environment applies only if it was set against this config file and
        the file still defines it; a lapsed one is kept, to be confirmed.
        """
        self.active = None

        if self.env_var in environ:
            env = environ[self.env_var]
            if env in NO_VALUE:
                environ.pop(self.env_var, None)
                return None
            expires = parse_timestamp(environ.get(self.expires_var) or None)
            source = "explicit"
            if expires is not None:
                source = "shell"
            self.active = ActiveEnvironment(name=env, source=source, expires=expires)
            return env

        state = self.remembered()
        if state is None:
            return None

        if not _same_file(state.config_file, config_file):
            log.info(
                "The remembered environment %r was set for %s and does not apply "
                "to %s; ignoring it here.",
                state.name,
                state.config_file,
                config_file,
            )
            return None

        if config_file is not None and state.name not in environments_in(config_file):
            log.warning(
                "The remembered environment %r is not defined in %s; ignoring it.",
                state.name,
                config_file,
            )
            return None

        state.config_file = config_file
        self.active = state
        return state.name
