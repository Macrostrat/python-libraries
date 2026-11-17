# Macrostrat manager

This is a CLI toolset for managing data and services for a Dockerized
application. It was originally developed for the Sparrow application, but it has
been generalized for use with other projects including Macrostrat.

`uv run test-app` can be used to test various interactions.

## Environments

`macrostrat.app_frame.environments` gives an application a set of named
environments, each a table in a TOML file found at or above the working
directory:

```toml
[default]
database = "postgresql://localhost/field"

[development]
env_class = "development"
database = "postgresql://dev.example.org/field"

[production]
env_class = "production"
confirm = { data = "environment-name", schema = "environment-name" }
```

```python
from macrostrat.app_frame import AccessScope, Environments

environments = Environments("field")          # field.toml, FIELD_* variables
environments.settings.get("database")         # top level, [default], [env], then FIELD_DATABASE
environments.require_access(AccessScope.Schema, action="schema migration")
cli.add_typer(environments.cli(), name="env")  # field env [NAME] [--shell] [--unset] [--list]
```

- **Selection.** `--env NAME` for one command, `FIELD_ENV` for a process,
  `eval "$(field env --shell NAME)"` for a shell, or `field env NAME` to
  remember it. A remembered environment applies only to the config file it was
  set against and lapses after a time-to-live (never for `local`, 8 h
  development, 1 h staging, 15 min production; `active_ttl` overrides). A
  lapsed one is kept and confirmed before use.
- **Classes.** `env_class` is `local`, `development`, `staging` or
  `production`. An environment that declares none is treated as production,
  unless it is named `local`.
- **Confirmation.** Each class has a default level per scope (`read`, `data`,
  `schema`): `none`, `prompt` (y/N, or `--yes`), `environment-name` (typed at a
  terminal; nothing else satisfies it) and `reauthorize` (typed, then the
  application's `reauthorize` hook, e.g. re-fetching a credential). `confirm`
  overrides them.
- **Overrides.** `FIELD_*` variables override settings, with `__` for nesting.
  With no config file, the variables alone describe the environment.
- **Secrets.** A setting may name a secret instead of holding one:
  `database = "op://vault/item/field"` (1Password), `env://VAR`, `file:///path`
  or `keychain://service/account`. `settings.get` returns a `Secret`, which
  fetches nothing until `.get()` and never prints its value;
  `settings.secret(key)` resolves it. `op_account` names the 1Password account.
  `Environments(..., credentials=["database"])` makes the `reauthorize` level
  fetch those settings again, so the secret manager's approval is in the path.

`macrostrat.app_frame.secrets` can be used on its own (`as_secret`, `reveal`,
`register_resolver`, and helpers that redact credentials from printed output).
