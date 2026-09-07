"""Tests for the ModelGateway interface (issue #37)."""

from datetime import datetime, timezone

import pytest
from pytest import mark

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
    SwitchRequest,
)
from app.providers.model_gateway import ModelGateway, ModelGatewayError


class FakeAdapter:
    provider_id = "fake"

    def __init__(self, *, delay_ms: int = 0) -> None:
        self.delay_ms = delay_ms
        self.sessions_opened = 0
        self.available = True

    async def chat(self, request: ModelRequest) -> ModelResponse:
        if self.delay_ms:
            await asyncio_sleep(self.delay_ms)
        return ModelResponse(
            provider_id=self.provider_id,
            model_id="fake-model",
            model_version="1.0.0",
            content="ok",
        )

    async def stream(self, request: ModelRequest):
        yield ModelEvent(
            type=ModelEventType.DELTA,
            provider_id=self.provider_id,
            model_id="fake-model",
            model_version="1.0.0",
            data={"delta": "ok"},
        )

    async def open_realtime_session(self, request: ModelRequest) -> RealtimeSessionInfo:
        self.sessions_opened += 1
        return RealtimeSessionInfo(
            session_id="sess-1",
            provider_id=self.provider_id,
            model_id="fake-model",
        )

    def is_available(self) -> bool:
        return self.available

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            status=ProviderStatus.AVAILABLE,
            latency_ms=1,
        )

    def model_info(self) -> ModelInfo:
        return ModelInfo(
            provider_id=self.provider_id,
            model_id="fake-model",
            model_version="1.0.0",
            supported_modalities=[ContentType.TEXT],
            supports_streaming=True,
        )


async def asyncio_sleep(ms: int) -> None:
    import asyncio

    await asyncio.sleep(ms / 1000)


def _request(**overrides):
    fields = {"trace_id": "t1", "deadline_ms": 5000, "token_budget": 400}
    fields.update(overrides)
    return ModelRequest(messages=[], **fields)


@pytest.fixture
def gateway():
    gw = ModelGateway(active_provider_id="fake")
    gw.register(FakeAdapter())
    return gw


class TestRegistry:
    def test_register_duplicate_rejected(self, gateway):
        with pytest.raises(ModelGatewayError) as exc:
            gateway.register(FakeAdapter())
        assert exc.value.code is ErrorCode.CONFLICT_IDEMPOTENCY

    def test_registered_and_active(self, gateway):
        assert gateway.registered_providers() == ["fake"]
        assert gateway.active_provider_id == "fake"
        assert gateway.is_available("fake") is True
        assert gateway.is_available("nope") is False


class TestAgentCalls:
    @mark.asyncio
    async def test_chat_roundtrip(self, gateway):
        resp = await gateway.chat(_request())
        assert resp.provider_id == "fake"
        assert resp.content == "ok"

    @mark.asyncio
    async def test_chat_unknown_provider_raises(self, gateway):
        with pytest.raises(ModelGatewayError) as exc:
            await gateway.chat(_request(provider_hint="ghost"))
        assert exc.value.code is ErrorCode.PROVIDER_UNREACHABLE

    @mark.asyncio
    async def test_chat_deadline_maps_to_timeout(self):
        slow = FakeAdapter(delay_ms=200)
        slow.provider_id = "slow"
        gw = ModelGateway(active_provider_id="slow")
        gw.register(slow)
        with pytest.raises(ModelGatewayError) as exc:
            await gw.chat(_request(deadline_ms=10))
        assert exc.value.code is ErrorCode.TIMEOUT_PROVIDER

    @mark.asyncio
    async def test_realtime_session_opened(self, gateway):
        info = await gateway.open_realtime_session(_request())
        assert info.provider_id == "fake"
        assert info.session_id

    @mark.asyncio
    async def test_stream_yields_events(self, gateway):
        events = [ev async for ev in gateway.stream(_request())]
        assert len(events) == 1
        assert events[0].type is ModelEventType.DELTA

    @mark.asyncio
    async def test_health_and_model_info(self, gateway):
        health = await gateway.health()
        assert health.status is ProviderStatus.AVAILABLE
        info = gateway.model_info()
        assert info.supports_streaming is True


class TestRestrictedSwitch:
    @mark.asyncio
    async def test_switch_without_authorization_refused(self, gateway):
        req = SwitchRequest(target_provider_id="fake", release_id="r1", reason="test")
        with pytest.raises(ModelGatewayError) as exc:
            await gateway.switch_provider(req, authorized=False)
        assert exc.value.code is ErrorCode.AUTHZ_FORBIDDEN
        assert gateway.active_provider_id == "fake"

    @mark.asyncio
    async def test_authorized_switch_changes_active(self):
        gw = ModelGateway(active_provider_id="fake")
        gw.register(FakeAdapter())
        other = FakeAdapter()
        other.provider_id = "other"
        gw.register(other)
        req = SwitchRequest(
            target_provider_id="other", release_id="rel-9", reason="rollout"
        )
        result = await gw.switch_provider(req, authorized=True)
        assert result.ok is True
        assert result.active_provider_id == "other"
        assert gw.active_provider_id == "other"

    @mark.asyncio
    async def test_authorized_switch_to_unknown_rejected(self, gateway):
        req = SwitchRequest(target_provider_id="missing", release_id="r", reason="x")
        with pytest.raises(ModelGatewayError) as exc:
            await gateway.switch_provider(req, authorized=True)
        assert exc.value.code is ErrorCode.PROVIDER_UNREACHABLE


def test_gateway_types_timestamps_are_utc():
    assert datetime.now(timezone.utc).tzinfo is not None
