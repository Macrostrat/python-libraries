# Changelog

## [2.2.0] - 2026-10-24 [_changes_](https://github.com/Macrostrat/python-libraries/compare/macrostrat.package_tools-v2.1.0...macrostrat.package_tools-v2.2.0)

- Version deployed services: directory-glob packages, `package.json` versions, release tags as the release record (`released-by = "tag"`), prereleases (`mono version --pre`), `-p` to release selected packages, and an `[Unreleased]` changelog mode
- Changelog compare links need `[tool.mono] repository`

## [2.1.0] - 2026-09-14 [_changes_](https://github.com/Macrostrat/python-libraries/compare/macrostrat.package_tools-v2.0.1...macrostrat.package_tools-v2.1.0)

- Add `mono status`, `mono changeset` and `mono version` for
  change-fragment-driven releases.
- Split building from publishing (`mono build`), and make `mono publish` pure:
  it no longer regenerates lock files or commits to the repository, so it can
  run in CI.
- Support PyPI trusted publishing by not forcing token authentication when no
  token is set.
