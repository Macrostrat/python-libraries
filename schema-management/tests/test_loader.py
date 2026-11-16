from macrostrat.schema_management import load_migrations

MODULE = """
from macrostrat.schema_management import Migration


class Base(Migration):
    subsystem = "t"


class First(Base):
    name = "first"


class Second(Base):
    name = "second"
    depends_on = ["first"]
"""


def test_loads_modules_and_packages(tmp_path):
    (tmp_path / "loader_single.py").write_text(MODULE)
    package = tmp_path / "loader_pkg"
    package.mkdir()
    (package / "__init__.py").write_text(MODULE.replace("first", "third"))
    names = sorted(c.name for c in load_migrations(tmp_path))
    assert names == ["first", "second", "second", "third"]


def test_a_package_directory_is_loaded_as_one_package(tmp_path):
    package = tmp_path / "loader_whole"
    package.mkdir()
    (package / "__init__.py").write_text(MODULE)
    (package / "notes.sql").write_text("SELECT 1;")
    assert sorted(c.name for c in load_migrations(package)) == ["first", "second"]


def test_reloading_does_not_duplicate(tmp_path):
    (tmp_path / "loader_again.py").write_text(MODULE)
    load_migrations(tmp_path)
    assert len(load_migrations(tmp_path)) == 2
