"""Internal SSE stream engine (issue #65B — slice B-1, engine only).

This slice owns the async snapshot READ INTERFACE, the snapshot invariants and
the stream algorithm. No Redis wiring, no public HTTP route (both #65B-2).

Read interface (single atomic snapshot, blocking):

    await wait_page(cursor, timeout_s) -> StreamSnapshot(
        events, state, oldest_available_seq, latest_seq, terminal_seq, timed_out)

Invariants are enforced BEFORE any branch is taken (``_validate_snapshot``):
``0 <= oldest_available_seq <= latest_seq``; ``cursor <= latest_seq``; every
event seq inside the declared window; ``timed_out`` implies no events, a
non-terminal state and ``latest_seq == cursor``; a terminal state carries
``terminal_seq == latest_seq``; a non-terminal state carries no
``terminal_seq``; a non-timeout snapshot always makes progress.

Terminal rules:
- only a terminal EVENT (``run.completed``) ends page emission;
- that event must sit exactly at ``terminal_seq == latest_seq`` — a terminal
  event with further seqs behind it is a storage invariant violation, never a
  silent success;
- a cursor already at ``terminal_seq`` closes the stream silently (the client
  has the terminal frame);
- a cursor before ``terminal_seq`` keeps paging until the terminal frame is
  emitted;
- a terminal state with no legal terminal position fails explicitly.

Heartbeats are emitted ONLY for a validated idle timeout; ``heartbeat_s`` is
validated when the engine is created (non-positive, NaN and infinity are
rejected immediately). The engine holds no cancellation policy and never
mutates run state.
"""

from __future__ import annotations

import json
import math
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
    CURSOR_AHEAD = "cursor_ahead"
    SNAPSHOT_INCONSISTENT = "snapshot_inconsistent"


class SSEStreamError(RuntimeError):
    """Structured streaming fault (mapped by the #36 boundary)."""

    def __init__(self, fault: StreamFault, message: str = "") -> None:
        super().__init__(message or fault.value)
        self.fault = fault
        self.code = ErrorCode.INTERNAL_UNKNOWN


@dataclass(frozen=True)
class StreamSnapshot:
    """One atomic read result. Every field is required: a partially filled
    snapshot is exactly the kind of illegal state this interface must reject,
    so no defaults are provided."""

    events: tuple[SSEEvent, ...]
    state: RunState
    oldest_available_seq: int
    latest_seq: int
    terminal_seq: int | None
    timed_out: bool


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


def _validate_heartbeat(heartbeat_s: float) -> float:
    """Reject non-positive / NaN / infinite heartbeat windows immediately."""
    if isinstance(heartbeat_s, bool) or not isinstance(heartbeat_s, (int, float)):
        raise TypeError(
            f"heartbeat_s must be a positive finite number: {heartbeat_s!r}"
        )
    if not math.isfinite(heartbeat_s) or heartbeat_s <= 0:
        raise ValueError(
            f"heartbeat_s must be a positive finite number: {heartbeat_s!r}"
        )
    return float(heartbeat_s)


def _validate_snapshot(snapshot: StreamSnapshot, cursor: int) -> None:
    """Enforce the snapshot invariants before ANY branch is taken."""
    oldest = snapshot.oldest_available_seq
    latest = snapshot.latest_seq
    if oldest < 0 or latest < oldest:
        raise SSEStreamError(
            StreamFault.SNAPSHOT_INCONSISTENT,
            f"window bounds invalid: oldest {oldest}, latest {latest}",
        )
    # zero-value semantics: 0 means "empty window" and nothing else; SSE seq
    # numbers start at 1, so a non-empty window can never start below 1
    if latest == 0:
        if oldest != 0:
            raise SSEStreamError(
                StreamFault.SNAPSHOT_INCONSISTENT,
                f"empty window must be (0, 0), got oldest {oldest}",
            )
    elif oldest < 1:
        raise SSEStreamError(
            StreamFault.SNAPSHOT_INCONSISTENT,
            f"non-empty window must start at seq >= 1, got oldest {oldest}",
        )
    if cursor > latest:
        raise SSEStreamError(
            StreamFault.CURSOR_AHEAD,
            f"cursor_ahead: cursor {cursor} > latest_seq {latest}",
        )
    for event in snapshot.events:
        if not (oldest <= event.seq <= latest):
            raise SSEStreamError(
                StreamFault.SNAPSHOT_INCONSISTENT,
                f"event seq {event.seq} outside window [{oldest}, {latest}]",
            )
    terminal_state = snapshot.state in TERMINAL_STATES
    if terminal_state:
        # seq 0 cannot be a real event: a terminal run must carry a terminal
        # seq >= 1, and it must be the newest retained event
        if snapshot.terminal_seq is None or snapshot.terminal_seq < 1:
            raise SSEStreamError(
                StreamFault.MISSING_TERMINAL_EVENT,
                f"terminal state {snapshot.state.value} without a valid "
                f"terminal_seq (got {snapshot.terminal_seq!r})",
            )
        if snapshot.terminal_seq != latest:
            raise SSEStreamError(
                StreamFault.SNAPSHOT_INCONSISTENT,
                f"terminal_seq {snapshot.terminal_seq} != latest_seq {latest}",
            )
    elif snapshot.terminal_seq is not None:
        raise SSEStreamError(
            StreamFault.SNAPSHOT_INCONSISTENT,
            f"non-terminal state {snapshot.state.value} carries terminal_seq "
            f"{snapshot.terminal_seq}",
        )
    if snapshot.timed_out:
        if snapshot.events:
            raise SSEStreamError(
                StreamFault.SNAPSHOT_INCONSISTENT, "timed_out snapshot with events"
            )
        if terminal_state or latest != cursor:
            raise SSEStreamError(
                StreamFault.SNAPSHOT_INCONSISTENT,
                "timed_out snapshot must be idle and non-terminal at the cursor",
            )
        return
    # a non-timeout read must make progress: overlapping duplicates alone are
    # NOT progress (a page that only repeats the cursor would otherwise spin)
    if any(event.seq != cursor for event in snapshot.events):
        return
    if terminal_state and cursor == snapshot.terminal_seq == latest:
        return  # client already holds the terminal frame: silent close
    raise SSEStreamError(
        StreamFault.SNAPSHOT_INCONSISTENT,
        "non-timeout snapshot without new events",
    )


def _validate_event(event: SSEEvent, cursor: int, snapshot: StreamSnapshot) -> None:
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


def _validate_terminal_event(event: SSEEvent, snapshot: StreamSnapshot) -> None:
    """A terminal event must be the LAST retained event, exactly."""
    if event.event is not SSEEventType.RUN_COMPLETED:
        return
    if snapshot.terminal_seq is None or event.seq != snapshot.terminal_seq:
        raise SSEStreamError(
            StreamFault.SNAPSHOT_INCONSISTENT,
            f"terminal event at seq {event.seq} but terminal_seq is "
            f"{snapshot.terminal_seq}",
        )


async def _stream(
    wait_page: SnapshotReader, cursor: int, heartbeat_s: float
) -> AsyncIterator[str]:
    while True:
        snapshot = await wait_page(cursor, heartbeat_s)
        _validate_snapshot(snapshot, cursor)
        if snapshot.timed_out:
            # validated idle timeout: exactly one keep-alive, cursor unchanged
            yield keep_alive()
            continue
        for event in snapshot.events:
            _validate_event(event, cursor, snapshot)
            if event.seq == cursor:
                continue
            _validate_terminal_event(event, snapshot)
            cursor = event.seq
            yield frame(event)
            if event.event is SSEEventType.RUN_COMPLETED:
                return
        if snapshot.state in TERMINAL_STATES:
            if cursor == snapshot.terminal_seq:
                return  # client already received the terminal frame
            continue  # keep paging until terminal_seq is emitted
        # non-terminal: wait for the next snapshot


def stream_engine(
    *,
    wait_page: SnapshotReader,
    after_seq: int = 0,
    heartbeat_s: float = DEFAULT_HEARTBEAT_MS / 1000,
) -> AsyncIterator[str]:
    """Create the run stream. ``heartbeat_s`` is validated here, so an invalid
    window fails at creation time rather than mid-stream."""
    window = _validate_heartbeat(heartbeat_s)
    return _stream(wait_page, max(after_seq, 0), window)


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
