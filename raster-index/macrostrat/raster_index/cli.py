"""Command-line surface for the raster index.

A plain module-level Typer app. The database is a parameter of the app itself —
`--database`, or the `RASTER_INDEX_DATABASE`/`DATABASE_URL` environment
variables — resolved once in the callback and handed to commands through the
Click context.

A host application that already knows its own connection (Macrostrat mounts
these commands as `macrostrat raster`) calls `set_default_connection()` to supply
it, and its users never have to pass `--database`.
"""

from dataclasses import dataclass
from typing import Callable, Optional, Union

from rich import print
from rich.table import Table
from typer import Argument, BadParameter, Context, Exit, Option, Typer

from .defs import LayerDefinition
from .index import Connectable, RasterIndex
from .scan import scan_prefix

__all__ = ["cli", "set_default_connection", "index_for"]

# A connection, or something that produces one when a command actually needs it.
ConnectionSource = Union[Connectable, Callable[[], Connectable]]

# Set by a host application to supply its own database. Lower priority than an
# explicit `--database` so a user can always point the CLI somewhere else.
_default_connection: Optional[ConnectionSource] = None


def set_default_connection(source: Optional[ConnectionSource]) -> None:
    """Supply the connection to use when `--database` isn't given.

    Accepts a URL, an engine or `Database`, or a zero-argument callable
    returning one — a callable defers reading the host's configuration until a
    command actually runs.
    """
    global _default_connection
    _default_connection = source


@dataclass
class RasterIndexContext:
    """CLI state, attached to the Click context by the callback.

    The index is built on first use rather than in the callback, so `--help` and
    argument errors don't require a working database.
    """

    connection: Optional[ConnectionSource] = None
    _index: Optional[RasterIndex] = None

    @property
    def index(self) -> RasterIndex:
        if self._index is not None:
            return self._index

        source = self.connection
        if callable(source) and not isinstance(source, str):
            source = source()
        if source is None:
            raise BadParameter(
                "No database configured. Pass --database, or set "
                "RASTER_INDEX_DATABASE (or DATABASE_URL).",
                param_hint="--database",
            )

        self._index = RasterIndex(source)
        return self._index


def index_for(ctx: Context, *, require_schema: bool = True) -> RasterIndex:
    """The index for the running command.

    The schema check turns the common first-run mistake — a database that's
    never been provisioned — into a pointed message rather than an
    `UndefinedTable` traceback from whichever query happened to run first.
    """
    index = ctx.find_object(RasterIndexContext).index
    if require_schema and not index.schema_exists():
        print(
            "[yellow]No [bold]raster_layers[/bold] schema in this database.\n"
            "[dim]Apply it through your schema-management system, or run "
            "`create-schema` for a scratch database."
        )
        raise Exit(1)
    return index


cli = Typer(no_args_is_help=True, short_help="Manage indexed raster datasets")


@cli.callback()
def main(
    ctx: Context,
    database: Optional[str] = Option(
        None,
        "--database",
        envvar=["RASTER_INDEX_DATABASE", "DATABASE_URL"],
        help="PostgreSQL connection string for the raster index",
        show_default=False,
    ),
):
    """Manage indexed raster datasets."""
    ctx.obj = RasterIndexContext(connection=database or _default_connection)


@cli.command(name="create-schema")
def create_schema(ctx: Context):
    """Apply the `raster_layers` schema directly.

    For scratch databases. Managed deployments should build the schema through
    their own schema-management system.
    """
    index_for(ctx, require_schema=False).create_schema()
    print("[green]Created [bold]raster_layers[/bold] schema")


@cli.command(name="layers")
def list_layers(ctx: Context):
    """List indexed raster layers."""
    index = index_for(ctx)

    counts = {}
    for raster in index.rasters():
        counts[raster["layer"]] = counts.get(raster["layer"], 0) + 1

    table = Table("Layer", "Name", "Rasters", "Zooms", box=None)
    for layer in index.layers():
        table.add_row(
            layer.slug,
            layer.name or "",
            str(counts.get(layer.slug, 0)),
            _zoom_range(layer.minzoom, layer.maxzoom),
        )
    print(table)


@cli.command(name="rasters")
def list_rasters(ctx: Context, layer: Optional[str] = Argument(None)):
    """List indexed rasters, optionally within a single layer."""
    table = Table("Layer", "Raster", "Zooms", "Type", "Href", box=None)
    for raster in index_for(ctx).rasters(layer):
        table.add_row(
            raster["layer"],
            raster["slug"],
            _zoom_range(raster["minzoom"], raster["maxzoom"]),
            f"{raster['dtype']}×{raster['nbands']}",
            raster["href"],
        )
    print(table)


@cli.command(name="define-layer")
def define_layer(
    ctx: Context,
    slug: str,
    name: Optional[str] = Option(None),
    description: Optional[str] = Option(None),
    minzoom: Optional[int] = Option(None),
    maxzoom: Optional[int] = Option(None),
):
    """Create or update a layer definition."""
    index_for(ctx).register_layer(
        LayerDefinition(
            slug=slug,
            name=name,
            description=description,
            minzoom=minzoom,
            maxzoom=maxzoom,
        )
    )
    print(f"[green]Defined layer [bold]{slug}[/bold]")


@cli.command(name="set-colormap")
def set_colormap(
    ctx: Context,
    layer: str,
    source: str = Option(
        ...,
        "--from",
        help="Raster whose embedded palette should become the layer's colormap",
    ),
):
    """Copy a raster's embedded color table onto its layer.

    Categorical rasters (classification maps) carry their palette in the file.
    Serving them means having that palette at render time, which is what the
    layer's colormap is for.
    """
    from .footprints import get_raster_info

    colormap = get_raster_info(source).colormap
    if not colormap:
        print(f"[yellow]{source} has no embedded color table")
        raise Exit(1)

    index = index_for(ctx)
    existing = {l.slug: l for l in index.layers()}
    if layer not in existing:
        print(f"[yellow]Layer [bold]{layer}[/bold] is not defined")
        raise Exit(1)

    # Stored as JSON, so keys become strings and tuples become lists; the
    # serving side normalizes them back.
    index.register_layer(
        existing[layer],
        colormap={str(k): list(v) for k, v in colormap.items()},
    )
    print(f"[green]Set a {len(colormap)}-entry colormap on [bold]{layer}[/bold]")


@cli.command(name="set-categories")
def set_categories(
    ctx: Context,
    layer: str,
    source: Optional[str] = Option(
        None,
        "--from",
        help="Raster (href or indexed slug) whose band metadata names the classes",
    ),
    from_json: Optional[str] = Option(
        None,
        "--from-json",
        help="JSON file holding a {value: label} mapping or a list of categories",
    ),
    qml: Optional[str] = Option(
        None, "--qml", help="QGIS .qml paletted style naming the classes"
    ),
    metadata_key: Optional[str] = Option(
        None,
        "--metadata-key",
        help="Band-metadata item to read (e.g. MINERAL_CLASSES). Inferred if omitted.",
    ),
):
    """Give a categorical layer a class vocabulary — names for its integers.

    Classification maps address classes by integer, which is no way to ask for a
    mineral. Resolving names once at ingest means a tile request can name what it
    wants (`?algorithm=classes&algorithm_params={"classes":["Kaolinite"]}`) and a
    client can draw a legend without opening a raster.

    Labels come from the raster's GDAL band metadata by default — the same thing
    `raster info --metadata` and `gdalinfo` print — joined to its color table.
    `--qml` and `--from-json` cover sources that label their classes elsewhere,
    or a vocabulary that needs correcting without touching data.
    """
    from .categories import (
        categories_from_info,
        categories_from_json,
        categories_from_qml,
    )

    sources = [s for s in (source, from_json, qml) if s is not None]
    if len(sources) != 1:
        raise BadParameter("Pass exactly one of --from, --from-json or --qml")

    index = index_for(ctx)
    existing = {l.slug: l for l in index.layers()}
    if layer not in existing:
        print(f"[yellow]Layer [bold]{layer}[/bold] is not defined")
        raise Exit(1)

    colormap = existing[layer].colormap

    try:
        if from_json is not None:
            categories = categories_from_json(from_json, colormap)
        elif qml is not None:
            categories = categories_from_qml(qml)
        else:
            info, table = _raster_metadata(index, source, layer)
            categories = categories_from_info(info, table or colormap, key=metadata_key)
    except (ValueError, OSError) as err:
        print(f"[red]{err}")
        raise Exit(1)

    index.set_categories(layer, categories)

    table_view = Table("Value", "Class", "Color")
    for category in categories:
        color = "" if category.color is None else str(tuple(category.color))
        table_view.add_row(str(category.value), category.label, color)
    print(table_view)
    print(f"[green]Set a {len(categories)}-class vocabulary on [bold]{layer}[/bold]")


def _raster_metadata(
    index: RasterIndex, source: str, layer: str
) -> tuple[dict, Optional[dict]]:
    """Reader metadata for a raster, from the index if it is already there.

    Registering a raster stores its full `src.info()` output, so a vocabulary can
    usually be derived without reopening a file in object storage. Falling back
    to reading it is what makes `--from` work for a raster that isn't indexed
    (or was indexed before this metadata was kept).
    """
    stored = index.raster_info(source, layer=layer)
    if stored and stored.get("band_metadata"):
        # The color table is deliberately not stored per raster, so a colormap
        # for the labels has to come from the layer.
        return stored, None

    from .footprints import get_raster_info

    info = get_raster_info(source)
    return info.metadata, info.colormap


@cli.command(name="add")
def add(
    ctx: Context,
    hrefs: list[str] = Argument(..., help="Raster URLs or paths"),
    layer: str = Option(..., "--layer", "-l", help="Layer to add rasters to"),
    mask_footprints: bool = Option(
        False,
        "--mask-footprints",
        help="Trace footprints from each raster's validity mask (reads the mask)",
    ),
    nodata: Optional[float] = Option(
        None,
        "--nodata",
        help="Reader nodata override, applied instead of the file's own value",
    ),
):
    """Register rasters in a layer, reading footprints and zoom ranges."""
    results = index_for(ctx).add_rasters(
        hrefs, layer, mask_footprint=mask_footprints, nodata=nodata
    )
    print(f"[green]Registered {len(results)}/{len(hrefs)} rasters in {layer}")


@cli.command(name="set-nodata")
def set_nodata(
    ctx: Context,
    layer: str,
    value: Optional[float] = Argument(
        None, help="The value to treat as missing; omit with --clear"
    ),
    slug: Optional[str] = Option(
        None, "--slug", "-s", help="One raster rather than the whole layer"
    ),
    clear: bool = Option(False, "--clear", help="Remove the override"),
):
    """Override what the reader treats as nodata, for a layer or one raster.

    The file's own nodata stays recorded; this is the value the serving side
    passes to the reader instead. SRTM GL1 declares -32768 but stores the ocean
    as 0, so `set-nodata srtm-gl1 0` is what makes a click in the water fall
    through to bathymetry rather than returning sea level.
    """
    if clear == (value is not None):
        raise BadParameter("Pass a value, or --clear, but not both")
    count = index_for(ctx).set_nodata(
        layer, None if clear else value, rasters=[slug] if slug else None
    )
    verb = "Cleared the nodata override on" if clear else f"Set nodata {value!r} on"
    print(f"[green]{verb} {count} raster(s) in [bold]{layer}[/bold]")


@cli.command(name="set-footprints")
def set_footprints(
    ctx: Context,
    layer: str,
    table: Optional[str] = Option(
        None, "--from", help="PostGIS table or view of polygons, e.g. public.land"
    ),
    geometry_column: str = Option(
        "geom", "--geometry-column", help="Geometry column of --from"
    ),
    file: Optional[str] = Option(None, "--from-file", help="A GeoJSON file"),
    url: Optional[str] = Option(None, "--from-url", help="A GeoJSON URL"),
    slug: Optional[str] = Option(
        None, "--slug", "-s", help="One raster rather than the whole layer"
    ),
    dry_run: bool = Option(False, "--dry-run", help="Report without writing"),
):
    """Clip footprints to an external geometry, without reading any raster.

    Each footprint becomes `bounds ∩ source`: a land-polygon table for a global
    terrestrial product, a publisher's survey boundaries for local ones. Always
    recomputed from the bounds, so it is safe to run again; a raster the source
    does not touch is reported and left alone.
    """
    from .external_footprints import FootprintSource

    sources = [s for s in (table, file, url) if s is not None]
    if len(sources) != 1:
        raise BadParameter("Pass exactly one of --from, --from-file or --from-url")
    if table is not None:
        source = FootprintSource.table(table, geometry_column)
    elif file is not None:
        source = FootprintSource.file(file)
    else:
        source = FootprintSource.url(url)

    report = index_for(ctx).set_footprints(
        layer, source, rasters=[slug] if slug else None, apply=not dry_run
    )
    if not report.rows:
        print(f"[yellow]No rasters in [bold]{layer}[/bold]")
        raise Exit(1)

    view = Table("Raster", "Coverage of bounds", "Vertices", box=None)
    for row in report.clipped:
        view.add_row(row.slug, f"{100 * row.fraction:.1f}%", str(row.vertices))
    if report.clipped:
        print(view)

    verb = "Would clip" if dry_run else "Clipped"
    summary = (
        f"[green]{verb} {len(report.clipped)} footprint(s) against {report.source}"
    )
    if report.whole:
        summary += f"; {len(report.whole)} lie entirely inside it (bounds kept)"
    print(summary)
    if report.empty:
        print(
            f"[yellow]{len(report.empty)} raster(s) are entirely outside the source "
            "and were left alone:"
        )
        for row in report.empty:
            print(f"  {row.slug}")


@cli.command(name="verify")
def verify(
    ctx: Context,
    layer: str,
    sample: int = Option(10, "--sample", "-n", help="How many rasters to open"),
    seed: Optional[int] = Option(None, "--seed", help="Make the sample repeatable"),
):
    """Open a random sample of a layer's rasters and compare them to the index.

    The check behind a declared registration: a profile is an assumption about
    a published product, and one reprocessed tile with a different nodata would
    otherwise go unnoticed. Exits non-zero on any disagreement.
    """
    report = index_for(ctx).verify_sample(layer, sample=sample, seed=seed)
    for slug, error in report.unreadable:
        print(f"[red]{slug}: could not be opened: {error}")
    for mismatch in report.mismatches:
        print(f"[red]{mismatch}")
    if report.ok:
        print(
            f"[green]{report.checked} raster(s) in [bold]{layer}[/bold] match the index"
        )
        return
    print(
        f"[red]{len(report.mismatches)} mismatch(es) and {len(report.unreadable)} "
        f"unreadable across {report.checked} sampled raster(s)"
    )
    raise Exit(1)


@cli.command(name="refine-footprints")
def refine_footprints(
    ctx: Context,
    layer: str,
    slug: Optional[str] = Option(
        None, "--slug", "-s", help="Refine a single raster instead of the whole layer"
    ),
    max_size: int = Option(
        1024, "--max-size", help="Longest edge, in pixels, of the decimated mask read"
    ),
    max_vertices: int = Option(
        500, "--max-vertices", help="Vertex budget for the stored geometry"
    ),
    dry_run: bool = Option(
        False, "--dry-run", help="Report what would change without writing"
    ),
):
    """Trace footprints from each raster's validity mask.

    A bounding box is a cheap footprint and a poor one: a diagonal swath inside
    its own bbox is selected — and opened over the network — by every tile in the
    box, most of which contain nothing but nodata. This replaces the box with the
    shape of the data, so those tiles stop selecting the raster at all.

    It reads each raster's mask (decimated, through overviews where they exist),
    so it is a deliberate step rather than part of registration. Re-runnable: the
    footprint is derived from the file, never from the stored geometry, so a
    second run with different settings simply replaces it.
    """
    from .mask_footprint import mask_footprint

    index = index_for(ctx)
    rasters = index.rasters(layer)
    if slug is not None:
        rasters = [r for r in rasters if r["slug"] == slug]
    if not rasters:
        print(f"[yellow]No rasters to refine in [bold]{layer}[/bold]")
        raise Exit(1)

    table = Table("Raster", "Coverage of bbox", "Vertices", "Parts")
    refined = 0
    unchanged = 0
    failed = 0

    for raster in rasters:
        try:
            result = mask_footprint(
                raster["href"], max_size=max_size, max_vertices=max_vertices
            )
        except Exception as err:
            # One unreadable raster shouldn't abandon the rest of the layer.
            print(f"[red]{raster['slug']}: {err}")
            failed += 1
            continue

        if result is None:
            # Fills its own bounding box; a traced rectangle would only add
            # vertices for the selection query to test against.
            unchanged += 1
            table.add_row(raster["slug"], "100% (bbox kept)", "—", "—")
            continue

        table.add_row(
            raster["slug"],
            f"{100 * result.area_fraction:.1f}%",
            str(result.vertices),
            str(result.parts),
        )
        if not dry_run:
            index.update_footprint(raster["href"], result.geometry)
        refined += 1

    print(table)
    verb = "Would refine" if dry_run else "Refined"
    summary = f"[green]{verb} {refined} footprint(s)"
    if unchanged:
        summary += f"; {unchanged} already fill their bounding box"
    if failed:
        summary += f"; [red]{failed} failed"
    print(summary)


@cli.command(name="scan")
def scan(
    ctx: Context,
    url: str = Argument(
        ...,
        help="Bucket prefix, e.g. https://storage.example.org/bucket/prefix/",
    ),
    layer: str = Option(..., "--layer", "-l", help="Layer to add rasters to"),
    dry_run: bool = Option(False, help="Show what would be registered"),
    credentials: bool = Option(False, help="Sign requests with AWS credentials"),
    endpoint_url: Optional[str] = Option(
        None, help="S3-compatible endpoint to list against (inferred from https URLs)"
    ),
    public_url: Optional[str] = Option(
        None, help="Rewrite hrefs onto this origin (inferred from https URLs)"
    ),
    mask_footprints: bool = Option(
        False,
        "--mask-footprints",
        help=(
            "Trace footprints from each raster's validity mask rather than using "
            "its bounding box. Reads each mask, so a large bucket takes a while; "
            "`refine-footprints` does the same job after the fact."
        ),
    ),
    nodata: Optional[float] = Option(
        None,
        "--nodata",
        help="Reader nodata override, applied instead of each file's own value",
    ),
):
    """Register every raster under a bucket prefix.

    An `https://` URL — the one you'd paste into a browser — needs nothing else:
    its origin is the endpoint, its first path segment the bucket, and the
    rasters are indexed at that same origin. Use `s3://` (with `--endpoint-url`)
    for buckets whose location isn't implied by a public URL. Listing is
    unsigned unless `--credentials` is given.
    """
    objects = list(
        scan_prefix(
            url,
            endpoint_url=endpoint_url,
            public_url=public_url,
            credentials=credentials,
        )
    )
    if not objects:
        print(f"[yellow]No rasters found under {url}")
        return

    if dry_run:
        for obj in objects:
            print(f"{obj.href} [dim]({obj.size / 1e6:.1f} MB)")
        print(f"[dim]{len(objects)} rasters (dry run — nothing registered)")
        return

    results = index_for(ctx).add_rasters(
        [o.href for o in objects], layer, mask_footprint=mask_footprints, nodata=nodata
    )
    print(f"[green]Registered {len(results)}/{len(objects)} rasters in {layer}")


@cli.command(name="remove")
def remove(ctx: Context, href: str):
    """Remove a raster from the index."""
    count = index_for(ctx).remove_raster(href)
    if count:
        print(f"[green]Removed {href}")
    else:
        print(f"[yellow]{href} is not in the index")


@cli.command(name="info")
def info(
    href: str,
    metadata: bool = Option(
        False,
        "--metadata",
        "--full",
        help="Include full reader metadata, including per-band metadata",
    ),
):
    """Show the metadata that would be indexed for a raster.

    Reads the raster directly, so it needs no database.

    `--metadata` adds the full reader output. That is where a classification
    map's class names live (as a band-metadata item), so it is the way to find
    out what `set-categories` has to work with — candidate vocabularies are
    called out explicitly.
    """
    from .categories import class_metadata_candidates
    from .footprints import get_raster_info

    info = get_raster_info(href)
    print(info.model_dump(exclude={"metadata"}))
    if not metadata:
        return

    print(info.metadata)
    candidates = class_metadata_candidates(info.metadata)
    if not candidates:
        print("[dim]No band-metadata item parses as a class vocabulary")
        return
    for key, mapping in candidates.items():
        print(f"[green]Class vocabulary [bold]{key}[/bold] ({len(mapping)} classes)")


@cli.command(name="assets")
def assets(
    ctx: Context,
    z: int,
    x: int,
    y: int,
    layers: list[str] = Option(..., "--layer", "-l"),
):
    """Show which rasters would be read for a tile."""
    index = index_for(ctx)

    table = Table("Layer", "Raster", "Zooms", "Overscaled", box=None)
    for asset in index.assets_for_tile(x, y, z, layers):
        table.add_row(
            asset.layer,
            asset.slug or "",
            _zoom_range(asset.minzoom, asset.maxzoom),
            "yes" if asset.overscaled else "",
        )
    print(table)

    if not index.should_generate_tile(x, y, z, layers):
        print("[yellow]No usable assets — this tile should not be generated")


def _zoom_range(minzoom, maxzoom) -> str:
    if minzoom is None and maxzoom is None:
        return ""
    low = minzoom if minzoom is not None else "?"
    high = maxzoom if maxzoom is not None else "?"
    return f"{low}–{high}"
