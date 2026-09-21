"""Ground sample distance and Web Mercator zoom, converted in one place.

The index describes every raster by a native zoom range, because that is the
vocabulary a tile route speaks. An elevation query speaks in metres: a target
ground sample distance, either requested outright or implied by a profile's
`length / samples`. These two functions are the whole of the translation, so
that no caller reinvents the constant.

At the equator one Web Mercator pixel at zoom `z` spans
`40 075 016.686 / (256 · 2^z)` metres; away from it, multiply by `cos(latitude)`.
"""

import math

__all__ = ["resolution_for_zoom", "zoom_for_resolution", "MAX_ZOOM"]

# WGS84 equatorial circumference, in metres.
EARTH_CIRCUMFERENCE = 40_075_016.686
TILE_SIZE = 256
# Where the conversion stops: the finest zoom the index ever ranks against.
MAX_ZOOM = 24


def resolution_for_zoom(zoom: float, *, latitude: float = 0.0) -> float:
    """Metres per pixel of a Web Mercator tile at `zoom`, at `latitude`."""
    return (
        EARTH_CIRCUMFERENCE * math.cos(math.radians(latitude)) / (TILE_SIZE * 2.0**zoom)
    )


def zoom_for_resolution(resolution: float, *, latitude: float = 0.0) -> int:
    """The zoom whose pixels are closest to `resolution` metres, at `latitude`.

    Rounded to the nearest level and clamped to `0..MAX_ZOOM`. 30 m (SRTM GL1)
    is zoom 12; 500 m (SRTM15+) is zoom 8; a 30 km sample spacing is zoom 2.
    """
    if resolution <= 0:
        raise ValueError("A ground sample distance must be positive")
    exact = math.log2(
        EARTH_CIRCUMFERENCE
        * math.cos(math.radians(latitude))
        / (TILE_SIZE * resolution)
    )
    return int(min(MAX_ZOOM, max(0, round(exact))))
