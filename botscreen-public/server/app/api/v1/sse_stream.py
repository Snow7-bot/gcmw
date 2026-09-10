"""Internal SSE stream engine (issue #65B — slice B-1, no public route).

Reviewer-narrowed scope for this slice: the engine and its protocol only. The
public ``/events/stream`` route, connection leases, reconnect grace windows,
atomic RunRepository persistence and async Redis waiting are all #65B-2.

Engine contract:
- frames are standard SSE ``id: <seq>`` / ``event:`` / ``data:`` records; the
  frame id is the run's event sequence number, which is what a client sends
  back as ``Last-Event-ID``;
- **sequence continuity is validated, never silently skipped**: a missing seq
  (gap, e.g. ``[1, 3]``), an out-of-order seq, or a stale cursor pointing
  before the still-available window fails loudly with a structured error
  instead of quietly jumping ahead;
- **event waiting and heartbeat timing are separate**: the engine polls at
  ``poll_s`` so new events ship within the poll SLA (much faster than the
  heartbeat), and a keep-alive comment frame is emitted only after the
  connection has been idle for a full ``heartbeat_s`` — never immediately;
- **the engine never mutates run state**: it holds no cancellation policy, so
  internal read failures (storage down, code bugs) can never cancel a run that
  should keep going or degrade. Cancellation belongs to the explicit
  lifecycle/lease layer in #65B-2;
- terminal detection trusts the event TYPE (``is_terminal_event``) and the
  authoritative run state (shared ``TERMINAL_STATES``) — never ``data.status``
  carried by an arbitrary event;
- clock and waiter are injected, so tests are deterministic and never depend on
  real sleeps.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from app.contracts.errors import ErrorCode
from app.contracts.events import SSEEvent, is_terminal_event
from app.contracts.run import TERMINAL_STATES, RunState

DEFAULT_POLL_MS = 50
DEFAULT_HEARTBEAT_MS = 15_000


class SSEStreamError(RuntimeError):
    """Structured streaming fault (mapped by the #36 boundary)."""

    def __init__(self, fault: StreamFault | None, message: str = "") -> None:
        super().__init__(message or (fault.value if fault else "stream error"))
        self.fault = fault
        self.code = ErrorCode.INTERNAL_UNKNOWN


class StreamFault(str, Enum):
    """Protocol-level faults, distinct from transport/state errors."""

    REPLAY_GAP = "replay_gap"
    OUT_OF_ORDER = "out_of_order"
    STALE_CURSOR = "stale_cursor"


class EventPageProtocol(Protocol):
    """What the engine needs from a run-event reader (injected)."""

    events: tuple[SSEEvent, ...]


PageReader = Callable[[int], Any]
StateReader = Callable[[], RunState]
Waiter = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


def frame(event: SSEEvent) -> str:
    """Serialize one event as an SSE frame (``id`` = seq for resume)."""
    payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
    return f"id: {event.seq}\nevent: {event.event.value}\ndata: {payload}\n\n"


def keep_alive() -> str:
    """SSE comment frame: no ``id``, so it can never perturb seq authority."""
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


def is_terminal(event: SSEEvent, state: RunState | None = None) -> bool:
    """Terminal only by event TYPE or authoritative run state.

    ``data.status`` of a non-terminal event is explicitly NOT trusted: a
    ``run.accepted`` carrying ``status=completed`` must not close the stream.
    """
    if is_terminal_event(event.event):
        return True
    return state in TERMINAL_STATES


@dataclass(frozen=True)
class StreamMetrics:
    """Deterministic counters for tests and observability hooks."""

    frames: int = 0
    heartbeats: int = 0
    polls: int = 0


def _validate(next_seq: int, cursor: int) -> None:
    """Enforce strict continuity; identical re-reads are the only tolerance."""
    if next_seq == cursor:
        return
    if next_seq < cursor:
        raise SSEStreamError(
            StreamFault.OUT_OF_ORDER,
            f"out_of_order: seq {next_seq} < cursor {cursor}",
        )
    if next_seq != cursor + 1:
        fault = StreamFault.STALE_CURSOR if cursor == 0 else StreamFault.REPLAY_GAP
        raise SSEStreamError(
            fault,
            f"{fault.value}: expected seq {cursor + 1}, got {next_seq}",
        )


async def stream_engine(
    *,
    read_page: PageReader,
    read_state: StateReader,
    after_seq: int = 0,
    poll_s: float = DEFAULT_POLL_MS / 1000,
    heartbeat_s: float = DEFAULT_HEARTBEAT_MS / 1000,
    clock: Clock | None = None,
    waiter: Waiter | None = None,
    max_idle_windows: int | None = None,
) -> AsyncIterator[str]:
    """Yield SSE frames for one run until terminal (or the optional idle cap).

    ``read_page(cursor)`` returns a page whose ``events`` are ordered by seq.
    Missing, re-ordered or stale sequences raise :class:`SSEStreamError`;
    reader/transport failures propagate unchanged — the engine adds NO
    cancellation side-effects of its own. ``max_idle_windows`` bounds idle
    polling for tests and orderly shutdown.
    """
    _clock = clock or (lambda: asyncio.get_running_loop().time())
    _wait = waiter or asyncio.sleep
    cursor = max(after_seq, 0)
    last_write = _clock()
    idle_windows = 0

    while True:
        page = read_page(cursor)
        for event in page.events:
            _validate(event.seq, cursor)
            if event.seq == cursor:
                continue  # idempotent overlap: exact re-read, nothing to send
            cursor = event.seq
            last_write = _clock()
            idle_windows = 0
            yield frame(event)
            if is_terminal(event, read_state()):
                return
        state = read_state()
        if state in TERMINAL_STATES and not page.events:
            return
        # wait for events first; a keep-alive only after a FULL idle window
        await _wait(poll_s)
        idle_windows += 1
        if _clock() - last_write >= heartbeat_s:
            last_write = _clock()
            yield keep_alive()
        if max_idle_windows is not None and idle_windows >= max_idle_windows:
            return


__all__ = [
    "DEFAULT_HEARTBEAT_MS",
    "DEFAULT_POLL_MS",
    "EventPageProtocol",
    "SSEStreamError",
    "StreamFault",
    "StreamMetrics",
    "effective_after_seq",
    "frame",
    "is_terminal",
    "keep_alive",
    "stream_engine",
]
