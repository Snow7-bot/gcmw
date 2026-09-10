"""SSE streaming for run events (issue #65B — slice B).

Reviewer-scoped slice B contract:
- ``GET /agent/runs/{id}/events/stream`` upgrades the run event feed to
  ``text/event-stream`` with standard ``id:`` frames (seq), so clients resume
  with the ``Last-Event-ID`` header;
- sequence validation: frames are emitted strictly in ascending seq order,
  duplicates (already-sent seq) are skipped idempotently and the query
  ``after_seq`` / header are combined by taking the maximum;
- terminal uniqueness: once a terminal run event has been emitted the stream
  closes; reconnecting at/after the terminal seq closes immediately WITHOUT
  resending the terminal event;
- keep-alive: when no new event arrives within the heartbeat window a standard
  SSE comment frame (``: keep-alive``) is written — comment frames never carry
  a seq and therefore never perturb the sequence authority;
- cancellation propagation: if the client disconnects mid-stream the run is
  cancelled through the lifecycle service (same single authority); terminal
  runs are left untouched. The behaviour can be disabled per request for
  observers/tests.

State and events stay single-authority: this module only *reads* the
authoritative feed produced by ``RunAdmissionService`` (#36a/#36b); it never
writes state or seq. Atomic state+event persistence (RunRepository) remains a
separate #65B work item.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from app.contracts.events import TERMINAL_EVENTS, SSEEventType
from app.contracts.run import RunState

_LOGGER = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_MS = 15_000
MIN_HEARTBEAT_MS = 100
MAX_HEARTBEAT_MS = 60_000

#: closed run state -> the single terminal event a client receives
TERMINAL_RUN_STATES = frozenset(
    {
        RunState.COMPLETED,
        RunState.DEGRADED,
        RunState.HANDOFF,
        RunState.FAILED,
        RunState.CANCELLED,
    }
)


def frame(event) -> str:
    """Serialize one SSEEvent as an ``id/event/data`` frame (id = seq)."""
    payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
    return f"id: {event.seq}\nevent: {event.event.value}\ndata: {payload}\n\n"


def keep_alive() -> str:
    """Standard SSE comment frame (no seq, no data contract involvement)."""
    return ": keep-alive\n\n"


def effective_after_seq(after_seq: int, last_event_id: str | None) -> int:
    """Combine the query cursor with the Last-Event-ID header (max wins).

    A malformed header is ignored (the query cursor still applies) — the
    stream must never fail because a client sent a broken resume id.
    """
    cursor = max(int(after_seq), 0)
    if last_event_id:
        try:
            cursor = max(cursor, int(last_event_id.strip()))
        except (TypeError, ValueError):
            pass
    return cursor


def _is_terminal(event) -> bool:
    return event.event in TERMINAL_EVENTS or event.data.get("status") in {
        "completed",
        "failed",
        "cancelled",
        "degraded",
    }


async def stream_run_events(
    service,
    principal,
    run_id: str,
    *,
    after_seq: int = 0,
    last_event_id: str | None = None,
    heartbeat_ms: int = DEFAULT_HEARTBEAT_MS,
    cancel_on_disconnect: bool = True,
) -> AsyncIterator[str]:
    """Yield SSE frames for one owned run until terminal or disconnect.

    Raises the service's AppError (404/403) before the first frame when the
    run is unknown or not owned by the principal.
    """
    cursor = effective_after_seq(after_seq, last_event_id)
    heartbeat_s = max(heartbeat_ms, MIN_HEARTBEAT_MS) / 1000
    # ownership/authorization is enforced by the service before streaming
    page = service.events(principal, run_id, cursor)

    try:
        while True:
            for event in page.events:
                if event.seq <= cursor:
                    continue  # idempotent duplicate suppression
                cursor = event.seq
                yield frame(event)
                if _is_terminal(event):
                    return  # terminal uniqueness: close once, never resend
            # nothing new to send: a finished run closes immediately (no
            # duplicate terminal on reconnect), otherwise keep the link warm
            snapshot = service.get_run(principal, run_id)
            if snapshot.state in TERMINAL_RUN_STATES:
                return
            yield keep_alive()
            await asyncio.sleep(heartbeat_s)
            page = service.events(principal, run_id, cursor)
    finally:
        if cancel_on_disconnect:
            try:
                snapshot = service.get_run(principal, run_id)
                if snapshot.state not in TERMINAL_RUN_STATES:
                    service.cancel_run(principal, run_id)
            except Exception:  # disconnect cleanup must never raise
                _LOGGER.debug("cancel-on-disconnect failed", exc_info=True)


__all__ = [
    "DEFAULT_HEARTBEAT_MS",
    "MAX_HEARTBEAT_MS",
    "MIN_HEARTBEAT_MS",
    "TERMINAL_EVENTS",
    "TERMINAL_RUN_STATES",
    "SSEEventType",
    "effective_after_seq",
    "frame",
    "keep_alive",
    "stream_run_events",
]
