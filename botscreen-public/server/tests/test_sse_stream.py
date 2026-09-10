"""Tests for the SSE streaming slice (issue #65B).

Coverage:
- replay from seq 0 and Last-Event-ID / after_seq resume (max wins, malformed
  header ignored);
- heartbeats as comment frames (never carrying a seq);
- terminal uniqueness: the terminal frame is sent once and a reconnect at/after
  it closes immediately without resending;
- cancellation propagation on disconnect (and the opt-out path);
- HTTP framing: text/event-stream content-type, `id:`/`event:`/`data:` frames,
  ownership (403) and unknown-run (404) envelopes before streaming starts.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from pytest import mark

from app.api.v1.agent_api import SERVICE
from app.api.v1.auth import DevicePrincipal, get_device_principal
from app.api.v1.errors import AppError
from app.api.v1.sse_stream import (
    effective_after_seq,
    frame,
    keep_alive,
    stream_run_events,
)
from app.contracts.api import Channel, CreateRunRequest, CreateSessionRequest, RunInput
from app.contracts.run import RunState
from app.main import app

PRINCIPAL = DevicePrincipal(tenant_id="t1", device_id="d1")
OTHER_DEVICE = DevicePrincipal(tenant_id="t1", device_id="OTHER")

FAST = 100  # heartbeat bound (ms) — keeps tests quick


def _service():
    service = type(SERVICE)()
    session = service.create_session(
        PRINCIPAL, CreateSessionRequest(channel=Channel.TEXT)
    )
    run = service.create_run(
        PRINCIPAL,
        CreateRunRequest(
            session_id=session.session_id,
            idempotency_key="k1",
            input=RunInput(text="你好"),
        ),
        request_id="req-1",
        trace_id="trace-1",
    )
    return service, session, run


class TestCursorCombination:
    def test_last_event_id_and_query_take_max(self):
        assert effective_after_seq(0, "3") == 3
        assert effective_after_seq(5, "3") == 5
        assert effective_after_seq(0, None) == 0

    def test_malformed_header_is_ignored(self):
        assert effective_after_seq(4, "abc") == 4
        assert effective_after_seq(0, "  ") == 0
        assert effective_after_seq(0, "-2") == 0  # negative resume => start

    def test_frame_carries_id_event_and_data(self):
        service, _, run = _service()
        page = service.events(PRINCIPAL, run.run_id, 0)
        text = frame(page.events[0])
        assert text.startswith("id: 1\nevent: run.accepted\ndata: {")
        assert text.endswith("\n\n")
        payload = json.loads(text.split("data: ", 1)[1].strip())
        assert payload["seq"] == 1

    def test_keep_alive_is_a_comment_frame(self):
        assert keep_alive() == ": keep-alive\n\n"
        assert "id:" not in keep_alive()  # never perturbs the sequence authority


class TestStreamBehaviour:
    @mark.asyncio
    async def test_replay_then_heartbeat(self):
        service, _, run = _service()
        stream = stream_run_events(service, PRINCIPAL, run.run_id, heartbeat_ms=FAST)
        first = await anext(stream)
        assert first.startswith("id: 1\nevent: run.accepted")
        second = await asyncio.wait_for(anext(stream), timeout=1)
        assert second == ": keep-alive\n\n"
        await stream.aclose()

    @mark.asyncio
    async def test_resume_from_last_event_id(self):
        service, _, run = _service()
        service.cancel_run(PRINCIPAL, run.run_id)  # seq 2, terminal
        stream = stream_run_events(
            service,
            PRINCIPAL,
            run.run_id,
            last_event_id="1",
            heartbeat_ms=FAST,
            cancel_on_disconnect=False,
        )
        first = await anext(stream)
        assert first.startswith("id: 2\nevent: run.completed")
        with pytest.raises(StopAsyncIteration):
            await anext(stream)  # terminal closes the stream right after it

    @mark.asyncio
    async def test_reconnect_at_terminal_does_not_resend(self):
        service, _, run = _service()
        service.cancel_run(PRINCIPAL, run.run_id)
        stream = stream_run_events(
            service,
            PRINCIPAL,
            run.run_id,
            last_event_id="2",
            heartbeat_ms=FAST,
            cancel_on_disconnect=False,
        )
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=1)

    @mark.asyncio
    async def test_disconnect_cancels_the_run(self):
        service, _, run = _service()
        stream = stream_run_events(service, PRINCIPAL, run.run_id, heartbeat_ms=FAST)
        await anext(stream)  # accepted frame
        await stream.aclose()  # client goes away
        assert service.get_run(PRINCIPAL, run.run_id).state is RunState.CANCELLED

    @mark.asyncio
    async def test_disconnect_opt_out_leaves_run_running(self):
        service, _, run = _service()
        stream = stream_run_events(
            service,
            PRINCIPAL,
            run.run_id,
            heartbeat_ms=FAST,
            cancel_on_disconnect=False,
        )
        await anext(stream)
        await stream.aclose()
        assert service.get_run(PRINCIPAL, run.run_id).state is RunState.ACCEPTED

    @mark.asyncio
    async def test_terminal_run_is_not_cancelled_again(self):
        service, _, run = _service()
        service.cancel_run(PRINCIPAL, run.run_id)
        stream = stream_run_events(service, PRINCIPAL, run.run_id, heartbeat_ms=FAST)
        frames = [f async for f in stream]
        assert [f.split("\n", 1)[0] for f in frames] == ["id: 1", "id: 2"]

    @mark.asyncio
    async def test_foreign_principal_rejected_before_frames(self):
        service, _, run = _service()
        stream = stream_run_events(service, OTHER_DEVICE, run.run_id, heartbeat_ms=FAST)
        with pytest.raises(AppError) as exc:  # ownership enforced pre-frame
            await anext(stream)
        assert exc.value.code.value == "E_AUTHZ_FORBIDDEN"


class TestHttpStream:
    @pytest.fixture
    def client(self):
        SERVICE.sessions.clear()
        SERVICE.runs.clear()
        SERVICE.idempotency.clear()
        app.dependency_overrides[get_device_principal] = lambda: PRINCIPAL
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
        app.dependency_overrides.clear()
        SERVICE.sessions.clear()
        SERVICE.runs.clear()
        SERVICE.idempotency.clear()

    def _create_run(self, client) -> str:
        session = client.post("/api/v1/sessions", json={"channel": "text"}).json()
        run = client.post(
            "/api/v1/agent/runs",
            json={
                "session_id": session["session_id"],
                "idempotency_key": "k1",
                "input": {"text": "你好"},
            },
        ).json()
        return run["run_id"]

    def test_stream_framing_and_content_type(self, client):
        run_id = self._create_run(client)
        # cancel first: the run is terminal, so the stream closes by itself and
        # the TestClient context can exit deterministically (no live tail here)
        client.delete(f"/api/v1/agent/runs/{run_id}")
        url = (
            f"/api/v1/agent/runs/{run_id}/events/stream"
            f"?heartbeat_ms={FAST}&cancel_on_disconnect=false"
        )
        with client.stream("GET", url) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache"
            lines = response.iter_lines()
            assert next(lines) == "id: 1"
            assert next(lines) == "event: run.accepted"
            assert next(lines).startswith("data: {")
            assert next(lines) == ""  # frame separator
            assert next(lines) == "id: 2"
            assert next(lines) == "event: run.completed"

    def test_resume_header_skips_earlier_frames(self, client):
        run_id = self._create_run(client)
        client.delete(f"/api/v1/agent/runs/{run_id}")  # cancel -> seq 2 terminal
        url = (
            f"/api/v1/agent/runs/{run_id}/events/stream"
            f"?heartbeat_ms={FAST}&cancel_on_disconnect=false"
        )
        with client.stream("GET", url, headers={"Last-Event-ID": "1"}) as response:
            lines = response.iter_lines()
            assert next(lines) == "id: 2"
            assert next(lines) == "event: run.completed"

    def test_unknown_run_returns_404_envelope(self, client):
        response = client.get("/api/v1/agent/runs/ghost/events/stream")
        assert response.status_code == 404
        assert response.json()["code"] == "E_NOT_FOUND_RUN"

    def test_foreign_device_returns_403_envelope(self, client):
        run_id = self._create_run(client)
        app.dependency_overrides[get_device_principal] = lambda: OTHER_DEVICE
        response = client.get(f"/api/v1/agent/runs/{run_id}/events/stream")
        assert response.status_code == 403
        assert response.json()["code"] == "E_AUTHZ_FORBIDDEN"
