"""A `schema` command group for any application's command-line interface."""

from pathlib import Path
from typing import Callable, Optional, Union

import click
from rich import print
from rich.table import Table
from typer import Argument, Exit, Option, Typer

from .app import SchemaApp
from .chunks.composer import order_chunks
from .plan import is_unsafe_statement
from .sync import print_failures
from .sync.selection import ObjectSelector

AppSource = Union[SchemaApp, Callable[[], SchemaApp]]

TARGET_OPTION = Option(
    None,
    "--target",
    help="Restrict to a subsystem (chunk); its dependencies are included "
    "unless --no-dependents.",
)

NO_DEPENDENTS_OPTION = Option(
    False,
    "--no-dependents",
    help="With --target, act on only that chunk, not the chunks it depends on.",
)

OBJECT_OPTION = Option(
    None,
    "--object",
    "-o",
    help="Restrict to statements on objects matching SCHEMA or SCHEMA.NAME "
    "(globs allowed). Repeatable.",
)

YES_OPTION = Option(
    False, "--yes", "-y", help="Skip the confirmation prompt where one is allowed"
)


def create_schema_cli(source: AppSource, *, name: str = "schema") -> Typer:
    """Build the `schema` commands for an application.

    ``source`` is a :class:`SchemaApp` or a function returning one. A function
    is called when a command runs, so configuration is read at that point.
    """
    cli = Typer(name=name, help="Manage the database schema", no_args_is_help=True)

    def get_app() -> SchemaApp:
        if isinstance(source, SchemaApp):
            return source
        return source()

    @cli.command(rich_help_panel="Automated migrations")
    def plan():
        """Compare the database with the declarative schema"""
        app = get_app()
        out_file, statements = app.write_plan()
        n_unsafe = len([s for s in statements if is_unsafe_statement(s)])
        if n_unsafe > 0:
            print(f"[red dim]{n_unsafe} unsafe statements in diff")
        print(f"[dim]Wrote {len(statements)} proposed changes to [bold]{out_file}[/]")

    @cli.command(rich_help_panel="Automated migrations")
    def review(edit: bool = False):
        """Review the latest plan"""
        app = get_app()
        pending = app.plan_file
        if not pending.exists():
            print(f"[yellow]No plan found. Run [cyan]{app.name} {name} plan[/] first.")
            raise Exit(1)
        if edit:
            click.edit(filename=str(pending))
            print(f"[dim]Updated plan saved to [bold]{pending}[/]")
        else:
            print(pending.read_text())

    @cli.command(rich_help_panel="Automated migrations")
    def apply(
        plan_file: Optional[Path] = Argument(None),
        safe: bool = Option(True, "--safe/--unsafe"),
        archive: bool = Option(False, "--archive/--no-archive"),
        yes: bool = YES_OPTION,
    ):
        """Apply a plan to the database"""
        app = get_app()
        pending = plan_file or app.plan_file
        if not pending.exists():
            print(f"[yellow]No plan found. Run [cyan]{app.name} {name} plan[/] first.")
            raise Exit(1)
        counter = app.apply_plan(pending, safe=safe, assume_yes=yes)
        counter.print_report()
        if not archive:
            return
        applied_file = app.log_applied(counter)
        if applied_file is None:
            print("[dim]No statements to log.")
        else:
            print(f"[dim]Logged statements to [bold]{applied_file}[/]")

    @cli.command(rich_help_panel="Manual migrations")
    def migrate(
        migration: str = Argument(None),
        apply: bool = Option(False, "--apply/--no-apply"),
        force: bool = Option(False, "--force/--no-force"),
        data: bool = Option(False, "--data/--no-data"),
        show_applied: bool = Option(
            False, "--show-applied", help="List migrations that have already applied"
        ),
        yes: bool = YES_OPTION,
    ):
        """List pending migrations, and apply them with --apply"""
        app = get_app()
        # Without --apply this only lists, which needs no write authorization.
        if apply:
            app.require_write("schema migration", yes)
        app.runner().run(
            apply=apply,
            name=migration,
            force=force,
            data_changes=data,
            show_applied=show_applied,
        )

    @cli.command(rich_help_panel="Utils")
    def provision(
        target: str = TARGET_OPTION,
        no_dependents: bool = NO_DEPENDENTS_OPTION,
        yes: bool = YES_OPTION,
    ):
        """Apply the declarative schema to the database"""
        app = get_app()
        counter = app.provision(
            target=target, no_dependents=no_dependents, assume_yes=yes
        )
        counter.print_report()

    @cli.command(rich_help_panel="Utils")
    def sync(
        views: bool = Option(True, "--views/--no-views", help="Rebuild views"),
        procedures: bool = Option(
            True, "--procedures/--no-procedures", help="Rebuild functions/procedures"
        ),
        data: bool = Option(
            True, "--data/--no-data", help="Re-apply idempotent seed data"
        ),
        permissions: bool = Option(
            True, "--permissions/--no-permissions", help="Re-apply roles and grants"
        ),
        target: str = TARGET_OPTION,
        no_dependents: bool = NO_DEPENDENTS_OPTION,
        objects: Optional[list[str]] = OBJECT_OPTION,
        yes: bool = YES_OPTION,
    ):
        """Re-apply views, procedures, seed data and permissions

        Everything a schema diff cannot manage on its own, so that
        [cyan]provision[/] ≡ [cyan]plan[/] + [cyan]apply[/] + [cyan]sync[/].
        """
        app = get_app()
        only = None
        if objects:
            only = ObjectSelector.parse(objects)
        res = app.sync(
            target=target,
            no_dependents=no_dependents,
            only=only,
            assume_yes=yes,
            views=views,
            procedures=procedures,
            data=data,
            permissions=permissions,
        )
        if only is not None and res.total == 0:
            print(f"[yellow]No statements matched {', '.join(objects)}")
            return
        if res.procedures is not None:
            r = res.procedures
            _report("procedures", r.applied, len(r.failed))
            if r.recreated:
                print(f"[dim]  {len(r.recreated)} recreated with a changed signature")
        if res.views is not None:
            r = res.views
            extra = ""
            if r.recreated:
                extra = f", {len(r.recreated)} recreated"
            print(f"[dim]{r.replaced} views replaced{extra}")
            if r.restored:
                print(f"[dim]  {len(r.restored)} dependents restored after a cascade")
        if res.data is not None:
            _report("data statements", res.data.applied, len(res.data.failed))
        if res.permissions is not None:
            r = res.permissions
            _report("permission statements", r.applied, len(r.failed), len(r.skipped))
        if len(res.failures) > 0:
            print("\n[red bold]Failures:")
            print_failures(res.failures)

    @cli.command(rich_help_panel="Automated migrations")
    def graph():
        """Show schema chunks, their dependencies, and application order"""
        app = get_app()
        selected = app.environment_chunks()
        table = Table(
            title=f"Schema chunks — [bold cyan]{app.environment}[/]"
            f" [dim]({app.env_class.value})[/]"
        )
        table.add_column("#", justify="right", style="dim")
        table.add_column("Chunk", style="bold cyan")
        table.add_column("Owner")
        table.add_column("Depends on")
        table.add_column("Provides")
        table.add_column("Classes")
        for i, chunk in enumerate(order_chunks(selected), start=1):
            providers = [_describe_provider(p, app.source_root) for p in chunk.provides]
            classes = "[dim]all[/]"
            if chunk.environments is not None:
                ranked = sorted(chunk.environments, key=lambda c: c.rank)
                classes = ", ".join(c.value for c in ranked)
            table.add_row(
                str(i),
                chunk.name,
                chunk.owner or "[dim]connector[/]",
                ", ".join(chunk.depends_on) or "[dim]—[/]",
                "\n".join(providers) or "[dim]—[/]",
                classes,
            )
        print(table)
        names = {c.name for c in selected}
        excluded = [c.name for c in app.all_chunks() if c.name not in names]
        if excluded:
            print(
                f"[dim]Not applied in [bold]{app.environment}[/]: "
                f"{', '.join(excluded)}[/]"
            )

    return cli


def _report(label: str, applied: int, failed: int, skipped: int = 0):
    msg = f"[dim]{applied} {label} applied"
    if skipped:
        msg += f" ({skipped} already present)"
    if failed:
        msg += f" ([yellow]{failed} failed[/])"
    print(msg)


def _describe_provider(provider, root: Optional[Path]) -> str:
    if isinstance(provider, Path):
        if root is not None and provider.is_relative_to(root):
            return str(provider.relative_to(root))
        return str(provider)
    name = getattr(provider, "__name__", repr(provider))
    module = getattr(provider, "__module__", "")
    return f"{name}() [dim]{module}[/]"
