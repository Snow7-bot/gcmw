"""MockProvider (issue #38): deterministic provider for unit & contract tests.

Rules:
- never calls a real model and never uses real data (V2.3 §8.1 CI matrix);
- deterministic: identical requests produce identical responses;
- never echoes request content back (the default reply is a fixed string —
  a mock must not become an accidental data-echo channel);
- error scenarios are triggered by explicit test rules and map to stable
  ErrorCodes, so the #35 error matrix can be exercised through the gateway;
- responds to cancellation promptly (gateway/agent cancellation gate).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

from app.contracts.errors import ErrorCode
from app.contracts.model import (
    ContentType,
    ModelEvent,
    ModelEventType,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    ProviderHealth,
    ProviderStatus,
    RealtimeSessionInfo,
)
from app.providers.model_gateway import ModelGatewayError


class MockProvider:
    provider_id = "mock"

    def __init__(
        self,
        *,
        model_id: str = "mock-model",
        model_version: str = "1.0.0",
        available: bool = True,
        delay_ms: int = 0,
        canned: dict[str, str] | None = None,
        error_triggers: dict[str, ErrorCode] | None = None,
        supports_function_calling: bool = True,
    ) -> None:
        self.model_id = model_id
        self.model_version = model_version
        self._available = available
        self._delay_ms = delay_ms
        # request-text substring -> canned reply / forced ErrorCode
        self._canned: dict[str, str] = dict(canned or {})
        self._error_triggers: dict[str, ErrorCode] = dict(error_triggers or {})
        self.supports_function_calling = supports_function_calling
        self.sessions_opened = 0
        self.stream_cancelled = False
        self.stream_finished = False

    # -- helpers -------------------------------------------------------------

    def _last_user_text(self, request: ModelRequest) -> str:
        for message in reversed(request.messages):
            role = message.get("role")
            content = message.get("content")
            if role == "user" and isinstance(content, str):
                return content
            if role == "user" and isinstance(content, list):
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "text"
                        and part.get("text")
                    ):
                        return str(part["text"])
        return ""

    def _apply_rules(self, request: ModelRequest) -> None:
        """Raise when the request matches an error trigger rule.

        Canned replies are resolved separately in ``_reply_for``; rules only
        cover forced-error scenarios used by the contract regression matrix.
        """
        text = self._last_user_text(request)
        for needle, code in self._error_triggers.items():
            if needle in text:
                raise ModelGatewayError(code, f"mock trigger {needle!r}")

    def _reply_for(self, request: ModelRequest) -> str:
        text = self._last_user_text(request)
        for needle, reply in self._canned.items():
            if needle in text:
                return reply
        return "mock-ok"

    async def _maybe_delay(self) -> None:
        if self._delay_ms:
            await asyncio.sleep(self._delay_ms / 1000)

    # -- ProviderAdapter contract --------------------------------------------

    async def chat(self, request: ModelRequest) -> ModelResponse:
        await self._maybe_delay()
        self._apply_rules(request)
        return ModelResponse(
            provider_id=self.provider_id,
            model_id=self.model_id,
            model_version=self.model_version,
            content=self._reply_for(request),
            finish_reason="stop",
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Yields one DELTA (default/canned reply) then a DONE event.

        Cancellation (aclose / task cancel) is observed via ``stream_cancelled``
        so tests can assert the provider reacts promptly.
        """
        try:
            await self._maybe_delay()
            self._apply_rules(request)
            text = self._reply_for(request)
            # one delta per 8 characters keeps the stream observable
            for i in range(0, len(text), 8):
                chunk = text[i : i + 8]
                yield ModelEvent(
                    type=ModelEventType.DELTA,
                    provider_id=self.provider_id,
                    model_id=self.model_id,
                    model_version=self.model_version,
                    data={"delta": chunk},
                )
            yield ModelEvent(
                type=ModelEventType.DONE,
                provider_id=self.provider_id,
                model_id=self.model_id,
                model_version=self.model_version,
            )
        except (GeneratorExit, asyncio.CancelledError):
            # cancelled at an await point (aclose/task cancel): observed
            # distinctly from a normal completion
            self.stream_cancelled = True
            raise
        finally:
            self.stream_finished = True

    async def open_realtime_session(self, request: ModelRequest) -> RealtimeSessionInfo:
        await self._maybe_delay()
        self._apply_rules(request)
        self.sessions_opened += 1
        return RealtimeSessionInfo(
            session_id=f"mock-session-{uuid.uuid4().hex[:16]}",
            provider_id=self.provider_id,
            model_id=self.model_id,
        )

    def is_available(self) -> bool:
        return self._available

    async def health(self) -> ProviderHealth:
        await self._maybe_delay()
        return ProviderHealth(
            provider_id=self.provider_id,
            status=ProviderStatus.AVAILABLE
            if self._available
            else ProviderStatus.UNAVAILABLE,
            latency_ms=0,
        )

    def model_info(self) -> ModelInfo:
        return ModelInfo(
            provider_id=self.provider_id,
            model_id=self.model_id,
            model_version=self.model_version,
            supported_modalities=[ContentType.TEXT],
            supports_function_calling=self.supports_function_calling,
            supports_streaming=True,
            supports_realtime=True,
            supports_json_schema=True,
        )
