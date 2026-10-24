"""CLI commands for recording and applying version changes."""

from pathlib import Path

import typer
from rich import print
from rich.prompt import Prompt

from macrostrat.utils import cmd

from .changes import (
    LEVELS,
    ChangeError,
    apply_releases,
    consume_fragments,
    is_prerelease,
    plan_releases,
    read_fragments,
    write_fragment,
)
from .packages import find_packages, mono_config


def create_changeset(
    package: list[str] = typer.Option(
        [],
        "--package",
        "-p",
        help="Package to change, as '<name>' or '<name>:<level>'. Repeatable.",
    ),
    level: str = typer.Option(
        None, "--level", "-l", help=f"Change level for packages given without one."
    ),
    message: str = typer.Option(None, "--message", "-m", help="Changelog entry."),
    path: Path = Path.cwd(),
):
    """Record a pending version change, to be applied by `mono version`."""
    packages = find_packages(path)
    names = [pkg.name for pkg in packages]

    if not package:
        print("[bold]Packages in this repository:[/]")
        for pkg in packages:
            print(f"  {pkg.name} ({pkg.version})")
        selected = Prompt.ask("\nPackages to change (comma-separated)")
        package = [p.strip() for p in selected.split(",") if p.strip()]

    bumps: dict[str, str] = {}
    for entry in package:
        name, _, entry_level = entry.partition(":")
        name = name.strip()
        if name not in names:
            raise typer.BadParameter(f"'{name}' is not a package in this repository")
        chosen = entry_level.strip() or level
        if not chosen:
            chosen = Prompt.ask(
                f"Change level for [cyan]{name}[/]", choices=LEVELS, default="patch"
            )
        if chosen not in LEVELS:
            raise typer.BadParameter(f"'{chosen}' is not one of {', '.join(LEVELS)}")
        bumps[name] = chosen

    if not bumps:
        print("[red]No packages selected.")
        raise SystemExit(1)

    if message is None:
        message = Prompt.ask("Changelog entry")

    fragment = write_fragment(path, bumps, message)
    print(f"\nWrote [cyan]{fragment.relative_to(path)}[/]. Commit it with your change.")


def apply_versions(
    path: Path = Path.cwd(),
    package: list[str] = typer.Option(
        [],
        "--package",
        "-p",
        help="Release only this package. Repeatable. Releases a prerelease as stable.",
    ),
    pre: bool = typer.Option(
        False, "--pre", help="Make prereleases, labelled by tool.mono.prerelease-label."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change, without writing anything."
    ),
    lock: bool = typer.Option(True, help="Refresh lock files for changed packages."),
):
    """Apply pending change fragments: versions, dependencies, and changelogs."""
    prerelease = None
    if pre:
        prerelease = mono_config(path).get("prerelease-label", "beta")

    try:
        fragments = read_fragments(path)
        packages = find_packages(path)
        releases = plan_releases(
            fragments, packages, path, prerelease=prerelease, only=package or None
        )
    except ChangeError as err:
        print(f"[bold red]error:[/] {err}")
        raise SystemExit(1)

    if not releases:
        print("[green]No pending changes.")
        for pkg in packages:
            if is_prerelease(pkg.version):
                print(
                    f"[dim]{pkg.name} is at {pkg.version}; "
                    f"`mono version -p {pkg.name}` releases it as stable."
                )
        return

    for release in releases:
        reason = " [dim](dependency)[/]" if release.cascaded else ""
        level = release.level
        if is_prerelease(release.from_version) and not release.bullets:
            level = "stable"
        print(
            f"[cyan]{release.package.name}[/] "
            f"{release.from_version} → [bold]{release.to_version}[/] "
            f"({level}){reason}"
        )

    if dry_run:
        print("\n[dim]Dry run; nothing was written.")
        return

    apply_releases(releases, path)
    consume_fragments(fragments, {r.package.name for r in releases})

    if lock:
        for release in releases:
            if release.package.is_python:
                cmd("uv lock", cwd=release.package.path)
        cmd("uv lock", cwd=path)

    print("\nApplied. Review the diff, then commit the version change.")
