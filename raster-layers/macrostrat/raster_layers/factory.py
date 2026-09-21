"""FastAPI routes for serving indexed raster layers.

`RasterMosaicFactory` is titiler's `MosaicTilerFactory` with the index bound in
as its backend. Since rio-tiler 8 moved the mosaic backend contract into
rio-tiler itself, every route titiler registers — tiles, tilejson, info, point,
assets — works against an index-backed mosaic unchanged. What's added here is
the layer's own default colormap and a footprints layer.
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, Optional

from attrs import define, field
from fastapi import Depends, Path, Query
from starlette.responses import JSONResponse, Response
from titiler.core.algorithm import Algorithms
from titiler.core.dependencies import DefaultDependency
from titiler.core.utils import render_image
from titiler.mosaic.factory import MosaicTilerFactory
from typing_extensions import Annotated

from macrostrat.raster_index import RasterIndex, zoom_for_resolution
from macrostrat.utils import get_logger

from .algorithms import categorical_algorithms
from .backend import PGRasterMosaic

log = get_logger(__name__)

__all__ = [
    "RasterMosaicFactory",
    "fixed_layers",
    "LayerListParams",
    "DatasetParams",
    "MVT_MEDIA_TYPE",
]

MVT_MEDIA_TYPE = "application/vnd.mapbox-vector-tile"


def fixed_layers(*slugs: str) -> Callable[[], list[str]]:
    """A path dependency serving a fixed set of layers.

    The usual case: a route that *is* a layer, with the layer list baked in
    rather than accepted from the client.
    """

    def dependency() -> list[str]:
        return list(slugs)

    return dependency


@dataclass
class DatasetParams(DefaultDependency):
    """Narrow a mosaic to specific rasters, by slug.

    Flowed in through titiler's `backend_dependency`, which is splatted into the
    backend constructor on *every* route — so `?datasets=` applies uniformly to
    tiles, `/point`, `/info` and `/assets` without each route knowing about it.

    `DefaultDependency.as_dict()` drops `None`, so an absent parameter leaves the
    backend at its default of "every raster in the layer".

    `?resolution=` — a target ground sample distance in metres — arrives the same
    way and becomes the backend's `target_zoom`, so `/point` and `/part` on a
    scale-aware layer can be answered at the scale the client is working at.
    """

    rasters: Annotated[
        Optional[str],
        Query(
            alias="datasets",
            description=(
                "Comma-separated raster slugs to restrict the mosaic to — one "
                "dataset viewed through the layer, keeping the layer's palette "
                "and class vocabulary. Slugs come from `/footprints`."
            ),
        ),
    ] = None

    target_zoom: Annotated[
        Optional[float],
        Query(
            alias="resolution",
            gt=0,
            description=(
                "Target ground sample distance in metres. On a scale-aware layer "
                "this selects the rasters closest to that scale rather than the "
                "finest available; a coarse request reads a coarse dataset."
            ),
        ),
    ] = None

    def __post_init__(self):
        """Parse the query forms into what the backend wants."""
        if isinstance(self.rasters, str):
            slugs = [slug.strip() for slug in self.rasters.split(",") if slug.strip()]
            self.rasters = slugs or None
        if self.target_zoom is not None:
            # Converted at the equator: a point or bbox route has no single
            # latitude to hand, and a one-level error is within the window's
            # tolerance.
            self.target_zoom = zoom_for_resolution(float(self.target_zoom))


def LayerListParams(
    layers: Annotated[
        str, Query(description="Comma-delimited raster layers to composite")
    ] = "",
) -> list[str]:
    """A path dependency letting the client choose and order layers."""
    return [slug for slug in layers.split(",") if slug]


@define(kw_only=True)
class RasterMosaicFactory(MosaicTilerFactory):
    """Tile routes for one or more layers in a raster index."""

    index: Optional[RasterIndex] = field(default=None)

    # Required by the parent, but we always supply it ourselves in
    # `__attrs_post_init__` — the backend has to carry the index.
    backend: Any = field(default=None)

    # Colormap used when the request doesn't specify one. Falls back to the
    # layer's own colormap in the index (categorical rasters are unreadable
    # without it, and clients shouldn't have to know a 40-entry palette).
    default_colormap: Optional[dict] = field(default=None)
    use_index_colormap: bool = field(default=True)

    # Forwarded to `PGRasterMosaic` (e.g. `allow_overscaled`, `zoom_tolerance`).
    backend_options: dict = field(factory=dict)

    # Per-request backend arguments. Defaults to `?datasets=` support; a host
    # application can substitute its own.
    backend_dependency: Any = field(default=DatasetParams)

    # The viewer template pulls in remote assets; not useful for these layers.
    add_viewer: bool = field(default=False)

    # Whether these routes advertise the categorical class filter
    # (`?algorithm=classes`). On by default — a continuous layer can turn it off
    # rather than offering a filter that can never match.
    class_filtering: bool = field(default=True)
    # An explicit algorithm registry, if a host application wants to add its own.
    algorithms: Optional[Algorithms] = field(default=None)

    def __attrs_post_init__(self):
        if self.index is None:
            raise ValueError("RasterMosaicFactory requires a RasterIndex")
        if self.algorithms is not None:
            self.process_dependency = self.algorithms.dependency
        elif self.class_filtering:
            self.process_dependency = categorical_algorithms().dependency
        # titiler constructs the backend itself, and `backend_dependency` can't
        # carry a non-request-scoped object, so bind the index with a partial.
        self.backend = partial(PGRasterMosaic, index=self.index, **self.backend_options)
        self.render_func = self._render_func()
        super().__attrs_post_init__()

    def register_routes(self):
        super().register_routes()
        self.layer_metadata()
        self.footprints()

    def layer_metadata(self):
        """What this layer is: its palette and its class vocabulary.

        The counterpart to filtering by class name — a client needs the names
        before it can ask for one. Serving them from the index means a client
        needs no reference raster and no GDAL metadata parsing to draw a legend,
        and the request reads no pixels.

        A route may composite several indexed layers, so `layers` reports all of
        them while `colormap` and `categories` resolve the way rendering does:
        the first layer that declares one wins.
        """

        @self.router.get(
            "/layer",
            responses={200: {"description": "The layer's definition and vocabulary"}},
        )
        def layer_metadata(src_path=Depends(self.path_dependency)):
            slugs = list(src_path)
            definitions = [d for d in (self.index.layer(s) for s in slugs) if d]
            if not definitions:
                return JSONResponse(
                    {"detail": f"No indexed layer named {', '.join(slugs)}"},
                    status_code=404,
                )

            primary = definitions[0]
            colormap = _first(d.colormap for d in definitions)
            categories = _first(d.categories for d in definitions)
            return JSONResponse(
                {
                    "slug": primary.slug,
                    "name": primary.name,
                    "description": primary.description,
                    "layers": slugs,
                    "minzoom": primary.minzoom,
                    "maxzoom": primary.maxzoom,
                    "colormap": _json_colormap(colormap),
                    "categories": [
                        c.model_dump(mode="json") for c in (categories or [])
                    ],
                }
            )

    def footprints(self):
        """Coverage of these layers, without reading any pixels."""

        @self.router.get(
            "/footprints/{z}/{x}/{y}",
            responses={
                200: {
                    "content": {MVT_MEDIA_TYPE: {}},
                    "description": "Raster footprints as a vector tile",
                }
            },
            response_class=Response,
        )
        def footprint_tile(
            z: Annotated[int, Path(description="Tile zoom level")],
            x: Annotated[int, Path(description="Tile column")],
            y: Annotated[int, Path(description="Tile row")],
            src_path=Depends(self.path_dependency),
            backend_params=Depends(self.backend_dependency),
        ):
            """Raster footprints as a vector tile (layer `raster_footprints`)."""
            data = self.index.footprint_tile(
                x, y, z, src_path, **backend_params.as_dict()
            )
            return Response(data, media_type=MVT_MEDIA_TYPE)

        @self.router.get(
            "/footprints",
            responses={200: {"description": "Raster footprints as GeoJSON"}},
        )
        def footprints(
            src_path=Depends(self.path_dependency),
            backend_params=Depends(self.backend_dependency),
        ):
            """Every footprint in these layers, as a GeoJSON FeatureCollection."""
            return JSONResponse(
                self.index.footprints(src_path, **backend_params.as_dict())
            )

    # -- Colormap resolution -----------------------------------------------

    def _render_func(self) -> Callable[..., tuple[bytes, str]]:
        """Render, defaulting the colormap to the one the assets carried.

        The colormap arrives on the image itself (see `PGRasterMosaic.tile`),
        which is what lets a tile cost a single database query: the same lookup
        that decides *which* rasters to read also says how to draw them. A
        request that sends its own `colormap`/`colormap_name` still wins, since
        this only fills in when titiler resolved none.
        """
        default = self.default_colormap
        use_index = self.use_index_colormap

        def render(image, colormap=None, **kwargs):
            if colormap is None and default is not None:
                colormap = default
            elif colormap is None and use_index:
                carried = (image.metadata or {}).get("colormap")
                if carried:
                    colormap = normalize_colormap(carried)
            return render_image(image, colormap=colormap, **kwargs)

        return render


def _first(values):
    """The first truthy value, or None — the precedence rendering already uses."""
    for value in values:
        if value:
            return value
    return None


def _json_colormap(colormap: Optional[dict]) -> Optional[dict]:
    """A colormap in JSON form: string keys, list colors, alpha filled in."""
    if not colormap:
        return None
    return {str(k): list(v) for k, v in normalize_colormap(colormap).items()}


def normalize_colormap(colormap: dict) -> dict:
    """Coerce a stored colormap into rio-tiler's `{int: (r, g, b, a)}` form.

    JSON object keys are strings, and colormaps captured from a GDAL color table
    may be RGB triples rather than RGBA.
    """
    result = {}
    for key, value in colormap.items():
        color = list(value)
        if len(color) == 3:
            color.append(255)
        result[int(key)] = tuple(color[:4])
    return result
