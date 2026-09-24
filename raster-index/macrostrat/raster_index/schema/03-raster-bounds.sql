/*
A raster's bounding box, kept apart from its footprint.

`footprint` is *where to select this raster*, and may be far smaller than the
file: a coastal SRTM tile clipped to the land polygons, a survey swath traced
from its mask. `bounds` is *what the file covers*, and never changes. Keeping
both is what makes footprint operations re-runnable — a clip is always
`bounds ∩ source`, never `footprint ∩ source`, so successive runs cannot erode
it — and lets a footprint be recomputed from a different source without
re-reading the file.

Rows indexed before this column existed have their bounds recovered from the
footprint's envelope, which is exact for a bounding-box footprint and a safe
over-estimate for a traced one.
*/

ALTER TABLE raster_layers.raster
  ADD COLUMN IF NOT EXISTS bounds geometry(Polygon, 4326);

UPDATE raster_layers.raster
SET bounds = ST_Envelope(footprint)
WHERE bounds IS NULL;
