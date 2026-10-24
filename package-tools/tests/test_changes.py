from pathlib import Path
from textwrap import dedent

from pytest import fixture, raises

from macrostrat.package_tools.changes import (
    ChangeError,
    apply_releases,
    consume_fragments,
    max_level,
    next_version,
    parse_fragment,
    plan_releases,
    raise_dependency_floor,
    read_fragments,
    set_version,
    write_fragment,
)
from macrostrat.package_tools.packages import find_package, find_packages

ROOT_CONFIG = """
[project]
name = "example.monorepo"
version = "1.0.0"

[tool.uv]
package = false

[tool.mono]
update-internal-dependencies = "patch"

[tool.uv.sources]
"example.utils" = {{ path = "./utils", editable = true }}
"example.database" = {{ path = "./database", editable = true }}
"example.dinosaur" = {{ path = "./dinosaur", editable = true }}
"""

PACKAGE_CONFIG = """
[project]
name = "{name}"
version = "{version}"
classifiers = ["Programming Language :: Python :: 3"]
dependencies = [{dependencies}]
"""


def write_package(root: Path, directory: str, name: str, version: str, deps=()):
    path = root / directory
    path.mkdir()
    dependencies = ", ".join(f'"{d}"' for d in deps)
    (path / "pyproject.toml").write_text(
        PACKAGE_CONFIG.format(name=name, version=version, dependencies=dependencies)
    )
    (path / "CHANGELOG.md").write_text("# Changelog\n")
    return path


@fixture
def repo(tmp_path: Path) -> Path:
    """A miniature monorepo: dinosaur -> database -> utils."""
    (tmp_path / "pyproject.toml").write_text(
        ROOT_CONFIG.replace("{{", "{").replace("}}", "}")
    )
    write_package(tmp_path, "utils", "example.utils", "1.2.3")
    write_package(
        tmp_path, "database", "example.database", "4.0.0", ["example.utils>=1.2.0,<2"]
    )
    write_package(
        tmp_path,
        "dinosaur",
        "example.dinosaur",
        "2.5.1",
        ["example.database>=4.0.0,<5.0.0", "example.utils>=1.2.0,<2"],
    )
    return tmp_path


def test_next_version():
    assert next_version("1.2.3", "patch") == "1.2.4"
    assert next_version("1.2.3", "minor") == "1.3.0"
    assert next_version("1.2.3", "major") == "2.0.0"


def test_max_level():
    assert max_level(None, "patch") == "patch"
    assert max_level("patch", "minor") == "minor"
    assert max_level("major", "patch") == "major"


def test_parse_fragment(tmp_path: Path):
    path = tmp_path / "fragment.md"
    path.write_text(dedent("""\
            ---
            example.database: minor
            ---

            - Add a thing.
            """))
    fragment = parse_fragment(path)
    assert fragment.bumps == {"example.database": "minor"}
    assert fragment.bullets == ["- Add a thing."]


def test_prose_fragment_becomes_one_bullet(tmp_path: Path):
    path = tmp_path / "fragment.md"
    path.write_text("---\nexample.utils: patch\n---\n\nFix a thing\nthat was broken.\n")
    assert parse_fragment(path).bullets == ["- Fix a thing that was broken."]


def test_bad_level_is_rejected(tmp_path: Path):
    path = tmp_path / "fragment.md"
    path.write_text("---\nexample.utils: enormous\n---\n\nNope.\n")
    with raises(ChangeError):
        parse_fragment(path)


def test_unknown_package_is_rejected(repo: Path):
    write_fragment(repo, {"example.nonexistent": "patch"}, "Nope.")
    with raises(ChangeError):
        plan_releases(read_fragments(repo), find_packages(repo), repo)


def test_cascade_is_transitive(repo: Path):
    write_fragment(repo, {"example.utils": "minor"}, "- Add a helper.")
    releases = {
        r.package.name: r
        for r in plan_releases(read_fragments(repo), find_packages(repo), repo)
    }

    assert releases["example.utils"].to_version == "1.3.0"
    assert not releases["example.utils"].cascaded
    # database depends on utils, dinosaur on database
    assert releases["example.database"].to_version == "4.0.1"
    assert releases["example.dinosaur"].to_version == "2.5.2"
    assert releases["example.dinosaur"].cascaded


def test_cascade_can_be_disabled(repo: Path):
    config = repo / "pyproject.toml"
    config.write_text(
        config.read_text().replace(
            'update-internal-dependencies = "patch"',
            'update-internal-dependencies = "none"',
        )
    )
    write_fragment(repo, {"example.utils": "minor"}, "- Add a helper.")
    releases = plan_releases(read_fragments(repo), find_packages(repo), repo)
    assert [r.package.name for r in releases] == ["example.utils"]


def test_largest_level_wins(repo: Path):
    write_fragment(repo, {"example.utils": "patch"}, "- A fix.")
    write_fragment(repo, {"example.utils": "major"}, "- A breaking change.")
    releases = plan_releases(read_fragments(repo), find_packages(repo), repo)
    utils = next(r for r in releases if r.package.name == "example.utils")
    assert utils.to_version == "2.0.0"
    assert sorted(utils.bullets) == ["- A breaking change.", "- A fix."]


def test_set_version_preserves_the_rest_of_the_file(repo: Path):
    package = find_packages(repo)[0]
    original = package.read_text()
    set_version(package, "9.9.9")
    assert package.version == "9.9.9"
    assert (
        original.replace('version = "1.2.3"', 'version = "9.9.9"')
        == package.read_text()
    )


def test_dependency_floor_is_raised_but_not_the_ceiling(repo: Path):
    database = next(p for p in find_packages(repo) if p.name == "example.database")
    assert raise_dependency_floor(database, "example.utils", "1.3.0")
    assert "example.utils>=1.3.0,<2" in database.read_text()


def test_apply_writes_versions_changelogs_and_floors(repo: Path):
    write_fragment(repo, {"example.utils": "minor"}, "- Add a helper.")
    releases = plan_releases(read_fragments(repo), find_packages(repo), repo)
    apply_releases(releases, repo)

    packages = {p.name: p for p in find_packages(repo)}
    assert packages["example.utils"].version == "1.3.0"
    assert packages["example.database"].version == "4.0.1"

    utils_changelog = packages["example.utils"].changelog_file.read_text()
    assert "## [1.3.0]" in utils_changelog
    assert "- Add a helper." in utils_changelog

    # The dependent records why it moved, and its floor follows the release.
    database_text = packages["example.database"].read_text()
    assert "example.utils>=1.3.0,<2" in database_text
    database_changelog = packages["example.database"].changelog_file.read_text()
    assert "Require `example.utils` 1.3.0 or newer." in database_changelog


def test_changelog_entries_accumulate_newest_first(repo: Path):
    for message, level in [("- First.", "minor"), ("- Second.", "patch")]:
        write_fragment(repo, {"example.utils": level}, message)
        apply_releases(
            plan_releases(read_fragments(repo), find_packages(repo), repo), repo
        )
        for fragment in read_fragments(repo):
            fragment.path.unlink()

    changelog = (repo / "utils" / "CHANGELOG.md").read_text()
    assert changelog.startswith("# Changelog\n")
    assert changelog.index("## [1.3.1]") < changelog.index("## [1.3.0]")


def test_compare_link_needs_a_repository(repo: Path):
    write_fragment(repo, {"example.utils": "patch"}, "- A fix.")
    apply_releases(plan_releases(read_fragments(repo), find_packages(repo), repo), repo)
    assert "_changes_" not in (repo / "utils" / "CHANGELOG.md").read_text()

    config = repo / "pyproject.toml"
    config.write_text(
        config.read_text().replace(
            "[tool.mono]", '[tool.mono]\nrepository = "https://github.com/example/repo"'
        )
    )
    write_fragment(repo, {"example.utils": "patch"}, "- Another fix.")
    apply_releases(plan_releases(read_fragments(repo), find_packages(repo), repo), repo)
    assert (
        "https://github.com/example/repo/compare/example.utils-v1.2.4...example.utils-v1.2.5"
        in (repo / "utils" / "CHANGELOG.md").read_text()
    )


# --- prereleases --------------------------------------------------------------


def test_prerelease_from_stable():
    assert next_version("3.2.1", "minor", "beta") == "3.3.0-beta.1"
    assert next_version("3.2.1", "patch", "beta") == "3.2.2-beta.1"


def test_prerelease_counts_up_within_its_base():
    assert next_version("3.3.0-beta.1", "patch", "beta") == "3.3.0-beta.2"
    assert next_version("3.3.0-beta.1", "minor", "beta") == "3.3.0-beta.2"
    # uv's normalized spelling reads the same
    assert next_version("3.3.0b2", "patch", "beta") == "3.3.0-beta.3"


def test_prerelease_moves_base_for_a_larger_change():
    assert next_version("3.3.0-beta.2", "major", "beta") == "4.0.0-beta.1"
    assert next_version("3.2.1-beta.1", "minor", "beta") == "3.3.0-beta.1"


def test_prerelease_label_change_restarts_count():
    assert next_version("3.3.0-alpha.4", "patch", "beta") == "3.3.0-beta.1"


def test_stable_from_prerelease():
    assert next_version("3.3.0-beta.2", "patch") == "3.3.0"
    assert next_version("3.3.0-beta.2", "minor") == "3.3.0"
    assert next_version("3.3.0-beta.2", "major") == "4.0.0"
    assert next_version("4.0.0-beta.1", "major") == "4.0.0"


# --- service repositories --------------------------------------------------------

SERVICES_CONFIG = """
[project]
name = "example.services"
version = "0.1.0"

[tool.mono]
packages = ["services/*"]
update-internal-dependencies = "none"
released-by = "tag"
changelog = "unreleased"

[tool.mono.overrides.web]
changelog = "versions"
"""

SERVICE_CHANGELOG = """\
# Changelog

## [Unreleased]

- Hand-written entry

## [3.2.1] - 2026-10-22

- Older entry
"""


@fixture
def services(tmp_path: Path) -> Path:
    """Services named by directory: a Python one and a Node one."""
    (tmp_path / "pyproject.toml").write_text(SERVICES_CONFIG)
    api = tmp_path / "services" / "api"
    api.mkdir(parents=True)
    (api / "pyproject.toml").write_text(
        '[project]\nname = "api-service"\nversion = "3.2.1"\n'
    )
    (api / "CHANGELOG.md").write_text(SERVICE_CHANGELOG)
    web = tmp_path / "services" / "web"
    web.mkdir()
    (web / "package.json").write_text(
        '{\n  "name": "@example/web",\n  "version": "2.5.0"\n}\n'
    )
    (web / "CHANGELOG.md").write_text(
        "# Web changelog\n\n## [2.5.0] - 2026-10-22\n\n- Old\n"
    )
    (tmp_path / "services" / "notes").mkdir()
    return tmp_path


def release(root: Path, **kwargs):
    fragments = read_fragments(root)
    releases = plan_releases(fragments, find_packages(root), root, **kwargs)
    apply_releases(releases, root)
    consume_fragments(fragments, {r.package.name for r in releases})
    return {r.package.name: r for r in releases}


def test_services_are_named_by_directory(services: Path):
    packages = find_packages(services)
    assert [p.name for p in packages] == ["api", "web"]
    assert packages[1].version == "2.5.0"
    assert packages[1].tag == "web-v2.5.0"


def test_package_json_version_is_written(services: Path):
    web = find_package("web", services)
    set_version(web, "2.6.0-beta.1")
    assert web.version == "2.6.0-beta.1"
    assert '"name": "@example/web"' in web.read_text()


def test_prerelease_collects_under_unreleased(services: Path):
    write_fragment(services, {"api": "minor"}, "- New route")
    releases = release(services, prerelease="beta")
    assert releases["api"].to_version == "3.3.0-beta.1"

    changelog = (services / "services" / "api" / "CHANGELOG.md").read_text()
    assert changelog.startswith(
        "# Changelog\n\n## [Unreleased]\n\n- Hand-written entry\n- New route\n\n## [3.2.1]"
    )


def test_stable_release_seals_unreleased(services: Path):
    write_fragment(services, {"api": "minor"}, "- New route")
    release(services, prerelease="beta")
    releases = release(services, only=["api"])
    assert releases["api"].to_version == "3.3.0"

    changelog = (services / "services" / "api" / "CHANGELOG.md").read_text()
    sealed = "## [Unreleased]\n\n## [3.3.0] - "
    assert sealed in changelog
    assert changelog.index("- New route") < changelog.index("## [3.2.1]")
    assert "_changes_" not in changelog


def test_override_gives_prereleases_their_own_heading(services: Path):
    write_fragment(services, {"web": "patch"}, "- A fix")
    release(services, prerelease="beta")
    changelog = (services / "services" / "web" / "CHANGELOG.md").read_text()
    assert "## [2.5.1-beta.1] - " in changelog
    assert "Unreleased" not in changelog


def test_only_releases_named_packages_and_keeps_the_rest(services: Path):
    fragment = write_fragment(
        services, {"api": "patch", "web": "minor"}, "- Shared change"
    )
    releases = release(services, only=["web"])
    assert list(releases) == ["web"]
    assert parse_fragment(fragment).bumps == {"api": "patch"}

    release(services, only=["api"])
    assert not fragment.exists()


def test_only_needs_pending_changes_for_a_stable_package(services: Path):
    with raises(ChangeError):
        plan_releases([], find_packages(services), services, only=["web"])
