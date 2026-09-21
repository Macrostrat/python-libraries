"""Types shared between indexing and serving.

These are the contract between `macrostrat.raster_index` and
`macrostrat.raster_layers`: the serving side never touches the tables directly,
it consumes `RasterAsset`s.
"""

from typing import Any, Optional

from pydantic import BaseModel, Field

__all__ = [
    "RasterAsset",
    "RasterInfo",
    "DeclaredRaster",
    "LayerDefinition",
    "RasterCategory",
    "LayerExtent",
]


class RasterCategory(BaseModel):
    """One class in a categorical raster's vocabulary.

    Classification maps address their classes by integer, but people address
    them by name. This is the join between the two, resolved once at ingest
    (from GDAL band metadata and the color table) and stored on the layer, so
    neither the tile server nor a client has to re-derive it.
    """

    value: int
    label: str
    # From the raster's color table, where it has one. Carried alongside the
    # label so a client can draw a legend from a single request.
    color: Optional[tuple[int, int, int, int]] = None


class LayerExtent(BaseModel):
    """What a set of layers covers, and at what resolutions.

    Bounds and zoom range together because they are answered by one query, and a
    tile route asks for both on every request — see `RasterIndex.layer_extent`.
    """

    bounds: tuple[float, float, float, float]
    # Native zoom range across the selected rasters. `None` where no raster
    # records one, which is the caller's cue to fall back to the tile grid's.
    minzoom: Optional[int] = None
    maxzoom: Optional[int] = None


class RasterAsset(BaseModel):
    """A raster selected for a specific tile.

    Produced by `raster_layers.get_rasters`; consumed by the mosaic reader.
    """

    href: str
    layer: str
    slug: Optional[str] = None
    minzoom: Optional[int] = None
    maxzoom: Optional[int] = None
    rescale_range: Optional[list[float]] = None
    # Layer-level today, per-raster once leveling exists. Travels with the asset
    # so a tile read resolves both "which rasters" and "how to draw them".
    colormap: Optional[dict[str, Any]] = None
    # The layer's class vocabulary, for categorical rasters. Like `colormap`,
    # it travels with the asset so a tile read resolves both "which rasters" and
    # "what the values mean" in one query.
    categories: Optional[list[RasterCategory]] = None
    # A nodata value the reader must apply instead of the file's own — SRTM GL1
    # stores the ocean as 0, and only an override makes it fall through. None,
    # the common case, means the file is trusted and the reader stays on its
    # plain path. Set with `RasterIndex.set_nodata`.
    nodata: Optional[float] = None
    # (west, south, east, north) of the file, in EPSG:4326. Lets a caller that
    # reads several rasters skip one that cannot hold the points it still
    # needs, without opening it.
    bounds: Optional[tuple[float, float, float, float]] = None
    # True when the requested tile is zoomed in past what this raster resolves.
    overscaled: bool = False


class RasterInfo(BaseModel):
    """Metadata derived by opening a raster, before it is written to the index."""

    href: str
    # Bounding box in EPSG:4326, as (west, south, east, north).
    bounds: tuple[float, float, float, float]
    # Footprint as a GeoJSON geometry dict, in EPSG:4326.
    geometry: dict[str, Any]
    minzoom: int
    maxzoom: int
    dtype: str
    nbands: int
    nodata: Optional[float] = None
    crs: Optional[str] = None
    # Colormap embedded in the raster itself (a GDAL color table), if any.
    colormap: Optional[dict[int, tuple[int, int, int, int]]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeclaredRaster(BaseModel):
    """A raster described without being opened.

    For a standardized product — one 1° tile per file, identical dtype, nodata,
    CRS and resolution throughout — every field the index stores is known from
    the bucket listing and the product's documentation. Opening 14,000 files to
    learn nine identical facts costs hours; declaring them costs seconds. The
    declaration is *verified* against a sample afterwards
    (`RasterIndex.verify_sample`), never trusted outright.

    Everything a priori goes here: the bounds, the native zoom range, and — where
    something better than the bounding box is known — the footprint itself.
    """

    href: str
    slug: Optional[str] = None
    # (west, south, east, north) in EPSG:4326: what the file covers.
    bounds: tuple[float, float, float, float]
    minzoom: int
    maxzoom: int
    dtype: str
    nbands: int = 1
    # The nodata value the *file* declares. A reader override is applied
    # separately (`RasterIndex.set_nodata`), so that `verify_sample` can still
    # compare this against the file.
    nodata: Optional[float] = None
    crs: Optional[str] = "EPSG:4326"
    # A GeoJSON geometry, in EPSG:4326, standing in for the bounding box as the
    # selection footprint. Must lie within `bounds`.
    footprint: Optional[dict[str, Any]] = None
    # Anything the declaring pipeline wants recorded alongside — typically
    # where the declaration came from and when it was last verified.
    info: dict[str, Any] = Field(default_factory=dict)


class LayerDefinition(BaseModel):
    """A named mosaic, and the defaults its rasters inherit."""

    slug: str
    name: Optional[str] = None
    description: Optional[str] = None
    minzoom: Optional[int] = None
    maxzoom: Optional[int] = None
    rescale_range: Optional[list[float]] = None
    colormap: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None
    # Stored inside `metadata`, but modeled separately: the vocabulary is a
    # first-class part of what a categorical layer *is*, while `metadata` is the
    # jsonb column that happens to hold it (which is why this needed no schema
    # change). `RasterIndex` folds it in on write and lifts it back out on read.
    categories: Optional[list[RasterCategory]] = None
