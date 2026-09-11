"""Tests for Agent API, admission service, auth boundary (issue #36).

Round B2-B: the admission service keeps a LIFECYCLE MIRROR only — run state and
events belong to ``RunRepository``. Tests therefore assert on the public API and
on the repository's own atomic snapshot, never on an in-memory event list.
"""

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sse_frames import event_ids, event_names, protocol_frames

from app.api.v1.agent_api import SERVICE, RunAdmissionService
from app.api.v1.auth import DevicePrincipal, get_device_principal
from app.api.v1.errors import AppError
from app.contracts.api import Channel, CreateRunRequest, CreateSessionRequest, RunInput
from app.contracts.errors import ErrorCode, ErrorEnvelope
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


def _identity(run: dict) -> RunIdentity:
    return RunIdentity(
        run_id=run["run_id"],
        tenant_id=PRINCIPAL.tenant_id,
        device_id=PRINCIPAL.device_id,
        session_id=run["session_id"],
    )


async def _durable_events(repository, identity: RunIdentity) -> list[int]:
    page = await repository.snapshot(identity, 0, 0.0)
    return [event.seq for event in page.events]


def _stream_seqs(client, run_id: str, **kwargs) -> list[int]:
    """Read a TERMINAL run stream to completion (a non-terminal one is open)."""
    res = client.get(f"/api/v1/agent/runs/{run_id}/events", **kwargs)
    assert res.status_code == 200
    return event_ids(protocol_frames(res.text))


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value = self.value + timedelta(seconds=seconds)


@pytest.fixture
def client():
    # a fresh repository per test: asyncio primitives bind to the loop that
    # first contends on them, and every TestClient brings its own portal loop
    SERVICE.repository = MemoryRunRepository()
    SERVICE.sessions.clear()
    SERVICE.runs.clear()
    SERVICE.idempotency.clear()
    SERVICE._locks.clear()
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

    def test_repository_is_the_only_state_and_seq_authority(self, client):
        c, _ = client
        session = _new_session(c)
        run = _new_run(c, session["session_id"])
        record = SERVICE.runs[run["run_id"]]
        assert record.state is RunState.ACCEPTED
        assert run["state"] == "ACCEPTED"
        # the admission record mirrors state only: no machine, no event list
        assert not hasattr(record, "machine")
        assert not hasattr(record, "sse_events")
        cancelled = c.delete(f"/api/v1/agent/runs/{run['run_id']}")
        assert cancelled.json()["state"] == "CANCELLED"
        # the public stream is served straight from the repository snapshot
        assert _stream_seqs(c, run["run_id"]) == [1, 2]

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
        # terminal first, so the stream is finite; exactly one accepted event
        assert c.delete(f"/api/v1/agent/runs/{first['run_id']}").status_code == 200
        assert _stream_seqs(c, first["run_id"]) == [1, 2]

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
        res = c.get(f"/api/v1/agent/runs/{run['run_id']}/events")
        frames = protocol_frames(res.text)
        assert event_names(frames) == ["run.accepted", "run.completed"]
        assert event_ids(frames) == [1, 2]  # no gaps; terminal event exactly once

    def test_missing_run(self, client):
        c, _ = client
        res = c.get("/api/v1/agent/runs/ghost")
        assert res.status_code == 404
        assert _env(res).code == "E_NOT_FOUND_RUN"


class StubRepository:
    """Duck-typed repository that fails exactly where a test wants it to."""

    def __init__(self, fault: RunRepositoryFault) -> None:
        self.fault = fault
        self.state_value = RunState.ACCEPTED

    async def create(self, identity) -> int:
        return 1

    async def commit_transition(self, identity, **kwargs) -> int:
        raise RunRepositoryError(self.fault, "stub failure")

    async def state(self, identity) -> RunState:
        return self.state_value

    async def delete(self, identity) -> None:
        return None

    async def snapshot(self, identity, cursor, timeout_s):
        raise AssertionError("streaming is not part of this stub")


class TestConcurrency:
    """Concurrency is asserted on the service directly.

    Each test builds its OWN service + repository inside one ``asyncio.run``:
    the process singleton must stay usable from the portal loops of the
    ``TestClient`` tests, and asyncio primitives bind to a single loop.
    """

    @staticmethod
    def _service() -> RunAdmissionService:
        return RunAdmissionService(repository=MemoryRunRepository())

    @staticmethod
    def _session(service: RunAdmissionService) -> str:
        req = CreateSessionRequest(channel=Channel.TEXT, locale="zh-CN")
        return service.create_session(PRINCIPAL, req).session_id

    def test_concurrent_same_key_same_payload_single_run(self):
        async def main():
            service = self._service()
            session_id = self._session(service)
            req = CreateRunRequest(
                session_id=session_id,
                input=RunInput(type="text", text="同题并发"),
                idempotency_key="conc-same",
            )
            runs = await asyncio.gather(
                *(service.create_run(PRINCIPAL, req, "req", "trace") for _ in range(16))
            )
            stored = [r for r in service.runs.values() if r.session_id == session_id]
            return {r.run_id for r in runs}, len(stored)

        ids, stored = asyncio.run(main())
        assert len(ids) == 1
        assert stored == 1

    def test_concurrent_different_keys_single_active_run(self):
        async def main():
            service = self._service()
            session_id = self._session(service)

            async def fire(i):
                try:
                    await service.create_run(
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

            outcomes = await asyncio.gather(*(fire(i) for i in range(12)))
            active = [r for r in service.runs.values() if r.session_id == session_id]
            return outcomes, active

        outcomes, active = asyncio.run(main())
        assert outcomes.count("created") == 1
        assert all(o == "E_CONFLICT_ACTIVE_RUN" for o in outcomes if o != "created")
        assert len(active) == 1
        assert active[0].state is RunState.ACCEPTED

    def test_concurrent_cancel_single_terminal_event(self):
        async def main():
            service = self._service()
            session_id = self._session(service)
            run = await service.create_run(
                PRINCIPAL,
                CreateRunRequest(
                    session_id=session_id,
                    input=RunInput(type="text", text="cancel me"),
                    idempotency_key="cc",
                ),
                "req-c",
                "trace",
            )
            await asyncio.gather(
                *(service.cancel_run(PRINCIPAL, run.run_id) for _ in range(12))
            )
            identity = RunIdentity(
                run_id=run.run_id,
                tenant_id=PRINCIPAL.tenant_id,
                device_id=PRINCIPAL.device_id,
                session_id=session_id,
            )
            return (
                await _durable_events(service.repository, identity),
                await service.repository.state(identity),
                service.runs[run.run_id].state,
            )

        seqs, durable_state, mirror_state = asyncio.run(main())
        assert seqs == [1, 2]  # exactly one terminal event, no gap
        assert durable_state is RunState.CANCELLED
        assert mirror_state is RunState.CANCELLED

    def test_create_vs_delete_race_keeps_invariants(self):
        async def main():
            service = self._service()
            session_id = self._session(service)

            async def creator():
                try:
                    await service.create_run(
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

            async def destroyer():
                try:
                    await service.delete_session(PRINCIPAL, session_id)
                    return "deleted"
                except AppError as exc:
                    return exc.code.value

            outcomes = await asyncio.gather(
                *(creator() for _ in range(8)), *(destroyer() for _ in range(8))
            )
            orphans = [
                run_id
                for run_id, run in service.runs.items()
                if run.session_id not in service.sessions
            ]
            return outcomes, orphans

        outcomes, orphans = asyncio.run(main())
        allowed = {"created", "deleted", "E_CONFLICT_ACTIVE_RUN", "E_NOT_FOUND_SESSION"}
        assert set(outcomes) <= allowed
        assert orphans == []


class TestRepositoryErrorMapping:
    """Repository failures become mapped ErrorCodes and never move the mirror."""

    async def _cancel_with(self, fault: RunRepositoryFault):
        service = RunAdmissionService(repository=StubRepository(fault))
        session_id = service.create_session(
            PRINCIPAL, CreateSessionRequest(channel=Channel.TEXT, locale="zh-CN")
        ).session_id
        run = await service.create_run(
            PRINCIPAL,
            CreateRunRequest(
                session_id=session_id,
                input=RunInput(type="text", text="mapped"),
                idempotency_key="m",
            ),
            "req",
            "trace",
        )
        with pytest.raises(AppError) as excinfo:
            await service.cancel_run(PRINCIPAL, run.run_id)
        return excinfo.value.code, service.runs[run.run_id].state

    def test_invariant_failure_maps_and_leaves_state_untouched(self):
        code, mirror = asyncio.run(self._cancel_with(RunRepositoryFault.INVARIANT))
        assert code is ErrorCode.INTERNAL_UNKNOWN
        assert mirror is RunState.ACCEPTED

    def test_unavailable_failure_maps_to_overloaded(self):
        code, mirror = asyncio.run(self._cancel_with(RunRepositoryFault.UNAVAILABLE))
        assert code is ErrorCode.UNAVAILABLE_OVERLOADED
        assert mirror is RunState.ACCEPTED

    def test_persistent_cas_conflict_is_bounded_not_infinite(self):
        async def main():
            service = RunAdmissionService(
                repository=StubRepository(RunRepositoryFault.CAS_CONFLICT)
            )
            session_id = service.create_session(
                PRINCIPAL, CreateSessionRequest(channel=Channel.TEXT, locale="zh-CN")
            ).session_id
            run = await service.create_run(
                PRINCIPAL,
                CreateRunRequest(
                    session_id=session_id,
                    input=RunInput(type="text", text="cas"),
                    idempotency_key="cas",
                ),
                "req",
                "trace",
            )
            with pytest.raises(AppError) as excinfo:
                await service.cancel_run(PRINCIPAL, run.run_id)
            return excinfo.value.code

        assert asyncio.run(main()) is ErrorCode.CONFLICT_ACTIVE_RUN

    def test_status_state_cancelled_always_consistent(self, client):
        c, _ = client
        session = _new_session(c)
        run = _new_run(c, session["session_id"], key="sc-1")
        before = c.get(f"/api/v1/agent/runs/{run['run_id']}").json()
        assert before["state"] == "ACCEPTED" and before["cancelled"] is False
        cancelled = c.delete(f"/api/v1/agent/runs/{run['run_id']}").json()
        assert cancelled["state"] == "CANCELLED" and cancelled["cancelled"] is True
        after = c.get(f"/api/v1/agent/runs/{run['run_id']}").json()
        assert after["state"] == "CANCELLED" and after["cancelled"] is True

    def test_concurrent_cancel_keeps_event_sequence_gapless(self):
        async def main():
            service = RunAdmissionService(repository=MemoryRunRepository())
            session_id = service.create_session(
                PRINCIPAL, CreateSessionRequest(channel=Channel.TEXT, locale="zh-CN")
            ).session_id
            run = await service.create_run(
                PRINCIPAL,
                CreateRunRequest(
                    session_id=session_id,
                    input=RunInput(type="text", text="cc"),
                    idempotency_key="cc-imm",
                ),
                "req",
                "trace",
            )
            identity = RunIdentity(
                run_id=run.run_id,
                tenant_id=PRINCIPAL.tenant_id,
                device_id=PRINCIPAL.device_id,
                session_id=session_id,
            )
            pages: list[list[int]] = []

            async def read_page():
                page = await service.repository.snapshot(identity, 0, 0.0)
                seqs = [event.seq for event in page.events]
                assert seqs == list(range(1, len(seqs) + 1))  # no gaps, no tails
                pages.append(seqs)

            await asyncio.gather(
                *(service.cancel_run(PRINCIPAL, run.run_id) for _ in range(8)),
                *(read_page() for _ in range(8)),
            )
            return await _durable_events(service.repository, identity), pages

        final, pages = asyncio.run(main())
        assert final == [1, 2]
        assert pages  # the concurrent readers really ran


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
