"""Tests for the Agent API, auth boundary and error handling (issue #36)."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.api.v1.agent_api import STORE
from app.api.v1.auth import DevicePrincipal, get_device_principal
from app.contracts.errors import ErrorEnvelope
from app.main import app

PRINCIPAL = DevicePrincipal(tenant_id="t1", device_id="d1")


@pytest.fixture
def client():
    app.dependency_overrides[get_device_principal] = lambda: PRINCIPAL
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


def _parse_envelope(response):
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
        assert client.get("/api/v1/health/live").json() == {"status": "alive"}

    def test_ready_does_not_leak_counts(self, client):
        body = client.get("/api/v1/health/ready").json()
        assert body["status"] == "ready"
        assert "sessions" not in json.dumps(body)
        assert "runs" not in json.dumps(body)


class TestAuthBoundary:
    def test_default_deny_without_principal(self):
        # no dependency override active: routes must refuse
        with TestClient(app, raise_server_exceptions=False) as anon:
            res = anon.post("/api/v1/sessions", json={"channel": "text"})
            assert res.status_code == 401
            assert _parse_envelope(res).code == "E_AUTH_MISSING_CREDENTIALS"


class TestSessions:
    def test_create_session_derives_identity_from_principal(self, client):
        body = _new_session(client)
        assert body["tenant_id"] == "t1"
        assert body["device_id"] == "d1"
        assert body["channel"] == "text"

    def test_session_validation_error_is_envelope(self, client):
        res = client.post("/api/v1/sessions", json={"channel": "not-a-channel"})
        assert res.status_code == 400
        assert _parse_envelope(res).code == "E_VALIDATION_INVALID_INPUT"

    def test_delete_session_cleans_runs_and_idempotency(self, client):
        session = _new_session(client)
        _new_run(client, session["session_id"], text="q1", key="clean-key")
        assert len(STORE.idempotency) == 1
        res = client.delete(f"/api/v1/sessions/{session['session_id']}")
        assert res.status_code == 204
        assert STORE.idempotency == {}

    def test_delete_foreign_session_forbidden(self, client):
        session = _new_session(client)
        app.dependency_overrides[get_device_principal] = lambda: DevicePrincipal(
            tenant_id="t1", device_id="OTHER"
        )
        try:
            res = client.delete(f"/api/v1/sessions/{session['session_id']}")
        finally:
            app.dependency_overrides[get_device_principal] = lambda: PRINCIPAL
        assert res.status_code == 403
        assert _parse_envelope(res).code == "E_AUTHZ_FORBIDDEN"


class TestRuns:
    def test_run_snapshot_preserves_input_and_ids(self, client):
        session = _new_session(client)
        res = client.post(
            "/api/v1/agent/runs",
            json={
                "session_id": session["session_id"],
                "input": {"type": "text", "text": "孩子近视后需要复查吗"},
                "idempotency_key": "snap",
            },
            headers={"X-Request-ID": "my-req-42"},
        )
        run_id = res.json()["run_id"]
        snap = STORE.runs[run_id].snapshot
        assert snap.text == "孩子近视后需要复查吗"
        assert snap.request_id == "my-req-42"
        assert snap.trace_id
        assert snap.channel.value == "text"

    def test_payload_hash_never_contains_plaintext(self, client):
        session = _new_session(client)
        secret = "疑似青光眼-20260907-秘密问题"
        run = _new_run(client, session["session_id"], text=secret, key="hash")
        run_id = run["run_id"]
        stored = STORE.runs[run_id]
        assert stored.payload_hash != secret
        assert secret not in stored.payload_hash
        assert len(stored.payload_hash) == 64
        canonical = json.dumps(
            {"text": secret, "locale": "zh-CN", "channel": "text"},
            sort_keys=True,
            ensure_ascii=False,
        )
        assert stored.payload_hash == hashlib.sha256(canonical.encode()).hexdigest()

    def test_run_state_machine_authority(self, client):
        session = _new_session(client)
        run = _new_run(client, session["session_id"])
        machine = STORE.runs[run["run_id"]].machine
        assert machine.current.value == run["state"] == "ACCEPTED"
        # events seq follows the machine
        assert machine.event_seq == 1

    def test_missing_session_run_is_envelope(self, client):
        res = client.post(
            "/api/v1/agent/runs",
            json={
                "session_id": "ghost",
                "input": {"type": "text", "text": "hi"},
                "idempotency_key": "k",
            },
        )
        assert res.status_code == 404
        assert _parse_envelope(res).code == "E_NOT_FOUND_SESSION"

    def test_idempotent_replay_returns_same_run(self, client):
        session = _new_session(client)
        payload = {
            "session_id": session["session_id"],
            "input": {"type": "text", "text": "hi"},
            "idempotency_key": "same-key",
        }
        first = client.post("/api/v1/agent/runs", json=payload).json()
        replay = client.post("/api/v1/agent/runs", json=payload)
        assert replay.status_code == 200
        assert replay.json()["run_id"] == first["run_id"]
        events = client.get(f"/api/v1/agent/runs/{first['run_id']}/events").json()
        assert len(events["events"]) == 1  # no duplicate accept

    def test_same_key_different_payload_conflicts(self, client):
        session = _new_session(client)
        payload = {
            "session_id": session["session_id"],
            "input": {"type": "text", "text": "first"},
            "idempotency_key": "same-key-2",
        }
        assert client.post("/api/v1/agent/runs", json=payload).status_code == 200
        changed = dict(payload, input={"type": "text", "text": "second"})
        res = client.post("/api/v1/agent/runs", json=changed)
        assert res.status_code == 409
        assert _parse_envelope(res).code == "E_CONFLICT_IDEMPOTENCY"

    def test_run_ownership_enforced(self, client):
        session = _new_session(client)
        run = _new_run(client, session["session_id"])
        app.dependency_overrides[get_device_principal] = lambda: DevicePrincipal(
            tenant_id="t1", device_id="OTHER"
        )
        try:
            assert client.get(f"/api/v1/agent/runs/{run['run_id']}").status_code == 403
            assert (
                client.get(f"/api/v1/agent/runs/{run['run_id']}/events").status_code
                == 403
            )
            assert (
                client.delete(f"/api/v1/agent/runs/{run['run_id']}").status_code == 403
            )
        finally:
            app.dependency_overrides[get_device_principal] = lambda: PRINCIPAL

    def test_events_replay_and_cancel_terminal_once(self, client):
        session = _new_session(client)
        run = _new_run(client, session["session_id"])
        page = client.get(f"/api/v1/agent/runs/{run['run_id']}/events").json()
        assert page["next_seq"] == 1
        assert page["events"][0]["event"] == "run.accepted"
        empty = client.get(
            f"/api/v1/agent/runs/{run['run_id']}/events?after_seq=1"
        ).json()
        assert empty["events"] == []

        cancelled = client.delete(f"/api/v1/agent/runs/{run['run_id']}").json()
        assert cancelled["state"] == "CANCELLED"
        assert cancelled["cancelled"] is True
        # repeat cancel: terminal state — no duplicate terminal event
        again = client.delete(f"/api/v1/agent/runs/{run['run_id']}").json()
        assert again["state"] == "CANCELLED"
        events = client.get(f"/api/v1/agent/runs/{run['run_id']}/events").json()
        names = [e["event"] for e in events["events"]]
        assert names == ["run.accepted", "run.completed"]

    def test_missing_run_is_envelope(self, client):
        res = client.get("/api/v1/agent/runs/ghost")
        assert res.status_code == 404
        assert _parse_envelope(res).code == "E_NOT_FOUND_RUN"


class TestRequestIds:
    def test_malformed_x_request_id_is_replaced(self, client):
        res = client.post(
            "/api/v1/sessions",
            json={"channel": "text"},
            headers={"X-Request-ID": "bad id <script>"},
        )
        echoed = res.headers.get("x-request-id")
        assert echoed and "<script>" not in echoed
        assert echoed != "bad id <script>"

    def test_request_and_trace_ids_echoed(self, client):
        res = client.post(
            "/api/v1/sessions",
            json={"channel": "text"},
            headers={"X-Request-ID": "good-req-1"},
        )
        assert res.headers.get("x-request-id") == "good-req-1"
        assert res.headers.get("x-trace-id")


class TestErrorBoundary:
    def test_uncaught_exception_maps_to_internal_envelope(self, client):
        @app.get("/__boom")
        async def boom():
            raise ValueError("sensitive provider detail: /etc/passwd")

        try:
            res = client.get("/__boom")
        finally:
            for route in list(app.routes):
                if getattr(route, "path", None) == "/__boom":
                    app.routes.remove(route)
        assert res.status_code == 500
        body = res.json()
        assert body["code"] == "E_INTERNAL_UNKNOWN"
        assert "sensitive" not in res.text
        assert "/etc/passwd" not in res.text
        assert set(body.keys()) == {
            "code",
            "message",
            "request_id",
            "trace_id",
            "retryable",
            "retry_after_ms",
        }
