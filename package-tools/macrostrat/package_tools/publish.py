#!/usr/bin/env python
"""Building and publishing packages to PyPI.

Publishing is deliberately *pure*: it builds, uploads and tags. It does not
regenerate lock files or commit to the repository, so it behaves the same way
on a laptop and in CI, and a re-run after a partial failure does the same thing
as the first run.

In GitHub Actions with `id-token: write`, ``uv publish`` authenticates through
PyPI trusted publishing and needs no token. Locally, set ``UV_PUBLISH_TOKEN``.
"""

from os import environ
from pathlib import Path

import typer
from rich import print

from macrostrat.utils import cmd
from macrostrat.utils.shell import git_has_changes

from .packages import Package, find_packages, is_published


def build_module(pkg: Package):
    cmd("uv build --clear", cwd=pkg.path)


def publish_module(pkg: Package) -> bool:
    cmd_env = {**environ}
    if "UV_PUBLISH_TOKEN" in environ or "UV_PUBLISH_PASSWORD" in environ:
        cmd_env.setdefault("UV_PUBLISH_USERNAME", "__token__")
    res = cmd("uv publish", cwd=pkg.path, env=cmd_env)
    if res.returncode != 0:
        print(f"[red]Failed to publish {pkg.version_string}")
        return False
    cmd(f"git tag -a {pkg.tag} -m '{pkg.name} version {pkg.version}'", shell=True)
    return True


def modules_to_publish(packages: list[Package], omit: list[str] = []) -> list[Package]:
    selected = []
    for pkg in packages:
        if pkg.name in omit:
            continue
        if pkg.is_private:
            print(f"[dim]{pkg.version_string} is private and will not be published.")
            continue
        if is_published(pkg):
            print(f"[dim]{pkg.version_string} already exists on PyPI.")
            continue
        print(f"[cyan]{pkg.name}[/] ([bold]{pkg.version}[/]) will be published")
        selected.append(pkg)
    return selected


def build_packages(path: Path = Path.cwd(), omit: list[str] = []):
    """Build every package that is not yet published."""
    for pkg in modules_to_publish(find_packages(path), omit):
        build_module(pkg)


def publish_packages(
    path: Path = Path.cwd(),
    omit: list[str] = [],
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be published, and stop."
    ),
    allow_dirty: bool = typer.Option(
        False, "--allow-dirty", help="Publish even with uncommitted changes."
    ),
):
    """Publish all packages whose version is not yet on PyPI."""
    packages = modules_to_publish(find_packages(path), omit)

    if not packages:
        print("[green]All packages are already published.")
        return

    if dry_run:
        print("\n[dim]Dry run; nothing was published.")
        return

    if git_has_changes() and not allow_dirty:
        print(
            "[red]There are uncommitted changes in this repository. "
            "Commit or stash them, or pass --allow-dirty."
        )
        raise SystemExit(1)

    failed = []
    for pkg in packages:
        build_module(pkg)
        if not publish_module(pkg):
            failed.append(pkg)

    if failed:
        names = ", ".join(pkg.version_string for pkg in failed)
        print(f"[bold red]Failed to publish: {names}")
        raise SystemExit(1)
