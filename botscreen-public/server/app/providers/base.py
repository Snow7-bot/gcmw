"""ModelGateway provider contract."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from ..contracts.model import ModelEvent, ModelRequest, ModelResponse


class BaseModelProvider(ABC):
    provider_id: str
    model_id: str
    model_version: str = ""

    @abstractmethod
    async def chat(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError

    @abstractmethod
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        raise NotImplementedError

    @abstractmethod
    def is_available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def health(self) -> dict[str, Any]:
        raise NotImplementedError
