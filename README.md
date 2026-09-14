# Macrostrat Python libraries

A monorepo containing Python-based tools and libraries for Earth data projects.

- This is still very early-stage.
- The intent is to share common subsystems between Sparrow, Macrostrat and other
  tools.
- All modules can be consumed as PyPI packages, or embedded locally as a
  submodule (though this is less-recommended).

## Modules

- `macrostrat.app_frame`: A control framework for managing Dockerized
  applications. Currently used by Sparrow, Mapboard GIS, and Macrostrat.
- `macrostrat.auth_system`: Authentication utilities
- `macrostrat.database`: Database connection and query utilities geared towards
  PostgreSQL
- `macrostrat.dinosaur`: Utilities for on-the-fly database migration and
  conformance testing
- `macrostrat.package_tools`: Monorepo versioning and PyPI publishing utilities
- `macrostrat.raster_index`: An index of cloud-optimized rasters (COGs), grouped
  into named layers, with the `raster_layers` schema and a registration CLI
- `macrostrat.raster_layers`: Serves those layers as mosaicked map tiles, as
  FastAPI routes mountable in any application
- `macrostrat.utils`: Helpers for logging and command-line apps

## Development

You need `python >= 3.10` and the [`uv`](https://docs.astral.sh/uv/) package
manager to develop the modules here.
Running `uv sync` (aliased to `make install`) bootstraps the project in a local
virtual environment.

Dependencies can be installed by adding them to the respective `pyproject.toml`
files or by running `uv add ...`.
Keep development dependencies (e.g., for testing) separate from core package
dependencies using `uv add --dev ...`.

## Testing

Tests can be run using `make test`, or, for added control, `uv run pytest ...`.
Docker is required to run all tests, as some of them require several containers.

### Testing the `macrostrat.app_frame` module

The `app_frame` module can be tested using a simple mock application,
which can be controlled using the `uv run test-app` command. This command
presents the application's CLI interface, which can be used to start and stop
the application, e.g. with `uv run test-app up`.

## Releasing on PyPI

This repository is designed to facilitate rapid iteration of its components and
release to PyPI. All modules are part of the `macrostrat` namespace package:
`macrostrat.database`, `macrostrat.dinosaur`, `macrostrat.utils`, etc.

Releases are driven by **change fragments** collected in `.changes/`, and
merging to `main` publishes. This mirrors the
[changesets](https://github.com/changesets/changesets) workflow used by
[Macrostrat's web component libraries](https://github.com/UW-Macrostrat/web-components),
so the two monorepos can be reasoned about the same way — see
[Parallels with `web-components`](#parallels-with-web-components) below.

The tooling is the `mono` command, from the `macrostrat.package_tools` module in
this repository; [its README](package-tools/README.md) documents each
subcommand.

### 1. Record the change, in the pull request that makes it

```bash
uv run mono changeset
```

This asks which packages changed, how far each should move (`patch`, `minor` or
`major`), and what the changelog entry says. It writes a small markdown file to
`.changes/`:

```markdown
---
macrostrat.database: minor
---

- Add `copy_settings` to `template_database`.
```

**Commit the fragment alongside your change.** Do not edit `version` fields in
`pyproject.toml` or write `CHANGELOG.md` entries by hand — `mono version` owns
both, and hand-edits are what the release check catches.

Fragments accumulate across pull requests. Several may name the same package;
the largest level wins and the entries are collected together.

### 2. See the release plan

```bash
uv run mono status
```

This prints each package's local version, its version on PyPI, what publishing
will do with it on merge, and any pending change fragments that apply to it.

`mono status --check` runs as a required check on every pull request
(`.github/workflows/testing.yml`). It fails if a package's version moved without
a `CHANGELOG.md` entry for it, if a public package has no changelog at all, or
if a package's `uv.lock` is stale. The point is that the publish set is visible
*before* the merge rather than at publish time.

### 3. Turn the pending changes into a release

When the accumulated fragments should become a release, apply them:

```bash
uv run mono version
```

This raises each affected package's version, cascades to packages that depend on
it within the monorepo, writes each `CHANGELOG.md` entry, refreshes lock files,
and deletes the fragments it applied. Review the diff — it is an ordinary
change, and the pull request that carries it is the release.

Use `mono version --dry-run` to see the resulting versions without writing
anything.

#### The dependency cascade

A package that depends on one being released moves too: its `>=` floor on that
dependency is raised to the new version, and it gets its own release. How far it
moves is set by `update-internal-dependencies` in the root `pyproject.toml`:

```toml
[tool.mono]
update-internal-dependencies = "patch"  # or "minor", "major", "none"
```

The cascade is transitive, so a change to `macrostrat.utils` moves
`macrostrat.database` and, through it, `macrostrat.dinosaur`. This is the
equivalent of the changesets option of the same name. Set it to `"none"` to
leave dependents alone and manage their floors by hand.

### 4. Publishing happens in CI

`.github/workflows/release.yaml` runs on push to `main`. It publishes every
package whose version is not yet on PyPI, then pushes a
`macrostrat.<package>-v<version>` tag for each.

Authentication is [PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/)
through the `pypi` GitHub environment: there is no API token anywhere, and
approving that environment is the release gate. Each package needs a pending
publisher registered on PyPI for the `UW-Macrostrat/python-libraries`
repository, the `release.yaml` workflow and the `pypi` environment.

Nothing about a release is irreversible until this step. A version that reaches
PyPI can never be reused, so recovery from a bad release is a new patch version,
never a retry of the same one.

### Publishing by hand

Publishing locally is a fallback, not the normal path — prefer merging to
`main`. If you do need it, set `UV_PUBLISH_TOKEN` to a PyPI API token and run:

```bash
uv run mono publish --dry-run   # report what would be published
uv run mono publish             # build, upload and tag
git push --tags
```

`mono publish` does not commit or regenerate lock files, so it does the same
thing locally as it does in CI. It refuses to run against a dirty working tree
(`--allow-dirty` overrides). It publishes *every* package whose version is not
yet on PyPI, not only the one you had in mind — read the dry run first.

A package is never published if it carries the `Private :: Do Not Upload`
classifier, or if it has no `classifiers` field at all.

### Parallels with `web-components`

The two monorepos are deliberately the same shape:

| | `web-components` | here |
| --- | --- | --- |
| Record a change | `yarn changeset` | `uv run mono changeset` |
| Where it lives | `.changeset/*.md` | `.changes/*.md` |
| Release plan | `yarn run status` | `uv run mono status` |
| Apply versions and changelogs | `yarn run update-versions` | `uv run mono version` |
| Dependents of a released package | `updateInternalDependencies` | `tool.mono.update-internal-dependencies` |
| Publish | CI, on push to `main` | CI, on push to `main` |
| Tags | `<package>-v<version>` | `macrostrat.<package>-v<version>` |

In both repositories, applying the versions is a deliberate step taken when a
release is wanted, not an automatic one — only publishing is automatic. The
substantive difference is authentication: publishing here uses trusted
publishing, so there is no token to store or rotate, where `web-components`
publishes with an npm token held as a repository secret.

## Structure and similar projects

- [Macrostrat's web component libraries](https://github.com/UW-Macrostrat/web-components)
  are also structured as a monorepo, and use the same change-fragment release
  model — see [Parallels with `web-components`](#parallels-with-web-components).
- [Opendoor Labs' Python monorepo](https://medium.com/opendoor-labs/our-python-monorepo-d34028f2b6fa)
  is a reference for code organization
