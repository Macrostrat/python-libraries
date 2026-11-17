"""The `env` command for an application that keeps environments."""

from typing import Optional

from rich import print
from rich.table import Table
from typer import Argument, Context, Option, Typer

from .policy import WRITE_SCOPES, AccessScope, format_duration


def create_env_cli(environments) -> Typer:
    name = environments.command_name
    cli = Typer(
        name="env",
        help="Show or set the active environment",
        invoke_without_command=True,
        no_args_is_help=False,
    )

    @cli.callback()
    def env(
        ctx: Context,
        env: Optional[str] = Argument(None, help="Environment to activate"),
        unset: bool = Option(False, "--unset", help="Forget the remembered one"),
        shell: bool = Option(
            False,
            "--shell",
            help="Print export lines instead of remembering. "
            f'Use as: eval "$({name} env --shell staging)"',
        ),
        list_all: bool = Option(False, "--list", "-l", help="List environments"),
    ):
        """Show or set the active environment

        A `local`-class environment is remembered indefinitely. Any other is
        remembered for its class's time-to-live (8 h development, 1 h staging,
        15 min production; `active_ttl` overrides it). A lapsed environment is
        kept, and you are asked before the next command that would use it.
        """
        if unset:
            environments.forget()
            print("Forgot the remembered environment.")
            return
        if list_all:
            print(_listing(environments))
            return
        if env is None:
            _report(environments)
            return
        if shell:
            # Nothing is persisted: the environment dies with the shell.
            for line in environments.shell_exports(env):
                print(line)
            return

        expires = environments.select(env)
        policy = environments.policy_for(env)
        klass = policy.env_class.value
        if expires is None:
            print(f"Activated [bold cyan]{env}[/] [bold yellow]({klass})[/]")
            return
        print(
            f"Activated [bold cyan]{env}[/] [bold yellow]({klass})[/] "
            f"[dim]for {format_duration(policy.ttl)}, "
            f"until {expires.astimezone():%H:%M}[/dim]"
        )
        print(
            "[dim]Use --env for a single command, or "
            f'eval "$({name} env --shell {env})" for a shell session.[/dim]'
        )

    return cli


def _report(environments):
    name = environments.command_name
    active = environments.active
    if active is None:
        remembered = environments.state.remembered()
        if remembered is not None:
            where = remembered.config_file or "another config file"
            print(
                "No environment applies here. The remembered environment "
                f"[bold cyan]{remembered.name}[/] was set for [dim]{where}[/dim]."
            )
            return
        print(
            f"No environment set. Run [bold]{name} env <name>[/] to activate one; "
            f"[bold]{name} env --list[/] lists them."
        )
        return

    klass = environments.policy.env_class.value
    line = f"[bold cyan]{active.name}[/] [bold yellow]({klass})[/]"
    if active.source == "explicit":
        line += " [dim](--env or variable, this command only)[/dim]"
    elif active.expires is None:
        line += " [dim](does not lapse)[/dim]"
    elif active.lapsed:
        ago = format_duration(-active.remaining)
        line += (
            f" [yellow]lapsed {ago} ago[/yellow] [dim]— you will be asked before "
            f"it is used; `{name} env {active.name}` re-activates it[/dim]"
        )
    else:
        via = "remembered"
        if active.source == "shell":
            via = "shell session"
        line += f" [dim]({via}, lapses in {format_duration(active.remaining)})[/dim]"
    print(line)


def _listing(environments) -> Table:
    table = Table(title=str(environments.config_file or "No config file"))
    table.add_column("Environment", style="bold cyan")
    table.add_column("Class")
    for scope in (AccessScope.Read, *WRITE_SCOPES):
        table.add_column(f"Confirm {scope.value}")
    table.add_column("Remembered for")
    for env in environments.available():
        policy = environments.policy_for(env)
        klass = policy.env_class.value
        if policy.inferred:
            klass += " [dim](assumed)[/]"
        label = env
        if env == environments.env:
            label += " [green]●[/]"
        table.add_row(
            label,
            klass,
            *(policy.gate_for(s).value for s in (AccessScope.Read, *WRITE_SCOPES)),
            format_duration(policy.ttl),
        )
    return table
