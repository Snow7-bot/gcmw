"""Agent API router (issue #36, v1 skeleton — revision round).

Architecture rules enforced here:
- ONE state authority per run: ``RunStateMachine`` (#10/#21). Routes never
  assign ``run.state`` directly; SSE events are appended ONLY through
  ``emit_sse`` right after a machine transition, and the event sequence is
  the machine's own ``event_seq`` — terminal events therefore occur at most
  once per run;
- idempotency is keyed on (session_id, idempotency_key, payload_hash); the
  payload hash is a SHA-256 of the canonicalised payload — the raw question
  text is never stored in fingerprints and only lives in the run snapshot
  (removed with the session);
- tenant/device ALWAYS derive from the authenticated DevicePrincipal
  (default deny); run ownership is enforced on every read/cancel/events call.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.v1.auth import DevicePrincipal, get_device_principal
from app.api.v1.errors import AppError
from app.contracts.api import (
    CreateRunRequest,
    CreateSessionRequest,
    RunStatusResponse,
    SessionResponse,
)
from app.contracts.common import Channel
from app.contracts.errors import ErrorCode
from app.contracts.events import SSEEvent, SSEEventType
from app.contracts.run import RunState
from app.orchestration.state_machine import RunStateMachine

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class SessionRecord:
    session_id: str
    tenant_id: str
    device_id: str
    channel: Channel
    locale: str
    created_at: datetime
    ttl_s: int = 1800


@dataclass
class RunSnapshot:
    """Minimal snapshot the future Manager/QA agents need to execute a run.

    Short-lived: owned by the run record and deleted with its session. The
    request_id/trace_id are persisted here for audit correlation only.
    """

    tenant_id: str
    device_id: str
    session_id: str
    channel: Channel
    text: str
    locale: str
    request_id: str
    trace_id: str


@dataclass
class RunRecord:
    run_id: str
    session_id: str
    machine: RunStateMachine
    snapshot: RunSnapshot
    payload_hash: str
    sse_events: list[SSEEvent] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# In-memory store (Redis-backed replacement lands in #36b).
# ---------------------------------------------------------------------------


class MemoryStore:
    def __init__(self) -> None:
        self.sessions: dict[str, SessionRecord] = {}
        self.runs: dict[str, RunRecord] = {}
        # (session_id, idempotency_key) -> (run_id, payload_hash)
        self.idempotency: dict[tuple[str, str], tuple[str, str]] = {}

    # -- sessions -----------------------------------------------------------

    def create_session(
        self, principal: DevicePrincipal, req: CreateSessionRequest
    ) -> SessionResponse:
        record = SessionRecord(
            session_id=uuid.uuid4().hex,
            tenant_id=principal.tenant_id,
            device_id=principal.device_id,
            channel=req.channel,
            locale=req.locale,
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

    def delete_session(self, principal: DevicePrincipal, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            raise AppError(ErrorCode.NOT_FOUND_SESSION)
        if not self._owns(session, principal):
            raise AppError(ErrorCode.AUTHZ_FORBIDDEN)
        del self.sessions[session_id]
        for run_id in [
            r for r in list(self.runs) if self.runs[r].session_id == session_id
        ]:
            del self.runs[run_id]
        for key in [k for k in list(self.idempotency) if k[0] == session_id]:
            del self.idempotency[key]

    def _owns(self, session: SessionRecord, principal: DevicePrincipal) -> bool:
        return (
            session.tenant_id == principal.tenant_id
            and session.device_id == principal.device_id
        )

    def require_owned_session(
        self, principal: DevicePrincipal, session_id: str
    ) -> SessionRecord:
        session = self.sessions.get(session_id)
        if session is None:
            raise AppError(ErrorCode.NOT_FOUND_SESSION)
        if not self._owns(session, principal):
            raise AppError(ErrorCode.AUTHZ_FORBIDDEN)
        return session

    # -- runs ---------------------------------------------------------------

    @staticmethod
    def payload_hash(session: SessionRecord, req: CreateRunRequest) -> str:
        canonical = json.dumps(
            {
                "text": req.input.text,
                "locale": session.locale,
                "channel": session.channel.value,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def emit_sse(self, run: RunRecord, event_type: SSEEventType, data: dict) -> None:
        """THE only place SSE events are appended.

        Called right after the corresponding RunStateMachine transition; the
        SSE sequence equals the machine's event_seq (single sequence source).
        """
        event = SSEEvent(
            seq=run.machine.event_seq,
            tenant_id=run.snapshot.tenant_id,
            device_id=run.snapshot.device_id,
            session_id=run.snapshot.session_id,
            run_id=run.run_id,
            layer="process",
            event=event_type,
            data=data,
        )
        run.sse_events.append(event)

    def create_run(
        self,
        principal: DevicePrincipal,
        req: CreateRunRequest,
        request_id: str,
        trace_id: str,
    ) -> RunRecord:
        session = self.require_owned_session(principal, req.session_id)
        payload_hash = self.payload_hash(session, req)
        key = (session.session_id, req.idempotency_key)
        existing = self.idempotency.get(key)
        if existing is not None:
            run_id, stored_hash = existing
            if run_id in self.runs:
                if stored_hash == payload_hash:
                    # idempotent replay: original run returned untouched
                    return self.runs[run_id]
                raise AppError(ErrorCode.CONFLICT_IDEMPOTENCY)

        run_id = uuid.uuid4().hex
        machine = RunStateMachine(run_id)  # initial ACCEPTED, event_seq == 1
        run = RunRecord(
            run_id=run_id,
            session_id=session.session_id,
            machine=machine,
            snapshot=RunSnapshot(
                tenant_id=session.tenant_id,
                device_id=session.device_id,
                session_id=session.session_id,
                channel=session.channel,
                text=req.input.text,
                locale=session.locale,
                request_id=request_id,
                trace_id=trace_id,
            ),
            payload_hash=payload_hash,
        )
        self.emit_sse(
            run,
            SSEEventType.RUN_ACCEPTED,
            {"status": "accepted", "message": "问题已接收"},
        )
        self.runs[run.run_id] = run
        self.idempotency[key] = (run.run_id, payload_hash)
        return run

    def require_owned_run(self, principal: DevicePrincipal, run_id: str) -> RunRecord:
        run = self.runs.get(run_id)
        if run is None:
            raise AppError(ErrorCode.NOT_FOUND_RUN)
        snap = run.snapshot
        if (
            snap.tenant_id != principal.tenant_id
            or snap.device_id != principal.device_id
        ):
            raise AppError(ErrorCode.AUTHZ_FORBIDDEN)
        return run

    def cancel_run(self, principal: DevicePrincipal, run_id: str) -> RunRecord:
        run = self.require_owned_run(principal, run_id)
        if run.machine.is_terminal:
            return run
        run.machine.transition(RunState.CANCELLED)
        self.emit_sse(run, SSEEventType.RUN_COMPLETED, {"status": "cancelled"})
        return run


STORE = MemoryStore()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _status(run: RunRecord) -> RunStatusResponse:
    return RunStatusResponse(
        run_id=run.run_id,
        session_id=run.session_id,
        state=run.machine.current,
        created_at=run.created_at,
        cancelled=run.machine.current is RunState.CANCELLED,
    )


@router.post(
    "/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED
)
async def create_session(
    req: CreateSessionRequest,
    principal: DevicePrincipal = Depends(get_device_principal),
) -> SessionResponse:
    return STORE.create_session(principal, req)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    principal: DevicePrincipal = Depends(get_device_principal),
) -> None:
    STORE.delete_session(principal, session_id)


@router.post("/agent/runs", response_model=RunStatusResponse)
async def create_run(
    req: CreateRunRequest,
    request: Request,
    principal: DevicePrincipal = Depends(get_device_principal),
) -> RunStatusResponse:
    run = STORE.create_run(
        principal,
        req,
        request_id=request.state.request_id,
        trace_id=request.state.trace_id,
    )
    return _status(run)


@router.get("/agent/runs/{run_id}", response_model=RunStatusResponse)
async def get_run(
    run_id: str,
    principal: DevicePrincipal = Depends(get_device_principal),
) -> RunStatusResponse:
    return _status(STORE.require_owned_run(principal, run_id))


@router.delete("/agent/runs/{run_id}", response_model=RunStatusResponse)
async def cancel_run(
    run_id: str,
    principal: DevicePrincipal = Depends(get_device_principal),
) -> RunStatusResponse:
    return _status(STORE.cancel_run(principal, run_id))


@router.get("/agent/runs/{run_id}/events")
async def get_run_events(
    run_id: str,
    principal: DevicePrincipal = Depends(get_device_principal),
    after_seq: int = Query(0, ge=0),
) -> dict:
    run = STORE.require_owned_run(principal, run_id)
    events = [e for e in run.sse_events if e.seq > after_seq]
    return {
        "run_id": run_id,
        "next_seq": run.machine.event_seq,
        "events": [e.model_dump(mode="json") for e in events],
    }


@router.get("/health/live")
async def live() -> dict:
    return {"status": "alive"}


@router.get("/health/ready")
async def ready() -> dict:
    # component status only — never expose session/run counts publicly
    return {"status": "ready", "checks": {"core": "ok"}}
