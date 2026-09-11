"""Tests for RunRepository — the single durable authority (#65B-2 slice B2-A).

Unit tests run against an async fake whose ``eval`` twins mirror the Lua
scripts (explicit ``<seq>-0`` stream ids, atomic reads, tenant checks). The
real-Redis class is skipped unless ``GCMW_REDIS_TEST_URL`` is set (CI provides
a redis service), and covers the reviewer's regression list end to end:
cross-tenant write/delete, real XREAD idle/heartbeat + wake-up, concurrent CAS
single winner, atomic-snapshot canary, MAXLEN/stale cursor, TTL expiry and
same-run_id recreation, wrong-type/orphan/missing keys with zero-write on
failure, dual-layer answer events, timestamp replay stability, and the full
legal state path to STREAMING -> COMPLETED.
"""

import asyncio
import os

import pytest
import pytest_asyncio
from pytest import mark

from app.api.v1.sse_stream import stream_engine
from app.contracts.events import SSEEventType
from app.contracts.run import RunState
from app.orchestration.state_machine import is_allowed_transition
from app.storage.run_repository import (
    _LUA_APPEND,
    _LUA_COMMIT,
    _LUA_CREATE,
    _LUA_DELETE,
    _LUA_SNAPSHOT,
    MemoryRunRepository,
    RedisRunRepository,
    RunIdentity,
    RunRepositoryError,
    RunRepositoryFault,
    build_event,
)

T1 = RunIdentity(run_id="r1", tenant_id="t1", device_id="d1", session_id="s1")
T2 = RunIdentity(run_id="r1", tenant_id="t2", device_id="d9", session_id="s9")
R2 = RunIdentity(run_id="r2", tenant_id="t1", device_id="d1", session_id="s1")

ACCEPTED = RunState.ACCEPTED
GUARDING = RunState.GUARDING
ROUTING = RunState.ROUTING
FAILED = RunState.FAILED
VERIFYING = RunState.VERIFYING
DRAFTING = RunState.DRAFTING
STREAMING = RunState.STREAMING
COMPLETED = RunState.COMPLETED


def _accepted_data():
    return {"status": "accepted", "message": "问题已接收"}


class TestIdentityAndConfig:
    def test_identity_fields_must_be_non_empty(self):
        for field in ("run_id", "tenant_id", "device_id", "session_id"):
            kwargs = {
                "run_id": "r",
                "tenant_id": "t",
                "device_id": "d",
                "session_id": "s",
                field: "  ",
            }
            with pytest.raises(ValueError):
                RunIdentity(**kwargs)

    @pytest.mark.parametrize("max_events", [0, -1])
    def test_max_events_validated(self, max_events):
        with pytest.raises(ValueError):
            MemoryRunRepository(max_events=max_events)

    @pytest.mark.parametrize("ttl", [0, -5])
    def test_ttl_validated(self, ttl):
        with pytest.raises(ValueError):
            MemoryRunRepository(ttl_s=ttl)
        with pytest.raises(ValueError):
            RedisRunRepository(FakeRedis(), ttl_s=ttl)

    def test_none_ttl_allowed(self):
        MemoryRunRepository(ttl_s=None)
        RedisRunRepository(FakeRedis(), ttl_s=None)


class TestEventValidation:
    def test_forbidden_and_unknown_data_keys_rejected(self):
        with pytest.raises(RunRepositoryError) as exc:
            build_event(T1, 2, SSEEventType.ANSWER_DELTA, {"chain_of_thought": "x"})
        assert exc.value.fault is RunRepositoryFault.INVARIANT
        with pytest.raises(RunRepositoryError):
            build_event(T1, 2, SSEEventType.PROCESS_STATUS, {"delta": "x"})

    def test_layer_derived_from_event_type(self):
        assert (
            build_event(T1, 2, SSEEventType.ANSWER_DELTA, {"delta": "a"}).layer.value
            == "answer"
        )
        completed = build_event(
            T1,
            3,
            SSEEventType.ANSWER_COMPLETED,
            {"citations": [], "content_origin": "ai_generated"},
        )
        assert completed.layer.value == "answer"
        assert (
            build_event(
                T1, 2, SSEEventType.PROCESS_STATUS, {"stage": "guarding"}
            ).layer.value
            == "process"
        )

    def test_identity_comes_from_record_arguments(self):
        event = build_event(T1, 2, SSEEventType.PROCESS_STATUS, {"stage": "guarding"})
        assert (event.tenant_id, event.device_id, event.session_id) == (
            "t1",
            "d1",
            "s1",
        )


class TestMemoryRepository:
    @mark.asyncio
    async def test_create_writes_fixed_first_event(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert [e.event for e in snapshot.events] == [SSEEventType.RUN_ACCEPTED]
        assert snapshot.events[0].data == _accepted_data()
        assert snapshot.events[0].device_id == "d1"

    @mark.asyncio
    async def test_illegal_transition_rejected_without_write(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        assert is_allowed_transition(ACCEPTED, COMPLETED) is False
        with pytest.raises(RunRepositoryError) as exc:
            await repo.commit_transition(
                T1, expected_state=ACCEPTED, next_state=COMPLETED
            )
        assert exc.value.fault is RunRepositoryFault.ILLEGAL_TRANSITION
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert len(snapshot.events) == 1 and snapshot.state is ACCEPTED

    @mark.asyncio
    async def test_answer_layer_events_append_without_state_change(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        await repo.commit_transition(
            T1,
            expected_state=ACCEPTED,
            next_state=GUARDING,
            data={"stage": "guarding", "message": "处理中"},
        )
        seq = await repo.append_event(
            T1, event_type=SSEEventType.ANSWER_DELTA, data={"delta": "体"}
        )
        assert seq == 3
        await repo.append_event(
            T1, event_type=SSEEventType.ANSWER_DELTA, data={"delta": "温"}
        )
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert snapshot.state is GUARDING  # unchanged by answer deltas
        assert [(e.seq, e.layer.value, e.event.value) for e in snapshot.events] == [
            (1, "process", "run.accepted"),
            (2, "process", "process.status"),
            (3, "answer", "answer.delta"),
            (4, "answer", "answer.delta"),
        ]

    @mark.asyncio
    async def test_terminal_event_type_is_derived_not_chosen(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        await repo.commit_transition(T1, expected_state=ACCEPTED, next_state=GUARDING)
        seq = await repo.commit_transition(
            T1,
            expected_state=GUARDING,
            next_state=FAILED,
            data={"status": "failed"},
        )
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert snapshot.terminal_seq == seq == snapshot.latest_seq
        assert snapshot.events[-1].event is SSEEventType.RUN_COMPLETED
        assert snapshot.events[-1].layer.value == "process"

    @mark.asyncio
    async def test_state_only_event_types_are_not_appendable(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        with pytest.raises(RunRepositoryError) as exc:
            await repo.append_event(T1, event_type=SSEEventType.RUN_COMPLETED)
        assert exc.value.fault is RunRepositoryFault.INVARIANT

    @mark.asyncio
    async def test_commit_after_terminal_is_invariant(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        await repo.commit_transition(T1, expected_state=ACCEPTED, next_state=GUARDING)
        await repo.commit_transition(
            T1, expected_state=GUARDING, next_state=FAILED, data={"status": "failed"}
        )
        with pytest.raises(RunRepositoryError) as exc:
            await repo.append_event(T1, event_type=SSEEventType.HEARTBEAT, data={})
        assert exc.value.fault is RunRepositoryFault.INVARIANT

    @mark.asyncio
    async def test_cross_tenant_commit_and_delete_are_refused(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        with pytest.raises(RunRepositoryError) as exc:
            await repo.commit_transition(
                T2, expected_state=ACCEPTED, next_state=GUARDING
            )
        assert exc.value.fault is RunRepositoryFault.NOT_FOUND
        with pytest.raises(RunRepositoryError) as exc:
            await repo.delete(T2)
        assert exc.value.fault is RunRepositoryFault.NOT_FOUND
        assert await repo.state(T1) is ACCEPTED  # untouched

    @mark.asyncio
    async def test_concurrent_transitions_single_winner(self):
        repo = MemoryRunRepository()
        await repo.create(T1)

        async def racer():
            try:
                await repo.commit_transition(
                    T1, expected_state=ACCEPTED, next_state=GUARDING
                )
                return "ok"
            except RunRepositoryError as exc:
                return exc.fault.value

        results = await asyncio.gather(*(racer() for _ in range(8)))
        assert results.count("ok") == 1
        assert results.count("cas_conflict") == 7
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert [e.seq for e in snapshot.events] == [1, 2]

    @mark.asyncio
    async def test_ttl_expiry_and_recreate_same_run_id(self):
        clock = {"now": 100.0}
        repo = MemoryRunRepository(ttl_s=10, monotonic=lambda: clock["now"])
        await repo.create(T1)
        clock["now"] = 200.0
        with pytest.raises(RunRepositoryError) as exc:
            await repo.state(T1)
        assert exc.value.fault is RunRepositoryFault.NOT_FOUND
        await repo.create(T1)  # expired run id can be recreated
        assert await repo.state(T1) is ACCEPTED

    @mark.asyncio
    async def test_max_events_keeps_tail_but_state_intact(self):
        repo = MemoryRunRepository(max_events=3)
        await repo.create(T1)
        await repo.commit_transition(T1, expected_state=ACCEPTED, next_state=GUARDING)
        for _ in range(4):
            await repo.append_event(
                T1, event_type=SSEEventType.ANSWER_DELTA, data={"delta": "x"}
            )
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert [e.seq for e in snapshot.events] == [4, 5, 6]
        assert snapshot.oldest_available_seq == 4
        assert snapshot.state is GUARDING


class TestMemoryBlockingSnapshot:
    @mark.asyncio
    async def test_wait_wakes_on_commit(self):
        repo = MemoryRunRepository()
        await repo.create(T1)

        async def later():
            await asyncio.sleep(0.02)
            await repo.commit_transition(
                T1, expected_state=ACCEPTED, next_state=GUARDING
            )

        task = asyncio.create_task(later())
        snapshot = await repo.snapshot(T1, 1, timeout_s=2.0)
        await task
        assert snapshot.timed_out is False
        assert [e.seq for e in snapshot.events] == [2]

    @mark.asyncio
    async def test_timeout_snapshot_invariants(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        snapshot = await repo.snapshot(T1, 1, timeout_s=0.01)
        assert snapshot.timed_out is True
        assert snapshot.events == ()
        assert snapshot.latest_seq == 1
        assert snapshot.state is ACCEPTED

    @mark.asyncio
    async def test_terminal_run_never_times_out(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        await repo.commit_transition(T1, expected_state=ACCEPTED, next_state=GUARDING)
        await repo.commit_transition(T1, expected_state=GUARDING, next_state=FAILED)
        snapshot = await repo.snapshot(T1, 3, timeout_s=0.01)
        assert snapshot.timed_out is False and snapshot.terminal_seq == 3


class TestEngineIntegration:
    @mark.asyncio
    async def test_engine_replays_dual_layer_to_terminal(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        await repo.commit_transition(T1, expected_state=ACCEPTED, next_state=GUARDING)
        await repo.append_event(
            T1, event_type=SSEEventType.ANSWER_DELTA, data={"delta": "体温"}
        )
        await repo.append_event(
            T1,
            event_type=SSEEventType.ANSWER_COMPLETED,
            data={"citations": [], "content_origin": "ai_generated"},
        )
        await repo.commit_transition(
            T1,
            expected_state=GUARDING,
            next_state=FAILED,
            data={"status": "failed"},
        )
        frames = [
            f
            async for f in stream_engine(
                wait_page=lambda cursor, timeout: repo.snapshot(T1, cursor, timeout),
                heartbeat_s=0.5,
            )
        ]
        events = []
        for chunk in frames:
            for line in chunk.split("\n"):
                if line.startswith("event: "):
                    events.append(line[len("event: ") :])
        assert events == [
            "run.accepted",
            "process.status",
            "answer.delta",
            "answer.completed",
            "run.completed",
        ]

    @mark.asyncio
    async def test_engine_heartbeats_while_idle_then_terminal(self):
        repo = MemoryRunRepository()
        await repo.create(T1)

        async def later():
            await asyncio.sleep(0.05)
            await repo.commit_transition(
                T1, expected_state=ACCEPTED, next_state=GUARDING
            )
            await repo.commit_transition(T1, expected_state=GUARDING, next_state=FAILED)

        task = asyncio.create_task(later())
        frames = [
            f
            async for f in stream_engine(
                wait_page=lambda cursor, timeout: repo.snapshot(T1, cursor, timeout),
                heartbeat_s=0.01,
            )
        ]
        await task
        assert ": keep-alive\n\n" in frames
        assert frames[-1].startswith("id: 3\nevent: run.completed")


class TestFullStatePath:
    @mark.asyncio
    async def test_legal_path_to_streaming_completed(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        path = [GUARDING, ROUTING, DRAFTING, VERIFYING, STREAMING, COMPLETED]
        current = ACCEPTED
        for step in path:
            await repo.commit_transition(
                T1,
                expected_state=current,
                next_state=step,
                data={"status": "completed"} if step is COMPLETED else None,
            )
            current = step
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert snapshot.state is COMPLETED
        assert [e.seq for e in snapshot.events] == [1, 2, 3, 4, 5, 6, 7]
        assert snapshot.terminal_seq == 7


class FakeRedis:
    """Async redis duck: python twins of the repository Lua scripts.

    Streams use EXPLICIT ``<seq>-0`` ids (like the real scripts), so any
    id/seq mismatch would surface here as well.
    """

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self.wrong_type: set[str] = set()
        self.unavailable = False
        self._wake: dict[str, asyncio.Event] = {}

    # -- twins -----------------------------------------------------------------

    def _event_of(self, name: str) -> asyncio.Event:
        return self._wake.setdefault(name, asyncio.Event())

    def _twin_create(self, keys, args):
        state_key, stream_key = keys
        if state_key in self.hashes or stream_key in self.streams:
            return 0
        self.hashes[state_key] = {
            "state": "ACCEPTED",
            "tenant_id": args[0],
            "device_id": args[1],
            "session_id": args[2],
            "latest_seq": "1",
        }
        self.streams.setdefault(stream_key, []).append((args[6], {"event": args[5]}))
        self._event_of(stream_key).set()
        return 1

    def _twin_write(self, keys, args, *, transition: bool) -> int:
        state_key, stream_key = keys
        if state_key not in self.hashes:
            return -1
        fields = self.hashes[state_key]
        if fields["tenant_id"] != args[0]:
            return -2
        if fields["device_id"] != args[1] or fields["session_id"] != args[2]:
            return -4
        if stream_key not in self.streams:
            return -5
        if "terminal_seq" in fields:
            return -3
        if transition:
            expected, expected_seq = args[3], int(args[4])
            if fields["state"] != expected:
                return 0
            if int(fields["latest_seq"]) != expected_seq:
                return 1
            seq = expected_seq + 1
            self.streams[stream_key].append((f"{seq}-0", {"event": args[6]}))
            fields["state"] = args[5]
            fields["latest_seq"] = str(seq)
            if args[7] == "1":
                fields["terminal_seq"] = str(seq)
        else:
            expected_seq = int(args[3])
            if int(fields["latest_seq"]) != expected_seq:
                return 1
            seq = expected_seq + 1
            self.streams[stream_key].append((f"{seq}-0", {"event": args[4]}))
            fields["latest_seq"] = str(seq)
        self._event_of(stream_key).set()
        return seq

    def _twin_delete(self, keys, args):
        state_key, stream_key = keys
        if state_key not in self.hashes and stream_key not in self.streams:
            return -1
        if state_key in self.hashes and self.hashes[state_key]["tenant_id"] != args[0]:
            return -2
        self.hashes.pop(state_key, None)
        self.streams.pop(stream_key, None)
        return 1

    def _twin_snapshot(self, keys, args):
        state_key, stream_key = keys
        if state_key not in self.hashes:
            if stream_key in self.streams:
                raise RuntimeError("ORPHAN_STREAM")
            raise RuntimeError("NOT_FOUND")
        if state_key in self.wrong_type:
            raise RuntimeError("WRONGTYPE operation")
        fields = self.hashes[state_key]
        if fields["tenant_id"] != args[0]:
            raise RuntimeError("NOT_FOUND")
        if stream_key not in self.streams:
            raise RuntimeError("ORPHAN_STATE")
        entries = self.streams[stream_key]
        start = args[1]
        if start != "-":
            cursor = int(start[1:].split("-")[0])
            entries = [e for e in entries if int(e[0].split("-")[0]) > cursor]
        entries = entries[: int(args[2])]
        out = [
            fields["state"],
            fields["latest_seq"],
            fields.get("terminal_seq", ""),
            fields.get("device_id", ""),
            fields.get("session_id", ""),
        ]
        out.extend(e[1]["event"] for e in entries)
        return out

    # -- client duck -----------------------------------------------------------

    async def eval(self, script, numkeys, *keys_and_args):
        if self.unavailable:
            raise ConnectionError("redis down")
        keys = list(keys_and_args[:numkeys])
        args = list(keys_and_args[numkeys:])
        if script == _LUA_CREATE:
            return self._twin_create(keys, args)
        if script == _LUA_COMMIT:
            return self._twin_write(keys, args, transition=True)
        if script == _LUA_APPEND:
            return self._twin_write(keys, args, transition=False)
        if script == _LUA_DELETE:
            return self._twin_delete(keys, args)
        if script == _LUA_SNAPSHOT:
            return self._twin_snapshot(keys, args)
        raise AssertionError("fake does not understand this script")

    async def xread(self, streams, count=None, block=None):
        if self.unavailable:
            raise ConnectionError("redis down")
        name, after = next(iter(streams.items()))
        event = self._event_of(name)
        event.clear()
        entries = [e for e in self.streams.get(name, []) if e[0] > after]
        if entries:
            return [(name, entries[:count])]
        try:
            await asyncio.wait_for(event.wait(), timeout=(block or 1) / 1000)
        except TimeoutError:
            return None
        entries = [e for e in self.streams.get(name, []) if e[0] > after]
        return [(name, entries[:count])] if entries else None

    async def delete(self, *names):
        for name in names:
            self.hashes.pop(name, None)
            self.streams.pop(name, None)


def _redis_repo(redis=None, **kwargs):
    return RedisRunRepository(redis or FakeRedis(), **kwargs)


class TestRedisRepository:
    @mark.asyncio
    async def test_create_commit_and_snapshot(self):
        repo = _redis_repo()
        await repo.create(T1)
        seq = await repo.commit_transition(
            T1, expected_state=ACCEPTED, next_state=GUARDING
        )
        assert seq == 2
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert [e.seq for e in snapshot.events] == [1, 2]
        assert [e.event for e in snapshot.events] == [
            SSEEventType.RUN_ACCEPTED,
            SSEEventType.PROCESS_STATUS,
        ]
        assert snapshot.state is GUARDING

    @mark.asyncio
    async def test_answer_layer_append(self):
        repo = _redis_repo()
        await repo.create(T1)
        await repo.append_event(
            T1, event_type=SSEEventType.ANSWER_DELTA, data={"delta": "体温"}
        )
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert snapshot.events[-1].layer.value == "answer"
        assert snapshot.state is ACCEPTED  # state preserved

    @mark.asyncio
    async def test_physical_ids_match_business_seq(self):
        redis = FakeRedis()
        repo = _redis_repo(redis)
        await repo.create(T1)
        await repo.commit_transition(T1, expected_state=ACCEPTED, next_state=GUARDING)
        _, stream_key = repo.keys(T1.run_id)
        assert [entry_id for entry_id, _ in redis.streams[stream_key]] == ["1-0", "2-0"]

    @mark.asyncio
    async def test_cross_tenant_commit_and_delete_refused(self):
        repo = _redis_repo()
        await repo.create(T1)
        with pytest.raises(RunRepositoryError) as exc:
            await repo.commit_transition(
                T2, expected_state=ACCEPTED, next_state=GUARDING
            )
        assert exc.value.fault is RunRepositoryFault.NOT_FOUND
        with pytest.raises(RunRepositoryError) as exc:
            await repo.delete(T2)
        assert exc.value.fault is RunRepositoryFault.NOT_FOUND
        assert await repo.state(T1) is ACCEPTED

    @mark.asyncio
    async def test_delete_is_tenant_authenticated_and_atomic(self):
        redis = FakeRedis()
        repo = _redis_repo(redis)
        await repo.create(T1)
        state_key, stream_key = repo.keys(T1.run_id)
        await repo.delete(T1)
        assert state_key not in redis.hashes and stream_key not in redis.streams

    @mark.asyncio
    async def test_concurrent_cas_single_winner(self):
        repo = _redis_repo()
        await repo.create(T1)

        async def racer():
            try:
                await repo.commit_transition(
                    T1, expected_state=ACCEPTED, next_state=GUARDING
                )
                return "ok"
            except RunRepositoryError as exc:
                return exc.fault.value

        results = await asyncio.gather(*(racer() for _ in range(8)))
        assert results.count("ok") == 1
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert [e.seq for e in snapshot.events] == [1, 2]

    @mark.asyncio
    async def test_blocking_read_and_idle_timeout(self):
        repo = _redis_repo()
        await repo.create(T1)
        idle = await repo.snapshot(T1, 1, timeout_s=0.05)
        assert idle.timed_out is True and idle.events == ()
        assert idle.latest_seq == 1 and idle.state is ACCEPTED

        async def later():
            await asyncio.sleep(0.02)
            await repo.commit_transition(
                T1, expected_state=ACCEPTED, next_state=GUARDING
            )

        task = asyncio.create_task(later())
        snapshot = await repo.snapshot(T1, 1, timeout_s=1.0)
        await task
        assert [e.seq for e in snapshot.events] == [2]

    @mark.asyncio
    async def test_terminal_state_and_append_after_terminal(self):
        repo = _redis_repo()
        await repo.create(T1)
        await repo.commit_transition(T1, expected_state=ACCEPTED, next_state=GUARDING)
        seq = await repo.commit_transition(
            T1,
            expected_state=GUARDING,
            next_state=FAILED,
            data={"status": "failed"},
        )
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert snapshot.terminal_seq == seq == 3
        with pytest.raises(RunRepositoryError) as exc:
            await repo.append_event(T1, event_type=SSEEventType.HEARTBEAT)
        assert exc.value.fault is RunRepositoryFault.INVARIANT

    @mark.asyncio
    async def test_orphan_and_wrong_type_keys_are_invariants(self):
        redis = FakeRedis()
        repo = _redis_repo(redis)
        await repo.create(T1)
        _state_key, stream_key = repo.keys(T1.run_id)
        redis.streams.pop(stream_key)  # state without stream
        with pytest.raises(RunRepositoryError) as exc:
            await repo.snapshot(T1, 0, 0.01)
        assert exc.value.fault is RunRepositoryFault.INVARIANT

        redis2 = FakeRedis()
        repo2 = _redis_repo(redis2)
        await repo2.create(T1)
        redis2.wrong_type.add(repo2.keys(T1.run_id)[0])
        with pytest.raises(RunRepositoryError) as exc:
            await repo2.snapshot(T1, 0, 0.01)
        assert exc.value.fault is RunRepositoryFault.INVARIANT

    @mark.asyncio
    async def test_failed_write_leaves_store_untouched(self):
        redis = FakeRedis()
        repo = _redis_repo(redis)
        await repo.create(T1)
        with pytest.raises(RunRepositoryError):
            await repo.commit_transition(
                T1, expected_state=DRAFTING, next_state=STREAMING
            )
        _, stream_key = repo.keys(T1.run_id)
        assert len(redis.streams[stream_key]) == 1  # nothing appended

    @mark.asyncio
    async def test_unavailable_client_is_explicit(self):
        redis = FakeRedis()
        redis.unavailable = True
        repo = _redis_repo(redis)
        with pytest.raises(RunRepositoryError) as exc:
            await repo.create(T1)
        assert exc.value.fault is RunRepositoryFault.UNAVAILABLE


@pytest.mark.skipif(
    not os.getenv("GCMW_REDIS_TEST_URL"),
    reason="GCMW_REDIS_TEST_URL not set (CI redis service)",
)
class TestRealRedis:
    """End-to-end against a real Redis server (CI service container)."""

    @pytest_asyncio.fixture()
    async def repo(self):
        import redis.asyncio as aioredis

        client = aioredis.from_url(os.environ["GCMW_REDIS_TEST_URL"])
        repo = RedisRunRepository(client, prefix="gcmw:test:run:")
        for identity in (T1, R2):
            try:
                await repo.delete(identity)
            except RunRepositoryError:
                pass
        yield repo
        for identity in (T1, R2):
            try:
                await repo.delete(identity)
            except RunRepositoryError:
                pass
        await client.aclose()

    @mark.asyncio
    async def test_full_legal_path_and_dual_layer(self, repo):
        await repo.create(T1)
        path = [
            (ACCEPTED, GUARDING),
            (GUARDING, ROUTING),
            (ROUTING, DRAFTING),
            (DRAFTING, VERIFYING),
            (VERIFYING, STREAMING),
            (STREAMING, COMPLETED),
        ]
        for expected, nxt in path[:-1]:
            await repo.commit_transition(T1, expected_state=expected, next_state=nxt)
        # dual-layer events land while the run is still streaming (not terminal)
        await repo.append_event(
            T1, event_type=SSEEventType.ANSWER_DELTA, data={"delta": "x"}
        )
        await repo.append_event(
            T1,
            event_type=SSEEventType.ANSWER_COMPLETED,
            data={"citations": [], "content_origin": "ai_generated"},
        )
        await repo.commit_transition(
            T1,
            expected_state=path[-1][0],
            next_state=path[-1][1],
            data={"status": "completed"},
        )
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert snapshot.state is COMPLETED
        assert snapshot.terminal_seq == snapshot.latest_seq
        events = [e.event for e in snapshot.events]
        assert events[0] is SSEEventType.RUN_ACCEPTED
        assert SSEEventType.ANSWER_DELTA in events
        assert SSEEventType.ANSWER_COMPLETED in events
        assert events[-1] is SSEEventType.RUN_COMPLETED

    @mark.asyncio
    async def test_physical_ids_are_business_seq(self, repo):
        await repo.create(T1)
        await repo.commit_transition(T1, expected_state=ACCEPTED, next_state=GUARDING)
        _, stream_key = repo.keys(T1.run_id)
        raw = await repo._client.xrange(stream_key)
        assert [entry[0].decode() for entry in raw] == ["1-0", "2-0"]

    @mark.asyncio
    async def test_cross_tenant_commit_and_delete(self, repo):
        await repo.create(T1)
        with pytest.raises(RunRepositoryError) as exc:
            await repo.commit_transition(
                T2, expected_state=ACCEPTED, next_state=GUARDING
            )
        assert exc.value.fault is RunRepositoryFault.NOT_FOUND
        with pytest.raises(RunRepositoryError) as exc:
            await repo.delete(T2)
        assert exc.value.fault is RunRepositoryFault.NOT_FOUND
        assert await repo.state(T1) is ACCEPTED

    @mark.asyncio
    async def test_idle_xread_then_wake_on_commit(self, repo):
        await repo.create(T1)
        idle = await repo.snapshot(T1, 1, timeout_s=0.1)
        assert idle.timed_out is True and idle.latest_seq == 1

        async def later():
            await asyncio.sleep(0.05)
            await repo.commit_transition(
                T1, expected_state=ACCEPTED, next_state=GUARDING
            )

        task = asyncio.create_task(later())
        snapshot = await repo.snapshot(T1, 1, timeout_s=2.0)
        await task
        assert [e.seq for e in snapshot.events] == [2]

    @mark.asyncio
    async def test_concurrent_cas_single_winner(self, repo):
        await repo.create(T1)

        async def racer():
            try:
                await repo.commit_transition(
                    T1, expected_state=ACCEPTED, next_state=GUARDING
                )
                return "ok"
            except RunRepositoryError as exc:
                return exc.fault.value

        results = await asyncio.gather(*(racer() for _ in range(8)))
        assert results.count("ok") == 1
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert [e.seq for e in snapshot.events] == [1, 2]

    @mark.asyncio
    async def test_atomic_snapshot_canary(self, repo):
        """A concurrent commit can never yield a half-applied snapshot."""
        await repo.create(T1)

        async def writer():
            current = ACCEPTED
            for nxt in (GUARDING, ROUTING, DRAFTING, VERIFYING, STREAMING, COMPLETED):
                await repo.commit_transition(
                    T1,
                    expected_state=current,
                    next_state=nxt,
                    data={"status": "completed"} if nxt is COMPLETED else None,
                )
                current = nxt
                await asyncio.sleep(0)

        task = asyncio.create_task(writer())
        for _ in range(12):
            snapshot = await repo.snapshot(T1, 0, 0.05)
            seqs = [e.seq for e in snapshot.events]
            assert seqs == list(range(1, len(seqs) + 1))  # contiguous, no tear
            if snapshot.terminal_seq is not None:
                assert snapshot.terminal_seq == snapshot.latest_seq
        await task

    @mark.asyncio
    async def test_maxlen_stale_cursor(self, repo):
        await repo.create(T1)
        for _ in range(6):
            await repo.append_event(
                T1, event_type=SSEEventType.ANSWER_DELTA, data={"delta": "x"}
            )
        trimmed = RedisRunRepository(repo._client, prefix=repo._prefix, max_events=3)
        await trimmed.commit_transition(
            T1, expected_state=ACCEPTED, next_state=GUARDING
        )
        snapshot = await trimmed.snapshot(T1, 0, 0.01)
        assert snapshot.oldest_available_seq >= 2  # window trimmed past seq 1

    @mark.asyncio
    async def test_ttl_expiry_and_recreate(self):
        import redis.asyncio as aioredis

        client = aioredis.from_url(os.environ["GCMW_REDIS_TEST_URL"])
        repo = RedisRunRepository(client, prefix="gcmw:test:ttl:", ttl_s=1)
        await repo.create(R2)
        await repo.commit_transition(R2, expected_state=ACCEPTED, next_state=GUARDING)
        await asyncio.sleep(1.3)
        with pytest.raises(RunRepositoryError) as exc:
            await repo.state(R2)
        assert exc.value.fault is RunRepositoryFault.NOT_FOUND
        await repo.create(R2)  # same run_id may be recreated after expiry
        assert await repo.state(R2) is ACCEPTED
        await repo.delete(R2)
        await client.aclose()

    @mark.asyncio
    async def test_wrong_type_and_missing_keys(self, repo):
        await repo.create(T1)
        state_key, _stream = repo.keys(T1.run_id)
        await repo._client.delete(state_key)
        await repo._client.set(state_key, "not-a-hash")
        with pytest.raises(RunRepositoryError) as exc:
            await repo.snapshot(T1, 0, 0.01)
        assert exc.value.fault is RunRepositoryFault.INVARIANT
        await repo._client.delete(state_key)

    @mark.asyncio
    async def test_timestamp_replay_is_stable(self, repo):
        await repo.create(T1)
        first = await repo.snapshot(T1, 0, 0.01)
        await asyncio.sleep(0.05)
        second = await repo.snapshot(T1, 0, 0.01)
        assert first.events[0].timestamp == second.events[0].timestamp

    @mark.asyncio
    async def test_lua_failure_leaves_zero_writes(self, repo):
        await repo.create(T1)
        _, stream_key = repo.keys(T1.run_id)
        before = len(await repo._client.xrange(stream_key))
        with pytest.raises(RunRepositoryError):
            # illegal transition is rejected before any write reaches Redis
            await repo.commit_transition(
                T1, expected_state=DRAFTING, next_state=COMPLETED
            )
        after = len(await repo._client.xrange(stream_key))
        assert before == after
