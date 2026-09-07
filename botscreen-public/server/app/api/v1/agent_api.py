"""Agent API router (issue #36, skeleton).

Implements the API surface with an in-memory store:
- sessions: create/delete (real TTL persistence moves to Redis in #36b);
- runs: create (ACCEPTED state + run.accepted event), status, events replay
  (``after_seq`` — the Last-Event-ID semantics consumers use), cancel;
- orchestration (Manager/MedicalQA/Verifier) consumes accepted runs later
  (#52/#55); everything here is deterministic and testable with the store.

Errors are raised as RegistryError/ModelGatewayError/AppError and mapped to
ErrorEnvelope by the app-level handlers in ``app/main.py``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, status

from app.contracts.api import (
    Channel,
    CreateRunRequest,
    CreateSessionRequest,
    RunStatusResponse,
    SessionResponse,
)
from app.contracts.errors import ErrorCode
from app.contracts.events import SSEEvent, SSEEventType
from app.contracts.run import TERMINAL_STATES, RunState

router = APIRouter(prefix="/api/v1")


class AppError(RuntimeError):
    """Application-level failure with a stable ErrorCode (#35)."""

    def __init__(self, code: ErrorCode, message: str = "") -> None:
        self.code = code
        super().__init__(message or code.value)


# ---------------------------------------------------------------------------
# In-memory store (Redis-backed replacement lands in #36b).
# ---------------------------------------------------------------------------


@dataclass
class SessionRecord:
    session_id: str
    tenant_id: str
    device_id: str
    channel: Channel
    created_at: datetime
    ttl_s: int = 1800


@dataclass
class RunRecord:
    run_id: str
    session_id: str
    tenant_id: str
    device_id: str
    state: RunState
    created_at: datetime
    events: list[SSEEvent] = field(default_factory=list)
    idempotency_key: str | None = None
    request_fingerprint: str | None = None


class MemoryStore:
    def __init__(self) -> None:
        self.sessions: dict[str, SessionRecord] = {}
        self.runs: dict[str, RunRecord] = {}
        self.idempotency: dict[tuple[str, str], str] = {}  # (session,key) -> run_id

    def create_session(self, req: CreateSessionRequest) -> SessionResponse:
        record = SessionRecord(
            session_id=uuid.uuid4().hex,
            tenant_id=req.tenant_id,
            device_id=req.device_id,
            channel=req.channel,
            created_at=datetime.now(timezone.utc),
        )
        self.sessions[record.session_id] = record
        return SessionResponse(
            session_id=record.session_id,
            tenant_id=record.tenant_id,
            device_id=record.device_id,
            channel=record.channel,
            created_at=record.created_at,
            ttl_s=record.ttl_s,
        )

    def get_session(self, session_id: str) -> SessionRecord | None:
        return self.sessions.get(session_id)

    def delete_session(self, session_id: str) -> bool:
        removed = self.sessions.pop(session_id, None) is not None
        for run_id in [
            r for r in list(self.runs) if self.runs[r].session_id == session_id
        ]:
            del self.runs[run_id]
        return removed

    def fingerprint(self, req: CreateRunRequest) -> str:
        payload = {
            "session_id": req.session_id,
            "device_id": req.device_id,
            "channel": req.channel.value
            if hasattr(req.channel, "value")
            else str(req.channel),
            "text": req.input.text,
            "locale": req.locale,
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)

    def create_run(self, session: SessionRecord, req: CreateRunRequest) -> RunRecord:
        key = (session.session_id, req.idempotency_key)
        fingerprint = self.fingerprint(req)
        existing_run_id = self.idempotency.get(key)
        if existing_run_id is not None and existing_run_id in self.runs:
            existing = self.runs[existing_run_id]
            # idempotent replay: same key + same payload returns the original run
            if existing.request_fingerprint == fingerprint:
                return existing
            raise AppError(ErrorCode.CONFLICT_IDEMPOTENCY)
        run = RunRecord(
            run_id=uuid.uuid4().hex,
            session_id=session.session_id,
            tenant_id=session.tenant_id,
            device_id=session.device_id,
            state=RunState.ACCEPTED,
            created_at=datetime.now(timezone.utc),
            idempotency_key=req.idempotency_key,
            request_fingerprint=fingerprint,
        )
        run.events.append(
            SSEEvent(
                seq=1,
                tenant_id=session.tenant_id,
                device_id=session.device_id,
                session_id=session.session_id,
                run_id=run.run_id,
                layer="process",
                event=SSEEventType.RUN_ACCEPTED,
                data={"status": "accepted", "message": "问题已接收"},
            )
        )
        self.runs[run.run_id] = run
        self.idempotency[key] = run.run_id
        return run

    def get_run(self, run_id: str) -> RunRecord | None:
        return self.runs.get(run_id)

    def cancel_run(self, run_id: str) -> RunRecord | None:
        run = self.runs.get(run_id)
        if run is None:
            return None
        if run.state not in TERMINAL_STATES:
            run.state = RunState.CANCELLED
            run.events.append(
                SSEEvent(
                    seq=len(run.events) + 1,
                    tenant_id=run.tenant_id,
                    device_id=run.device_id,
                    session_id=run.session_id,
                    run_id=run.run_id,
                    layer="process",
                    event=SSEEventType.RUN_COMPLETED,
                    data={"status": "cancelled"},
                )
            )
        return run


STORE = MemoryStore()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _require_session(session_id: str) -> SessionRecord:
    session = STORE.get_session(session_id)
    if session is None:
        raise AppError(ErrorCode.NOT_FOUND_SESSION)
    return session


def _require_run(run_id: str) -> RunRecord:
    run = STORE.get_run(run_id)
    if run is None:
        raise AppError(ErrorCode.NOT_FOUND_RUN)
    return run


@router.post(
    "/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED
)
async def create_session(req: CreateSessionRequest) -> SessionResponse:
    return STORE.create_session(req)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: str) -> None:
    if not STORE.delete_session(session_id):
        raise AppError(ErrorCode.NOT_FOUND_SESSION)


@router.post(
    "/agent/runs", response_model=RunStatusResponse, status_code=status.HTTP_200_OK
)
async def create_run(req: CreateRunRequest) -> RunStatusResponse:
    session = _require_session(req.session_id)
    if session.device_id != req.device_id:
        # session/device binding: a session belongs to one device (isolation)
        raise AppError(ErrorCode.AUTHZ_FORBIDDEN)
    run = STORE.create_run(session, req)
    return RunStatusResponse(
        run_id=run.run_id,
        session_id=run.session_id,
        state=run.state.value,
        created_at=run.created_at,
    )


@router.get("/agent/runs/{run_id}", response_model=RunStatusResponse)
async def get_run(run_id: str) -> RunStatusResponse:
    run = _require_run(run_id)
    return RunStatusResponse(
        run_id=run.run_id,
        session_id=run.session_id,
        state=run.state.value,
        created_at=run.created_at,
        cancelled=run.state is RunState.CANCELLED,
    )


@router.delete("/agent/runs/{run_id}", response_model=RunStatusResponse)
async def cancel_run(run_id: str) -> RunStatusResponse:
    run = _require_run(run_id)
    run = STORE.cancel_run(run_id)
    assert run is not None
    return RunStatusResponse(
        run_id=run.run_id,
        session_id=run.session_id,
        state=run.state.value,
        created_at=run.created_at,
        cancelled=run.state is RunState.CANCELLED,
    )


@router.get("/agent/runs/{run_id}/events")
async def get_run_events(run_id: str, after_seq: int = 0) -> dict:
    run = _require_run(run_id)
    events = [e for e in run.events if e.seq > after_seq]
    return {
        "run_id": run_id,
        "next_seq": max((e.seq for e in run.events), default=0),
        "events": [e.model_dump(mode="json") for e in events],
    }


@router.get("/health/live")
async def live() -> dict:
    return {"status": "alive"}


@router.get("/health/ready")
async def ready() -> dict:
    return {
        "status": "ready",
        "checks": {"store": {"sessions": len(STORE.sessions), "runs": len(STORE.runs)}},
    }
