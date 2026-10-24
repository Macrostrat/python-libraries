# Macrostrat Package Tools

Tools for managing packages in a Python monorepo: version changes, release
status, and publishing to PyPI. Requires `uv` (version 2.0.0 and later).

The `mono` command operates on the packages listed in the root
`pyproject.toml`'s `[tool.uv.sources]`, or on the directories matched by
`[tool.mono] packages` (see [Service repositories](#service-repositories)).

| Command | What it does |
| --- | --- |
| `mono install` | Lock and install every package into the root environment |
| `mono changeset` | Record a pending version change in `.changes/` |
| `mono version` | Apply pending changes: versions, dependencies, changelogs |
| `mono status` | Show the release plan; `--check` fails on problems |
| `mono build` | Build every package that is not yet published |
| `mono publish` | Build, upload and tag every unpublished package |

## Change fragments

A fragment is a markdown file in `.changes/` naming the packages it affects and
how far each should move:

```markdown
---
macrostrat.database: minor
---

- Add `copy_settings` to `template_database`.
```

`mono version` collects every fragment, takes the largest level per package,
and cascades to packages that depend on one being released — raising their
`>=` dependency floors and moving them by the level set in
`[tool.mono] update-internal-dependencies` (default `patch`, or `none` to leave
dependents alone).

## Publishing

`mono publish` builds, uploads and tags. It deliberately does not commit or
regenerate lock files, so it behaves identically on a laptop and in CI. In
GitHub Actions with `id-token: write`, `uv publish` authenticates through PyPI
trusted publishing and needs no token; locally, set `UV_PUBLISH_TOKEN`.

## Prereleases

`mono version --pre` makes prereleases, labelled by `[tool.mono]
prerelease-label` (default `beta`): a minor change on 3.2.1 gives
3.3.0-beta.1, and further changes give 3.3.0-beta.2 unless they need a larger
jump. `mono version -p <name>` releases a package at a prerelease as stable;
`-p` also limits any run to the named packages, leaving other fragment entries
in place.

## Service repositories

A repository of deployed services, released by CI tagging `<name>-vX.Y.Z` on
merge rather than by publishing, is configured like this:

```toml
[tool.mono]
packages = ["services/*"]           # each named by its directory
released-by = "tag"                 # `mono status` reads release tags; no `mono publish`
update-internal-dependencies = "none"
changelog = "unreleased"            # prereleases collect under ## [Unreleased]

[tool.mono.overrides.api-v2]
changelog = "versions"              # a heading for every release, prereleases too
```

A package may keep its version in `package.json` instead of `pyproject.toml`.
Changelog headings link to a tag comparison only when `[tool.mono] repository`
is set.

