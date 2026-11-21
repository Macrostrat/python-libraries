---
macrostrat.raster_index: minor
---

- `set-categories --from` reads GDAL category names from a raster's `.aux.xml` sidecar when its band metadata names no classes, and accepts the sidecar directly. Add `categories_from_aux_xml` and `NoClassVocabulary`.
