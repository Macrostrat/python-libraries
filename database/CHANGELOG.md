# Changelog

## [4.7.0] - 2026-09-23 [_changes_](https://github.com/Macrostrat/python-libraries/compare/macrostrat.database-v4.6.1...macrostrat.database-v4.7.0)

- Print each statement's summary _before_ it runs in `run_sql` and
  `run_fixtures`, so a long-running or hung statement is identifiable. On an
  interactive terminal, a spinner and elapsed time are shown on the same line
  if the statement runs for more than two seconds (configurable with
  `MACROSTRAT_ACTIVITY_DELAY`), and an interrupted statement is left in red.
- Add `macrostrat.database.progress.activity` for showing the same progress
  indicator around other blocking work.
- `run_query` no longer prints anything by default (it previously printed only
  on some error paths); pass `output_mode` to enable output.

## [4.6.1] - 2026-08-28

- Fix `template_database(close_source_connections=True)` leaving the source
  unusable: evicting its sessions kills the caller's pooled connections too, so
  they are now discarded (`discard_stale_connections`) and the next use reconnects.

## [4.6.0] - 2026-08-28

- Add `copy_settings` to `template_database` (default on): replay the source's
  database-level settings onto the copy, which `CREATE DATABASE ... TEMPLATE`
  does not do. Exposed separately as `copy_database_settings`.
- Fix `drop_database(force=True)` connecting with no database name, which let
  libpq fall back to `PGDATABASE` and target an unrelated database.
- Add `maintenance_url` for cluster-level connections.
- `template_database(close_source_connections=True)` now evicts sessions from a
  maintenance connection, so it can clear the last one.

## [4.5.0] - 2026-07-05

- Add `on_error` hook to `Database.run_query` to allow for custom error handling
  and recovery. This can support retrying queries and adjusting their behavior
  as needed.

## [4.4.1] - 2026-06-26

- Fix executemany functionality in `Database.run_query`.

## [4.4.0] - 2026-06-24

- Rework `Database` class creation to preserve initialized engines if passed.
  Remove `recreate` parameter.
- Remove SQL formatting to speed up query execution.

## [4.3.1] - 2026-06-19

Add sequence management utilities to the database module (for database sequence
updates and value resets).

## [4.3.0] - 2026-06-19

- Add `Database.get_table` and `Database.get_model` methods for easier access to
  object mappings.
- Improve tests
- Improve and fix bugs with `temporary_database` and `template_database` context
  managers, which are useful for testing.
- Use more consistent and open-ended approaches to passing database instances,
  urls, and connection strings.

## [4.2.1] - 2026-06-18

Fix logic for `OutputMode.NONE` in `run_sql` function, which was not properly
suppressing output.

## [4.2.0] - 2026-06-17

- Add `template_database` context manager to create a database with an existing
  template.
- Rename `temp_database` -> `temporary_database` to align names with
  `template_database`.
- Reorganize imports for database query utilities

## [4.1.1] - 2026-06-12

- Fix a bug in with escape sequences.

## [4.1.0] - 2026-06-11

- Add new tests of client cursor for binding parameters in a Psycopg2-compatible
  way
- Add new parameter escaping of `:variable::text`-style SQLAlchemy+Postgres type
  casts, for more flexibility in query construction
- Updated dependency versions

## [4.0.2] - 2026-05-03

- Change the `upsert` function to infer primary key columns from the table
  metadata, and to not error if there is a empty update clause.

## [4.0.1] - 2026-04-29

- Change default PostgreSQL insert mode to `restrict`, making on-conflict
  behavior explicitly opt-in.
- Added an `upsert` function
- Add conversion of Psycopg2 SQL types to Psycopg3 types to allow for smoother
  migration of parameter binding code.
- Moved all `run_sql` utility functions to `macrostrat.database.query`
- Add a `transform_statement` function to `run_sql` to allow for transformation
  of statements before execution, which can be used to add parameters or other
  modifications to statements in a SQL file.

## [4.0.0] - Unreleased

- Upgrade to `psycopg` version 3 instead of Psycopg2
- `psycopg`'s new
  [adaptation system](https://www.psycopg.org/psycopg3/docs/advanced/adapt.html)
  means that parameter binding has changed substantially. Consequently, `AsIs`
  and other parameter-binding extensions are no longer supported.
- `psycopg`'s new
  [async mode](https://www.psycopg.org/psycopg3/docs/advanced/async.html#interrupting-async-operations)
  allows us to remove the `set_wait_callback` approach to waiting for
  long-running operations
- This is a major breaking change, but it should be mostly transparent, except
  for a need to update custom parameter binding code.

## [3.5.3] - 2024-12-23

- Fix errors and add tests for `run_sql` changes
- Rename `PrintMode` -> `OutputMode`

## [3.5.2] - 2024-12-23

- Add the ability to print less with the `run_sql` function
- Add a `Database.refresh_schema` method

## [3.5.1] - 2024-12-21

- Add a `statement_filter` parameter to the `run_sql` function to allow for
  filtering of statements in a SQL file.
- Improved the consistency of the `Database.run_sql` function with the `run_sql`
  utility function.

## [3.5.0] - 2024-11-25

- Add database transfer utilities for asynchronous `pg_load` and `pg_dump`
  operations.

## [3.4.1] - 2024-10-28

- Update the underlying version of `sqlparse` and `geoalchemy2`.

## [3.4.0] - 2024-10-17

- Add capability to instantiate the `Database` wrapper class from an engine as
  well as a URL.

## [3.3.0] - 2024-03-30

This release focuses on nicer semantics for applying database fixtures.

- Mark `Database.reflect_table` function as deprecated.
- Add a `Database.instance_params` dictionary to store common parameters that
  can be applied to any queries

## [3.2.0] - 2024-03-13

Add `macrostrat.database.transfer` module to incorporate functions for
streamlining `pg_dump`, `pg_restore` and similar operations.

## [3.0.0] - 2024-01-04

- Switch to sqlalchemy v2
- Improve database automapping

## [2.1.2] - 2023-05-16

- Added a `has_server_binds` method to the `run_sql` utility function to allow
  parameter-style inference to be disabled.
- Added some tests and documentation for the `run_sql` function.
