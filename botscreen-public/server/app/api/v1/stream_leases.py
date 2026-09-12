"""SSE connection leases (#65B-2 slice B2-C — minimal).

One lease per live stream, counted per run. When the LAST subscriber of a run
goes away the registry starts a fixed RECONNECT GRACE window; a reconnect
inside that window revokes the pending decision, otherwise the run is cancelled
through the lifecycle service (``cancel_for_disconnect``).

That is deliberately the whole slice: no replay buffer, no queueing, no new
storage, no changes to the stream engine. The grace window is the only policy,
and it exists so a flaky mobile link cannot kill a run the user is about to
reconnect to.

The registry is created per application run (lifespan scope) and lives on the
single event loop that owns the service — every mutation below is
await-free, hence atomic, and the entries remove themselves once no subscriber
and no pending timer remain.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

#: how long a run survives without any subscriber before it is cancelled
DEFAULT_RECONNECT_GRACE_S = 2.0


def _validate_grace(grace_s: float) -> float:
    if isinstance(grace_s, bool) or not isinstance(grace_s, (int, float)):
        raise TypeError(f"grace_s must be a positive finite number: {grace_s!r}")
    if not math.isfinite(grace_s) or grace_s <= 0:
        raise ValueError(f"grace_s must be a positive finite number: {grace_s!r}")
    return float(grace_s)


@dataclass
class _Lease:
    subscribers: int = 0
    timer: asyncio.Task | None = field(default=None)


class RunLeaseRegistry:
    """Per-run subscriber counting plus the reconnect grace decision."""

    def __init__(
        self,
        on_expire: Callable[[str], Awaitable[None]],
        *,
        grace_s: float = DEFAULT_RECONNECT_GRACE_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._on_expire = on_expire
        self._grace_s = _validate_grace(grace_s)
        self._sleep = sleep
        self._entries: dict[str, _Lease] = {}

    # -- lease lifecycle -------------------------------------------------------

    def open(self, run_id: str) -> None:
        """Register a subscriber, revoking any pending disconnect-cancel."""
        entry = self._entries.get(run_id)
        if entry is None:
            entry = _Lease()
            self._entries[run_id] = entry
        if entry.timer is not None:
            entry.timer.cancel()  # a reconnect inside the grace revokes it
            entry.timer = None
        entry.subscribers += 1

    def close(self, run_id: str) -> None:
        """Release a subscriber; the last one starts the grace window."""
        entry = self._entries.get(run_id)
        if entry is None:  # defensive: closing an unopened lease is a no-op
            return
        if entry.subscribers > 0:
            entry.subscribers -= 1
        if entry.subscribers == 0 and entry.timer is None:
            entry.timer = asyncio.create_task(self._expire(run_id))

    def subscribers(self, run_id: str) -> int:
        entry = self._entries.get(run_id)
        return entry.subscribers if entry is not None else 0

    def tracked(self) -> int:
        """Runs with at least one subscriber or a pending grace timer."""
        return len(self._entries)

    def pending_expiries(self) -> int:
        return sum(1 for entry in self._entries.values() if entry.timer is not None)

    # -- grace window ------------------------------------------------------------

    async def _expire(self, run_id: str) -> None:
        try:
            await self._sleep(self._grace_s)
        except asyncio.CancelledError:
            return  # revoked by a reconnect within the grace window
        entry = self._entries.get(run_id)
        if entry is None or entry.subscribers > 0:
            return
        # drop the entry BEFORE the cancel: a reconnect that arrives while the
        # cancel is in flight starts a fresh lease and cannot revoke it any more
        del self._entries[run_id]
        await self._on_expire(run_id)

    async def shutdown(self) -> None:
        """Cancel every pending timer so no task outlives the application."""
        timers = [
            entry.timer
            for entry in self._entries.values()
            if entry.timer is not None and not entry.timer.done()
        ]
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        self._entries.clear()


__all__ = [
    "DEFAULT_RECONNECT_GRACE_S",
    "RunLeaseRegistry",
]
