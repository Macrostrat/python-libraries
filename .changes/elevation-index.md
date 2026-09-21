---
macrostrat.raster_index: minor
macrostrat.raster_layers: minor
---

- `raster_index`: a `bounds` column kept apart from the footprint (schema file
  `03-raster-bounds.sql`, backfilled from the footprint envelope)
- `raster_index`: `add_declared()` registers rasters from a declared profile
  without opening them; `verify_sample()` and `verify` check a random sample
  against the files
- `raster_index`: `set_footprints()` / `set-footprints` clip footprints to an
  external geometry (a PostGIS table, a GeoJSON file or URL) — always
  `bounds ∩ source`, re-runnable, empty clips reported rather than stored
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
