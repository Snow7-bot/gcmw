"""SSE connection leases (#65B-2 slice B2-C — minimal).

One lease per live stream, counted per run. Only a **confirmed client
disconnect** releases the last subscriber into the reconnect grace window; a
stream that ends server-side (a terminal frame, or a structured ``stream.error``
frame after a storage fault) ends the lease without ever scheduling a cancel —
a storage outage must not be mistaken for a user walking away.

Inside the grace window a reconnect revokes the pending decision. Once the
decision is being APPLIED it is no longer revocable (the callback is in flight,
and a half-applied cancel would be worse than a cancel the client can see in the
terminal frame).

That is deliberately the whole slice: no replay buffer, no queueing, no new
storage. The registry is created per application run (lifespan scope) and lives
on the single event loop that owns the service — every mutation below is
await-free, hence atomic, and entries remove themselves once no subscriber, no
pending timer and no in-flight callback remain.
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
    #: True while the expiry callback is running: the decision is being applied
    expiring: bool = False


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
        # every expiry task, pending or already inside the callback, so
        # ``shutdown`` can end them instead of leaking a state-mutating task
        self._tasks: set[asyncio.Task] = set()

    # -- lease lifecycle -------------------------------------------------------

    def open(self, run_id: str) -> None:
        """Register a subscriber, revoking a pending (not yet applied) cancel."""
        entry = self._entries.get(run_id)
        if entry is None:
            entry = _Lease()
            self._entries[run_id] = entry
        if entry.timer is not None and not entry.expiring:
            entry.timer.cancel()  # a reconnect inside the grace revokes it
            entry.timer = None
        entry.subscribers += 1

    def close(self, run_id: str, *, client_gone: bool) -> None:
        """Release a subscriber.

        ``client_gone`` must only be true for a CONFIRMED disconnect (the ASGI
        server closed or cancelled the response body). A stream that ended
        server-side drops the entry immediately: nothing to revoke, nothing to
        cancel.
        """
        entry = self._entries.get(run_id)
        if entry is None:  # defensive: closing an unopened lease is a no-op
            return
        if entry.subscribers > 0:
            entry.subscribers -= 1
        if entry.subscribers > 0:
            return
        if not client_gone:
            self._drop_if_idle(run_id)
            return
        if entry.timer is None:
            entry.timer = self._start_expiry(run_id)

    def subscribers(self, run_id: str) -> int:
        entry = self._entries.get(run_id)
        return entry.subscribers if entry is not None else 0

    def tracked(self) -> int:
        """Runs with a subscriber, a pending timer or an in-flight callback."""
        return len(self._entries)

    def pending_expiries(self) -> int:
        return sum(1 for entry in self._entries.values() if entry.timer is not None)

    def in_flight_callbacks(self) -> int:
        return sum(1 for entry in self._entries.values() if entry.expiring)

    # -- grace window ------------------------------------------------------------

    def _start_expiry(self, run_id: str) -> asyncio.Task:
        task = asyncio.create_task(self._expire(run_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _drop_if_idle(self, run_id: str) -> None:
        entry = self._entries.get(run_id)
        if entry is None or entry.subscribers > 0 or entry.expiring:
            return
        timer = entry.timer
        if timer is not None and not timer.done():
            return
        del self._entries[run_id]

    async def _expire(self, run_id: str) -> None:
        entry = self._entries.get(run_id)
        if entry is None:
            return
        try:
            await self._sleep(self._grace_s)
        except asyncio.CancelledError:
            # revoked by a reconnect inside the window, or cancelled at shutdown
            entry.timer = None
            self._drop_if_idle(run_id)
            return
        entry = self._entries.get(run_id)
        if entry is None or entry.subscribers > 0:
            # defensive: a reconnect would have cancelled this task instead
            if entry is not None:
                entry.timer = None
            self._drop_if_idle(run_id)
            return
        entry.expiring = True
        try:
            await self._on_expire(run_id)
        finally:
            entry.expiring = False
            entry.timer = None
            self._drop_if_idle(run_id)

    async def shutdown(self) -> None:
        """End every pending AND in-flight expiry, then forget the leases.

        Pending timers are cancelled; a callback that already started is
        cancelled and AWAITED, so once ``shutdown()`` returns no disconnect
        cancel can still mutate run state.
        """
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._entries.clear()


__all__ = [
    "DEFAULT_RECONNECT_GRACE_S",
    "RunLeaseRegistry",
]
