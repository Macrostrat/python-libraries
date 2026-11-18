---
macrostrat.raster_index: minor
---

- Allow rasters with no layer (`raster.layer` is nullable); `add`, `scan` and `add_raster` no longer require one.
- Re-registering without a layer keeps the raster's existing layer.
- Add `rasters(unassigned=True)` and `rasters --unassigned`.
