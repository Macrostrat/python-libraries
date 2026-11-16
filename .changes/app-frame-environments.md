---
macrostrat.app_frame: minor
---

- Add `macrostrat.app_frame.environments`: a TOML file with a table per environment, layered settings with `<PREFIX>_*` overrides, an active environment (`--env`, per shell, or remembered with a time-to-live), environment classes, and per-scope confirmation before access. `Environments(...).cli()` gives the `env` command. Generalized from Macrostrat's CLI.
- Add `macrostrat.app_frame.secrets`: deferred, redacting secret references (`op://`, `env://`, `file://`, `keychain://`) with a per-secret 1Password account. Environment settings return them, and the `reauthorize` level re-fetches the settings named in `credentials`.
