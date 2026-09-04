"""Base Agent interface."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..contracts.agent import AgentContext, AgentManifest, AgentResult


class BaseAgent(ABC):
    manifest: AgentManifest

    @abstractmethod
    async def run(self, context: AgentContext) -> AgentResult:
        raise NotImplementedError
