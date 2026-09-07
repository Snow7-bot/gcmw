"""Tests for the AgentRegistry (issue #39)."""

import pytest

from app.agents.registry import AgentRegistry, RegistryError
from app.contracts.agent import AgentManifest, RiskLevel
from app.contracts.errors import ErrorCode


def _manifest(agent_id: str, intents=None, **overrides):
    fields = {
        "agent_id": agent_id,
        "version": "1.0.0",
        "supported_intents": intents or [],
        "risk_level": RiskLevel.LOW,
    }
    fields.update(overrides)
    return AgentManifest(**fields)


@pytest.fixture
def registry():
    reg = AgentRegistry()
    reg.register(_manifest("manager", ["route"]))
    reg.register(_manifest("medical_qa", ["knowledge"], risk_level=RiskLevel.MEDIUM))
    reg.register(_manifest("verifier", ["verify"]))
    return reg


class TestRegistration:
    def test_register_and_list(self, registry):
        ids = [m.agent_id for m in registry.list_agents()]
        assert ids == ["manager", "medical_qa", "verifier"]

    def test_duplicate_registration_rejected(self, registry):
        with pytest.raises(RegistryError) as exc:
            registry.register(_manifest("manager"))
        assert exc.value.code is ErrorCode.CONFLICT_IDEMPOTENCY

    def test_unregister_and_get_missing(self, registry):
        registry.unregister("manager")
        with pytest.raises(RegistryError) as exc:
            registry.get("manager")
        assert exc.value.code is ErrorCode.NOT_FOUND_AGENT

    def test_get_and_len(self, registry):
        assert registry.get("manager").agent_id == "manager"
        assert len(registry) == 3


class TestEnableDisable:
    def test_disable_then_not_routable(self, registry):
        registry.disable("medical_qa")
        assert registry.is_enabled("medical_qa") is False
        assert registry.resolve("knowledge") is None
        # re-enable restores routing
        registry.enable("medical_qa")
        assert registry.resolve("knowledge").agent_id == "medical_qa"

    def test_disable_unknown_agent(self, registry):
        with pytest.raises(RegistryError) as exc:
            registry.disable("ghost")
        assert exc.value.code is ErrorCode.NOT_FOUND_AGENT

    def test_manifest_carries_enabled_flag(self, registry):
        manifest = registry.disable("manager")
        assert manifest.enabled is False
        assert registry.get("manager").enabled is False


class TestRouting:
    def test_resolve_first_registered_enabled_wins(self):
        reg = AgentRegistry()
        reg.register(_manifest("a", ["x"]))
        reg.register(_manifest("b", ["x"]))
        assert reg.resolve("x").agent_id == "a"

    def test_resolve_none_for_unknown_intent(self, registry):
        assert registry.resolve("does-not-exist") is None

    def test_disabled_agent_never_resolved_even_if_first(self, registry):
        reg = AgentRegistry()
        first = _manifest("first", ["x"])
        reg.register(first)
        reg.disable("first")
        reg.register(_manifest("second", ["x"]))
        assert reg.resolve("x").agent_id == "second"
