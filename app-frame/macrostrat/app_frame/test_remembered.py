"""The remembered environment, and how it stops being sticky.

A remembered environment lapses but is not dropped: it is kept, marked, and
confirmed before use. It applies only to the config file it was set against,
and only if that file defines it.
"""

import json
from datetime import datetime, timedelta, timezone

from pytest import fixture

from macrostrat.app_frame.environments import EnvironmentState
from macrostrat.app_frame.environments.state import (
    ACTIVE_ENV_CONFIG_KEY,
    ACTIVE_ENV_EXPIRES_KEY,
    ACTIVE_ENV_KEY,
)

ENV_VAR = "FIELD_ENV"
ENV_EXPIRES_VAR = "FIELD_ENV_EXPIRES"

CONFIG = """
[local]
env_class = "local"

[development]
env_class = "development"

[production]
env_class = "production"
"""

TTL = timedelta(minutes=15)


@fixture
def config(tmp_path):
    cfg = tmp_path / "project" / "field.toml"
    cfg.parent.mkdir()
    cfg.write_text(CONFIG)
    return cfg


@fixture
def state(tmp_path, monkeypatch, config):
    """A state directory of its own, with the variables cleared."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv(ENV_EXPIRES_VAR, raising=False)
    state = EnvironmentState(
        "field",
        env_var=ENV_VAR,
        expires_var=ENV_EXPIRES_VAR,
        state_dir=tmp_path / "state",
    )
    state.config = config
    return state


def remaining(state):
    """Time left on the pointer as written, or None if it does not expire."""
    remembered = state.remembered()
    if remembered is None:
        return None
    return remembered.remaining


def put(state, key, value):
    """Write one key by hand, as an older version of the file might hold."""
    data = state.read_state()
    data[key] = value
    state.write_state(data)


class TestStickiness:
    def test_local_is_remembered_indefinitely(self, state, config):
        state.remember("local", expires_in=None, config_file=config)
        assert remaining(state) is None
        assert state.resolve(state.config) == "local"
        active = state.active
        assert active.source == "remembered"
        assert not active.lapsed

    def test_non_local_expires(self, state, config):
        state.remember("production", expires_in=TTL, config_file=config)
        left = remaining(state)
        assert left is not None
        assert timedelta(0) < left <= TTL
        assert state.resolve(state.config) == "production"
        assert not state.active.needs_approval

    def test_a_lapsed_environment_is_kept_and_marked(self, state, config):
        """Lapsing means 'ask before use', not 'drop to no environment'."""
        state.remember(
            "production", expires_in=timedelta(seconds=-1), config_file=config
        )
        assert state.resolve(state.config) == "production"
        active = state.active
        assert active.lapsed
        assert active.needs_approval
        # Still remembered: nothing was cleared behind the operator's back.
        assert state.read_state().get(ACTIVE_ENV_KEY) == "production"

    def test_loading_writes_nothing(self, state, config):
        """Config load runs for every invocation, `--help` included."""
        state.remember(
            "production", expires_in=timedelta(seconds=-1), config_file=config
        )
        before = state.state_file.read_text()
        state.resolve(state.config)
        assert state.state_file.read_text() == before

    def test_an_unparseable_expiry_is_treated_as_lapsed(self, state, config):
        """Fail closed: a corrupt timestamp must not mean 'never expires'."""
        put(state, ACTIVE_ENV_KEY, "production")
        put(state, ACTIVE_ENV_EXPIRES_KEY, "not-a-timestamp")
        assert state.resolve(state.config) == "production"
        assert state.active.needs_approval

    def test_a_naive_legacy_timestamp_is_read_as_local_time(self, state, config):
        put(state, ACTIVE_ENV_KEY, "production")
        future = (datetime.now() + timedelta(minutes=10)).isoformat()
        put(state, ACTIVE_ENV_EXPIRES_KEY, future)
        state.resolve(state.config)
        remaining = state.active.remaining
        assert timedelta(minutes=9) < remaining <= timedelta(minutes=10)

    def test_explicit_env_is_never_subject_to_expiry(self, state, config, monkeypatch):
        """--env is per-invocation by construction, so it cannot go stale."""
        state.remember(
            "production", expires_in=timedelta(seconds=-1), config_file=config
        )
        monkeypatch.setenv(ENV_VAR, "production")
        assert state.resolve(state.config) == "production"
        active = state.active
        assert active.source == "explicit"
        assert not active.lapsed

    def test_explicit_env_overrides_a_live_remembered_one(
        self, state, config, monkeypatch
    ):
        state.remember("local", expires_in=None, config_file=config)
        monkeypatch.setenv(ENV_VAR, "staging")
        assert state.resolve(state.config) == "staging"

    def test_a_shell_session_lapses_when_its_expiry_says_so(self, state, monkeypatch):
        """An exported variable is not a way around the TTL."""
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        monkeypatch.setenv(ENV_VAR, "production")
        monkeypatch.setenv(ENV_EXPIRES_VAR, past)
        assert state.resolve(state.config) == "production"
        active = state.active
        assert active.source == "shell"
        assert active.needs_approval

    def test_a_fresh_shell_session_is_fine(self, state, monkeypatch):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        monkeypatch.setenv(ENV_VAR, "production")
        monkeypatch.setenv(ENV_EXPIRES_VAR, future)
        state.resolve(state.config)
        assert state.active.source == "shell"
        assert not state.active.lapsed

    def test_unsetting_clears_everything(self, state, config):
        state.remember("production", expires_in=TTL, config_file=config)
        state.remember(None)
        assert state.read_state().get(ACTIVE_ENV_KEY) is None
        assert state.read_state().get(ACTIVE_ENV_CONFIG_KEY) is None
        assert remaining(state) is None
        assert state.active is None

    def test_no_state_file_means_no_environment(self, state):
        assert not state.state_file.exists()
        assert state.resolve(state.config) is None
        assert state.active is None


class TestConfigScoping:
    """App state is per user; the config file is per directory."""

    def test_a_pointer_for_another_config_is_ignored_not_cleared(
        self, state, config, tmp_path
    ):
        other = tmp_path / "elsewhere" / "field.toml"
        state.remember("development", expires_in=TTL, config_file=other)
        assert state.resolve(state.config) is None
        assert state.active is None
        # It still applies in the directory it was set for.
        assert state.read_state().get(ACTIVE_ENV_KEY) == "development"
        assert state.remembered().config_file == other

    def test_a_pointer_the_config_does_not_define_is_ignored(self, state, config):
        state.remember("nonexistent", expires_in=TTL, config_file=config)
        assert state.resolve(state.config) is None
        assert state.active is None

    def test_a_legacy_pointer_without_a_config_applies(self, state, config):
        """State written before pointers were scoped has no config recorded."""
        put(state, ACTIVE_ENV_KEY, "development")
        assert state.resolve(state.config) == "development"

    def test_a_legacy_pointer_is_still_checked_against_the_file(self, state, config):
        put(state, ACTIVE_ENV_KEY, "criticalmaas")
        assert state.resolve(state.config) is None

    def test_the_config_path_is_recorded_resolved(self, state, config):
        state.remember("development", expires_in=TTL, config_file=config)
        assert state.read_state().get(ACTIVE_ENV_CONFIG_KEY) == str(config.resolve())


class TestRenewal:
    def test_renew_restamps_the_expiry_and_approves(self, state, config):
        state.remember(
            "production", expires_in=timedelta(seconds=-1), config_file=config
        )
        state.resolve(state.config)
        assert state.active.needs_approval
        expires = state.renew(TTL)
        active = state.active
        assert not active.lapsed
        assert active.approved
        assert timedelta(0) < active.remaining <= TTL
        # Persisted, so the next invocation is fresh too.
        assert remaining(state) > timedelta(0)
        assert expires is not None

    def test_renew_to_never(self, state, config):
        state.remember(
            "production", expires_in=timedelta(seconds=-1), config_file=config
        )
        state.resolve(state.config)
        assert state.renew(None) is None
        assert state.active.remaining is None
        assert remaining(state) is None

    def test_renewing_a_shell_session_persists_nothing(self, state, monkeypatch):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        monkeypatch.setenv(ENV_VAR, "production")
        monkeypatch.setenv(ENV_EXPIRES_VAR, past)
        state.resolve(state.config)
        state.renew(TTL)
        assert state.active.approved
        assert not state.state_file.exists()

    def test_renew_without_an_environment_is_a_noop(self, state):
        assert state.renew(TTL) is None
