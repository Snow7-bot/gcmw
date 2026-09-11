"""Tests for the internal SSE stream engine (issue #65B — slice B-1).

The engine consumes an injected ASYNC SNAPSHOT reader, so these tests use a
scripted fake — no Redis, no polling timers, no real sleeps. Acceptance list
from the review round:

- terminal replay ``[accepted, completed]`` delivers BOTH frames (an
  authoritative terminal state must never truncate a page);
- page size 1 still reaches the terminal frame (pagination-safe);
- ``after_seq=5`` with ``oldest_available_seq=9`` is ``stale_cursor``;
- a missing seq INSIDE the window is ``replay_gap``;
- out-of-order seqs are ``out_of_order``;
- events arriving immediately produce NO heartbeat;
- a wait timeout with no events produces exactly one heartbeat;
- a terminal state without its terminal event raises the storage-invariant
  fault;
- the engine never mutates run state (no cancellation policy).

Real network-disconnect verification belongs to the public route in #65B-2 and
is deliberately NOT claimed here.
"""

import pytest
from pytest import mark

from app.api.v1.sse_stream import (
    SSEStreamError,
    StreamFault,
    StreamSnapshot,
    effective_after_seq,
    frame,
    keep_alive,
    stream_engine,
)
from app.contracts.events import SSEEvent, SSEEventType
from app.contracts.run import RunState
from app.main import app as real_app

HEARTBEAT_S = 1.0


def _event(seq: int, event=SSEEventType.PROCESS_STATUS, **data) -> SSEEvent:
    defaults = {
        SSEEventType.RUN_ACCEPTED: {"status": "accepted", "message": "问题已接收"},
        SSEEventType.PROCESS_STATUS: {"stage": "guarding", "message": "处理中"},
        SSEEventType.RUN_COMPLETED: {"status": "completed"},
    }
    payload = dict(defaults.get(event, {}))
    payload.update(data)
    return SSEEvent(
        seq=seq,
        tenant_id="t1",
        device_id="d1",
        session_id="s1",
        run_id="r1",
        layer="process",
        event=event,
        data=payload,
    )


ACCEPTED = _event(1, SSEEventType.RUN_ACCEPTED)
COMPLETED = _event(2, SSEEventType.RUN_COMPLETED)


class FakeReader:
    """Scripted snapshot reader: each call returns the next snapshot."""

    def __init__(self, snapshots) -> None:
        self._snapshots = list(snapshots)
        self.calls: list[tuple[int, float]] = []

    async def __call__(self, cursor: int, timeout_s: float) -> StreamSnapshot:
        self.calls.append((cursor, timeout_s))
        if not self._snapshots:
            return StreamSnapshot(
                state=RunState.ACCEPTED, latest_seq=cursor, timed_out=True
            )
        return self._snapshots.pop(0)


def _snapshot(
    events=(),
    state=RunState.ACCEPTED,
    oldest=None,
    latest=None,
    timed_out=False,
) -> StreamSnapshot:
    seqs = [e.seq for e in events]
    return StreamSnapshot(
        events=tuple(events),
        state=state,
        oldest_available_seq=oldest
        if oldest is not None
        else (min(seqs) if seqs else 0),
        latest_seq=latest if latest is not None else (max(seqs) if seqs else 0),
        timed_out=timed_out,
    )


class TestCursorCombination:
    def test_last_event_id_and_query_take_max(self):
        assert effective_after_seq(0, "3") == 3
        assert effective_after_seq(5, "3") == 5

    def test_malformed_or_negative_header_ignored(self):
        assert effective_after_seq(4, "abc") == 4
        assert effective_after_seq(4, "  ") == 4
        assert effective_after_seq(4, "-2") == 4

    def test_frame_and_keep_alive_shapes(self):
        text = frame(COMPLETED)
        assert text.startswith("id: 2\nevent: run.completed\ndata: {")
        assert text.endswith("\n\n")
        assert keep_alive() == ": keep-alive\n\n"
        assert "id:" not in keep_alive()


class TestTerminalReplay:
    @mark.asyncio
    async def test_full_terminal_replay_delivers_both_frames(self):
        reader = FakeReader(
            [_snapshot([ACCEPTED, COMPLETED], state=RunState.COMPLETED)]
        )
        frames = [f async for f in stream_engine(wait_page=reader)]
        assert [f.split("\n", 1)[0] for f in frames] == ["id: 1", "id: 2"]

    @mark.asyncio
    async def test_page_size_one_still_reaches_terminal(self):
        reader = FakeReader(
            [
                _snapshot([ACCEPTED], latest=2),
                _snapshot([COMPLETED], state=RunState.COMPLETED, latest=2),
            ]
        )
        frames = [f async for f in stream_engine(wait_page=reader)]
        assert [f.split("\n", 1)[0] for f in frames] == ["id: 1", "id: 2"]
        assert reader.calls[0][0] == 0 and reader.calls[1][0] == 1

    @mark.asyncio
    async def test_state_close_only_after_latest_seq_reached(self):
        # terminal state but latest_seq ahead of the page: keep reading
        reader = FakeReader(
            [
                _snapshot([ACCEPTED], state=RunState.COMPLETED, latest=2),
                _snapshot([COMPLETED], state=RunState.COMPLETED),
            ]
        )
        frames = [f async for f in stream_engine(wait_page=reader)]
        assert [f.split("\n", 1)[0] for f in frames] == ["id: 1", "id: 2"]

    @mark.asyncio
    async def test_terminal_state_without_terminal_event_reports_invariant(self):
        reader = FakeReader([_snapshot([ACCEPTED], state=RunState.COMPLETED)])
        with pytest.raises(SSEStreamError) as exc:
            async for _ in stream_engine(wait_page=reader):
                pass
        assert exc.value.fault is StreamFault.MISSING_TERMINAL_EVENT

    @mark.asyncio
    async def test_completed_event_ends_stream_even_before_latest_seq(self):
        reader = FakeReader([_snapshot([ACCEPTED, COMPLETED], latest=5)])
        frames = [f async for f in stream_engine(wait_page=reader)]
        assert frames[-1].startswith("id: 2")


class TestSequenceContinuity:
    @mark.asyncio
    async def test_stale_cursor_when_window_trimmed(self):
        # after_seq=5, oldest retained = 9 -> the cursor fell out of the window
        reader = FakeReader([_snapshot([_event(9)], oldest=9, latest=9)])
        with pytest.raises(SSEStreamError) as exc:
            async for _ in stream_engine(wait_page=reader, after_seq=5):
                pass
        assert exc.value.fault is StreamFault.STALE_CURSOR

    @mark.asyncio
    async def test_replay_gap_inside_the_window(self):
        reader = FakeReader(
            [_snapshot([_event(1, SSEEventType.RUN_ACCEPTED), _event(3)], oldest=1)]
        )
        with pytest.raises(SSEStreamError) as exc:
            async for _ in stream_engine(wait_page=reader):
                pass
        assert exc.value.fault is StreamFault.REPLAY_GAP

    @mark.asyncio
    async def test_out_of_order_after_cursor(self):
        # non-terminal page so the ordering fault is reached (a terminal event
        # legitimately ends the page and the stream)
        page = [
            ACCEPTED,
            _event(2, SSEEventType.PROCESS_STATUS),
            ACCEPTED,  # seq 1 reappears after cursor advanced to 2
        ]
        reader = FakeReader([_snapshot(page, oldest=1, latest=2)])
        with pytest.raises(SSEStreamError) as exc:
            async for _ in stream_engine(wait_page=reader):
                pass
        assert exc.value.fault is StreamFault.OUT_OF_ORDER

    @mark.asyncio
    async def test_exact_reread_of_last_seq_is_tolerated(self):
        reader = FakeReader(
            [
                _snapshot([ACCEPTED], latest=1),
                _snapshot([ACCEPTED, COMPLETED], state=RunState.COMPLETED, latest=2),
            ]
        )
        frames = [f async for f in stream_engine(wait_page=reader)]
        assert [f.split("\n", 1)[0] for f in frames] == ["id: 1", "id: 2"]

    @mark.asyncio
    async def test_resume_after_terminal_does_not_resend(self):
        # cursor already at latest_seq and the run is terminal: close silently
        reader = FakeReader([_snapshot(state=RunState.COMPLETED, latest=2)])
        frames = [f async for f in stream_engine(wait_page=reader, after_seq=2)]
        assert frames == []  # nothing re-sent, no duplicate terminal frame


class TestHeartbeatTiming:
    @mark.asyncio
    async def test_events_arriving_immediately_never_heartbeat(self):
        reader = FakeReader(
            [
                _snapshot([ACCEPTED], latest=1),
                _snapshot([COMPLETED], state=RunState.COMPLETED, latest=2),
            ]
        )
        frames = [f async for f in stream_engine(wait_page=reader)]
        assert all(f != keep_alive() for f in frames)
        assert len(frames) == 2

    @mark.asyncio
    async def test_one_heartbeat_per_idle_timeout_then_events(self):
        reader = FakeReader(
            [
                _snapshot(timed_out=True),
                _snapshot(timed_out=True),
                _snapshot([ACCEPTED, COMPLETED], state=RunState.COMPLETED),
            ]
        )
        frames = [
            f async for f in stream_engine(wait_page=reader, heartbeat_s=HEARTBEAT_S)
        ]
        assert frames == [keep_alive(), keep_alive(), frame(ACCEPTED), frame(COMPLETED)]
        assert [c[1] for c in reader.calls] == [HEARTBEAT_S] * 3  # one timeout knob
        assert [c[0] for c in reader.calls] == [0, 0, 0]  # cursor unchanged while idle


class TestNoSideEffects:
    @mark.asyncio
    async def test_reader_failure_propagates_untouched(self):
        class Broken:
            async def __call__(self, cursor, timeout_s):
                raise RuntimeError("storage unavailable")

        with pytest.raises(RuntimeError):
            async for _ in stream_engine(wait_page=Broken()):
                pass

    @mark.asyncio
    async def test_two_subscribers_are_independent(self):
        def reader():
            return FakeReader(
                [
                    _snapshot([ACCEPTED], latest=2),
                    _snapshot([COMPLETED], state=RunState.COMPLETED, latest=2),
                ]
            )

        first = stream_engine(wait_page=reader())
        second = stream_engine(wait_page=reader())
        assert (await anext(first)).startswith("id: 1")
        await first.aclose()
        assert (await anext(second)).startswith("id: 1")
        await second.aclose()


class TestPublicSurface:
    def test_no_public_stream_route_yet(self):
        """The HTTP route is #65B-2; this slice is engine-only."""
        paths = set(real_app.openapi().get("paths", {}))
        assert not any("events/stream" in path for path in paths)

    def test_wait_page_timeout_is_engine_configuration(self):
        import inspect

        params = inspect.signature(stream_engine).parameters
        assert params["heartbeat_s"].kind is inspect.Parameter.KEYWORD_ONLY
        assert "poll_s" not in params  # polling (and its clock) is gone
        assert "max_idle_windows" not in params
