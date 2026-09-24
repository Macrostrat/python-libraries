"""Sampling values from indexed rasters: at a point, or along a line.

The primitive behind an elevation service. It is generic raster work — nothing
here knows what the numbers mean — which is why it lives beside the mosaic
backend rather than in the application that turns it into an API.

Two decisions shape it, both measured before they were made:

- **A line is not N point reads.** One decimated window per intersecting raster,
  sized to the requested resolution so GDAL serves it from the overviews, was
  3× faster cold and three orders of magnitude faster warm than reading each
  sample as a point. Samples are then picked out of the window in memory.
- **Scale enters selection, not just reading.** The window's resolution is
  implied by `length / samples`, and that same figure is handed to the index as
  a target zoom — so a coarse continental profile is answered from the coarse
  global dataset and never opens the fine tiles it crosses. Every asset that is
  opened was chosen on purpose.

Validity is decided by the mask, never by comparing values: a NaN or a sentinel
must never reach a caller as a number, and a masked pixel falls through to the
next raster in order.
"""

import math
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

import numpy
from rasterio.transform import rowcol
from rio_tiler.constants import WGS84_CRS
from rio_tiler.errors import PointOutsideBounds, TileOutsideBounds
from rio_tiler.io import Reader

from macrostrat.raster_index import RasterAsset, RasterIndex, zoom_for_resolution
from macrostrat.utils import get_logger

log = get_logger(__name__)

__all__ = ["Sample", "Profile", "sample_point", "sample_line"]

# Mean Earth radius, as PostGIS's `ST_DistanceSphere` uses it.
EARTH_RADIUS = 6_371_008.8
# Metres per degree, for turning a resolution into a pixel size in EPSG:4326.
METRES_PER_DEGREE_LAT = 110_540.0
METRES_PER_DEGREE_LNG = 111_320.0

# Longest edge of one window read. Past this a window is read coarser than
# asked rather than larger, since the point of a window is that it is small.
MAX_WINDOW = 2048
# Consecutive samples per window. Samples are spaced about one target pixel
# apart, so this bounds the window to roughly as many pixels.
CHUNK_POINTS = 512


@dataclass
class Sample:
    """One sampled location: the value, and the raster that supplied it."""

    lng: float
    lat: float
    value: Optional[float]
    source: Optional[RasterAsset]
    # Metres along the line from its start; 0 for a point.
    distance: float = 0.0


@dataclass
class Profile:
    """A line of samples, with the scale it was answered at."""

    samples: list[Sample]
    # Great-circle length of the line, in metres.
    length: float
    # Metres between consecutive samples.
    spacing: float
    # The ground sample distance the reads were made at, in metres.
    resolution: Optional[float]
    # The zoom that resolution was translated to for selection.
    zoom: Optional[int]
    # Every raster the index selected for the line, in read order.
    assets: list[RasterAsset] = field(default_factory=list)


def sample_point(
    index: RasterIndex,
    layers: list[str],
    lng: float,
    lat: float,
    *,
    resolution: Optional[float] = None,
    rasters: Optional[list[str]] = None,
    zoom_tolerance: int = 3,
    reader: type = Reader,
    reader_options: Optional[dict[str, Any]] = None,
) -> Sample:
    """The first valid value at a point, from the rasters the index ranks first.

    `resolution` (metres) turns the scale window on: the index then prefers the
    raster closest to that ground sample distance. Without it, the finest
    raster covering the point is read first — the right default for a click.
    """
    zoom = None if resolution is None else zoom_for_resolution(resolution, latitude=lat)
    assets = index.assets_for_point(
        lng,
        lat,
        layers,
        rasters=rasters,
        zoom=zoom,
        zoom_tolerance=zoom_tolerance,
        scale_aware=zoom is not None,
    )
    for asset in assets:
        with _open(reader, asset, reader_options) as src:
            try:
                point = src.point(lng, lat, coord_crs=WGS84_CRS)
            except PointOutsideBounds:
                continue
        value = _valid(point.array[0])
        if value is not None:
            return Sample(lng, lat, value, asset)
    return Sample(lng, lat, None, None)


def sample_line(
    index: RasterIndex,
    layers: list[str],
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    samples: int = 201,
    resolution: Optional[float] = None,
    rasters: Optional[list[str]] = None,
    zoom_tolerance: int = 3,
    reader: type = Reader,
    reader_options: Optional[dict[str, Any]] = None,
    resampling: str = "nearest",
) -> Profile:
    """Values at `samples` evenly spaced points from `start` to `end`.

    Points are interpolated linearly in longitude and latitude — the segment as
    drawn on a plate carrée map — and distances are great-circle, from `start`.
    The line is not split at the antimeridian.

    `resolution` defaults to the sample spacing, which is what makes the reads
    come off the overviews and the selection prefer a dataset that resolves the
    spacing. Asking for a finer resolution than the spacing costs larger windows
    for no extra samples.
    """
    if samples < 2:
        raise ValueError("A profile needs at least two samples")
    (x0, y0), (x1, y1) = start, end
    lngs = numpy.linspace(x0, x1, samples)
    lats = numpy.linspace(y0, y1, samples)
    distances = [_haversine(x0, y0, float(x), float(y)) for x, y in zip(lngs, lats)]
    length = distances[-1]
    spacing = length / (samples - 1)

    if resolution is None and spacing > 0:
        resolution = spacing
    mid_lat = (y0 + y1) / 2
    zoom = (
        None
        if resolution is None
        else zoom_for_resolution(resolution, latitude=mid_lat)
    )

    line = {"type": "LineString", "coordinates": [[x0, y0], [x1, y1]]}
    assets = index.assets_for_geometry(
        line,
        layers,
        rasters=rasters,
        zoom=zoom,
        zoom_tolerance=zoom_tolerance,
        scale_aware=zoom is not None,
    )

    values: list[Optional[float]] = [None] * samples
    sources: list[Optional[RasterAsset]] = [None] * samples

    # One target pixel, in degrees, at the line's latitude. A zero-length line
    # has no spacing; it is sampled as a point, once.
    if resolution is None:
        for i in range(samples):
            found = sample_point(
                index,
                layers,
                float(lngs[i]),
                float(lats[i]),
                rasters=rasters,
                zoom_tolerance=zoom_tolerance,
                reader=reader,
                reader_options=reader_options,
            )
            values[i], sources[i] = found.value, found.source
        return Profile(
            _samples(lngs, lats, distances, values, sources),
            length,
            spacing,
            None,
            None,
            assets,
        )

    dx = resolution / (
        METRES_PER_DEGREE_LNG * max(math.cos(math.radians(mid_lat)), 0.01)
    )
    dy = resolution / METRES_PER_DEGREE_LAT

    for asset in assets:
        pending = [
            i
            for i in range(samples)
            if values[i] is None
            and _inside(asset.bounds, float(lngs[i]), float(lats[i]))
        ]
        if not pending:
            # Every point this raster could answer already has a value from a
            # raster ranked above it — and the file is never opened.
            continue

        with _open(reader, asset, reader_options) as src:
            if asset.bounds is None:
                bounds = src.get_geographic_bounds(WGS84_CRS)
                pending = [
                    i
                    for i in pending
                    if _inside(bounds, float(lngs[i]), float(lats[i]))
                ]
            for chunk in _chunks(pending, CHUNK_POINTS):
                xs = lngs[chunk]
                ys = lats[chunk]
                west, east = float(xs.min()) - dx, float(xs.max()) + dx
                south, north = float(ys.min()) - dy, float(ys.max()) + dy
                width = int(min(MAX_WINDOW, max(2, math.ceil((east - west) / dx))))
                height = int(min(MAX_WINDOW, max(2, math.ceil((north - south) / dy))))
                try:
                    img = src.part(
                        (west, south, east, north),
                        bounds_crs=WGS84_CRS,
                        dst_crs=WGS84_CRS,
                        width=width,
                        height=height,
                        resampling_method=resampling,
                    )
                except TileOutsideBounds:
                    continue
                band = img.array[0]
                rows, cols = rowcol(img.transform, xs, ys)
                for i, row, col in zip(chunk, rows, cols):
                    row = min(max(int(row), 0), band.shape[0] - 1)
                    col = min(max(int(col), 0), band.shape[1] - 1)
                    value = _valid(band[row, col])
                    if value is not None:
                        values[i] = value
                        sources[i] = asset

    return Profile(
        _samples(lngs, lats, distances, values, sources),
        length,
        spacing,
        resolution,
        zoom,
        assets,
    )


# -- helpers -------------------------------------------------------------------


@contextmanager
def _open(reader: type, asset: RasterAsset, reader_options: Optional[dict]) -> Iterator:
    """Open an asset with its nodata override, if the index holds one."""
    kwargs = dict(reader_options or {})
    if asset.nodata is not None:
        options = dict(kwargs.get("options") or {})
        options["nodata"] = asset.nodata
        kwargs["options"] = options
    with reader(asset.href, **kwargs) as src:
        yield src


def _valid(pixel: Any) -> Optional[float]:
    """A pixel as a float, or None if it is masked or not a finite number."""
    if numpy.ma.is_masked(pixel):
        return None
    value = float(pixel)
    if not math.isfinite(value):
        return None
    return value


def _inside(
    bounds: Optional[tuple[float, float, float, float]], x: float, y: float
) -> bool:
    if bounds is None:
        # Unknown bounds (a row indexed before they were stored): try it.
        return True
    west, south, east, north = bounds
    return west <= x <= east and south <= y <= north


def _chunks(indices: list[int], size: int) -> Iterator[list[int]]:
    """Runs of consecutive indices, each at most `size` long."""
    run: list[int] = []
    for i in indices:
        if run and (i != run[-1] + 1 or len(run) >= size):
            yield run
            run = []
        run.append(i)
    if run:
        yield run


def _haversine(x0: float, y0: float, x1: float, y1: float) -> float:
    """Great-circle distance in metres."""
    phi0, phi1 = math.radians(y0), math.radians(y1)
    dphi = phi1 - phi0
    dlam = math.radians(x1 - x0)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi0) * math.cos(phi1) * math.sin(dlam / 2) ** 2
    )
    return 2 * EARTH_RADIUS * math.asin(math.sqrt(a))


def _samples(lngs, lats, distances, values, sources) -> list[Sample]:
    return [
        Sample(float(x), float(y), v, s, d)
        for x, y, d, v, s in zip(lngs, lats, distances, values, sources)
    ]
