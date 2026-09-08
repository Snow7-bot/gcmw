"""Tests for Agent API, admission service, auth boundary (issue #36)."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.v1.agent_api import SERVICE
from app.api.v1.auth import DevicePrincipal, get_device_principal
from app.api.v1.errors import AppError
from app.contracts.api import Channel, CreateRunRequest, CreateSessionRequest, RunInput
from app.contracts.errors import ErrorEnvelope
from app.contracts.run import RunState
from app.main import app

PRINCIPAL = DevicePrincipal(tenant_id="t1", device_id="d1")
OTHER_DEVICE = DevicePrincipal(tenant_id="t1", device_id="OTHER")


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value = self.value + timedelta(seconds=seconds)


@pytest.fixture
def client():
    SERVICE.sessions.clear()
    SERVICE.runs.clear()
    SERVICE.idempotency.clear()
    clock = FakeClock()
    original_clock = SERVICE._clock
    SERVICE._clock = clock
    app.dependency_overrides[get_device_principal] = lambda: PRINCIPAL
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, clock
    app.dependency_overrides.clear()
    SERVICE._clock = original_clock
    SERVICE.sessions.clear()
    SERVICE.runs.clear()
    SERVICE.idempotency.clear()


def _env(response):
    return ErrorEnvelope.model_validate(response.json())


def _new_session(client, channel: str = "text") -> dict:
    res = client.post("/api/v1/sessions", json={"channel": channel})
    assert res.status_code == 201
    return res.json()


def _new_run(client, session_id: str, text: str = "hi", key: str = "k") -> dict:
    res = client.post(
        "/api/v1/agent/runs",
        json={
            "session_id": session_id,
            "input": {"type": "text", "text": text},
            "idempotency_key": key,
        },
    )
    assert res.status_code == 200
    return res.json()


class TestHealth:
    def test_live(self, client):
        c, _ = client
        assert c.get("/api/v1/health/live").json() == {"status": "alive"}

    def test_ready_does_not_leak_counts(self, client):
        c, _ = client
        body = c.get("/api/v1/health/ready").json()
        assert body["status"] == "ready"
        assert "sessions" not in json.dumps(body)
        assert "runs" not in json.dumps(body)


class TestAuthBoundary:
    def test_default_deny_without_principal(self):
        with TestClient(app, raise_server_exceptions=False) as anon:
            res = anon.post("/api/v1/sessions", json={"channel": "text"})
            assert res.status_code == 401
            assert _env(res).code == "E_AUTH_MISSING_CREDENTIALS"


class TestSessions:
    def test_create_session_derives_identity(self, client):
        c, _ = client
        body = _new_session(c)
        assert body["tenant_id"] == "t1"
        assert body["device_id"] == "d1"

    def test_validation_error_is_envelope(self, client):
        c, _ = client
        res = c.post("/api/v1/sessions", json={"channel": "nope"})
        assert res.status_code == 400
        assert _env(res).code == "E_VALIDATION_INVALID_INPUT"

    def test_delete_cleans_runs_and_idempotency(self, client):
        c, _ = client
        session = _new_session(c)
        _new_run(c, session["session_id"])
        assert len(SERVICE.idempotency) == 1
        assert c.delete(f"/api/v1/sessions/{session['session_id']}").status_code == 204
        assert SERVICE.sessions == {}
        assert SERVICE.runs == {}
        assert SERVICE.idempotency == {}

    def test_delete_foreign_session_forbidden(self, client):
        c, _ = client
        session = _new_session(c)
        app.dependency_overrides[get_device_principal] = lambda: OTHER_DEVICE
        try:
            res = c.delete(f"/api/v1/sessions/{session['session_id']}")
        finally:
            app.dependency_overrides[get_device_principal] = lambda: PRINCIPAL
        assert res.status_code == 403
        assert _env(res).code == "E_AUTHZ_FORBIDDEN"


class TestExpiry:
    def test_expired_session_rejects_new_runs_and_purges_snapshots(self, client):
        c, clock = client
        session = _new_session(c)
        run = _new_run(
            c, session["session_id"], text="敏感医疗问题-必须随过期消失", key="ttl"
        )
        assert SERVICE.runs[run["run_id"]].snapshot.text.startswith("敏感")
        clock.advance(SERVICE.sessions[session["session_id"]].ttl_s + 1)

        # run reads now fail, run creation on the expired session fails
        assert c.get(f"/api/v1/agent/runs/{run['run_id']}").status_code == 404
        res = c.post(
            "/api/v1/agent/runs",
            json={
                "session_id": session["session_id"],
                "input": {"type": "text", "text": "new"},
                "idempotency_key": "new",
            },
        )
        assert res.status_code == 404
        assert _env(res).code == "E_NOT_FOUND_SESSION"
        # session, runs and raw snapshots are gone
        assert SERVICE.sessions == {}
        assert SERVICE.runs == {}
        assert SERVICE.idempotency == {}

    def test_expired_session_delete_returns_not_found(self, client):
        c, clock = client
        session = _new_session(c)
        clock.advance(3600)
        assert c.delete(f"/api/v1/sessions/{session['session_id']}").status_code == 404


class TestRuns:
    def test_snapshot_preserves_input_and_ids(self, client):
        c, _ = client
        session = _new_session(c)
        res = c.post(
            "/api/v1/agent/runs",
            json={
                "session_id": session["session_id"],
                "input": {"type": "text", "text": "孩子近视后需要复查吗"},
                "idempotency_key": "snap",
            },
            headers={"X-Request-ID": "my-req-42"},
        )
        snap = SERVICE.runs[res.json()["run_id"]].snapshot
        assert snap.text == "孩子近视后需要复查吗"
        assert snap.request_id == "my-req-42"
        assert snap.trace_id

    def test_payload_hash_never_contains_plaintext(self, client):
        c, _ = client
        session = _new_session(c)
        secret = "疑似青光眼-20260907-秘密问题"
        run = _new_run(c, session["session_id"], text=secret, key="hash")
        stored = SERVICE.runs[run["run_id"]]
        assert secret not in stored.payload_hash
        assert len(stored.payload_hash) == 64
        canonical = json.dumps(
            {"text": secret, "locale": "zh-CN", "channel": "text"},
            sort_keys=True,
            ensure_ascii=False,
        )
        assert stored.payload_hash == hashlib.sha256(canonical.encode()).hexdigest()

    def test_state_machine_authority_and_seq_consistency(self, client):
        c, _ = client
        session = _new_session(c)
        run = _new_run(c, session["session_id"])
        machine = SERVICE.runs[run["run_id"]].machine
        assert machine.current.value == run["state"] == "ACCEPTED"
        events = c.get(f"/api/v1/agent/runs/{run['run_id']}/events").json()
        assert events["next_seq"] == machine.event_seq == 1
        assert [e["seq"] for e in events["events"]] == [1]

    def test_missing_session_run(self, client):
        c, _ = client
        res = c.post(
            "/api/v1/agent/runs",
            json={
                "session_id": "ghost",
                "input": {"type": "text", "text": "hi"},
                "idempotency_key": "k",
            },
        )
        assert res.status_code == 404
        assert _env(res).code == "E_NOT_FOUND_SESSION"

    def test_idempotent_replay_same_run_no_duplicate_events(self, client):
        c, _ = client
        session = _new_session(c)
        payload = {
            "session_id": session["session_id"],
            "input": {"type": "text", "text": "hi"},
            "idempotency_key": "same-key",
        }
        first = c.post("/api/v1/agent/runs", json=payload).json()
        replay = c.post("/api/v1/agent/runs", json=payload)
        assert replay.status_code == 200
        assert replay.json()["run_id"] == first["run_id"]
        events = c.get(f"/api/v1/agent/runs/{first['run_id']}/events").json()
        assert len(events["events"]) == 1

    def test_same_key_different_payload_conflicts(self, client):
        c, _ = client
        session = _new_session(c)
        payload = {
            "session_id": session["session_id"],
            "input": {"type": "text", "text": "first"},
            "idempotency_key": "same-key-2",
        }
        assert c.post("/api/v1/agent/runs", json=payload).status_code == 200
        changed = dict(payload, input={"type": "text", "text": "second"})
        res = c.post("/api/v1/agent/runs", json=changed)
        assert res.status_code == 409
        assert _env(res).code == "E_CONFLICT_IDEMPOTENCY"

    def test_ownership_enforced(self, client):
        c, _ = client
        session = _new_session(c)
        run = _new_run(c, session["session_id"])
        app.dependency_overrides[get_device_principal] = lambda: OTHER_DEVICE
        try:
            assert c.get(f"/api/v1/agent/runs/{run['run_id']}").status_code == 403
            assert (
                c.get(f"/api/v1/agent/runs/{run['run_id']}/events").status_code == 403
            )
            assert c.delete(f"/api/v1/agent/runs/{run['run_id']}").status_code == 403
        finally:
            app.dependency_overrides[get_device_principal] = lambda: PRINCIPAL

    def test_cancel_terminal_once_and_events_no_gap(self, client):
        c, _ = client
        session = _new_session(c)
        run = _new_run(c, session["session_id"])
        cancelled = c.delete(f"/api/v1/agent/runs/{run['run_id']}").json()
        assert cancelled["state"] == "CANCELLED"
        again = c.delete(f"/api/v1/agent/runs/{run['run_id']}").json()
        assert again["state"] == "CANCELLED"
        events = c.get(f"/api/v1/agent/runs/{run['run_id']}/events").json()
        names = [e["event"] for e in events["events"]]
        seqs = [e["seq"] for e in events["events"]]
        assert names == ["run.accepted", "run.completed"]
        assert seqs == [1, 2]  # no gaps; terminal event appended exactly once

    def test_missing_run(self, client):
        c, _ = client
        res = c.get("/api/v1/agent/runs/ghost")
        assert res.status_code == 404
        assert _env(res).code == "E_NOT_FOUND_RUN"


class TestConcurrency:
    """Concurrency semantics are asserted on RunAdmissionService directly —
    TestClient/httpx portals are not thread-safe for parallel requests."""

    def _seed_session(self) -> str:
        req = CreateSessionRequest(channel=Channel.TEXT, locale="zh-CN")
        return SERVICE.create_session(PRINCIPAL, req).session_id

    def test_concurrent_same_key_same_payload_single_run(self):
        session_id = self._seed_session()
        req = CreateRunRequest(
            session_id=session_id,
            input=RunInput(type="text", text="同题并发"),
            idempotency_key="conc-same",
        )

        def fire():
            return SERVICE.create_run(PRINCIPAL, req, "req", "trace")

        with ThreadPoolExecutor(max_workers=8) as pool:
            runs = list(pool.map(lambda _: fire(), range(16)))
        ids = {r.run_id for r in runs}
        assert len(ids) == 1
        stored = [r for r in SERVICE.runs.values() if r.session_id == session_id]
        assert len(stored) == 1

    def test_concurrent_different_keys_single_active_run(self):
        session_id = self._seed_session()

        def fire(i):
            try:
                SERVICE.create_run(
                    PRINCIPAL,
                    CreateRunRequest(
                        session_id=session_id,
                        input=RunInput(type="text", text=f"q-{i}"),
                        idempotency_key=f"key-{i}",
                    ),
                    f"req-{i}",
                    "trace",
                )
                return "created"
            except AppError as exc:
                return exc.code.value

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(fire, range(12)))
        assert outcomes.count("created") == 1
        assert all(o == "E_CONFLICT_ACTIVE_RUN" for o in outcomes if o != "created")
        active = [r for r in SERVICE.runs.values() if r.session_id == session_id]
        assert len(active) == 1
        assert active[0].machine.current is RunState.ACCEPTED

    def test_concurrent_cancel_single_terminal_event(self):
        session_id = self._seed_session()
        run = SERVICE.create_run(
            PRINCIPAL,
            CreateRunRequest(
                session_id=session_id,
                input=RunInput(type="text", text="cancel me"),
                idempotency_key="cc",
            ),
            "req-c",
            "trace",
        )
        run_id = run.run_id

        def fire():
            return SERVICE.cancel_run(PRINCIPAL, run_id)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: fire(), range(12)))
        events = SERVICE.runs[run_id].sse_events
        assert [e.event.value for e in events] == ["run.accepted", "run.completed"]
        assert SERVICE.runs[run_id].machine.current is RunState.CANCELLED

    def test_create_vs_delete_race_keeps_invariants(self):
        session_id = self._seed_session()

        def creator():
            try:
                SERVICE.create_run(
                    PRINCIPAL,
                    CreateRunRequest(
                        session_id=session_id,
                        input=RunInput(type="text", text="race"),
                        idempotency_key="race-key",
                    ),
                    "req-r",
                    "trace",
                )
                return "created"
            except AppError as exc:
                return exc.code.value

        def destroyer():
            try:
                SERVICE.delete_session(PRINCIPAL, session_id)
                return "deleted"
            except AppError as exc:
                return exc.code.value

        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(lambda f: f(), [creator] * 8 + [destroyer] * 8))
        allowed = {"created", "deleted", "E_CONFLICT_ACTIVE_RUN", "E_NOT_FOUND_SESSION"}
        assert set(outcomes) <= allowed
        for run_id, run in SERVICE.runs.items():
            assert run.snapshot.session_id in SERVICE.sessions, f"orphan run {run_id}"


class TestRequestIds:
    def test_malformed_x_request_id_replaced(self, client):
        c, _ = client
        res = c.post(
            "/api/v1/sessions",
            json={"channel": "text"},
            headers={"X-Request-ID": "bad id <x>"},
        )
        echoed = res.headers.get("x-request-id")
        assert echoed and "<x>" not in echoed

    def test_ids_echoed(self, client):
        c, _ = client
        res = c.post(
            "/api/v1/sessions",
            json={"channel": "text"},
            headers={"X-Request-ID": "good-req-1"},
        )
        assert res.headers.get("x-request-id") == "good-req-1"
        assert res.headers.get("x-trace-id")


class TestErrorBoundary:
    def test_uncaught_exception_maps_to_internal_envelope(self, client):
        c, _ = client

        @app.get("/__boom")
        async def boom():
            raise ValueError("sensitive provider detail: /etc/passwd")

        try:
            res = c.get("/__boom")
        finally:
            for route in list(app.routes):
                if getattr(route, "path", None) == "/__boom":
                    app.routes.remove(route)
        assert res.status_code == 500
        body = res.json()
        assert body["code"] == "E_INTERNAL_UNKNOWN"
        assert "sensitive" not in res.text
        assert set(body.keys()) == {
            "code",
            "message",
            "request_id",
            "trace_id",
            "retryable",
            "retry_after_ms",
        }
