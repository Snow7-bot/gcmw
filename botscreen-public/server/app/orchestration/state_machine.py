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


class RunStateMachine:
    """Tracks one run's state, event sequence, and immutable terminal states."""

    def __init__(self, run_id: str, initial: RunState = RunState.ACCEPTED) -> None:
        if not run_id:
            raise ValueError("run_id is required")
        self.run_id = run_id
        self.current = initial
        self.event_seq = 0
        self.events: list[RunEvent] = []

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


class RunIdempotencyRegistry:
    """Tracks request_id to prevent duplicate run creation or duplicate terminal events."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def register(self, request_id: str, run_id: str) -> bool:
        """Returns True if this request_id is new, False if already known."""
        if not request_id:
            raise ValueError("request_id is required")
        if request_id in self._seen:
            return False
        self._seen[request_id] = run_id
        return True

    def get_run_id(self, request_id: str) -> str | None:
        return self._seen.get(request_id)


class RunCoordinator:
    """Connects request_id idempotency with Run creation/execution entry.

    Duplicate start requests return the original RunStateMachine and do not
    create or advance another run.
    """

    def __init__(self) -> None:
        self._registry = RunIdempotencyRegistry()
        self._machines: dict[str, RunStateMachine] = {}

    def start_or_get(self, request_id: str, run_id: str) -> RunStateMachine:
        if not request_id:
            raise ValueError("request_id is required")
        if not run_id:
            raise ValueError("run_id is required")
        existing_run_id = self._registry.get_run_id(request_id)
        if existing_run_id is not None:
            return self._machines[existing_run_id]
        if run_id in self._machines:
            raise ValueError(f"run_id already exists: {run_id}")
        if not self._registry.register(request_id, run_id):
            raise RuntimeError("request_id registration failed unexpectedly")
        machine = RunStateMachine(run_id)
        self._machines[run_id] = machine
        return machine

    def get_machine(self, run_id: str) -> RunStateMachine | None:
        return self._machines.get(run_id)
