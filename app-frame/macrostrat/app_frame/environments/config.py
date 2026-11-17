"""Finding an application's config file and layering one environment from it.

.. code-block:: toml

    log_level = "info"            # top-level keys apply everywhere

    [default]                     # then the shared base layer
    database = "postgresql://localhost/app"

    [production]                  # then the selected environment
    env_class = "production"
    database = "postgresql://db.example.org/app"

``<PREFIX>_*`` environment variables override the result, with ``__`` for
nesting: ``APP_DATABASE__PORT=5433`` sets ``database.port``.
"""

from copy import deepcopy
from os import environ
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from click import get_app_dir

from ..secrets import as_secret, is_secret_ref, redact_mapping, reveal
from .policy import DEFAULT_ENV

ENV_NESTING = "__"
NO_VALUE = ("", "none", "None")


def read_config(config_file: Optional[Path]) -> dict:
    """The parsed file, or an empty table when it is absent or unreadable."""
    if config_file is None:
        return {}
    try:
        with open(config_file, "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError, TypeError):
        return {}


def find_upwards(start_dir: Path, file_name: str) -> Optional[Path]:
    next_dir = start_dir.resolve()
    while True:
        if (next_dir / file_name).exists():
            return next_dir / file_name
        if next_dir == next_dir.parent:
            return None
        next_dir = next_dir.parent


def find_config(
    file_name: str,
    *,
    env_var: Optional[str] = None,
    app_name: Optional[str] = None,
    search: Iterable[Path] = (),
) -> Optional[Path]:
    """The config file for this invocation, or None.

    In order: the path in *env_var* (``none`` means no config), the nearest
    *file_name* at or above the working directory, then above each of *search*,
    then the user's configuration directory for *app_name*.
    """
    if env_var is not None and env_var in environ:
        if environ[env_var] in NO_VALUE:
            return None
        return Path(environ[env_var])
    starts = [Path.cwd(), *search]
    if app_name is not None:
        starts.append(Path(get_app_dir(app_name)))
        starts.append(Path.home() / ".config" / app_name)
    for start in starts:
        found = find_upwards(Path(start), file_name)
        if found is not None:
            return found
    return None


def environments_in(config_file: Optional[Path]) -> list[str]:
    """The selectable environments in a config file: its tables, bar `default`."""
    raw = read_config(config_file)
    return [k for k, v in raw.items() if isinstance(v, Mapping) and k != DEFAULT_ENV]


def deep_merge(base: Optional[Mapping], override: Optional[Mapping]) -> dict:
    """*override* on top of *base*, recursing into tables. Lists replace."""
    out = deepcopy(dict(base or {}))
    for key, value in dict(override or {}).items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def merged_environment(raw: Mapping, env: Optional[str]) -> dict:
    """Top-level keys, then ``[default]``, then ``[env]``."""
    top = {k: v for k, v in raw.items() if not isinstance(v, Mapping)}
    merged = deep_merge(top, raw.get(DEFAULT_ENV, {}))
    if env is not None:
        merged = deep_merge(merged, raw.get(env, {}))
    return merged


def _parse_env_value(text: str) -> Any:
    """A TOML-typed value if *text* parses as one, else the string itself."""
    try:
        return tomllib.loads(f"v = {text}")["v"]
    except (tomllib.TOMLDecodeError, KeyError):
        return text


def environment_overrides(
    prefix: str,
    env: Mapping[str, str] = environ,
    *,
    control_variables: Iterable[str] = (),
) -> dict:
    """``<prefix>*`` variables as a nested settings table.

    *control_variables* (given without the prefix) steer the tools themselves
    and are never settings.
    """
    control = set(control_variables)
    out: dict = {}
    for name, value in env.items():
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix) :]
        if not rest or rest in control:
            continue
        path = [p.lower() for p in rest.split(ENV_NESTING) if p]
        if not path:
            continue
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                break
        else:
            node[path[-1]] = _parse_env_value(value)
    return out


#: Setting naming the 1Password account its `op://` references are read from.
OP_ACCOUNT_KEY = "op_account"


class Settings(dict):
    """One environment's settings: a table with dotted and attribute access.

    A value that names a secret (``op://…``, ``env://…``, ``file://…``) comes
    back from `get` and attribute access as a `Secret`, which fetches nothing
    until its ``get()`` is called and never prints its value. Subscripting
    returns what the file holds.
    """

    def get(self, key, default=None):
        node = self
        for part in str(key).split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return self._wrap(node)

    def _wrap(self, value):
        if isinstance(value, Mapping):
            child = Settings(value)
            object.__setattr__(child, "_account", self._op_account())
            object.__setattr__(child, "_config_dir", self._dir())
            return child
        return as_secret(value, account=self._op_account())

    def _op_account(self) -> Optional[str]:
        inherited = self.__dict__.get("_account")
        return dict.get(self, OP_ACCOUNT_KEY, inherited)

    def _dir(self) -> Optional[Path]:
        return self.__dict__.get("_config_dir")

    def __getattr__(self, key):
        if key.startswith("_") or key not in self:
            raise AttributeError(key)
        return self.get(key)

    def secret(self, key) -> Optional[str]:
        """The plain value of *key*, resolving it now if it names a secret."""
        return reveal(self.get(key))

    def redacted(self) -> dict:
        """The table for printing: references kept, credential-ish values masked."""
        return _redact(self)

    def path(self, key, default=None) -> Optional[Path]:
        """A path setting, resolved against the config file's directory."""
        value = self.get(key, default)
        if value is None:
            return None
        value = Path(value).expanduser()
        base = self._dir()
        if not value.is_absolute() and base is not None:
            value = base / value
        return value


def _redact(table: Mapping) -> dict:
    out = {}
    flat = {}
    for key, value in table.items():
        if isinstance(value, Mapping):
            out[key] = _redact(value)
        elif is_secret_ref(value):
            # A reference names a secret; it is safe, and useful, to show.
            out[key] = value
        else:
            flat[key] = value
    out.update(redact_mapping(flat))
    return {k: out[k] for k in table}


def load_settings(
    config_file: Optional[Path],
    env: Optional[str],
    *,
    prefix: Optional[str] = None,
    control_variables: Iterable[str] = (),
    defaults: Optional[Mapping] = None,
) -> Settings:
    """*defaults*, then the file's layers for *env*, then environment variables."""
    table = deep_merge(defaults, merged_environment(read_config(config_file), env))
    if prefix is not None:
        overrides = environment_overrides(prefix, control_variables=control_variables)
        table = deep_merge(table, overrides)
    settings = Settings(table)
    # Not a key, so it never shadows a setting of the same name.
    object.__setattr__(settings, "_config_dir", None)
    if config_file is not None:
        object.__setattr__(settings, "_config_dir", Path(config_file).resolve().parent)
    return settings
