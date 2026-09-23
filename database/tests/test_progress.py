"""Tests for console progress reporting while statements run."""

import sqlite3
import time
from io import StringIO

from pytest import raises
from rich.console import Console
from sqlalchemy import create_engine, event

from macrostrat.database.progress import ActivityIndicator, activity, get_console
from macrostrat.database.query import run_query, run_sql


def _terminal_console(buf):
    return Console(file=buf, force_terminal=True, width=60, highlight=False)


def _sqlite_engine(**functions):
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _register(dbapi_conn, _):
        for name, fn in functions.items():
            dbapi_conn.create_function(name, 0, fn)

    return engine


def test_statement_printed_before_execution():
    """The statement summary is on the console while it is still running."""
    buf = StringIO()
    seen = []
    engine = _sqlite_engine(peek=lambda: seen.append(buf.getvalue()) or 1)

    with engine.connect() as conn:
        run_sql(conn, "SELECT peek(); SELECT 2", output_file=buf)

    assert seen == ["SELECT peek\n"]
    assert buf.getvalue() == "SELECT peek\nSELECT 2\n"


def test_failed_statement_printed_once():
    buf = StringIO()
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        run_sql(conn, "INSERT INTO missing (id) VALUES (1)", output_file=buf)
    lines = buf.getvalue().splitlines()
    assert lines[0] == "INSERT INTO missing"
    assert lines.count("INSERT INTO missing") == 1
    assert "no such table" in lines[1]


def test_raised_statement_is_shown():
    buf = StringIO()
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        with raises(Exception):
            run_sql(
                conn,
                "INSERT INTO missing (id) VALUES (1)",
                output_file=buf,
                raise_errors=True,
            )
    assert buf.getvalue() == "INSERT INTO missing\n"


def test_run_query_is_quiet_by_default():
    buf = StringIO()
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        assert run_query(conn, "SELECT 1", output_file=buf).scalar() == 1
    assert buf.getvalue() == ""


def test_interactive_statement_rewritten_in_place():
    """On a terminal, the pending line is erased and replaced once done."""
    buf = StringIO()
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        run_sql(conn, "SELECT 1", console=_terminal_console(buf))
    out = buf.getvalue()
    # Pending label, erase line, then the final label
    assert out.count("SELECT 1") == 2
    assert "\x1b[2K" in out
    assert out.endswith("\n")


def test_indicator_shown_for_slow_work():
    buf = StringIO()
    with activity("Slow work", console=_terminal_console(buf), delay=0.05):
        time.sleep(0.4)
    out = buf.getvalue()
    # A spinner frame and the elapsed time were displayed
    assert any(frame in out for frame in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
    assert "0s" in out
    assert out.rstrip().endswith("Slow work\x1b[0m")


def test_no_indicator_for_fast_work():
    buf = StringIO()
    with activity("Fast work", console=_terminal_console(buf), delay=5):
        pass
    assert not any(frame in buf.getvalue() for frame in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")


def test_failed_activity_is_red():
    buf = StringIO()
    with raises(ValueError):
        with activity("Broken work", console=_terminal_console(buf), delay=5):
            raise ValueError("boom")
    assert "\x1b[31mBroken work" in buf.getvalue()


def test_non_interactive_activity():
    buf = StringIO()
    indicator = ActivityIndicator("Some work", console=get_console(buf), delay=0)
    indicator.start()
    time.sleep(0.1)
    indicator.done()
    indicator.done()  # Finishing twice is harmless
    assert buf.getvalue() == "Some work\n"
    assert indicator.label_visible
