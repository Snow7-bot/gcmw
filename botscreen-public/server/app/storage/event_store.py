"""Event storage for run SSE events (issue #36b, remediation round).

Reviewer-driven constraints (2026-09 round):
- run-level TTL only — events are NEVER expired individually, so a stream can
  never develop sequence holes; the whole run key expires atomically;
- append is an atomic compare-and-append: "read last seq, then XADD" is not
  atomic and can duplicate seq under concurrency — Redis appends run through
  one Lua script (``XREVRANGE ... + - COUNT 1`` read of the newest entry
  inside the script, then conditional XADD + EXPIRE); the memory reference
  serializes the same invariant on a single lock;
- no ``KEYS`` — prefix listing uses SCAN semantics;
- a store outage raises ``EventStoreUnavailable`` explicitly; there is never a
  silent fallback to memory writes.

The run state machine (#36a) stays the single sequence authority for the
process-local machine; this module durably mirrors already-validated events
and refuses to persist a non-contiguous append (conflict). Wiring the mirror
into the lifecycle service only happens once state AND events can be persisted
atomically (see #65B) — until then the storage layer stands alone.
"""

from __future__ import annotations

import base64
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Protocol

from app.contracts.errors import ErrorCode

DEFAULT_MAX_EVENTS_PER_RUN = 10_000
DEFAULT_RUN_TTL_S = 1800  # V2.3 session idle window (30 min)

# Lua compare-and-append: atomically check the newest entry against the
# expected seq, XADD with an EXACT MAXLEN (hard capacity), refresh the
# run-level TTL. Returns 1 on success, 0 on seq conflict or on a malformed
# existing entry (fail closed — never treated as an empty stream). Errors
# propagate as Redis exceptions (mapped to EventStoreUnavailable by caller).
_LUA_APPEND = """
local entries = redis.call('XREVRANGE', KEYS[1], '+', '-', 'COUNT', 1)
local expected = tonumber(ARGV[1])
local last = 0
local have = false
if entries[1] then
  -- RESP2: an entry is {id, {f1, v1, f2, v2, ...}} — scan the nested pairs
  local fields = entries[1][2]
  for i = 1, #fields - 1, 2 do
    if fields[i] == 'seq' then
      last = tonumber(fields[i + 1])
      have = true
      break
    end
  end
  if not have then
    return 0  -- existing entry without seq: fail closed
  end
end
if last + 1 ~= expected then
  return 0
end
redis.call('XADD', KEYS[1], 'MAXLEN', '=', ARGV[2], '*', 'seq', ARGV[1], 'data', ARGV[3])
if tonumber(ARGV[4]) > 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[4])
end
return 1
"""
#: fingerprint used by fakes to dispatch eval() to a python twin
LUA_APPEND_SHA = "gcmw-caa-v1"


class EventStoreError(RuntimeError):
    """Storage failure carrying a stable ErrorCode (mapped by the #36 boundary)."""

    def __init__(self, code: ErrorCode, message: str = "") -> None:
        super().__init__(message or code.value)
        self.code = code


class EventStoreUnavailable(EventStoreError):
    """Redis (or its client) is unreachable — explicit error, no fallback."""

    def __init__(self, message: str = "") -> None:
        super().__init__(ErrorCode.UNAVAILABLE_OVERLOADED, message)


def _now() -> float:
    return time.monotonic()


def _validate_store_options(max_len: int, default_ttl_s: int | None) -> None:
    """Reject retention policy holes at construction time.

    - ``default_ttl_s`` must be None (no expiry — explicit test/dev choice) or
      a positive integer; 0/negative would silently mean "expire now" in
      Memory but "never expire" in Redis — the divergence is refused here;
    - ``max_len`` must be >= 1; negative values previously crashed Memory with
      a non-contract KeyError instead of a clean validation error.
    """
    if not isinstance(max_len, int) or isinstance(max_len, bool) or max_len < 1:
        raise ValueError(f"max_len must be a positive integer, got {max_len!r}")
    if default_ttl_s is not None and (
        not isinstance(default_ttl_s, int)
        or isinstance(default_ttl_s, bool)
        or default_ttl_s <= 0
    ):
        raise ValueError(
            f"default_ttl_s must be None or a positive integer, got {default_ttl_s!r}"
        )


_GLOB_META = ("\\", "*", "?", "[", "]")


def _glob_escape(text: str) -> str:
    """Escape Redis MATCH glob metacharacters so ``scan(prefix)`` matches the
    literal prefix, mirroring MemoryEventStore's ``startswith`` semantics."""
    return "".join("\\" + ch if ch in _GLOB_META else ch for ch in text)


class EventStore(Protocol):
    """Durable, seq-guarded append-only event log per key (run-level TTL)."""

    def append(self, key: str, seq: int, data: bytes) -> None: ...

    def read(
        self, key: str, after_seq: int | None = None, limit: int = 100
    ) -> tuple[tuple[int, bytes], ...]: ...

    def next_seq(self, key: str) -> int: ...

    def delete(self, key: str) -> None: ...

    def scan(self, prefix: str) -> list[str]: ...


class MemoryEventStore:
    """Deterministic reference store (tests / single-process development).

    Invariants mirror the Redis adapter: contiguous append under one lock,
    MAXLEN trimming of the oldest entries, whole-key TTL (never per-event
    expiry, so seq holes cannot form), SCAN-style prefix listing.
    """

    def __init__(
        self,
        *,
        max_len: int = DEFAULT_MAX_EVENTS_PER_RUN,
        default_ttl_s: int | None = DEFAULT_RUN_TTL_S,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        _validate_store_options(max_len, default_ttl_s)
        self._max_len = max_len
        self._default_ttl_s = default_ttl_s
        self._monotonic = monotonic or _now
        self._lock = threading.RLock()
        # key -> (expires_at_monotonic | None, OrderedDict[seq, bytes])
        self._streams: dict[str, tuple[float | None, OrderedDict[int, bytes]]] = {}

    def _deadline(self, ttl_s: int | None) -> float | None:
        if ttl_s is None:
            ttl_s = self._default_ttl_s
        if ttl_s is None:
            return None
        return self._monotonic() + ttl_s

    def _live(self, key: str) -> OrderedDict[int, bytes] | None:
        """Return the stream when present and unexpired; expired keys are
        dropped atomically so the caller sees Redis-equivalent semantics."""
        entry = self._streams.get(key)
        if entry is None:
            return None
        expires_at, stream = entry
        if expires_at is not None and self._monotonic() >= expires_at:
            del self._streams[key]
            return None
        return stream

    # -- EventStore --------------------------------------------------------------

    def append(self, key: str, seq: int, data: bytes) -> None:
        with self._lock:
            stream = self._live(key)
            if stream is None:
                # absent OR expired: a fresh stream may start at seq 1; Run id
                # reuse protection belongs to the future RunRepository
                if seq != 1:
                    raise EventStoreError(
                        ErrorCode.CONFLICT_IDEMPOTENCY,
                        f"{key}: first seq must be 1, got {seq}",
                    )
                stream = OrderedDict()
            else:
                latest = next(reversed(stream))
                if seq != latest + 1:
                    raise EventStoreError(
                        ErrorCode.CONFLICT_IDEMPOTENCY,
                        f"{key}: expected seq {latest + 1}, got {seq}",
                    )
            stream[seq] = data
            while len(stream) > self._max_len:  # exact MAXLEN (hard cap)
                stream.popitem(last=False)
            self._streams[key] = (self._deadline(None), stream)

    def read(
        self, key: str, after_seq: int | None = None, limit: int = 100
    ) -> tuple[tuple[int, bytes], ...]:
        with self._lock:
            stream = self._live(key)
            if stream is None:
                return ()  # missing/expired reads as empty, like Redis
            start = after_seq if after_seq is not None else 0
            return tuple(
                (seq, payload) for seq, payload in stream.items() if seq > start
            )[:limit]

    def next_seq(self, key: str) -> int:
        with self._lock:
            stream = self._live(key)
            if stream is None:
                return 0  # expired keys are gone: no seq can leak out
            try:
                return next(reversed(stream)) + 1
            except StopIteration:
                return 0

    def delete(self, key: str) -> None:
        with self._lock:
            self._streams.pop(key, None)

    def scan(self, prefix: str) -> list[str]:
        with self._lock:
            alive: list[str] = []
            for key in [k for k in self._streams if k.startswith(prefix)]:
                if self._live(key) is not None:
                    alive.append(key)
            return alive

    def sweep_expired(self, prefix: str = "") -> int:
        """Proactive whole-key cleanup; returns the number of keys removed."""
        with self._lock:
            removed = 0
            for key in [k for k in self._streams if k.startswith(prefix)]:
                expires_at, _ = self._streams[key]
                if expires_at is not None and self._monotonic() >= expires_at:
                    del self._streams[key]
                    removed += 1
            return removed


# ---------------------------------------------------------------------------
# Redis Streams adapter (atomic compare-and-append)
# ---------------------------------------------------------------------------


class RedisClientDuck(Protocol):
    """Redis surface the adapter needs.

    ``eval`` executes the Lua compare-and-append atomically server-side; the
    payload's own ``seq`` field stays the single source of truth (server
    stream IDs are never used as sequence numbers).
    """

    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...

    def xrevrange(
        self, name: str, start: str = "+", end: str = "-", *, count: int | None = None
    ) -> list[Any]: ...

    def xlen(self, name: str) -> int: ...

    def delete(self, name: str) -> int: ...

    def scan(
        self, cursor: int = 0, match: str | None = None
    ) -> tuple[int, list[Any]]: ...

    def ttl(self, name: str) -> int: ...


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decode(payload: str) -> bytes:
    return base64.b64decode(payload.encode("ascii"))


class RedisStreamEventStore:
    """Redis Streams durable mirror with atomic compare-and-append.

    One stream per run key with a whole-key TTL refreshed on append (events
    are never expired individually). Prefix listing uses SCAN, never KEYS.
    """

    def __init__(
        self,
        client: RedisClientDuck,
        *,
        prefix: str = "gcmw:run-events:",
        max_len: int = DEFAULT_MAX_EVENTS_PER_RUN,
        default_ttl_s: int = DEFAULT_RUN_TTL_S,
    ) -> None:
        _validate_store_options(max_len, default_ttl_s)
        self._client = client
        self._prefix = prefix
        self._max_len = max_len
        self._default_ttl_s = default_ttl_s

    def _name(self, key: str) -> str:
        return f"{self._prefix}{key}"

    @staticmethod
    def _arg(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)

    @staticmethod
    def _fields(entry: Any) -> dict[bytes, bytes]:
        _, fields = entry
        if isinstance(fields, dict) and all(isinstance(k, bytes) for k in fields):
            return fields
        return {str(k).encode(): str(v).encode() for k, v in fields.items()}

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        prefix: str = "gcmw:run-events:",
        max_len: int = DEFAULT_MAX_EVENTS_PER_RUN,
        default_ttl_s: int = DEFAULT_RUN_TTL_S,
        connect_timeout_s: float = 2.0,
    ) -> RedisStreamEventStore:
        """Build over ``redis.Redis.from_url`` — fails loudly when the client
        package is missing or the server is unreachable. Never falls back."""
        try:
            import redis  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise EventStoreUnavailable(
                "redis client package is not installed"
            ) from exc
        try:
            client = redis.Redis.from_url(url, socket_connect_timeout=connect_timeout_s)
            client.ping()  # explicit availability check at construction
        except Exception as exc:  # pragma: no cover - environment dependent
            raise EventStoreUnavailable(f"redis unreachable: {exc}") from exc
        return cls(client, prefix=prefix, max_len=max_len, default_ttl_s=default_ttl_s)

    # -- EventStore --------------------------------------------------------------

    def append(self, key: str, seq: int, data: bytes) -> None:
        """Atomic compare-and-append (Lua). Raises CONFLICT_IDEMPOTENCY when
        another writer already advanced the stream; unavailable clients raise
        EventStoreUnavailable — never a silent drop."""
        try:
            outcome = self._client.eval(
                _LUA_APPEND,
                1,
                self._name(key),
                str(seq),
                str(self._max_len),
                _encode(data),
                str(self._default_ttl_s or 0),
            )
            if int(self._arg(outcome)) != 1:
                raise EventStoreError(
                    ErrorCode.CONFLICT_IDEMPOTENCY,
                    f"{key}: compare-and-append conflict at seq {seq}",
                )
        except EventStoreError:
            raise
        except Exception as exc:
            raise EventStoreUnavailable(f"redis append failed: {exc}") from exc

    def read(
        self, key: str, after_seq: int | None = None, limit: int = 100
    ) -> tuple[tuple[int, bytes], ...]:
        try:
            entries = self._client.xrevrange(self._name(key), "+", "-")
            entries.reverse()  # oldest -> newest
            collected: list[tuple[int, bytes]] = []
            for entry in entries:
                fields = self._fields(entry)
                seq = int(self._arg(fields[b"seq"]))
                if seq > (after_seq or 0):
                    collected.append((seq, _decode(self._arg(fields[b"data"]))))
            return tuple(collected[:limit])
        except Exception as exc:
            raise EventStoreUnavailable(f"redis read failed: {exc}") from exc

    def next_seq(self, key: str) -> int:
        try:
            entries = self._client.xrevrange(self._name(key), "+", "-", count=1)
            if not entries:
                return 0
            return int(self._arg(self._fields(entries[0])[b"seq"])) + 1
        except Exception as exc:
            raise EventStoreUnavailable(f"redis read failed: {exc}") from exc

    def delete(self, key: str) -> None:
        try:
            self._client.delete(self._name(key))
        except Exception as exc:
            raise EventStoreUnavailable(f"redis delete failed: {exc}") from exc

    def scan(self, prefix: str) -> list[str]:
        """SCAN-based prefix listing (never KEYS). Returns LOGICAL keys — the
        namespace prefix is stripped so ``scan() -> delete() -> scan()`` is a
        closed loop, exactly like MemoryEventStore.

        ``prefix`` is matched LITERALLY (Memory ``startswith`` semantics):
        glob metacharacters in the prefix are escaped so a tenant prefix like
        ``tenant:*`` can never widen into a cross-tenant delete.
        """
        try:
            pattern = f"{self._prefix}{_glob_escape(prefix)}*"
            cursor = 0
            found: list[str] = []
            while True:
                cursor, batch = self._client.scan(cursor, match=pattern)
                for key in batch:
                    name = self._arg(key)
                    if name.startswith(self._prefix):
                        found.append(name[len(self._prefix) :])
                if not cursor:
                    break
            return found
        except Exception as exc:
            raise EventStoreUnavailable(f"redis scan failed: {exc}") from exc

    def ttl(self, key: str) -> int:
        """Remaining run-level TTL seconds (-1 = no expiry, -2 = absent)."""
        try:
            return int(self._arg(self._client.ttl(self._name(key))))
        except Exception as exc:
            raise EventStoreUnavailable(f"redis ttl failed: {exc}") from exc
