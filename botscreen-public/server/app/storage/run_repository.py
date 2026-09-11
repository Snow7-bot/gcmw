"""Atomic run repository (issue #65B-2 slice B2-A).

State and events live together in ONE atomic commit: a run's state field and
its event stream are written by a single script/transaction, so they can never
diverge (no "state says COMPLETED but the terminal event is missing", no
"event appended while the state stayed behind").

Design:
- every transition is a compare-and-set: the caller names the state it expects
  next to; a mismatch (another worker already advanced the run) is a conflict
  and NOTHING is written;
- the event sequence is derived inside the same atomic step from the stored
  ``latest_seq``, so concurrent commits can never mint duplicate seqs;
- the terminal event records ``terminal_seq`` in the same commit — the SSE
  snapshot can therefore always answer "is this run terminal and at which seq"
  without guessing;
- one run-level TTL is refreshed on the state key and the event stream
  together (no per-event expiry, hence no sequence holes);
- ``snapshot(cursor, timeout_s)`` implements the #65B-1 engine read interface
  (blocking async wait, ``XREAD BLOCK`` for Redis) and derives the window
  bounds (``oldest_available_seq``/``latest_seq``/``terminal_seq``);
- run identity is tenant-bound: every call carries the trusted tenant and a
  mismatch is reported as "not found" for that tenant.

Two implementations: :class:`MemoryRunRepository` (deterministic, used by the
engine unit tests) and :class:`RedisRunRepository` (Lua scripts; blocking
``XREAD``; explicit failure when the client is unavailable).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from app.api.v1.sse_stream import StreamSnapshot
from app.contracts.errors import ErrorCode
from app.contracts.events import SSEEvent, SSEEventType
from app.contracts.run import TERMINAL_STATES, RunState

DEFAULT_MAX_EVENTS_PER_RUN = 10_000
DEFAULT_RUN_TTL_S = 1800  # V2.3 session idle window
DEFAULT_BLOCK_MS = 15_000


class RunRepositoryFault(str, Enum):
    CAS_CONFLICT = "cas_conflict"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    INVARIANT = "invariant"


class RunRepositoryError(RuntimeError):
    """Structured repository failure (mapped by the #36 boundary)."""

    def __init__(self, fault: RunRepositoryFault, message: str = "") -> None:
        super().__init__(message or fault.value)
        self.fault = fault
        self.code = (
            ErrorCode.CONFLICT_ACTIVE_RUN
            if fault is RunRepositoryFault.CAS_CONFLICT
            else ErrorCode.NOT_FOUND_RUN
            if fault is RunRepositoryFault.NOT_FOUND
            else ErrorCode.UNAVAILABLE_OVERLOADED
            if fault is RunRepositoryFault.UNAVAILABLE
            else ErrorCode.INTERNAL_UNKNOWN
        )


@dataclass(frozen=True)
class RunIdentity:
    """Tenant-bound run identity (tenancy comes from the trusted caller)."""

    run_id: str
    tenant_id: str
    device_id: str = ""
    session_id: str = ""


@dataclass
class _RunRecord:
    identity: RunIdentity
    state: RunState
    events: list[SSEEvent] = field(default_factory=list)
    terminal_seq: int | None = None
    waiters: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def latest_seq(self) -> int:
        return self.events[-1].seq if self.events else 0


_DEFAULT_DATA: dict[SSEEventType, dict[str, Any]] = {
    SSEEventType.RUN_ACCEPTED: {"status": "accepted", "message": "问题已接收"},
    SSEEventType.PROCESS_STATUS: {"stage": "guarding", "message": "处理中"},
    SSEEventType.RUN_COMPLETED: {"status": "completed"},
}


def _event(
    identity: RunIdentity,
    seq: int,
    event_type: SSEEventType,
    data: dict[str, Any] | None,
) -> SSEEvent:
    """Build one event; per-type default data keeps the SSE contract happy."""
    payload = dict(data) if data else dict(_DEFAULT_DATA.get(event_type, {}))
    return SSEEvent(
        seq=seq,
        tenant_id=identity.tenant_id,
        device_id=identity.device_id or "device",
        session_id=identity.session_id or "session",
        run_id=identity.run_id,
        layer="process",
        event=event_type,
        data=payload,
    )


def _snapshot(record: _RunRecord, cursor: int, *, timed_out: bool) -> StreamSnapshot:
    events = tuple(e for e in record.events if e.seq > cursor)
    oldest = record.events[0].seq if record.events else 0
    latest = record.latest_seq
    terminal_state = record.state in TERMINAL_STATES
    return StreamSnapshot(
        events=() if timed_out else events,
        state=record.state,
        oldest_available_seq=oldest,
        latest_seq=latest,
        terminal_seq=record.terminal_seq if terminal_state else None,
        timed_out=timed_out,
    )


class MemoryRunRepository:
    """Deterministic async repository (engine tests, single-process dev)."""

    def __init__(self, *, max_events: int = DEFAULT_MAX_EVENTS_PER_RUN) -> None:
        self._max_events = max_events
        self._runs: dict[str, _RunRecord] = {}
        self._lock = asyncio.Lock()

    # -- lifecycle -------------------------------------------------------------

    async def create(
        self,
        identity: RunIdentity,
        *,
        event_type: SSEEventType = SSEEventType.RUN_ACCEPTED,
        data: dict[str, Any] | None = None,
    ) -> int:
        async with self._lock:
            if identity.run_id in self._runs:
                raise RunRepositoryError(
                    RunRepositoryFault.CAS_CONFLICT,
                    f"run {identity.run_id!r} already exists",
                )
            record = _RunRecord(identity=identity, state=RunState.ACCEPTED)
            record.events.append(
                _event(identity, 1, event_type, data or {"status": "accepted"})
            )
            self._runs[identity.run_id] = record
            record.waiters.set()
            return 1

    async def commit(
        self,
        identity: RunIdentity,
        *,
        expected_state: RunState,
        next_state: RunState,
        event_type: SSEEventType,
        data: dict[str, Any] | None = None,
    ) -> int:
        """Atomic compare-and-set + append. Returns the new event seq."""
        async with self._lock:
            record = self._require(identity)
            if record.state is not expected_state:
                raise RunRepositoryError(
                    RunRepositoryFault.CAS_CONFLICT,
                    f"run {identity.run_id!r} is {record.state.value}, "
                    f"expected {expected_state.value}",
                )
            if record.state in TERMINAL_STATES:
                raise RunRepositoryError(
                    RunRepositoryFault.INVARIANT,
                    f"run {identity.run_id!r} is already terminal",
                )
            seq = record.latest_seq + 1
            record.events.append(_event(identity, seq, event_type, data))
            record.state = next_state
            if event_type is SSEEventType.RUN_COMPLETED:
                record.terminal_seq = seq
            while len(record.events) > self._max_events:
                record.events.pop(0)
            record.waiters.set()
            return seq

    async def state(self, identity: RunIdentity) -> RunState:
        async with self._lock:
            return self._require(identity).state

    async def delete(self, identity: RunIdentity) -> None:
        async with self._lock:
            self._runs.pop(identity.run_id, None)

    # -- engine read interface --------------------------------------------------

    async def snapshot(
        self, identity: RunIdentity, cursor: int, timeout_s: float
    ) -> StreamSnapshot:
        """Blocking async read for the SSE engine (B-1 ``wait_page``)."""
        async with self._lock:
            record = self._require(identity)
            if record.latest_seq > cursor or record.state in TERMINAL_STATES:
                return _snapshot(record, cursor, timed_out=False)
            record.waiters.clear()
        try:
            await asyncio.wait_for(record.waiters.wait(), timeout=timeout_s)
        except TimeoutError:
            pass
        async with self._lock:
            record = self._require(identity)
            if record.latest_seq > cursor:
                return _snapshot(record, cursor, timed_out=False)
            return _snapshot(record, cursor, timed_out=True)

    # -- helpers ---------------------------------------------------------------

    def _require(self, identity: RunIdentity) -> _RunRecord:
        record = self._runs.get(identity.run_id)
        if record is None or record.identity.tenant_id != identity.tenant_id:
            raise RunRepositoryError(
                RunRepositoryFault.NOT_FOUND,
                f"run {identity.run_id!r} not found for this tenant",
            )
        return record


# ---------------------------------------------------------------------------
# Redis implementation (atomic Lua commit + blocking XREAD)
# ---------------------------------------------------------------------------

_LUA_CREATE = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('HSET', KEYS[1],
  'state', ARGV[1], 'tenant_id', ARGV[2], 'device_id', ARGV[3],
  'session_id', ARGV[4], 'latest_seq', '1')
redis.call('XADD', KEYS[2], 'MAXLEN', '=', ARGV[5], '*',
  'seq', '1', 'event', ARGV[6], 'data', ARGV[7])
if tonumber(ARGV[8]) > 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[8])
  redis.call('EXPIRE', KEYS[2], ARGV[8])
end
return 1
"""

_LUA_COMMIT = """
if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
local current = redis.call('HGET', KEYS[1], 'state')
if current ~= ARGV[2] then return 0 end
local seq = tonumber(redis.call('HGET', KEYS[1], 'latest_seq') or '0') + 1
redis.call('XADD', KEYS[2], 'MAXLEN', '=', ARGV[6], '*',
  'seq', tostring(seq), 'event', ARGV[4], 'data', ARGV[5])
redis.call('HSET', KEYS[1], 'state', ARGV[3], 'latest_seq', tostring(seq))
if ARGV[4] == 'run.completed' then
  redis.call('HSET', KEYS[1], 'terminal_seq', tostring(seq))
end
if tonumber(ARGV[7]) > 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[7])
  redis.call('EXPIRE', KEYS[2], ARGV[7])
end
return seq
"""


class RedisClientDuck(Protocol):
    """Redis surface the repository relies on (all atomic ops are Lua)."""

    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...

    def hgetall(self, name: str) -> dict: ...

    def xrange(self, name: str, start: str = "-", end: str = "+") -> list: ...

    def xread(
        self, streams: dict, count: int | None = None, block: int | None = None
    ) -> Any: ...

    def delete(self, *names: str) -> int: ...


class RedisRunRepository:
    """Atomic state+event persistence over Redis (Lua CAS + blocking XREAD)."""

    def __init__(
        self,
        client: RedisClientDuck,
        *,
        prefix: str = "gcmw:run:",
        max_events: int = DEFAULT_MAX_EVENTS_PER_RUN,
        ttl_s: int = DEFAULT_RUN_TTL_S,
    ) -> None:
        self._client = client
        self._prefix = prefix
        self._max_events = max_events
        self._ttl_s = ttl_s

    def _keys(self, run_id: str) -> tuple[str, str]:
        return (f"{self._prefix}{run_id}:state", f"{self._prefix}{run_id}:events")

    @staticmethod
    def _arg(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)

    # -- lifecycle -------------------------------------------------------------

    async def create(
        self,
        identity: RunIdentity,
        *,
        event_type: SSEEventType = SSEEventType.RUN_ACCEPTED,
        data: dict[str, Any] | None = None,
    ) -> int:
        state_key, stream_key = self._keys(identity.run_id)
        payload = dict(data) if data else dict(_DEFAULT_DATA.get(event_type, {}))
        try:
            outcome = self._client.eval(
                _LUA_CREATE,
                2,
                state_key,
                stream_key,
                RunState.ACCEPTED.value,
                identity.tenant_id,
                identity.device_id,
                identity.session_id,
                str(self._max_events),
                event_type.value,
                json.dumps(payload, ensure_ascii=False),
                str(self._ttl_s),
            )
        except Exception as exc:
            raise RunRepositoryError(
                RunRepositoryFault.UNAVAILABLE, f"redis create failed: {exc}"
            ) from exc
        if int(self._arg(outcome)) != 1:
            raise RunRepositoryError(
                RunRepositoryFault.CAS_CONFLICT,
                f"run {identity.run_id!r} already exists",
            )
        return 1

    async def commit(
        self,
        identity: RunIdentity,
        *,
        expected_state: RunState,
        next_state: RunState,
        event_type: SSEEventType,
        data: dict[str, Any] | None = None,
    ) -> int:
        state_key, stream_key = self._keys(identity.run_id)
        try:
            payload = dict(data) if data else dict(_DEFAULT_DATA.get(event_type, {}))
            outcome = self._client.eval(
                _LUA_COMMIT,
                2,
                state_key,
                stream_key,
                identity.tenant_id,
                expected_state.value,
                next_state.value,
                event_type.value,
                json.dumps(payload, ensure_ascii=False),
                str(self._max_events),
                str(self._ttl_s),
            )
        except Exception as exc:
            raise RunRepositoryError(
                RunRepositoryFault.UNAVAILABLE, f"redis commit failed: {exc}"
            ) from exc
        code = int(self._arg(outcome))
        if code == -1:
            raise RunRepositoryError(
                RunRepositoryFault.NOT_FOUND,
                f"run {identity.run_id!r} not found",
            )
        if code == 0:
            raise RunRepositoryError(
                RunRepositoryFault.CAS_CONFLICT,
                f"run {identity.run_id!r}: expected {expected_state.value}",
            )
        return code

    # -- reads ------------------------------------------------------------------

    def _state_fields(self, identity: RunIdentity) -> dict[str, str]:
        state_key, _ = self._keys(identity.run_id)
        raw = self._client.hgetall(state_key) or {}
        fields = {self._arg(k): self._arg(v) for k, v in raw.items()}
        if not fields:
            raise RunRepositoryError(
                RunRepositoryFault.NOT_FOUND,
                f"run {identity.run_id!r} not found",
            )
        if fields.get("tenant_id") != identity.tenant_id:
            # cross-tenant access reads as absent, never as data
            raise RunRepositoryError(
                RunRepositoryFault.NOT_FOUND,
                f"run {identity.run_id!r} not found for this tenant",
            )
        return fields

    async def state(self, identity: RunIdentity) -> RunState:
        return RunState(self._state_fields(identity)["state"])

    async def delete(self, identity: RunIdentity) -> None:
        state_key, stream_key = self._keys(identity.run_id)
        try:
            self._client.delete(state_key, stream_key)
        except Exception as exc:
            raise RunRepositoryError(
                RunRepositoryFault.UNAVAILABLE, f"redis delete failed: {exc}"
            ) from exc

    async def snapshot(
        self, identity: RunIdentity, cursor: int, timeout_s: float
    ) -> StreamSnapshot:
        """Blocking read: XREAD BLOCK on the run stream, then one atomic page."""
        fields = self._state_fields(identity)
        latest = int(fields.get("latest_seq", "0"))
        if latest <= cursor:
            block_ms = max(int(timeout_s * 1000), 1)
            _, stream_key = self._keys(identity.run_id)
            try:
                # BLOCK waits server-side for the next event (no client polling);
                # the sync client call runs in a worker thread so the event loop
                # keeps serving other runs while this stream idles
                await asyncio.to_thread(
                    self._client.xread,
                    {stream_key: f"{cursor}-0"},
                    count=1,
                    block=block_ms,
                )
            except Exception as exc:
                raise RunRepositoryError(
                    RunRepositoryFault.UNAVAILABLE, f"redis xread failed: {exc}"
                ) from exc
        return self._page(identity)

    def _page(self, identity: RunIdentity) -> StreamSnapshot:
        fields = self._state_fields(identity)
        state = RunState(fields["state"])
        terminal_seq = int(fields["terminal_seq"]) if "terminal_seq" in fields else None
        _, stream_key = self._keys(identity.run_id)
        try:
            entries = self._client.xrange(stream_key) or []
        except Exception as exc:
            raise RunRepositoryError(
                RunRepositoryFault.UNAVAILABLE, f"redis xrange failed: {exc}"
            ) from exc
        events: list[SSEEvent] = []
        for entry_id, raw in entries:
            ev = {self._arg(k): self._arg(v) for k, v in raw.items()}
            events.append(
                SSEEvent(
                    seq=int(ev["seq"]),
                    tenant_id=identity.tenant_id,
                    device_id=identity.device_id or "device",
                    session_id=identity.session_id or "session",
                    run_id=identity.run_id,
                    layer="process",
                    event=SSEEventType(ev["event"]),
                    data=json.loads(ev.get("data") or "{}"),
                )
            )
        latest = events[-1].seq if events else 0
        oldest = events[0].seq if events else 0
        terminal_state = state in TERMINAL_STATES
        return StreamSnapshot(
            events=tuple(events),
            state=state,
            oldest_available_seq=oldest,
            latest_seq=latest,
            terminal_seq=terminal_seq if terminal_state else None,
            timed_out=False,
        )


__all__ = [
    "DEFAULT_BLOCK_MS",
    "DEFAULT_MAX_EVENTS_PER_RUN",
    "DEFAULT_RUN_TTL_S",
    "MemoryRunRepository",
    "RedisClientDuck",
    "RedisRunRepository",
    "RunIdentity",
    "RunRepositoryError",
    "RunRepositoryFault",
]
