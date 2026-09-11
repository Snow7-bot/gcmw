"""RunRepository — the single durable authority for run state + events (#65B-2 A).

Reviewer-driven design (round 3):

- **one persistence authority**: run state AND its event stream are owned here;
  the legacy ``event_store`` module is deprecated for run events;
- **tenant + device + session bound**: Redis keys are scoped by tenant and run
  inside one hash tag (``{gcmw:run:<tenant>:<run_id>}``) so both keys land in
  the same cluster slot; Memory keys on ``(tenant_id, run_id)``; and every
  write/read/delete compares the CALLER's tenant/device/session against the
  stored record (a mismatch is "not found", never data);
- **two atomic writes with an explicit state whitelist**:
  * ``commit_transition`` — legal transitions only (rules reused from
    ``RunStateMachine``), event type + layer derived from the target state;
  * ``append_event`` — state-preserving events restricted per state:
    ``answer.delta``/``answer.completed`` only while STREAMING,
    ``evidence.found`` during retrieval/draft/verify/stream, ``reflection.result``
    while VERIFYING, ``mic_status`` at the edges. ``heartbeat`` is NEVER
    persisted (B-1 defines it as a sequence-free comment frame), and
    ``answer.completed`` is single-shot — no delta afterwards;
- **business seq == physical stream id** (``<seq>-0``), so resume uses the
  business cursor directly;
- **atomic snapshot with full invariant validation**: one Lua read returns
  state fields, retained-oldest, newest id, terminal-event validity and the
  page; Python then verifies stream-id↔seq, event identity vs the stored
  record, newest id vs ``latest_seq`` and the terminal event, and reports
  violations as explicit invariant faults;
- **bounded waits and operations**: blocking reads use async ``XREAD BLOCK``
  (``redis.asyncio``) wrapped in a repository-level timeout; a cursor beyond
  ``latest_seq`` returns immediately so the engine can classify ``cursor_ahead``;
- **retry-safe conflict codes**: Lua control codes are negative and never
  collide with a real (positive) sequence number; a seq that moved under us
  retries, state mismatch is a CAS conflict;
- **Memory parity**: expired records are purged before ``create``, and both
  writes and reads use deep copies so callers can never mutate stored history.
"""

from __future__ import annotations

import asyncio
import json
import math
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
DEFAULT_OP_TIMEOUT_S = 5.0
MAX_COMMIT_RETRIES = 5

#: Lua control codes (negative so they can never look like a real seq)
_CODE_NOT_FOUND = -1
_CODE_TENANT_MISMATCH = -2
_CODE_TERMINAL = -3
_CODE_IDENTITY_MISMATCH = -4
_CODE_ORPHAN_STATE = -5
_CODE_SEQ_MOVED = -6  # retry with a fresh snapshot
_CODE_STATE_MISMATCH = -7  # CAS conflict
_CODE_STATE_NOT_ALLOWED = -8  # event/state whitelist or answer already sealed

#: state-preserving events and the states they may be written in.
#: ``heartbeat`` is intentionally absent: B-1 defines it as a comment frame
#: without a sequence number, so it must never occupy a business seq.
STATELESS_EVENT_STATES: dict[SSEEventType, frozenset[RunState]] = {
    SSEEventType.ANSWER_DELTA: frozenset({RunState.STREAMING}),
    SSEEventType.ANSWER_COMPLETED: frozenset({RunState.STREAMING}),
    SSEEventType.EVIDENCE_FOUND: frozenset(
        {RunState.RETRIEVING, RunState.DRAFTING, RunState.VERIFYING, RunState.STREAMING}
    ),
    SSEEventType.REFLECTION_RESULT: frozenset({RunState.VERIFYING}),
    SSEEventType.MIC_STATUS: frozenset(
        {RunState.ACCEPTED, RunState.GUARDING, RunState.STREAMING}
    ),
}

#: events that seal the answer: no further delta may follow them
SEALING_EVENT_TYPES = frozenset({SSEEventType.ANSWER_COMPLETED})


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


def _validate_config(
    max_events: int, ttl_s: int | None, op_timeout_s: float | None = None
) -> None:
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
    if op_timeout_s is not None and (
        isinstance(op_timeout_s, bool)
        or not isinstance(op_timeout_s, (int, float))
        or not math.isfinite(op_timeout_s)
        or op_timeout_s <= 0
    ):
        raise ValueError(
            f"op_timeout_s must be a positive number, got {op_timeout_s!r}"
        )


def _layer_for(event_type: SSEEventType) -> EventLayer:
    """Derive the SSE layer from the event type (single authority)."""
    if event_type in {SSEEventType.ANSWER_DELTA, SSEEventType.ANSWER_COMPLETED}:
        return EventLayer.ANSWER
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
    payload = json.loads(json.dumps(data or {}))  # detach from caller objects
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


def _require_stateless_support(
    event_type: SSEEventType, state: RunState, *, answer_sealed: bool
) -> None:
    """Python-side safety gate (the Lua script re-checks the same rules)."""
    if event_type not in STATELESS_EVENT_STATES:
        if event_type is SSEEventType.HEARTBEAT:
            raise RunRepositoryError(
                RunRepositoryFault.INVARIANT,
                "heartbeat is a comment frame and must never occupy a seq",
            )
        raise RunRepositoryError(
            RunRepositoryFault.INVARIANT,
            f"{event_type.value} is not a state-preserving event",
        )
    allowed_states = STATELESS_EVENT_STATES[event_type]
    if state not in allowed_states:
        raise RunRepositoryError(
            RunRepositoryFault.INVARIANT,
            f"{event_type.value} is not allowed while {state.value} "
            f"(allowed: {sorted(s.value for s in allowed_states)})",
        )
    if answer_sealed:
        raise RunRepositoryError(
            RunRepositoryFault.INVARIANT,
            "the answer is already completed — no further answer events",
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
    answer_sealed: bool = False
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
        # tenant-scoped keys: (tenant_id, run_id)
        self._records: dict[tuple[str, str], _Record] = {}
        self._lock = asyncio.Lock()

    # -- lifecycle -------------------------------------------------------------

    async def create(self, identity: RunIdentity) -> int:
        async with self._lock:
            self._purge_expired(identity)  # expired ids may be recreated
            if self._key(identity) in self._records:
                raise RunRepositoryError(
                    RunRepositoryFault.CAS_CONFLICT,
                    f"run {identity.run_id!r} already exists",
                )
            record = _Record(
                identity=identity,
                state=RunState.ACCEPTED,
                expires_at=self._deadline(),
            )
            record.events.append(
                build_event(
                    identity,
                    1,
                    SSEEventType.RUN_ACCEPTED,
                    {"status": "accepted", "message": "问题已接收"},
                ).model_copy(deep=True)
            )
            self._records[self._key(identity)] = record
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
            if is_terminal_state(record.state):
                raise RunRepositoryError(
                    RunRepositoryFault.INVARIANT,
                    f"run {identity.run_id!r} is already terminal",
                )
            if not is_allowed_transition(expected_state, next_state):
                raise RunRepositoryError(
                    RunRepositoryFault.ILLEGAL_TRANSITION,
                    f"illegal transition {expected_state.value} -> {next_state.value}",
                )
            event_type = SSEEventType(transition_event_type(next_state))
            seq = record.latest_seq + 1
            event = build_event(record.identity, seq, event_type, data)
            record.events.append(event.model_copy(deep=True))
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
        """State-preserving append; the event/state whitelist is enforced."""
        async with self._lock:
            record = self._require(identity)
            if is_terminal_state(record.state):
                raise RunRepositoryError(
                    RunRepositoryFault.INVARIANT,
                    f"run {identity.run_id!r} is terminal",
                )
            _require_stateless_support(
                event_type, record.state, answer_sealed=record.answer_sealed
            )
            seq = record.latest_seq + 1
            event = build_event(record.identity, seq, event_type, data)
            record.events.append(event.model_copy(deep=True))
            if event_type in SEALING_EVENT_TYPES:
                record.answer_sealed = True
            self._trim(record)
            self._refresh_ttl(record)
            record.waiters.set()
            return seq

    async def state(self, identity: RunIdentity) -> RunState:
        async with self._lock:
            return self._require(identity).state

    async def delete(self, identity: RunIdentity) -> None:
        """Tenant+device+session authenticated delete."""
        async with self._lock:
            self._require(identity)
            self._records.pop(self._key(identity), None)

    # -- engine read interface --------------------------------------------------

    async def snapshot(
        self, identity: RunIdentity, cursor: int, timeout_s: float
    ) -> StreamSnapshot:
        async with self._lock:
            record = self._require(identity)
            if record.latest_seq > cursor or is_terminal_state(record.state):
                return self._page(record, cursor, timed_out=False)
            if cursor > record.latest_seq:
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
        events = (
            ()
            if timed_out
            else tuple(e.model_copy(deep=True) for e in record.events if e.seq > cursor)
        )
        return _snapshot_from(
            state=record.state,
            events=events,
            oldest=record.events[0].seq if record.events else 0,
            latest=record.latest_seq,
            terminal_seq=record.terminal_seq,
            timed_out=timed_out,
        )

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _key(identity: RunIdentity) -> tuple[str, str]:
        return (identity.tenant_id, identity.run_id)

    def _deadline(self) -> float | None:
        return None if self._ttl_s is None else self._monotonic() + self._ttl_s

    def _expired(self, record: _Record) -> bool:
        return record.expires_at is not None and self._monotonic() >= record.expires_at

    def _purge_expired(self, identity: RunIdentity) -> None:
        key = self._key(identity)
        record = self._records.get(key)
        if record is not None and self._expired(record):
            del self._records[key]

    def _trim(self, record: _Record) -> None:
        while len(record.events) > self._max_events:
            record.events.pop(0)

    def _refresh_ttl(self, record: _Record) -> None:
        record.expires_at = self._deadline()

    def _require(self, identity: RunIdentity) -> _Record:
        record = self._records.get(self._key(identity))
        if record is None:
            raise RunRepositoryError(
                RunRepositoryFault.NOT_FOUND, f"run {identity.run_id!r} not found"
            )
        if (
            record.identity.tenant_id != identity.tenant_id
            or record.identity.device_id != identity.device_id
            or record.identity.session_id != identity.session_id
        ):
            # foreign/mismatched identity: absent, never data
            raise RunRepositoryError(
                RunRepositoryFault.NOT_FOUND,
                f"run {identity.run_id!r} not found for this principal",
            )
        if self._expired(record):
            del self._records[self._key(identity)]
            raise RunRepositoryError(
                RunRepositoryFault.NOT_FOUND, f"run {identity.run_id!r} expired"
            )
        return record


# ---------------------------------------------------------------------------
# Redis implementation (redis.asyncio; tenant-scoped keys, Lua-only writes)
# ---------------------------------------------------------------------------

_LUA_CREATE = """
if redis.call('EXISTS', KEYS[1]) == 1 or redis.call('EXISTS', KEYS[2]) == 1 then
  return 0
end
redis.call('HSET', KEYS[1],
  'state', 'ACCEPTED', 'tenant_id', ARGV[1], 'device_id', ARGV[2],
  'session_id', ARGV[3], 'latest_seq', '1', 'answer_sealed', '0')
redis.call('XADD', KEYS[2], 'MAXLEN', '=', ARGV[5], '1-0', 'event', ARGV[6])
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
if redis.call('HGET', KEYS[1], 'state') ~= ARGV[4] then return -7 end
local latest = tonumber(redis.call('HGET', KEYS[1], 'latest_seq') or '0')
if latest ~= tonumber(ARGV[5]) then return -6 end
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
if redis.call('HGET', KEYS[1], 'answer_sealed') == '1' then return -8 end
local state = redis.call('HGET', KEYS[1], 'state')
local allowed = false
for token in string.gmatch(ARGV[8], '[^,]+') do
  if token == state then allowed = true end
end
if not allowed then return -8 end
local latest = tonumber(redis.call('HGET', KEYS[1], 'latest_seq') or '0')
if latest ~= tonumber(ARGV[4]) then return -6 end
local seq = latest + 1
redis.call('XADD', KEYS[2], 'MAXLEN', '=', ARGV[7], tostring(seq) .. '-0',
  'event', ARGV[5])
redis.call('HSET', KEYS[1], 'latest_seq', tostring(seq))
if ARGV[9] == '1' then
  redis.call('HSET', KEYS[1], 'answer_sealed', '1')
end
if tonumber(ARGV[10]) > 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[10])
  redis.call('EXPIRE', KEYS[2], ARGV[10])
end
return seq
"""

_LUA_DELETE = """
if redis.call('EXISTS', KEYS[1]) == 0 and redis.call('EXISTS', KEYS[2]) == 0 then
  return -1
end
if redis.call('EXISTS', KEYS[1]) == 1 then
  if redis.call('HGET', KEYS[1], 'tenant_id') ~= ARGV[1] then return -2 end
  if redis.call('HGET', KEYS[1], 'device_id') ~= ARGV[2] then return -2 end
  if redis.call('HGET', KEYS[1], 'session_id') ~= ARGV[3] then return -2 end
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
if redis.call('HGET', KEYS[1], 'tenant_id') ~= ARGV[1]
   or redis.call('HGET', KEYS[1], 'device_id') ~= ARGV[2]
   or redis.call('HGET', KEYS[1], 'session_id') ~= ARGV[3] then
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
local oldest = ''
local first = redis.call('XRANGE', KEYS[2], '-', '+', 'COUNT', 1)
if first[1] then oldest = first[1][1] end
local newest = ''
local last = redis.call('XREVRANGE', KEYS[2], '+', '-', 'COUNT', 1)
if last[1] then newest = last[1][1] end
local terminal_ok = '0'
if terminal ~= '' then
  local entry = redis.call('XRANGE', KEYS[2], terminal .. '-0', terminal .. '-0',
    'COUNT', 1)
  if entry[1] then
    local fields = entry[1][2]
    for i = 1, #fields - 1, 2 do
      if fields[i] == 'event' then
        local payload = cjson.decode(fields[i + 1])
        if payload['event'] == 'run.completed' then terminal_ok = '1' end
      end
    end
  end
end
local out = {state, latest, terminal, device, session, oldest, newest, terminal_ok}
local entries = redis.call('XRANGE', KEYS[2], ARGV[4], '+', 'COUNT', ARGV[5])
for i = 1, #entries do
  out[#out + 1] = entries[i][1]
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


@dataclass(frozen=True)
class _Page:
    state: RunState
    latest_seq: int
    terminal_seq: int | None
    device_id: str
    session_id: str
    oldest_seq: int
    events: tuple[SSEEvent, ...]


class RedisRunRepository:
    """Redis-backed implementation: tenant-scoped keys, Lua-only atomic writes."""

    def __init__(
        self,
        client: RedisClientDuck,
        *,
        prefix: str = "gcmw:run",
        max_events: int = DEFAULT_MAX_EVENTS_PER_RUN,
        ttl_s: int | None = DEFAULT_RUN_TTL_S,
        snapshot_limit: int = DEFAULT_SNAPSHOT_LIMIT,
        op_timeout_s: float = DEFAULT_OP_TIMEOUT_S,
    ) -> None:
        _validate_config(max_events, ttl_s, op_timeout_s)
        self._client = client
        self._prefix = prefix
        self._max_events = max_events
        self._ttl_s = ttl_s
        self._snapshot_limit = snapshot_limit
        self._op_timeout_s = float(op_timeout_s)

    # -- helpers ---------------------------------------------------------------

    def keys(self, identity: RunIdentity) -> tuple[str, str]:
        """Tenant-scoped keys inside ONE hash tag (same cluster slot)."""
        tag = f"{self._prefix}:{identity.tenant_id}:{identity.run_id}"
        return (f"{{{tag}}}:state", f"{{{tag}}}:events")

    @staticmethod
    def _arg(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)

    async def _call(self, awaitable: Any, what: str) -> Any:
        try:
            async with asyncio.timeout(self._op_timeout_s):
                return await awaitable
        except TimeoutError as exc:
            raise RunRepositoryError(
                RunRepositoryFault.UNAVAILABLE, f"redis {what} timed out"
            ) from exc
        except Exception as exc:
            raise self._fault_for(exc) from exc

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
        state_key, stream_key = self.keys(identity)
        event = build_event(
            identity,
            1,
            SSEEventType.RUN_ACCEPTED,
            {"status": "accepted", "message": "问题已接收"},
        )
        outcome = await self._call(
            self._client.eval(
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
            ),
            "create",
        )
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
        state_key, stream_key = self.keys(identity)
        for _ in range(MAX_COMMIT_RETRIES):
            page = await self._read_atomic(identity, cursor=0, limit=1)
            if is_terminal_state(page.state):
                raise RunRepositoryError(
                    RunRepositoryFault.INVARIANT,
                    f"run {identity.run_id!r} is already terminal",
                )
            seq = page.latest_seq + 1
            event = build_event(
                RunIdentity(
                    run_id=identity.run_id,
                    tenant_id=identity.tenant_id,
                    device_id=page.device_id,
                    session_id=page.session_id,
                ),
                seq,
                SSEEventType(transition_event_type(next_state)),
                data,
            )
            terminal = "1" if is_terminal_state(next_state) else "0"
            code = await self._eval_code(
                _LUA_COMMIT,
                (state_key, stream_key),
                (
                    identity.tenant_id,
                    identity.device_id,
                    identity.session_id,
                    expected_state.value,
                    str(page.latest_seq),
                    next_state.value,
                    event.model_dump_json(),
                    terminal,
                    str(self._max_events),
                    str(self._ttl_s or 0),
                ),
                what="commit",
            )
            if code > 0:
                return code
            if code == _CODE_SEQ_MOVED:
                continue  # another writer advanced: retry with a fresh read
            raise self._control_fault(code, expected_state)
        raise RunRepositoryError(
            RunRepositoryFault.CONCURRENT_MODIFICATION,
            f"run {identity.run_id!r}: too many concurrent modifications",
        )

    async def append_event(
        self,
        identity: RunIdentity,
        *,
        event_type: SSEEventType,
        data: dict[str, Any] | None = None,
    ) -> int:
        if event_type not in STATELESS_EVENT_STATES:
            if event_type is SSEEventType.HEARTBEAT:
                raise RunRepositoryError(
                    RunRepositoryFault.INVARIANT,
                    "heartbeat is a comment frame and must never occupy a seq",
                )
            raise RunRepositoryError(
                RunRepositoryFault.INVARIANT,
                f"{event_type.value} is not a state-preserving event",
            )
        state_key, stream_key = self.keys(identity)
        for _ in range(MAX_COMMIT_RETRIES):
            page = await self._read_atomic(identity, cursor=0, limit=1)
            if is_terminal_state(page.state):
                raise RunRepositoryError(
                    RunRepositoryFault.INVARIANT,
                    f"run {identity.run_id!r} is terminal",
                )
            allowed = STATELESS_EVENT_STATES[event_type]
            if page.state not in allowed:
                raise RunRepositoryError(
                    RunRepositoryFault.INVARIANT,
                    f"{event_type.value} is not allowed while {page.state.value}",
                )
            seq = page.latest_seq + 1
            event = build_event(
                RunIdentity(
                    run_id=identity.run_id,
                    tenant_id=identity.tenant_id,
                    device_id=page.device_id,
                    session_id=page.session_id,
                ),
                seq,
                event_type,
                data,
            )
            sealing = "1" if event_type in SEALING_EVENT_TYPES else "0"
            code = await self._eval_code(
                _LUA_APPEND,
                (state_key, stream_key),
                (
                    identity.tenant_id,
                    identity.device_id,
                    identity.session_id,
                    str(page.latest_seq),
                    event.model_dump_json(),
                    "0",
                    str(self._max_events),
                    ",".join(sorted(s.value for s in allowed)),
                    sealing,
                    str(self._ttl_s or 0),
                ),
                what="append",
            )
            if code > 0:
                return code
            if code == _CODE_SEQ_MOVED:
                continue
            raise self._control_fault(code, page.state)
        raise RunRepositoryError(
            RunRepositoryFault.CONCURRENT_MODIFICATION,
            f"run {identity.run_id!r}: too many concurrent modifications",
        )

    async def state(self, identity: RunIdentity) -> RunState:
        page = await self._read_atomic(identity, cursor=0, limit=1)
        return page.state

    async def delete(self, identity: RunIdentity) -> None:
        state_key, stream_key = self.keys(identity)
        code = await self._eval_code(
            _LUA_DELETE,
            (state_key, stream_key),
            (identity.tenant_id, identity.device_id, identity.session_id),
            what="delete",
        )
        if code == 1:
            return
        if code in (_CODE_NOT_FOUND, _CODE_TENANT_MISMATCH):
            raise RunRepositoryError(
                RunRepositoryFault.NOT_FOUND,
                f"run {identity.run_id!r} not found for this principal",
            )
        raise self._control_fault(code, None)

    # -- reads ------------------------------------------------------------------

    async def _eval_code(
        self, script: str, keys: tuple[str, str], args: tuple[Any, ...], *, what: str
    ) -> int:
        raw = await self._call(
            self._client.eval(script, 2, keys[0], keys[1], *args), what
        )
        return int(self._arg(raw))

    @staticmethod
    def _control_fault(code: int, expected: RunState | None) -> RunRepositoryError:
        if code in (_CODE_NOT_FOUND, _CODE_TENANT_MISMATCH, _CODE_IDENTITY_MISMATCH):
            return RunRepositoryError(
                RunRepositoryFault.NOT_FOUND, "run not found for this principal"
            )
        if code == _CODE_TERMINAL:
            return RunRepositoryError(
                RunRepositoryFault.INVARIANT, "run is already terminal"
            )
        if code == _CODE_ORPHAN_STATE:
            return RunRepositoryError(
                RunRepositoryFault.INVARIANT, "state key without its stream"
            )
        if code == _CODE_STATE_NOT_ALLOWED:
            return RunRepositoryError(
                RunRepositoryFault.INVARIANT,
                "event not allowed in the current state (or answer sealed)",
            )
        if code == _CODE_STATE_MISMATCH:
            return RunRepositoryError(
                RunRepositoryFault.CAS_CONFLICT,
                f"expected {expected.value if expected else '?'}",
            )
        return RunRepositoryError(
            RunRepositoryFault.INVARIANT, f"unexpected repository code {code}"
        )

    async def _read_atomic(
        self, identity: RunIdentity, *, cursor: int, limit: int
    ) -> _Page:
        """One atomic Lua read + full invariant validation of the result."""
        state_key, stream_key = self.keys(identity)
        start = "-" if cursor <= 0 else f"({cursor}-0"
        raw = await self._call(
            self._client.eval(
                _LUA_SNAPSHOT,
                2,
                state_key,
                stream_key,
                identity.tenant_id,
                identity.device_id,
                identity.session_id,
                start,
                str(limit),
            ),
            "snapshot",
        )
        values = [self._arg(v) for v in raw]
        state = RunState(values[0])
        latest = int(values[1])
        terminal = int(values[2]) if values[2] else None
        device_id, session_id = values[3], values[4]
        if device_id != identity.device_id or session_id != identity.session_id:
            raise RunRepositoryError(
                RunRepositoryFault.NOT_FOUND,
                f"run {identity.run_id!r} not found for this principal",
            )
        retained_oldest = int(values[5].split("-")[0]) if values[5] else 0
        newest_id = values[6]
        terminal_ok = values[7] == "1"
        if newest_id:
            newest_seq = int(newest_id.split("-")[0])
            if newest_seq != latest:
                raise RunRepositoryError(
                    RunRepositoryFault.INVARIANT,
                    f"stream newest id {newest_seq} != hash latest_seq {latest}",
                )
        if terminal is not None and not terminal_ok:
            raise RunRepositoryError(
                RunRepositoryFault.INVARIANT,
                f"terminal_seq {terminal} does not hold run.completed",
            )
        events: list[SSEEvent] = []
        pairs = values[8:]
        for index in range(0, len(pairs) - 1, 2):
            stream_id, payload = pairs[index], pairs[index + 1]
            event = SSEEvent.model_validate(json.loads(payload))
            id_seq = int(stream_id.split("-")[0])
            if id_seq != event.seq:
                raise RunRepositoryError(
                    RunRepositoryFault.INVARIANT,
                    f"stream id {id_seq} != event seq {event.seq}",
                )
            if (
                event.tenant_id != identity.tenant_id
                or event.device_id != device_id
                or event.session_id != session_id
                or event.run_id != identity.run_id
            ):
                raise RunRepositoryError(
                    RunRepositoryFault.INVARIANT,
                    f"event {event.seq} identity does not match the run record",
                )
            events.append(event)
        return _Page(
            state=state,
            latest_seq=latest,
            terminal_seq=terminal,
            device_id=device_id,
            session_id=session_id,
            oldest_seq=retained_oldest,
            events=tuple(events),
        )

    async def snapshot(
        self, identity: RunIdentity, cursor: int, timeout_s: float
    ) -> StreamSnapshot:
        page = await self._read_atomic(
            identity, cursor=cursor, limit=self._snapshot_limit
        )
        if page.events or is_terminal_state(page.state) or cursor > page.latest_seq:
            # cursor beyond the newest seq: hand it straight to the engine so it
            # can classify cursor_ahead — never wait a heartbeat for it
            return self._to_snapshot(page, timed_out=False)
        if page.latest_seq > cursor:
            raise RunRepositoryError(
                RunRepositoryFault.INVARIANT,
                f"run {identity.run_id!r}: latest_seq {page.latest_seq} exceeds "
                f"visible events at cursor {cursor}",
            )
        block_ms = max(int(timeout_s * 1000), 1)
        _, stream_key = self.keys(identity)
        await self._call(
            self._client.xread({stream_key: f"{cursor}-0"}, count=1, block=block_ms),
            "xread",
        )
        page = await self._read_atomic(
            identity, cursor=cursor, limit=self._snapshot_limit
        )
        if page.events or is_terminal_state(page.state):
            return self._to_snapshot(page, timed_out=False)
        return self._to_snapshot(page, timed_out=True)

    def _to_snapshot(self, page: _Page, *, timed_out: bool) -> StreamSnapshot:
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
    "DEFAULT_OP_TIMEOUT_S",
    "DEFAULT_RUN_TTL_S",
    "MAX_COMMIT_RETRIES",
    "SEALING_EVENT_TYPES",
    "STATELESS_EVENT_STATES",
    "MemoryRunRepository",
    "RedisClientDuck",
    "RedisRunRepository",
    "RunIdentity",
    "RunRepositoryError",
    "RunRepositoryFault",
    "build_event",
]
