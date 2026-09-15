# Macrostrat Python libraries

A monorepo containing Python-based tools and libraries for Earth data projects.
The intent is to share common subsystems between Sparrow, Macrostrat and other
tools. Modules can be consumed as PyPI packages, or embedded locally as a
submodule (though this is less recommended).

## Modules

All modules are part of the `macrostrat` namespace package:

- `macrostrat.app_frame`: A control framework for managing Dockerized
  applications. Currently used by Sparrow, Mapboard GIS, and Macrostrat.
- `macrostrat.auth_system`: Authentication utilities
- `macrostrat.database`: Database connection and query utilities geared towards
  PostgreSQL
- `macrostrat.dinosaur`: Utilities for on-the-fly database migration and
  conformance testing. **Deprecated**
- `macrostrat.package_tools`: Monorepo versioning and PyPI publishing utilities
- `macrostrat.raster_index`: An index of cloud-optimized rasters (COGs), grouped
  into named layers, with the `raster_layers` schema and a registration CLI
- `macrostrat.raster_layers`: Serves those layers as mosaicked map tiles, as
  FastAPI routes mountable in any application
- `macrostrat.utils`: Helpers for logging and command-line apps

A package with the `Private :: Do Not Upload` classifier, or without a
`classifiers` field, is not included in publishing.

## Development

You need `python >= 3.10` and the [`uv`](https://docs.astral.sh/uv/) package
manager to develop the modules here. Running `uv sync` (aliased to
`make install`) bootstraps the project in a local virtual environment.

Dependencies can be installed by adding them to the respective `pyproject.toml`
files or by running `uv add ...` or `uv add --dev ...` for development
dependencies.

## Testing

Tests can be run using `make test` or, equivalently, `uv run pytest ...`. Docker
is required to run all tests, as several of them require a database.

The `macrostrat.app_frame` module is tested using a simple mock application. The
`uv run test-app` command can be used to control the mock application (e.g. with
`uv run test-app up`).

## Releasing on PyPI

This repository supports a CI-based PyPI release cycle. The change-fragment
release model is borrowed from
[Macrostrat's web component libraries](https://github.com/UW-Macrostrat/web-components),
which use the standard [Changesets](https://github.com/changesets/changesets)
tool. Here, we use a custom approach in the `mono` command
(`macrostrat.package_tools` module).

### Capture _Changesets_

Changesets should be created during development:

```bash
uv run mono changeset
```

This asks which packages changed, how far each should move (`patch`, `minor` or
`major`), and what the changelog entry says. Changes are queued in `.changes/`,
e.g.:

```markdown
---
macrostrat.database: minor
---

- Add `copy_settings` to `template_database`.
```

Fragments accumulate alongside changes. avoid editing `version` fields in
`pyproject.toml` or write `CHANGELOG.md` entries by hand.

### See the release plan

`uv run mono status` prints each package's local version, its version on PyPI,
and any pending actions. `mono status --check` runs as a required check on every
pull request, failing if changelogs are missing or outdated, or if lock files
are out of date.

### Create a release

When a release is ready, `uv run mono version` raises each affected package's
version, cascades to dependents, writes each `CHANGELOG.md` entry, refreshes
lock files, and deletes the fragments it applied. `mono version --dry-run`
builds versions without writing anything.

### Publishing to PyPI (GitHub Actions)

`.github/workflows/release.yaml` runs on push to `main`. It publishes every
package whose version is not yet on PyPI, then pushes a
`macrostrat.<package>-v<version>` tag for each.

Authentication is
[PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/) through the
`pypi` GitHub environment: there is no API token anywhere, and approving that
environment is the release gate. Each package needs a pending publisher
registered on PyPI for the `UW-Macrostrat/python-libraries` repository, the
`release.yaml` workflow and the `pypi` environment.

Nothing about a release is irreversible until this step. A version that reaches
PyPI can never be reused, so recovery from a bad release is a new patch version,
never a retry of the same one.

### Publishing by hand

> [!note] Publishing locally Publishing locally requires setting
> `UV_PUBLISH_TOKEN` to a PyPI API token and running:
>
> ```bash
> uv run mono publish --dry-run   # report what would be published
> uv run mono publish             # build, upload and tag
> git push --tags
> ```
