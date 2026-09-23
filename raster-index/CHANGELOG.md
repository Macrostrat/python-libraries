# Changelog

## [0.3.2] - 2026-09-23 [_changes_](https://github.com/Macrostrat/python-libraries/compare/macrostrat.raster_index-v0.3.1...macrostrat.raster_index-v0.3.2)

- Require `macrostrat.database` 4.7.0 or newer.

## [0.3.1] - 2026-08-25

- `RasterIndex.layer_extent()` returns bounds *and* the native zoom range of the
  selected rasters in one query; `layer_bounds()` delegates to it

## [0.3.0] - 2026-08-23

- Footprints can be traced from a raster's validity mask instead of its bounding
  box, so tiles outside the data stop selecting it: `refine-footprints` for
  indexed rasters, `--mask-footprints` on `add`/`scan` for new ones. The mask is
  read decimated (through overviews where present) and the result generalized to
  a vertex budget, then grown so it still covers its own data. Needs the new
  `footprints` extra (shapely)
- Selection accepts a `rasters=` filter, narrowing a layer to specific slugs
- `RasterIndex.update_footprint()`, and `add_raster(mask_footprint=True)`
- `register_layer` no longer nulls `metadata` when it isn't supplied

## [0.2.0] - 2026-08-22

- Layers can carry a class vocabulary — named classes for a categorical raster's
  integer values — stored in the existing `metadata` column, so no schema change
- Derive vocabularies from GDAL band metadata, a `.qml` sidecar or a JSON file
  (`categories_from_info`, `categories_from_qml`, `categories_from_json`)
- New `set-categories` command; `info --metadata` shows band metadata and the
  class vocabularies it holds
- Selection returns each layer's `categories`, so a tile read resolves what its
  values mean in the same query that picks the assets
- `register_layer` no longer nulls out `metadata` when it isn't supplied
- `RasterIndex.layer()` and `raster_info()` for reading one layer or one
  raster's stored reader metadata

## [0.1.1] - 2026-08-12

- Move raster selection out of stored functions into query text owned by the
  package (`queries.py`), leaving the schema as tables and indexes only
- Selection returns each layer's `colormap`, so a raster carries how to draw it
  as well as where it is; `RasterAsset` gained the matching field

## [0.1.0] - 2026-08-12

- Initial release: the `raster_layers` schema, `RasterIndex`, footprint
  extraction through rio-tiler, bucket scanning, and a mountable Typer CLI
