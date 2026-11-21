"""Deriving a categorical raster's class vocabulary.

A classification map stores integers; the meaning of those integers lives in
metadata that no two producers write the same way. The EMIT mineral maps carry a
`MINERAL_CLASSES` band-metadata item holding a Python-literal dict, plus a
paletted `.qml` sidecar, plus an embedded color table. The AVIRIS maps name
their classes as GDAL category names in an `.aux.xml` sidecar. Other sources
will differ.

Rather than teach the serving side about any of that, resolution happens once at
ingest and the *result* is stored on the layer. Everything here turns some source
of labels into `RasterCategory` list; the caller decides which source to use.
"""

import ast
import json
from pathlib import Path
from typing import Any, Iterator, Optional, Union
from xml.etree import ElementTree

from macrostrat.utils import get_logger

from .defs import RasterCategory

log = get_logger(__name__)

__all__ = [
    "NoClassVocabulary",
    "categories_from_mapping",
    "categories_from_info",
    "categories_from_aux_xml",
    "categories_from_qml",
    "categories_from_json",
    "class_metadata_candidates",
]

# The metadata items that have been seen to carry a class vocabulary. Used only
# to order candidates when no key is given — an unrecognized name that parses
# correctly is still accepted.
KNOWN_KEYS = ("MINERAL_CLASSES", "CLASS_NAMES", "CLASSES", "CATEGORIES")


class NoClassVocabulary(ValueError):
    """A source names no classes at all, so another source may be tried."""


def categories_from_mapping(
    mapping: dict,
    colormap: Optional[dict] = None,
) -> list[RasterCategory]:
    """Build a vocabulary from a `{value: label}` mapping and a color table.

    Values are coerced to ints (JSON object keys arrive as strings) and sorted,
    so the stored vocabulary reads in class order regardless of how the source
    happened to be written.
    """
    colors = {int(k): tuple(v) for k, v in (colormap or {}).items()}
    categories = []
    for key, label in mapping.items():
        value = int(key)
        color = colors.get(value)
        categories.append(
            RasterCategory(
                value=value,
                label=str(label).strip(),
                color=_rgba(color),
            )
        )
    return sorted(categories, key=lambda c: c.value)


def class_metadata_candidates(info: dict) -> dict[str, dict]:
    """Band-metadata items that parse as a class vocabulary, keyed by name.

    GDAL metadata values are strings, so a vocabulary arrives as something like
    `"{1: 'Kaolinite', 2: 'Alunite'}"`. Parsed with `ast.literal_eval` — the
    values are Python literals and nothing else should be evaluated.
    """
    candidates = {}
    for key, value in _band_metadata_items(info):
        mapping = _parse_mapping(value)
        if mapping is not None:
            candidates[key] = mapping
    return candidates


def categories_from_info(
    info: dict,
    colormap: Optional[dict] = None,
    *,
    key: Optional[str] = None,
) -> list[RasterCategory]:
    """Build a vocabulary from a raster's band metadata and color table.

    `info` is the reader metadata the index already stores per raster
    (`RasterInfo.metadata`, persisted in `raster.info`), so this normally costs
    no file access at all.

    With no `key`, the single metadata item that parses as a `{int: str}` mapping
    is used. Several candidates is an error naming them rather than a guess —
    picking the wrong vocabulary would be silently wrong everywhere downstream.
    """
    candidates = class_metadata_candidates(info)
    if not candidates:
        raise NoClassVocabulary(
            "No band-metadata item parses as a class vocabulary. "
            f"Available items: {sorted(_metadata_keys(info)) or 'none'}"
        )

    if key is not None:
        if key not in candidates:
            raise ValueError(
                f"Band metadata has no class vocabulary under {key!r}. "
                f"Candidates: {sorted(candidates)}"
            )
        chosen = key
    elif len(candidates) == 1:
        chosen = next(iter(candidates))
    else:
        known = [k for k in KNOWN_KEYS if k in candidates]
        if len(known) != 1:
            raise ValueError(
                "Several band-metadata items look like a class vocabulary; "
                f"pass one explicitly: {sorted(candidates)}"
            )
        chosen = known[0]

    log.info("Reading class vocabulary from band metadata %s", chosen)
    return categories_from_mapping(candidates[chosen], colormap)


def categories_from_aux_xml(
    source: Union[str, Path],
    colormap: Optional[dict] = None,
    *,
    band: int = 1,
) -> list[RasterCategory]:
    """Build a vocabulary from the category names in a GDAL `.aux.xml` sidecar.

    GDAL keeps a band's `CategoryNames` in this persistent-metadata file, and
    rasterio exposes them nowhere, so the file is read directly. The n-th name
    labels value n; an empty name leaves that value unlabeled. A color table in
    the same band wins over `colormap`, as GDAL would apply it to the raster.
    `source` is a local path or an `http(s)` URL.
    """
    try:
        root = ElementTree.fromstring(_read_text(source))
    except ElementTree.ParseError as err:
        raise ValueError(f"{source} is not readable XML: {err}") from err

    element = root.find(f"PAMRasterBand[@band='{band}']")
    if element is None:
        raise NoClassVocabulary(f"{source} has no metadata for band {band}")

    names = [(c.text or "").strip() for c in element.iterfind("CategoryNames/Category")]
    mapping = {value: name for value, name in enumerate(names) if name}
    if not mapping:
        raise NoClassVocabulary(f"{source} has no category names for band {band}")

    table = _pam_color_table(element)
    return categories_from_mapping(mapping, table or colormap)


def categories_from_qml(path: Union[str, Path]) -> list[RasterCategory]:
    """Build a vocabulary from a QGIS `.qml` paletted style.

    The sidecar route, for sources that label their classes in a style file and
    not in the raster. Colors come from the style rather than the color table,
    since that is the whole point of having one.
    """
    tree = ElementTree.parse(str(path))
    categories = []
    for entry in tree.iter("paletteEntry"):
        value = entry.get("value")
        if value is None:
            continue
        label = entry.get("label") or str(value)
        categories.append(
            RasterCategory(
                value=int(float(value)),
                label=label.strip(),
                color=_parse_hex_color(entry.get("color"), entry.get("alpha")),
            )
        )
    if not categories:
        raise ValueError(f"{path} has no `paletteEntry` elements")
    return sorted(categories, key=lambda c: c.value)


def categories_from_json(
    path: Union[str, Path],
    colormap: Optional[dict] = None,
) -> list[RasterCategory]:
    """Build a vocabulary from a JSON file.

    The escape hatch: a curated vocabulary that needs no raster at all, and can
    be corrected without touching data. Accepts either a `{value: label}` object
    or a list of `RasterCategory` objects.
    """
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        return categories_from_mapping(data, colormap)
    categories = [RasterCategory(**item) for item in data]
    return sorted(categories, key=lambda c: c.value)


# -- Internals -------------------------------------------------------------


def _band_metadata_items(info: dict) -> Iterator[tuple[str, Any]]:
    """Every `(key, value)` in a reader's per-band metadata.

    rio-tiler reports `band_metadata` as `[(band_name, {...}), ...]`. Bands are
    flattened together: a single-band classification is the case that matters,
    and a vocabulary shared across bands is the same vocabulary.
    """
    for entry in info.get("band_metadata") or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        metadata = entry[1]
        if isinstance(metadata, dict):
            yield from metadata.items()


def _metadata_keys(info: dict) -> list[str]:
    return [key for key, _ in _band_metadata_items(info)]


def _parse_mapping(value: Any) -> Optional[dict]:
    """A `{int: str}` mapping from a metadata value, or None if it isn't one."""
    if isinstance(value, dict):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text.startswith("{"):
            return None
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return None
    else:
        return None

    if not isinstance(parsed, dict) or not parsed:
        return None
    try:
        return {int(k): str(v) for k, v in parsed.items()}
    except (TypeError, ValueError):
        return None


def _read_text(source: Union[str, Path]) -> str:
    text = str(source)
    if text.startswith(("http://", "https://")):
        from urllib.request import urlopen

        with urlopen(text, timeout=30) as response:  # noqa: S310 - caller-supplied URL
            return response.read().decode("utf-8")
    return Path(source).read_text()


def _pam_color_table(band: ElementTree.Element) -> Optional[dict[int, tuple]]:
    """A PAM band's `<ColorTable>`, indexed by entry position."""
    entries = band.findall("ColorTable/Entry")
    if not entries:
        return None
    return {
        value: tuple(int(entry.get(f"c{i}", 255)) for i in range(1, 5))
        for value, entry in enumerate(entries)
    }


def _rgba(color: Optional[tuple]) -> Optional[tuple[int, int, int, int]]:
    """Pad an RGB triple to RGBA, as GDAL color tables may omit alpha."""
    if color is None:
        return None
    values = [int(v) for v in color]
    if len(values) == 3:
        values.append(255)
    return tuple(values[:4])


def _parse_hex_color(
    color: Optional[str], alpha: Optional[str]
) -> Optional[tuple[int, int, int, int]]:
    if not color:
        return None
    text = color.lstrip("#")
    if len(text) not in (6, 8):
        return None
    rgb = [int(text[i : i + 2], 16) for i in (0, 2, 4)]
    if len(text) == 8:
        return (*rgb, int(text[6:8], 16))
    return (*rgb, int(alpha) if alpha is not None else 255)
