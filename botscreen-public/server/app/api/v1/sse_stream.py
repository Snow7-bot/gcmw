"""Internal SSE stream engine (issue #65B — slice B-1, engine only).

Reviewer-narrowed scope (round 2): this slice defines the ASYNC SNAPSHOT READ
INTERFACE and the stream algorithm, plus a fake implementation in tests. It
ships no Redis wiring and no public HTTP route — those are #65B-2.

Read interface (the only way the engine learns about a run):

    await wait_page(cursor, timeout_s) -> StreamSnapshot(
        events,               # ordered events with seq > cursor
        state,                # authoritative run state
        oldest_available_seq, # first seq still retained (0 when empty)
        latest_seq,           # newest seq the store holds (0 when empty)
        timed_out,            # no event arrived within timeout_s
    )

A blocking async read replaces polling entirely: there is no fixed poll
interval, no injected clock and no idle-window counter. Heartbeats are emitted
ONLY when the read times out with no events; an event that arrives immediately
never triggers a keep-alive.

Stream rules:
- frames carry ``id: <seq>``; the client resumes with ``Last-Event-ID``;
- **only a terminal EVENT (``run.completed``) ends page emission** — an
  authoritative terminal state never truncates a page mid-way;
- while ``cursor < latest_seq`` the engine keeps reading (pagination-safe,
  even with page size 1);
- only after the page is drained AND ``cursor`` has reached ``latest_seq`` may
  the stream close on the authoritative terminal state;
- a terminal state whose expected terminal event is missing from the retained
  window is reported explicitly (storage invariant fault), never silently
  accepted;
- continuity uses the window bounds: cursor before the retained window →
  ``stale_cursor``; a missing seq inside the window → ``replay_gap``; an
  older seq after the cursor → ``out_of_order``; an exact re-read of the last
  emitted seq is the only tolerated overlap;
- the engine holds NO cancellation policy and never mutates run state, so an
  internal read failure can never cancel a run.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from app.contracts.errors import ErrorCode
from app.contracts.events import SSEEvent, SSEEventType
from app.contracts.run import TERMINAL_STATES, RunState

DEFAULT_HEARTBEAT_MS = 15_000


class StreamFault(str, Enum):
    """Protocol/storage faults, each independently reportable."""

    REPLAY_GAP = "replay_gap"
    OUT_OF_ORDER = "out_of_order"
    STALE_CURSOR = "stale_cursor"
    MISSING_TERMINAL_EVENT = "missing_terminal_event"


class SSEStreamError(RuntimeError):
    """Structured streaming fault (mapped by the #36 boundary)."""

    def __init__(self, fault: StreamFault, message: str = "") -> None:
        super().__init__(message or fault.value)
        self.fault = fault
        self.code = ErrorCode.INTERNAL_UNKNOWN


@dataclass(frozen=True)
class StreamSnapshot:
    """One atomic read result: events plus the store's window bounds."""

    events: tuple[SSEEvent, ...] = ()
    state: RunState = RunState.ACCEPTED
    oldest_available_seq: int = 0
    latest_seq: int = 0
    timed_out: bool = False


SnapshotReader = Callable[[int, float], Awaitable[StreamSnapshot]]


def frame(event: SSEEvent) -> str:
    """Serialize one event as an SSE frame (``id`` = seq for resume)."""
    payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
    return f"id: {event.seq}\nevent: {event.event.value}\ndata: {payload}\n\n"


def keep_alive() -> str:
    """SSE comment frame: no ``id``, so it never perturbs seq authority."""
    return ": keep-alive\n\n"


def effective_after_seq(after_seq: int, last_event_id: str | None) -> int:
    """Combine the cursor and the Last-Event-ID header (max wins).

    A malformed or negative header is ignored: a bad resume id must never break
    the stream, it just falls back to the explicit cursor.
    """
    cursor = max(int(after_seq), 0)
    if last_event_id:
        try:
            candidate = int(last_event_id.strip())
        except (TypeError, ValueError):
            return cursor
        if candidate > 0:
            cursor = max(cursor, candidate)
    return cursor


def _validate(event: SSEEvent, cursor: int, snapshot: StreamSnapshot) -> None:
    """Strict continuity, classified from the store's window bounds."""
    if event.seq == cursor:
        return  # exact re-read of the last emitted seq: tolerated overlap
    if event.seq < cursor:
        raise SSEStreamError(
            StreamFault.OUT_OF_ORDER,
            f"out_of_order: seq {event.seq} < cursor {cursor}",
        )
    if event.seq != cursor + 1:
        first_expected = cursor + 1
        if snapshot.oldest_available_seq > first_expected:
            raise SSEStreamError(
                StreamFault.STALE_CURSOR,
                f"stale_cursor: cursor {cursor} precedes retained window "
                f"(oldest {snapshot.oldest_available_seq})",
            )
        raise SSEStreamError(
            StreamFault.REPLAY_GAP,
            f"replay_gap: expected seq {first_expected}, got {event.seq}",
        )


async def stream_engine(
    *,
    wait_page: SnapshotReader,
    after_seq: int = 0,
    heartbeat_s: float = DEFAULT_HEARTBEAT_MS / 1000,
) -> AsyncIterator[str]:
    """Yield SSE frames for one run until its terminal event is delivered.

    ``wait_page`` blocks (async) until events are available or ``heartbeat_s``
    elapses; the engine never polls, never cancels runs and never writes state.
    """
    cursor = max(after_seq, 0)
    while True:
        snapshot = await wait_page(cursor, heartbeat_s)
        if snapshot.timed_out and not snapshot.events:
            # idle for a full heartbeat window: one keep-alive, keep waiting
            yield keep_alive()
            continue
        drained = False
        for event in snapshot.events:
            _validate(event, cursor, snapshot)
            if event.seq == cursor:
                continue
            drained = True
            cursor = event.seq
            yield frame(event)
            if event.event is SSEEventType.RUN_COMPLETED:
                return  # terminal EVENT only — never truncated by state
        if cursor < snapshot.latest_seq:
            continue  # pagination: keep reading until we reach latest_seq
        if snapshot.state in TERMINAL_STATES:
            if drained and not _saw_terminal(snapshot):
                raise SSEStreamError(
                    StreamFault.MISSING_TERMINAL_EVENT,
                    f"storage invariant: state {snapshot.state.value} at "
                    f"latest seq {snapshot.latest_seq} without a terminal event",
                )
            return


def _saw_terminal(snapshot: StreamSnapshot) -> bool:
    return any(e.event is SSEEventType.RUN_COMPLETED for e in snapshot.events)


__all__ = [
    "DEFAULT_HEARTBEAT_MS",
    "SSEStreamError",
    "SnapshotReader",
    "StreamFault",
    "StreamSnapshot",
    "effective_after_seq",
    "frame",
    "keep_alive",
    "stream_engine",
]
