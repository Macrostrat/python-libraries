from datetime import timedelta

import pytest
from typer.testing import CliRunner

from macrostrat.app_frame.environments import (
    AccessRefused,
    AccessScope,
    EnvironmentClass,
    Environments,
    StaleEnvironment,
    UnknownEnvironment,
    WriteGate,
    access,
    extract_env_from_argv,
)

CONFIG = """
log_level = "info"

[default]
database = "postgresql://localhost/field"

[default.storage]
bucket = "field"
region = "us"

[local]
env_class = "local"

[development]
env_class = "development"
database = "postgresql://dev.example.org/field"

[development.storage]
bucket = "field-dev"

[staging]
env_class = "staging"
active_ttl = "30m"

[production]
env_class = "production"
confirm = "environment-name"

[unlabelled]
database = "postgresql://somewhere/field"
"""


@pytest.fixture
def make(tmp_path, monkeypatch):
    config = tmp_path / "field.toml"
    config.write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    for var in ("FIELD_ENV", "FIELD_ENV_EXPIRES", "FIELD_CONFIG", "FIELD_DATABASE"):
        monkeypatch.delenv(var, raising=False)

    def _make(**kwargs):
        kwargs.setdefault("state_dir", tmp_path / "state")
        kwargs.setdefault("read_argv", False)
        return Environments("field", **kwargs)

    return _make


def test_no_environment_gets_base_layers_and_fails_closed(make):
    envs = make()
    assert envs.env is None
    assert envs.settings.get("database") == "postgresql://localhost/field"
    assert envs.settings.log_level == "info"
    assert envs.env_class == EnvironmentClass.Production
    assert envs.available() == [
        "local",
        "development",
        "staging",
        "production",
        "unlabelled",
    ]


def test_layers_merge_and_variables_override(make, monkeypatch):
    monkeypatch.setenv("FIELD_ENV", "development")
    envs = make()
    assert envs.settings.get("database") == "postgresql://dev.example.org/field"
    assert envs.settings.get("storage.bucket") == "field-dev"
    assert envs.settings.storage.region == "us"
    assert envs.settings.get("missing.key", 3) == 3
    assert "env" not in envs.settings

    monkeypatch.setenv("FIELD_DATABASE", "postgresql://override/field")
    monkeypatch.setenv("FIELD_STORAGE__PORT", "9000")
    envs = make()
    assert envs.settings.get("database") == "postgresql://override/field"
    assert envs.settings.get("storage.port") == 9000


def test_unknown_environment_is_refused(make, monkeypatch):
    monkeypatch.setenv("FIELD_ENV", "nowhere")
    with pytest.raises(UnknownEnvironment) as err:
        make()
    assert "development" in err.value.details


def test_command_line_construction_exits_cleanly(make, monkeypatch, capsys):
    monkeypatch.setenv("FIELD_ENV", "nowhere")
    with pytest.raises(SystemExit):
        Environments.for_command_line("field", read_argv=False)
    assert "nowhere is not defined" in capsys.readouterr().err


def test_without_a_file_variables_describe_the_environment(make, monkeypatch, tmp_path):
    monkeypatch.setenv("FIELD_CONFIG", "none")
    monkeypatch.setenv("FIELD_ENV", "local")
    monkeypatch.setenv("FIELD_DATABASE", "postgresql://container/field")
    envs = make()
    assert envs.config_file is None
    assert envs.env_class == EnvironmentClass.Local
    assert envs.settings.get("database") == "postgresql://container/field"


def test_undeclared_class_is_treated_as_production(make, monkeypatch):
    monkeypatch.setenv("FIELD_ENV", "unlabelled")
    envs = make()
    assert envs.env_class == EnvironmentClass.Production
    assert envs.policy.inferred


def test_remembered_environment_and_its_scope(make, tmp_path, monkeypatch):
    envs = make()
    expires = envs.select("staging")
    assert expires is not None
    monkeypatch.delenv("FIELD_ENV")

    again = make()
    assert again.env == "staging"
    assert again.active.source == "remembered"
    assert timedelta(minutes=29) < again.active.remaining <= timedelta(minutes=30)

    # A different config file is not affected by the pointer.
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "field.toml").write_text(CONFIG)
    monkeypatch.chdir(other)
    assert make().env is None

    monkeypatch.chdir(tmp_path)
    again.forget()
    assert make().env is None


def test_local_does_not_lapse(make):
    envs = make()
    assert envs.select("local") is None


def test_lapsed_environment_is_kept_and_refused_without_a_terminal(make, monkeypatch):
    envs = make()
    envs.select("staging")
    monkeypatch.delenv("FIELD_ENV")
    envs = make()
    envs.state.renew(timedelta(minutes=-5))
    envs.active.approved = False
    monkeypatch.setattr(access, "is_interactive", lambda: False)
    assert envs.env == "staging"
    with pytest.raises(StaleEnvironment):
        envs.require_environment()

    # At a terminal, yes renews it.
    monkeypatch.setattr(access, "is_interactive", lambda: True)
    monkeypatch.setattr(access, "prompt", lambda message: "y")
    envs.require_environment()
    assert not envs.active.lapsed


@pytest.mark.parametrize(
    "env,gate",
    [
        ("local", WriteGate.NoGate),
        ("development", WriteGate.Confirm),
        ("staging", WriteGate.Typed),
        ("production", WriteGate.Typed),
        ("unlabelled", WriteGate.Escalate),
    ],
)
def test_default_and_declared_levels(make, monkeypatch, env, gate):
    monkeypatch.setenv("FIELD_ENV", env)
    assert make().policy.gate_for(AccessScope.Schema) == gate


def test_access_levels(make, monkeypatch):
    monkeypatch.setattr(access, "is_interactive", lambda: False)

    monkeypatch.setenv("FIELD_ENV", "local")
    make().require_access(AccessScope.Data)

    monkeypatch.setenv("FIELD_ENV", "development")
    envs = make()
    with pytest.raises(AccessRefused):
        envs.require_access(AccessScope.Data)
    envs.require_access(AccessScope.Data, assume_yes=True)
    envs.require_access(AccessScope.Read)

    # --yes never satisfies the typed level, and neither does no terminal.
    monkeypatch.setenv("FIELD_ENV", "production")
    envs = make()
    with pytest.raises(AccessRefused):
        envs.require_access(AccessScope.Schema, assume_yes=True)

    monkeypatch.setattr(access, "is_interactive", lambda: True)
    monkeypatch.setattr(access, "prompt", lambda message: "staging")
    with pytest.raises(AccessRefused):
        envs.require_access(AccessScope.Schema)
    monkeypatch.setattr(access, "prompt", lambda message: "production")
    envs.require_access(AccessScope.Schema)


def test_reauthorize_needs_a_hook(make, monkeypatch):
    monkeypatch.setenv("FIELD_ENV", "unlabelled")
    monkeypatch.setattr(access, "is_interactive", lambda: True)
    monkeypatch.setattr(access, "prompt", lambda message: "unlabelled")
    with pytest.raises(AccessRefused):
        make().require_access(AccessScope.Data)

    called = []
    make(reauthorize=called.append).require_access(AccessScope.Data)
    assert called == [AccessScope.Data]


SECRET_CONFIG = """
op_account = "work.1password.com"

[production]
env_class = "production"
database = "stub://prod-db"
api_token = "literal-token"

[production.storage]
secret_key = "stub://prod-storage"
endpoint = "https://storage.example.org"
"""


@pytest.fixture
def stub(make, tmp_path, monkeypatch):
    from macrostrat.app_frame import secrets

    calls = []

    def resolve(body):
        calls.append((body, secrets.current_account()))
        return f"value-of-{body}"

    secrets.forget_all_secrets()
    secrets.register_resolver("stub", resolve)
    (tmp_path / "field.toml").write_text(SECRET_CONFIG)
    monkeypatch.setenv("FIELD_ENV", "production")
    yield calls
    secrets.RESOLVERS.pop("stub", None)
    secrets.forget_all_secrets()


def test_references_come_back_as_unresolved_secrets(make, stub):
    from macrostrat.app_frame.secrets import REDACTED, Secret

    settings = make().settings
    database = settings.get("database")
    assert isinstance(database, Secret)
    assert isinstance(settings.storage.secret_key, Secret)
    assert settings.get("storage.endpoint") == "https://storage.example.org"
    assert f"{database}" == REDACTED
    assert stub == []

    assert settings.secret("database") == "value-of-prod-db"
    assert settings.storage.secret("secret_key") == "value-of-prod-storage"
    # The file's account goes with each secret, nested tables included.
    assert stub == [
        ("prod-db", "work.1password.com"),
        ("prod-storage", "work.1password.com"),
    ]
    assert settings.secret("api_token") == "literal-token"
    assert settings["database"] == "stub://prod-db"


def test_redacted_settings_keep_references_and_mask_literals(make, stub):
    from macrostrat.app_frame.secrets import REDACTED

    shown = make().settings.redacted()
    assert shown["database"] == "stub://prod-db"
    assert shown["api_token"] == REDACTED
    assert shown["storage"]["endpoint"] == "https://storage.example.org"
    assert stub == []


def test_reauthorize_refetches_named_credentials(make, stub, monkeypatch):
    monkeypatch.setattr(access, "is_interactive", lambda: True)
    monkeypatch.setattr(access, "prompt", lambda message: "production")

    envs = make(credentials=["database"])
    envs.settings.secret("database")
    envs.require_access(AccessScope.Schema)
    assert [body for body, _ in stub] == ["prod-db", "prod-db"]

    # A literal credential cannot be re-authorized.
    with pytest.raises(AccessRefused) as err:
        make(credentials=["api_token"]).require_access(AccessScope.Schema)
    assert "not a secret reference" in err.value.message


def test_env_flag_is_taken_from_argv():
    args = ["field", "--env", "staging", "schema", "plan"]
    assert extract_env_from_argv(args) == "staging"
    assert args == ["field", "schema", "plan"]
    args = ["field", "--env=local", "x"]
    assert extract_env_from_argv(args) == "local"
    assert extract_env_from_argv(["field", "x"]) is None


def test_env_command(make, monkeypatch):
    envs = make()
    runner = CliRunner()
    cli = envs.cli()

    res = runner.invoke(cli, [])
    assert "No environment set" in res.output

    res = runner.invoke(cli, ["--list"])
    assert "staging" in res.output and "prompt" in res.output

    res = runner.invoke(cli, ["staging"])
    assert res.exit_code == 0, res.output
    assert "Activated" in res.output and "30 min" in res.output

    res = runner.invoke(cli, ["--shell", "development"])
    assert "export FIELD_ENV=development" in res.output
    assert "export FIELD_ENV_EXPIRES=" in res.output

    res = runner.invoke(cli, ["nowhere"])
    assert res.exit_code == 1

    monkeypatch.delenv("FIELD_ENV")
    res = runner.invoke(make().cli(), [])
    assert "staging" in res.output and "remembered" in res.output

    runner.invoke(cli, ["--unset"])
    assert make().env is None
