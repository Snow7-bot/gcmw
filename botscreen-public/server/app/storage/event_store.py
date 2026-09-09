"""Event storage for run SSE events (issue #36b).

Single-sequence-authority stays in the run state machine (#36a): the storage
layer only *durably mirrors* already-validated events, keyed per run, and
guards two invariants:

- contiguity — ``append`` accepts seq only when it equals the stored next
  seq; a mismatch (another worker already advanced the run) raises a conflict
  instead of tearing the stream;
- atomic paged reads — ``read(key, after_seq, limit)`` returns one snapshot
  window ``(after_seq, after_seq+limit]``; concurrent appends can never
  produce a torn page like ``([1], 2)``.

Two implementations:

- :class:`MemoryEventStore` — deterministic reference used by unit/contract
  tests and single-process development (explicit choice, never a silent
  fallback);
- :class:`RedisStreamEventStore` — Redis Streams (MAXLEN) adapter over an
  injected client duck; ``from_url`` fails loudly (explicit error) when the
  redis client is missing or the server is unreachable — there is no silent
  fallback to memory writes.

TTL and capacity are enforced at write time (oldest trimmed past
``max_len``, expired keys deleted on access) and by ``sweep_expired`` for
proactive cleanup across workers (#64 lazy expiry owns in-memory state; this
module owns the durable copy).
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
DEFAULT_RUN_TTL_S = 3600


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


class EventStore(Protocol):
    """Durable, seq-guarded append-only event log per key."""

    def append(self, key: str, seq: int, data: bytes) -> None: ...

    def read(
        self, key: str, after_seq: int | None = None, limit: int = 100
    ) -> tuple[tuple[int, bytes], ...]: ...

    def next_seq(self, key: str) -> int: ...

    def trim(self, key: str, max_len: int) -> int: ...

    def expire(self, key: str, ttl_s: int) -> None: ...

    def delete(self, key: str) -> None: ...

    def keys(self, prefix: str) -> list[str]: ...


class MemoryEventStore:
    """Deterministic reference store (tests / single-process development).

    Contiguity is enforced per key; the newest entry is trimmed once the key
    passes ``max_len`` (Redis MAXLEN semantics); TTL is checked on access and
    by ``sweep_expired``. All operations are serialized on one RLock so a
    paged read is always a consistent snapshot.
    """

    def __init__(
        self,
        *,
        max_len: int = DEFAULT_MAX_EVENTS_PER_RUN,
        default_ttl_s: int | None = DEFAULT_RUN_TTL_S,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._max_len = max_len
        self._default_ttl_s = default_ttl_s
        self._monotonic = monotonic or _now
        self._lock = threading.RLock()
        # key -> ordered seq -> (expires_at_monotonic | None, payload)
        self._streams: dict[str, OrderedDict[int, tuple[float | None, bytes]]] = {}

    # -- helpers ---------------------------------------------------------------

    def _entry_ttl_s(self, ttl_s: int | None) -> float | None:
        if ttl_s is None:
            ttl_s = self._default_ttl_s
        if ttl_s is None:
            return None
        return self._monotonic() + ttl_s

    def _alive(self, expires_at: float | None) -> bool:
        return expires_at is None or self._monotonic() < expires_at

    def _require_key(self, key: str) -> OrderedDict[int, tuple[float | None, bytes]]:
        stream = self._streams.get(key)
        if stream is None:
            raise EventStoreError(ErrorCode.NOT_FOUND_RUN, f"store key {key!r} absent")
        # lazy TTL enforcement on access
        expired = [s for s in stream if not self._alive(stream[s][0])]
        for seq in expired:
            del stream[seq]
        if expired and not stream:
            del self._streams[key]
            raise EventStoreError(ErrorCode.NOT_FOUND_RUN, f"store key {key!r} absent")
        return stream

    # -- EventStore --------------------------------------------------------------

    def append(
        self, key: str, seq: int, data: bytes, *, ttl_s: int | None = None
    ) -> None:
        with self._lock:
            stream = self._streams.setdefault(key, OrderedDict())
            if stream:
                latest = next(reversed(stream))
                if seq != latest + 1:
                    raise EventStoreError(
                        ErrorCode.CONFLICT_IDEMPOTENCY,
                        f"{key}: expected seq {latest + 1}, got {seq}",
                    )
            else:
                if seq != 1:
                    raise EventStoreError(
                        ErrorCode.CONFLICT_IDEMPOTENCY,
                        f"{key}: first seq must be 1, got {seq}",
                    )
            stream[seq] = (self._entry_ttl_s(ttl_s), data)
            while len(stream) > self._max_len:  # MAXLEN semantics
                stream.popitem(last=False)

    def read(
        self, key: str, after_seq: int | None = None, limit: int = 100
    ) -> tuple[tuple[int, bytes], ...]:
        with self._lock:
            stream = self._require_key(key)
            start = after_seq if after_seq is not None else 0
            return tuple(
                (seq, payload)
                for seq, (expires, payload) in stream.items()
                if seq > start and self._alive(expires)
            )[:limit]

    def next_seq(self, key: str) -> int:
        with self._lock:
            stream = self._streams.get(key)
            if not stream:
                return 0
            try:
                return next(reversed(stream)) + 1
            except StopIteration:
                return 0

    def trim(self, key: str, max_len: int) -> int:
        with self._lock:
            stream = self._require_key(key)
            removed = max(len(stream) - max_len, 0)
            for _ in range(removed):
                stream.popitem(last=False)
            return removed

    def expire(self, key: str, ttl_s: int) -> None:
        with self._lock:
            stream = self._streams.get(key)
            if stream is None:
                return
            deadline = self._monotonic() + ttl_s
            for seq in list(stream):
                stream[seq] = (deadline, stream[seq][1])

    def delete(self, key: str) -> None:
        with self._lock:
            self._streams.pop(key, None)

    def keys(self, prefix: str) -> list[str]:
        with self._lock:
            return [k for k in self._streams if k.startswith(prefix)]

    def sweep_expired(self, prefix: str = "") -> int:
        """Proactive cleanup across workers: delete expired keys matching a
        prefix; returns the number of keys removed."""
        with self._lock:
            removed = 0
            for key in [k for k in self._streams if k.startswith(prefix)]:
                stream = self._streams[key]
                alive = [
                    seq for seq, (expires, _) in stream.items() if self._alive(expires)
                ]
                if not alive:
                    del self._streams[key]
                    removed += 1
        return removed


# ---------------------------------------------------------------------------
# Redis Streams adapter
# ---------------------------------------------------------------------------


class RedisClientDuck(Protocol):
    """Minimal surface of a Redis client the adapter relies on.

    ``xadd`` entries carry string fields; IDs returned by the server are not
    used as sequence numbers — the payload's own ``seq`` field is the single
    source of truth, so appends stay exactly-once per seq.
    """

    def xadd(self, name: str, fields: dict, *, maxlen: int | None = None) -> Any: ...

    def xrange(self, name: str, start: str = "-", end: str = "+") -> list[Any]: ...

    def xlen(self, name: str) -> int: ...

    def delete(self, name: str) -> int: ...

    def expire(self, name: str, ttl_s: int) -> bool: ...

    def keys(self, pattern: str) -> list[str]: ...


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decode(payload: str) -> bytes:
    return base64.b64decode(payload.encode("ascii"))


class RedisStreamEventStore:
    """Redis Streams implementation of the durable event mirror.

    One stream per run key; entries carry ``seq`` and base64 ``data`` fields.
    Contiguity is validated against the newest entry before every append
    (another worker that already advanced the run → conflict, no tear).
    Capacity is bounded with ``MAXLEN`` (approximate trim, Redis semantics);
    TTL is applied per run stream at append time and by ``expire``.
    """

    def __init__(
        self,
        client: RedisClientDuck,
        *,
        prefix: str = "gcmw:run-events:",
        max_len: int = DEFAULT_MAX_EVENTS_PER_RUN,
        default_ttl_s: int = DEFAULT_RUN_TTL_S,
    ) -> None:
        self._client = client
        self._prefix = prefix
        self._max_len = max_len
        self._default_ttl_s = default_ttl_s

    def _name(self, key: str) -> str:
        return f"{self._prefix}{key}"

    @staticmethod
    def _field(fields: Any, name: str) -> str:
        value = fields.get(name) if isinstance(fields, dict) else fields[name]
        return value.decode() if isinstance(value, bytes) else str(value)

    def _last_seq(self, key: str) -> int:
        entries = self._client.xrange(self._name(key), "+", "+")
        if not entries:
            return 0
        return int(self._field(entries[-1][1], "seq"))

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

    def append(
        self, key: str, seq: int, data: bytes, *, ttl_s: int | None = None
    ) -> None:
        try:
            expected = self._last_seq(key) + 1
            if seq != expected:
                raise EventStoreError(
                    ErrorCode.CONFLICT_IDEMPOTENCY,
                    f"{key}: expected seq {expected}, got {seq}",
                )
            fields: dict[str, Any] = {"seq": str(seq), "data": _encode(data)}
            self._client.xadd(self._name(key), fields, maxlen=self._max_len)
            if self._default_ttl_s is not None:
                self._client.expire(self._name(key), ttl_s or self._default_ttl_s)
        except EventStoreError:
            raise
        except Exception as exc:
            raise EventStoreUnavailable(f"redis append failed: {exc}") from exc

    def read(
        self, key: str, after_seq: int | None = None, limit: int = 100
    ) -> tuple[tuple[int, bytes], ...]:
        try:
            entries = self._client.xrange(self._name(key))
            collected: list[tuple[int, bytes]] = []
            for _, fields in entries:
                seq = int(self._field(fields, "seq"))
                if seq > (after_seq or 0):
                    collected.append((seq, _decode(self._field(fields, "data"))))
            return tuple(collected[:limit])
        except Exception as exc:
            raise EventStoreUnavailable(f"redis read failed: {exc}") from exc

    def next_seq(self, key: str) -> int:
        try:
            return self._last_seq(key) + 1
        except Exception as exc:
            raise EventStoreUnavailable(f"redis read failed: {exc}") from exc

    def trim(self, key: str, max_len: int) -> int:
        """Report how far the stream exceeds the cap.

        Actual eviction happens approximately via XADD MAXLEN on the next
        append (Redis semantics); a deterministic trim would need XTRIM,
        which the duck surface does not carry in v1.
        """
        try:
            total = self._client.xlen(self._name(key))
            return max(total - max_len, 0)
        except Exception as exc:
            raise EventStoreUnavailable(f"redis trim failed: {exc}") from exc

    def expire(self, key: str, ttl_s: int) -> None:
        try:
            self._client.expire(self._name(key), ttl_s)
        except Exception as exc:
            raise EventStoreUnavailable(f"redis expire failed: {exc}") from exc

    def delete(self, key: str) -> None:
        try:
            self._client.delete(self._name(key))
        except Exception as exc:
            raise EventStoreUnavailable(f"redis delete failed: {exc}") from exc

    def keys(self, prefix: str) -> list[str]:
        try:
            pattern = f"{self._prefix}{prefix}*"
            return [
                k.decode() if isinstance(k, bytes) else str(k)
                for k in self._client.keys(pattern)
            ]
        except Exception as exc:
            raise EventStoreUnavailable(f"redis keys failed: {exc}") from exc
