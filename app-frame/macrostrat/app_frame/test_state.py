"""`--env` parsing and time-to-live durations."""

from datetime import timedelta

from pytest import mark, raises

from macrostrat.app_frame.environments import (
    InvalidDuration,
    extract_env_from_argv,
    format_duration,
    parse_duration,
)


class TestArgvParsing:
    """One parser for --env. Two is how they drift."""

    @mark.parametrize(
        "args,expected,left",
        [
            (
                ["macrostrat", "--env", "staging", "db", "restore"],
                "staging",
                ["macrostrat", "db", "restore"],
            ),
            (
                ["macrostrat", "-e", "production", "db"],
                "production",
                ["macrostrat", "db"],
            ),
            (["macrostrat", "--env=staging", "db"], "staging", ["macrostrat", "db"]),
            (["macrostrat", "db", "restore"], None, ["macrostrat", "db", "restore"]),
            # A dangling flag with no value must not swallow anything.
            (["macrostrat", "--env"], None, ["macrostrat", "--env"]),
        ],
    )
    def test_extracts_and_removes_both_tokens(self, args, expected, left):
        assert extract_env_from_argv(args) == expected
        assert args == left


class TestDurations:
    @mark.parametrize(
        "value,expected",
        [
            ("15m", timedelta(minutes=15)),
            ("15 min", timedelta(minutes=15)),
            ("8h", timedelta(hours=8)),
            ("2h30m", timedelta(hours=2, minutes=30)),
            ("1d", timedelta(days=1)),
            ("90s", timedelta(seconds=90)),
            ("45", timedelta(minutes=45)),
            (45, timedelta(minutes=45)),
            (2.5, timedelta(minutes=2, seconds=30)),
            ("never", None),
            ("NONE", None),
            (False, None),
            (None, None),
        ],
    )
    def test_parses(self, value, expected):
        assert parse_duration(value) == expected

    @mark.parametrize("bad", ["soon", "0m", "-5m", 0, -1, True, "h"])
    def test_rejects_junk(self, bad):
        with raises(InvalidDuration):
            parse_duration(bad)

    @mark.parametrize(
        "value,text",
        [
            (None, "never"),
            (timedelta(minutes=15), "15 min"),
            (timedelta(hours=8), "8 h"),
            (timedelta(hours=2, minutes=30), "2 h 30 min"),
            (timedelta(days=1, hours=1), "1 d 1 h"),
            (timedelta(seconds=30), "30 s"),
        ],
    )
    def test_formats(self, value, text):
        assert format_duration(value) == text
