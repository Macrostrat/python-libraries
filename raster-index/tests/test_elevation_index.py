"""What an elevation layer asks of the index, with nothing read but headers.

Two continuous rasters in the shape of the real arrangement — a fine land tile
whose ocean is stored as 0, over a coarse global layer with NaN nodata — and the
four mechanisms that make them serve elevation correctly: declared registration
with verification, a priori footprints, the nodata override, and the scale
window. Its own database, so the categorical fixtures elsewhere are untouched.
"""

import json
import math
from pathlib import Path

from pytest import fixture
from shapely.geometry import box, shape
from sqlalchemy import text
from sqlalchemy.engine import make_url

from macrostrat.database.utils import temporary_database
from macrostrat.raster_index import (
    DeclaredRaster,
    FootprintSource,
    RasterIndex,
    get_raster_info,
)
from macrostrat.raster_index.testing import create_test_dem

# A 0.1° "land" tile at 128 px (~85 m pixels), with a sea along its western third.
FINE_BOUNDS = (-105.0, 40.0, -104.9, 40.1)
FINE_OCEAN = (-105.0, 40.0, -104.97, 40.1)
# A 1° "global" tile at 64 px (~1.7 km pixels) underneath it, everywhere valid.
COARSE_BOUNDS = (-105.5, 39.5, -104.5, 40.5)
# Far away, to have something the coastline clip cannot touch.
ELSEWHERE_BOUNDS = (10.0, 10.0, 10.1, 10.1)

# "Land": everything east of the fine tile's ocean, as GeoJSON.
LAND = {
    "type": "Feature",
    "geometry": {
        "type": "Polygon",
        "coordinates": [
            [
                [-104.97, 39.0],
                [-104.0, 39.0],
                [-104.0, 41.0],
                [-104.97, 41.0],
                [-104.97, 39.0],
            ]
        ],
    },
}


@fixture(scope="module")
def dem_files(tmp_path_factory) -> dict[str, Path]:
    directory = tmp_path_factory.mktemp("dems")
    return {
        "fine": create_test_dem(
            directory / "fine.tif",
            FINE_BOUNDS,
            size=128,
            dtype="int16",
            nodata=-32768,
            base=1000,
            ocean=FINE_OCEAN,
            ocean_value=0,
        ),
        "coarse": create_test_dem(
            directory / "coarse.tif",
            COARSE_BOUNDS,
            size=64,
            dtype="float32",
            nodata=math.nan,
            base=-4000,
            step=10,
        ),
        "elsewhere": create_test_dem(
            directory / "elsewhere.tif", ELSEWHERE_BOUNDS, size=32, base=5
        ),
    }


@fixture(scope="module")
def index(database_url, pytestconfig, dem_files):
    url = make_url(str(database_url)).set(database="raster_elevation_test")
    with temporary_database(
        url, drop=pytestconfig.option.teardown, ensure_empty=True, force_drop=True
    ) as engine:
        index = RasterIndex(engine)
        index.create_schema()
        index.register_layer("land", name="Fine land tiles")
        index.register_layer("global", name="Coarse global layer")
        index.add_raster(dem_files["fine"], layer="land", slug="fine")
        index.add_raster(dem_files["elsewhere"], layer="land", slug="elsewhere")
        index.add_raster(dem_files["coarse"], layer="global", slug="coarse")
        yield index


def _footprint_area(index, slug) -> float:
    row = next(r for r in index.rasters() if r["slug"] == slug)
    return shape(json.loads(row["footprint"])).area


class TestBounds:
    def test_bounds_are_stored_apart_from_the_footprint(self, index):
        row = next(r for r in index.rasters("land") if r["slug"] == "fine")
        assert tuple(row["bounds"]) == FINE_BOUNDS

    def test_file_nodata_is_recorded(self, index):
        rows = {r["slug"]: r for r in index.rasters()}
        assert rows["fine"]["nodata"] == -32768
        assert math.isnan(rows["coarse"]["nodata"])
        assert rows["fine"]["nodata_override"] is None


class TestDeclared:
    """Registering from a declaration, then checking it against the files."""

    def test_declared_rows_match_a_real_read(self, index, dem_files):
        info = get_raster_info(str(dem_files["fine"]))
        index.register_layer("declared", name="Declared")
        written = index.add_declared(
            "declared",
            [
                DeclaredRaster(
                    href=str(dem_files["fine"]),
                    slug="fine-declared",
                    bounds=FINE_BOUNDS,
                    minzoom=info.minzoom,
                    maxzoom=info.maxzoom,
                    dtype="int16",
                    nodata=-32768,
                    info={"declared_from": "the test's own knowledge"},
                )
            ],
        )
        # The href is the key, so declaring the same file *moves* it. Move it back
        # under its original layer afterwards.
        try:
            assert written == 1
            report = index.verify_sample("declared", sample=5, seed=1)
            assert report.checked == 1
            assert report.ok, [str(m) for m in report.mismatches]
        finally:
            index.add_raster(dem_files["fine"], layer="land", slug="fine")
            index.remove_layer("declared")

    def test_a_wrong_declaration_is_caught(self, index, dem_files):
        info = get_raster_info(str(dem_files["elsewhere"]))
        index.register_layer("declared", name="Declared")
        index.add_declared(
            "declared",
            [
                DeclaredRaster(
                    href=str(dem_files["elsewhere"]),
                    slug="wrong",
                    bounds=ELSEWHERE_BOUNDS,
                    minzoom=info.minzoom,
                    maxzoom=info.maxzoom,
                    dtype="float32",  # it is int16
                    nodata=-9999,  # it is -32768
                )
            ],
        )
        try:
            report = index.verify_sample("declared", sample=5, seed=1)
            assert not report.ok
            assert {m.field for m in report.mismatches} == {"dtype", "nodata"}
        finally:
            index.add_raster(dem_files["elsewhere"], layer="land", slug="elsewhere")
            index.remove_layer("declared")

    def test_an_unreadable_raster_is_reported_not_raised(self, index):
        index.register_layer("declared", name="Declared")
        index.add_declared(
            "declared",
            [
                DeclaredRaster(
                    href="/nowhere/N43W090.tif",
                    bounds=(-90, 43, -89, 44),
                    minzoom=8,
                    maxzoom=12,
                    dtype="int16",
                )
            ],
        )
        try:
            report = index.verify_sample("declared", sample=5)
            assert report.unreadable and not report.ok
        finally:
            index.remove_layer("declared", cascade=True)

    def test_declared_footprint_is_used(self, index):
        index.register_layer("declared", name="Declared")
        half = box(-90, 43, -89.5, 44)
        index.add_declared(
            "declared",
            [
                DeclaredRaster(
                    href="/nowhere/N43W090.tif",
                    bounds=(-90, 43, -89, 44),
                    footprint=half.__geo_interface__,
                    minzoom=8,
                    maxzoom=12,
                    dtype="int16",
                )
            ],
        )
        try:
            assert _footprint_area(index, "N43W090") == half.area
            assert index.assets_for_point(-89.25, 43.5, ["declared"]) == []
            assert [
                a.slug for a in index.assets_for_point(-89.75, 43.5, ["declared"])
            ] == ["N43W090"]
        finally:
            index.remove_layer("declared", cascade=True)


class TestNodataOverride:
    def test_override_rides_on_the_asset(self, index):
        assert index.assets_for_point(-104.95, 40.05, ["land"])[0].nodata is None
        index.set_nodata("land", 0)
        try:
            asset = index.assets_for_point(-104.95, 40.05, ["land"])[0]
            assert asset.nodata == 0
            rows = {r["slug"]: r for r in index.rasters("land")}
            # The file's own nodata is untouched: verification still compares it.
            assert rows["fine"]["nodata"] == -32768
            assert rows["fine"]["nodata_override"] == 0
        finally:
            index.set_nodata("land", None)
        assert index.assets_for_point(-104.95, 40.05, ["land"])[0].nodata is None

    def test_nan_override_survives_json(self, index):
        index.set_nodata("global", math.nan)
        try:
            asset = index.assets_for_point(-105, 40, ["global"])[0]
            assert math.isnan(asset.nodata)
        finally:
            index.set_nodata("global", None)

    def test_override_survives_re_registration(self, index, dem_files):
        index.set_nodata("land", 0, rasters=["fine"])
        try:
            index.add_raster(dem_files["fine"], layer="land", slug="fine")
            assert index.assets_for_point(-104.95, 40.05, ["land"])[0].nodata == 0
        finally:
            index.set_nodata("land", None)


class TestExternalFootprints:
    """Footprints clipped from a geometry, never traced from pixels."""

    def test_dry_run_reports_without_writing(self, index):
        before = _footprint_area(index, "fine")
        report = index.set_footprints(
            "land", FootprintSource.geojson(LAND), apply=False
        )
        assert not report.applied
        clipped = {r.slug: r for r in report.clipped}
        assert clipped["fine"].fraction == approx_fraction(0.7)
        assert [r.slug for r in report.empty] == ["elsewhere"]
        assert _footprint_area(index, "fine") == before

    def test_clip_is_bounds_intersect_source(self, index):
        report = index.set_footprints("land", FootprintSource.geojson(LAND))
        assert report.applied
        expected = box(*FINE_BOUNDS).intersection(shape(LAND["geometry"]))
        assert _footprint_area(index, "fine") == approx_fraction(
            expected.area, abs=1e-9
        )
        # The sea is no longer selected; the land still is.
        assert index.assets_for_point(-104.99, 40.05, ["land"]) == []
        assert [a.slug for a in index.assets_for_point(-104.95, 40.05, ["land"])] == [
            "fine"
        ]

    def test_outside_rasters_are_left_alone(self, index):
        assert _footprint_area(index, "elsewhere") == box(*ELSEWHERE_BOUNDS).area

    def test_rerun_does_not_erode(self, index):
        first = _footprint_area(index, "fine")
        index.set_footprints("land", FootprintSource.geojson(LAND))
        assert _footprint_area(index, "fine") == first

    def test_re_registration_keeps_the_clipped_footprint(self, index, dem_files):
        clipped = _footprint_area(index, "fine")
        assert clipped < box(*FINE_BOUNDS).area
        index.add_raster(dem_files["fine"], layer="land", slug="fine")
        assert _footprint_area(index, "fine") == clipped

    def test_from_a_table(self, index):
        with index.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE public.test_land AS SELECT "
                    "ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326) AS geom"
                ),
                dict(g=json.dumps(LAND["geometry"])),
            )
        try:
            report = index.set_footprints(
                "land", FootprintSource.table("public.test_land"), apply=False
            )
            assert {r.slug for r in report.clipped} == {"fine"}
        finally:
            with index.engine.begin() as conn:
                conn.execute(text("DROP TABLE public.test_land"))

    def test_identifiers_are_validated(self):
        from pytest import raises

        with raises(ValueError):
            FootprintSource.table("public.land; DROP TABLE x")


def approx_fraction(value, abs=1e-6):
    from pytest import approx

    return approx(value, abs=abs)


class TestScaleWindow:
    """Priority is a function of the query's scale, not only of dataset order."""

    @fixture(autouse=True, scope="class")
    def unclipped(self, index, dem_files):
        """Bounding-box footprints, so overlap is total and only scale decides."""
        index.add_raster(
            dem_files["fine"],
            layer="land",
            slug="fine",
            footprint=box(*FINE_BOUNDS).__geo_interface__,
        )

    def test_default_ordering_is_finest_first(self, index):
        assets = index.assets_for_point(-104.95, 40.05, ["land", "global"])
        assert [a.slug for a in assets] == ["fine", "coarse"]
        assets = index.assets_for_bbox(
            -104.99, 40.01, -104.91, 40.09, ["global", "land"]
        )
        # Layer order still comes first.
        assert [a.slug for a in assets] == ["coarse", "fine"]

    def test_a_coarse_query_prefers_the_coarse_raster(self, index):
        rows = {r["slug"]: r for r in index.rasters()}
        coarse_zoom = rows["coarse"]["maxzoom"]
        assert coarse_zoom < rows["fine"]["minzoom"], "fixtures must differ in scale"
        assets = index.assets_for_point(
            -104.95, 40.05, ["land", "global"], zoom=coarse_zoom, scale_aware=True
        )
        # Same layer list, opposite answer — because the layers are listed as
        # equals here. In practice layer order is the outer key; see below.
        assert [a.slug for a in assets] == ["coarse"] or [a.slug for a in assets][
            0
        ] == "coarse"

    def test_layer_order_still_outranks_scale(self, index):
        rows = {r["slug"]: r for r in index.rasters()}
        assets = index.assets_for_point(
            -104.95,
            40.05,
            ["land", "global"],
            zoom=rows["fine"]["minzoom"],
            scale_aware=True,
        )
        assert [a.slug for a in assets] == ["fine", "coarse"]

    def test_far_too_fine_rasters_are_filtered_out(self, index):
        rows = {r["slug"]: r for r in index.rasters()}
        far_below = rows["fine"]["minzoom"] - 4
        assets = index.assets_for_bbox(
            *COARSE_BOUNDS, ["land", "global"], zoom=far_below, zoom_tolerance=3
        )
        assert [a.slug for a in assets] == ["coarse"]

    def test_no_zoom_means_no_filter(self, index):
        assets = index.assets_for_bbox(*COARSE_BOUNDS, ["land", "global"])
        assert {a.slug for a in assets} == {"fine", "coarse"}
        assert not any(a.overscaled for a in assets)

    def test_a_point_query_is_exact_and_carries_bounds(self, index):
        assets = index.assets_for_point(-104.95, 40.05, ["land"])
        assert assets[0].bounds == FINE_BOUNDS
        assert index.assets_for_point(-104.95, 40.2, ["land"]) == []

    def test_geometry_selection_follows_the_line(self, index):
        line = {
            "type": "LineString",
            "coordinates": [[-105.4, 39.6], [-105.2, 39.6]],
        }
        assets = index.assets_for_geometry(line, ["land", "global"])
        assert [a.slug for a in assets] == ["coarse"]
        line["coordinates"] = [[-105.4, 39.6], [-104.95, 40.05]]
        assets = index.assets_for_geometry(line, ["land", "global"])
        assert [a.slug for a in assets] == ["fine", "coarse"]
