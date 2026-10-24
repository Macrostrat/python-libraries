"""Discovery and metadata for the packages in this monorepo.

Packages are found through the root ``pyproject.toml``'s ``[tool.uv.sources]``
table, which is the same list that ``mono install`` operates on. A repository
whose releasable units are not uv sources (deployed services, say) lists them
instead as directory globs in ``[tool.mono] packages``; each is then named by
its directory, and may keep its version in ``package.json``.
"""

import json
import subprocess
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Optional

import requests
from packaging.version import InvalidVersion, Version
from toml import load

PRIVATE_CLASSIFIER = "Private :: Do Not Upload"


def load_pkg_config(fp: Path) -> dict:
    if fp.is_dir():
        fp = fp / "pyproject.toml"
    with fp.open("r") as f:
        return load(f)


def get_local_dependencies(pkg_cfg: dict) -> dict:
    """Get UV source packages that are local to the project."""
    return pkg_cfg["tool"]["uv"]["sources"]


def mono_config(root: Path) -> dict:
    return load_pkg_config(root).get("tool", {}).get("mono", {})


def package_setting(root: Path, name: str, key: str, default=None):
    """A ``[tool.mono]`` setting, as overridden for one package."""
    cfg = mono_config(root)
    override = cfg.get("overrides", {}).get(name, {})
    return override.get(key, cfg.get(key, default))


def released_by(root: Path) -> str:
    """How a version is released: published to PyPI, or tagged in git by CI."""
    setting = mono_config(root).get("released-by", "pypi")
    if setting not in ("pypi", "tag"):
        raise ValueError(
            f"tool.mono.released-by must be 'pypi' or 'tag' (got '{setting}')"
        )
    return setting


@dataclass
class Package:
    name: str
    path: Path

    @cached_property
    def config(self) -> dict:
        if self.is_python:
            return load_pkg_config(self.path)
        return json.loads(self.version_file.read_text())

    @property
    def version_file(self) -> Path:
        pyproject = self.path / "pyproject.toml"
        if pyproject.exists():
            return pyproject
        return self.path / "package.json"

    @property
    def is_python(self) -> bool:
        return self.version_file.name == "pyproject.toml"

    @property
    def project(self) -> dict:
        if self.is_python:
            return self.config["project"]
        return self.config

    @property
    def changelog_file(self) -> Path:
        return self.path / "CHANGELOG.md"

    @property
    def version(self) -> str:
        return self.project["version"]

    @property
    def classifiers(self) -> Optional[list[str]]:
        return self.project.get("classifiers")

    @property
    def is_private(self) -> bool:
        """A package is private if it opts out, or has no classifiers at all."""
        if self.classifiers is None:
            return True
        return PRIVATE_CLASSIFIER in self.classifiers

    @property
    def dependencies(self) -> list[str]:
        # Only Python requirements take part in the dependency cascade.
        if not self.is_python:
            return []
        return self.project.get("dependencies", [])

    @property
    def version_string(self) -> str:
        return f"{self.name} ({self.version})"

    @property
    def tag(self) -> str:
        return f"{self.name}-v{self.version}"

    def read_text(self) -> str:
        return self.version_file.read_text()

    def write_text(self, text: str):
        self.version_file.write_text(text)
        # The config on disk has changed; drop the memoized copy.
        self.__dict__.pop("config", None)


def find_packages(root: Path = Path.cwd()) -> list[Package]:
    """All monorepo packages, in the order they appear in the root config."""
    patterns = mono_config(root).get("packages")
    if patterns is not None:
        return find_package_directories(root, patterns)

    sources = get_local_dependencies(load_pkg_config(root))
    packages = []
    for name, source in sources.items():
        path = source.get("path")
        if path is None:
            continue
        packages.append(Package(name=name, path=(root / path).resolve()))
    return packages


def find_package_directories(root: Path, patterns: list[str]) -> list[Package]:
    packages = []
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if (
                not (path / "pyproject.toml").exists()
                and not (path / "package.json").exists()
            ):
                continue
            packages.append(Package(name=path.name, path=path.resolve()))
    return packages


def find_package(name: str, root: Path = Path.cwd()) -> Optional[Package]:
    for pkg in find_packages(root):
        if pkg.name == name:
            return pkg
    return None


def published_versions(name: str) -> set[str]:
    """Versions of a package that already exist on PyPI."""
    uri = f"https://pypi.org/pypi/{name}/json"
    response = requests.get(uri, timeout=30)
    if response.status_code == 404:
        return set()
    response.raise_for_status()
    return set(response.json().get("releases", {}).keys())


def tagged_versions(pkg: Package, root: Path) -> set[str]:
    """Versions of a package that have a release tag on the remote."""
    # Ask the remote, so an unfetched checkout answers the way CI will
    prefix = f"refs/tags/{pkg.name}-v"
    res = subprocess.run(
        ["git", "ls-remote", "--tags", "origin", f"{prefix}*"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    versions = set()
    for line in res.stdout.splitlines():
        ref = line.split()[-1].removesuffix("^{}")
        versions.add(ref.removeprefix(prefix))
    return versions


def released_versions(pkg: Package, root: Path) -> set[str]:
    if released_by(root) == "tag":
        return tagged_versions(pkg, root)
    return published_versions(pkg.name)


def latest_version(versions: set[str]) -> Optional[str]:
    parsed = []
    for text in versions:
        try:
            parsed.append((Version(text), text))
        except InvalidVersion:
            continue
    if not parsed:
        return None
    return max(parsed)[1]


def is_published(pkg: Package) -> bool:
    return pkg.version in published_versions(pkg.name)


def compare_url(root: Path, from_tag: str, to_tag: str) -> Optional[str]:
    repository = mono_config(root).get("repository")
    if repository is None:
        return None
    return f"{repository.rstrip('/')}/compare/{from_tag}...{to_tag}"
