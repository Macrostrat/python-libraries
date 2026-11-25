"""The CLI's database wiring.

The commands themselves are thin wrappers over `RasterIndex` (covered in
test_raster_index.py); what's worth testing here is how a connection is
resolved — explicitly, from a host application's default, or not at all.
"""

import re

from pytest import fixture
from typer.testing import CliRunner

from macrostrat.raster_index.cli import cli, set_default_connection


@fixture
def runner():
    return CliRunner()


@fixture
def database_arg(populated_index):
    return ["--database", str(populated_index.engine.url)]


@fixture(autouse=True)
def no_host_default():
    """Each test starts with no host-configured connection."""
    set_default_connection(None)
    yield
    set_default_connection(None)


def test_help_needs_no_database(runner):
    """`--help` must work before anything is configured."""
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Manage indexed raster datasets" in result.output


def test_missing_database_is_a_parameter_error(runner):
    result = runner.invoke(cli, ["layers"])
    assert result.exit_code != 0
    assert "No database configured" in result.output


def test_database_option(runner, database_arg):
    result = runner.invoke(cli, [*database_arg, "layers"])
    assert result.exit_code == 0, result.output
    assert "minerals" in result.output


def test_host_supplied_default(runner, populated_index):
    """What Macrostrat does: configure once, commands need no --database."""
    set_default_connection(lambda: str(populated_index.engine.url))
    result = runner.invoke(cli, ["layers"])
    assert result.exit_code == 0, result.output
    assert "minerals" in result.output


def test_option_overrides_host_default(runner, populated_index):
    set_default_connection("postgresql://nonexistent/should-not-be-used")
    result = runner.invoke(
        cli, ["--database", str(populated_index.engine.url), "layers"]
    )
    assert result.exit_code == 0, result.output
    assert "minerals" in result.output


def test_environment_variable(runner, populated_index, monkeypatch):
    monkeypatch.setenv("RASTER_INDEX_DATABASE", str(populated_index.engine.url))
    result = runner.invoke(cli, ["rasters"])
    assert result.exit_code == 0, result.output
    assert "fine" in result.output


def test_assets_command(runner, database_arg):
    result = runner.invoke(
        cli, [*database_arg, "assets", "12", "853", "1549", "--layer", "minerals"]
    )
    assert result.exit_code == 0, result.output


def test_unprovisioned_database_is_reported(runner, raster_index, monkeypatch):
    """A database without the schema gets a pointed message, not a traceback."""
    monkeypatch.setattr(type(raster_index), "schema_exists", lambda self: False)
    result = runner.invoke(cli, ["--database", str(raster_index.engine.url), "layers"])
    assert result.exit_code == 1
    assert "raster_layers" in result.output


class TestSetCategories:
    """Naming a categorical layer's classes, the operator-facing path."""

    def test_from_an_indexed_raster(self, runner, database_arg, populated_index):
        result = runner.invoke(
            cli, [*database_arg, "set-categories", "minerals", "--from", "fine"]
        )
        assert result.exit_code == 0, result.output
        assert "Kaolinite" in result.output
        assert [c.label for c in populated_index.get_categories("minerals")] == [
            "Kaolinite",
            "Alunite",
            "Chlorite",
        ]

    def test_from_a_file_that_is_not_indexed(
        self, runner, database_arg, populated_index, raster_files
    ):
        """`--from` takes an href too, for a raster not (yet) in the index."""
        result = runner.invoke(
            cli,
            [
                *database_arg,
                "set-categories",
                "minerals",
                "--from",
                str(raster_files["elsewhere"]),
            ],
        )
        assert result.exit_code == 0, result.output
        assert populated_index.get_categories("minerals")

    def test_falls_back_to_the_aux_xml_sidecar(
        self, runner, database_arg, populated_index, tmp_path
    ):
        """Category names in a sidecar, when band metadata names no classes."""
        from macrostrat.raster_index.testing import FINE_BOUNDS, create_test_raster

        path = create_test_raster(
            tmp_path / "unlabeled.tif", FINE_BOUNDS, size=64, classes=None
        )
        sidecar = tmp_path / "unlabeled.tif.aux.xml"
        sidecar.write_text(
            "<PAMDataset><PAMRasterBand band='1'><CategoryNames>"
            "<Category>None</Category><Category>Kaolinite</Category>"
            "</CategoryNames></PAMRasterBand></PAMDataset>"
        )
        for source in (path, sidecar):
            result = runner.invoke(
                cli,
                [*database_arg, "set-categories", "minerals", "--from", str(source)],
            )
            assert result.exit_code == 0, result.output
            labels = [c.label for c in populated_index.get_categories("minerals")]
            assert labels == ["None", "Kaolinite"]

    def test_no_vocabulary_anywhere_is_reported(self, runner, database_arg, tmp_path):
        from macrostrat.raster_index.testing import FINE_BOUNDS, create_test_raster

        path = create_test_raster(
            tmp_path / "unlabeled.tif", FINE_BOUNDS, size=64, classes=None
        )
        result = runner.invoke(
            cli, [*database_arg, "set-categories", "minerals", "--from", str(path)]
        )
        assert result.exit_code == 1
        assert "No sidecar either" in result.output

    def test_wrong_metadata_key_is_reported(self, runner, database_arg):
        result = runner.invoke(
            cli,
            [
                *database_arg,
                "set-categories",
                "minerals",
                "--from",
                "fine",
                "--metadata-key",
                "ROCK_TYPES",
            ],
        )
        assert result.exit_code == 1
        assert "MINERAL_CLASSES" in result.output

    def test_undefined_layer_is_reported(self, runner, database_arg):
        result = runner.invoke(
            cli, [*database_arg, "set-categories", "nonexistent", "--from", "fine"]
        )
        assert result.exit_code == 1
        assert "not defined" in result.output

    def test_a_source_is_required(self, runner, database_arg):
        result = runner.invoke(cli, [*database_arg, "set-categories", "minerals"])
        assert result.exit_code != 0
        # CI's terminal colors the usage error, splitting `--from` with escapes.
        assert "--from" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)

    def test_from_a_qml_style(self, runner, database_arg, populated_index, tmp_path):
        """A label file is read by its extension, with no flag of its own."""
        style = tmp_path / "style.qml"
        style.write_text(
            '<qgis><paletteEntry value="2" label="Alunite" color="#da3284"/></qgis>'
        )
        result = runner.invoke(
            cli, [*database_arg, "set-categories", "minerals", "--from", str(style)]
        )
        assert result.exit_code == 0, result.output
        categories = populated_index.get_categories("minerals")
        assert [c.label for c in categories] == ["Alunite"]


def test_info_metadata_surfaces_class_vocabularies(runner, raster_files):
    """`info --metadata` is how you find out what set-categories can read."""
    result = runner.invoke(cli, ["info", str(raster_files["fine"]), "--metadata"])
    assert result.exit_code == 0, result.output
    assert "MINERAL_CLASSES" in result.output
    assert "3 classes" in result.output


def test_info_without_metadata_stays_terse(runner, raster_files):
    result = runner.invoke(cli, ["info", str(raster_files["fine"])])
    assert result.exit_code == 0, result.output
    assert "MINERAL_CLASSES" not in result.output
