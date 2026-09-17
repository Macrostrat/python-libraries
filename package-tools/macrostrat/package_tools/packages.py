"""Discovery and metadata for the packages in this monorepo.

Packages are found through the root ``pyproject.toml``'s ``[tool.uv.sources]``
table, which is the same list that ``mono install`` operates on.
"""

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Optional

import requests
from packaging.version import InvalidVersion, Version
from toml import load

PRIVATE_CLASSIFIER = "Private :: Do Not Upload"
REPO_BASE_URL = "https://github.com/Macrostrat/python-libraries"


def load_pkg_config(fp: Path) -> dict:
    if fp.is_dir():
        fp = fp / "pyproject.toml"
    with fp.open("r") as f:
        return load(f)


def get_local_dependencies(pkg_cfg: dict) -> dict:
    """Get UV source packages that are local to the project."""
    return pkg_cfg["tool"]["uv"]["sources"]


@dataclass
class Package:
    name: str
    path: Path

    @cached_property
    def config(self) -> dict:
        return load_pkg_config(self.path)

    @property
    def pyproject_file(self) -> Path:
        return self.path / "pyproject.toml"

    @property
    def changelog_file(self) -> Path:
        return self.path / "CHANGELOG.md"

    @property
    def version(self) -> str:
        return self.config["project"]["version"]

    @property
    def classifiers(self) -> Optional[list[str]]:
        return self.config["project"].get("classifiers")

    @property
    def is_private(self) -> bool:
        """A package is private if it opts out, or has no classifiers at all."""
        if self.classifiers is None:
            return True
        return PRIVATE_CLASSIFIER in self.classifiers

    @property
    def dependencies(self) -> list[str]:
        return self.config["project"].get("dependencies", [])

    @property
    def version_string(self) -> str:
        return f"{self.name} ({self.version})"

    @property
    def tag(self) -> str:
        return f"{self.name}-v{self.version}"

    def read_text(self) -> str:
        return self.pyproject_file.read_text()

    def write_text(self, text: str):
        self.pyproject_file.write_text(text)
        # The config on disk has changed; drop the memoized copy.
        self.__dict__.pop("config", None)


def find_packages(root: Path = Path.cwd()) -> list[Package]:
    """All monorepo packages, in the order they appear in the root config."""
    sources = get_local_dependencies(load_pkg_config(root))
    packages = []
    for name, source in sources.items():
        path = source.get("path")
        if path is None:
            continue
        packages.append(Package(name=name, path=(root / path).resolve()))
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


def latest_published_version(name: str) -> Optional[str]:
    versions = []
    for text in published_versions(name):
        try:
            versions.append(Version(text))
        except InvalidVersion:
            continue
    if not versions:
        return None
    return str(max(versions))


def is_published(pkg: Package) -> bool:
    return pkg.version in published_versions(pkg.name)


def compare_url(from_tag: str, to_tag: str) -> str:
    return f"{REPO_BASE_URL}/compare/{from_tag}...{to_tag}"
