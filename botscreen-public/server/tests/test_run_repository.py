"""Tests for the atomic run repository (issue #65B-2 slice B2-A).

Coverage:
- state + event are committed ATOMICALLY: a compare-and-set mismatch writes
  nothing (state unchanged, no event appended);
- concurrent commits under the same expected state produce exactly one winner
  (no duplicate seqs, no divergence), the rest conflict;
- the event sequence is derived inside the atomic step (contiguity);
- the terminal event records ``terminal_seq`` in the same commit;
- ``snapshot()`` implements the #65B-1 engine read interface: blocking wait
  returns as soon as an event lands, timeout yields a snapshot satisfying the
  engine invariants (no events, non-terminal, ``latest == cursor``);
- window bounds (oldest/latest/terminal_seq) are derived consistently;
- tenant binding: another tenant's run reads as absent;
- Redis implementation: Lua CAS twin over a fake client (create/commit/conflict
  /snapshot), blocking read, explicit failure when the client is down;
- end-to-end: the B-1 stream engine drives the repository to a terminal frame.
"""

import asyncio
import threading

import pytest
from pytest import mark

from app.api.v1.sse_stream import stream_engine
from app.contracts.events import SSEEventType
from app.contracts.run import RunState
from app.storage.run_repository import (
    _LUA_COMMIT,
    _LUA_CREATE,
    MemoryRunRepository,
    RedisRunRepository,
    RunIdentity,
    RunRepositoryError,
    RunRepositoryFault,
)

T1 = RunIdentity(run_id="r1", tenant_id="t1", device_id="d1", session_id="s1")
T1_OTHER_RUN = RunIdentity(run_id="r2", tenant_id="t1")
T2_SAME_RUN = RunIdentity(run_id="r1", tenant_id="t2")


class TestMemoryAtomicity:
    @mark.asyncio
    async def test_create_writes_state_and_first_event_together(self):
        repo = MemoryRunRepository()
        assert await repo.create(T1) == 1
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert [e.seq for e in snapshot.events] == [1]
        assert snapshot.state is RunState.ACCEPTED
        assert snapshot.oldest_available_seq == snapshot.latest_seq == 1
        assert snapshot.terminal_seq is None

    @mark.asyncio
    async def test_cas_mismatch_writes_nothing(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        before = await repo.snapshot(T1, 0, 0.01)
        with pytest.raises(RunRepositoryError) as exc:
            await repo.commit(
                T1,
                expected_state=RunState.DRAFTING,  # wrong: run is ACCEPTED
                next_state=RunState.DRAFTING,
                event_type=SSEEventType.PROCESS_STATUS,
            )
        assert exc.value.fault is RunRepositoryFault.CAS_CONFLICT
        after = await repo.snapshot(T1, 0, 0.01)
        assert after.state is before.state
        assert [e.seq for e in after.events] == [e.seq for e in before.events]

    @mark.asyncio
    async def test_concurrent_commits_single_winner(self):
        repo = MemoryRunRepository()
        await repo.create(T1)

        async def racer():
            try:
                seq = await repo.commit(
                    T1,
                    expected_state=RunState.ACCEPTED,
                    next_state=RunState.GUARDING,
                    event_type=SSEEventType.PROCESS_STATUS,
                )
                return ("ok", seq)
            except RunRepositoryError as exc:
                return (exc.fault.value, None)

        results = await asyncio.gather(*(racer() for _ in range(8)))
        assert [status for status, _ in results].count("ok") == 1
        assert [status for status, _ in results].count("cas_conflict") == 7
        snapshot = await repo.snapshot(T1, 0, 0.01)
        seqs = [e.seq for e in snapshot.events]
        assert seqs == [1, 2]  # exactly one new event, contiguous
        assert snapshot.state is RunState.GUARDING

    @mark.asyncio
    async def test_terminal_commit_records_terminal_seq_atomically(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        await repo.commit(
            T1,
            expected_state=RunState.ACCEPTED,
            next_state=RunState.COMPLETED,
            event_type=SSEEventType.RUN_COMPLETED,
            data={"status": "completed"},
        )
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert snapshot.state is RunState.COMPLETED
        assert snapshot.terminal_seq == snapshot.latest_seq == 2
        assert [e.event for e in snapshot.events] == [
            SSEEventType.RUN_ACCEPTED,
            SSEEventType.RUN_COMPLETED,
        ]

    @mark.asyncio
    async def test_commit_after_terminal_is_invariant_error(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        await repo.commit(
            T1,
            expected_state=RunState.ACCEPTED,
            next_state=RunState.COMPLETED,
            event_type=SSEEventType.RUN_COMPLETED,
        )
        with pytest.raises(RunRepositoryError) as exc:
            await repo.commit(
                T1,
                expected_state=RunState.COMPLETED,
                next_state=RunState.FAILED,
                event_type=SSEEventType.RUN_COMPLETED,
            )
        assert exc.value.fault is RunRepositoryFault.INVARIANT

    @mark.asyncio
    async def test_tenant_binding_hides_foreign_run(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        with pytest.raises(RunRepositoryError) as exc:
            await repo.snapshot(T2_SAME_RUN, 0, 0.01)
        assert exc.value.fault is RunRepositoryFault.NOT_FOUND


class TestBlockingSnapshot:
    @mark.asyncio
    async def test_wait_returns_as_soon_as_event_lands(self):
        repo = MemoryRunRepository()
        await repo.create(T1)

        async def commit_later():
            await asyncio.sleep(0.02)
            await repo.commit(
                T1,
                expected_state=RunState.ACCEPTED,
                next_state=RunState.GUARDING,
                event_type=SSEEventType.PROCESS_STATUS,
            )

        task = asyncio.create_task(commit_later())
        snapshot = await repo.snapshot(T1, 1, timeout_s=2.0)  # blocks, then wakes
        await task
        assert snapshot.timed_out is False
        assert [e.seq for e in snapshot.events] == [2]

    @mark.asyncio
    async def test_timeout_snapshot_satisfies_engine_invariants(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        snapshot = await repo.snapshot(T1, 1, timeout_s=0.01)
        assert snapshot.timed_out is True
        assert snapshot.events == ()
        assert snapshot.state not in {RunState.COMPLETED, RunState.CANCELLED}
        assert snapshot.latest_seq == 1  # latest == cursor

    @mark.asyncio
    async def test_terminal_run_never_times_out(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        await repo.commit(
            T1,
            expected_state=RunState.ACCEPTED,
            next_state=RunState.COMPLETED,
            event_type=SSEEventType.RUN_COMPLETED,
        )
        snapshot = await repo.snapshot(T1, 2, timeout_s=0.01)
        assert snapshot.timed_out is False
        assert snapshot.terminal_seq == 2
        assert snapshot.events == ()  # nothing beyond the terminal


class TestEngineIntegration:
    @mark.asyncio
    async def test_engine_drives_repository_to_terminal(self):
        repo = MemoryRunRepository()
        await repo.create(T1)
        await repo.commit(
            T1,
            expected_state=RunState.ACCEPTED,
            next_state=RunState.GUARDING,
            event_type=SSEEventType.PROCESS_STATUS,
        )
        await repo.commit(
            T1,
            expected_state=RunState.GUARDING,
            next_state=RunState.COMPLETED,
            event_type=SSEEventType.RUN_COMPLETED,
        )
        frames = [
            f
            async for f in stream_engine(
                wait_page=lambda cursor, timeout: repo.snapshot(T1, cursor, timeout),
                heartbeat_s=0.5,
            )
        ]
        ids = [f.split("\n", 1)[0] for f in frames]
        assert ids == ["id: 1", "id: 2", "id: 3"]
        assert frames[-1].startswith("id: 3\nevent: run.completed")

    @mark.asyncio
    async def test_engine_heartbeats_while_idle_then_completes(self):
        repo = MemoryRunRepository()
        await repo.create(T1)

        async def finish_later():
            await asyncio.sleep(0.05)
            await repo.commit(
                T1,
                expected_state=RunState.ACCEPTED,
                next_state=RunState.COMPLETED,
                event_type=SSEEventType.RUN_COMPLETED,
            )

        task = asyncio.create_task(finish_later())
        frames = [
            f
            async for f in stream_engine(
                wait_page=lambda cursor, timeout: repo.snapshot(T1, cursor, timeout),
                heartbeat_s=0.01,  # short idle window → at least one keep-alive
            )
        ]
        await task
        assert frames[0].startswith("id: 1")
        assert ": keep-alive\n\n" in frames
        assert frames[-1].startswith("id: 2\nevent: run.completed")

    @mark.asyncio
    async def test_repository_missing_run_is_a_structured_fault(self):
        repo = MemoryRunRepository()

        async def wait_page(cursor, timeout):
            return await repo.snapshot(T1_OTHER_RUN, cursor, timeout)

        with pytest.raises(RunRepositoryError) as exc:
            async for _ in stream_engine(wait_page=wait_page, heartbeat_s=0.5):
                pass
        assert exc.value.fault is RunRepositoryFault.NOT_FOUND


class FakeRedis:
    """Redis duck whose eval() runs python twins of the repository scripts."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self.unavailable = False
        self._wake = threading.Event()  # emulates XREAD BLOCK wake-up

    def _fail(self):
        if self.unavailable:
            raise ConnectionError("redis down")

    def _next_id(self, name: str) -> str:
        return f"{len(self.streams.get(name, [])) + 1}-0"

    def _twin_create(self, keys, args):
        state_key, stream_key = keys
        if state_key in self.hashes:
            return 0
        self.hashes[state_key] = {
            "state": args[0],
            "tenant_id": args[1],
            "device_id": args[2],
            "session_id": args[3],
            "latest_seq": "1",
        }
        self.streams.setdefault(stream_key, []).append(
            (self._next_id(stream_key), {"seq": "1", "event": args[5], "data": args[6]})
        )
        return 1

    def _twin_commit(self, keys, args):
        state_key, stream_key = keys
        fields = self.hashes.get(state_key)
        if fields is None:
            return -1
        if fields["state"] != args[1]:
            return 0
        seq = int(fields["latest_seq"]) + 1
        self.streams.setdefault(stream_key, []).append(
            (
                self._next_id(stream_key),
                {"seq": str(seq), "event": args[3], "data": args[4]},
            )
        )
        fields["state"] = args[2]
        fields["latest_seq"] = str(seq)
        if args[3] == "run.completed":
            fields["terminal_seq"] = str(seq)
        self._wake.set()
        return seq

    def eval(self, script, numkeys, *keys_and_args):
        self._fail()
        keys = list(keys_and_args[:numkeys])
        args = list(keys_and_args[numkeys:])
        if script == _LUA_CREATE:
            return self._twin_create(keys, args)
        if script == _LUA_COMMIT:
            return self._twin_commit(keys, args)
        raise AssertionError("fake only understands the repository scripts")

    def hgetall(self, name):
        self._fail()
        return dict(self.hashes.get(name, {}))

    def xrange(self, name, start="-", end="+"):
        self._fail()
        return list(self.streams.get(name, []))

    def xread(self, streams, count=None, block=None):
        self._fail()
        name, after = next(iter(streams.items()))
        # block like the server: clear any prior signal, then wake on the next
        # commit or after the timeout
        self._wake.clear()
        self._wake.wait((block or 1) / 1000)
        entries = [e for e in self.streams.get(name, []) if e[0] > after]
        return [(name, entries[:count])] if entries else None

    def delete(self, *names):
        self._fail()
        removed = 0
        for name in names:
            removed += 1 if self.hashes.pop(name, None) is not None else 0
            removed += 1 if self.streams.pop(name, None) is not None else 0
        return removed


class TestRedisRepository:
    def _repo(self, redis=None):
        return RedisRunRepository(redis or FakeRedis())

    @mark.asyncio
    async def test_create_and_commit_are_atomic(self):
        redis = FakeRedis()
        repo = self._repo(redis)
        assert await repo.create(T1) == 1
        seq = await repo.commit(
            T1,
            expected_state=RunState.ACCEPTED,
            next_state=RunState.COMPLETED,
            event_type=SSEEventType.RUN_COMPLETED,
            data={"status": "completed"},
        )
        assert seq == 2
        snapshot = await repo.snapshot(T1, 0, 0.01)
        assert [e.seq for e in snapshot.events] == [1, 2]
        assert snapshot.state is RunState.COMPLETED
        assert snapshot.terminal_seq == 2

    @mark.asyncio
    async def test_cas_conflict_writes_nothing(self):
        redis = FakeRedis()
        repo = self._repo(redis)
        await repo.create(T1)
        with pytest.raises(RunRepositoryError) as exc:
            await repo.commit(
                T1,
                expected_state=RunState.GUARDING,
                next_state=RunState.COMPLETED,
                event_type=SSEEventType.RUN_COMPLETED,
            )
        assert exc.value.fault is RunRepositoryFault.CAS_CONFLICT
        _, stream_key = repo._keys(T1.run_id)
        assert len(redis.streams[stream_key]) == 1  # nothing appended
        assert redis.hashes[repo._keys(T1.run_id)[0]]["state"] == "ACCEPTED"

    @mark.asyncio
    async def test_missing_run_is_not_found(self):
        repo = self._repo()
        with pytest.raises(RunRepositoryError) as exc:
            await repo.commit(
                T1,
                expected_state=RunState.ACCEPTED,
                next_state=RunState.GUARDING,
                event_type=SSEEventType.PROCESS_STATUS,
            )
        assert exc.value.fault is RunRepositoryFault.NOT_FOUND

    @mark.asyncio
    async def test_tenant_mismatch_reads_as_absent(self):
        repo = self._repo()
        await repo.create(T1)
        with pytest.raises(RunRepositoryError) as exc:
            await repo.snapshot(T2_SAME_RUN, 0, 0.01)
        assert exc.value.fault is RunRepositoryFault.NOT_FOUND

    @mark.asyncio
    async def test_unavailable_client_is_explicit(self):
        redis = FakeRedis()
        redis.unavailable = True
        repo = self._repo(redis)
        with pytest.raises(RunRepositoryError) as exc:
            await repo.create(T1)
        assert exc.value.fault is RunRepositoryFault.UNAVAILABLE

    @mark.asyncio
    async def test_blocking_read_returns_after_commit(self):
        redis = FakeRedis()
        repo = self._repo(redis)
        await repo.create(T1)

        async def commit_later():
            await asyncio.sleep(0.02)
            await repo.commit(
                T1,
                expected_state=RunState.ACCEPTED,
                next_state=RunState.GUARDING,
                event_type=SSEEventType.PROCESS_STATUS,
            )

        task = asyncio.create_task(commit_later())
        snapshot = await repo.snapshot(T1, 1, timeout_s=0.5)
        await task
        assert [e.seq for e in snapshot.events] == [1, 2]
        assert snapshot.state is RunState.GUARDING
