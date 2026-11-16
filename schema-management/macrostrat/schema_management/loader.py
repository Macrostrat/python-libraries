"""Load migration classes from a directory of modules."""

import sys
from importlib import util
from pathlib import Path

from .migrations import Migration, concrete_migrations


def load_migrations(migrations_dir: Path, *, base: type = Migration) -> list[type]:
    """Import each module or package in ``migrations_dir``; return its migrations.

    A ``migrations_dir`` holding an ``__init__.py`` is imported as one package.
    Only classes defined by the loaded modules are returned, so two applications
    in one process each see their own migrations.
    """
    loaded = set()
    entries = sorted(migrations_dir.iterdir())
    if (migrations_dir / "__init__.py").exists():
        # The directory is itself a package defining the migrations.
        entries = [migrations_dir]
    for entry in entries:
        if entry.is_file() and entry.suffix == ".py" and entry.stem != "__init__":
            loaded.add(_import_file(entry))
        elif entry.is_dir() and (entry / "__init__.py").exists():
            loaded.add(_import_file(entry))
    classes = concrete_migrations(base)
    return [
        c for c in classes if c.__module__.split(".")[0] in loaded and _is_current(c)
    ]


def _is_current(cls: type) -> bool:
    # A directory loaded twice leaves the first load's classes alive.
    module = sys.modules.get(cls.__module__)
    return getattr(module, cls.__name__, None) is cls


def _import_file(module_path: Path) -> str:
    name = module_path.stem
    locations = None
    if module_path.is_dir():
        # A package, so a migration can import its own submodules.
        locations = [str(module_path)]
        module_path = module_path / "__init__.py"

    spec = util.spec_from_file_location(
        name, str(module_path), submodule_search_locations=locations
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module from {module_path}")
    module = util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return name
