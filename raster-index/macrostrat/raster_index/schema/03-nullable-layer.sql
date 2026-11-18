-- Lets a raster be registered before it is assigned to a layer.
ALTER TABLE raster_layers.raster ALTER COLUMN layer DROP NOT NULL;
