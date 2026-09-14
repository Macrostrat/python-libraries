from pathlib import Path
from textwrap import dedent

from pytest import fixture, raises

from macrostrat.package_tools.changes import (
    ChangeError,
    apply_releases,
    max_level,
    next_version,
    parse_fragment,
    plan_releases,
    raise_dependency_floor,
    read_fragments,
    set_version,
    write_fragment,
)
from macrostrat.package_tools.packages import find_packages

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
    (tmp_path / "pyproject.toml").write_text(ROOT_CONFIG.replace("{{", "{").replace("}}", "}"))
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
    path.write_text(
        dedent(
            """\
            ---
            example.database: minor
            ---

            - Add a thing.
            """
        )
    )
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
        r.package.name: r for r in plan_releases(read_fragments(repo), find_packages(repo), repo)
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
    assert original.replace('version = "1.2.3"', 'version = "9.9.9"') == package.read_text()


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
