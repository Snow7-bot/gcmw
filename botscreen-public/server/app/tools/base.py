"""Tool Gateway base contract."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..contracts.agent import ToolRequest, ToolResult


class BaseTool(ABC):
    name: str

    @abstractmethod
    async def execute(self, request: ToolRequest) -> ToolResult:
        raise NotImplementedError
