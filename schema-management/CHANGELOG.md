# Changelog

## [2.0.1] - 2026-11-22 [_changes_](https://github.com/Macrostrat/python-libraries/compare/macrostrat.schema_management-v2.0.0...macrostrat.schema_management-v2.0.1)

- Inspect databases for a plan through their own engines, so `connect_args` apply and URL `options` containing spaces connect.

## [2.0.0] - 2026-11-16

- Declarative schema chunks, schema diffs, sync and condition-checked migrations, generalized from Macrostrat's `schema-management` module.
- `SchemaApp` describes an application; `create_schema_cli` builds its `schema` commands.
