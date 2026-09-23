"""
Console progress reporting for ongoing work (e.g., SQL statements).

The standard pattern is to print a short description of a unit of work *before*
it starts, so that a hung task is always identifiable. If the work blocks for
longer than a short threshold, an activity indicator (a spinner and elapsed
time) is shown on the same line. When the work finishes, the line is replaced
with its final, styled form.

On a non-interactive output (a file, pipe, or CI log), the description is
printed once, up front, and no indicator is shown.
"""

import os
import threading
from time import monotonic
from typing import IO, Optional, Union

from rich.console import Console, ConsoleOptions, RenderResult
from rich.control import Control
from rich.live import Live
from rich.segment import ControlType
from rich.spinner import Spinner
from rich.text import Text

__all__ = ["ActivityIndicator", "activity", "get_console", "DEFAULT_DELAY"]


def _default_delay() -> float:
    try:
        return float(os.environ.get("MACROSTRAT_ACTIVITY_DELAY", 2.0))
    except ValueError:
        return 2.0


# Seconds to wait before showing an activity indicator for blocking work.
# Can be overridden with the MACROSTRAT_ACTIVITY_DELAY environment variable.
DEFAULT_DELAY = _default_delay()


def get_console(file: Optional[IO] = None) -> Console:
    """Get a console suitable for writing progress information to ``file``
    (standard error by default)."""
    return Console(file=file, stderr=file is None, highlight=False)


def _is_interactive(console: Console) -> bool:
    return console.is_terminal and not console.is_dumb_terminal


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


class _PendingLine:
    """A single-line renderable: the label, a spinner, and elapsed time."""

    def __init__(self, label: Text, start_time: float):
        self.label = label
        self.start_time = start_time
        self.spinner = Spinner("dots", style="cyan")

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        now = monotonic()
        suffix = Text.assemble(
            " ",
            self.spinner.render(now),
            (f" {_format_elapsed(now - self.start_time)}", "dim"),
        )
        label = _truncate(self.label, options.max_width - suffix.cell_len)
        yield Text.assemble(label, suffix, no_wrap=True, end="")


def _truncate(text: Text, width: int) -> Text:
    text = text.copy()
    text.truncate(max(width, 1), overflow="ellipsis")
    return text


class ActivityIndicator:
    """Report a unit of blocking work on the console.

    Calling :meth:`start` prints the label immediately. On an interactive
    terminal, the label is left "pending" on the current line and, if the work is
    still running after ``delay`` seconds, a spinner and elapsed time are added
    to it. :meth:`done` then replaces the pending line with the final label, and
    :meth:`clear` removes it. On a non-interactive output, :meth:`start` prints
    the label as a complete line, and :meth:`done`/:meth:`clear` print nothing.

    Usually used as a context manager (see :func:`activity`).
    """

    def __init__(
        self,
        label: Union[str, Text],
        *,
        console: Optional[Console] = None,
        delay: Optional[float] = None,
        style: str = "dim",
    ):
        if not isinstance(label, Text):
            label = Text(label)
        self.label = label
        self.console = console or get_console()
        self.delay = DEFAULT_DELAY if delay is None else delay
        self.style = style
        self.interactive = _is_interactive(self.console)

        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._live: Optional[Live] = None
        self._start_time: Optional[float] = None
        self._running = False
        # Whether the label is currently shown as a complete line
        self.label_visible = False

    def start(self) -> "ActivityIndicator":
        self._start_time = monotonic()
        self._running = True
        label = self._styled(self.style)
        if not self.interactive:
            self.console.print(label, soft_wrap=True)
            self.label_visible = True
            return self

        # Keep the pending line to the terminal width so it can be rewritten
        self.console.print(_truncate(label, self.console.width - 1), end="")
        self._timer = threading.Timer(self.delay, self._show_indicator)
        self._timer.daemon = True
        self._timer.start()
        return self

    def done(self, style: Optional[str] = None):
        """Finish the work, leaving the label on the console (in ``style``,
        which defaults to the indicator's style)."""
        if not self._stop():
            return
        if self.interactive:
            self.console.print(self._styled(style or self.style), soft_wrap=True)
            self.label_visible = True

    def clear(self):
        """Finish the work, removing the pending label from an interactive
        console."""
        self._stop()

    @property
    def elapsed(self) -> Optional[float]:
        if self._start_time is None:
            return None
        return monotonic() - self._start_time

    def _styled(self, style: Optional[str]) -> Text:
        label = self.label.copy()
        if style:
            label.stylize(style)
        return label

    def _show_indicator(self):
        with self._lock:
            if not self._running:
                return
            self._erase_line()
            self._live = Live(
                _PendingLine(self._styled(self.style), self._start_time),
                console=self.console,
                transient=True,
                refresh_per_second=10,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self._live.start()

    def _stop(self) -> bool:
        """Stop the indicator, erasing any pending output. Returns False if the
        indicator was not running."""
        if self._timer is not None:
            self._timer.cancel()
        with self._lock:
            if not self._running:
                return False
            self._running = False
            if self._live is not None:
                # A transient live display erases itself
                self._live.stop()
                self._live = None
            elif self.interactive:
                self._erase_line()
        return True

    def _erase_line(self):
        self.console.control(
            Control.move_to_column(0), Control((ControlType.ERASE_IN_LINE, 2))
        )

    def __enter__(self) -> "ActivityIndicator":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.done(style=None if exc_type is None else "red")


def activity(label: Union[str, Text], **kwargs) -> ActivityIndicator:
    """Show progress for a block of blocking work.

    >>> with activity("Refreshing materialized views"):
    ...     refresh_views()

    The label is printed before the block runs. If the block takes longer than
    ``delay`` seconds, a spinner and elapsed time are shown alongside it. If the
    block raises, the label is left in red.
    """
    return ActivityIndicator(label, **kwargs)
