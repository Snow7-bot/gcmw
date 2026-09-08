"""Agent API router (issue #36, v1 skeleton — revision round 2).

Architecture rules enforced here:
- ONE state authority per run: ``RunStateMachine`` (#10/#21); the legacy
  RunCoordinator/RunIdempotencyRegistry are deleted — no second machine set
  or request_id-keyed dedup may reappear (request_id is tracing only);
- state transition + SSE append happen inside ONE locked lifecycle service
  (``RunAdmissionService``): the SSE seq comes from the transition's own
  returned event_seq, so state and SSE can never diverge;
- admission is atomic: idempotent replay returns the original run, one active
  (non-terminal) run per session at a time, all under a single RLock;
- sessions expire on an injectable clock; expiry removes session, its runs,
  the idempotency entries and the raw question snapshots;
- tenant/device always derive from the DevicePrincipal (default deny); run
  ownership is enforced on read/cancel/events.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request, status

from app.api.v1.auth import DevicePrincipal, PrincipalDep
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

DEFAULT_SESSION_TTL_S = 1800


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
    ttl_s: int = DEFAULT_SESSION_TTL_S

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(seconds=self.ttl_s)


@dataclass
class RunSnapshot:
    """Minimal snapshot the future Manager/QA agents need to execute a run.

    Short-lived: removed on session expiry/deletion (never persisted beyond
    the store). request_id/trace_id are kept for audit correlation only.
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
# RunAdmissionService: the single lifecycle authority (atomic, TTL-aware).
# ---------------------------------------------------------------------------


class RunAdmissionService:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.sessions: dict[str, SessionRecord] = {}
        self.runs: dict[str, RunRecord] = {}
        # (session_id, idempotency_key) -> (run_id, payload_hash)
        self.idempotency: dict[tuple[str, str], tuple[str, str]] = {}

    def now(self) -> datetime:
        return self._clock()

    # -- expiry ---------------------------------------------------------------

    def _purge_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        for run_id in [
            r for r in list(self.runs) if self.runs[r].session_id == session_id
        ]:
            del self.runs[run_id]
        for key in [k for k in list(self.idempotency) if k[0] == session_id]:
            del self.idempotency[key]

    def _session_expired(self, session: SessionRecord) -> bool:
        return self.now() >= session.expires_at

    def _expire_if_needed(self, session_id: str) -> None:
        """Called under the admission lock. Expired sessions vanish entirely —
        session, runs, idempotency and the raw text snapshots included."""
        session = self.sessions.get(session_id)
        if session is not None and self._session_expired(session):
            self._purge_session(session_id)

    # -- sessions --------------------------------------------------------------

    def create_session(
        self,
        principal: DevicePrincipal,
        req: CreateSessionRequest,
        ttl_s: int = DEFAULT_SESSION_TTL_S,
    ) -> SessionResponse:
        with self._lock:
            record = SessionRecord(
                session_id=uuid.uuid4().hex,
                tenant_id=principal.tenant_id,
                device_id=principal.device_id,
                channel=req.channel,
                locale=req.locale,
                created_at=self.now(),
                ttl_s=ttl_s,
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
        with self._lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise AppError(ErrorCode.NOT_FOUND_SESSION)
            if self._session_expired(session):
                self._purge_session(session_id)
                raise AppError(ErrorCode.NOT_FOUND_SESSION)
            if not self._owns(session, principal):
                raise AppError(ErrorCode.AUTHZ_FORBIDDEN)
            self._purge_session(session_id)

    @staticmethod
    def _owns(session: SessionRecord, principal: DevicePrincipal) -> bool:
        return (
            session.tenant_id == principal.tenant_id
            and session.device_id == principal.device_id
        )

    # -- runs -------------------------------------------------------------------

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

    def _advance(
        self, run: RunRecord, target: RunState, event_type: SSEEventType, data: dict
    ) -> None:
        """State transition + SSE append in one locked step.

        The SSE sequence is taken from the event returned by transition(), so
        state history and SSE events can never diverge or skip numbers.
        """
        run_event = run.machine.transition(target)
        seq = run_event.event_seq
        sse = SSEEvent(
            seq=seq,
            tenant_id=run.snapshot.tenant_id,
            device_id=run.snapshot.device_id,
            session_id=run.snapshot.session_id,
            run_id=run.run_id,
            layer="process",
            event=event_type,
            data=data,
        )
        run.sse_events.append(sse)

    def create_run(
        self,
        principal: DevicePrincipal,
        req: CreateRunRequest,
        request_id: str,
        trace_id: str,
    ) -> RunRecord:
        with self._lock:
            self._expire_if_needed(req.session_id)
            session = self.sessions.get(req.session_id)
            if session is None:
                raise AppError(ErrorCode.NOT_FOUND_SESSION)
            if not self._owns(session, principal):
                raise AppError(ErrorCode.AUTHZ_FORBIDDEN)

            payload_hash = self.payload_hash(session, req)
            key = (session.session_id, req.idempotency_key)
            existing = self.idempotency.get(key)
            if existing is not None:
                run_id, stored_hash = existing
                if run_id in self.runs:
                    if stored_hash == payload_hash:
                        return self.runs[run_id]  # idempotent replay
                    raise AppError(ErrorCode.CONFLICT_IDEMPOTENCY)

            # one active (non-terminal) run per session
            for run in self.runs.values():
                if run.session_id == session.session_id and not run.machine.is_terminal:
                    raise AppError(ErrorCode.CONFLICT_ACTIVE_RUN)

            run_id = uuid.uuid4().hex
            machine = RunStateMachine(run_id)  # ACCEPTED, event_seq == 1
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
            # accepted SSE event is appended by _advance-equivalent under the
            # same lock; initial machine event_seq is 1
            run.sse_events.append(
                SSEEvent(
                    seq=1,
                    tenant_id=run.snapshot.tenant_id,
                    device_id=run.snapshot.device_id,
                    session_id=run.snapshot.session_id,
                    run_id=run.run_id,
                    layer="process",
                    event=SSEEventType.RUN_ACCEPTED,
                    data={"status": "accepted", "message": "问题已接收"},
                )
            )
            self.runs[run.run_id] = run
            self.idempotency[key] = (run.run_id, payload_hash)
            return run

    def get_run(self, principal: DevicePrincipal, run_id: str) -> RunRecord:
        with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                raise AppError(ErrorCode.NOT_FOUND_RUN)
            self._expire_if_needed(run.snapshot.session_id)
            run = self.runs.get(run_id)
            if run is None:  # expired while we looked
                raise AppError(ErrorCode.NOT_FOUND_RUN)
            if not self._owns_run(run, principal):
                raise AppError(ErrorCode.AUTHZ_FORBIDDEN)
            return run

    def cancel_run(self, principal: DevicePrincipal, run_id: str) -> RunRecord:
        with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                raise AppError(ErrorCode.NOT_FOUND_RUN)
            self._expire_if_needed(run.snapshot.session_id)
            run = self.runs.get(run_id)
            if run is None:
                raise AppError(ErrorCode.NOT_FOUND_RUN)
            if not self._owns_run(run, principal):
                raise AppError(ErrorCode.AUTHZ_FORBIDDEN)
            if run.machine.is_terminal:
                return run
            self._advance(
                run,
                RunState.CANCELLED,
                SSEEventType.RUN_COMPLETED,
                {"status": "cancelled"},
            )
            return run

    def events(
        self, principal: DevicePrincipal, run_id: str, after_seq: int
    ) -> tuple[RunRecord, list[SSEEvent]]:
        with self._lock:
            run = self.get_run(principal, run_id)
            return run, [e for e in run.sse_events if e.seq > after_seq]

    @staticmethod
    def _owns_run(run: RunRecord, principal: DevicePrincipal) -> bool:
        snap = run.snapshot
        return (
            snap.tenant_id == principal.tenant_id
            and snap.device_id == principal.device_id
        )


SERVICE = RunAdmissionService()


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
    principal: DevicePrincipal = PrincipalDep,
) -> SessionResponse:
    return SERVICE.create_session(principal, req)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    principal: DevicePrincipal = PrincipalDep,
) -> None:
    SERVICE.delete_session(principal, session_id)


@router.post("/agent/runs", response_model=RunStatusResponse)
async def create_run(
    req: CreateRunRequest,
    request: Request,
    principal: DevicePrincipal = PrincipalDep,
) -> RunStatusResponse:
    run = SERVICE.create_run(
        principal,
        req,
        request_id=request.state.request_id,
        trace_id=request.state.trace_id,
    )
    return _status(run)


@router.get("/agent/runs/{run_id}", response_model=RunStatusResponse)
async def get_run(
    run_id: str,
    principal: DevicePrincipal = PrincipalDep,
) -> RunStatusResponse:
    return _status(SERVICE.get_run(principal, run_id))


@router.delete("/agent/runs/{run_id}", response_model=RunStatusResponse)
async def cancel_run(
    run_id: str,
    principal: DevicePrincipal = PrincipalDep,
) -> RunStatusResponse:
    return _status(SERVICE.cancel_run(principal, run_id))


@router.get("/agent/runs/{run_id}/events")
async def get_run_events(
    run_id: str,
    principal: DevicePrincipal = PrincipalDep,
    after_seq: int = Query(0, ge=0),
) -> dict:
    run, events = SERVICE.events(principal, run_id, after_seq)
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
