from datetime import timezone

import pytest

from app.contracts.run import RunState
from app.orchestration.state_machine import RunStateMachine


def test_happy_path():
    sm = RunStateMachine("run-1")
    sm.transition(RunState.GUARDING)
    sm.transition(RunState.ROUTING)
    sm.transition(RunState.RETRIEVING)
    sm.transition(RunState.DRAFTING)
    sm.transition(RunState.VERIFYING)
    sm.transition(RunState.STREAMING)
    sm.transition(RunState.COMPLETED)
    assert sm.current == RunState.COMPLETED
    assert sm.is_terminal
    assert sm.event_seq == 8  # run.accepted + 7 transitions


@pytest.mark.parametrize(
    "terminal",
    [
        RunState.COMPLETED,
        RunState.DEGRADED,
        RunState.HANDOFF,
        RunState.FAILED,
        RunState.CANCELLED,
    ],
)
def test_terminal_state_is_irreversible(terminal):
    # Go through a legal path to each terminal state from ACCEPTED.
    sm = RunStateMachine("run-2")
    if terminal == RunState.COMPLETED:
        for s in [
            RunState.GUARDING,
            RunState.ROUTING,
            RunState.RETRIEVING,
            RunState.DRAFTING,
            RunState.VERIFYING,
            RunState.STREAMING,
        ]:
            sm.transition(s)
    elif terminal == RunState.DEGRADED:
        for s in [RunState.GUARDING, RunState.ROUTING, RunState.RETRIEVING]:
            sm.transition(s)
    elif terminal in (RunState.HANDOFF, RunState.FAILED):
        sm.transition(RunState.GUARDING)
    sm.transition(terminal)
    with pytest.raises(ValueError):
        sm.transition(RunState.ACCEPTED)


def test_illegal_transition_rejected():
    sm = RunStateMachine("run-3")
    with pytest.raises(ValueError):
        sm.transition(RunState.COMPLETED)


def test_cancel_from_accepted():
    sm = RunStateMachine("run-4")
    sm.transition(RunState.CANCELLED)
    assert sm.current == RunState.CANCELLED


def test_event_has_utc_timestamp_and_increasing_seq():
    sm = RunStateMachine("run-5")
    e1 = sm.transition(RunState.GUARDING)
    e2 = sm.transition(RunState.ROUTING)
    assert e1.event_seq == 2
    assert e2.event_seq == 3
    assert e1.timestamp.tzinfo is not None
    assert e1.timestamp.tzinfo == timezone.utc
    assert e2.timestamp > e1.timestamp


def test_initial_accepted_event_is_recorded():
    sm = RunStateMachine("run-50")
    assert sm.event_seq == 1
    assert sm.history[0].state == RunState.ACCEPTED
    assert sm.history[0].event_seq == 1
