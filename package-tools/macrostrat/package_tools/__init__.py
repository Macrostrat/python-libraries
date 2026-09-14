from typer import Typer

from .commands import apply_versions, create_changeset
from .install import install_packages
from .publish import build_packages, publish_packages
from .status import show_status

mono = Typer(no_args_is_help=True)
mono.command(name="install")(install_packages)
mono.command(name="status")(show_status)
mono.command(name="changeset")(create_changeset)
mono.command(name="version")(apply_versions)
mono.command(name="build")(build_packages)
mono.command(name="publish")(publish_packages)
