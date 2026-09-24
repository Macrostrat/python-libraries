"""Ground sample distance <-> zoom, the one conversion every scale decision uses."""

from pytest import approx, raises

from macrostrat.raster_index import resolution_for_zoom, zoom_for_resolution


def test_known_products_land_on_expected_zooms():
    assert zoom_for_resolution(30) == 12  # SRTM GL1, 1 arcsec
    assert zoom_for_resolution(500) == 8  # SRTM15+, 15 arcsec
    assert zoom_for_resolution(30_000) == 2  # a 6000 km profile at 201 samples
    assert zoom_for_resolution(1) == 17  # 1 m lidar


def test_round_trip():
    for zoom in range(0, 20):
        assert zoom_for_resolution(resolution_for_zoom(zoom)) == zoom


def test_latitude_shrinks_pixels():
    assert resolution_for_zoom(10, latitude=60) == approx(
        resolution_for_zoom(10) / 2, rel=1e-6
    )


def test_clamped_to_the_grid():
    assert zoom_for_resolution(1e9) == 0
    assert zoom_for_resolution(1e-9) == 24


def test_rejects_nonpositive():
    with raises(ValueError):
        zoom_for_resolution(0)
