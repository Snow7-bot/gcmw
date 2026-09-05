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


def test_coordinator_rejects_different_request_id_same_run_id():
    from app.orchestration.state_machine import RunCoordinator

    coordinator = RunCoordinator()
    coordinator.start_or_get("request-30", "run-30")
    with pytest.raises(ValueError):
        coordinator.start_or_get("request-31", "run-30")


def test_coordinator_empty_run_id_does_not_pollute_registry():
    from app.orchestration.state_machine import RunCoordinator

    coordinator = RunCoordinator()
    with pytest.raises(ValueError):
        coordinator.start_or_get("request-40", "")
    assert coordinator.get_machine("") is None
    # A later valid request should still work
    machine = coordinator.start_or_get("request-40", "run-40")
    assert machine is not None


def test_initial_accepted_event_is_recorded():
    sm = RunStateMachine("run-50")
    assert sm.event_seq == 1
    assert sm.history[0].state == RunState.ACCEPTED
    assert sm.history[0].event_seq == 1


def test_registry_rejects_empty_run_id():
    reg = RunIdempotencyRegistry()
    with pytest.raises(ValueError):
        reg.register("request-60", "")


def test_coordinator_concurrent_start_same_request_returns_same_run():
    from concurrent.futures import ThreadPoolExecutor

    from app.orchestration.state_machine import RunCoordinator

    coordinator = RunCoordinator()
    results = []

    def start():
        return coordinator.start_or_get("request-concurrent", "run-concurrent")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: start(), range(16)))

    assert all(machine is results[0] for machine in results)
    assert coordinator.get_machine("run-concurrent") is results[0]
