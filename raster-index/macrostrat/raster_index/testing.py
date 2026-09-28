"""Helpers for building bite-sized test rasters.

Test rasters are *generated*, not committed: a few hundred pixels of synthetic
categorical data is enough to exercise footprints, asset selection, compositing
and rendering, and it keeps binary fixtures out of the repository. Shared here
rather than in a test file so `macrostrat.raster_layers` — and Macrostrat's own
integration tests — can use the same fixtures.
"""

from pathlib import Path
from typing import Optional

import numpy as np
import rasterio
from rasterio.transform import from_bounds

__all__ = [
    "create_test_raster",
    "create_test_rasters",
    "create_test_dem",
    "CATEGORICAL_COLORMAP",
    "CATEGORICAL_CLASSES",
    "CLASS_METADATA_KEY",
    "FINE_BOUNDS",
    "COARSE_BOUNDS",
    "ELSEWHERE_BOUNDS",
]

# The standard arrangement: two overlapping rasters at different resolutions —
# what asset selection actually has to reason about — plus one far enough away
# that no tile over the others can touch it.
FINE_BOUNDS = (-105.0, 40.0, -104.9, 40.1)
COARSE_BOUNDS = (-105.05, 39.95, -104.8, 40.2)
ELSEWHERE_BOUNDS = (10.0, 10.0, 10.2, 10.2)

# A tiny paletted colormap, in the shape of the EMIT mineral-map palettes:
# value 0 is transparent nodata, the rest are opaque classes.
CATEGORICAL_COLORMAP = {
    0: (0, 0, 0, 0),
    1: (150, 150, 150, 255),
    2: (218, 50, 132, 255),
    3: (80, 164, 50, 255),
}


# Class names for the values in `CATEGORICAL_COLORMAP`, written into band
# metadata the way the EMIT mineral maps write theirs: a Python-literal dict
# under a producer-chosen key. Value 0 is nodata and so is deliberately unnamed.
CATEGORICAL_CLASSES = {
    1: "Kaolinite",
    2: "Alunite",
    3: "Chlorite",
}

# The band-metadata item holding the class names. Matches what the EMIT rasters
# use, since that is the case the ingest path has to handle.
CLASS_METADATA_KEY = "MINERAL_CLASSES"


def create_test_raster(
    path: Path,
    bounds: tuple[float, float, float, float],
    *,
    size: int = 256,
    values: Optional[np.ndarray] = None,
    colormap: Optional[dict] = CATEGORICAL_COLORMAP,
    classes: Optional[dict] = CATEGORICAL_CLASSES,
    nodata: Optional[float] = 0,
    crs: str = "EPSG:4326",
) -> Path:
    """Write a single-band Byte raster covering `bounds`.

    Data is banded across the image so a composited tile visibly differs from
    either source, and the border row/column is left as nodata so masking is
    exercised too.

    `classes` is written as band metadata, so the fixtures exercise deriving a
    class vocabulary from a real GDAL header rather than from a hand-built dict.
    """
    west, south, east, north = bounds
    if values is None:
        values = np.zeros((size, size), dtype="uint8")
        # Three horizontal bands of distinct classes, inset from the edges.
        inset = max(1, size // 32)
        band_height = (size - 2 * inset) // 3
        for i in range(3):
            start = inset + i * band_height
            values[start : start + band_height, inset : size - inset] = i + 1

    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "count": 1,
        "height": size,
        "width": size,
        "crs": crs,
        "transform": from_bounds(west, south, east, north, size, size),
        "tiled": True,
        "blockxsize": 128,
        "blockysize": 128,
        "compress": "deflate",
    }
    if nodata is not None:
        profile["nodata"] = nodata

    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values, 1)
        if colormap is not None:
            dst.write_colormap(1, {k: v for k, v in colormap.items()})
        if classes is not None:
            # Stringified exactly as GDAL stores it, so parsing is tested too.
            dst.update_tags(1, **{CLASS_METADATA_KEY: str(classes)})
        dst.build_overviews([2, 4])

    return path


def create_test_rasters(directory: Path) -> dict[str, Path]:
    """The standard three test rasters, keyed by slug."""
    return {
        "fine": create_test_raster(directory / "fine.tif", FINE_BOUNDS, size=512),
        "coarse": create_test_raster(directory / "coarse.tif", COARSE_BOUNDS, size=128),
        "elsewhere": create_test_raster(
            directory / "elsewhere.tif", ELSEWHERE_BOUNDS, size=128
        ),
    }


def create_test_dem(
    path: Path,
    bounds: tuple[float, float, float, float],
    *,
    size: int = 128,
    dtype: str = "int16",
    nodata: Optional[float] = -32768,
    base: float = 100.0,
    step: float = 1.0,
    ocean: Optional[tuple[float, float, float, float]] = None,
    ocean_value: float = 0.0,
    overviews: tuple[int, ...] = (2, 4),
) -> Path:
    """Write a single-band *continuous* raster: a north-to-south ramp.

    The elevation fixture. Value at row `r` is `base + step * r`, so a sample's
    expected value follows from its latitude alone. `ocean` marks a rectangle
    (west, south, east, north) that is filled with `ocean_value` instead — SRTM
    GL1 stores the sea as 0 rather than nodata, and that is the case a nodata
    override exists for. With a float dtype `ocean_value` may be NaN, which is
    how SRTM15+ marks what it does not cover.
    """
    west, south, east, north = bounds
    rows = np.arange(size, dtype="float64")[:, None]
    values = np.broadcast_to(base + step * rows, (size, size)).astype("float64")
    values = values.copy()

    if ocean is not None:
        ow, os_, oe, on = ocean
        # Pixel indices of the ocean rectangle. Row 0 is the northern edge.
        col0 = int(round((ow - west) / (east - west) * size))
        col1 = int(round((oe - west) / (east - west) * size))
        row0 = int(round((north - on) / (north - south) * size))
        row1 = int(round((north - os_) / (north - south) * size))
        values[max(0, row0) : min(size, row1), max(0, col0) : min(size, col1)] = (
            ocean_value
        )

    profile = {
        "driver": "GTiff",
        "dtype": dtype,
        "count": 1,
        "height": size,
        "width": size,
        "crs": "EPSG:4326",
        "transform": from_bounds(west, south, east, north, size, size),
        "tiled": True,
        "blockxsize": 64,
        "blockysize": 64,
        "compress": "deflate",
    }
    if nodata is not None:
        profile["nodata"] = nodata

    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values.astype(dtype), 1)
        if overviews:
            dst.build_overviews(list(overviews))
    return path
