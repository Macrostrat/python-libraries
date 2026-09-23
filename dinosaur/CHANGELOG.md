# Changelog

## [4.2.2] - 2026-09-23 [_changes_](https://github.com/Macrostrat/python-libraries/compare/macrostrat.dinosaur-v4.2.1...macrostrat.dinosaur-v4.2.2)

- Require `macrostrat.database` 4.7.0 or newer.

## [4.2.1] - 2026-08-12

- Fix `database_cluster` on testcontainers 4.x, which now sets `tmpfs` itself and
  errored on the value passed through `with_kwargs`

## [4.2.0] - 2026-06-19

- Rework `upgrade_cluster` function and remove legacy `upgrade_cluster_legacy`
  and associated methods
- Upgrade tests

## [4.1.0] - 2026-06-11

- Switch to `results` and deprecate `migra` and `schemainspect`

## [4.0.0] - 2026-04-29

- Upgrade to `macrostrat.database` v4.0.0
- Use testcontainers for managing docker containers
- New root-level `database_cluster` functions
