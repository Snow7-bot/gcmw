"""Tests for the SSE event storage layer (issue #36b, remediation round).

Coverage:
- MemoryEventStore: contiguous append (first=1, no gaps/duplicates), MAXLEN
  trim of oldest, whole-key TTL (never per-event — no seq holes), proactive
  sweep, atomic paged reads under concurrent appends, SCAN listing;
- RedisStreamEventStore: append/read round-trip over a fake redis duck whose
  eval() runs the Lua compare-and-append twin atomically; a concurrent probe
  proves duplicate seq ([1,2,2]) is impossible; outage → EventStoreUnavailable;
  prefix listing is SCAN-based; run-level TTL is applied;
- real-Redis integration (skipped unless GCMW_REDIS_TEST_URL is set — CI runs
  it against the redis service container).
"""

import os
import threading

import pytest

from app.contracts.errors import ErrorCode
from app.storage.event_store import (
    _LUA_APPEND,
    DEFAULT_MAX_EVENTS_PER_RUN,
    EventStoreError,
    EventStoreUnavailable,
    MemoryEventStore,
    RedisStreamEventStore,
)


class TestMemoryStore:
    def test_first_seq_must_be_one(self):
        store = MemoryEventStore(default_ttl_s=None)
        with pytest.raises(EventStoreError) as exc:
            store.append("r", 0, b"x")
        assert exc.value.code is ErrorCode.CONFLICT_IDEMPOTENCY

    def test_gaps_and_duplicates_rejected(self):
        store = MemoryEventStore(default_ttl_s=None)
        store.append("r", 1, b"a")
        with pytest.raises(EventStoreError):
            store.append("r", 3, b"c")  # gap
        store.append("r", 2, b"b")
        with pytest.raises(EventStoreError):
            store.append("r", 2, b"dup")  # duplicate

    def test_read_pages_after_seq(self):
        store = MemoryEventStore(default_ttl_s=None)
        for seq in range(1, 6):
            store.append("r", seq, f"e{seq}".encode())
        page = store.read("r", after_seq=2, limit=2)
        assert [seq for seq, _ in page] == [3, 4]

    def test_maxlen_trims_oldest_keeps_tail_contiguous(self):
        store = MemoryEventStore(max_len=3, default_ttl_s=None)
        for seq in range(1, 6):
            store.append("r", seq, b"x")
        assert [seq for seq, _ in store.read("r")] == [3, 4, 5]
        assert store.next_seq("r") == 6  # tail stays contiguous

    def test_whole_key_ttl_expires_atomically(self):
        monotonic = {"now": 100.0}
        store = MemoryEventStore(default_ttl_s=10, monotonic=lambda: monotonic["now"])
        store.append("r", 1, b"a")
        store.append("r", 2, b"b")
        monotonic["now"] = 115.0
        # expired keys behave exactly like missing keys (Redis semantics)
        assert store.read("r") == ()
        assert store.next_seq("r") == 0
        assert store.scan("r") == []

    def test_expired_key_can_be_recreated_at_seq_1(self):
        monotonic = {"now": 100.0}
        store = MemoryEventStore(default_ttl_s=10, monotonic=lambda: monotonic["now"])
        store.append("r", 1, b"a")
        store.append("r", 2, b"b")
        monotonic["now"] = 115.0
        store.append("r", 1, b"fresh")  # expired -> fresh stream allowed
        assert [seq for seq, _ in store.read("r")] == [1]
        assert store.next_seq("r") == 2

    def test_sweep_expired_removes_keys(self):
        monotonic = {"now": 100.0}
        store = MemoryEventStore(default_ttl_s=None, monotonic=lambda: monotonic["now"])
        store.append("a:1", 1, b"x")
        store._streams["a:1"] = (monotonic["now"] + 5, store._streams["a:1"][1])
        store.append("a:2", 1, b"x")
        store._streams["a:2"] = (monotonic["now"] + 5, store._streams["a:2"][1])
        monotonic["now"] = 200.0
        assert store.sweep_expired("a:") == 2
        assert store.scan("a:") == []

    def test_concurrent_appends_never_duplicate_seq(self):
        store = MemoryEventStore(max_len=10_000, default_ttl_s=None)
        conflicts = []

        def writer(start: int):
            for seq in range(start, start + 50):
                try:
                    store.append("r", seq, b"x")
                except EventStoreError:
                    conflicts.append(seq)

        threads = [
            threading.Thread(target=writer, args=(1,)),
            threading.Thread(target=writer, args=(1,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        page = store.read("r", after_seq=0, limit=10_000)
        seqs = [seq for seq, _ in page]
        assert seqs == list(range(1, 51))  # exactly once each, contiguous
        assert conflicts  # the loser writer only ever saw conflicts

    def test_scan_delete_loop_returns_empty(self):
        store = MemoryEventStore(default_ttl_s=None)
        store.append("run:r1", 1, b"x")
        store.append("run:r2", 1, b"x")
        assert sorted(store.scan("run:")) == ["run:r1", "run:r2"]
        for key in store.scan("run:"):
            store.delete(key)
        assert store.scan("run:") == []

    def test_scan_excludes_expired_keys(self):
        monotonic = {"now": 100.0}
        store = MemoryEventStore(default_ttl_s=None, monotonic=lambda: monotonic["now"])
        store.append("run:a", 1, b"x")
        store._streams["run:a"] = (monotonic["now"] + 5, store._streams["run:a"][1])
        store.append("run:b", 1, b"x")  # no expiry (ttl None)
        monotonic["now"] = 200.0
        assert store.scan("run:") == ["run:b"]


class FakeRedis:
    """Redis duck whose eval() executes the Lua compare-and-append twin.

    The twin runs under one lock, mirroring the atomicity of the server-side
    script — the concurrency probe therefore exercises the exact invariant.
    """

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._ttls: dict[str, int] = {}
        self.unavailable = False

    def _lock_for(self, name: str) -> threading.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = threading.Lock()
            self._locks[name] = lock
        return lock

    def _next_id(self, name: str) -> str:
        return f"{len(self.streams.get(name, [])) + 1}-0"

    def _twin_append(self, name, seq, maxlen, data, ttl_s):
        entries = self.xrevrange(name, "+", "-", count=1)
        last = 0
        if entries:
            last = int(entries[0][1]["seq"])
        if last + 1 != seq:
            return 0
        stream = self.streams.setdefault(name, [])
        stream.append((self._next_id(name), {"seq": str(seq), "data": data}))
        if len(stream) > maxlen:
            del stream[: len(stream) - maxlen]
        if ttl_s > 0:
            self._ttls[name] = ttl_s
        return 1

    # -- client duck -----------------------------------------------------------

    def eval(self, script, numkeys, *keys_and_args):
        if self.unavailable:
            raise ConnectionError("redis down")
        assert script == _LUA_APPEND, "fake only understands the append script"
        name = keys_and_args[0]
        seq, maxlen, data, ttl_s = keys_and_args[1:5]
        with self._lock_for(name):
            return self._twin_append(name, int(seq), int(maxlen), data, int(ttl_s))

    def xrevrange(self, name, start="+", end="-", *, count=None):
        if self.unavailable:
            raise ConnectionError("redis down")
        entries = list(reversed(list(self.streams.get(name, []))))
        if count is not None:
            entries = entries[:count]
        return entries

    def xlen(self, name):
        if self.unavailable:
            raise ConnectionError("redis down")
        return len(self.streams.get(name, []))

    def delete(self, name):
        if self.unavailable:
            raise ConnectionError("redis down")
        return 1 if self.streams.pop(name, None) is not None else 0

    def scan(self, cursor=0, match=None):
        if self.unavailable:
            raise ConnectionError("redis down")
        prefix = match.split("*")[0]
        return (0, sorted(k for k in self.streams if k.startswith(prefix)))

    def ttl(self, name):
        if self.unavailable:
            raise ConnectionError("redis down")
        return self._ttls.get(name, -2)


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

    def test_compare_and_append_rejects_conflict(self):
        redis = FakeRedis()
        store = RedisStreamEventStore(redis, default_ttl_s=None)
        store.append("r", 1, b"a")
        with pytest.raises(EventStoreError) as exc:
            store.append("r", 3, b"c")
        assert exc.value.code is ErrorCode.CONFLICT_IDEMPOTENCY
        with pytest.raises(EventStoreError):
            store.append("r", 1, b"dup")

    def test_concurrent_probe_never_duplicates_seq(self):
        """Reproduces the reviewer's [1,2,2] probe against the Lua twin."""
        redis = FakeRedis()
        store = RedisStreamEventStore(redis, default_ttl_s=None)
        conflicts = []

        def writer(start: int):
            for seq in range(start, start + 60):
                try:
                    store.append("r", seq, f"w{seq}".encode())
                except EventStoreError:
                    conflicts.append(seq)

        threads = [threading.Thread(target=writer, args=(1,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        seqs = [seq for seq, _ in store.read("r")]
        assert seqs == list(range(1, 61))  # no [1,2,2]
        assert len(seqs) == len(set(seqs))
        assert conflicts  # losers observed conflicts, never duplicated

    def test_run_level_ttl_refreshed_on_append(self):
        redis = FakeRedis()
        store = RedisStreamEventStore(redis, default_ttl_s=100)
        store.append("r", 1, b"a")
        store.append("r", 2, b"b")
        assert store.ttl("r") == 100  # whole key, refreshed

    def test_maxlen_trimmed_by_lua_append(self):
        redis = FakeRedis()
        store = RedisStreamEventStore(redis, max_len=2, default_ttl_s=None)
        for seq in range(1, 5):
            store.append("r", seq, b"x")
        assert [seq for seq, _ in store.read("r")] == [3, 4]

    def test_unavailable_client_raises_explicitly(self):
        redis = FakeRedis()
        redis.unavailable = True
        store = RedisStreamEventStore(redis, default_ttl_s=None)
        with pytest.raises(EventStoreUnavailable):
            store.append("r", 1, b"x")
        with pytest.raises(EventStoreUnavailable):
            store.read("r")

    def test_scan_returns_logical_keys_and_delete_loop_closes(self):
        redis = FakeRedis()
        store = RedisStreamEventStore(redis, default_ttl_s=None)
        store.append("run:r1", 1, b"x")
        store.append("run:r2", 1, b"x")
        found = store.scan("run:")
        # no namespace prefix — delete() accepts what scan() returns
        assert sorted(found) == ["run:r1", "run:r2"]
        for key in found:
            store.delete(key)
        assert store.scan("run:") == []
        assert redis.streams == {}  # physically cleared

    def test_default_caps_are_reasonable(self):
        assert DEFAULT_MAX_EVENTS_PER_RUN == 10_000


@pytest.mark.skipif(
    not os.getenv("GCMW_REDIS_TEST_URL"),
    reason="GCMW_REDIS_TEST_URL not set (CI redis service)",
)
class TestRealRedisIntegration:
    @pytest.fixture()
    def live(self):
        url = os.environ["GCMW_REDIS_TEST_URL"]
        store = RedisStreamEventStore.from_url(url)
        for key in store.scan("test:"):
            store.delete(key)
        yield store
        for key in store.scan("test:"):
            store.delete(key)

    def test_roundtrip(self, live):
        for seq in range(1, 4):
            live.append("test:r", seq, f"e{seq}".encode())
        assert [seq for seq, _ in live.read("test:r")] == [1, 2, 3]
        assert live.next_seq("test:r") == 4

    def test_conflict_is_atomic(self, live):
        live.append("test:r2", 1, b"a")
        with pytest.raises(EventStoreError) as exc:
            live.append("test:r2", 3, b"c")
        assert exc.value.code is ErrorCode.CONFLICT_IDEMPOTENCY
        assert [seq for seq, _ in live.read("test:r2")] == [1]

    def test_run_level_ttl(self, live):
        store = RedisStreamEventStore.from_url(
            os.environ["GCMW_REDIS_TEST_URL"], default_ttl_s=60
        )
        store.append("test:ttl", 1, b"x")
        assert 0 < store.ttl("test:ttl") <= 60
        store.delete("test:ttl")

    def test_barrier_concurrent_same_seq_single_winner(self, live):
        """N threads race to append seq=2 after seq=1 — exactly one wins."""
        import threading

        live.append("test:race", 1, b"seed")
        barrier = threading.Barrier(6)
        outcomes: list[bool] = []
        lock = threading.Lock()

        def racer():
            barrier.wait()
            try:
                live.append("test:race", 2, b"winner")
            except EventStoreError:
                outcome = False
            else:
                outcome = True
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=racer) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert outcomes.count(True) == 1
        assert outcomes.count(False) == 5
        assert [seq for seq, _ in live.read("test:race")] == [1, 2]

    def test_real_ttl_expiry_removes_the_key(self, live):
        import time

        store = RedisStreamEventStore.from_url(
            os.environ["GCMW_REDIS_TEST_URL"], default_ttl_s=1
        )
        store.append("test:expiry", 1, b"x")
        assert store.read("test:expiry") != ()
        time.sleep(1.5)
        assert store.read("test:expiry") == ()  # whole key actually expired
        assert store.next_seq("test:expiry") == 0
        assert "test:expiry" not in store.scan("test:")

    def test_real_scan_delete_loop(self, live):
        live.append("test:sd1", 1, b"x")
        live.append("test:sd2", 1, b"x")
        found = live.scan("test:sd")
        assert sorted(found) == ["test:sd1", "test:sd2"]
        for key in found:
            live.delete(key)
        assert live.scan("test:sd") == []

    def test_hard_capacity_is_exact(self, live):
        store = RedisStreamEventStore.from_url(
            os.environ["GCMW_REDIS_TEST_URL"], max_len=3, default_ttl_s=None
        )
        for seq in range(1, 6):
            store.append("test:cap", seq, b"x")
        # exact MAXLEN '=' — never more than the promised hard cap
        assert [seq for seq, _ in store.read("test:cap")] == [3, 4, 5]
        store.delete("test:cap")
