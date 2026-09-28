"""Serve indexed cloud-optimized rasters as mosaicked tile layers.

Reads assets from a `macrostrat.raster_index` database and composites them into
tiles, as FastAPI routes that can be mounted into any application.
"""

from .algorithms import CLASS_FILTER_NAME, ClassFilter, categorical_algorithms
from .backend import PGRasterMosaic
from .factory import MVT_MEDIA_TYPE, LayerListParams, RasterMosaicFactory, fixed_layers
from .layers import (
    RasterLayerConfig,
    install_exception_handlers,
    register_raster_layers,
)
from .sampling import Profile, Sample, sample_line, sample_point

__all__ = [
    "PGRasterMosaic",
    "ClassFilter",
    "categorical_algorithms",
    "CLASS_FILTER_NAME",
    "RasterMosaicFactory",
    "RasterLayerConfig",
    "register_raster_layers",
    "install_exception_handlers",
    "fixed_layers",
    "LayerListParams",
    "MVT_MEDIA_TYPE",
    "Sample",
    "Profile",
    "sample_point",
    "sample_line",
]
