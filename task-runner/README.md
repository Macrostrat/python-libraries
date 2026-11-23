# macrostrat.task_runner

Long-running tasks run on a Celery worker with their terminal output streamed
live to a browser, and a cancel that behaves like Ctrl-C. Used by Macrostrat's
management tasks and Mapboard's topology rebuilds.

The application keeps its own record of runs (a table with an id and a state)
and its own routes; this package supplies the parts in between:

- **Worker:** `execute(run_id, task, db_url=, redis_url=)` calls `task(db, ctx)`
  with stdout and stderr sent, ANSI intact, to the Redis stream
  `task-run:<id>:out`, and returns an `Outcome` (state, result, error, the whole
  log). It must run in a main thread, as under Celery's prefork pool with one
  task per child.
- **Cancel:** `request_cancel(redis, run_id)` publishes on the run's control
  channel. The worker interrupts the task's main thread with `TaskCancelled`, a
  `BaseException` no library `except Exception` swallows, and cancels the
  statements of every Postgres session named `task-run:<id>`.
- **API:** `output_events(redis, run_id, state=, archived_log=)` yields
  Server-Sent Events: the stream replayed from the start (or `Last-Event-ID`),
  then followed until the run ends, or the archived log once the stream has
  expired. Return it with `SSE_HEADERS` as `text/event-stream`.
- **Catalog:** `TaskSpec` and `load_registry(group)`, for applications that
  register tasks through entry points.

A browser draws the stream with xterm.js at 120 columns, matching the
`COLUMNS` the worker sets. A process that will not stop is killed through
Celery (`revoke(terminate=True)`), and the application records that itself.
