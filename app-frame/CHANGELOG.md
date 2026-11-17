# Changelog

## [3.2.0] - 2026-11-16 [_changes_](https://github.com/Macrostrat/python-libraries/compare/macrostrat.app_frame-v3.1.0...macrostrat.app_frame-v3.2.0)

- Add `macrostrat.app_frame.environments`: a TOML file with a table per environment, layered settings with `<PREFIX>_*` overrides, an active environment (`--env`, per shell, or remembered with a time-to-live), environment classes, and per-scope confirmation before access. `Environments(...).cli()` gives the `env` command. Generalized from Macrostrat's CLI.
- Add `macrostrat.app_frame.secrets`: deferred, redacting secret references (`op://`, `env://`, `file://`, `keychain://`) with a per-secret 1Password account. Environment settings return them, and the `reauthorize` level re-fetches the settings named in `credentials`.
