from datetime import timezone

import pytest

from app.contracts.run import RunState
from app.orchestration.state_machine import RunIdempotencyRegistry, RunStateMachine


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
    assert sm.event_seq == 7


def test_terminal_state_is_irreversible():
    sm = RunStateMachine("run-2")
    sm.transition(RunState.CANCELLED)
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
    assert e1.event_seq == 1
    assert e2.event_seq == 2
    assert e1.timestamp.tzinfo is not None
    assert e1.timestamp.tzinfo == timezone.utc
    assert e2.timestamp > e1.timestamp


def test_idempotency_registry_rejects_duplicate_request():
    reg = RunIdempotencyRegistry()
    assert reg.register("request-1", "run-1") is True
    assert reg.register("request-1", "run-2") is False
    assert reg.get_run_id("request-1") == "run-1"


def test_duplicate_request_id_returns_original_run_id_and_does_not_advance_event_seq():
    sm = RunStateMachine("run-10")
    reg = RunIdempotencyRegistry()
    assert reg.register("request-10", "run-10") is True
    sm.transition(RunState.GUARDING)
    before_seq = sm.event_seq
    # A duplicate create/advance request must not create a new run or change event seq.
    assert reg.register("request-10", "run-10") is False
    assert reg.get_run_id("request-10") == "run-10"
    assert sm.event_seq == before_seq


def test_coordinator_duplicate_start_returns_original_run_without_advancing():
    from app.orchestration.state_machine import RunCoordinator

    coordinator = RunCoordinator()
    machine = coordinator.start_or_get("request-20", "run-20")
    machine.transition(RunState.GUARDING)
    before_seq = machine.event_seq

    duplicate = coordinator.start_or_get("request-20", "run-21")
    assert duplicate is machine
    assert duplicate.event_seq == before_seq
    assert coordinator.get_machine("run-21") is None
