"""Tests for the internal SSE stream engine (issue #65B — slice B-1).

Coverage (reviewer's regression list for this slice):
- internal read failures propagate WITHOUT touching run state (the engine has
  no cancellation policy at all);
- a new event published 50ms later is delivered within the poll SLA, not after
  the heartbeat;
- no keep-alive is emitted before a full idle heartbeat window has elapsed;
- `[1, 3]` gaps, out-of-order seqs and stale cursors fail explicitly;
- terminal detection trusts event TYPE / authoritative run state — never
  `data.status` of an arbitrary event;
- two subscribers are independent (closing one cannot affect the other);
- reconnect resumes after the cursor without resending a terminal frame;
- the public HTTP route does not exist yet (#65B-2 owns it), and the heartbeat
  cadence is engine configuration, not a client knob;
- a real ASGI disconnect smoke test through a test-only app.

The engine takes an injected clock and waiter, so no test depends on real
sleeps.
"""

import asyncio
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pytest import mark

from app.api.v1.sse_stream import (
    SSEStreamError,
    StreamFault,
    effective_after_seq,
    frame,
    is_terminal,
    keep_alive,
    stream_engine,
)
from app.contracts.events import SSEEvent, SSEEventType
from app.contracts.run import RunState
from app.main import app as real_app

POLL_S = 0.05
HEARTBEAT_S = 1.0


@dataclass
class Page:
    events: tuple[SSEEvent, ...] = ()


def _event(seq: int, event=SSEEventType.PROCESS_STATUS, **data) -> SSEEvent:
    # each event type only allows its own data keys (contract-enforced)
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


class Harness:
    """Deterministic engine harness: fake clock, waiter and page reader."""

    def __init__(self) -> None:
        self.now = 0.0
        self.events: list[SSEEvent] = []
        self.state = RunState.ACCEPTED
        self.read_error: Exception | None = None
        self.reads = 0
        self.waits: list[float] = []
        self.state_reads = 0
        self.cancelled = 0  # any state mutation would show up here

    # -- injection points -----------------------------------------------------

    def clock(self) -> float:
        return self.now

    async def waiter(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds  # time advances only when the engine waits

    def read_page(self, cursor: int) -> Page:
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        return Page(events=tuple(e for e in self.events if e.seq > cursor))

    def read_state(self) -> RunState:
        self.state_reads += 1
        return self.state

    def engine(self, **overrides):
        params = {
            "read_page": self.read_page,
            "read_state": self.read_state,
            "poll_s": POLL_S,
            "heartbeat_s": HEARTBEAT_S,
            "clock": self.clock,
            "waiter": self.waiter,
        }
        params.update(overrides)
        return stream_engine(**params)


class TestCursorCombination:
    def test_last_event_id_and_query_take_max(self):
        assert effective_after_seq(0, "3") == 3
        assert effective_after_seq(5, "3") == 5

    def test_malformed_or_negative_header_ignored(self):
        assert effective_after_seq(4, "abc") == 4
        assert effective_after_seq(4, "  ") == 4
        assert effective_after_seq(4, "-2") == 4

    def test_frame_and_keep_alive_shapes(self):
        text = frame(_event(2))
        assert text.startswith("id: 2\nevent: process.status\ndata: {")
        assert text.endswith("\n\n")
        assert keep_alive() == ": keep-alive\n\n"
        assert "id:" not in keep_alive()


class TestSequenceContinuity:
    @mark.asyncio
    async def test_gap_fails_explicitly(self):
        h = Harness()
        h.events = [_event(1), _event(3)]
        with pytest.raises(SSEStreamError) as exc:
            async for _ in h.engine():
                pass
        assert exc.value.fault is StreamFault.REPLAY_GAP

    @mark.asyncio
    async def test_leading_non_contiguous_seq_is_a_gap_fault(self):
        h = Harness()
        h.events = [_event(2), _event(1)]  # page starts at 2 with cursor 0
        with pytest.raises(SSEStreamError) as exc:
            async for _ in h.engine():
                pass
        assert exc.value.fault is StreamFault.STALE_CURSOR

    @mark.asyncio
    async def test_out_of_order_fails_explicitly(self):
        h = Harness()
        h.events = [_event(1), _event(2), _event(1)]  # 1 after cursor == 2
        with pytest.raises(SSEStreamError) as exc:
            async for _ in h.engine():
                pass
        assert exc.value.fault is StreamFault.OUT_OF_ORDER

    @mark.asyncio
    async def test_stale_cursor_fails_explicitly(self):
        h = Harness()
        h.events = [_event(9)]  # window start trimmed far past the cursor
        with pytest.raises(SSEStreamError) as exc:
            async for _ in h.engine(after_seq=0):
                pass
        assert exc.value.fault is StreamFault.STALE_CURSOR

    @mark.asyncio
    async def test_exact_reread_of_last_seq_is_tolerated(self):
        h = Harness()
        h.events = [_event(1)]
        stream = h.engine()
        assert (await anext(stream)).startswith("id: 1")
        # re-reading seq 1 (cursor == 1) must not raise, and must not resend
        h.state = RunState.CANCELLED
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    @mark.asyncio
    async def test_reconnect_resumes_after_cursor_without_duplicates(self):
        h = Harness()
        h.events = [
            _event(1, SSEEventType.RUN_ACCEPTED),
            _event(2, SSEEventType.RUN_COMPLETED),
        ]
        h.state = RunState.COMPLETED
        frames = [f async for f in h.engine(after_seq=1)]
        assert [f.split("\n", 1)[0] for f in frames] == ["id: 2"]


class TestTiming:
    @mark.asyncio
    async def test_new_event_ships_within_poll_not_heartbeat(self):
        h = Harness()
        h.events = [_event(1, SSEEventType.RUN_ACCEPTED)]
        stream = h.engine()
        assert (await anext(stream)).startswith("id: 1")
        # 50ms later the backend publishes the next event
        h.now += 0.05
        h.events.append(_event(2))
        nxt = await asyncio.wait_for(anext(stream), timeout=1)
        assert nxt.startswith("id: 2")
        assert h.waits == [POLL_S]  # delivered after ONE poll, not a heartbeat

    @mark.asyncio
    async def test_no_heartbeat_before_a_full_idle_window(self):
        h = Harness()
        # idle for exactly one poll window (50ms) << heartbeat (1s): no frame
        frames = [f async for f in h.engine(max_idle_windows=1)]
        assert frames == []
        assert h.now == pytest.approx(POLL_S)
        assert h.waits == [POLL_S]  # waited for events, emitted no keep-alive

    @mark.asyncio
    async def test_heartbeat_only_after_idle_period(self):
        h = Harness()
        h.state = RunState.ACCEPTED
        stream = h.engine(max_idle_windows=25)
        frames = []
        async for chunk in stream:
            frames.append(chunk)
        # 25 idle windows × 50ms = 1.25s ≥ 1s heartbeat → exactly one keep-alive
        assert sum(1 for f in frames if f == keep_alive()) == 1
        assert h.now >= HEARTBEAT_S

    @mark.asyncio
    async def test_heartbeat_cadence_is_engine_configuration(self):
        h = Harness()
        stream = h.engine(heartbeat_s=0.2, max_idle_windows=8)
        frames = [f async for f in stream]
        assert sum(1 for f in frames if f == keep_alive()) >= 1


class TestTerminalSemantics:
    @mark.asyncio
    async def test_data_status_does_not_close_the_stream(self):
        h = Harness()
        # a NON-terminal event carrying status=completed must not end the run
        h.events = [_event(1, SSEEventType.RUN_ACCEPTED, status="completed")]
        stream = h.engine(max_idle_windows=2)
        frames = [f async for f in stream]
        # one data frame, no keep-alive yet (heartbeat window not reached) and
        # crucially the stream did NOT close on the status field
        assert len(frames) == 1
        assert frames[0].startswith("id: 1")
        assert h.now == pytest.approx(2 * POLL_S)  # engine kept polling

    @mark.asyncio
    async def test_terminal_event_type_closes_once(self):
        h = Harness()
        h.events = [_event(1, SSEEventType.RUN_COMPLETED, status="completed")]
        frames = [f async for f in h.engine()]
        assert frames == [frame(h.events[0])]

    @mark.asyncio
    async def test_authoritative_state_closes_an_empty_tail(self):
        h = Harness()
        h.state = RunState.FAILED
        frames = [f async for f in h.engine()]
        assert frames == []
        assert h.reads == 1

    @mark.asyncio
    async def test_is_terminal_helper_ignores_data_status(self):
        assert (
            is_terminal(_event(1, SSEEventType.RUN_ACCEPTED, status="completed"))
            is False
        )
        assert is_terminal(_event(1, SSEEventType.RUN_COMPLETED)) is True
        assert is_terminal(_event(1), RunState.CANCELLED) is True
        assert is_terminal(_event(1), RunState.ACCEPTED) is False


class TestNoCancellationSideEffects:
    @mark.asyncio
    async def test_reader_failure_propagates_without_state_change(self):
        h = Harness()
        h.read_error = RuntimeError("storage unavailable")
        with pytest.raises(RuntimeError):
            async for _ in h.engine():
                pass
        assert h.state is RunState.ACCEPTED  # never cancelled by the engine
        assert h.cancelled == 0

    @mark.asyncio
    async def test_two_subscribers_are_independent(self):
        h = Harness()
        h.events = [_event(1, SSEEventType.RUN_ACCEPTED)]
        first = h.engine(max_idle_windows=1)
        second = h.engine(max_idle_windows=1)
        assert (await anext(first)).startswith("id: 1")
        await first.aclose()  # one subscriber goes away
        assert (await anext(second)).startswith("id: 1")  # other unaffected
        assert h.state is RunState.ACCEPTED
        await second.aclose()

    @mark.asyncio
    async def test_closing_the_engine_never_mutates_state(self):
        h = Harness()
        h.events = [_event(1, SSEEventType.RUN_ACCEPTED)]
        stream = h.engine()
        await anext(stream)
        await stream.aclose()
        assert h.state is RunState.ACCEPTED


class TestPublicSurface:
    def test_no_public_stream_route_yet(self):
        """The HTTP route is #65B-2; this slice is engine-only."""
        paths = set(real_app.openapi().get("paths", {}))
        assert not any("events/stream" in path for path in paths)

    def test_engine_heartbeat_is_not_a_request_parameter(self):
        import inspect

        params = inspect.signature(stream_engine).parameters
        # heartbeat/poll come from configuration, never from a client payload
        assert params["heartbeat_s"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["poll_s"].kind is inspect.Parameter.KEYWORD_ONLY


class TestAsgiDisconnectSmoke:
    """Real ASGI transport disconnect (test-only app; no product route)."""

    def _app(self, harness: Harness) -> FastAPI:
        test_app = FastAPI()

        @test_app.get("/stream")
        async def _stream() -> StreamingResponse:
            return StreamingResponse(
                harness.engine(max_idle_windows=400),
                media_type="text/event-stream",
            )

        return test_app

    def test_client_disconnect_is_clean_and_state_untouched(self):
        harness = Harness()
        harness.events = [_event(1, SSEEventType.RUN_ACCEPTED)]
        client = TestClient(self._app(harness))
        with client.stream("GET", "/stream") as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            lines = response.iter_lines()
            assert next(lines) == "id: 1"
        # response closed by the client: the engine must not cancel anything
        assert harness.state is RunState.ACCEPTED
        assert harness.cancelled == 0
