"""Deterministic Run state machine with idempotency and event sequencing."""

from __future__ import annotations

from datetime import datetime, timezone

from ..contracts.events import RunEvent
from ..contracts.run import TERMINAL_STATES, RunState

_ALLOWED: dict[RunState, set[RunState]] = {
    RunState.ACCEPTED: {RunState.GUARDING, RunState.FAILED, RunState.CANCELLED},
    RunState.GUARDING: {
        RunState.ROUTING,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.HANDOFF,
    },
    RunState.ROUTING: {
        RunState.RETRIEVING,
        RunState.DRAFTING,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.HANDOFF,
    },
    RunState.RETRIEVING: {
        RunState.DRAFTING,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.HANDOFF,
        RunState.DEGRADED,
    },
    RunState.DRAFTING: {
        RunState.VERIFYING,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.HANDOFF,
    },
    RunState.VERIFYING: {
        RunState.STREAMING,
        RunState.DRAFTING,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.HANDOFF,
    },
    RunState.STREAMING: {
        RunState.COMPLETED,
        RunState.DEGRADED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.HANDOFF,
    },
    RunState.COMPLETED: set(),
    RunState.DEGRADED: set(),
    RunState.HANDOFF: set(),
    RunState.FAILED: set(),
    RunState.CANCELLED: set(),
}


def is_terminal_state(state: RunState) -> bool:
    """Public terminal check (shared by the repository and the API layer)."""
    return state in TERMINAL_STATES


def is_allowed_transition(source: RunState, target: RunState) -> bool:
    """Single source of truth for legal run transitions (#10/#21 rules).

    Terminal states are irreversible and a run never transitions to itself.
    The repository MUST use this instead of re-declaring transition tables.
    """
    if is_terminal_state(source):
        return False
    return target in _ALLOWED.get(source, set())


def transition_event_type(target: RunState) -> str:
    """Which SSE event a transition to ``target`` produces."""
    return "run.completed" if is_terminal_state(target) else "process.status"


class RunStateMachine:
    """Tracks one run's state, event sequence, and immutable terminal states."""

    def __init__(self, run_id: str, initial: RunState = RunState.ACCEPTED) -> None:
        if not run_id:
            raise ValueError("run_id is required")
        self.run_id = run_id
        self.current = initial
        self.events: list[RunEvent] = []
        if initial == RunState.ACCEPTED:
            self.events.append(
                RunEvent(
                    run_id=run_id,
                    state=RunState.ACCEPTED,
                    event_seq=1,
                    payload={},
                    timestamp=datetime.now(timezone.utc),
                )
            )
        self.event_seq = len(self.events)

    def _next_seq(self) -> int:
        self.event_seq += 1
        return self.event_seq

    def can_transition(self, target: RunState) -> bool:
        if self.current in TERMINAL_STATES:
            return False
        return target in _ALLOWED.get(self.current, set())

    def transition(self, target: RunState, payload: dict | None = None) -> RunEvent:
        """Transitions to target and returns a structured RunEvent.

        Terminal states are irreversible; attempting to transition out of a
        terminal state raises ValueError.
        """
        if not self.can_transition(target):
            raise ValueError(
                f"Illegal Run transition: {self.current.value} -> {target.value}"
            )
        self.current = target
        event = RunEvent(
            run_id=self.run_id,
            state=self.current,
            event_seq=self._next_seq(),
            payload=payload or {},
            timestamp=datetime.now(timezone.utc),
        )
        self.events.append(event)
        return event

    @property
    def is_terminal(self) -> bool:
        return self.current in TERMINAL_STATES

    @property
    def history(self) -> list[RunEvent]:
        return list(self.events)
