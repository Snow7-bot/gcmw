"""Agent API router (issue #36 / #36c — B2-B wiring round).

Architecture rules enforced here:
- ``RunRepository`` (#65B-2 slice B2-A) is the ONLY authority for run state and
  events: every transition is committed through its expected-state CAS and the
  public SSE route consumes its atomic snapshots. No second event list, and no
  second sequence number, may reappear in this layer;
- the admission service keeps a LIFECYCLE MIRROR only (session TTL, idempotency,
  one active run per session, payload hashes) and never invents a seq;
- admission is atomic (asyncio lock, one per running loop): an idempotent replay
  returns the original run, one active (non-terminal) run per session;
- sessions expire on an injectable clock; expiry removes session, runs,
  idempotency entries, raw question snapshots AND the durable run records;
- tenant/device always derive from the DevicePrincipal (default deny); run
  ownership is checked ONCE, before the stream response starts — an unknown or
  foreign run never receives a single streamed byte;
- this slice (B2-B) exposes the public SSE route only: no subscriber lease, no
  disconnect->cancel propagation and no reconnect grace (all B2-C).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
import weakref
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api.v1.auth import DevicePrincipal, PrincipalDep
from app.api.v1.errors import AppError, request_ids
from app.api.v1.sse_stream import (
    DEFAULT_HEARTBEAT_MS,
    SnapshotReader,
    SSEStreamError,
    StreamSnapshot,
    effective_after_seq,
    stream_engine,
    stream_error_frame,
)
from app.contracts.api import (
    CreateRunRequest,
    CreateSessionRequest,
    RunStatusResponse,
    SessionResponse,
)
from app.contracts.common import Channel
from app.contracts.errors import ErrorCode
from app.contracts.run import RunState
from app.orchestration.state_machine import is_terminal_state
from app.storage.run_repository import (
    MemoryRunRepository,
    RunIdentity,
    RunRepositoryError,
    RunRepositoryFault,
)

router = APIRouter(prefix="/api/v1")

DEFAULT_SESSION_TTL_S = 1800

#: fixed server-side heartbeat — never derived from client input
SSE_HEARTBEAT_S = DEFAULT_HEARTBEAT_MS / 1000

#: SSE responses are per-principal: no store, and no proxy buffering
SSE_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
}

#: bounded CAS retries for cancel: a run may move under us, but the loop ends
MAX_CANCEL_ATTEMPTS = 3


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
    """Admission/lifecycle record: mirror state + the durable identity.

    ``state`` is a MIRROR of the repository's authoritative state, refreshed on
    every read/transition; the repository's expected-state CAS is what actually
    guarantees the two can never diverge silently.
    """

    run_id: str
    session_id: str
    identity: RunIdentity
    snapshot: RunSnapshot
    payload_hash: str
    state: RunState = RunState.ACCEPTED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# RunAdmissionService: admission + lifecycle only (no storage authority).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunStatusSnapshot:
    """Immutable status DTO produced under the admission lock."""

    run_id: str
    session_id: str
    state: RunState
    created_at: datetime

    @property
    def cancelled(self) -> bool:
        return self.state is RunState.CANCELLED


class RunAdmissionService:
    def __init__(
        self,
        repository: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.repository = repository or MemoryRunRepository()
        # one admission lock per running loop: the service is a process-wide
        # singleton while an asyncio.Lock belongs to the loop that contends on
        # it (tests legitimately drive the same service from several loops)
        self._locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self.sessions: dict[str, SessionRecord] = {}
        self.runs: dict[str, RunRecord] = {}
        # (session_id, idempotency_key) -> (run_id, payload_hash)
        self.idempotency: dict[tuple[str, str], tuple[str, str]] = {}

    def now(self) -> datetime:
        return self._clock()

    def _admission_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[loop] = lock
        return lock

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
        """Create a session record.

        Deliberately synchronous: it is a single dict insertion with no
        check-then-act window, so no admission lock (and no await) is needed;
        the run lifecycle — which does span awaits — is locked.
        """
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

    async def delete_session(self, principal: DevicePrincipal, session_id: str) -> None:
        async with self._admission_lock():
            session = self.sessions.get(session_id)
            if session is None:
                raise AppError(ErrorCode.NOT_FOUND_SESSION)
            if self._session_expired(session):
                self._purge_session(session_id)
                raise AppError(ErrorCode.NOT_FOUND_SESSION)
            if not self._owns(session, principal):
                raise AppError(ErrorCode.AUTHZ_FORBIDDEN)
            # durable records go FIRST: a storage failure must not leave the
            # session purged in memory while its events are still readable
            for record in [r for r in self.runs.values() if r.session_id == session_id]:
                await self._delete_durable(record)
            self._purge_session(session_id)

    @staticmethod
    def _owns(session: SessionRecord, principal: DevicePrincipal) -> bool:
        return (
            session.tenant_id == principal.tenant_id
            and session.device_id == principal.device_id
        )

    # -- durable plumbing --------------------------------------------------------

    async def _delete_durable(self, record: RunRecord) -> None:
        try:
            await self.repository.delete(record.identity)
        except RunRepositoryError as exc:
            if exc.fault is RunRepositoryFault.NOT_FOUND:
                return  # already gone (TTL/expiry): delete is idempotent
            raise AppError(exc.code) from exc

    async def _durable_state(self, record: RunRecord) -> RunState:
        """Read the authoritative state and refresh the mirror."""
        try:
            state = await self.repository.state(record.identity)
        except RunRepositoryError as exc:
            raise AppError(exc.code) from exc
        record.state = state
        return state

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

    def _snapshot(self, run: RunRecord) -> RunStatusSnapshot:
        return RunStatusSnapshot(
            run_id=run.run_id,
            session_id=run.session_id,
            state=run.state,
            created_at=run.created_at,
        )

    def _owned_run(self, principal: DevicePrincipal, run_id: str) -> RunRecord:
        """Owned-run lookup; must be called under the admission lock."""
        record = self.runs.get(run_id)
        if record is None:
            raise AppError(ErrorCode.NOT_FOUND_RUN)
        self._expire_if_needed(record.session_id)
        record = self.runs.get(run_id)
        if record is None:  # expired while we looked
            raise AppError(ErrorCode.NOT_FOUND_RUN)
        snapshot = record.snapshot
        if (
            snapshot.tenant_id != principal.tenant_id
            or snapshot.device_id != principal.device_id
        ):
            raise AppError(ErrorCode.AUTHZ_FORBIDDEN)
        return record

    async def create_run(
        self,
        principal: DevicePrincipal,
        req: CreateRunRequest,
        request_id: str,
        trace_id: str,
    ) -> RunStatusSnapshot:
        async with self._admission_lock():
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
                record = self.runs.get(run_id)
                if record is not None:
                    if stored_hash == payload_hash:
                        return self._snapshot(record)  # idempotent replay
                    raise AppError(ErrorCode.CONFLICT_IDEMPOTENCY)

            # one active (non-terminal) run per session
            for run in self.runs.values():
                if run.session_id == session.session_id and not is_terminal_state(
                    run.state
                ):
                    raise AppError(ErrorCode.CONFLICT_ACTIVE_RUN)

            run_id = uuid.uuid4().hex
            identity = RunIdentity(
                run_id=run_id,
                tenant_id=session.tenant_id,
                device_id=session.device_id,
                session_id=session.session_id,
            )
            try:
                # seeds seq 1 = run.accepted inside the repository's own atomic
                # create (state + first event committed together)
                await self.repository.create(identity)
            except RunRepositoryError as exc:
                raise AppError(exc.code) from exc

            record = RunRecord(
                run_id=run_id,
                session_id=session.session_id,
                identity=identity,
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
            self.runs[run_id] = record
            self.idempotency[key] = (run_id, payload_hash)
            return self._snapshot(record)

    async def get_run(
        self, principal: DevicePrincipal, run_id: str
    ) -> RunStatusSnapshot:
        async with self._admission_lock():
            record = self._owned_run(principal, run_id)
            await self._durable_state(record)
            return self._snapshot(record)

    async def cancel_run(
        self, principal: DevicePrincipal, run_id: str
    ) -> RunStatusSnapshot:
        async with self._admission_lock():
            record = self._owned_run(principal, run_id)
            for _ in range(MAX_CANCEL_ATTEMPTS):
                state = await self._durable_state(record)
                if is_terminal_state(state):
                    return self._snapshot(record)  # cancel is idempotent
                try:
                    # expected-state CAS: a concurrent transition can never be
                    # silently overwritten by a cancel
                    await self.repository.commit_transition(
                        record.identity,
                        expected_state=state,
                        next_state=RunState.CANCELLED,
                    )
                except RunRepositoryError as exc:
                    if exc.fault is RunRepositoryFault.CAS_CONFLICT:
                        continue  # the run moved: re-read and retry, bounded
                    raise AppError(exc.code) from exc
                record.state = RunState.CANCELLED
                return self._snapshot(record)
            raise AppError(ErrorCode.CONFLICT_ACTIVE_RUN)

    # -- streaming ---------------------------------------------------------------

    async def authorize_stream(
        self, principal: DevicePrincipal, run_id: str
    ) -> tuple[RunIdentity, RunState]:
        """Single authentication + ownership read, BEFORE the response starts.

        Both the tenant/device check and the existence check happen here, so a
        foreign or unknown run is answered with a JSON error envelope and never
        with a half-open stream.
        """
        async with self._admission_lock():
            record = self._owned_run(principal, run_id)
            state = await self._durable_state(record)
            return record.identity, state

    def reader(self, identity: RunIdentity) -> SnapshotReader:
        """Bind the repository's atomic read interface to one run identity."""

        async def wait_page(cursor: int, timeout_s: float) -> StreamSnapshot:
            return await self.repository.snapshot(identity, cursor, timeout_s)

        return wait_page


SERVICE = RunAdmissionService()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _snapshot_response(snap: RunStatusSnapshot) -> RunStatusResponse:
    return RunStatusResponse(
        run_id=snap.run_id,
        session_id=snap.session_id,
        state=snap.state,
        created_at=snap.created_at,
        cancelled=snap.cancelled,
    )


async def _stream_with_errors(
    source: AsyncIterator[str], *, request_id: str, trace_id: str
) -> AsyncIterator[str]:
    """Map every mid-stream failure onto ONE structured error frame.

    ``CancelledError`` (client disconnect) is deliberately not caught: B2-B has
    no cancellation policy and the B-1 engine stays side-effect free. The frame
    carries the same ErrorEnvelope as the JSON boundary and always ends the
    stream — raw exception text never reaches a client.
    """
    try:
        async for chunk in source:
            yield chunk
    except (AppError, SSEStreamError, RunRepositoryError) as exc:
        yield stream_error_frame(exc.code, request_id=request_id, trace_id=trace_id)
    except Exception:  # noqa: BLE001 - registry-only envelope, never the text
        yield stream_error_frame(
            ErrorCode.INTERNAL_UNKNOWN, request_id=request_id, trace_id=trace_id
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
    await SERVICE.delete_session(principal, session_id)


@router.post("/agent/runs", response_model=RunStatusResponse)
async def create_run(
    req: CreateRunRequest,
    request: Request,
    principal: DevicePrincipal = PrincipalDep,
) -> RunStatusResponse:
    request_id, trace_id = request_ids(request)
    snap = await SERVICE.create_run(
        principal, req, request_id=request_id, trace_id=trace_id
    )
    return _snapshot_response(snap)


@router.get("/agent/runs/{run_id}", response_model=RunStatusResponse)
async def get_run(
    run_id: str,
    principal: DevicePrincipal = PrincipalDep,
) -> RunStatusResponse:
    return _snapshot_response(await SERVICE.get_run(principal, run_id))


@router.delete("/agent/runs/{run_id}", response_model=RunStatusResponse)
async def cancel_run(
    run_id: str,
    principal: DevicePrincipal = PrincipalDep,
) -> RunStatusResponse:
    return _snapshot_response(await SERVICE.cancel_run(principal, run_id))


@router.get("/agent/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    principal: DevicePrincipal = PrincipalDep,
    after_seq: int = Query(0, ge=0),
) -> StreamingResponse:
    """Public SSE route: one atomic read stream per authenticated run.

    Order matters: authentication (``PrincipalDep``, default deny) and the
    ownership/existence check both complete before the response starts, so
    failures are plain JSON envelopes. Resume state comes from ``Last-Event-ID``
    and/or ``after_seq`` (max wins); the heartbeat window is a server constant.
    """
    identity, _state = await SERVICE.authorize_stream(principal, run_id)
    cursor = effective_after_seq(after_seq, request.headers.get("last-event-id"))
    request_id, trace_id = request_ids(request)
    engine = stream_engine(
        wait_page=SERVICE.reader(identity),
        after_seq=cursor,
        heartbeat_s=SSE_HEARTBEAT_S,
    )
    return StreamingResponse(
        _stream_with_errors(engine, request_id=request_id, trace_id=trace_id),
        media_type="text/event-stream",
        headers=dict(SSE_RESPONSE_HEADERS),
    )


@router.get("/health/live")
async def live() -> dict:
    return {"status": "alive"}


@router.get("/health/ready")
async def ready() -> dict:
    # component status only — never expose session/run counts publicly
    return {"status": "ready", "checks": {"core": "ok"}}
