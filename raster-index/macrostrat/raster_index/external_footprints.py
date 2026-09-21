"""Footprints injected from outside, rather than traced from the data.

A footprint only has to be *roughly* right and never smaller than the data, once
validity is handled at read time (a nodata override). That means it can come from
anywhere: a land-polygon table already in the database, a publisher's survey
boundaries, a hand-drawn GeoJSON. Clipping each raster's bounding box against
such a geometry reproduces what tracing the pixels would say — to within a tenth
of a percent on SRTM's coastal tiles — in seconds, with no COG opened.

Two guarantees are built in. The result is always `bounds ∩ source`, recomputed
from the stored bounds rather than the current footprint, so re-running it never
erodes anything and a raster can never claim ground outside its own file. And
an empty clip is *reported, not stored*: a raster wholly outside the source is
either one to remove or a wrong source, and either way a human should see it.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

__all__ = ["FootprintSource", "FootprintReport", "FootprintRow"]

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote(identifier: str) -> str:
    if not _IDENTIFIER.match(identifier):
        raise ValueError(f"{identifier!r} is not a plain SQL identifier")
    return f'"{identifier}"'


@dataclass(frozen=True)
class FootprintSource:
    """A geometry to clip footprints against, as a subquery yielding `geom`.

    Built through the constructors below; `sql` is composed from validated
    identifiers or bound parameters only, never from user text.
    """

    sql: str
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""

    @classmethod
    def table(cls, name: str, geometry_column: str = "geom") -> "FootprintSource":
        """A PostGIS table (or view), optionally schema-qualified.

        Kept as a plain `SELECT` so the planner inlines it and the table's own
        spatial index drives the `&&` test against each raster's bounds.
        """
        parts = name.split(".")
        if len(parts) > 2:
            raise ValueError(f"{name!r} should be `table` or `schema.table`")
        relation = ".".join(_quote(p) for p in parts)
        return cls(
            sql=f"SELECT {_quote(geometry_column)} AS geom FROM {relation}",
            description=name,
        )

    @classmethod
    def geojson(
        cls, data: dict[str, Any], description: str = "GeoJSON"
    ) -> "FootprintSource":
        """A GeoJSON geometry, Feature or FeatureCollection, in EPSG:4326.

        Every polygon becomes its own row, so a collection of survey boundaries
        behaves like a table of them.
        """
        geometries = list(_geometries(data))
        if not geometries:
            raise ValueError("The GeoJSON holds no geometries")
        collection = {"type": "GeometryCollection", "geometries": geometries}
        return cls(
            sql=(
                "SELECT (ST_Dump(ST_MakeValid(ST_SetSRID("
                "ST_GeomFromGeoJSON(CAST(:fp_source AS text)), 4326)))).geom AS geom"
            ),
            params={"fp_source": json.dumps(collection)},
            description=description,
        )

    @classmethod
    def file(cls, path: Union[str, Path]) -> "FootprintSource":
        path = Path(path)
        return cls.geojson(json.loads(path.read_text()), description=str(path))

    @classmethod
    def url(cls, url: str) -> "FootprintSource":
        """A GeoJSON document fetched over HTTP — publisher boundaries, usually."""
        from urllib.request import urlopen

        with urlopen(url) as response:  # noqa: S310 - caller-supplied URL
            data = json.load(response)
        return cls.geojson(data, description=url)


def _geometries(data: dict[str, Any]):
    kind = data.get("type")
    if kind == "FeatureCollection":
        for feature in data.get("features", []):
            yield from _geometries(feature)
    elif kind == "Feature":
        geometry = data.get("geometry")
        if geometry:
            yield geometry
    elif kind == "GeometryCollection":
        yield from data.get("geometries", [])
    elif kind is not None:
        yield data


@dataclass(frozen=True)
class FootprintRow:
    slug: str
    # Footprint area as a share of the bounding box; 0 where nothing intersects.
    fraction: float
    vertices: int

    @property
    def empty(self) -> bool:
        return self.fraction == 0


@dataclass
class FootprintReport:
    """What clipping a layer's footprints did, or would do."""

    layer: str
    source: str
    rows: list[FootprintRow]
    applied: bool

    @property
    def clipped(self) -> list[FootprintRow]:
        """Rasters whose footprint is now smaller than their bounding box."""
        return [r for r in self.rows if 0 < r.fraction < 0.9999]

    @property
    def whole(self) -> list[FootprintRow]:
        """Rasters lying entirely inside the source: the bounding box stands."""
        return [r for r in self.rows if r.fraction >= 0.9999]

    @property
    def empty(self) -> list[FootprintRow]:
        """Rasters entirely outside the source. Reported, never written."""
        return [r for r in self.rows if r.empty]


# The clip, shared by the report and the write so they cannot disagree. Each
# raster's *bounds* — never its current footprint — are intersected with every
# source row they touch, and the pieces unioned.
CLIP = """
    WITH source AS ({source}),
    targets AS (
      SELECT id, slug, bounds
      FROM raster_layers.raster
      WHERE layer = :layer
        AND (CAST(:rasters AS text[]) IS NULL OR slug = ANY(CAST(:rasters AS text[])))
    ),
    clipped AS (
      SELECT t.id,
             ST_Multi(ST_CollectionExtract(
               ST_MakeValid(ST_Union(ST_Intersection(t.bounds, s.geom))), 3
             )) AS geom
      FROM targets t
      JOIN source s ON s.geom && t.bounds
      GROUP BY t.id
    )
"""

REPORT = CLIP + """
    SELECT t.slug,
           CASE WHEN c.geom IS NULL OR ST_IsEmpty(c.geom) THEN 0
                ELSE ST_Area(c.geom) / nullif(ST_Area(t.bounds), 0) END AS fraction,
           CASE WHEN c.geom IS NULL OR ST_IsEmpty(c.geom) THEN 0
                ELSE ST_NPoints(c.geom) END AS vertices
    FROM targets t
    LEFT JOIN clipped c USING (id)
    ORDER BY t.slug
"""

APPLY = CLIP + """
    UPDATE raster_layers.raster r
    SET footprint = c.geom, updated_at = now()
    FROM clipped c
    WHERE c.id = r.id AND NOT ST_IsEmpty(c.geom)
"""


def report_sql(source: FootprintSource) -> str:
    return REPORT.format(source=source.sql)


def apply_sql(source: FootprintSource) -> str:
    return APPLY.format(source=source.sql)
