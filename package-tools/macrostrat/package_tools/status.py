"""``mono status``: the release plan for the monorepo, and the checks that guard it.

This is meant to run on every pull request. It answers the question that was
previously only answerable at publish time: *which packages will be published
when this merges, and is everything they need in place?*
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from rich import print
from rich.table import Table

from macrostrat.utils import cmd

from .changes import ChangeError, plan_releases, read_fragments
from .packages import (
    Package,
    find_packages,
    latest_published_version,
    published_versions,
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


def check_package(pkg: Package, report: Report, *, check_locks: bool) -> str:
    """Check one package, and return what publishing will do with it."""
    if pkg.is_private:
        if pkg.classifiers is None:
            report.warn(
                f"{pkg.name} has no classifiers, so it will never be published. "
                "Add the 'Private :: Do Not Upload' classifier to say so on purpose."
            )
        return "private"

    published = published_versions(pkg.name)
    if pkg.version in published:
        return "up to date"

    if not pkg.changelog_file.exists():
        report.error(f"{pkg.name} has no CHANGELOG.md")
    elif not changelog_has_version(pkg, pkg.version):
        report.error(
            f"{pkg.name} moved to {pkg.version} with no CHANGELOG entry for it"
        )

    if check_locks and not lockfile_is_current(pkg.path):
        report.error(
            f"{pkg.name} has a stale uv.lock — run `uv lock` in {pkg.path.name}/"
        )

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
    table.add_column("PyPI")
    table.add_column("On merge")
    table.add_column("Pending changes")

    for pkg in packages:
        action = check_package(pkg, report, check_locks=check_locks)
        latest = latest_published_version(pkg.name) or "—"
        release = pending.get(pkg.name)
        if release is None:
            planned = "—"
        else:
            note = " (dependency)" if release.cascaded else ""
            planned = f"{release.level} → {release.to_version}{note}"

        style = {
            "will publish": "[bold green]will publish[/]",
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
