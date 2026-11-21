"""Deriving and storing a categorical layer's class vocabulary."""

import json

from pytest import raises

from macrostrat.raster_index import (
    NoClassVocabulary,
    RasterCategory,
    categories_from_aux_xml,
    categories_from_info,
    categories_from_json,
    categories_from_mapping,
    categories_from_qml,
    class_metadata_candidates,
    get_raster_info,
)
from macrostrat.raster_index.testing import CATEGORICAL_CLASSES, CATEGORICAL_COLORMAP


class TestDerivingFromRasters:
    """Band metadata is the primary source: what `gdalinfo` would show."""

    def test_candidates_found_in_band_metadata(self, raster_files):
        info = get_raster_info(str(raster_files["fine"]))
        candidates = class_metadata_candidates(info.metadata)
        assert candidates == {"MINERAL_CLASSES": CATEGORICAL_CLASSES}

    def test_categories_join_labels_to_the_color_table(self, raster_files):
        info = get_raster_info(str(raster_files["fine"]))
        categories = categories_from_info(info.metadata, info.colormap)
        assert [c.label for c in categories] == ["Kaolinite", "Alunite", "Chlorite"]
        assert categories[1].color == CATEGORICAL_COLORMAP[2]

    def test_explicit_key(self, raster_files):
        info = get_raster_info(str(raster_files["fine"]))
        categories = categories_from_info(info.metadata, key="MINERAL_CLASSES")
        assert len(categories) == 3
        # No color table passed, so the labels stand alone.
        assert categories[0].color is None

    def test_wrong_key_names_the_candidates(self, raster_files):
        info = get_raster_info(str(raster_files["fine"]))
        with raises(ValueError, match="MINERAL_CLASSES"):
            categories_from_info(info.metadata, key="ROCK_TYPES")

    def test_no_vocabulary_is_an_error(self, tmp_path, raster_files):
        """Rather than an empty vocabulary, which would look like success."""
        from macrostrat.raster_index.testing import FINE_BOUNDS, create_test_raster

        path = create_test_raster(
            tmp_path / "unlabeled.tif", FINE_BOUNDS, size=64, classes=None
        )
        info = get_raster_info(str(path))
        with raises(ValueError, match="No band-metadata item"):
            categories_from_info(info.metadata)

    def test_ambiguous_candidates_are_an_error(self):
        """Picking the wrong vocabulary would be silently wrong downstream."""
        info = {
            "band_metadata": [
                ["b1", {"FOO": "{1: 'a'}", "BAR": "{1: 'b'}"}],
            ]
        }
        with raises(ValueError, match="pass one explicitly"):
            categories_from_info(info)

    def test_known_key_wins_over_an_unrelated_candidate(self):
        info = {
            "band_metadata": [
                ["b1", {"MINERAL_CLASSES": "{1: 'Kaolinite'}", "SCALING": "{1: '2'}"}],
            ]
        }
        assert categories_from_info(info)[0].label == "Kaolinite"


class TestOtherSources:
    def test_from_mapping_sorts_and_coerces_keys(self):
        categories = categories_from_mapping(
            {"3": "Chlorite", "1": "Kaolinite"}, {1: (1, 2, 3)}
        )
        assert [c.value for c in categories] == [1, 3]
        # An RGB triple from a color table is padded to RGBA.
        assert categories[0].color == (1, 2, 3, 255)

    def test_from_json_mapping(self, tmp_path):
        path = tmp_path / "classes.json"
        path.write_text(json.dumps({"1": "Kaolinite", "2": "Alunite"}))
        assert [c.label for c in categories_from_json(path)] == [
            "Kaolinite",
            "Alunite",
        ]

    def test_from_json_list(self, tmp_path):
        path = tmp_path / "classes.json"
        path.write_text(
            json.dumps([{"value": 2, "label": "Alunite", "color": [1, 2, 3, 4]}])
        )
        categories = categories_from_json(path)
        assert categories[0].color == (1, 2, 3, 4)

    def test_from_qml(self, tmp_path):
        path = tmp_path / "style.qml"
        path.write_text("""<qgis><pipe><rasterrenderer>
            <colorPalette>
              <paletteEntry value="2" label="Alunite" color="#da3284" alpha="255"/>
              <paletteEntry value="1" label="Kaolinite" color="#969696"/>
            </colorPalette>
            </rasterrenderer></pipe></qgis>""")
        categories = categories_from_qml(path)
        assert [c.label for c in categories] == ["Kaolinite", "Alunite"]
        assert categories[1].color == (218, 50, 132, 255)

    def test_qml_without_entries_is_an_error(self, tmp_path):
        path = tmp_path / "empty.qml"
        path.write_text("<qgis/>")
        with raises(ValueError, match="paletteEntry"):
            categories_from_qml(path)


AUX_XML = """<PAMDataset>
  <PAMRasterBand band="1">
    <Description>Group 2 mineral categories</Description>
    <CategoryNames>
      <Category>Unclassified</Category>
      <Category/>
      <Category>Alunite</Category>
    </CategoryNames>
    {color_table}
  </PAMRasterBand>
</PAMDataset>"""


class TestAuxXml:
    """GDAL category names, as the AVIRIS maps carry them in a sidecar."""

    def test_names_label_values_by_position(self, tmp_path):
        path = tmp_path / "map.tif.aux.xml"
        path.write_text(AUX_XML.format(color_table=""))
        categories = categories_from_aux_xml(path, {"2": [1, 2, 3, 255]})
        # The empty name leaves value 1 unlabeled.
        assert [(c.value, c.label) for c in categories] == [
            (0, "Unclassified"),
            (2, "Alunite"),
        ]
        assert categories[1].color == (1, 2, 3, 255)

    def test_sidecar_color_table_wins(self, tmp_path):
        path = tmp_path / "map.tif.aux.xml"
        table = """<ColorTable>
          <Entry c1="0" c2="0" c3="0" c4="0"/>
          <Entry c1="9" c2="9" c3="9" c4="255"/>
          <Entry c1="218" c2="50" c3="132" c4="255"/>
        </ColorTable>"""
        path.write_text(AUX_XML.format(color_table=table))
        categories = categories_from_aux_xml(path, {"2": [1, 2, 3, 255]})
        assert categories[1].color == (218, 50, 132, 255)

    def test_no_category_names_is_an_error(self, tmp_path):
        path = tmp_path / "map.tif.aux.xml"
        path.write_text("<PAMDataset><PAMRasterBand band='1'/></PAMDataset>")
        with raises(NoClassVocabulary, match="no category names"):
            categories_from_aux_xml(path)

    def test_missing_band_is_an_error(self, tmp_path):
        path = tmp_path / "map.tif.aux.xml"
        path.write_text(AUX_XML.format(color_table=""))
        with raises(NoClassVocabulary, match="band 2"):
            categories_from_aux_xml(path, band=2)


class TestStorage:
    """The vocabulary lives in the layer's existing `metadata` jsonb column."""

    def test_round_trip(self, populated_index):
        categories = categories_from_mapping(CATEGORICAL_CLASSES, CATEGORICAL_COLORMAP)
        populated_index.set_categories("minerals", categories)
        assert populated_index.get_categories("minerals") == categories

    def test_categories_appear_on_the_layer_definition(self, populated_index):
        populated_index.set_categories(
            "minerals", [RasterCategory(value=1, label="Kaolinite")]
        )
        layer = populated_index.layer("minerals")
        assert [c.label for c in layer.categories] == ["Kaolinite"]
        # Lifted out of `metadata`, so it isn't stored twice.
        assert "categories" not in (layer.metadata or {})

    def test_other_metadata_is_preserved(self, populated_index):
        populated_index.register_layer("minerals", metadata={"source": "EMIT"})
        populated_index.set_categories(
            "minerals", [RasterCategory(value=1, label="Kaolinite")]
        )
        layer = populated_index.layer("minerals")
        assert layer.metadata == {"source": "EMIT"}
        assert layer.categories is not None

    def test_registering_a_layer_does_not_drop_the_vocabulary(self, populated_index):
        """A later `define-layer` must not silently destroy the class names."""
        populated_index.set_categories(
            "minerals", [RasterCategory(value=1, label="Kaolinite")]
        )
        populated_index.register_layer("minerals", name="Renamed")
        assert populated_index.get_categories("minerals")

    def test_categories_reach_selected_assets(self, populated_index):
        """Serving needs the vocabulary on the asset, from the same query."""
        populated_index.set_categories(
            "minerals", [RasterCategory(value=2, label="Alunite")]
        )
        assets = populated_index.assets_for_bbox(
            -105.0, 40.0, -104.9, 40.1, ["minerals"]
        )
        assert assets
        assert assets[0].categories[0].label == "Alunite"

    def test_undefined_layer_is_an_error(self, populated_index):
        with raises(ValueError, match="not defined"):
            populated_index.set_categories("nonexistent", [])

    def test_no_vocabulary_is_an_empty_list(self, populated_index):
        assert populated_index.get_categories("other") == []


class TestStoredRasterInfo:
    """Deriving a vocabulary shouldn't need to reopen a file in object storage."""

    def test_info_is_recoverable_by_slug(self, populated_index):
        info = populated_index.raster_info("fine", layer="minerals")
        assert info is not None
        assert class_metadata_candidates(info) == {
            "MINERAL_CLASSES": CATEGORICAL_CLASSES
        }

    def test_unknown_raster_is_none(self, populated_index):
        assert populated_index.raster_info("nope") is None
