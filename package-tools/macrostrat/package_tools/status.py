"""``mono status``: the release plan for the monorepo, and the checks that guard it.

This is meant to run on every pull request. It answers the question that was
previously only answerable at publish time: *which packages will be published
when this merges, and is everything they need in place?* Where CI releases by
tagging (``released-by = "tag"``), "published" means a release tag exists.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from rich import print
from rich.table import Table

from macrostrat.utils import cmd

from .changes import (
    ChangeError,
    changelog_mode,
    is_prerelease,
    plan_releases,
    read_fragments,
)
from .packages import (
    Package,
    find_packages,
    latest_version,
    released_by,
    released_versions,
)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, message: str):
        self.errors.append(message)

    def warn(self, message: str):
        self.warnings.append(message)


def changelog_has_version(pkg: Package, version: str) -> bool:
    if not pkg.changelog_file.exists():
        return False
    pattern = re.compile(r"^##\s*\[?" + re.escape(version) + r"\]?", re.MULTILINE)
    return pattern.search(pkg.changelog_file.read_text()) is not None


def lockfile_is_current(pkg_dir: Path) -> bool:
    res = cmd("uv lock --check", cwd=pkg_dir, capture_output=True)
    return res.returncode == 0


def needs_changelog_heading(pkg: Package, root: Path) -> bool:
    # Prereleases collect under [Unreleased] until the stable release
    if changelog_mode(root, pkg) == "unreleased":
        return not is_prerelease(pkg.version)
    return True


def check_package(
    pkg: Package, released: set[str], report: Report, root: Path, *, check_locks: bool
) -> str:
    """Check one package, and return what publishing will do with it."""
    tagged = released_by(root) == "tag"
    if pkg.is_private and not tagged:
        if pkg.classifiers is None:
            report.warn(
                f"{pkg.name} has no classifiers, so it will never be published. "
                "Add the 'Private :: Do Not Upload' classifier to say so on purpose."
            )
        return "private"

    if pkg.version in released:
        return "up to date"

    if not pkg.changelog_file.exists():
        report.error(f"{pkg.name} has no CHANGELOG.md")
    elif needs_changelog_heading(pkg, root) and not changelog_has_version(
        pkg, pkg.version
    ):
        report.error(
            f"{pkg.name} moved to {pkg.version} with no CHANGELOG entry for it"
        )

    if check_locks and pkg.is_python and not lockfile_is_current(pkg.path):
        report.error(
            f"{pkg.name} has a stale uv.lock — run `uv lock` in {pkg.path.name}/"
        )

    if tagged:
        return "will tag"
    return "will publish"


def show_status(
    path: Path = Path.cwd(),
    check: bool = False,
    check_locks: bool = True,
):
    """Show which packages will be published, and check that they are ready."""
    report = Report()
    packages = find_packages(path)

    try:
        fragments = read_fragments(path)
        releases = plan_releases(fragments, packages, path)
    except ChangeError as err:
        report.error(str(err))
        fragments, releases = [], []

    pending = {r.package.name: r for r in releases}

    table = Table(title="Release status", title_justify="left")
    table.add_column("Package")
    table.add_column("Local")
    table.add_column("Tagged" if released_by(path) == "tag" else "PyPI")
    table.add_column("On merge")
    table.add_column("Pending changes")

    for pkg in packages:
        released = released_versions(pkg, path)
        action = check_package(pkg, released, report, path, check_locks=check_locks)
        latest = latest_version(released) or "—"
        release = pending.get(pkg.name)
        if release is None:
            planned = "—"
        else:
            note = " (dependency)" if release.cascaded else ""
            planned = f"{release.level} → {release.to_version}{note}"

        style = {
            "will publish": "[bold green]will publish[/]",
            "will tag": "[bold green]will tag[/]",
            "up to date": "[dim]up to date[/]",
            "private": "[dim]private[/]",
        }[action]

        table.add_row(pkg.name, pkg.version, latest, style, planned)

    print(table)

    if fragments:
        print(f"\n[bold]{len(fragments)} pending change fragment(s)[/] in .changes/")
        print("Run [cyan]mono version[/] to apply them.")

    for message in report.warnings:
        print(f"[yellow]warning:[/] {message}")
    for message in report.errors:
        print(f"[bold red]error:[/] {message}")

    if check and report.errors:
        raise SystemExit(1)
