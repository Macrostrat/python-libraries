"""Environment classification and confirmation-level resolution."""

from pytest import mark

from macrostrat.app_frame.environments import (
    DEFAULT_GATES,
    AccessScope,
    EnvironmentClass,
    EnvironmentPolicy,
    WriteGate,
    parse_gate,
)


class TestClassResolution:
    def test_declared_class_is_used(self):
        p = EnvironmentPolicy.resolve("staging", env_class="staging")
        assert p.env_class == EnvironmentClass.Staging
        assert not p.inferred

    @mark.parametrize("declared", ["Production", " production ", "PRODUCTION"])
    def test_class_is_case_and_space_insensitive(self, declared):
        p = EnvironmentPolicy.resolve("prod", env_class=declared)
        assert p.env_class == EnvironmentClass.Production

    def test_local_is_trusted_by_name(self):
        p = EnvironmentPolicy.resolve("local")
        assert p.env_class == EnvironmentClass.Local
        assert p.is_local
        # Inferred, but not a fail-closed inference.
        assert p.inferred

    @mark.parametrize("name", ["development", "staging", "criticalmaas", "anything"])
    def test_undeclared_non_local_fails_closed(self, name):
        """The core safety property: silence means production."""
        p = EnvironmentPolicy.resolve(name)
        assert p.env_class == EnvironmentClass.Production
        assert p.inferred
        assert "failed closed" in p.reason

    def test_unknown_class_fails_closed(self):
        """A typo must not read as something safe."""
        p = EnvironmentPolicy.resolve("dev", env_class="devlopment")
        assert p.env_class == EnvironmentClass.Production
        assert p.inferred

    def test_no_environment_selected_fails_closed(self):
        p = EnvironmentPolicy.resolve(None)
        assert p.env_class == EnvironmentClass.Production
        assert "no environment selected" in p.reason

    def test_local_name_does_not_override_a_declared_class(self):
        """Naming a remote environment `local` must not launder it."""
        p = EnvironmentPolicy.resolve("local", env_class="production")
        assert p.env_class == EnvironmentClass.Production
        assert not p.inferred


class TestGates:
    def test_class_defaults_apply(self):
        p = EnvironmentPolicy.resolve("prod", env_class="production")
        assert p.gate_for(AccessScope.Data) == WriteGate.Escalate
        assert p.gate_for(AccessScope.Schema) == WriteGate.Escalate

    def test_local_is_ungated(self):
        p = EnvironmentPolicy.resolve("local", env_class="local")
        assert all(g == WriteGate.NoGate for g in p.gates.values())

    def test_every_class_covers_every_scope(self):
        for env_class in EnvironmentClass:
            p = EnvironmentPolicy.resolve("x", env_class=env_class.value)
            assert set(p.gates) == set(AccessScope)

    def test_override_replaces_only_the_named_scope(self):
        p = EnvironmentPolicy.resolve(
            "staging", env_class="staging", write_gate={"schema": "escalate"}
        )
        assert p.gate_for(AccessScope.Schema) == WriteGate.Escalate
        # Untouched scopes keep the class default.
        assert (
            p.gate_for(AccessScope.Data)
            == DEFAULT_GATES[EnvironmentClass.Staging][AccessScope.Data]
        )

    def test_override_can_relax_a_gate(self):
        """Overrides are declarative, not a floor — a human can loosen one."""
        p = EnvironmentPolicy.resolve(
            "prod", env_class="production", write_gate={"data": "confirm"}
        )
        assert p.gate_for(AccessScope.Data) == WriteGate.Confirm

    @mark.parametrize(
        "bad", [{"data": "sometimes"}, {"services": "none"}, "not-a-table", 7]
    )
    def test_unparseable_overrides_fall_back_to_defaults(self, bad):
        """Junk must never silently *widen* access."""
        p = EnvironmentPolicy.resolve("prod", env_class="production", write_gate=bad)
        assert p.gate_for(AccessScope.Data) == WriteGate.Escalate

    def test_reads_are_ungated_by_default_in_every_class(self):
        for env_class in EnvironmentClass:
            p = EnvironmentPolicy.resolve("x", env_class=env_class.value)
            assert p.gate_for(AccessScope.Read) == WriteGate.NoGate

    def test_confirm_table_sets_each_kind_of_access(self):
        p = EnvironmentPolicy.resolve(
            "prod",
            env_class="production",
            confirm={
                "read": "prompt",
                "data": "environment-name",
                "schema": "reauthorize",
            },
        )
        assert p.gate_for(AccessScope.Read) == WriteGate.Confirm
        assert p.gate_for(AccessScope.Data) == WriteGate.Typed
        assert p.gate_for(AccessScope.Schema) == WriteGate.Escalate

    def test_confirm_scalar_applies_to_both_writes_but_not_reads(self):
        p = EnvironmentPolicy.resolve("dev", env_class="development", confirm="none")
        assert p.gate_for(AccessScope.Data) == WriteGate.NoGate
        assert p.gate_for(AccessScope.Schema) == WriteGate.NoGate
        assert p.gate_for(AccessScope.Read) == WriteGate.NoGate
        p = EnvironmentPolicy.resolve("local", env_class="local", confirm=True)
        assert p.gate_for(AccessScope.Data) == WriteGate.Confirm
        assert p.gate_for(AccessScope.Read) == WriteGate.NoGate

    @mark.parametrize(
        "word,expected",
        [
            ("none", WriteGate.NoGate),
            (False, WriteGate.NoGate),
            ("prompt", WriteGate.Confirm),
            ("confirm", WriteGate.Confirm),
            (True, WriteGate.Confirm),
            ("environment-name", WriteGate.Typed),
            ("typed", WriteGate.Typed),
            ("reauthorize", WriteGate.Escalate),
            ("escalate", WriteGate.Escalate),
        ],
    )
    def test_level_spellings_old_and_new(self, word, expected):
        assert parse_gate(word) == expected

    def test_the_level_words_are_plain_english(self):
        assert [g.value for g in WriteGate] == [
            "none",
            "prompt",
            "environment-name",
            "reauthorize",
        ]

    def test_gate_severity_is_ordered(self):
        order = [
            WriteGate.NoGate,
            WriteGate.Confirm,
            WriteGate.Typed,
            WriteGate.Escalate,
        ]
        assert [g.severity for g in order] == sorted(g.severity for g in order)
