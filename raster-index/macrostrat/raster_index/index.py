"""The index itself: reading and writing the `raster_layers` schema.

Deliberately built on SQLAlchemy Core rather than ORM models. The index has two
very different consumers — a CLI that registers rasters, and a tile server that
asks one hot question per tile — and neither benefits from reflection. Keeping to
Core also means the serving side can hold a plain engine and run inside
FastAPI's threadpool without dragging session state along.
"""

import json
import math
import random
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from macrostrat.utils import get_logger

from .declared import VerificationReport, compare_declared
from .defs import (
    DeclaredRaster,
    LayerDefinition,
    LayerExtent,
    RasterAsset,
    RasterCategory,
    RasterInfo,
)
from .external_footprints import (
    FootprintReport,
    FootprintRow,
    FootprintSource,
    apply_sql,
    report_sql,
)
from .footprints import bounds_to_geometry, get_raster_info
from .queries import (
    GEOJSON_GEOMETRY,
    POINT_GEOMETRY,
    TILE_ENVELOPE,
    bbox_envelope,
    selection,
)

log = get_logger(__name__)

__all__ = ["RasterIndex", "schema_files"]

__here__ = Path(__file__).parent

# Anything that can be turned into an engine: a URL, a SQLAlchemy engine or
# connection, or an object exposing `.engine` (such as `macrostrat.database`'s
# `Database`, duck-typed so this package doesn't pin a version of it).
Connectable = Union[Engine, Connection, str, Any]


def schema_files() -> list[Path]:
    """The SQL files defining the `raster_layers` schema, in application order.

    Exposed so a host application can fold them into its own schema-management
    system (Macrostrat wraps them in a `SchemaDefinition` chunk) instead of this
    package having to know anything about how schemas get applied.
    """
    return sorted((__here__ / "schema").glob("*.sql"))


def _resolve_engine(conn: Connectable) -> Engine:
    if isinstance(conn, Engine):
        return conn
    if isinstance(conn, Connection):
        return conn.engine
    engine = getattr(conn, "engine", None)
    if engine is not None:
        return engine
    return create_engine(str(conn))


class RasterIndex:
    """An index of cloud-optimized rasters, grouped into named layers."""

    engine: Engine

    def __init__(self, connection: Connectable):
        self.engine = _resolve_engine(connection)

    # -- Schema ------------------------------------------------------------

    def create_schema(self) -> None:
        """Apply the `raster_layers` schema.

        Standalone use (tests, a scratch database) only — in Macrostrat the
        schema is applied by the schema-management system, from the same files.
        PostGIS is enabled here rather than in the SQL, because installing an
        extension needs privileges the role that owns an application schema
        generally doesn't have.
        """
        # Imported lazily: applying multi-statement SQL files is the only thing
        # `macrostrat.database` is needed for, and nothing on the serving path
        # should pay for importing it.
        from macrostrat.database import Database

        with self.engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        db = Database(self.engine.url)
        for file in schema_files():
            db.run_sql(file)

    def schema_exists(self) -> bool:
        """Whether the `raster_layers` schema has been applied."""
        sql = text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.schemata "
            "WHERE schema_name = 'raster_layers')"
        )
        with self.engine.connect() as conn:
            return bool(conn.execute(sql).scalar())

    # -- Layers ------------------------------------------------------------

    def register_layer(
        self, layer: Union[LayerDefinition, str], **kwargs
    ) -> LayerDefinition:
        """Create or update a layer definition.

        Idempotent: re-registering a layer overwrites the fields it specifies.
        `metadata` (and so the class vocabulary inside it) is the exception — it
        is left alone when not supplied, rather than being nulled out.
        """
        if isinstance(layer, str):
            layer = LayerDefinition(slug=layer, **kwargs)
        elif kwargs:
            layer = layer.model_copy(update=kwargs)

        sql = text("""
            INSERT INTO raster_layers.layer
              (slug, name, description, minzoom, maxzoom, rescale_range,
               colormap, metadata)
            VALUES
              (:slug, :name, :description, :minzoom, :maxzoom, CAST(:rescale_range AS numeric[]),
               CAST(:colormap AS jsonb), CAST(:metadata AS jsonb))
            ON CONFLICT (slug) DO UPDATE SET
              name = excluded.name,
              description = excluded.description,
              minzoom = excluded.minzoom,
              maxzoom = excluded.maxzoom,
              rescale_range = excluded.rescale_range,
              colormap = excluded.colormap,
              -- Unlike the other fields, an unspecified `metadata` leaves what
              -- is there alone. The class vocabulary lives in this column and is
              -- written by a separate command, so a later `define-layer` that
              -- says nothing about metadata must not silently destroy it.
              metadata = coalesce(excluded.metadata, layer.metadata)
            """)
        params = dict(
            slug=layer.slug,
            name=layer.name or layer.slug,
            description=layer.description,
            minzoom=layer.minzoom,
            maxzoom=layer.maxzoom,
            rescale_range=layer.rescale_range,
            colormap=_json(layer.colormap),
            metadata=_json(_stored_metadata(layer)),
        )
        with self.engine.begin() as conn:
            conn.execute(sql, params)
        log.info("Registered layer %s", layer.slug)
        return layer

    def layers(self) -> list[LayerDefinition]:
        sql = text("""
            SELECT slug, name, description, minzoom, maxzoom, rescale_range,
                   colormap, metadata
            FROM raster_layers.layer
            ORDER BY slug
            """)
        with self.engine.connect() as conn:
            rows = conn.execute(sql).mappings().all()
        return [_layer_from_row(row) for row in rows]

    def layer(self, slug: str) -> Optional[LayerDefinition]:
        """One layer definition, or None if it isn't defined.

        The serving side exposes this so a client can fetch a layer's palette and
        class vocabulary in a single request, instead of reading them out of a
        raster it had to know the name of.
        """
        sql = text("""
            SELECT slug, name, description, minzoom, maxzoom, rescale_range,
                   colormap, metadata
            FROM raster_layers.layer
            WHERE slug = :slug
            """)
        with self.engine.connect() as conn:
            row = conn.execute(sql, dict(slug=slug)).mappings().first()
        if row is None:
            return None
        return _layer_from_row(row)

    # -- Class vocabularies ------------------------------------------------

    def get_categories(self, layer: str) -> list[RasterCategory]:
        """The class vocabulary of a categorical layer, in class order."""
        sql = text(
            "SELECT metadata -> 'categories' FROM raster_layers.layer "
            "WHERE slug = :slug"
        )
        with self.engine.connect() as conn:
            value = conn.execute(sql, dict(slug=layer)).scalar()
        return _categories(value)

    def set_categories(
        self, layer: str, categories: Iterable[RasterCategory]
    ) -> list[RasterCategory]:
        """Store a layer's class vocabulary.

        Merged into `metadata` rather than replacing it: the column holds other
        layer-level metadata, and setting a vocabulary must not silently drop it.
        """
        resolved = sorted(categories, key=lambda c: c.value)
        sql = text("""
            UPDATE raster_layers.layer
            SET metadata = coalesce(metadata, '{}'::jsonb)
                           || jsonb_build_object('categories', CAST(:categories AS jsonb))
            WHERE slug = :slug
            """)
        params = dict(
            slug=layer,
            categories=json.dumps([c.model_dump(mode="json") for c in resolved]),
        )
        with self.engine.begin() as conn:
            res = conn.execute(sql, params)
        if res.rowcount == 0:
            raise ValueError(f"Layer {layer!r} is not defined")
        log.info("Set a %d-class vocabulary on layer %s", len(resolved), layer)
        return resolved

    def remove_layer(self, slug: str, *, cascade: bool = False) -> int:
        """Delete a layer. Its rasters must be gone first unless `cascade`."""
        with self.engine.begin() as conn:
            if cascade:
                conn.execute(
                    text("DELETE FROM raster_layers.raster WHERE layer = :slug"),
                    dict(slug=slug),
                )
            res = conn.execute(
                text("DELETE FROM raster_layers.layer WHERE slug = :slug"),
                dict(slug=slug),
            )
        return res.rowcount

    # -- Rasters -----------------------------------------------------------

    def add_raster(
        self,
        href: Union[str, Path],
        layer: str,
        *,
        slug: Optional[str] = None,
        info: Optional[RasterInfo] = None,
        rescale_range: Optional[list[float]] = None,
        minzoom: Optional[int] = None,
        maxzoom: Optional[int] = None,
        ensure_layer: bool = True,
        reader_options: Optional[dict] = None,
        mask_footprint: bool = False,
        footprint: Optional[dict[str, Any]] = None,
        nodata: Optional[float] = None,
    ) -> RasterInfo:
        """Register a raster, reading its metadata if not supplied.

        Keyed on `href`, so this is an upsert: pointing the CLI at the same
        bucket twice refreshes the index rather than duplicating it. One
        consequence worth knowing — a raster belongs to exactly one layer, so
        registering the same href under a different layer *moves* it rather than
        adding a second copy. A footprint or nodata override set earlier
        survives a plain re-registration (see the SQL below).

        `footprint` supplies the selection geometry a priori — a survey boundary,
        a coastline clip — in place of the bounding box. `mask_footprint` derives
        one from the data instead, trading registration time for selectivity.

        `nodata` is a *reader override*: the value the serving side treats as
        missing regardless of what the file declares. The file's own nodata is
        still recorded in the `nodata` column, so a verification can compare it.
        """
        href = str(href)
        if info is None:
            info = get_raster_info(href, **(reader_options or {}))
        if slug is None:
            slug = default_slug(href)

        geometry = footprint if footprint is not None else info.geometry
        if mask_footprint and footprint is None:
            # Reads the raster's mask, so it is opt-in — see `mask_footprint`.
            # `None` means the data fills its bounding box and the bbox stands.
            from .mask_footprint import mask_footprint as compute_mask_footprint

            derived = compute_mask_footprint(href, **(reader_options or {}))
            if derived is not None:
                log.info(
                    "Footprint for %s covers %.1f%% of its bounding box",
                    slug,
                    100 * derived.area_fraction,
                )
                geometry = derived.geometry

        if ensure_layer:
            self._ensure_layer(layer)

        metadata = dict(info.metadata)
        if nodata is not None:
            metadata["nodata_override"] = nodata

        params = _raster_row(
            layer=layer,
            slug=slug,
            href=href,
            geometry=geometry,
            bounds=info.bounds,
            minzoom=minzoom if minzoom is not None else info.minzoom,
            maxzoom=maxzoom if maxzoom is not None else info.maxzoom,
            dtype=info.dtype,
            nbands=info.nbands,
            nodata=info.nodata,
            crs=info.crs,
            rescale_range=rescale_range,
            info=metadata,
        )
        with self.engine.begin() as conn:
            conn.execute(_UPSERT_RASTER, params)
        log.info("Registered raster %s in layer %s", slug, layer)
        return info

    def add_declared(
        self,
        layer: str,
        rasters: Iterable[DeclaredRaster],
        *,
        ensure_layer: bool = True,
        batch_size: int = 1000,
    ) -> int:
        """Register rasters from declarations, opening no files.

        The path for a standardized product: rows come from a bucket listing
        plus a declared profile, and the whole index is written in one
        statement per batch. Nothing is read, so nothing is *checked* here —
        follow with `verify_sample`, which is the other half of the contract.

        Returns how many rows were written.
        """
        if ensure_layer:
            self._ensure_layer(layer)

        written = 0
        batch: list[dict] = []

        def flush():
            nonlocal written
            if not batch:
                return
            with self.engine.begin() as conn:
                conn.execute(_UPSERT_RASTER, batch)
            written += len(batch)
            batch.clear()

        for declared in rasters:
            geometry = declared.footprint or bounds_to_geometry(declared.bounds)
            batch.append(
                _raster_row(
                    layer=layer,
                    slug=declared.slug or default_slug(declared.href),
                    href=declared.href,
                    geometry=geometry,
                    bounds=declared.bounds,
                    minzoom=declared.minzoom,
                    maxzoom=declared.maxzoom,
                    dtype=declared.dtype,
                    nbands=declared.nbands,
                    nodata=declared.nodata,
                    crs=declared.crs,
                    rescale_range=None,
                    info=dict(declared.info),
                )
            )
            if len(batch) >= batch_size:
                flush()
        flush()
        log.info("Registered %d declared rasters in layer %s", written, layer)
        return written

    def set_nodata(
        self,
        layer: str,
        nodata: Optional[float],
        *,
        rasters: Optional[list[str]] = None,
    ) -> int:
        """Set (or, with None, clear) the reader nodata override for a layer.

        The override is what the serving side passes to the reader; the
        `nodata` column keeps what the file declares. SRTM GL1 is the motivating
        case — the file says -32768 and stores the ocean as 0 — and one override
        is what lets water fall through to a bathymetry layer beneath it.
        Returns how many rasters were affected.
        """
        if nodata is None:
            sql = text("""
                UPDATE raster_layers.raster
                SET info = info - 'nodata_override', updated_at = now()
                WHERE layer = :layer
                  AND info ? 'nodata_override'
                  AND (CAST(:rasters AS text[]) IS NULL
                       OR slug = ANY(CAST(:rasters AS text[])))
                """)
        else:
            sql = text("""
                UPDATE raster_layers.raster
                SET info = coalesce(info, CAST('{}' AS jsonb))
                           || jsonb_build_object('nodata_override', CAST(:nodata AS text)),
                    updated_at = now()
                WHERE layer = :layer
                  AND (CAST(:rasters AS text[]) IS NULL
                       OR slug = ANY(CAST(:rasters AS text[])))
                """)
        # Stored as text so NaN survives: JSON has no NaN, and Postgres reads
        # the string back as a float when the selection query casts it.
        params = dict(layer=layer, rasters=rasters, nodata=_float_text(nodata))
        with self.engine.begin() as conn:
            count = conn.execute(sql, params).rowcount
        log.info("Set nodata override %r on %d rasters in %s", nodata, count, layer)
        return count

    def set_footprints(
        self,
        layer: str,
        source: FootprintSource,
        *,
        rasters: Optional[list[str]] = None,
        apply: bool = True,
    ) -> FootprintReport:
        """Clip each raster's footprint to an external geometry.

        The footprint becomes `bounds ∩ source` — computed from the stored
        bounds every time, so this is re-runnable and can never claim ground
        outside the file. A raster that the source does not touch at all is
        *reported* in the result and left alone; that is either a raster to
        remove or the wrong source, and neither should be decided silently.

        With `apply=False` the report is produced and nothing is written.
        """
        params: dict[str, Any] = dict(layer=layer, rasters=rasters, **source.params)
        with self.engine.begin() as conn:
            rows = conn.execute(text(report_sql(source)), params).mappings().all()
            report = FootprintReport(
                layer=layer,
                source=source.description,
                rows=[
                    FootprintRow(
                        r["slug"], float(r["fraction"] or 0.0), int(r["vertices"])
                    )
                    for r in rows
                ],
                applied=apply,
            )
            if apply:
                conn.execute(text(apply_sql(source)), params)
        if apply:
            log.info(
                "Clipped %d footprints in %s against %s (%d unchanged, %d outside)",
                len(report.clipped),
                layer,
                source.description,
                len(report.whole),
                len(report.empty),
            )
        return report

    def verify_sample(
        self,
        layer: str,
        *,
        sample: int = 10,
        seed: Optional[int] = None,
        rasters: Optional[list[str]] = None,
        reader_options: Optional[dict] = None,
    ) -> VerificationReport:
        """Open a random sample of a layer's rasters and compare them to the index.

        The check that turns a declaration into a dated verification: every
        stored fact about each sampled raster — dtype, bands, CRS, nodata, zoom
        range, bounds — is compared against a fresh read of the file. Any
        disagreement is returned rather than raised, so a caller can print all of
        them at once.
        """
        rows = self.rasters(layer)
        if rasters is not None:
            wanted = set(rasters)
            rows = [r for r in rows if r["slug"] in wanted]
        chosen = random.Random(seed).sample(rows, min(sample, len(rows)))

        report = VerificationReport(layer, len(chosen), [], [])
        for row in chosen:
            try:
                info = get_raster_info(row["href"], **(reader_options or {}))
            except Exception as err:  # noqa: BLE001 - reported, not raised
                report.unreadable.append((row["slug"], str(err)))
                continue
            report.mismatches.extend(compare_declared(row, info))
        return report

    def add_rasters(
        self, hrefs: Iterable[Union[str, Path]], layer: str, **kwargs
    ) -> list[RasterInfo]:
        """Register several rasters, skipping (but reporting) any that fail.

        A bad or unreadable object in a bucket shouldn't abort the rest of a
        scan; the log line is the record of what was left out.
        """
        results = []
        for href in hrefs:
            try:
                results.append(self.add_raster(href, layer, **kwargs))
            except Exception as err:
                log.error("Could not register %s: %s", href, err)
        return results

    def rasters(self, layer: Optional[str] = None) -> list[dict[str, Any]]:
        sql = """
            SELECT id, layer, slug, href, minzoom, maxzoom, dtype, nbands,
                   nodata, crs, ST_AsGeoJSON(footprint) footprint,
                   ARRAY[ST_XMin(bounds), ST_YMin(bounds),
                         ST_XMax(bounds), ST_YMax(bounds)] bounds,
                   CAST(info ->> 'nodata_override' AS double precision) nodata_override
            FROM raster_layers.raster
        """
        params: dict[str, Any] = {}
        if layer is not None:
            sql += " WHERE layer = :layer"
            params["layer"] = layer
        sql += " ORDER BY layer, slug"
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql), params).mappings()]

    def remove_raster(self, href: str) -> int:
        with self.engine.begin() as conn:
            res = conn.execute(
                text("DELETE FROM raster_layers.raster WHERE href = :href"),
                dict(href=href),
            )
        return res.rowcount

    def update_footprint(self, href: str, geometry: dict[str, Any]) -> None:
        """Replace a raster's footprint.

        `ST_MakeValid` + `ST_CollectionExtract` rather than storing the geometry
        as handed over: an invalid ring would make every `ST_Intersects` against
        this row raise, which would take out the whole layer rather than one
        raster.
        """
        sql = text("""
            UPDATE raster_layers.raster
            SET footprint = ST_Multi(
                  ST_CollectionExtract(
                    ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(:geometry), 4326)),
                    3
                  )
                ),
                updated_at = now()
            WHERE href = :href
            """)
        with self.engine.begin() as conn:
            res = conn.execute(sql, dict(href=href, geometry=json.dumps(geometry)))
        if res.rowcount == 0:
            raise ValueError(f"No indexed raster with href {href!r}")

    def raster_info(
        self, ref: str, *, layer: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """The stored reader metadata for a raster, by href or slug.

        `add_raster` keeps the full `src.info()` output, so anything derived from
        a raster's headers — a class vocabulary, notably — can be recovered from
        the index without reopening the file in object storage.
        """
        sql = "SELECT info FROM raster_layers.raster WHERE (href = :ref OR slug = :ref)"
        params: dict[str, Any] = dict(ref=ref)
        if layer is not None:
            sql += " AND layer = :layer"
            params["layer"] = layer
        sql += " ORDER BY id LIMIT 1"
        with self.engine.connect() as conn:
            row = conn.execute(text(sql), params).first()
        if row is None:
            return None
        return row[0]

    # -- Raster selection --------------------------------------------------
    #
    # Every lookup composes `queries.selection`, so the ordering and zoom rules
    # live in exactly one place. These methods differ only in the area they ask
    # about and what they do with the rows.

    # Columns needed to build a `RasterAsset`.
    _ASSET_COLUMNS = (
        "href, layer, slug, minzoom, maxzoom, rescale_range, colormap, "
        "categories, nodata, bounds, overscaled"
    )

    def _select_assets(self, geometry: str, params: dict) -> list[RasterAsset]:
        """Run the selection over an area and return assets in read order."""
        sql = text(
            f"SELECT {self._ASSET_COLUMNS} FROM ({selection(geometry)}) selected"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
        return [
            RasterAsset(
                **{
                    **dict(row),
                    "rescale_range": _floats(row["rescale_range"]),
                    "bounds": _bounds(row["bounds"]),
                }
            )
            for row in rows
        ]

    def assets_for_tile(
        self,
        x: int,
        y: int,
        z: int,
        layers: list[str],
        *,
        zoom_tolerance: int = 3,
        rasters: Optional[list[str]] = None,
        scale_aware: bool = False,
    ) -> list[RasterAsset]:
        """The rasters to composite for a tile, in compositing order.

        `rasters` narrows the mosaic to specific slugs — one dataset viewed
        through the layer rather than on its own terms. `scale_aware` ranks by
        closeness to the tile's zoom instead of finest-first; off by default so
        existing tile layers are untouched.
        """
        return self._select_assets(
            TILE_ENVELOPE,
            dict(
                x=x,
                y=y,
                z=z,
                layers=layers,
                rasters=rasters,
                **_scale_params(z, zoom_tolerance, scale_aware),
            ),
        )

    def assets_for_bbox(
        self,
        west: float,
        south: float,
        east: float,
        north: float,
        layers: list[str],
        *,
        rasters: Optional[list[str]] = None,
        zoom: Optional[int] = None,
        zoom_tolerance: int = 3,
        scale_aware: bool = False,
    ) -> list[RasterAsset]:
        """The rasters intersecting a WGS84 bounding box, in compositing order.

        Without `zoom` nothing is filtered or flagged as overscaled. With one,
        the same window a tile query applies is in force — rasters far too fine
        for the requested scale are left out — and `scale_aware` additionally
        ranks by closeness to it. This is what keeps a coarse continental
        profile from opening every 1° tile it crosses.
        """
        return self._select_assets(
            bbox_envelope,
            dict(
                west=west,
                south=south,
                east=east,
                north=north,
                layers=layers,
                rasters=rasters,
                **_scale_params(zoom, zoom_tolerance, scale_aware),
            ),
        )

    def assets_for_point(
        self,
        lng: float,
        lat: float,
        layers: list[str],
        *,
        rasters: Optional[list[str]] = None,
        zoom: Optional[int] = None,
        zoom_tolerance: int = 3,
        scale_aware: bool = False,
    ) -> list[RasterAsset]:
        """The rasters whose footprint contains a point, in read order.

        An exact test against the footprint rather than against the tile
        around the point, so a footprint clipped to a coastline is honored
        precisely: a click in the water never selects the land tile whose
        bounding box covers it. Scale parameters as for `assets_for_bbox`.
        """
        return self._select_assets(
            POINT_GEOMETRY,
            dict(
                lng=lng,
                lat=lat,
                layers=layers,
                rasters=rasters,
                **_scale_params(zoom, zoom_tolerance, scale_aware),
            ),
        )

    def assets_for_geometry(
        self,
        geometry: dict[str, Any],
        layers: list[str],
        *,
        rasters: Optional[list[str]] = None,
        zoom: Optional[int] = None,
        zoom_tolerance: int = 3,
        scale_aware: bool = False,
    ) -> list[RasterAsset]:
        """The rasters intersecting a GeoJSON geometry (EPSG:4326), in read order.

        A profile line selects only what it actually crosses, rather than every
        raster in its bounding box. Scale parameters as for `assets_for_bbox`.
        """
        return self._select_assets(
            GEOJSON_GEOMETRY,
            dict(
                geometry=json.dumps(geometry),
                layers=layers,
                rasters=rasters,
                **_scale_params(zoom, zoom_tolerance, scale_aware),
            ),
        )

    def should_generate_tile(
        self,
        x: int,
        y: int,
        z: int,
        layers: list[str],
        *,
        rasters: Optional[list[str]] = None,
    ) -> bool:
        """Whether a tile has any asset that isn't overscaled.

        Used to avoid caching (or even rendering) tiles that would only ever be a
        blurry magnification of data already served at a lower zoom.
        """
        sql = text(f"""
            SELECT EXISTS (
              SELECT 1 FROM ({selection(TILE_ENVELOPE)}) selected
              WHERE NOT overscaled
            )
            """)
        params = dict(
            x=x,
            y=y,
            z=z,
            layers=layers,
            rasters=rasters,
            zoom=z,
            tolerance=3,
            target_zoom=None,
        )
        with self.engine.connect() as conn:
            return bool(conn.execute(sql, params).scalar())

    def layer_extent(
        self, layers: list[str], *, rasters: Optional[list[str]] = None
    ) -> Optional[LayerExtent]:
        """What a set of layers covers, and the zoom range its rasters resolve.

        One query for both, because a tile route asks for both when it builds a
        backend — and it does that on every request.

        The zoom range matters beyond bookkeeping: it is what `/info`,
        `tilejson.json` and the WMTS capabilities document advertise. Reporting
        the tile grid's range instead (0–24) tells clients there is data at
        resolutions no raster in the layer can produce.

        `None` when no rasters are indexed — the caller decides whether that is
        an empty layer or a typo.
        """
        sql = text(f"""
            SELECT ST_XMin(e) west, ST_YMin(e) south,
                   ST_XMax(e) east, ST_YMax(e) north,
                   mn minzoom, mx maxzoom
            FROM (
              SELECT ST_Extent(footprint) e,
                     min(minzoom) mn, max(maxzoom) mx
              FROM ({selection()}) selected
            ) a
            WHERE e IS NOT NULL
            """)
        params = dict(
            layers=layers, rasters=rasters, zoom=None, tolerance=None, target_zoom=None
        )
        with self.engine.connect() as conn:
            row = conn.execute(sql, params).first()
        if row is None:
            return None
        return LayerExtent(
            bounds=(row.west, row.south, row.east, row.north),
            minzoom=row.minzoom,
            maxzoom=row.maxzoom,
        )

    def layer_bounds(
        self, layers: list[str], *, rasters: Optional[list[str]] = None
    ) -> Optional[tuple[float, float, float, float]]:
        """The combined extent of a set of layers, in EPSG:4326.

        `None` when no rasters are indexed for them — the caller decides whether
        that is an empty layer or a typo.
        """
        extent = self.layer_extent(layers, rasters=rasters)
        if extent is None:
            return None
        return extent.bounds

    def footprints(
        self, layers: list[str], *, rasters: Optional[list[str]] = None
    ) -> dict[str, Any]:
        """The footprints of a set of layers, as a GeoJSON FeatureCollection."""
        sql = text(f"""
            SELECT jsonb_build_object(
              'type', 'Feature',
              'geometry', ST_AsGeoJSON(footprint)::jsonb,
              'properties', jsonb_build_object(
                'id', id, 'layer', layer, 'slug', slug, 'href', href,
                'minzoom', minzoom, 'maxzoom', maxzoom, 'dtype', dtype
              )
            )
            FROM ({selection()}) selected
            """)
        params = dict(
            layers=layers, rasters=rasters, zoom=None, tolerance=None, target_zoom=None
        )
        with self.engine.connect() as conn:
            features = [r[0] for r in conn.execute(sql, params)]
        return {"type": "FeatureCollection", "features": features}

    def footprint_tile(
        self,
        x: int,
        y: int,
        z: int,
        layers: Optional[list[str]] = None,
        *,
        rasters: Optional[list[str]] = None,
    ) -> bytes:
        """Raster footprints for a tile, as Mapbox Vector Tile bytes.

        The raster-side counterpart to Macrostrat's map-footprints layer: an
        index of *where coverage is*, cheap enough to draw at any zoom, without
        touching a single COG. No zoom filter — coverage should be visible even
        where the rasters themselves would be overscaled.

        The MVT layer name `raster_footprints` is a cross-repo contract: it must
        match the `source-layer` used by any client style.

        Empty (zero-length) where nothing intersects, which is a valid empty
        tile as far as a client is concerned.
        """
        sql = text(f"""
            WITH selected AS ({selection(TILE_ENVELOPE)}),
            footprints AS (
              SELECT
                id, layer, slug, href, minzoom, maxzoom, dtype,
                ST_AsMVTGeom(
                  ST_Transform(ST_Intersection(footprint, {TILE_ENVELOPE}), 3857),
                  ST_TileEnvelope(:z, :x, :y),
                  4096, 8, true
                ) AS geom
              FROM selected
            )
            SELECT ST_AsMVT(footprints, 'raster_footprints', 4096, 'geom')
            FROM footprints
            WHERE geom IS NOT NULL
            """)
        params = dict(
            x=x,
            y=y,
            z=z,
            layers=layers,
            rasters=rasters,
            zoom=None,
            tolerance=None,
            target_zoom=None,
        )
        with self.engine.connect() as conn:
            data = conn.execute(sql, params).scalar()
        return bytes(data) if data is not None else b""

    # -- Internals ---------------------------------------------------------

    def _ensure_layer(self, slug: str) -> None:
        """Create a bare layer if it doesn't exist, without clobbering one that does."""
        sql = text("""
            INSERT INTO raster_layers.layer (slug, name)
            VALUES (:slug, :slug)
            ON CONFLICT (slug) DO NOTHING
            """)
        with self.engine.begin() as conn:
            conn.execute(sql, dict(slug=slug))


# One statement for every way a raster row is written, so that reading a file
# and declaring one cannot drift apart. Keyed on `href`.
_UPSERT_RASTER = text("""
    INSERT INTO raster_layers.raster
      (layer, slug, href, footprint, bounds, minzoom, maxzoom, dtype, nbands,
       nodata, crs, rescale_range, info)
    VALUES (
      :layer, :slug, :href,
      ST_SetSRID(ST_GeomFromGeoJSON(CAST(:geometry AS text)), 4326),
      ST_MakeEnvelope(:west, :south, :east, :north, 4326),
      :minzoom, :maxzoom, :dtype, :nbands, :nodata, :crs,
      CAST(:rescale_range AS numeric[]), CAST(:info AS jsonb)
    )
    ON CONFLICT (href) DO UPDATE SET
      layer = excluded.layer,
      slug = excluded.slug,
      -- A footprint that was deliberately set — clipped to a coastline, traced
      -- from the mask — survives a plain re-registration of the same file.
      -- Only a registration that brings a footprint of its own, or a file whose
      -- bounds have changed, replaces it.
      footprint = CASE
        WHEN ST_Equals(excluded.footprint, excluded.bounds)
         AND raster.bounds IS NOT NULL
         AND ST_Equals(raster.bounds, excluded.bounds)
         AND NOT ST_Equals(raster.footprint, raster.bounds)
        THEN raster.footprint
        ELSE excluded.footprint
      END,
      bounds = excluded.bounds,
      minzoom = excluded.minzoom,
      maxzoom = excluded.maxzoom,
      dtype = excluded.dtype,
      nbands = excluded.nbands,
      nodata = excluded.nodata,
      crs = excluded.crs,
      rescale_range = excluded.rescale_range,
      -- Likewise a nodata override, unless the new registration carries one.
      info = CASE
        WHEN raster.info ? 'nodata_override'
         AND NOT (excluded.info ? 'nodata_override')
        THEN excluded.info
             || jsonb_build_object('nodata_override', raster.info -> 'nodata_override')
        ELSE excluded.info
      END,
      updated_at = now()
    """)


def _raster_row(
    *,
    layer: str,
    slug: str,
    href: str,
    geometry: dict[str, Any],
    bounds: tuple[float, float, float, float],
    minzoom: Optional[int],
    maxzoom: Optional[int],
    dtype: Optional[str],
    nbands: Optional[int],
    nodata: Optional[float],
    crs: Optional[str],
    rescale_range: Optional[list[float]],
    info: dict[str, Any],
) -> dict[str, Any]:
    """Bind parameters for `_UPSERT_RASTER`."""
    west, south, east, north = bounds
    return dict(
        layer=layer,
        slug=slug,
        href=href,
        geometry=json.dumps(geometry),
        west=west,
        south=south,
        east=east,
        north=north,
        minzoom=minzoom,
        maxzoom=maxzoom,
        dtype=dtype,
        nbands=nbands,
        nodata=nodata,
        crs=crs,
        rescale_range=rescale_range,
        info=_json(info),
    )


def _scale_params(
    zoom: Optional[int], tolerance: int, scale_aware: bool
) -> dict[str, Optional[int]]:
    """The three scale bind parameters, from one zoom and one switch.

    `zoom` drives the filter and the `overscaled` flag; `target_zoom` drives
    the ranking, and is only set when the caller asked for scale-aware
    selection. Without a zoom there is nothing to filter or rank by.
    """
    if zoom is None:
        return dict(zoom=None, tolerance=None, target_zoom=None)
    return dict(
        zoom=zoom, tolerance=tolerance, target_zoom=zoom if scale_aware else None
    )


def default_slug(href: str) -> str:
    """A raster's identifier within its layer, from the tail of its href."""
    return Path(href.split("?")[0]).stem


def _layer_from_row(row) -> LayerDefinition:
    """A layer definition from a `layer` row, with categories lifted out.

    `categories` lives inside the `metadata` jsonb column but is modeled as its
    own field, so it is removed from `metadata` here and folded back in by
    `_stored_metadata` on write — a round trip that leaves the column with one
    copy of the vocabulary rather than two.
    """
    metadata = dict(row["metadata"] or {})
    categories = _categories(metadata.pop("categories", None))
    return LayerDefinition(
        **{
            **dict(row),
            "rescale_range": _floats(row["rescale_range"]),
            "metadata": metadata or None,
            "categories": categories or None,
        }
    )


def _stored_metadata(layer: LayerDefinition) -> Optional[dict]:
    """The `metadata` column's value for a layer, with categories folded in."""
    metadata = dict(layer.metadata or {})
    if layer.categories:
        metadata["categories"] = [
            c.model_dump(mode="json")
            for c in sorted(layer.categories, key=lambda c: c.value)
        ]
    return metadata or None


def _categories(value: Optional[Any]) -> list[RasterCategory]:
    """Coerce a stored `categories` value into models, in class order."""
    if not value:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    categories = [RasterCategory(**item) for item in value]
    return sorted(categories, key=lambda c: c.value)


def _json(value: Optional[dict]) -> Optional[str]:
    """Serialize a dict for a `jsonb` column, preserving SQL NULL.

    JSON has no NaN, and a float32 raster's nodata very often is one. Non-finite
    floats become the strings Postgres itself uses for them, which it reads
    back as floats when the value is cast.
    """
    if value is None:
        return None
    return json.dumps(_jsonable(value))


def _jsonable(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return _float_text(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _float_text(value: Optional[float]) -> Optional[str]:
    """A float as the text Postgres reads back with `CAST(... AS double precision)`."""
    if value is None:
        return None
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return repr(float(value))


def _bounds(value: Optional[Iterable]) -> Optional[tuple[float, float, float, float]]:
    """A `bounds` array column as a tuple; None where the row has no bounds yet."""
    if value is None:
        return None
    values = [v for v in value]
    if any(v is None for v in values):
        return None
    west, south, east, north = (float(v) for v in values)
    return (west, south, east, north)


def _floats(value: Optional[Iterable]) -> Optional[list[float]]:
    """Coerce a `numeric[]` column (which comes back as Decimals) to floats."""
    if value is None:
        return None
    return [float(v) for v in value]
