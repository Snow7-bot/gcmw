"""Tests for the SSE event storage layer (issue #36b).

Coverage:
- MemoryEventStore: seq contiguity guard (first=1, no gaps, no duplicates),
  MAXLEN trimming of the oldest entries, TTL expiry on access, proactive
  sweep, atomic paged reads under concurrent appends (no torn windows),
  key lifecycle (expire/delete/keys);
- RedisStreamEventStore: append/read round-trip over a fake redis duck,
  contiguity enforcement from the newest entry, unavailable client surfaces
  EventStoreUnavailable (never silent);
- service integration: a configured store receives every validated event in
  seq order, purges on session deletion, and a store outage surfaces an
  explicit AppError instead of a silent memory fallback.
"""

import json
import threading

import pytest

from app.api.v1.agent_api import RunAdmissionService
from app.api.v1.auth import DevicePrincipal
from app.api.v1.errors import AppError
from app.contracts.api import Channel, CreateRunRequest, CreateSessionRequest, RunInput
from app.contracts.errors import ErrorCode
from app.storage.event_store import (
    DEFAULT_MAX_EVENTS_PER_RUN,
    EventStoreError,
    EventStoreUnavailable,
    MemoryEventStore,
    RedisStreamEventStore,
)

PRINCIPAL = DevicePrincipal(tenant_id="t1", device_id="d1")


class FakeClock:
    def __init__(self) -> None:
        self.value = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )

    def __call__(self):
        return self.value

    def advance(self, seconds: float) -> None:
        import datetime

        self.value = self.value + datetime.timedelta(seconds=seconds)


def _request(text="你好", channel="text"):
    return CreateRunRequest(
        session_id="unused",
        idempotency_key="k1",
        input=RunInput(text=text),
    )


class TestMemoryStore:
    def test_first_seq_must_be_one(self):
        store = MemoryEventStore()
        with pytest.raises(EventStoreError) as exc:
            store.append("r", 0, b"x")
        assert exc.value.code is ErrorCode.CONFLICT_IDEMPOTENCY

    def test_gaps_and_duplicates_rejected(self):
        store = MemoryEventStore()
        store.append("r", 1, b"a")
        with pytest.raises(EventStoreError):
            store.append("r", 3, b"c")  # gap
        store.append("r", 2, b"b")
        with pytest.raises(EventStoreError):
            store.append("r", 2, b"dup")  # duplicate

    def test_read_pages_after_seq(self):
        store = MemoryEventStore()
        for seq in range(1, 6):
            store.append("r", seq, f"e{seq}".encode())
        page = store.read("r", after_seq=2, limit=2)
        assert [seq for seq, _ in page] == [3, 4]

    def test_maxlen_trims_oldest(self):
        store = MemoryEventStore(max_len=3)
        for seq in range(1, 6):
            store.append("r", seq, b"x")
        assert [seq for seq, _ in store.read("r")] == [3, 4, 5]
        assert store.next_seq("r") == 6

    def test_ttl_expires_entries_on_access(self):
        monotonic = {"now": 100.0}
        store = MemoryEventStore(default_ttl_s=None, monotonic=lambda: monotonic["now"])
        store.append("r", 1, b"a", ttl_s=10)
        monotonic["now"] = 115.0
        with pytest.raises(EventStoreError) as exc:
            store.read("r")
        assert exc.value.code is ErrorCode.NOT_FOUND_RUN

    def test_sweep_expired_removes_keys(self):
        monotonic = {"now": 100.0}
        store = MemoryEventStore(default_ttl_s=None, monotonic=lambda: monotonic["now"])
        store.append("a:1", 1, b"x", ttl_s=5)
        store.append("a:2", 1, b"x", ttl_s=5)
        store.append("b:1", 1, b"x", ttl_s=1000)
        monotonic["now"] = 200.0
        assert store.sweep_expired("a:") == 2
        assert store.keys("a:") == []

    def test_concurrent_appends_yield_no_torn_pages(self):
        store = MemoryEventStore(max_len=10_000)
        errors: list[Exception] = []

        def writer(start: int):
            try:
                for seq in range(start, start + 50):
                    store.append("r", seq, b"x")
            except Exception as exc:  # noqa: BLE001 - collected below
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(1,)),
            threading.Thread(target=writer, args=(1,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # one writer wins every seq; the other only ever hits conflicts
        assert not errors or all(isinstance(e, EventStoreError) for e in errors)
        page = store.read("r", after_seq=0, limit=1000)
        seqs = [seq for seq, _ in page]
        assert seqs == list(range(1, len(seqs) + 1))  # contiguous, no tearing

    def test_expire_delete_keys(self):
        store = MemoryEventStore()
        store.append("r", 1, b"x")
        store.expire("r", 10)
        assert store.keys("run:") == []
        store.append("run:r2", 1, b"x")
        assert store.keys("run:") == ["run:r2"]
        store.delete("run:r2")
        assert store.keys("run:") == []


class FakeRedis:
    """In-memory redis duck: streams with monotonic ids."""

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self.unavailable = False

    def _fail(self):
        if self.unavailable:
            raise ConnectionError("redis down")

    def _next_id(self, name: str) -> str:
        index = len(self.streams.get(name, [])) + 1
        return f"{index}-0"

    def xadd(self, name, fields, *, maxlen=None):
        self._fail()
        stream = self.streams.setdefault(name, [])
        stream.append((self._next_id(name), dict(fields)))
        if maxlen is not None and len(stream) > maxlen:
            del stream[: len(stream) - maxlen]
        return stream[-1][0]

    def xrange(self, name, start="-", end="+"):
        self._fail()
        return list(self.streams.get(name, []))

    def xlen(self, name):
        self._fail()
        return len(self.streams.get(name, []))

    def delete(self, name):
        self._fail()
        return 1 if self.streams.pop(name, None) is not None else 0

    def expire(self, name, ttl_s):
        self._fail()
        return name in self.streams

    def keys(self, pattern):
        self._fail()
        prefix = pattern.split("*")[0]
        return [k for k in self.streams if k.startswith(prefix)]


class TestRedisStore:
    def test_roundtrip_and_paging(self):
        redis = FakeRedis()
        store = RedisStreamEventStore(redis, default_ttl_s=None)
        for seq in range(1, 4):
            store.append("r", seq, f"e{seq}".encode())
        assert [seq for seq, _ in store.read("r")] == [1, 2, 3]
        assert store.next_seq("r") == 4
        page = store.read("r", after_seq=1, limit=1)
        assert [seq for seq, _ in page] == [2]

    def test_contiguity_from_newest_entry(self):
        redis = FakeRedis()
        store = RedisStreamEventStore(redis, default_ttl_s=None)
        store.append("r", 1, b"a")
        with pytest.raises(EventStoreError) as exc:
            store.append("r", 3, b"c")
        assert exc.value.code is ErrorCode.CONFLICT_IDEMPOTENCY

    def test_maxlen_approximate_trim_on_append(self):
        redis = FakeRedis()
        store = RedisStreamEventStore(redis, max_len=2, default_ttl_s=None)
        for seq in range(1, 5):
            store.append("r", seq, b"x")
        assert store.trim("r", max_len=2) == 0  # already trimmed by MAXLEN
        assert len(store.read("r")) == 2

    def test_unavailable_client_raises_explicitly(self):
        redis = FakeRedis()
        redis.unavailable = True
        store = RedisStreamEventStore(redis, default_ttl_s=None)
        with pytest.raises(EventStoreUnavailable):
            store.append("r", 1, b"x")
        with pytest.raises(EventStoreUnavailable):
            store.read("r")

    def test_keys_prefix(self):
        redis = FakeRedis()
        store = RedisStreamEventStore(redis, default_ttl_s=None)
        store.append("run:r1", 1, b"x")
        store.append("run:r2", 1, b"x")
        assert sorted(store.keys("run:")) == [
            "gcmw:run-events:run:r1",
            "gcmw:run-events:run:r2",
        ]

    def test_default_caps_are_reasonable(self):
        assert DEFAULT_MAX_EVENTS_PER_RUN == 10_000


class TestServiceIntegration:
    def _service(self, store=None):
        return RunAdmissionService(event_store=store)

    def test_events_mirrored_in_seq_order(self):
        store = MemoryEventStore()
        service = self._service(store)
        session = service.create_session(
            PRINCIPAL, CreateSessionRequest(channel=Channel.TEXT)
        )
        run = service.create_run(
            PRINCIPAL,
            CreateRunRequest(
                session_id=session.session_id,
                idempotency_key="k1",
                input=RunInput(text="你好"),
            ),
            request_id="req-1",
            trace_id="trace-1",
        )
        service.cancel_run(PRINCIPAL, run.run_id)
        stored = store.read(f"run:{run.run_id}")
        seqs = [seq for seq, _ in stored]
        assert seqs == [1, 2]
        # durable payload is the same validated contract JSON
        assert json.loads(stored[0][1])["seq"] == 1
        assert json.loads(stored[0][1])["event"] == "run.accepted"

    def test_session_delete_purges_durable_keys(self):
        store = MemoryEventStore()
        service = self._service(store)
        session = service.create_session(
            PRINCIPAL, CreateSessionRequest(channel=Channel.TEXT)
        )
        run = service.create_run(
            PRINCIPAL,
            CreateRunRequest(
                session_id=session.session_id,
                idempotency_key="k1",
                input=RunInput(text="你好"),
            ),
            request_id="req-1",
            trace_id="trace-1",
        )
        assert store.keys("run:") == [f"run:{run.run_id}"]
        service.delete_session(PRINCIPAL, session.session_id)
        assert store.keys("run:") == []

    def test_store_outage_surfaces_explicit_error(self):
        class BrokenStore:
            def append(self, key, seq, data):
                raise EventStoreUnavailable("redis down")

        service = self._service(BrokenStore())
        session = service.create_session(
            PRINCIPAL, CreateSessionRequest(channel=Channel.TEXT)
        )
        with pytest.raises(AppError) as exc:
            service.create_run(
                PRINCIPAL,
                CreateRunRequest(
                    session_id=session.session_id,
                    idempotency_key="k1",
                    input=RunInput(text="你好"),
                ),
                request_id="req-1",
                trace_id="trace-1",
            )
        assert exc.value.code is ErrorCode.UNAVAILABLE_OVERLOADED
