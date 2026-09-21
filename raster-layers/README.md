# `macrostrat.raster_layers`

Serve the layers in a [`macrostrat.raster_index`](https://github.com/UW-Macrostrat/python-libraries/tree/main/raster-index) database as
mosaicked map tiles, as FastAPI routes mountable in any application.

Given a tile, the index says which COGs cover it and in what order; this package
reads them, composites them, and renders the result. It is titiler's
`MosaicTilerFactory` with the assets coming from a live spatial query instead of
a MosaicJSON document, so tiling, rescaling, colormaps and output formats are all
inherited rather than reimplemented.

## Usage

```python
from fastapi import FastAPI
from macrostrat.raster_index import RasterIndex
from macrostrat.raster_layers import RasterLayerConfig, register_raster_layers

app = FastAPI()
index = RasterIndex("postgresql://localhost:5432/macrostrat")

register_raster_layers(
    app,
    index,
    [
        # A unified layer over several indexed collections, composited in order.
        RasterLayerConfig(
            slug="mineral-maps",
            layers=["emit-minerals", "aviris-minerals"],
            resampling="nearest",  # categorical data
        ),
        RasterLayerConfig(slug="elevation", resampling="bilinear"),
    ],
    prefix="/rasters",
)
```

Each layer gets titiler's full mosaic route set — `/tiles/{tileMatrixSetId}/{z}/{x}/{y}`,
`/{tileMatrixSetId}/tilejson.json`, `/info`, `/point/{lon},{lat}`, the
`/assets` lookups — plus two of its own:

- `/footprints/{z}/{x}/{y}` — coverage as a **vector tile**, MVT layer
  `raster_footprints`. The raster counterpart to a map-footprints layer: where
  the data is, without reading a pixel.
- `/footprints` — the same thing as GeoJSON, for small layers and diagnostics.

`register_raster_layers` installs its own exception handlers, so a host
application needs no extra setup. Don't register titiler's `MOSAIC_STATUS_CODES`
*after* mounting — it would replace the no-coverage handler with one that
returns a body-carrying 204 (see below).

## Behavior worth knowing

- **Overscaled tiles keep rendering.** Zooming past a raster's native
  resolution magnifies it rather than making the layer vanish, which is what
  every other raster service does. Set `allow_overscaled=False` in
  `backend_options` for the opposite; `raster_layers.should_generate_tile` in
  the index is the right tool for deciding what to *cache*.
- **No coverage is a bodyless `204`, with an explicit `Content-Length: 0`.**
  Both details matter: titiler's stock handler returns a 204 carrying a JSON
  body, and Starlette omits the length header entirely — either one makes
  Varnish fail the fetch and turn ordinary panning into 503s. Watch out for
  compression middleware too: gzipping an empty body re-attaches a
  `content-length` and reintroduces the same failure.
- **Colormaps default per layer, and ride along with the assets.** Categorical
  rasters are unreadable without their palette, so when a request doesn't send
  one, the layer's `colormap` from the index is used. It comes back on the same
  query that decides which rasters to read — one database round trip per tile,
  no cache to invalidate — so `set-colormap` and later edits take effect
  immediately. An explicit `colormap` / `colormap_name` query parameter always
  wins. Because the colormap travels per asset, per-raster rendering (leveling
  from `rescale_range`) has somewhere to live later.
- **Whole-mosaic operations are unsupported.** `/info`, `/statistics`, `/preview`
  and friends would mean reading every raster in a layer; layer-wide metadata
  belongs to the index.
- **No tile caching.** Left to the host application's caching layer.

## Sampling

`sample_point()` and `sample_line()` read values rather than render them — the
primitive behind an elevation service. A line is answered with one decimated
window read per intersecting raster, sized to the sample spacing so it comes
off the COG overviews, and the spacing is also handed to the index as a target
scale, so a coarse continental profile is read from a coarse global dataset
rather than from every fine tile it crosses. Validity is decided by the mask:
a NaN or a sentinel never reaches the caller, and a masked pixel falls through
to the next raster.

`PGRasterMosaic` applies each raster's nodata override (see `raster_index`) as
it opens the file, resolves points exactly against footprints, and with
`scale_aware=True` honours `?resolution=` on every mosaic route.
