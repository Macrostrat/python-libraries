# Change fragments

Each file here records a pending version change, to be applied at release time.
Add one in the same pull request as the change it describes:

```bash
uv run mono changeset
```

A fragment names the packages it affects and how far each should move, followed
by the changelog entry:

```markdown
---
macrostrat.database: minor
---

- Add `copy_settings` to `template_database`.
```

Levels are `patch`, `minor` and `major`. A package that depends on one being
released moves too — see `update-internal-dependencies` in the root
`pyproject.toml`.

`mono version` applies every fragment here, raises versions and intra-repo
dependency floors, writes each package's `CHANGELOG.md`, refreshes lock files,
and deletes the fragments it applied. `mono status` shows the plan without
changing anything.
