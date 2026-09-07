"""Tests for the Agent API and central error handling (issue #36)."""

from fastapi.testclient import TestClient

from app.contracts.errors import ErrorEnvelope
from app.main import app

client = TestClient(app, raise_server_exceptions=False)


def _parse_envelope(response):
    return ErrorEnvelope.model_validate(response.json())


class TestHealth:
    def test_live(self):
        res = client.get("/api/v1/health/live")
        assert res.status_code == 200
        assert res.json() == {"status": "alive"}

    def test_ready(self):
        res = client.get("/api/v1/health/ready")
        assert res.status_code == 200
        assert res.json()["status"] == "ready"


class TestSessions:
    def test_create_session(self):
        res = client.post(
            "/api/v1/sessions",
            json={"tenant_id": "t1", "device_id": "d1", "channel": "text"},
        )
        assert res.status_code == 201
        body = res.json()
        assert body["session_id"]
        assert body["tenant_id"] == "t1"
        assert res.headers.get("x-request-id")

    def test_create_session_validation_error_is_envelope(self):
        res = client.post("/api/v1/sessions", json={"tenant_id": "", "device_id": "d1"})
        assert res.status_code == 400
        env = _parse_envelope(res)
        assert env.code == "E_VALIDATION_INVALID_INPUT"
        assert "details" not in res.json()

    def test_delete_missing_session_is_envelope(self):
        res = client.delete("/api/v1/sessions/nope")
        assert res.status_code == 404
        env = _parse_envelope(res)
        assert env.code == "E_NOT_FOUND_SESSION"


class TestRuns:
    def _new_session(self) -> str:
        res = client.post(
            "/api/v1/sessions", json={"tenant_id": "t1", "device_id": "d1"}
        )
        return res.json()["session_id"]

    def test_create_and_get_run(self):
        session_id = self._new_session()
        res = client.post(
            "/api/v1/agent/runs",
            json={
                "session_id": session_id,
                "device_id": "d1",
                "input": {"type": "text", "text": "孩子近视后需要复查吗"},
                "idempotency_key": "k1",
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["state"] == "ACCEPTED"
        got = client.get(f"/api/v1/agent/runs/{body['run_id']}")
        assert got.json()["state"] == "ACCEPTED"

    def test_run_on_missing_session_is_envelope(self):
        res = client.post(
            "/api/v1/agent/runs",
            json={
                "session_id": "ghost",
                "device_id": "d1",
                "input": {"type": "text", "text": "hi"},
                "idempotency_key": "k",
            },
        )
        assert res.status_code == 404
        assert _parse_envelope(res).code == "E_NOT_FOUND_SESSION"

    def test_idempotent_replay_returns_original_run(self):
        session_id = self._new_session()
        payload = {
            "session_id": session_id,
            "device_id": "d1",
            "input": {"type": "text", "text": "hi"},
            "idempotency_key": "same-key",
        }
        first = client.post("/api/v1/agent/runs", json=payload)
        assert first.status_code == 200
        replay = client.post("/api/v1/agent/runs", json=payload)
        assert replay.status_code == 200
        assert replay.json()["run_id"] == first.json()["run_id"]
        events = client.get(
            f"/api/v1/agent/runs/{first.json()['run_id']}/events"
        ).json()
        assert len(events["events"]) == 1

    def test_same_key_different_payload_conflicts(self):
        session_id = self._new_session()
        payload = {
            "session_id": session_id,
            "device_id": "d1",
            "input": {"type": "text", "text": "first"},
            "idempotency_key": "same-key-2",
        }
        assert client.post("/api/v1/agent/runs", json=payload).status_code == 200
        changed = dict(payload, input={"type": "text", "text": "second"})
        res = client.post("/api/v1/agent/runs", json=changed)
        assert res.status_code == 409
        assert _parse_envelope(res).code == "E_CONFLICT_IDEMPOTENCY"

    def test_run_device_must_match_session_device(self):
        session_id = self._new_session()  # bound to device d1
        res = client.post(
            "/api/v1/agent/runs",
            json={
                "session_id": session_id,
                "device_id": "OTHER-DEVICE",
                "input": {"type": "text", "text": "hi"},
                "idempotency_key": "k-dev",
            },
        )
        assert res.status_code == 403
        assert _parse_envelope(res).code == "E_AUTHZ_FORBIDDEN"

    def test_events_replay_after_seq(self):
        session_id = self._new_session()
        res = client.post(
            "/api/v1/agent/runs",
            json={
                "session_id": session_id,
                "device_id": "d1",
                "input": {"type": "text", "text": "hi"},
                "idempotency_key": "k-replay",
            },
        )
        run_id = res.json()["run_id"]
        page = client.get(f"/api/v1/agent/runs/{run_id}/events").json()
        assert page["next_seq"] == 1
        assert page["events"][0]["event"] == "run.accepted"
        # replay with after_seq=1 returns nothing new, next_seq still 1
        page2 = client.get(f"/api/v1/agent/runs/{run_id}/events?after_seq=1").json()
        assert page2["events"] == []

    def test_cancel_run(self):
        session_id = self._new_session()
        res = client.post(
            "/api/v1/agent/runs",
            json={
                "session_id": session_id,
                "device_id": "d1",
                "input": {"type": "text", "text": "hi"},
                "idempotency_key": "k-cancel",
            },
        )
        run_id = res.json()["run_id"]
        cancelled = client.delete(f"/api/v1/agent/runs/{run_id}")
        assert cancelled.status_code == 200
        assert cancelled.json()["cancelled"] is True
        assert cancelled.json()["state"] == "CANCELLED"
        # events now carry the terminal run.completed
        events = client.get(f"/api/v1/agent/runs/{run_id}/events").json()["events"]
        assert events[-1]["event"] == "run.completed"

    def test_missing_run_is_envelope(self):
        res = client.get("/api/v1/agent/runs/ghost")
        assert res.status_code == 404
        assert _parse_envelope(res).code == "E_NOT_FOUND_RUN"


class TestErrorBoundary:
    def test_uncaught_exception_maps_to_internal_envelope(self):
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
        # envelope carries only safe keys
        assert set(body.keys()) == {
            "code",
            "message",
            "request_id",
            "trace_id",
            "retryable",
            "retry_after_ms",
        }
