"""Utilities for Typer and Click command-line interfaces."""

from os import environ
from typing import List, Optional

import typer
from click import Parameter
from typer import Context, Typer
from typer.core import TyperGroup

from macrostrat.utils import get_logger

log = get_logger(__name__)

DELIMITER = ", "


class CommandBase(Typer):
    def __init__(
        self,
        **kwargs,
    ):
        kwargs.setdefault("add_completion", False)
        kwargs.setdefault("no_args_is_help", True)
        kwargs.setdefault("cls", ControlCommandGroup)
        super().__init__(**kwargs)

    # Aliased command names
    def add_typer(self, typer, **kwargs):
        name = kwargs.pop("name", None)
        aliases = kwargs.pop("aliases", None)
        _name = self._name_modifier(name, aliases)
        if _name is not None:
            kwargs["name"] = _name
        super().add_typer(typer, **kwargs)

    def command(self, name=None, **kwargs):
        """Simple wrapper around command that replaces names in the docstring"""
        _name = self._name_modifier(name, kwargs.pop("aliases", None))
        return super().command(_name, **kwargs)

    def _name_modifier(self, name, aliases: list[str] = None):
        """Return a function that modifies the name for a command to include aliases
        https://github.com/fastapi/typer/issues/132
        """
        if aliases is None:
            return name
        if name is None:
            raise ValueError("name must be provided if aliases are set")
        if isinstance(name, str):
            return DELIMITER.join([name, *aliases])
        return lambda: DELIMITER.join([name(), *aliases])

    def add_command(self, cmd, *args, **kwargs):
        """Simple wrapper around command"""
        self.command(*args, **kwargs)(cmd)

    def add_click_command(self, cmd, *args, **kwargs):
        add_click_command(self, cmd, *args, **kwargs)


class ControlCommandGroup(TyperGroup):
    """A Typer group that lists commands in the order they were added, and
    also allows for aliases.
    """

    def get_command(self, ctx, cmd_name):
        cmd_name = self._group_cmd_name(cmd_name)
        return super().get_command(ctx, cmd_name)

    def _group_cmd_name(self, default_name):
        for cmd in self.commands.values():
            name = cmd.name
            if name and default_name in name.split(DELIMITER):
                return name
        return default_name

    def list_commands(self, ctx: Context):
        """Return list of commands in the order of appearance."""
        deprecated = []
        commands = []

        for name, command in self.commands.items():
            if command.deprecated:
                deprecated.append(name)
            else:
                commands.append(name)
        return commands + deprecated

    def get_params(self, ctx: Context) -> List[Parameter]:
        """Don't show the completion options in the help text, to avoid cluttering the output"""
        return [
            p
            for p in super().get_params(ctx)
            if not p.name in ("install_completion", "show_completion")
        ]


def add_click_command(base: Typer, cmd, *args, **kwargs):
    """Add a click command to a Typer app
    params:
        base: Typer
        cmd: callable
        args: arguments to pass to typer.command
        kwargs: keyword arguments to pass to typer.command
    """

    def _click_command(ctx: typer.Context):
        cmd(ctx.args)

    _click_command.__doc__ = cmd.__doc__

    kwargs["context_settings"] = {
        "allow_extra_args": True,
        "ignore_unknown_options": True,
        "help_option_names": [],
        **kwargs.get("context_settings", {}),
    }

    base.command(*args, **kwargs)(_click_command)


def get_env_boolean(envvar: str, default: Optional[bool] = None) -> Optional[bool]:
    """Get a boolean value from an environment variable"""
    val = environ.get(envvar, None)
    if val is None:
        return default
    val = val.lower()
    if val in ("1", "true", "yes", "on"):
        return True
    elif val in ("0", "false", "no", "off"):
        return False
    return default
