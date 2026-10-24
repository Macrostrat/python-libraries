"""Change fragments: record a version change in the pull request that makes it.

A fragment is a markdown file in ``.changes/`` with a small frontmatter block
naming the packages it affects and how far each should move:

```markdown
---
macrostrat.database: minor
---

Add `copy_settings` to `template_database`.
```

``mono version`` consumes every fragment, raises the affected packages'
versions, cascades to their dependents within the monorepo, writes each
package's ``CHANGELOG.md``, and removes the fragments it applied. With
``--pre`` it makes prereleases (``1.3.0-beta.1``) instead.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from random import choice
from typing import Optional

from packaging.version import Version

from .packages import (
    Package,
    compare_url,
    find_packages,
    load_pkg_config,
    package_setting,
)

CHANGES_DIR = ".changes"
LEVELS = ["patch", "minor", "major"]
CHANGELOG_MODES = ["versions", "unreleased"]

ADJECTIVES = [
    "brave",
    "calm",
    "clever",
    "eager",
    "fuzzy",
    "gentle",
    "jolly",
    "lucky",
    "mellow",
    "nimble",
    "plucky",
    "quiet",
    "rapid",
    "sturdy",
    "wise",
]
NOUNS = [
    "badger",
    "basalt",
    "canyon",
    "cobble",
    "dolomite",
    "ferns",
    "geode",
    "granite",
    "lichen",
    "moraine",
    "otter",
    "pebble",
    "shale",
    "trilobite",
]


@dataclass
class Fragment:
    path: Path
    bumps: dict[str, str]
    description: str

    @property
    def bullets(self) -> list[str]:
        """The description, as one or more changelog bullets."""
        text = self.description.strip()
        if not text:
            return []
        if text.startswith("-") or text.startswith("*"):
            return [line.rstrip() for line in text.splitlines() if line.strip()]
        # Fold a prose fragment into a single bullet.
        collapsed = " ".join(line.strip() for line in text.splitlines() if line.strip())
        return [f"- {collapsed}"]


class ChangeError(Exception):
    pass


def changes_dir(root: Path) -> Path:
    return root / CHANGES_DIR


def parse_fragment(path: Path) -> Fragment:
    text = path.read_text()
    match = re.match(r"^---\n(?P<frontmatter>.*?)\n---\n(?P<body>.*)$", text, re.DOTALL)
    if match is None:
        raise ChangeError(f"{path.name} has no frontmatter block")

    bumps = {}
    for line in match.group("frontmatter").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ChangeError(
                f"{path.name}: can't read '{line}' as '<package>: <level>'"
            )
        name, level = line.split(":", 1)
        name = name.strip().strip("\"'")
        level = level.strip().strip("\"'")
        if level not in LEVELS:
            raise ChangeError(
                f"{path.name}: '{level}' is not one of {', '.join(LEVELS)}"
            )
        bumps[name] = level

    return Fragment(path=path, bumps=bumps, description=match.group("body"))


def read_fragments(root: Path = Path.cwd()) -> list[Fragment]:
    directory = changes_dir(root)
    if not directory.is_dir():
        return []
    files = sorted(f for f in directory.glob("*.md") if f.name != "README.md")
    return [parse_fragment(f) for f in files]


def fragment_name() -> str:
    return f"{choice(ADJECTIVES)}-{choice(NOUNS)}"


def fragment_text(bumps: dict[str, str], description: str) -> str:
    frontmatter = "\n".join(f"{name}: {level}" for name, level in bumps.items())
    return f"---\n{frontmatter}\n---\n\n{description.strip()}\n"


def write_fragment(root: Path, bumps: dict[str, str], description: str) -> Path:
    directory = changes_dir(root)
    directory.mkdir(exist_ok=True)
    path = directory / f"{fragment_name()}.md"
    while path.exists():
        path = directory / f"{fragment_name()}.md"

    path.write_text(fragment_text(bumps, description))
    return path


def consume_fragments(fragments: list[Fragment], released: set[str]):
    """Remove applied entries; a fragment naming an unreleased package keeps that entry."""
    for fragment in fragments:
        remaining = {k: v for k, v in fragment.bumps.items() if k not in released}
        if not remaining:
            fragment.path.unlink()
        elif remaining != fragment.bumps:
            fragment.path.write_text(fragment_text(remaining, fragment.description))


def max_level(a: Optional[str], b: str) -> str:
    if a is None:
        return b
    return LEVELS[max(LEVELS.index(a), LEVELS.index(b))]


def stable_target(current: str, level: str) -> str:
    """The stable version a change of `level` leads to."""
    version = Version(current)
    major, minor, patch = (list(version.release) + [0, 0, 0])[:3]
    if version.pre is None:
        if level == "major":
            return f"{major + 1}.0.0"
        if level == "minor":
            return f"{major}.{minor + 1}.0"
        return f"{major}.{minor}.{patch + 1}"

    # A prerelease already carries its bump, as in npm's semver: 1.3.0-beta.2
    # absorbs a minor change, but a major one moves it to 2.0.0.
    base = f"{major}.{minor}.{patch}"
    if level == "major" and (minor, patch) != (0, 0):
        return f"{major + 1}.0.0"
    if level == "minor" and patch != 0:
        return f"{major}.{minor + 1}.0"
    return base


def next_version(current: str, level: str, prerelease: Optional[str] = None) -> str:
    """The next version, written in semver form (`1.3.0-beta.1`) for prereleases."""
    target = stable_target(current, level)
    if prerelease is None:
        return target

    version = Version(current)
    same_label = (
        version.pre is not None
        and Version(f"0.0.0-{prerelease}.1").pre[0] == version.pre[0]
    )
    if same_label and target == version.base_version:
        return f"{target}-{prerelease}.{version.pre[1] + 1}"
    return f"{target}-{prerelease}.1"


def is_prerelease(version: str) -> bool:
    return Version(version).is_prerelease


# --- applying a set of fragments -------------------------------------------


@dataclass
class Release:
    package: Package
    level: str
    from_version: str
    to_version: str
    bullets: list[str] = field(default_factory=list)
    cascaded: bool = False


def internal_dependency_level(root: Path) -> Optional[str]:
    """How far to move a package when a sibling it depends on is released."""
    cfg = load_pkg_config(root)
    setting = (
        cfg.get("tool", {}).get("mono", {}).get("update-internal-dependencies", "patch")
    )
    if setting == "none":
        return None
    if setting not in LEVELS:
        raise ChangeError(
            f"tool.mono.update-internal-dependencies must be one of "
            f"{', '.join(LEVELS)} or 'none' (got '{setting}')"
        )
    return setting


def dependency_specifier(pkg: Package, dependency: str) -> Optional[str]:
    """The raw requirement string by which `pkg` depends on `dependency`."""
    for requirement in pkg.dependencies:
        name = re.split(r"[<>=!~\[ ]", requirement, maxsplit=1)[0].strip()
        if name == dependency:
            return requirement
    return None


def plan_releases(
    fragments: list[Fragment],
    packages: list[Package],
    root: Path = Path.cwd(),
    *,
    prerelease: Optional[str] = None,
    only: Optional[list[str]] = None,
) -> list[Release]:
    """Work out which packages move, how far, and why.

    `only` limits the release to the named packages. Naming a package at a
    prerelease with no pending changes releases its stable version.
    """
    by_name = {pkg.name: pkg for pkg in packages}
    for name in only or []:
        if name not in by_name:
            raise ChangeError(f"'{name}' is not a package in this repository")

    levels: dict[str, str] = {}
    bullets: dict[str, list[str]] = {}
    for fragment in fragments:
        for name, level in fragment.bumps.items():
            if name not in by_name:
                raise ChangeError(
                    f"{fragment.path.name} names unknown package '{name}'"
                )
            if only is not None and name not in only:
                continue
            levels[name] = max_level(levels.get(name), level)
            bullets.setdefault(name, []).extend(fragment.bullets)

    if prerelease is None:
        for name in only or []:
            if name not in levels and is_prerelease(by_name[name].version):
                levels[name] = "patch"

    for name in only or []:
        if name not in levels:
            raise ChangeError(f"{name} has no pending changes to release")

    directly_changed = set(levels)

    cascade_level = internal_dependency_level(root)
    if cascade_level is not None:
        # Repeat until nothing new moves, so utils -> database -> dinosaur works.
        changed = True
        while changed:
            changed = False
            for pkg in packages:
                for name in list(levels):
                    if name == pkg.name:
                        continue
                    if dependency_specifier(pkg, name) is None:
                        continue
                    if pkg.name in levels:
                        continue
                    levels[pkg.name] = cascade_level
                    changed = True

    releases = []
    for pkg in packages:
        level = levels.get(pkg.name)
        if level is None:
            continue
        releases.append(
            Release(
                package=pkg,
                level=level,
                from_version=pkg.version,
                to_version=next_version(pkg.version, level, prerelease),
                bullets=bullets.get(pkg.name, []),
                cascaded=pkg.name not in directly_changed,
            )
        )
    return releases


def set_version(pkg: Package, version: str):
    if pkg.is_python:
        pattern, replacement = r'^version\s*=\s*"[^"]+"', f'version = "{version}"'
    else:
        pattern, replacement = r'^(\s*"version"\s*:\s*)"[^"]+"', rf'\g<1>"{version}"'
    new_text, count = re.subn(
        pattern, replacement, pkg.read_text(), count=1, flags=re.MULTILINE
    )
    if count != 1:
        raise ChangeError(f"Could not find a version field in {pkg.version_file}")
    pkg.write_text(new_text)


def raise_dependency_floor(pkg: Package, dependency: str, version: str) -> bool:
    """Raise the `>=` floor of an intra-repo dependency, leaving its ceiling alone."""
    requirement = dependency_specifier(pkg, dependency)
    if requirement is None:
        return False
    updated = re.sub(r">=\s*[^,\]\"']+", f">={version}", requirement, count=1)
    if updated == requirement:
        return False
    pkg.write_text(pkg.read_text().replace(f'"{requirement}"', f'"{updated}"'))
    return True


UNRELEASED_HEADING = re.compile(
    r"^## \[?Unreleased\]?[^\n]*\n?", re.MULTILINE | re.IGNORECASE
)
NEXT_HEADING = re.compile(r"^## ", re.MULTILINE)


def changelog_mode(root: Path, pkg: Package) -> str:
    """`versions`: a heading per release. `unreleased`: prereleases collect under
    `## [Unreleased]`, which a stable release turns into its heading."""
    mode = package_setting(root, pkg.name, "changelog", "versions")
    if mode not in CHANGELOG_MODES:
        raise ChangeError(
            f"tool.mono changelog for {pkg.name} must be one of "
            f"{', '.join(CHANGELOG_MODES)} (got '{mode}')"
        )
    return mode


def changelog_header(release: Release, root: Path) -> str:
    header = f"## [{release.to_version}] - {date.today().isoformat()}"
    from_tag = f"{release.package.name}-v{release.from_version}"
    to_tag = f"{release.package.name}-v{release.to_version}"
    url = compare_url(root, from_tag, to_tag)
    if url is None:
        return header
    return f"{header} [_changes_]({url})"


def insert_below_title(existing: str, entry: str) -> str:
    lines = existing.splitlines()
    if lines and lines[0].startswith("# "):
        head = "\n".join(lines[:1])
        tail = "\n".join(lines[1:]).lstrip("\n")
        return f"{head}\n\n{entry}\n{tail}".rstrip() + "\n"
    return f"# Changelog\n\n{entry}\n{existing.lstrip()}".rstrip() + "\n"


def write_changelog(
    release: Release, extra_bullets: list[str], root: Path = Path.cwd()
):
    path = release.package.changelog_file
    existing = path.read_text() if path.exists() else "# Changelog\n"
    bullets = release.bullets + extra_bullets

    if changelog_mode(root, release.package) == "unreleased":
        new_text = write_unreleased(existing, release, bullets, root)
    else:
        if not bullets:
            bullets = ["- Maintenance release."]
        entry = changelog_header(release, root) + "\n\n" + "\n".join(bullets) + "\n"
        new_text = insert_below_title(existing, entry)

    path.write_text(new_text)


def write_unreleased(
    existing: str, release: Release, bullets: list[str], root: Path
) -> str:
    match = UNRELEASED_HEADING.search(existing)
    if match is None:
        existing = insert_below_title(existing, "## [Unreleased]\n")
        match = UNRELEASED_HEADING.search(existing)

    following = NEXT_HEADING.search(existing, match.end())
    end = following.start() if following else len(existing)
    entries = existing[match.end() : end].strip("\n")
    if bullets:
        entries = "\n".join(filter(None, [entries, *bullets]))

    section = "## [Unreleased]\n\n"
    if not is_prerelease(release.to_version):
        section += changelog_header(release, root) + "\n\n"
        if not entries:
            entries = "- Maintenance release."
    if entries:
        section += entries + "\n\n"

    return (existing[: match.start()] + section + existing[end:]).rstrip() + "\n"


def apply_releases(releases: list[Release], root: Path = Path.cwd()):
    versions = {r.package.name: r.to_version for r in releases}
    all_packages = find_packages(root)

    for release in releases:
        set_version(release.package, release.to_version)

    # Raise intra-repo dependency floors to the versions being released.
    extra_bullets: dict[str, list[str]] = {}
    for pkg in all_packages:
        for name, version in versions.items():
            if name == pkg.name:
                continue
            if raise_dependency_floor(pkg, name, version):
                extra_bullets.setdefault(pkg.name, []).append(
                    f"- Require `{name}` {version} or newer."
                )

    for release in releases:
        write_changelog(release, extra_bullets.get(release.package.name, []), root)
