# Changelog

## [1.1.1] - 2026-09-23 [_changes_](https://github.com/Macrostrat/python-libraries/compare/macrostrat.auth_system-v1.1.0...macrostrat.auth_system-v1.1.1)

- Require `macrostrat.database` 4.7.0 or newer.

All notable changes to this project will be documented in this file.

## [0.1.0] - 2024-10-17

### Bring in legacy Sparrow authentication system

We started this module by copying Sparrow's authentication code
at commit `ff0284620462fcaff127069ee00cef91b6412fa5` (2024-09-19). We have
begun by excising Sparrow-specific code and replacing it with a more
generalized authentication system.

- Remove user model code from `sparrow.database`
- Get all tests to pass by mocking database

There are now 16 passing tests of the old auth system!
These can be run with `poetry run pytest auth-system`

### Begin bringing in Macrostrat's newer ORCID-based authentication system

- Copied the Macrostrat v2 security model from Macrostrat-xdd repository [commit
  `79330fa`](https://github.com/UW-Macrostrat/macrostrat-xdd/commit/79d30fa3fe3be62ca80cedc69752d3825fabadbf).
- Made minimal changes to align with the new module structure.

## [1.0.0]

- Integrate the system more closely with the `macrostrat.database` module
- Update to newer versions of `pyjwt` and `werkzeug`.
- Use `ContextVar` rather than global variables for session storage.
- Rename `orcid` -> `core` to reflect the uncertain scope of the module.
