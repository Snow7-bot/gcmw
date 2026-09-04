"""In-process Agent registry."""
from __future__ import annotations

from ..contracts.agent import AgentManifest
from .base import BaseAgent


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> None:
        if not agent.manifest.enabled:
            return
        self._agents[agent.manifest.agent_id] = agent

    def get(self, agent_id: str) -> BaseAgent | None:
        return self._agents.get(agent_id)

    def list_manifests(self) -> list[AgentManifest]:
        return [agent.manifest for agent in self._agents.values()]
