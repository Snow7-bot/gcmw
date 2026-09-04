"""Run state and transition contracts."""
from __future__ import annotations

from enum import Enum


class RunState(str, Enum):
    ACCEPTED = "ACCEPTED"
    GUARDING = "GUARDING"
    ROUTING = "ROUTING"
    RETRIEVING = "RETRIEVING"
    DRAFTING = "DRAFTING"
    VERIFYING = "VERIFYING"
    STREAMING = "STREAMING"
    COMPLETED = "COMPLETED"
    DEGRADED = "DEGRADED"
    HANDOFF = "HANDOFF"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = {
    RunState.COMPLETED,
    RunState.DEGRADED,
    RunState.HANDOFF,
    RunState.FAILED,
    RunState.CANCELLED,
}
