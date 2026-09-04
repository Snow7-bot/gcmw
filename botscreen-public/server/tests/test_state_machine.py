import pytest

from app.orchestration.state_machine import RunStateMachine
from app.contracts.run import RunState


def test_happy_path():
    sm = RunStateMachine()
    sm.transition(RunState.GUARDING)
    sm.transition(RunState.ROUTING)
    sm.transition(RunState.RETRIEVING)
    sm.transition(RunState.DRAFTING)
    sm.transition(RunState.VERIFYING)
    sm.transition(RunState.STREAMING)
    sm.transition(RunState.COMPLETED)
    assert sm.current == RunState.COMPLETED
    assert sm.is_terminal


def test_terminal_state_is_irreversible():
    sm = RunStateMachine()
    sm.transition(RunState.COMPLETED)
    with pytest.raises(ValueError):
        sm.transition(RunState.ACCEPTED)


def test_illegal_transition_rejected():
    sm = RunStateMachine()
    with pytest.raises(ValueError):
        sm.transition(RunState.COMPLETED)


def test_cancel_from_accepted():
    sm = RunStateMachine()
    sm.transition(RunState.CANCELLED)
    assert sm.current == RunState.CANCELLED
