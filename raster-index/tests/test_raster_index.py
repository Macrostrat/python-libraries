"""Indexing and asset selection.

The behavior worth pinning down is not "can we insert a row" but "given a tile,
do we get the right rasters, in the right order, and nothing else".
"""

import shutil

from morecantile import tms
from pytest import fixture
from shapely.geometry import Point, box, shape

from macrostrat.raster_index import get_raster_info
from macrostrat.raster_index.index import default_slug
from macrostrat.raster_index.testing import COARSE_BOUNDS, FINE_BOUNDS

web_mercator = tms.get("WebMercatorQuad")

# A point inside both test rasters.
OVERLAP = Point(-104.95, 40.05)
# Inside the coarse raster only.
COARSE_ONLY = Point(-104.85, 40.15)


def _tile_for(point: Point, zoom: int):
    return web_mercator.tile(point.x, point.y, zoom)


class TestRasterMetadata:
    def test_footprint_matches_bounds(self, raster_files):
        info = get_raster_info(str(raster_files["fine"]))
        footprint = shape(info.geometry)
        assert footprint.equals(box(*FINE_BOUNDS))

    def test_reads_dtype_and_nodata(self, raster_files):
        info = get_raster_info(str(raster_files["fine"]))
        assert info.dtype == "uint8"
        assert info.nbands == 1
        assert info.nodata == 0
        assert info.crs == "EPSG:4326"

    def test_captures_embedded_colormap(self, raster_files):
        info = get_raster_info(str(raster_files["fine"]))
        assert info.colormap is not None
        # GDAL pads a color table to 256 entries; the classes we wrote are in it.
        assert info.colormap[2][:3] == (218, 50, 132)

    def test_finer_raster_has_higher_maxzoom(self, raster_files):
        fine = get_raster_info(str(raster_files["fine"]))
        coarse = get_raster_info(str(raster_files["coarse"]))
        assert fine.maxzoom > coarse.maxzoom

    def test_default_slug_from_href(self):
        assert default_slug("s3://bucket/prefix/nevada_clipped.tif") == "nevada_clipped"
        assert default_slug("https://example.org/a/b.tif?v=2") == "b"


class TestRegistration:
    def test_layers_are_listed(self, populated_index):
        slugs = [layer.slug for layer in populated_index.layers()]
        assert slugs == ["minerals", "other"]

    def test_rasters_are_scoped_to_layer(self, populated_index):
        rasters = populated_index.rasters("minerals")
        assert {r["slug"] for r in rasters} == {"fine", "coarse"}

    def test_registration_is_idempotent(self, populated_index, raster_files):
        before = len(populated_index.rasters())
        populated_index.add_raster(raster_files["fine"], layer="minerals", slug="fine")
        assert len(populated_index.rasters()) == before

    def test_layer_bounds_cover_both_rasters(self, populated_index):
        bounds = populated_index.layer_bounds(["minerals"])
        assert box(*bounds).contains(box(*COARSE_BOUNDS))
        assert box(*bounds).contains(box(*FINE_BOUNDS))

    def test_unknown_layer_has_no_bounds(self, populated_index):
        assert populated_index.layer_bounds(["nonexistent"]) is None


class TestAssetSelection:
    def test_overlap_returns_both_finest_first(self, populated_index):
        tile = _tile_for(OVERLAP, 12)
        assets = populated_index.assets_for_tile(tile.x, tile.y, tile.z, ["minerals"])
        assert [a.slug for a in assets] == ["fine", "coarse"]

    def test_non_overlapping_area_returns_one(self, populated_index):
        tile = _tile_for(COARSE_ONLY, 12)
        assets = populated_index.assets_for_tile(tile.x, tile.y, tile.z, ["minerals"])
        assert [a.slug for a in assets] == ["coarse"]

    def test_other_layers_are_excluded(self, populated_index):
        tile = _tile_for(OVERLAP, 12)
        assets = populated_index.assets_for_tile(tile.x, tile.y, tile.z, ["other"])
        assert assets == []

    def test_layer_order_sets_compositing_order(self, populated_index, raster_files):
        """A layer listed first wins the pixel, regardless of resolution."""
        # `elsewhere` doesn't overlap, so use the two layers over the same tile
        # by temporarily moving the coarse raster into the `other` layer.
        populated_index.add_raster(raster_files["coarse"], layer="other", slug="coarse")
        try:
            tile = _tile_for(OVERLAP, 12)
            assets = populated_index.assets_for_tile(
                tile.x, tile.y, tile.z, ["other", "minerals"]
            )
            assert [a.layer for a in assets] == ["other", "minerals"]
        finally:
            populated_index.add_raster(
                raster_files["coarse"], layer="minerals", slug="coarse"
            )

    def test_deep_zoom_is_overscaled(self, populated_index):
        tile = _tile_for(OVERLAP, 22)
        assets = populated_index.assets_for_tile(tile.x, tile.y, tile.z, ["minerals"])
        assert assets, "assets should still be found, just flagged"
        assert all(a.overscaled for a in assets)
        assert not populated_index.should_generate_tile(
            tile.x, tile.y, tile.z, ["minerals"]
        )

    def test_reasonable_zoom_should_generate(self, populated_index):
        tile = _tile_for(OVERLAP, 12)
        assert populated_index.should_generate_tile(
            tile.x, tile.y, tile.z, ["minerals"]
        )

    def test_footprints_are_geojson(self, populated_index):
        result = populated_index.footprints(["minerals"])
        assert result["type"] == "FeatureCollection"
        assert len(result["features"]) == 2
        feature = result["features"][0]
        assert feature["geometry"]["type"] == "Polygon"
        assert feature["properties"]["layer"] == "minerals"


class TestUnifiedSelection:
    """All lookups run through `raster_layers.select_rasters`.

    The point of collapsing them is that the ordering and zoom rules can't drift
    apart between entry points, so that agreement is what's asserted here.
    """

    def test_bbox_and_tile_agree_over_the_same_area(self, populated_index):
        tile = _tile_for(OVERLAP, 12)
        west, south, east, north = web_mercator.bounds(tile)

        by_tile = populated_index.assets_for_tile(tile.x, tile.y, tile.z, ["minerals"])
        by_bbox = populated_index.assets_for_bbox(
            west, south, east, north, ["minerals"]
        )
        assert [a.slug for a in by_bbox] == [a.slug for a in by_tile]

    def test_bbox_never_flags_overscaled(self, populated_index):
        """A bbox query has no zoom, so nothing can be 'past its resolution'."""
        assets = populated_index.assets_for_bbox(
            -105.0, 40.0, -104.9, 40.1, ["minerals"]
        )
        assert assets
        assert not any(a.overscaled for a in assets)

    def test_footprints_are_unfiltered_by_zoom(self, populated_index):
        """Coverage stays visible even where the rasters would be overscaled."""
        result = populated_index.footprints(["minerals"])
        assert len(result["features"]) == 2

    def test_selection_order_is_stable(self, populated_index):
        tile = _tile_for(OVERLAP, 12)
        runs = [
            [
                a.slug
                for a in populated_index.assets_for_tile(
                    tile.x, tile.y, tile.z, ["minerals"]
                )
            ]
            for _ in range(3)
        ]
        assert runs[0] == runs[1] == runs[2] == ["fine", "coarse"]


class TestLayerExtent:
    """Bounds and native zoom range, answered together.

    The zoom range is what `/info`, `tilejson.json` and WMTS advertise. Claiming
    the tile grid's full 0-24 tells clients there is data at resolutions no
    raster in the layer can produce — and in WMTS it costs one
    `TileMatrixLimits` block per claimed zoom, per advertised layer.
    """

    def test_zoom_range_spans_the_rasters(self, populated_index):
        rasters = populated_index.rasters("minerals")
        extent = populated_index.layer_extent(["minerals"])
        assert extent is not None
        assert extent.minzoom == min(r["minzoom"] for r in rasters)
        assert extent.maxzoom == max(r["maxzoom"] for r in rasters)

    def test_zoom_range_is_narrower_than_the_grid(self, populated_index):
        """Otherwise there was no point doing this."""
        extent = populated_index.layer_extent(["minerals"])
        assert extent.maxzoom < 24

    def test_bounds_match_the_dedicated_helper(self, populated_index):
        extent = populated_index.layer_extent(["minerals"])
        assert extent.bounds == populated_index.layer_bounds(["minerals"])

    def test_narrowing_to_a_raster_narrows_the_range(self, populated_index):
        """`?datasets=` should narrow what the mosaic claims about itself."""
        whole = populated_index.layer_extent(["minerals"])
        fine = populated_index.layer_extent(["minerals"], rasters=["fine"])
        assert fine is not None
        assert (fine.minzoom, fine.maxzoom) != (whole.minzoom, whole.maxzoom)

    def test_empty_layer_has_no_extent(self, populated_index):
        assert populated_index.layer_extent(["minerals"], rasters=["nope"]) is None


class TestUnassignedRasters:
    @fixture
    def loose_raster(self, populated_index, raster_files, tmp_path):
        href = tmp_path / "loose.tif"
        shutil.copy(raster_files["fine"], href)
        yield href
        populated_index.remove_raster(str(href))

    def test_registers_without_a_layer(self, populated_index, loose_raster):
        populated_index.add_raster(loose_raster)
        rows = populated_index.rasters(unassigned=True)
        assert [r["slug"] for r in rows] == ["loose"]
        assert rows[0]["layer"] is None

    def test_unassigned_raster_is_never_selected(self, populated_index, loose_raster):
        layers = ["minerals", "other"]
        before = populated_index.footprints(layers)
        populated_index.add_raster(loose_raster)
        assert populated_index.footprints(layers) == before

    def test_layerless_reregistration_keeps_the_layer(
        self, populated_index, raster_files
    ):
        populated_index.add_raster(raster_files["fine"], slug="fine")
        slugs = {r["slug"] for r in populated_index.rasters("minerals")}
        assert slugs == {"fine", "coarse"}
        assert populated_index.rasters(unassigned=True) == []

    def test_naming_a_layer_assigns_it(self, populated_index, loose_raster):
        populated_index.add_raster(loose_raster)
        populated_index.add_raster(loose_raster, layer="other")
        assert populated_index.rasters(unassigned=True) == []
        assert "loose" in {r["slug"] for r in populated_index.rasters("other")}


class TestIndexedHrefs:
    def test_finds_rasters_in_a_layer(self, populated_index, raster_files):
        href = str(raster_files["coarse"])
        missing = str(raster_files["coarse"]) + ".absent"
        assert populated_index.indexed_hrefs([href, missing]) == {href}

    def test_nothing_asked_is_nothing_found(self, populated_index):
        assert populated_index.indexed_hrefs([]) == set()
