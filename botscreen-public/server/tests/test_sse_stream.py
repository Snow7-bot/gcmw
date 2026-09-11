"""Tests for the internal SSE stream engine (issue #65B — slice B-1).

The engine consumes an injected async snapshot reader; these tests use a
scripted fake — no Redis, no polling timers, no real sleeps. Acceptance list
from the review round:

- terminal replay ``[accepted, completed]`` delivers BOTH frames;
- page size 1 still reaches the terminal frame;
- a cursor already at ``terminal_seq`` closes silently (no duplicate terminal);
- ``timed_out`` invariants: no events, non-terminal state, ``latest == cursor``;
- a terminal state without ``terminal_seq`` fails explicitly (including on a
  reconnect that stops at a non-terminal seq);
- ``terminal_seq < latest_seq`` fails; a terminal EVENT before ``latest_seq``
  fails; a terminal event with a non-terminal state fails;
- ``cursor > latest_seq`` fails with ``cursor_ahead``;
- an empty non-timeout snapshot fails (no busy spin);
- window bounds and event-in-window are enforced;
- stale/gap/out-of-order are classified from the window bounds;
- events arriving immediately produce NO heartbeat; a validated timeout emits
  exactly one;
- invalid ``heartbeat_s`` (non-positive, NaN, infinity, wrong type) is rejected
  when the stream is created, before any read;
- the engine never mutates run state.

Real network-disconnect verification belongs to the public route in #65B-2 and
is deliberately not claimed here.
"""

import math

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


_AUTO = object()


def snap(
    events=(),
    *,
    state=RunState.ACCEPTED,
    oldest=None,
    latest=None,
    terminal=_AUTO,
    timed_out=False,
) -> StreamSnapshot:
    """Build a snapshot; terminal states auto-fill a legal terminal_seq unless
    an explicit value (including explicit ``None``) is provided."""
    seqs = [e.seq for e in events]
    resolved_latest = latest if latest is not None else (max(seqs) if seqs else 0)
    if oldest is not None:
        resolved_oldest = oldest
    elif seqs:
        resolved_oldest = min(seqs)
    else:
        # no events: a non-empty window still starts at >= 1 (0 only for empty)
        resolved_oldest = max(0, resolved_latest)
    if terminal is _AUTO:
        terminal = (
            resolved_latest
            if state in {RunState.COMPLETED, RunState.CANCELLED}
            else None
        )
    return StreamSnapshot(
        events=tuple(events),
        state=state,
        oldest_available_seq=resolved_oldest,
        latest_seq=resolved_latest,
        terminal_seq=terminal,
        timed_out=timed_out,
    )


class FakeReader:
    def __init__(self, snapshots) -> None:
        self._snapshots = list(snapshots)
        self.calls: list[tuple[int, float]] = []
        self.reads = 0

    async def __call__(self, cursor: int, timeout_s: float) -> StreamSnapshot:
        self.calls.append((cursor, timeout_s))
        self.reads += 1
        if not self._snapshots:
            return snap(timed_out=True, latest=cursor)
        return self._snapshots.pop(0)


async def _collect(reader, **kwargs):
    return [f async for f in stream_engine(wait_page=reader, **kwargs)]


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


class TestHeartbeatValidation:
    @pytest.mark.parametrize("bad", [0, -1, -0.5, math.nan, math.inf, -math.inf])
    def test_non_positive_or_non_finite_rejected_at_creation(self, bad):
        with pytest.raises(ValueError):
            stream_engine(wait_page=FakeReader([]), heartbeat_s=bad)

    @pytest.mark.parametrize("bad", ["1", None, True])
    def test_wrong_type_rejected_at_creation(self, bad):
        with pytest.raises(TypeError):
            stream_engine(wait_page=FakeReader([]), heartbeat_s=bad)

    def test_valid_window_creates_stream(self):
        stream = stream_engine(wait_page=FakeReader([]), heartbeat_s=0.5)
        assert hasattr(stream, "__anext__")


class TestTerminalReplay:
    @mark.asyncio
    async def test_full_terminal_replay_delivers_both_frames(self):
        reader = FakeReader(
            [snap([ACCEPTED, COMPLETED], state=RunState.COMPLETED, latest=2)]
        )
        frames = await _collect(reader)
        assert [f.split("\n", 1)[0] for f in frames] == ["id: 1", "id: 2"]

    @mark.asyncio
    async def test_page_size_one_still_reaches_terminal(self):
        reader = FakeReader(
            [
                snap([ACCEPTED], latest=2),
                snap([COMPLETED], state=RunState.COMPLETED, latest=2),
            ]
        )
        frames = await _collect(reader)
        assert [f.split("\n", 1)[0] for f in frames] == ["id: 1", "id: 2"]
        assert [c[0] for c in reader.calls] == [0, 1]

    @mark.asyncio
    async def test_cursor_at_terminal_closes_silently(self):
        reader = FakeReader([snap(state=RunState.COMPLETED, latest=2, terminal=2)])
        frames = await _collect(reader, after_seq=2)
        assert frames == []  # client already has the terminal frame

    @mark.asyncio
    async def test_cursor_before_terminal_keeps_paging(self):
        reader = FakeReader(
            [
                snap([ACCEPTED], state=RunState.COMPLETED, latest=2, terminal=2),
                snap([COMPLETED], state=RunState.COMPLETED, latest=2, terminal=2),
            ]
        )
        frames = await _collect(reader, after_seq=0)
        assert [f.split("\n", 1)[0] for f in frames] == ["id: 1", "id: 2"]

    @mark.asyncio
    async def test_terminal_event_before_latest_seq_fails(self):
        # the reviewer's inversion: completed at 2 with latest 5 must NOT pass
        reader = FakeReader(
            [snap([ACCEPTED, COMPLETED], state=RunState.COMPLETED, latest=5)]
        )
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader)
        assert exc.value.fault is StreamFault.SNAPSHOT_INCONSISTENT

    @mark.asyncio
    async def test_terminal_event_with_non_terminal_state_fails(self):
        reader = FakeReader([snap([ACCEPTED, COMPLETED], latest=2)])
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader)
        assert exc.value.fault is StreamFault.SNAPSHOT_INCONSISTENT


class TestTerminalInvariants:
    @mark.asyncio
    async def test_terminal_state_without_terminal_seq_fails(self):
        reader = FakeReader(
            [snap([ACCEPTED], state=RunState.COMPLETED, latest=1, terminal=None)]
        )
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader)
        assert exc.value.fault is StreamFault.MISSING_TERMINAL_EVENT

    @mark.asyncio
    async def test_reconnect_at_non_terminal_seq_with_missing_terminal_fails(self):
        # after_seq=1, state COMPLETED, latest=1, empty page, no terminal_seq
        reader = FakeReader([snap(state=RunState.COMPLETED, latest=1, terminal=None)])
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader, after_seq=1)
        assert exc.value.fault is StreamFault.MISSING_TERMINAL_EVENT

    @mark.asyncio
    async def test_terminal_seq_before_latest_seq_fails(self):
        reader = FakeReader([snap(state=RunState.COMPLETED, latest=5, terminal=2)])
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader)
        assert exc.value.fault is StreamFault.SNAPSHOT_INCONSISTENT

    @mark.asyncio
    async def test_non_terminal_state_with_terminal_seq_fails(self):
        reader = FakeReader([snap([ACCEPTED], latest=2, terminal=2)])
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader)
        assert exc.value.fault is StreamFault.SNAPSHOT_INCONSISTENT


class TestSnapshotValidation:
    @mark.asyncio
    async def test_cursor_ahead_fails(self):
        reader = FakeReader([snap([ACCEPTED], latest=1)])
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader, after_seq=9)
        assert exc.value.fault is StreamFault.CURSOR_AHEAD

    @mark.asyncio
    async def test_empty_non_timeout_snapshot_fails(self):
        reader = FakeReader([snap(latest=3)])  # empty, not timed out, no progress
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader)
        assert exc.value.fault is StreamFault.SNAPSHOT_INCONSISTENT
        assert reader.reads == 1  # no busy spin

    @mark.asyncio
    async def test_window_bounds_invalid_fails(self):
        reader = FakeReader([snap(latest=3, oldest=5)])
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader)
        assert exc.value.fault is StreamFault.SNAPSHOT_INCONSISTENT

    @mark.asyncio
    async def test_event_outside_window_fails(self):
        reader = FakeReader([snap([_event(11)], oldest=1, latest=9)])
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader)
        assert exc.value.fault is StreamFault.SNAPSHOT_INCONSISTENT

    @mark.asyncio
    async def test_stale_cursor_when_window_trimmed(self):
        reader = FakeReader([snap([_event(9)], oldest=9, latest=9)])
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader, after_seq=5)
        assert exc.value.fault is StreamFault.STALE_CURSOR

    @mark.asyncio
    async def test_replay_gap_inside_the_window(self):
        reader = FakeReader([snap([ACCEPTED, _event(3)], oldest=1, latest=3)])
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader)
        assert exc.value.fault is StreamFault.REPLAY_GAP

    @mark.asyncio
    async def test_out_of_order_after_cursor(self):
        page = [ACCEPTED, _event(2), ACCEPTED]
        reader = FakeReader([snap(page, oldest=1, latest=2)])
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader)
        assert exc.value.fault is StreamFault.OUT_OF_ORDER

    @mark.asyncio
    async def test_exact_reread_of_last_seq_is_tolerated(self):
        reader = FakeReader(
            [
                snap([ACCEPTED], latest=1),
                snap([ACCEPTED, COMPLETED], state=RunState.COMPLETED, latest=2),
            ]
        )
        frames = await _collect(reader)
        assert [f.split("\n", 1)[0] for f in frames] == ["id: 1", "id: 2"]


class TestNoProgressAndZeroSemantics:
    @mark.asyncio
    async def test_duplicate_only_page_is_not_progress(self):
        # cursor=1 with a page that only repeats seq 1 must NOT spin silently
        reader = FakeReader([snap([ACCEPTED], oldest=1, latest=2)])
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader, after_seq=1)
        assert exc.value.fault is StreamFault.SNAPSHOT_INCONSISTENT
        assert reader.reads == 1  # no busy loop

    @mark.asyncio
    async def test_terminal_seq_zero_is_not_a_real_event(self):
        # seq 0 cannot exist: COMPLETED with latest=0/terminal_seq=0 must fail
        reader = FakeReader(
            [snap(state=RunState.COMPLETED, latest=0, oldest=0, terminal=0)]
        )
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader)
        assert exc.value.fault in {
            StreamFault.MISSING_TERMINAL_EVENT,
            StreamFault.SNAPSHOT_INCONSISTENT,
        }

    @mark.asyncio
    async def test_non_empty_window_must_start_at_one(self):
        reader = FakeReader([snap([_event(1)], oldest=0, latest=1)])
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader)
        assert exc.value.fault is StreamFault.SNAPSHOT_INCONSISTENT


class TestHeartbeatTiming:
    @mark.asyncio
    async def test_events_arriving_immediately_never_heartbeat(self):
        reader = FakeReader(
            [
                snap([ACCEPTED], latest=1),
                snap([COMPLETED], state=RunState.COMPLETED, latest=2),
            ]
        )
        frames = await _collect(reader, heartbeat_s=HEARTBEAT_S)
        assert frames == [frame(ACCEPTED), frame(COMPLETED)]

    @mark.asyncio
    async def test_one_heartbeat_per_idle_timeout(self):
        reader = FakeReader(
            [
                snap(timed_out=True, latest=0),
                snap(timed_out=True, latest=0),
                snap([ACCEPTED, COMPLETED], state=RunState.COMPLETED, latest=2),
            ]
        )
        frames = await _collect(reader, heartbeat_s=HEARTBEAT_S)
        assert frames == [keep_alive(), keep_alive(), frame(ACCEPTED), frame(COMPLETED)]
        assert [c[0] for c in reader.calls] == [0, 0, 0]  # cursor unchanged while idle
        assert {c[1] for c in reader.calls} == {HEARTBEAT_S}

    @mark.asyncio
    async def test_terminal_snapshot_with_timed_out_never_heartbeats(self):
        # invalid snapshot: a terminal run must not idle-wait (no heartbeat loop)
        reader = FakeReader(
            [snap(state=RunState.COMPLETED, latest=2, terminal=2, timed_out=True)]
        )
        with pytest.raises(SSEStreamError) as exc:
            await _collect(reader, after_seq=2)
        assert exc.value.fault is StreamFault.SNAPSHOT_INCONSISTENT
        assert reader.reads == 1  # no keep-alive cycle


class TestNoSideEffects:
    @mark.asyncio
    async def test_reader_failure_propagates_untouched(self):
        class Broken:
            async def __call__(self, cursor, timeout_s):
                raise RuntimeError("storage unavailable")

        with pytest.raises(RuntimeError):
            await _collect(Broken())

    @mark.asyncio
    async def test_two_subscribers_are_independent(self):
        def reader():
            return FakeReader(
                [
                    snap([ACCEPTED], latest=2),
                    snap([COMPLETED], state=RunState.COMPLETED, latest=2),
                ]
            )

        first = stream_engine(wait_page=reader(), heartbeat_s=HEARTBEAT_S)
        second = stream_engine(wait_page=reader(), heartbeat_s=HEARTBEAT_S)
        assert (await anext(first)).startswith("id: 1")
        await first.aclose()
        assert (await anext(second)).startswith("id: 1")
        await second.aclose()


class TestPublicSurface:
    def test_no_public_stream_route_yet(self):
        """The HTTP route is #65B-2; this slice is engine-only."""
        paths = set(real_app.openapi().get("paths", {}))
        assert not any("events/stream" in path for path in paths)

    def test_engine_takes_no_polling_knobs(self):
        import inspect

        params = inspect.signature(stream_engine).parameters
        assert "poll_s" not in params
        assert "max_idle_windows" not in params
        assert "heartbeat_s" in params
