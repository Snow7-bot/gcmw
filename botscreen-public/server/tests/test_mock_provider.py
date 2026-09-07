"""Tests for MockProvider (issue #38)."""

import asyncio

import pytest
from pytest import mark

from app.contracts.errors import ErrorCode
from app.contracts.model import (
    ContentType,
    ModelEventType,
    ModelRequest,
    ProviderStatus,
)
from app.providers.mock import MockProvider
from app.providers.model_gateway import ModelGateway, ModelGatewayError


def _request(text: str = "hi", **overrides):
    fields = {"trace_id": "t1", "deadline_ms": 5000, "token_budget": 400}
    fields.update(overrides)
    return ModelRequest(messages=[{"role": "user", "content": text}], **fields)


class TestChat:
    @mark.asyncio
    async def test_default_reply_is_fixed_and_does_not_echo_input(self):
        provider = MockProvider()
        resp = await provider.chat(_request("please echo my secret: hunter2"))
        assert resp.content == "mock-ok"
        assert "hunter2" not in resp.content
        assert resp.provider_id == "mock"
        assert resp.finish_reason == "stop"

    @mark.asyncio
    async def test_deterministic_same_input_same_output(self):
        provider = MockProvider(canned={"近视": "近视后需要定期复查（已审核样例）"})
        a = await provider.chat(_request("孩子近视后需要复查吗"))
        b = await provider.chat(_request("孩子近视后需要复查吗"))
        assert a.content == b.content
        assert "复查" in a.content

    @mark.asyncio
    async def test_error_trigger_maps_to_stable_code(self):
        provider = MockProvider(error_triggers={"触发超时": ErrorCode.TIMEOUT_PROVIDER})
        with pytest.raises(ModelGatewayError) as exc:
            await provider.chat(_request("请触发超时场景"))
        assert exc.value.code is ErrorCode.TIMEOUT_PROVIDER


class TestStream:
    @mark.asyncio
    async def test_stream_yields_delta_then_done(self):
        provider = MockProvider()
        events = [ev async for ev in provider.stream(_request("mock-ok"))]
        assert events[-1].type is ModelEventType.DONE
        deltas = "".join(
            ev.data["delta"] for ev in events if ev.type is ModelEventType.DELTA
        )
        assert deltas == "mock-ok"
        # normal completion must not be flagged as a cancellation
        assert provider.stream_cancelled is False
        assert provider.stream_finished is True

    @mark.asyncio
    async def test_stream_cancellation_is_observed(self):
        provider = MockProvider(delay_ms=200)

        async def consume():
            async for _ in provider.stream(_request("slow")):
                pass

        task = asyncio_create_task(consume())
        await asyncio_sleep(50)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.stream_cancelled is True

    @mark.asyncio
    async def test_stream_error_trigger(self):
        provider = MockProvider(error_triggers={"x": ErrorCode.MODEL_GENERATION_FAILED})
        with pytest.raises(ModelGatewayError) as exc:
            async for _ in provider.stream(_request("trigger x")):
                pass
        assert exc.value.code is ErrorCode.MODEL_GENERATION_FAILED


class TestRealtimeAndInfo:
    @mark.asyncio
    async def test_realtime_session_has_no_secrets(self):
        provider = MockProvider()
        info = await provider.open_realtime_session(_request())
        assert provider.sessions_opened == 1
        assert info.session_id
        assert info.provider_id == "mock"
        payload = info.model_dump()
        for key in ("url", "api_key", "token", "endpoint"):
            assert key not in payload
        for value in payload.values():
            assert "http" not in str(value)

    def test_health_and_info(self):
        provider = MockProvider()
        assert provider.is_available() is True
        assert provider.model_info().supported_modalities == [ContentType.TEXT]
        assert provider.model_info().supports_streaming is True
        assert provider.model_info().supports_realtime is True

    @mark.asyncio
    async def test_unavailable_reports_status(self):
        provider = MockProvider(available=False)
        assert provider.is_available() is False
        health = await provider.health()
        assert health.status is ProviderStatus.UNAVAILABLE


class TestGatewayIntegration:
    """#37 acceptance: MockProvider runs through the ModelGateway."""

    @mark.asyncio
    async def test_gateway_chat_and_stream_with_mock(self):
        gw = ModelGateway(active_provider_id="mock")
        gw.register(MockProvider())
        resp = await gw.chat(_request("ok"))
        assert resp.provider_id == "mock"
        events = [ev async for ev in gw.stream(_request("ok"))]
        assert events[-1].type is ModelEventType.DONE
        assert gw.is_available("mock") is True
        assert gw.model_info("mock").supports_json_schema is True


def asyncio_create_task(coro):
    return asyncio.get_running_loop().create_task(coro)


async def asyncio_sleep(ms: int) -> None:
    await asyncio.sleep(ms / 1000)
