"""RunRepository — the single durable authority for run state + events (#65B-2 A).

Reviewer-driven design (round 2):

- **one persistence authority**: this repository owns run state AND its event
  stream. The older ``event_store`` module is deprecated for run events (see
  its docstring); nothing else may claim that authority;
- **tenant binding everywhere**: keys carry the run id, every write/delete/
  read is bound to the trusted tenant, and ownership is verified INSIDE the
  Redis Lua scripts (delete included) — another tenant's run reads/writes as
  "not found", never as data;
- **two atomic write operations**:
  * ``commit_transition`` — a legal state transition (rules reused from
    ``RunStateMachine``) that changes the state and appends its event in the
    same atomic step, deriving the event type (``process.status`` /
    ``run.completed``) and layer (``process``) from the target state;
  * ``append_event`` — a state-preserving event append (``answer.delta``,
    ``answer.completed``, ``evidence.found``, ...) for the answer layer;
- **business seq == physical Stream ID**: events are written with the explicit
  Redis id ``<seq>-0``, so ``XREAD``/``XRANGE`` resume directly by business
  cursor — no id/seq mixing;
- **atomic snapshot**: a single Lua script returns the state fields together
  with the event page, so a commit can never be observed half-applied;
  blocking waits use async ``XREAD BLOCK`` (``redis.asyncio``) followed by a
  fresh atomic snapshot, and an idle wait yields a valid timeout snapshot;
- **validated before commit**: the ``SSEEvent`` (layer, data whitelist,
  identity, immutable timestamp) is fully constructed and validated in Python
  BEFORE the atomic write, using the identity persisted at create time
  (never caller placeholders); the commit re-checks tenant/device/session and
  the expected seq, so a concurrent change forces a bounded retry;
- **first event is fixed**: ``create`` always writes ``run.accepted``;
- **configuration is validated**: ``max_events >= 1``, ``ttl_s`` is ``None``
  (no expiry) or ``>= 1`` — in both implementations;
- **orphan/terminal invariants**: a state key without its stream (or the
  reverse), a wrong-type key or a commit out of a terminal run is reported as
  an explicit invariant fault.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Protocol

from app.api.v1.sse_stream import StreamSnapshot
from app.contracts.errors import ErrorCode
from app.contracts.events import (
    EVENT_DATA_ALLOWED_KEYS,
    FORBIDDEN_DATA_KEYS,
    EventLayer,
    SSEEvent,
    SSEEventType,
)
from app.contracts.run import RunState
from app.orchestration.state_machine import (
    is_allowed_transition,
    is_terminal_state,
    transition_event_type,
)

DEFAULT_MAX_EVENTS_PER_RUN = 10_000
DEFAULT_RUN_TTL_S = 1800  # V2.3 session idle window
DEFAULT_BLOCK_MS = 15_000
DEFAULT_SNAPSHOT_LIMIT = 500
MAX_COMMIT_RETRIES = 4

#: events that may be appended WITHOUT changing run state (answer layer etc.)
STATELESS_EVENT_TYPES: frozenset[SSEEventType] = frozenset(
    {
        SSEEventType.ANSWER_DELTA,
        SSEEventType.ANSWER_COMPLETED,
        SSEEventType.EVIDENCE_FOUND,
        SSEEventType.REFLECTION_RESULT,
        SSEEventType.HEARTBEAT,
        SSEEventType.MIC_STATUS,
    }
)


class RunRepositoryFault(str, Enum):
    CAS_CONFLICT = "cas_conflict"
    CONCURRENT_MODIFICATION = "concurrent_modification"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    INVARIANT = "invariant"
    ILLEGAL_TRANSITION = "illegal_transition"


class RunRepositoryError(RuntimeError):
    """Structured repository failure (mapped by the #36 boundary)."""

    _CODES: ClassVar[dict[RunRepositoryFault, ErrorCode]] = {
        RunRepositoryFault.CAS_CONFLICT: ErrorCode.CONFLICT_ACTIVE_RUN,
        RunRepositoryFault.CONCURRENT_MODIFICATION: ErrorCode.CONFLICT_ACTIVE_RUN,
        RunRepositoryFault.NOT_FOUND: ErrorCode.NOT_FOUND_RUN,
        RunRepositoryFault.UNAVAILABLE: ErrorCode.UNAVAILABLE_OVERLOADED,
        RunRepositoryFault.INVARIANT: ErrorCode.INTERNAL_UNKNOWN,
        RunRepositoryFault.ILLEGAL_TRANSITION: ErrorCode.CONFLICT_ACTIVE_RUN,
    }

    def __init__(self, fault: RunRepositoryFault, message: str = "") -> None:
        super().__init__(message or fault.value)
        self.fault = fault
        self.code = self._CODES[fault]


@dataclass(frozen=True)
class RunIdentity:
    """Tenant-bound run identity; every field must be non-empty (validated)."""

    run_id: str
    tenant_id: str
    device_id: str
    session_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("run_id", self.run_id),
            ("tenant_id", self.tenant_id),
            ("device_id", self.device_id),
            ("session_id", self.session_id),
        ):
            if not value or not value.strip():
                raise ValueError(f"RunIdentity.{name} must be non-empty")


def _validate_config(max_events: int, ttl_s: int | None) -> None:
    if (
        isinstance(max_events, bool)
        or not isinstance(max_events, int)
        or max_events < 1
    ):
        raise ValueError(f"max_events must be a positive integer, got {max_events!r}")
    if ttl_s is not None and (
        isinstance(ttl_s, bool) or not isinstance(ttl_s, int) or ttl_s < 1
    ):
        raise ValueError(f"ttl_s must be None or a positive integer, got {ttl_s!r}")


def _layer_for(event_type: SSEEventType) -> EventLayer:
    """Derive the SSE layer from the event type (single authority)."""
    allowed = EVENT_DATA_ALLOWED_KEYS
    if event_type in {SSEEventType.ANSWER_DELTA, SSEEventType.ANSWER_COMPLETED}:
        return EventLayer.ANSWER
    if event_type.value in allowed or event_type in allowed:
        return EventLayer.PROCESS
    return EventLayer.PROCESS


def build_event(
    identity: RunIdentity,
    seq: int,
    event_type: SSEEventType,
    data: dict[str, Any] | None,
    *,
    timestamp: Any = None,
) -> SSEEvent:
    """Construct + validate the event BEFORE any write (whitelist enforced)."""
    payload = dict(data or {})
    for key in payload:
        if key in FORBIDDEN_DATA_KEYS:
            raise RunRepositoryError(
                RunRepositoryFault.INVARIANT, f"forbidden data key {key!r}"
            )
    allowed = EVENT_DATA_ALLOWED_KEYS.get(event_type, frozenset())
    unknown = set(payload) - set(allowed)
    if unknown:
        raise RunRepositoryError(
            RunRepositoryFault.INVARIANT,
            f"data keys {sorted(unknown)} not allowed for {event_type.value}",
        )
    kwargs: dict[str, Any] = {
        "seq": seq,
        "tenant_id": identity.tenant_id,
        "device_id": identity.device_id,
        "session_id": identity.session_id,
        "run_id": identity.run_id,
        "layer": _layer_for(event_type),
        "event": event_type,
        "data": payload,
    }
    if timestamp is not None:
        kwargs["timestamp"] = timestamp
    return SSEEvent(**kwargs)


def _snapshot_from(
    *,
    state: RunState,
    events: tuple[SSEEvent, ...],
    oldest: int,
    latest: int,
    terminal_seq: int | None,
    timed_out: bool,
) -> StreamSnapshot:
    terminal_state = is_terminal_state(state)
    return StreamSnapshot(
        events=events,
        state=state,
        oldest_available_seq=oldest,
        latest_seq=latest,
        terminal_seq=terminal_seq if terminal_state else None,
        timed_out=timed_out,
    )


# ---------------------------------------------------------------------------
# In-memory implementation (deterministic; same invariants as Redis)
# ---------------------------------------------------------------------------


@dataclass
class _Record:
    identity: RunIdentity
    state: RunState
    events: list[SSEEvent] = field(default_factory=list)
    terminal_seq: int | None = None
    expires_at: float | None = None
    waiters: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def latest_seq(self) -> int:
        return self.events[-1].seq if self.events else 0


class MemoryRunRepository:
    def __init__(
        self,
        *,
        max_events: int = DEFAULT_MAX_EVENTS_PER_RUN,
        ttl_s: int | None = DEFAULT_RUN_TTL_S,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        _validate_config(max_events, ttl_s)
        self._max_events = max_events
        self._ttl_s = ttl_s
        self._monotonic = monotonic or time.monotonic
        self._records: dict[str, _Record] = {}
        self._lock = asyncio.Lock()

    # -- lifecycle -------------------------------------------------------------

    async def create(self, identity: RunIdentity) -> int:
        async with self._lock:
            if identity.run_id in self._records:
                raise RunRepositoryError(
                    RunRepositoryFault.CAS_CONFLICT,
                    f"run {identity.run_id!r} already exists",
                )
            record = _Record(
                identity=identity,
                state=RunState.ACCEPTED,
                expires_at=(
                    None if self._ttl_s is None else self._monotonic() + self._ttl_s
                ),
            )
            record.events.append(
                build_event(
                    identity,
                    1,
                    SSEEventType.RUN_ACCEPTED,
                    {"status": "accepted", "message": "问题已接收"},
                )
            )
            self._records[identity.run_id] = record
            record.waiters.set()
            return 1

    async def commit_transition(
        self,
        identity: RunIdentity,
        *,
        expected_state: RunState,
        next_state: RunState,
        data: dict[str, Any] | None = None,
    ) -> int:
        async with self._lock:
            record = self._require(identity)
            if record.state is not expected_state:
                raise RunRepositoryError(
                    RunRepositoryFault.CAS_CONFLICT,
                    f"run {identity.run_id!r} is {record.state.value}, "
                    f"expected {expected_state.value}",
                )
            return self._transition_locked(record, expected_state, next_state, data)

    def _transition_locked(
        self,
        record: _Record,
        expected_state: RunState,
        next_state: RunState,
        data: dict[str, Any] | None,
    ) -> int:
        if is_terminal_state(record.state):
            raise RunRepositoryError(
                RunRepositoryFault.INVARIANT,
                f"run {record.identity.run_id!r} is already terminal",
            )
        if not is_allowed_transition(expected_state, next_state):
            raise RunRepositoryError(
                RunRepositoryFault.ILLEGAL_TRANSITION,
                f"illegal transition {expected_state.value} -> {next_state.value}",
            )
        event_type = SSEEventType(transition_event_type(next_state))
        seq = record.latest_seq + 1
        # identity comes from the persisted record — never from the caller
        event = build_event(record.identity, seq, event_type, data)
        record.events.append(event)
        record.state = next_state
        if is_terminal_state(next_state):
            record.terminal_seq = seq
        self._trim(record)
        self._refresh_ttl(record)
        record.waiters.set()
        return seq

    async def append_event(
        self,
        identity: RunIdentity,
        *,
        event_type: SSEEventType,
        data: dict[str, Any] | None = None,
    ) -> int:
        """State-preserving append for the answer layer (e.g. answer.delta)."""
        if event_type not in STATELESS_EVENT_TYPES:
            raise RunRepositoryError(
                RunRepositoryFault.INVARIANT,
                f"{event_type.value} is not a state-preserving event",
            )
        async with self._lock:
            record = self._require(identity)
            if is_terminal_state(record.state):
                raise RunRepositoryError(
                    RunRepositoryFault.INVARIANT,
                    f"run {record.identity.run_id!r} is terminal",
                )
            seq = record.latest_seq + 1
            record.events.append(build_event(record.identity, seq, event_type, data))
            self._trim(record)
            self._refresh_ttl(record)
            record.waiters.set()
            return seq

    async def state(self, identity: RunIdentity) -> RunState:
        async with self._lock:
            return self._require(identity).state

    async def delete(self, identity: RunIdentity) -> None:
        """Tenant-authenticated delete (foreign runs are never removed)."""
        async with self._lock:
            self._require(identity)
            self._records.pop(identity.run_id, None)

    # -- engine read interface --------------------------------------------------

    async def snapshot(
        self, identity: RunIdentity, cursor: int, timeout_s: float
    ) -> StreamSnapshot:
        async with self._lock:
            record = self._require(identity)
            if record.latest_seq > cursor or is_terminal_state(record.state):
                return self._page(record, cursor, timed_out=False)
            record.waiters.clear()
        try:
            await asyncio.wait_for(record.waiters.wait(), timeout=timeout_s)
        except TimeoutError:
            pass
        async with self._lock:
            record = self._require(identity)
            if record.latest_seq > cursor:
                return self._page(record, cursor, timed_out=False)
            return self._page(record, cursor, timed_out=True)

    def _page(self, record: _Record, cursor: int, *, timed_out: bool) -> StreamSnapshot:
        events = () if timed_out else tuple(e for e in record.events if e.seq > cursor)
        return _snapshot_from(
            state=record.state,
            events=events,
            oldest=record.events[0].seq if record.events else 0,
            latest=record.latest_seq,
            terminal_seq=record.terminal_seq,
            timed_out=timed_out,
        )

    # -- helpers ---------------------------------------------------------------

    def _trim(self, record: _Record) -> None:
        while len(record.events) > self._max_events:
            record.events.pop(0)

    def _refresh_ttl(self, record: _Record) -> None:
        if self._ttl_s is not None:
            record.expires_at = self._monotonic() + self._ttl_s

    def _require(self, identity: RunIdentity) -> _Record:
        record = self._records.get(identity.run_id)
        if record is None:
            raise RunRepositoryError(
                RunRepositoryFault.NOT_FOUND, f"run {identity.run_id!r} not found"
            )
        if record.identity.tenant_id != identity.tenant_id:
            # foreign run: absent, never data
            raise RunRepositoryError(
                RunRepositoryFault.NOT_FOUND,
                f"run {identity.run_id!r} not found for this tenant",
            )
        if record.expires_at is not None and self._monotonic() >= record.expires_at:
            del self._records[identity.run_id]
            raise RunRepositoryError(
                RunRepositoryFault.NOT_FOUND, f"run {identity.run_id!r} expired"
            )
        return record


# ---------------------------------------------------------------------------
# Redis implementation (redis.asyncio; explicit seq == stream id)
# ---------------------------------------------------------------------------

_LUA_CREATE = """
if redis.call('EXISTS', KEYS[1]) == 1 or redis.call('EXISTS', KEYS[2]) == 1 then
  return 0
end
redis.call('HSET', KEYS[1],
  'state', 'ACCEPTED', 'tenant_id', ARGV[1], 'device_id', ARGV[2],
  'session_id', ARGV[3], 'latest_seq', '1')
redis.call('XADD', KEYS[2], 'MAXLEN', '=', ARGV[5], ARGV[7], 'event', ARGV[6])
if tonumber(ARGV[4]) > 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[4])
  redis.call('EXPIRE', KEYS[2], ARGV[4])
end
return 1
"""

_LUA_COMMIT = """
if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
if redis.call('HGET', KEYS[1], 'tenant_id') ~= ARGV[1] then return -2 end
if redis.call('HGET', KEYS[1], 'device_id') ~= ARGV[2] then return -4 end
if redis.call('HGET', KEYS[1], 'session_id') ~= ARGV[3] then return -4 end
if redis.call('EXISTS', KEYS[2]) == 0 then return -5 end
if redis.call('HGET', KEYS[1], 'terminal_seq') then return -3 end
if redis.call('HGET', KEYS[1], 'state') ~= ARGV[4] then return 0 end
local latest = tonumber(redis.call('HGET', KEYS[1], 'latest_seq') or '0')
if latest ~= tonumber(ARGV[5]) then return 1 end
local seq = latest + 1
redis.call('XADD', KEYS[2], 'MAXLEN', '=', ARGV[9], tostring(seq) .. '-0',
  'event', ARGV[7])
redis.call('HSET', KEYS[1], 'state', ARGV[6], 'latest_seq', tostring(seq))
if ARGV[8] == '1' then
  redis.call('HSET', KEYS[1], 'terminal_seq', tostring(seq))
end
if tonumber(ARGV[10]) > 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[10])
  redis.call('EXPIRE', KEYS[2], ARGV[10])
end
return seq
"""

_LUA_APPEND = """
if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
if redis.call('HGET', KEYS[1], 'tenant_id') ~= ARGV[1] then return -2 end
if redis.call('HGET', KEYS[1], 'device_id') ~= ARGV[2] then return -4 end
if redis.call('HGET', KEYS[1], 'session_id') ~= ARGV[3] then return -4 end
if redis.call('EXISTS', KEYS[2]) == 0 then return -5 end
if redis.call('HGET', KEYS[1], 'terminal_seq') then return -3 end
local latest = tonumber(redis.call('HGET', KEYS[1], 'latest_seq') or '0')
if latest ~= tonumber(ARGV[4]) then return 1 end
local seq = latest + 1
redis.call('XADD', KEYS[2], 'MAXLEN', '=', ARGV[7], tostring(seq) .. '-0',
  'event', ARGV[5])
redis.call('HSET', KEYS[1], 'latest_seq', tostring(seq))
if tonumber(ARGV[8]) > 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[8])
  redis.call('EXPIRE', KEYS[2], ARGV[8])
end
return seq
"""

_LUA_DELETE = """
if redis.call('EXISTS', KEYS[1]) == 0 and redis.call('EXISTS', KEYS[2]) == 0 then
  return -1
end
if redis.call('EXISTS', KEYS[1]) == 1 then
  if redis.call('HGET', KEYS[1], 'tenant_id') ~= ARGV[1] then return -2 end
end
redis.call('DEL', KEYS[1])
redis.call('DEL', KEYS[2])
return 1
"""

_LUA_SNAPSHOT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  if redis.call('EXISTS', KEYS[2]) == 1 then return redis.error_reply('ORPHAN_STREAM') end
  return redis.error_reply('NOT_FOUND')
end
if redis.call('HGET', KEYS[1], 'tenant_id') ~= ARGV[1] then
  return redis.error_reply('NOT_FOUND')
end
if redis.call('EXISTS', KEYS[2]) == 0 then
  return redis.error_reply('ORPHAN_STATE')
end
local state = redis.call('HGET', KEYS[1], 'state')
local latest = redis.call('HGET', KEYS[1], 'latest_seq') or '0'
local terminal = redis.call('HGET', KEYS[1], 'terminal_seq') or ''
local device = redis.call('HGET', KEYS[1], 'device_id') or ''
local session = redis.call('HGET', KEYS[1], 'session_id') or ''
local entries = redis.call('XRANGE', KEYS[2], ARGV[2], '+', 'COUNT', ARGV[3])
local out = {state, latest, terminal, device, session}
for i = 1, #entries do
  out[#out + 1] = entries[i][2][2]
end
return out
"""


class RedisClientDuck(Protocol):
    """redis.asyncio surface the repository relies on (all writes are Lua)."""

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...

    async def xread(
        self, streams: dict, count: int | None = None, block: int | None = None
    ) -> Any: ...

    async def delete(self, *names: str) -> int: ...


class RedisRunRepository:
    """Redis-backed implementation with atomic Lua writes and snapshots."""

    def __init__(
        self,
        client: RedisClientDuck,
        *,
        prefix: str = "gcmw:run:",
        max_events: int = DEFAULT_MAX_EVENTS_PER_RUN,
        ttl_s: int | None = DEFAULT_RUN_TTL_S,
        snapshot_limit: int = DEFAULT_SNAPSHOT_LIMIT,
    ) -> None:
        _validate_config(max_events, ttl_s)
        self._client = client
        self._prefix = prefix
        self._max_events = max_events
        self._ttl_s = ttl_s
        self._snapshot_limit = snapshot_limit

    # -- helpers ---------------------------------------------------------------

    def keys(self, run_id: str) -> tuple[str, str]:
        return (f"{self._prefix}{run_id}:state", f"{self._prefix}{run_id}:events")

    @staticmethod
    def _arg(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)

    @staticmethod
    def _is_redis_error(exc: Exception) -> str | None:
        message = str(exc)
        for marker in ("NOT_FOUND", "ORPHAN_STREAM", "ORPHAN_STATE"):
            if marker in message:
                return marker
        if "WRONGTYPE" in message:
            return "WRONGTYPE"
        return None

    def _fault_for(self, exc: Exception) -> RunRepositoryError:
        marker = self._is_redis_error(exc)
        if marker == "NOT_FOUND":
            return RunRepositoryError(RunRepositoryFault.NOT_FOUND, str(exc))
        if marker in {"ORPHAN_STREAM", "ORPHAN_STATE", "WRONGTYPE"}:
            return RunRepositoryError(
                RunRepositoryFault.INVARIANT, f"store invariant: {marker}"
            )
        return RunRepositoryError(
            RunRepositoryFault.UNAVAILABLE, f"redis failure: {exc}"
        )

    # -- lifecycle -------------------------------------------------------------

    async def create(self, identity: RunIdentity) -> int:
        state_key, stream_key = self.keys(identity.run_id)
        event = build_event(
            identity,
            1,
            SSEEventType.RUN_ACCEPTED,
            {"status": "accepted", "message": "问题已接收"},
        )
        try:
            outcome = await self._client.eval(
                _LUA_CREATE,
                2,
                state_key,
                stream_key,
                identity.tenant_id,
                identity.device_id,
                identity.session_id,
                str(self._ttl_s or 0),
                str(self._max_events),
                event.model_dump_json(),
                "1-0",
            )
        except Exception as exc:
            raise self._fault_for(exc) from exc
        if int(self._arg(outcome)) != 1:
            raise RunRepositoryError(
                RunRepositoryFault.CAS_CONFLICT,
                f"run {identity.run_id!r} already exists (or orphan key present)",
            )
        return 1

    async def commit_transition(
        self,
        identity: RunIdentity,
        *,
        expected_state: RunState,
        next_state: RunState,
        data: dict[str, Any] | None = None,
    ) -> int:
        if not is_allowed_transition(expected_state, next_state):
            raise RunRepositoryError(
                RunRepositoryFault.ILLEGAL_TRANSITION,
                f"illegal transition {expected_state.value} -> {next_state.value}",
            )
        return await self._write(
            identity,
            expected_state=expected_state,
            next_state=next_state,
            event_type=SSEEventType(transition_event_type(next_state)),
            data=data,
        )

    async def append_event(
        self,
        identity: RunIdentity,
        *,
        event_type: SSEEventType,
        data: dict[str, Any] | None = None,
    ) -> int:
        if event_type not in STATELESS_EVENT_TYPES:
            raise RunRepositoryError(
                RunRepositoryFault.INVARIANT,
                f"{event_type.value} is not a state-preserving event",
            )
        return await self._write(
            identity,
            expected_state=None,
            next_state=None,
            event_type=event_type,
            data=data,
        )

    async def _write(
        self,
        identity: RunIdentity,
        *,
        expected_state: RunState | None,
        next_state: RunState | None,
        event_type: SSEEventType,
        data: dict[str, Any] | None,
    ) -> int:
        """Bounded retry loop: read persisted identity+seq atomically, build and
        validate the event, then commit with CAS on that exact seq."""
        state_key, stream_key = self.keys(identity.run_id)
        for _ in range(MAX_COMMIT_RETRIES):
            page = await self._read_atomic(identity, cursor=0, limit=1)
            stored_identity = RunIdentity(
                run_id=identity.run_id,
                tenant_id=identity.tenant_id,  # tenant already verified in Lua
                device_id=page.device_id,
                session_id=page.session_id,
            )
            if is_terminal_state(page.state):
                raise RunRepositoryError(
                    RunRepositoryFault.INVARIANT,
                    f"run {identity.run_id!r} is already terminal",
                )
            seq = page.latest_seq + 1
            event = build_event(stored_identity, seq, event_type, data)
            terminal = "1" if (next_state and is_terminal_state(next_state)) else "0"
            script = _LUA_COMMIT if next_state is not None else _LUA_APPEND
            if next_state is not None:
                args = (
                    identity.tenant_id,
                    page.device_id,
                    page.session_id,
                    expected_state.value,
                    str(page.latest_seq),
                    next_state.value,
                    event.model_dump_json(),
                    terminal,
                    str(self._max_events),
                    str(self._ttl_s or 0),
                )
            else:
                args = (
                    identity.tenant_id,
                    page.device_id,
                    page.session_id,
                    str(page.latest_seq),
                    event.model_dump_json(),
                    "0",
                    str(self._max_events),
                    str(self._ttl_s or 0),
                )
            try:
                outcome = await self._client.eval(
                    script, 2, state_key, stream_key, *args
                )
            except Exception as exc:
                raise self._fault_for(exc) from exc
            code = int(self._arg(outcome))
            if code > 0:
                return code
            if code == 1:
                continue  # seq moved under us: retry with fresh state
            if code in (-1, -2, -4):
                raise RunRepositoryError(
                    RunRepositoryFault.NOT_FOUND,
                    f"run {identity.run_id!r} not found for this tenant",
                )
            if code == -3:
                raise RunRepositoryError(
                    RunRepositoryFault.INVARIANT,
                    f"run {identity.run_id!r} is already terminal",
                )
            if code == -5:
                raise RunRepositoryError(
                    RunRepositoryFault.INVARIANT,
                    f"run {identity.run_id!r} has a state key without its stream",
                )
            raise RunRepositoryError(
                RunRepositoryFault.CAS_CONFLICT,
                f"run {identity.run_id!r}: expected {expected_state}",
            )
        raise RunRepositoryError(
            RunRepositoryFault.CONCURRENT_MODIFICATION,
            f"run {identity.run_id!r}: too many concurrent modifications",
        )

    async def state(self, identity: RunIdentity) -> RunState:
        page = await self._read_atomic(identity, cursor=0, limit=1)
        return page.state

    async def delete(self, identity: RunIdentity) -> None:
        state_key, stream_key = self.keys(identity.run_id)
        try:
            outcome = await self._client.eval(
                _LUA_DELETE, 2, state_key, stream_key, identity.tenant_id
            )
        except Exception as exc:
            raise self._fault_for(exc) from exc
        code = int(self._arg(outcome))
        if code == -1:
            raise RunRepositoryError(
                RunRepositoryFault.NOT_FOUND, f"run {identity.run_id!r} not found"
            )
        if code == -2:
            raise RunRepositoryError(
                RunRepositoryFault.NOT_FOUND,
                f"run {identity.run_id!r} not found for this tenant",
            )

    # -- reads ------------------------------------------------------------------

    @dataclass(frozen=True)
    class _Page:
        state: RunState
        latest_seq: int
        terminal_seq: int | None
        device_id: str
        session_id: str
        events: tuple[SSEEvent, ...]
        oldest_seq: int

    async def _read_atomic(
        self, identity: RunIdentity, *, cursor: int, limit: int
    ) -> RedisRunRepository._Page:
        state_key, stream_key = self.keys(identity.run_id)
        start = "-" if cursor <= 0 else f"({cursor}-0"
        try:
            raw = await self._client.eval(
                _LUA_SNAPSHOT,
                2,
                state_key,
                stream_key,
                identity.tenant_id,
                start,
                str(limit),
            )
        except Exception as exc:
            raise self._fault_for(exc) from exc
        values = [self._arg(v) for v in raw]
        state = RunState(values[0])
        latest = int(values[1])
        terminal = int(values[2]) if values[2] else None
        # identity travels INSIDE the same atomic snapshot (no second read)
        device_id, session_id = values[3], values[4]
        events = tuple(
            SSEEvent.model_validate(json.loads(payload)) for payload in values[5:]
        )
        oldest = events[0].seq if events else (cursor if latest == cursor else 0)
        return self._Page(
            state=state,
            latest_seq=latest,
            terminal_seq=terminal,
            device_id=device_id,
            session_id=session_id,
            events=events,
            oldest_seq=oldest,
        )

    async def snapshot(
        self, identity: RunIdentity, cursor: int, timeout_s: float
    ) -> StreamSnapshot:
        page = await self._read_atomic(
            identity, cursor=cursor, limit=self._snapshot_limit
        )
        if page.events or is_terminal_state(page.state):
            return self._to_snapshot(page, cursor, timed_out=False)
        if page.latest_seq > cursor:
            # the stream lost entries the state still counts: invariant break
            raise RunRepositoryError(
                RunRepositoryFault.INVARIANT,
                f"run {identity.run_id!r}: latest_seq {page.latest_seq} exceeds "
                f"visible events at cursor {cursor}",
            )
        block_ms = max(int(timeout_s * 1000), 1)
        _, stream_key = self.keys(identity.run_id)
        try:
            # async XREAD BLOCK: no event-loop blocking, cancellable by the caller
            await self._client.xread(
                {stream_key: f"{cursor}-0"}, count=1, block=block_ms
            )
        except Exception as exc:
            raise self._fault_for(exc) from exc
        page = await self._read_atomic(
            identity, cursor=cursor, limit=self._snapshot_limit
        )
        if page.events or is_terminal_state(page.state):
            return self._to_snapshot(page, cursor, timed_out=False)
        return self._to_snapshot(page, cursor, timed_out=True)

    def _to_snapshot(
        self, page: RedisRunRepository._Page, cursor: int, *, timed_out: bool
    ) -> StreamSnapshot:
        return _snapshot_from(
            state=page.state,
            events=() if timed_out else page.events,
            oldest=page.oldest_seq,
            latest=page.latest_seq,
            terminal_seq=page.terminal_seq,
            timed_out=timed_out,
        )


__all__ = [
    "DEFAULT_BLOCK_MS",
    "DEFAULT_MAX_EVENTS_PER_RUN",
    "DEFAULT_RUN_TTL_S",
    "MAX_COMMIT_RETRIES",
    "STATELESS_EVENT_TYPES",
    "MemoryRunRepository",
    "RedisClientDuck",
    "RedisRunRepository",
    "RunIdentity",
    "RunRepositoryError",
    "RunRepositoryFault",
    "build_event",
]
