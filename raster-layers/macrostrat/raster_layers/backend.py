"""A mosaic reader whose assets come from the raster index.

rio-tiler 8 owns the mosaic backend contract (`rio_tiler.mosaic.backend`), so all
this class supplies is the three asset-lookup methods — "which rasters cover
this tile / point / bbox?" — and the answer is a spatial query rather than a
MosaicJSON document. Reading, compositing and pixel selection are inherited.

Assets are hrefs (plain strings), which is what the base class expects: it
passes them straight to the reader and reports them back as the asset list.

Two things ride along with the assets from the index and are applied here. A
per-raster **nodata override** (SRTM GL1 stores the ocean as 0) is handed to the
reader when that asset is opened, so water falls through to the layer beneath.
And the **scale window**: with `scale_aware` on and a target zoom in hand, the
index ranks rasters by closeness to the requested scale rather than finest-first.
"""

from typing import Any, Optional

import attr
from morecantile import TileMatrixSet
from rasterio.crs import CRS
from rasterio.warp import transform_bounds
from rio_tiler.constants import WEB_MERCATOR_TMS, WGS84_CRS
from rio_tiler.io import BaseReader, Reader
from rio_tiler.mosaic.backend import BaseBackend
from rio_tiler.types import BBox

from macrostrat.raster_index import RasterAsset, RasterIndex
from macrostrat.utils import get_logger

log = get_logger(__name__)

__all__ = ["PGRasterMosaic"]


@attr.s
class PGRasterMosaic(BaseBackend):
    """Composite every indexed raster covering the requested area.

    `input` is the list of layer slugs to composite, in priority order — the
    mosaic's identity is the layer list, not a path.
    """

    input: list[str] = attr.ib()
    index: Optional[RasterIndex] = attr.ib(default=None)

    tms: TileMatrixSet = attr.ib(default=WEB_MERCATOR_TMS)
    reader: type[BaseReader] = attr.ib(default=Reader)
    reader_options: dict = attr.ib(factory=dict)

    # Narrow the mosaic to specific raster slugs. This is how a single dataset is
    # viewed *through* the layer — same palette, class vocabulary, empty-tile
    # behavior and point queries as the full mosaic — rather than through a
    # separate single-file route with its own rendering rules. Request-scoped:
    # titiler's `backend_dependency` supplies it per request.
    rasters: Optional[list[str]] = attr.ib(default=None)

    # Forwarded to `raster_layers.get_rasters`: how far below a raster's own
    # minzoom it may still be read.
    zoom_tolerance: int = attr.ib(default=3)
    # Whether to keep serving data past its native resolution. On by default: a
    # layer that vanishes when you zoom in reads as a bug, and a magnified tile
    # is what every other raster service gives you. `should_generate_tile` in
    # the index is the right tool for deciding what to *cache*.
    allow_overscaled: bool = attr.ib(default=True)

    # Rank rasters by closeness to the requested scale rather than finest-first.
    # Off by default so existing layers are untouched; a continuous layer that
    # mixes a global product with fine local tiles wants it on, or a coarse
    # view opens every fine tile it touches. See `RasterIndex.assets_for_bbox`.
    scale_aware: bool = attr.ib(default=False)
    # The zoom a point or area request should be answered at — from
    # `?resolution=` on the route. Tiles carry their own zoom and ignore it.
    target_zoom: Optional[int] = attr.ib(default=None)

    # Assets resolved for this request, kept so the colormap that came back with
    # them can reach rendering. The backend is constructed per request by the
    # route, so this is request-scoped state, not shared.
    resolved_assets: list = attr.ib(init=False, factory=list)
    # href -> nodata override for the assets resolved on this request.
    _overrides: dict = attr.ib(init=False, factory=dict)
    # The reader class as configured; `reader` itself becomes the wrapper.
    _open: Any = attr.ib(init=False, default=None)

    bounds: BBox = attr.ib(init=False, default=(-180, -90, 180, 90))
    crs: CRS = attr.ib(init=False, default=WGS84_CRS)
    minzoom: int = attr.ib(init=False, default=None)
    maxzoom: int = attr.ib(init=False, default=None)

    def __attrs_post_init__(self):
        if self.index is None:
            raise ValueError("PGRasterMosaic requires a RasterIndex")
        if isinstance(self.input, str):
            self.input = [self.input]

        # The tile grid's range is only a fallback. A mosaic that claims data
        # from zoom 0 to 24 misleads every client that reads `/info`,
        # `tilejson.json` or the WMTS capabilities — and in WMTS it is expensive,
        # since each advertised layer carries one `TileMatrixLimits` block per
        # zoom level it claims.
        self.minzoom = self.tms.minzoom
        self.maxzoom = self.tms.maxzoom

        # Bounds and zoom range in one query: this runs per request, on every
        # route, so it is not a place to spend a second round trip.
        extent = self.index.layer_extent(self.input, rasters=self.rasters)
        if extent is not None:
            self.bounds = extent.bounds
            if extent.minzoom is not None:
                self.minzoom = extent.minzoom
            if extent.maxzoom is not None:
                self.maxzoom = extent.maxzoom

        # The base class opens every asset as `self.reader(asset, **options)`,
        # with one set of options for the whole mosaic. A nodata override is per
        # raster, so the reader is wrapped to look each asset up as it is opened.
        self._open = self.reader
        self.reader = self._open_asset

    def _open_asset(self, asset: str, **kwargs: Any):
        nodata = self._overrides.get(asset)
        if nodata is not None:
            options = dict(kwargs.get("options") or {})
            # More specific than a mosaic-wide default, so it wins; a request's
            # own `?nodata=` still arrives on the read call and wins over both.
            options["nodata"] = nodata
            kwargs["options"] = options
        return self._open(asset, **kwargs)

    def _remember(self, assets: list[RasterAsset]) -> list[str]:
        """Keep what the index said about this request's assets; return hrefs."""
        self.resolved_assets = assets
        self._overrides = {a.href: a.nodata for a in assets if a.nodata is not None}
        return [a.href for a in assets]

    # -- Asset lookup ------------------------------------------------------
    #
    # Each returns hrefs, and an empty list where there's no coverage. The base
    # class turns "empty" into `NoAssetFoundError` *before* opening any reader,
    # so a miss costs one indexed query and nothing more.

    def assets_for_tile(self, x: int, y: int, z: int, **kwargs: Any) -> list[str]:
        assets = self.index.assets_for_tile(
            x,
            y,
            z,
            self.input,
            zoom_tolerance=self.zoom_tolerance,
            rasters=self.rasters,
            scale_aware=self.scale_aware,
        )
        if not self.allow_overscaled:
            assets = [a for a in assets if not a.overscaled]
        return self._remember(assets)

    @property
    def colormap(self) -> Optional[dict]:
        """The colormap for the assets resolved on this request, if any.

        First asset that declares one wins, which is the same precedence
        compositing uses.
        """
        for asset in self.resolved_assets:
            if asset.colormap:
                return asset.colormap
        return None

    @property
    def categories(self) -> Optional[list]:
        """The class vocabulary for the assets resolved on this request, if any.

        Same precedence as the colormap, and it came back on the same query.
        """
        for asset in self.resolved_assets:
            if asset.categories:
                return [c.model_dump(mode="json") for c in asset.categories]
        return None

    def tile(self, *args: Any, **kwargs: Any):
        """Read a tile, tagging it with what its assets said about themselves.

        Rendering (and any post-processing) happens back in the route, after the
        reader is closed, so the colormap and class vocabulary ride along on the
        image rather than being looked up again. This is what lets a request that
        filters by class name still cost one database query.
        """
        image, assets = super().tile(*args, **kwargs)
        metadata = {}
        colormap = self.colormap
        if colormap is not None:
            metadata["colormap"] = colormap
        categories = self.categories
        if categories is not None:
            metadata["categories"] = categories
        if metadata:
            image.metadata = {**(image.metadata or {}), **metadata}
        return image, assets

    def assets_for_point(
        self,
        lng: float,
        lat: float,
        coord_crs: Optional[CRS] = None,
        **kwargs: Any,
    ) -> list[str]:
        if coord_crs is not None and coord_crs != WGS84_CRS:
            lng, lat, _, _ = transform_bounds(coord_crs, WGS84_CRS, lng, lat, lng, lat)
        # An exact test against the footprint, not the tile around the point:
        # once footprints follow coastlines, a click in the water must not open
        # the land tile whose bounding box covers it.
        assets = self.index.assets_for_point(
            lng,
            lat,
            self.input,
            rasters=self.rasters,
            zoom=self.target_zoom,
            zoom_tolerance=self.zoom_tolerance,
            scale_aware=self.scale_aware,
        )
        if not self.allow_overscaled:
            assets = [a for a in assets if not a.overscaled]
        return self._remember(assets)

    def assets_for_bbox(
        self,
        xmin: float,
        ymin: float,
        xmax: float,
        ymax: float,
        coord_crs: Optional[CRS] = None,
        **kwargs: Any,
    ) -> list[str]:
        if coord_crs is not None and coord_crs != WGS84_CRS:
            xmin, ymin, xmax, ymax = transform_bounds(
                coord_crs, WGS84_CRS, xmin, ymin, xmax, ymax
            )
        assets = self.index.assets_for_bbox(
            xmin,
            ymin,
            xmax,
            ymax,
            self.input,
            rasters=self.rasters,
            zoom=self.target_zoom,
            zoom_tolerance=self.zoom_tolerance,
            scale_aware=self.scale_aware,
        )
        if not self.allow_overscaled:
            assets = [a for a in assets if not a.overscaled]
        return self._remember(assets)
