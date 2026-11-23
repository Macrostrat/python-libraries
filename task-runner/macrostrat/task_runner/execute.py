"""Execute one run on the worker: output to its stream, cancellable like Ctrl-C.

The application keeps its own record of runs; this only executes one and
reports how it ended.
"""

import os
import traceback
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit, urlunsplit

from redis import Redis

from macrostrat.database import Database

from .cancel import CancelWatcher, TaskCancelled, cancel_backends, interruptible
from .context import RunContext
from .output import OutputStream, expire, read_all

# Rich reads these when it creates a console, so they must be set before the
# task imports anything that does. The worker's service should set them too.
TERMINAL_ENV = {
    "FORCE_COLOR": "1",
    "COLORTERM": "truecolor",
    "TERM": "xterm-256color",
    "COLUMNS": "120",
    "LINES": "40",
}

FINAL_STATES = ("succeeded", "failed", "cancelled", "killed")


@dataclass
class Outcome:
    state: str
    result: Any = None
    error: str | None = None
    log: str = ""


def execute(
    run_id: str,
    task: Callable[[Database, RunContext], Any],
    *,
    db_url: str,
    redis_url: str,
) -> Outcome:
    """Call `task(db, ctx)` with its output on the run's stream and return how it ended.

    Must run in a main thread (see `interruptible`). `db` connects as
    `task-run:<run_id>`, which a cancel looks for; so does any libpq connection
    the task opens itself, through `PGAPPNAME`.
    """
    for key, value in TERMINAL_ENV.items():
        os.environ.setdefault(key, value)

    redis = Redis.from_url(redis_url)
    application_name = f"task-run:{run_id}"
    task_db = Database(with_application_name(db_url, application_name))
    output = OutputStream(redis, run_id)
    ctx = RunContext(run_id=run_id, output=output)
    watcher = CancelWatcher(
        redis, run_id, on_cancel=lambda: cancel_backends(db_url, application_name)
    )

    outcome = Outcome(state="succeeded")
    watcher.start()
    try:
        with (
            _environment(PGAPPNAME=application_name),
            redirect_stdout(output),
            redirect_stderr(output),
            interruptible(),
        ):
            outcome.result = task(task_db, ctx)
    except TaskCancelled:
        outcome.state = "cancelled"
        output.write("\n^C  cancelled\n")
    except BaseException as err:
        outcome.state = "failed"
        outcome.error = f"{type(err).__name__}: {err}"
        output.write("\n" + traceback.format_exc())
    finally:
        watcher.stop()
        output.flush()
        task_db.session.rollback()
        task_db.engine.dispose()

    outcome.log = read_all(redis, run_id)
    expire(redis, run_id)
    redis.close()
    return outcome


def with_application_name(db_url: str, name: str) -> str:
    """The URL with `application_name` set, so the run's sessions can be found."""
    parts = urlsplit(db_url)
    query = (
        parts.query
        + ("&" if parts.query else "")
        + urlencode({"application_name": name})
    )
    return urlunsplit(parts._replace(query=query))


@contextmanager
def _environment(**values: str):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
