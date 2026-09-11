"""Public SSE route tests (#65B-2 slice B2-B).

Scope guard — this slice ships the ROUTE only:
- authentication and ownership complete BEFORE the first byte;
- ``Last-Event-ID`` resume (plus the explicit ``after_seq`` cursor);
- ``Cache-Control: no-store``;
- a fixed server-side heartbeat that client input cannot influence;
- structured error mapping (JSON envelopes before the stream, one
  ``stream.error`` envelope frame after it).

Deliberately NOT here (B2-C): subscriber leases, connection counting, reconnect
grace and disconnect->cancel propagation. The disconnect test below asserts
that boundary instead of assuming it.

Two test vehicles, on purpose:
* FINITE streams (terminal run, pre-stream errors) go through ``TestClient``;
* OPEN-ENDED streams (idle run + heartbeat, early disconnect) call the real
  route coroutine and read a bounded number of chunks, because Starlette's
  ``TestClient`` buffers a whole response body before returning and could never
  observe a stream that is still open.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sse_frames import (
    event_ids,
    event_names,
    event_payload,
    parse_frames,
    protocol_frames,
)

from app.api.v1 import agent_api
from app.api.v1.agent_api import SERVICE
from app.api.v1.auth import DevicePrincipal, get_device_principal
from app.contracts.api import Channel, CreateRunRequest, CreateSessionRequest, RunInput
from app.contracts.errors import ErrorEnvelope
from app.contracts.run import RunState
from app.main import app
from app.storage.run_repository import (
    MemoryRunRepository,
    RunIdentity,
    RunRepositoryError,
    RunRepositoryFault,
)

PRINCIPAL = DevicePrincipal(tenant_id="t1", device_id="d1")
OTHER_DEVICE = DevicePrincipal(tenant_id="t1", device_id="OTHER")
OTHER_TENANT = DevicePrincipal(tenant_id="t2", device_id="d1")


@pytest.fixture
def client():
    """A client on FRESH service state.

    The repository is rebuilt per test on purpose: an ``asyncio`` lock/event
    binds to the loop that first contends on it, and every ``TestClient`` runs
    the app on its own portal loop.
    """
    SERVICE.repository = MemoryRunRepository()
    SERVICE.sessions.clear()
    SERVICE.runs.clear()
    SERVICE.idempotency.clear()
    SERVICE._locks.clear()
    app.dependency_overrides[get_device_principal] = lambda: PRINCIPAL
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()
    SERVICE.sessions.clear()
    SERVICE.runs.clear()
    SERVICE.idempotency.clear()


def _env(response):
    return ErrorEnvelope.model_validate(response.json())


def _new_run(client, text: str = "hi", key: str = "k") -> dict:
    session = client.post("/api/v1/sessions", json={"channel": "text"}).json()
    res = client.post(
        "/api/v1/agent/runs",
        json={
            "session_id": session["session_id"],
            "input": {"type": "text", "text": text},
            "idempotency_key": key,
        },
    )
    assert res.status_code == 200
    return res.json()


def _events_url(run_id: str) -> str:
    return f"/api/v1/agent/runs/{run_id}/events"


# ---------------------------------------------------------------------------
# Direct-route vehicle for open-ended streams
# ---------------------------------------------------------------------------


def _request(headers: dict[str, str] | None = None, query: str = "") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/agent/runs/x/events",
            "query_string": query.encode(),
            "headers": raw,
            "state": {},
        }
    )


async def _seed_run(text: str = "hi", key: str = "k") -> dict:
    """Create session + run through the same service the routes use."""
    session_id = SERVICE.create_session(
        PRINCIPAL, CreateSessionRequest(channel=Channel.TEXT, locale="zh-CN")
    ).session_id
    snap = await SERVICE.create_run(
        PRINCIPAL,
        CreateRunRequest(
            session_id=session_id,
            input=RunInput(type="text", text=text),
            idempotency_key=key,
        ),
        "req",
        "trace",
    )
    return {"run_id": snap.run_id, "session_id": session_id}


async def _read(response, *, chunks: int = 1, timeout_s: float = 2.0) -> list[str]:
    """Read a BOUNDED number of chunks from an open stream."""
    collected: list[str] = []

    async def pump() -> None:
        async for chunk in response.body_iterator:
            collected.append(chunk)
            if len(collected) >= chunks:
                return

    await asyncio.wait_for(pump(), timeout_s)
    return collected


async def _disconnect(response) -> None:
    """Close the body iterator the way a disconnecting client does."""
    aclose = getattr(response.body_iterator, "aclose", None)
    if aclose is not None:
        await aclose()


class TestStreamAuthBoundary:
    """Nothing streams for an unauthenticated, foreign or unknown run."""

    def test_anonymous_is_rejected_with_json_envelope(self):
        with TestClient(app, raise_server_exceptions=False) as anon:
            res = anon.get(_events_url("whatever"))
        assert res.status_code == 401
        assert _env(res).code == "E_AUTH_MISSING_CREDENTIALS"
        assert res.headers["content-type"].startswith("application/json")
        assert "data:" not in res.text

    def test_foreign_device_is_forbidden_before_first_byte(self, client):
        run = _new_run(client)
        app.dependency_overrides[get_device_principal] = lambda: OTHER_DEVICE
        try:
            res = client.get(_events_url(run["run_id"]))
        finally:
            app.dependency_overrides[get_device_principal] = lambda: PRINCIPAL
        assert res.status_code == 403
        assert _env(res).code == "E_AUTHZ_FORBIDDEN"
        assert res.headers["content-type"].startswith("application/json")
        assert "data:" not in res.text

    def test_foreign_tenant_is_forbidden_before_first_byte(self, client):
        run = _new_run(client)
        app.dependency_overrides[get_device_principal] = lambda: OTHER_TENANT
        try:
            res = client.get(_events_url(run["run_id"]))
        finally:
            app.dependency_overrides[get_device_principal] = lambda: PRINCIPAL
        assert res.status_code == 403
        assert _env(res).code == "E_AUTHZ_FORBIDDEN"

    def test_unknown_run_is_not_found_before_first_byte(self, client):
        res = client.get(_events_url("ghost"))
        assert res.status_code == 404
        assert _env(res).code == "E_NOT_FOUND_RUN"
        assert res.headers["content-type"].startswith("application/json")

    def test_authorization_happens_exactly_once_per_stream(self, client):
        """One ownership read feeds the whole stream (no per-page re-check)."""
        run = _new_run(client)
        assert client.delete(f"/api/v1/agent/runs/{run['run_id']}").status_code == 200
        calls = {"n": 0}
        original = SERVICE.authorize_stream

        async def counting(principal, run_id):
            calls["n"] += 1
            return await original(principal, run_id)

        SERVICE.authorize_stream = counting
        try:
            res = client.get(_events_url(run["run_id"]))
        finally:
            SERVICE.authorize_stream = original
        assert res.status_code == 200
        assert calls["n"] == 1


class TestFiniteStreams:
    """Terminal runs close the stream, so the whole body is observable."""

    def test_terminal_run_streams_validated_frames_and_closes(self, client):
        run = _new_run(client)
        assert client.delete(f"/api/v1/agent/runs/{run['run_id']}").status_code == 200

        res = client.get(_events_url(run["run_id"]))
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        assert res.headers["cache-control"] == "no-store"

        frames = protocol_frames(res.text)
        assert event_names(frames) == ["run.accepted", "run.completed"]
        assert event_ids(frames) == [1, 2]
        # the wire id IS the run seq (resume authority), and the payload agrees
        for frame in frames:
            assert frame["data"]["seq"] == frame["id"]
            assert frame["data"]["run_id"] == run["run_id"]
            assert frame["data"]["protocol_version"] == "1.0"
        # the terminal status is derived from the committed target state
        assert event_payload(frames[-1]) == {"status": "cancelled"}

    def test_last_event_id_resumes_without_replay(self, client):
        run = _new_run(client)
        client.delete(f"/api/v1/agent/runs/{run['run_id']}")

        res = client.get(_events_url(run["run_id"]), headers={"Last-Event-ID": "1"})
        frames = protocol_frames(res.text)
        assert event_ids(frames) == [2]
        assert event_names(frames) == ["run.completed"]

    def test_after_seq_query_resumes(self, client):
        run = _new_run(client)
        client.delete(f"/api/v1/agent/runs/{run['run_id']}")
        res = client.get(_events_url(run["run_id"]) + "?after_seq=1")
        assert event_ids(protocol_frames(res.text)) == [2]

    def test_malformed_last_event_id_falls_back_to_the_cursor(self, client):
        run = _new_run(client)
        client.delete(f"/api/v1/agent/runs/{run['run_id']}")
        res = client.get(
            _events_url(run["run_id"]), headers={"Last-Event-ID": "not-a-seq"}
        )
        assert event_ids(protocol_frames(res.text)) == [1, 2]

    def test_max_of_header_and_cursor_wins(self, client):
        run = _new_run(client)
        client.delete(f"/api/v1/agent/runs/{run['run_id']}")
        res = client.get(
            _events_url(run["run_id"]) + "?after_seq=0",
            headers={"Last-Event-ID": "1"},
        )
        assert event_ids(protocol_frames(res.text)) == [2]

    def test_cursor_never_goes_backwards_with_a_lower_header(self, client):
        run = _new_run(client)
        client.delete(f"/api/v1/agent/runs/{run['run_id']}")
        res = client.get(
            _events_url(run["run_id"]) + "?after_seq=2",
            headers={"Last-Event-ID": "1"},
        )
        assert protocol_frames(res.text) == []  # terminal frame already held


class TestOpenStreams:
    """Idle streams: first frame, heartbeat, client-controlled window."""

    def test_first_frame_of_a_non_terminal_run_is_accepted(self, client):
        async def main():
            run = await _seed_run(key="first")
            response = await agent_api.stream_run_events(
                run["run_id"], _request(), PRINCIPAL, after_seq=0
            )
            chunks = await _read(response, chunks=1)
            await _disconnect(response)
            return chunks

        frames = protocol_frames("".join(asyncio.run(main())))
        assert event_names(frames) == ["run.accepted"]
        assert event_ids(frames) == [1]

    def test_idle_stream_emits_comment_keepalive_without_seq_progress(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(agent_api, "SSE_HEARTBEAT_S", 0.05)

        async def main():
            run = await _seed_run(key="hb")
            # cursor already at the newest seq: the only legal traffic is a
            # heartbeat, and it must not carry an id/seq
            response = await agent_api.stream_run_events(
                run["run_id"],
                _request(headers={"last-event-id": "1"}),
                PRINCIPAL,
                after_seq=1,
            )
            chunks = await _read(response, chunks=1)
            await _disconnect(response)
            return chunks

        frames = parse_frames("".join(asyncio.run(main())))
        assert frames and frames[0].get("comment") == "keep-alive"
        assert "id" not in frames[0] and "event" not in frames[0]

    def test_heartbeat_window_is_a_server_constant(self):
        """The window is a server constant: request input cannot change it."""
        params = inspect.signature(agent_api.stream_run_events).parameters
        assert set(params) == {"run_id", "request", "principal", "after_seq"}
        assert agent_api.SSE_HEARTBEAT_S == 15.0

    def test_client_supplied_heartbeat_is_ignored(self, client, monkeypatch):
        """A huge ``heartbeat_s`` in the query must NOT delay the keep-alive."""
        monkeypatch.setattr(agent_api, "SSE_HEARTBEAT_S", 0.05)

        async def main():
            run = await _seed_run(key="hb2")
            response = await agent_api.stream_run_events(
                run["run_id"],
                _request(query="heartbeat_s=99999", headers={"last-event-id": "1"}),
                PRINCIPAL,
                after_seq=1,
            )
            chunks = await _read(response, chunks=1, timeout_s=1.0)
            await _disconnect(response)
            return chunks

        frames = parse_frames("".join(asyncio.run(main())))
        assert frames and frames[0].get("comment") == "keep-alive"


class TestDisconnectBoundary:
    """B2-C owns leases/cancellation: a dropped stream must change nothing."""

    def test_disconnect_does_not_cancel_or_lease_the_run(self, client):
        async def main():
            run = await _seed_run(key="dc")
            touched: list[str] = []
            repository = SERVICE.repository
            original_commit = repository.commit_transition
            original_delete = repository.delete

            async def spy_commit(identity, **kwargs):
                touched.append("commit")
                return await original_commit(identity, **kwargs)

            async def spy_delete(identity):
                touched.append("delete")
                return await original_delete(identity)

            repository.commit_transition = spy_commit
            repository.delete = spy_delete
            try:
                response = await agent_api.stream_run_events(
                    run["run_id"], _request(), PRINCIPAL, after_seq=0
                )
                await _read(response, chunks=1)
                await _disconnect(response)
            finally:
                repository.commit_transition = original_commit
                repository.delete = original_delete

            identity = RunIdentity(
                run_id=run["run_id"],
                tenant_id=PRINCIPAL.tenant_id,
                device_id=PRINCIPAL.device_id,
                session_id=run["session_id"],
            )
            state = await SERVICE.repository.state(identity)
            return touched, state, SERVICE.runs[run["run_id"]].state

        touched, durable_state, mirror_state = asyncio.run(main())
        assert touched == []  # no cancel, no write, no lease bookkeeping
        assert durable_state is RunState.ACCEPTED
        assert mirror_state is RunState.ACCEPTED

    def test_stream_does_not_track_subscribers(self, client):
        """No subscriber/lease state exists yet — that is B2-C's slice."""
        for attr in ("subscribers", "leases", "connections", "subscriber_count"):
            assert not hasattr(SERVICE, attr)
            assert not hasattr(SERVICE.repository, attr)


class TestStructuredErrorMapping:
    def test_storage_fault_mid_stream_becomes_one_envelope_frame(self, client):
        run = _new_run(client)

        async def failing_snapshot(identity, cursor, timeout_s):
            raise RunRepositoryError(RunRepositoryFault.UNAVAILABLE, "redis is down")

        SERVICE.repository.snapshot = failing_snapshot
        res = client.get(_events_url(run["run_id"]))

        frames = parse_frames(res.text)
        assert len(frames) == 1
        assert frames[0]["event"] == "stream.error"
        # a failed read is never progress: no id line at all
        assert "id" not in frames[0]
        envelope = ErrorEnvelope.model_validate(frames[0]["data"])
        assert envelope.code == "E_UNAVAILABLE_OVERLOADED"
        assert envelope.retryable is True
        assert envelope.request_id and envelope.trace_id

    def test_unexpected_fault_never_leaks_exception_text(self, client):
        run = _new_run(client)

        async def exploding_snapshot(identity, cursor, timeout_s):
            raise ValueError("secret provider detail /etc/passwd")

        SERVICE.repository.snapshot = exploding_snapshot
        res = client.get(_events_url(run["run_id"]))

        frames = parse_frames(res.text)
        assert len(frames) == 1
        envelope = ErrorEnvelope.model_validate(frames[0]["data"])
        assert envelope.code == "E_INTERNAL_UNKNOWN"
        assert "secret" not in res.text and "/etc/passwd" not in res.text

    def test_cursor_ahead_is_reported_structurally(self, client):
        run = _new_run(client)
        client.delete(f"/api/v1/agent/runs/{run['run_id']}")
        res = client.get(_events_url(run["run_id"]) + "?after_seq=99")

        frames = parse_frames(res.text)
        assert frames and frames[-1]["event"] == "stream.error"
        assert "id" not in frames[-1]
        envelope = ErrorEnvelope.model_validate(frames[-1]["data"])
        assert envelope.code == "E_INTERNAL_UNKNOWN"

    def test_expired_run_mid_stream_maps_to_not_found(self, client):
        """A run that vanishes (TTL) after authorization ends structurally."""
        run = _new_run(client)

        async def gone(identity, cursor, timeout_s):
            raise RunRepositoryError(RunRepositoryFault.NOT_FOUND, "expired")

        SERVICE.repository.snapshot = gone
        frames = parse_frames(client.get(_events_url(run["run_id"])).text)
        assert frames[-1]["event"] == "stream.error"
        envelope = ErrorEnvelope.model_validate(frames[-1]["data"])
        assert envelope.code == "E_NOT_FOUND_RUN"

    def test_error_frame_is_valid_json_line_protocol(self, client):
        run = _new_run(client)
        client.delete(f"/api/v1/agent/runs/{run['run_id']}")
        res = client.get(_events_url(run["run_id"]) + "?after_seq=99")
        line = [ln for ln in res.text.split("\n") if ln.startswith("data: ")][-1]
        json.loads(line[len("data: ") :])  # must be a single valid JSON object

    def test_error_frame_ids_match_the_request(self, client):
        run = _new_run(client)
        client.delete(f"/api/v1/agent/runs/{run['run_id']}")

        async def boom(identity, cursor, timeout_s):
            raise RunRepositoryError(RunRepositoryFault.UNAVAILABLE, "down")

        SERVICE.repository.snapshot = boom
        res = client.get(
            _events_url(run["run_id"]), headers={"X-Request-ID": "trace-me-42"}
        )
        envelope = ErrorEnvelope.model_validate(parse_frames(res.text)[0]["data"])
        assert envelope.request_id == "trace-me-42"
        assert envelope.trace_id

    def test_stream_failure_does_not_touch_run_state(self, client):
        """A broken read is a transport problem, not a run transition."""
        run = _new_run(client)

        async def boom(identity, cursor, timeout_s):
            raise RunRepositoryError(RunRepositoryFault.UNAVAILABLE, "down")

        SERVICE.repository.snapshot = boom
        client.get(_events_url(run["run_id"]))
        assert SERVICE.runs[run["run_id"]].state is RunState.ACCEPTED
        # the run is still cancellable through the normal write path
        assert client.delete(f"/api/v1/agent/runs/{run['run_id']}").status_code == 200
