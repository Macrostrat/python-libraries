"""An index of cloud-optimized rasters, for mosaicked tile serving.

Rasters live in object storage; this package records *where* they are, *what*
they cover, and *which named layer* they belong to, so a tile server can answer
"which files do I read for this tile?" with a single spatial query.

Serving is a separate concern, handled by `macrostrat.raster_layers`.
"""

from .categories import (
    categories_from_info,
    categories_from_json,
    categories_from_mapping,
    categories_from_qml,
    class_metadata_candidates,
)
from .declared import Mismatch, VerificationReport
from .defs import (
    DeclaredRaster,
    LayerDefinition,
    LayerExtent,
    RasterAsset,
    RasterCategory,
    RasterInfo,
)
from .external_footprints import FootprintReport, FootprintSource
from .footprints import get_raster_info
from .index import RasterIndex, schema_files
from .scale import resolution_for_zoom, zoom_for_resolution
from .scan import BucketPrefix, RasterObject, parse_bucket_url, scan_prefix

__all__ = [
    "RasterIndex",
    "schema_files",
    "get_raster_info",
    "scan_prefix",
    "RasterObject",
    "BucketPrefix",
    "parse_bucket_url",
    "RasterAsset",
    "RasterInfo",
    "DeclaredRaster",
    "FootprintSource",
    "FootprintReport",
    "Mismatch",
    "VerificationReport",
    "zoom_for_resolution",
    "resolution_for_zoom",
    "LayerDefinition",
    "LayerExtent",
    "RasterCategory",
    "categories_from_info",
    "categories_from_json",
    "categories_from_mapping",
    "categories_from_qml",
    "class_metadata_candidates",
]
