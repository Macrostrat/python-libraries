---
macrostrat.database: minor
---

- Print each statement's summary _before_ it runs in `run_sql` and
  `run_fixtures`, so a long-running or hung statement is identifiable. On an
  interactive terminal, a spinner and elapsed time are shown on the same line
  if the statement runs for more than two seconds (configurable with
  `MACROSTRAT_ACTIVITY_DELAY`), and an interrupted statement is left in red.
- Add `macrostrat.database.progress.activity` for showing the same progress
  indicator around other blocking work.
- `run_query` no longer prints anything by default (it previously printed only
  on some error paths); pass `output_mode` to enable output.
