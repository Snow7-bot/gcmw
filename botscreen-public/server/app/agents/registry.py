"""In-process AgentRegistry (issue #39).

- manifests are registered per agent (unique agent_id, versioned);
- agents can be enabled/disabled at runtime — a disabled agent is never
  resolved by intent routing (Manager routing contract);
- routing is deterministic: the first registered (and enabled) agent whose
  ``supported_intents`` matches wins;
- agents never talk to each other directly — the registry is the single
  dispatch source for Manager (V2.3 §6.4).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.contracts.agent import AgentManifest
from app.contracts.errors import ErrorCode


class RegistryError(RuntimeError):
    """Registry failure carrying a stable ErrorCode (mapped to envelopes by #36)."""

    def __init__(self, code: ErrorCode, message: str = "") -> None:
        super().__init__(message or code.value)
        self.code = code


@dataclass
class AgentEntry:
    manifest: AgentManifest
    registered_order: int


class AgentRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, AgentEntry] = {}
        self._order = 0

    # -- registration --------------------------------------------------------

    def register(self, manifest: AgentManifest) -> None:
        """Register an agent manifest. Duplicate agent_id is rejected; a newer
        version of an existing agent must be registered under the same id only
        through explicit replacement (not supported in v1) — version bumps ship
        as new registry state via the release flow."""
        if manifest.agent_id in self._entries:
            raise RegistryError(
                ErrorCode.CONFLICT_IDEMPOTENCY,
                f"agent {manifest.agent_id!r} already registered",
            )
        self._entries[manifest.agent_id] = AgentEntry(
            manifest=manifest, registered_order=self._order
        )
        self._order += 1

    def unregister(self, agent_id: str) -> None:
        if agent_id not in self._entries:
            raise RegistryError(
                ErrorCode.NOT_FOUND_AGENT, f"agent {agent_id!r} not found"
            )
        del self._entries[agent_id]

    # -- enable / disable ----------------------------------------------------

    def set_enabled(self, agent_id: str, enabled: bool) -> AgentManifest:
        """Enable or disable an agent. Disabled agents are never routed to."""
        entry = self._entries.get(agent_id)
        if entry is None:
            raise RegistryError(
                ErrorCode.NOT_FOUND_AGENT, f"agent {agent_id!r} not found"
            )
        # manifest dataclass holds pydantic model — replace with toggled copy
        toggled = entry.manifest.model_copy(update={"enabled": enabled})
        self._entries[agent_id] = AgentEntry(
            manifest=toggled, registered_order=entry.registered_order
        )
        return toggled

    def enable(self, agent_id: str) -> AgentManifest:
        return self.set_enabled(agent_id, True)

    def disable(self, agent_id: str) -> AgentManifest:
        return self.set_enabled(agent_id, False)

    # -- queries -------------------------------------------------------------

    def get(self, agent_id: str) -> AgentManifest:
        entry = self._entries.get(agent_id)
        if entry is None:
            raise RegistryError(
                ErrorCode.NOT_FOUND_AGENT, f"agent {agent_id!r} not found"
            )
        return entry.manifest

    def is_enabled(self, agent_id: str) -> bool:
        return self.get(agent_id).enabled

    def list_agents(self) -> list[AgentManifest]:
        """All manifests in registration order (enabled and disabled)."""
        return [
            e.manifest
            for e in sorted(self._entries.values(), key=lambda e: e.registered_order)
        ]

    def resolve(self, intent: str) -> AgentManifest | None:
        """Deterministic intent routing: first enabled agent that declares the
        intent. Returns None when no enabled agent can handle it."""
        for entry in sorted(self._entries.values(), key=lambda e: e.registered_order):
            manifest = entry.manifest
            if manifest.enabled and intent in manifest.supported_intents:
                return manifest
        return None

    def __len__(self) -> int:
        return len(self._entries)
