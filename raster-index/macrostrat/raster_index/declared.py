"""Checking a declared raster against the file it describes.

A declaration is an assumption about a published product. Its failure mode is
silent — one reprocessed tile with a different nodata, and the index describes a
raster that no longer exists — so a declaration must be *checked against a
sample* rather than assumed. This module is the comparison; `RasterIndex.verify_sample`
picks the sample and opens the files.
"""

import math
from dataclasses import dataclass
from typing import Any, Optional

from .defs import RasterInfo

__all__ = ["Mismatch", "compare_declared", "VerificationReport"]

# How far a declared bound may sit from the file's own before it counts as
# wrong. Generous against floating-point drift, tight against a mislabeled tile
# (one SRTM pixel is 1/3600°).
BOUNDS_TOLERANCE = 1e-6


@dataclass(frozen=True)
class Mismatch:
    """One field where the index and the file disagree."""

    slug: str
    href: str
    field: str
    declared: Any
    actual: Any

    def __str__(self) -> str:
        return f"{self.slug}: {self.field} declared {self.declared!r}, file says {self.actual!r}"


@dataclass
class VerificationReport:
    """What a sample verification found."""

    layer: str
    checked: int
    mismatches: list[Mismatch]
    # Rasters that could not be opened at all, with the error.
    unreadable: list[tuple[str, str]]

    @property
    def ok(self) -> bool:
        return not self.mismatches and not self.unreadable


def compare_declared(row: dict[str, Any], info: RasterInfo) -> list[Mismatch]:
    """Every field on which an indexed row disagrees with a freshly read file.

    `row` is a raster row from the index (`RasterIndex.rasters` shape).
    Compared: dtype, band count, CRS, the file's nodata and the native zoom
    range. The footprint is checked for *containment*, not equality: it may be
    far tighter than the file, but it must never claim ground outside it.
    """
    slug, href = row["slug"], row["href"]
    found: list[Mismatch] = []

    def check(field: str, declared: Any, actual: Any, same) -> None:
        if not same(declared, actual):
            found.append(Mismatch(slug, href, field, declared, actual))

    check("dtype", row.get("dtype"), info.dtype, lambda a, b: a == b)
    check("nbands", row.get("nbands"), info.nbands, lambda a, b: a == b)
    check("crs", row.get("crs"), info.crs, _same_crs)
    check("nodata", row.get("nodata"), info.nodata, _same_float)
    check("minzoom", row.get("minzoom"), info.minzoom, lambda a, b: a == b)
    check("maxzoom", row.get("maxzoom"), info.maxzoom, lambda a, b: a == b)
    bounds = row.get("bounds")
    if bounds is not None and not _within(bounds, info.bounds):
        found.append(
            Mismatch(slug, href, "footprint", tuple(bounds), tuple(info.bounds))
        )
    return found


def _same_float(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return math.isclose(a, b, rel_tol=0, abs_tol=1e-9)


def _same_crs(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return a.upper() == b.upper()


def _within(footprint_bounds, file_bounds) -> bool:
    """Whether a footprint's envelope lies inside the file's extent."""
    west, south, east, north = footprint_bounds
    fw, fs, fe, fn = file_bounds
    t = BOUNDS_TOLERANCE
    return west >= fw - t and south >= fs - t and east <= fe + t and north <= fn + t
