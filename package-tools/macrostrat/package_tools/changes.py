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
package's ``CHANGELOG.md``, and removes the fragments it applied.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from random import choice
from typing import Optional

from packaging.version import Version

from .packages import Package, compare_url, find_packages, load_pkg_config

CHANGES_DIR = ".changes"
LEVELS = ["patch", "minor", "major"]

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


def write_fragment(root: Path, bumps: dict[str, str], description: str) -> Path:
    directory = changes_dir(root)
    directory.mkdir(exist_ok=True)
    path = directory / f"{fragment_name()}.md"
    while path.exists():
        path = directory / f"{fragment_name()}.md"

    frontmatter = "\n".join(f"{name}: {level}" for name, level in bumps.items())
    path.write_text(f"---\n{frontmatter}\n---\n\n{description.strip()}\n")
    return path


def max_level(a: Optional[str], b: str) -> str:
    if a is None:
        return b
    return LEVELS[max(LEVELS.index(a), LEVELS.index(b))]


def next_version(current: str, level: str) -> str:
    version = Version(current)
    major, minor, patch = (list(version.release) + [0, 0, 0])[:3]
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


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
    fragments: list[Fragment], packages: list[Package], root: Path = Path.cwd()
) -> list[Release]:
    """Work out which packages move, how far, and why."""
    by_name = {pkg.name: pkg for pkg in packages}

    levels: dict[str, str] = {}
    bullets: dict[str, list[str]] = {}
    for fragment in fragments:
        for name, level in fragment.bumps.items():
            if name not in by_name:
                raise ChangeError(
                    f"{fragment.path.name} names unknown package '{name}'"
                )
            levels[name] = max_level(levels.get(name), level)
            bullets.setdefault(name, []).extend(fragment.bullets)

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
                to_version=next_version(pkg.version, level),
                bullets=bullets.get(pkg.name, []),
                cascaded=pkg.name not in directly_changed,
            )
        )
    return releases


def set_version(pkg: Package, version: str):
    text = pkg.read_text()
    new_text, count = re.subn(
        r'^version\s*=\s*"[^"]+"',
        f'version = "{version}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise ChangeError(f"Could not find a version field in {pkg.pyproject_file}")
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


def changelog_header(release: Release) -> str:
    header = f"## [{release.to_version}] - {date.today().isoformat()}"
    from_tag = f"{release.package.name}-v{release.from_version}"
    to_tag = f"{release.package.name}-v{release.to_version}"
    return f"{header} [_changes_]({compare_url(from_tag, to_tag)})"


def write_changelog(release: Release, extra_bullets: list[str]):
    path = release.package.changelog_file
    existing = path.read_text() if path.exists() else "# Changelog\n"

    bullets = release.bullets + extra_bullets
    if not bullets:
        bullets = ["- Maintenance release."]

    entry = changelog_header(release) + "\n\n" + "\n".join(bullets) + "\n"

    lines = existing.splitlines()
    # Insert below the top-level title, if there is one.
    if lines and lines[0].startswith("# "):
        head = "\n".join(lines[:1])
        tail = "\n".join(lines[1:]).lstrip("\n")
        new_text = f"{head}\n\n{entry}\n{tail}".rstrip() + "\n"
    else:
        new_text = f"# Changelog\n\n{entry}\n{existing.lstrip()}".rstrip() + "\n"

    path.write_text(new_text)


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
        write_changelog(release, extra_bullets.get(release.package.name, []))
