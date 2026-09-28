"""Point and line sampling — the primitive an elevation service is built on.

The fixture reproduces the real arrangement in miniature: a fine land tile that
stores its sea as 0 (SRTM GL1), over a coarse global layer with NaN nodata
(SRTM15+). What matters is not the numbers but *which raster answers*, and that
water falls through rather than reading as sea level.
"""

import math
from pathlib import Path

from pytest import fixture
from sqlalchemy.engine import make_url

from macrostrat.database.utils import temporary_database
from macrostrat.raster_index import RasterIndex
from macrostrat.raster_index.testing import create_test_dem
from macrostrat.raster_layers import PGRasterMosaic, sample_line, sample_point
from macrostrat.raster_layers.factory import DatasetParams

FINE_BOUNDS = (-105.0, 40.0, -104.9, 40.1)
FINE_OCEAN = (-105.0, 40.0, -104.97, 40.1)
COARSE_BOUNDS = (-105.5, 39.5, -104.5, 40.5)

LAND_POINT = (-104.92, 40.05)
SEA_POINT = (-104.99, 40.05)
FAR_POINT = (10.0, 10.0)

LAYERS = ["land", "global"]


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
    }


@fixture(scope="module")
def dem_index(database_url, pytestconfig, dem_files):
    url = make_url(str(database_url)).set(database="raster_sampling_test")
    with temporary_database(
        url, drop=pytestconfig.option.teardown, ensure_empty=True, force_drop=True
    ) as engine:
        index = RasterIndex(engine)
        index.create_schema()
        index.register_layer("land", name="Fine land tiles")
        index.register_layer("global", name="Coarse global layer")
        index.add_raster(dem_files["fine"], layer="land", slug="fine")
        index.add_raster(dem_files["coarse"], layer="global", slug="coarse")
        # The whole reason the override exists: the sea is stored as 0.
        index.set_nodata("land", 0)
        yield index


class TestPoint:
    def test_land_is_read_from_the_fine_raster(self, dem_index):
        sample = sample_point(dem_index, LAYERS, *LAND_POINT)
        assert sample.source.slug == "fine"
        assert 1000 <= sample.value <= 1127

    def test_sea_falls_through_to_bathymetry(self, dem_index):
        sample = sample_point(dem_index, LAYERS, *SEA_POINT)
        assert sample.source.slug == "coarse"
        assert sample.value < 0

    def test_without_the_override_the_sea_reads_as_zero(self, dem_index):
        dem_index.set_nodata("land", None)
        try:
            sample = sample_point(dem_index, LAYERS, *SEA_POINT)
            assert sample.source.slug == "fine"
            assert sample.value == 0
        finally:
            dem_index.set_nodata("land", 0)

    def test_nothing_there(self, dem_index):
        sample = sample_point(dem_index, LAYERS, *FAR_POINT)
        assert sample.value is None and sample.source is None

    def test_a_coarse_resolution_prefers_the_coarse_raster(self, dem_index):
        """Same point, same layers — the requested scale decides."""
        rows = {r["slug"]: r for r in dem_index.rasters()}
        assert rows["coarse"]["maxzoom"] < rows["fine"]["minzoom"] - 3
        sample = sample_point(
            dem_index, ["global", "land"], *LAND_POINT, resolution=5000
        )
        assert sample.source.slug == "coarse"
        sample = sample_point(dem_index, ["global", "land"], *LAND_POINT)
        assert sample.source.slug == "coarse", "layer order is the outer key"
        sample = sample_point(dem_index, LAYERS, *LAND_POINT, resolution=5000)
        assert sample.source.slug == "coarse", "the fine tile is filtered out entirely"


class TestLine:
    def test_a_profile_across_the_coast(self, dem_index):
        profile = sample_line(
            dem_index, LAYERS, (-104.995, 40.05), (-104.905, 40.05), samples=50
        )
        assert len(profile.samples) == 50
        assert all(s.value is not None for s in profile.samples)
        sources = [s.source.slug for s in profile.samples]
        assert sources[0] == "coarse" and sources[-1] == "fine"
        # Sea first, then land, in one run each: the coast is crossed once.
        assert sources == sorted(sources)

    def test_distances_are_cumulative_metres(self, dem_index):
        profile = sample_line(
            dem_index, LAYERS, (-104.99, 40.05), (-104.91, 40.05), samples=9
        )
        distances = [s.distance for s in profile.samples]
        assert distances[0] == 0
        assert all(b > a for a, b in zip(distances, distances[1:]))
        # 0.08° of longitude at 40°N is about 6.8 km.
        assert 6500 < profile.length < 7000
        assert profile.spacing == profile.length / 8

    def test_values_follow_the_ramp(self, dem_index):
        """A north-south line reads the fine raster's row ramp in order."""
        profile = sample_line(
            dem_index, LAYERS, (-104.92, 40.099), (-104.92, 40.001), samples=20
        )
        values = [s.value for s in profile.samples]
        assert all(v is not None for v in values)
        assert values == sorted(values)
        assert values[0] < values[-1]

    def test_a_coarse_profile_never_opens_the_fine_tiles(self, dem_index):
        profile = sample_line(
            dem_index, LAYERS, (-105.45, 39.55), (-104.55, 40.45), samples=20
        )
        assert profile.spacing > 4000
        assert [a.slug for a in profile.assets] == ["coarse"]
        assert {s.source.slug for s in profile.samples} == {"coarse"}

    def test_a_fine_profile_uses_both(self, dem_index):
        profile = sample_line(
            dem_index, LAYERS, (-104.95, 40.05), (-104.91, 40.05), samples=100
        )
        assert [a.slug for a in profile.assets] == ["fine", "coarse"]
        assert {s.source.slug for s in profile.samples} == {"fine"}

    def test_off_coverage_samples_are_none(self, dem_index):
        profile = sample_line(
            dem_index, LAYERS, (-104.6, 40.0), (-104.4, 40.0), samples=5
        )
        values = [s.value for s in profile.samples]
        assert values[0] is not None and values[-1] is None

    def test_nothing_non_finite_leaks(self, dem_index):
        profile = sample_line(
            dem_index, LAYERS, (-105.5, 39.5), (-104.5, 40.5), samples=101
        )
        for sample in profile.samples:
            assert sample.value is None or math.isfinite(sample.value)


class TestBackendOverride:
    """The mosaic backend applies the per-raster override when it opens a file."""

    def test_point_through_the_mosaic_masks_the_sea(self, dem_index):
        mosaic = PGRasterMosaic(LAYERS, index=dem_index)
        results = dict(mosaic.point(*SEA_POINT))
        fine = next(pt for href, pt in results.items() if href.endswith("fine.tif"))
        assert bool(fine.array.mask[0]) is True
        coarse = next(pt for href, pt in results.items() if href.endswith("coarse.tif"))
        assert not fine.array.mask.all() or coarse.array[0] < 0

    def test_land_is_not_masked(self, dem_index):
        mosaic = PGRasterMosaic(LAYERS, index=dem_index)
        results = dict(mosaic.point(*LAND_POINT))
        fine = next(pt for href, pt in results.items() if href.endswith("fine.tif"))
        assert bool(fine.array.mask[0]) is False

    def test_point_lookup_is_exact(self, dem_index):
        """Just outside the fine raster, but inside the tile that used to be used."""
        mosaic = PGRasterMosaic(["land"], index=dem_index)
        assert mosaic.assets_for_point(-104.8999, 40.05) == []


class TestResolutionParam:
    def test_resolution_becomes_a_target_zoom(self):
        params = DatasetParams(rasters=None, target_zoom=500.0)
        assert params.target_zoom == 8
        assert params.as_dict() == {"target_zoom": 8}

    def test_absent_means_absent(self):
        assert DatasetParams().as_dict() == {}
