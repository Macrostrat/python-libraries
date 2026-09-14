"""Backwards-compatible re-exports; these now live in `packages`."""

from .packages import get_local_dependencies, load_pkg_config

__all__ = ["get_local_dependencies", "load_pkg_config"]
