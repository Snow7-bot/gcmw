"""Deterministic Run state machine."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts.run import TERMINAL_STATES, RunState

# Allowed transitions. A state may transition to itself only for idempotent events.
_ALLOWED: dict[RunState, set[RunState]] = {
    RunState.ACCEPTED: {RunState.GUARDING, RunState.FAILED, RunState.CANCELLED},
    RunState.GUARDING: {RunState.ROUTING, RunState.FAILED, RunState.CANCELLED, RunState.HANDOFF},
    RunState.ROUTING: {RunState.RETRIEVING, RunState.DRAFTING, RunState.FAILED, RunState.CANCELLED, RunState.HANDOFF},
    RunState.RETRIEVING: {RunState.DRAFTING, RunState.FAILED, RunState.CANCELLED, RunState.HANDOFF, RunState.DEGRADED},
    RunState.DRAFTING: {RunState.VERIFYING, RunState.FAILED, RunState.CANCELLED, RunState.HANDOFF},
    RunState.VERIFYING: {RunState.STREAMING, RunState.DRAFTING, RunState.FAILED, RunState.CANCELLED, RunState.HANDOFF},
    RunState.STREAMING: {RunState.COMPLETED, RunState.DEGRADED, RunState.FAILED, RunState.CANCELLED, RunState.HANDOFF},
    # Terminal states are irreversible.
    RunState.COMPLETED: set(),
    RunState.DEGRADED: set(),
    RunState.HANDOFF: set(),
    RunState.FAILED: set(),
    RunState.CANCELLED: set(),
}


@dataclass
class RunStateMachine:
    current: RunState = RunState.ACCEPTED
    _transitions: list[tuple[RunState, RunState]] = field(default_factory=list)

    def can_transition(self, target: RunState) -> bool:
        if self.current in TERMINAL_STATES:
            return False
        return target in _ALLOWED.get(self.current, set())

    def transition(self, target: RunState) -> RunState:
        if not self.can_transition(target):
            raise ValueError(
                f"Illegal Run transition: {self.current.value} -> {target.value}"
            )
        self._transitions.append((self.current, target))
        self.current = target
        return self.current

    @property
    def is_terminal(self) -> bool:
        return self.current in TERMINAL_STATES

    @property
    def history(self) -> list[tuple[RunState, RunState]]:
        return list(self._transitions)
