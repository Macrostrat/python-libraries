# Macrostrat Database

Macrostrat's Database module provides a simplified wrapper over SQLAlchemy
databases, making it easier to build common database management functionality.

<!-- prettier-ignore-start -->
## ::: macrostrat.database.Database
    options:
      show_root_heading: true
<!-- prettier-ignore-end -->

## Console progress

Statements run with `run_sql` (and `run_fixtures`) are summarized on standard
error _before_ they run, so a long-running or hung statement is always
identifiable. On an interactive terminal, if a statement is still running after
two seconds, a spinner and the elapsed time are shown on the same line; when it
finishes, the line is replaced by the dimmed summary (or red on failure). On a
non-interactive output (a file, pipe, or CI log), each summary is printed once,
up front. The delay can be changed with the `MACROSTRAT_ACTIVITY_DELAY`
environment variable (in seconds).

The same indicator can be used for other blocking work:

```python
from macrostrat.database.progress import activity

with activity("Refreshing materialized views"):
    refresh_views()
```

<!-- prettier-ignore-start -->
### ::: macrostrat.database.progress.ActivityIndicator
    options:
      show_root_heading: true
<!-- prettier-ignore-end -->
