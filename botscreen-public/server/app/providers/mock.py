"""Mock provider for tests and local development without real model access."""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from ..contracts.model import ModelEvent, ModelRequest, ModelResponse
from .base import BaseModelProvider


class MockProvider(BaseModelProvider):
    provider_id = "mock"
    model_id = "mock-model"
    model_version = "0.0.0"

    async def chat(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            provider_id=self.provider_id,
            model_id=self.model_id,
            model_version=self.model_version,
            content="[mock answer]",
            finish_reason="stop",
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        for chunk in ["[mock ", "answer]"]:
            await asyncio.sleep(0)
            yield ModelEvent(type="delta", provider_id=self.provider_id, model_id=self.model_id, data={"delta": chunk})

    def is_available(self) -> bool:
        return True

    def health(self) -> dict[str, Any]:
        return {"provider": self.provider_id, "status": "ok", "model": self.model_id}
