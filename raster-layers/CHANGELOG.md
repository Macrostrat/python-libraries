# Changelog

## [0.4.0] - 2026-09-28 [_changes_](https://github.com/Macrostrat/python-libraries/compare/macrostrat.raster_layers-v0.3.3...macrostrat.raster_layers-v0.4.0)

- `raster_index`: `add_declared()` registers rasters from a declared profile
  without opening them; `verify_sample()` and `verify` check a random sample
  against the files
- `raster_index`: `set_footprints()` / `set-footprints` clip footprints to an
  external geometry (a PostGIS table, a GeoJSON file or URL); a footprint only
  gets tighter, re-registration never widens one, empty clips are reported
  rather than stored. No schema change
- `raster_index`: a per-raster reader nodata override (`set_nodata()` /
  `set-nodata`, `--nodata` on `add` and `scan`), carried on `RasterAsset.nodata`;
  the `nodata` column keeps what the file declares
- `raster_index`: the scale window — `zoom_for_resolution()` /
  `resolution_for_zoom()`, `zoom` and `scale_aware` on `assets_for_bbox()`,
  and new `assets_for_point()` (exact against the footprint) and
  `assets_for_geometry()`. Default ordering is unchanged
- `raster_index`: `RasterAsset.bounds`; `create_test_dem()` fixture helper
- `raster_layers`: `PGRasterMosaic` applies each asset's nodata override when
  opening it, resolves points exactly against footprints rather than through a
  zoom-14 tile, and takes `scale_aware` / `target_zoom`; `?resolution=` on every
  mosaic route sets the target
- `raster_layers`: `sample_point()` and `sample_line()` — one decimated window
  read per intersecting raster, scale-aware selection, mask-driven validity
- Require `macrostrat.raster_index` 0.4.0 or newer.

## [0.3.3] - 2026-09-23 [_changes_](https://github.com/Macrostrat/python-libraries/compare/macrostrat.raster_layers-v0.3.2...macrostrat.raster_layers-v0.3.3)

- Require `macrostrat.raster_index` 0.3.2 or newer.

## [0.3.2] - 2026-08-25

- Fix: a mounted layer advertised URLs that 404. `register_raster_layers` now
  tells each factory the prefix it is mounted at, so TileJSON `tiles` entries
  (and any WMTS templates) resolve. `RasterLayerConfig.router()` takes an
  optional `prefix`

## [0.3.1] - 2026-08-25

- A mosaic advertises the zoom range its rasters actually resolve, rather than
  the tile grid's 0-24. Affects `/info`, `tilejson.json` and WMTS capabilities;
  serving is unchanged, so overscaled tiles still render
- Bounds and zoom range come from one query, so backend construction still costs
  a single round trip
- Requires `macrostrat.raster_index` 0.3.1

## [0.3.0] - 2026-08-23

- `?datasets=` narrows a mosaic to specific rasters by slug, so a single dataset
  can be viewed through the layer — keeping its palette, class vocabulary,
  transparent empty tiles and per-asset point queries — rather than through a
  separate single-file route. Applies to tiles, `/point`, `/info`, `/assets` and
  `/footprints`, via titiler's `backend_dependency`
- Requires `macrostrat.raster_index` 0.3.0

## [0.2.1] - 2026-08-22

- Serve a transparent PNG where a layer has no coverage, instead of a bodyless
  204. mapbox-gl treats a 204 as a successful response and then fails to decode
  the empty body, so every tile past the edge of the data surfaced as "the image
  could not be decoded". `/point` and non-PNG formats keep the 204

## [0.2.0] - 2026-08-22

- Filter a categorical mosaic to named classes:
  `?algorithm=classes&algorithm_params={"classes":["Kaolinite"]}`. Applied after
  compositing, so excluded classes are masked and survivors keep the layer's
  palette
- New `/layer` route reporting a layer's palette and class vocabulary, so a
  client can build a legend without reading a raster
- Requires `macrostrat.raster_index` 0.2.0, which carries `categories` on assets

## [0.1.1] - 2026-08-12

- Resolve colormaps from the assets a tile read already fetched, rather than
  caching them or querying separately: `set-colormap` and later edits take effect
  on the next tile, with one database query per tile instead of two
- Requires `macrostrat.raster_index` 0.1.1, whose selection moved out of stored
  functions into the package

## [0.1.0] - 2026-08-12

- Initial release: `PGRasterMosaic` (a rio-tiler mosaic backend reading assets
  from `macrostrat.raster_index`), `RasterMosaicFactory`, canned
  `RasterLayerConfig` layers, and a `raster_footprints` vector-tile layer
