"""Confirmation before access: what constrains an agent, and what asks a person."""

import io
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pytest import fixture, mark, raises

from macrostrat.app_frame.environments import (
    AccessRefused,
    AccessScope,
    ActiveEnvironment,
    EnvironmentPolicy,
    StaleEnvironment,
    access,
)

# Stands in for an `EnvironmentState`: the active environment, and its renewal.
STATE = SimpleNamespace(active=None, renew=lambda ttl: None)


class _Settings:
    def __init__(self, policy):
        self.policy = policy


def policy_for(env, env_class, **kw):
    return EnvironmentPolicy.resolve(env, env_class=env_class, **kw)


def confirm(settings, action=None):
    return access.require_environment(
        STATE, settings.policy, command_name="field", action=action
    )


def gate(scope, *, settings, assume_yes=False, action=None):
    """Environment first, then the level: the order `Environments` applies."""
    scope = AccessScope(scope)
    confirm(settings, action)
    return access.require_access(
        scope, settings.policy, assume_yes=assume_yes, action=action
    )


@fixture
def tty(monkeypatch):
    """An interactive terminal with scripted answers.

    Substitutes the module's own `is_interactive` and `prompt` rather than
    patching `sys.stdin`: pytest's capture manager re-installs its streams for
    the call phase, undoing a fixture's patch of them.
    """

    class _Answers:
        def __init__(self):
            self.prompts = []
            self._queue = []

        def feed(self, *lines):
            self._queue = list(lines)

        def prompt(self, message):
            self.prompts.append(message)
            if self._queue:
                return self._queue.pop(0)
            return ""

    answers = _Answers()
    monkeypatch.setattr(access, "is_interactive", lambda: True)
    monkeypatch.setattr(access, "prompt", answers.prompt)
    return answers


@fixture
def no_tty(monkeypatch):
    """An agent: no terminal to prompt on."""
    monkeypatch.setattr(access, "is_interactive", lambda: False)


class TestUngated:
    def test_local_writes_proceed_silently(self, no_tty):
        s = _Settings(policy_for("local", "local"))
        for scope in AccessScope:
            assert gate(scope, settings=s) is None

    def test_an_unknown_scope_is_rejected(self, no_tty):
        """A typo in a scope name must not silently become a permitted write."""
        from pytest import raises as _raises

        s = _Settings(policy_for("production", "production"))
        with _raises(ValueError):
            gate("servcies", settings=s)


class TestNonInteractiveRefusal:
    """The property that actually constrains an agent."""

    @mark.parametrize(
        "env_class,scope",
        [
            ("development", AccessScope.Data),
            ("development", AccessScope.Schema),
            ("staging", AccessScope.Data),
            ("production", AccessScope.Data),
            ("production", AccessScope.Schema),
        ],
    )
    def test_every_gated_scope_refuses_without_a_tty(self, no_tty, env_class, scope):
        s = _Settings(policy_for("env", env_class))
        with raises(AccessRefused):
            gate(scope, settings=s)

    def test_yes_satisfies_confirm_only(self, no_tty):
        s = _Settings(policy_for("development", "development"))
        assert gate(AccessScope.Data, settings=s, assume_yes=True) is None

    @mark.parametrize("env_class", ["staging", "production"])
    def test_yes_does_not_satisfy_typed_or_escalate(self, no_tty, env_class):
        """There must be no flag that buys a typed gate."""
        s = _Settings(policy_for("env", env_class))
        with raises(AccessRefused) as err:
            gate(AccessScope.Data, settings=s, assume_yes=True)
        assert "no flag or environment variable" in err.value.details

    def test_refusal_names_the_environment_and_class(self, no_tty):
        s = _Settings(policy_for("production", "production"))
        with raises(AccessRefused) as err:
            gate(AccessScope.Data, settings=s)
        assert "production" in str(err.value.message)

    def test_a_failed_closed_inference_is_explained(self, no_tty):
        """An undeclared environment should say why it is being treated as prod."""
        s = _Settings(policy_for("criticalmaas", None))
        with raises(AccessRefused) as err:
            gate(AccessScope.Data, settings=s)
        assert "declares no class" in str(err.value.details)


class TestInteractiveConfirm:
    def test_yes_proceeds(self, tty):
        tty.feed("y")
        s = _Settings(policy_for("development", "development"))
        assert gate(AccessScope.Data, settings=s) is None

    @mark.parametrize("answer", ["n", "", "no", "nope", "Y E S"])
    def test_anything_else_declines(self, tty, answer):
        tty.feed(answer)
        s = _Settings(policy_for("development", "development"))
        if answer in ("y", "yes"):
            return
        with raises(AccessRefused):
            gate(AccessScope.Data, settings=s)

    def test_the_prompt_names_the_action_and_environment(self, tty):
        tty.feed("y")
        s = _Settings(policy_for("development", "development"))
        gate(AccessScope.Data, settings=s, action="restore")
        assert "Restore in development" in tty.prompts[0]


class TestInteractiveTyped:
    def test_exact_environment_name_proceeds(self, tty):
        tty.feed("staging")
        s = _Settings(policy_for("staging", "staging"))
        assert gate(AccessScope.Data, settings=s) is None

    @mark.parametrize("answer", ["", "y", "STAGING", "staging ", "production"])
    def test_anything_else_refuses(self, tty, answer):
        tty.feed(answer)
        s = _Settings(policy_for("staging", "staging"))
        if answer.strip() == "staging":
            return
        with raises(AccessRefused):
            gate(AccessScope.Data, settings=s)


class TestInteractivityDetection:
    """`is_interactive` and `_prompt` against real stream objects.

    `sys` is patched inside each test body rather than in a fixture, for the
    capture-manager reason described on the `tty` fixture.
    """

    @staticmethod
    def _stream(isatty):
        s = io.StringIO()
        s.isatty = lambda: isatty
        return s

    def test_both_ttys_is_interactive(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", self._stream(True))
        monkeypatch.setattr(sys, "stderr", self._stream(True))
        assert access.is_interactive()

    @mark.parametrize(
        "stdin_tty,stderr_tty", [(True, False), (False, True), (False, False)]
    )
    def test_either_stream_not_a_tty_is_not_interactive(
        self, monkeypatch, stdin_tty, stderr_tty
    ):
        monkeypatch.setattr(sys, "stdin", self._stream(stdin_tty))
        monkeypatch.setattr(sys, "stderr", self._stream(stderr_tty))
        assert not access.is_interactive()

    def test_closed_streams_are_not_interactive(self, monkeypatch):
        class _Closed:
            def isatty(self):
                raise ValueError("I/O operation on closed file")

        monkeypatch.setattr(sys, "stdin", _Closed())
        assert not access.is_interactive()

    def test_prompt_writes_to_stderr_and_reads_stdin(self, monkeypatch):
        """The prompt must never land in a command's piped stdout."""
        err = io.StringIO()
        out = io.StringIO()
        monkeypatch.setattr(sys, "stderr", err)
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stdin", io.StringIO("  staging  \n"))
        assert access.prompt("Type the name: ") == "staging"
        assert err.getvalue() == "Type the name: "
        assert out.getvalue() == ""


class TestRequireEnvironment:
    """A lapsed environment is questioned before it is used, not dropped."""

    def state(self, monkeypatch, *, lapsed=True, source="remembered", approved=False):
        delta = timedelta(minutes=12)
        if lapsed:
            delta = timedelta(minutes=-12)
        st = ActiveEnvironment(
            name="production",
            source=source,
            expires=datetime.now(timezone.utc) + delta,
            approved=approved,
        )
        renewed = []
        monkeypatch.setattr(STATE, "active", st)
        monkeypatch.setattr(STATE, "renew", lambda ttl: renewed.append(ttl))
        return st, renewed

    def settings(self):
        return _Settings(policy_for("production", "production"))

    def test_no_environment_is_fine(self, no_tty, monkeypatch):
        monkeypatch.setattr(STATE, "active", None)
        assert confirm(self.settings()) is None

    def test_a_fresh_environment_is_fine(self, no_tty, monkeypatch):
        self.state(monkeypatch, lapsed=False)
        assert confirm(self.settings()) is None

    def test_lapsed_without_a_tty_refuses(self, no_tty, monkeypatch):
        self.state(monkeypatch)
        with raises(StaleEnvironment) as err:
            confirm(self.settings(), action="restore")
        text = str(err.value.format_message())
        assert "production" in text
        assert "--env production" in text
        assert "12 min ago" in text

    def test_lapsed_with_approval_is_fine(self, no_tty, monkeypatch):
        self.state(monkeypatch, approved=True)
        assert confirm(self.settings()) is None

    def test_yes_renews_a_remembered_environment(self, tty, monkeypatch):
        st, renewed = self.state(monkeypatch)
        tty.feed("y")
        confirm(self.settings())
        assert renewed == [self.settings().policy.ttl]
        assert "lapsed 12 min ago" in tty.prompts[0]
        assert "15 min" in tty.prompts[0]

    def test_yes_approves_a_shell_session_without_persisting(self, tty, monkeypatch):
        st, renewed = self.state(monkeypatch, source="shell")
        tty.feed("yes")
        confirm(self.settings())
        assert st.approved
        assert renewed == []

    @mark.parametrize("answer", ["", "n", "no", "production"])
    def test_anything_else_declines(self, tty, monkeypatch, answer):
        self.state(monkeypatch)
        tty.feed(answer)
        with raises(StaleEnvironment):
            confirm(self.settings())

    def test_a_write_gate_asks_about_the_environment_first(self, tty, monkeypatch):
        """Both prompts fire, environment first, so the write prompt is
        unambiguously about the environment just confirmed."""
        self.state(monkeypatch)
        s = _Settings(policy_for("development", "development"))
        tty.feed("y", "y")
        gate(AccessScope.Data, settings=s)
        assert len(tty.prompts) == 2
        assert "lapsed" in tty.prompts[0]
        assert "Proceed?" in tty.prompts[1]

    def test_a_write_on_a_lapsed_environment_refuses_without_a_tty(
        self, no_tty, monkeypatch
    ):
        self.state(monkeypatch)
        s = _Settings(policy_for("local", "local"))
        # Even an ungated local write: the environment itself is in question.
        with raises(StaleEnvironment):
            gate(AccessScope.Data, settings=s)

    def test_stale_environment_is_a_refusal(self):
        assert issubclass(StaleEnvironment, access.Refusal)
        assert issubclass(AccessRefused, access.Refusal)
