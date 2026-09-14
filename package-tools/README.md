# Macrostrat Package Tools

Tools for managing packages in a Python monorepo: version changes, release
status, and publishing to PyPI. Requires `uv` (version 2.0.0 and later).

The `mono` command operates on the packages listed in the root
`pyproject.toml`'s `[tool.uv.sources]`.

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
