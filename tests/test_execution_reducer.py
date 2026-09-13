"""The reducer is pure, versioned and total over its vocabulary (task-264, durable-1)."""

from __future__ import annotations

import pytest

from agentjobs.execution.errors import HistoryIncompatible
from agentjobs.execution.reducer import (
    WORKFLOW_VERSION,
    Event,
    ExecutionState,
    next_intents,
    replay,
)

HISTORY = [
    Event(1, "accepted", {"envelope": {"runner": "r"}, "workflow_version": WORKFLOW_VERSION}),
    Event(2, "admitted", {"run_id": "run_a", "attempt_no": 1}),
    Event(3, "launched", {"run_id": "run_a", "session_id": "s-1"}),
    Event(4, "signal", {"source_event_id": "task-001#9@t"}),
]


def test_the_same_history_reduces_to_the_same_state_in_any_delivery_order() -> None:
    assert replay("exe", HISTORY) == replay("exe", list(reversed(HISTORY)))


def test_intents_are_stable_proposals_named_by_the_history() -> None:
    state = replay("exe", HISTORY)
    assert [(i.kind, i.activity_id) for i in next_intents(state)] == [
        ("observe", "exe:observe:run_a"),
        ("deliver_signal", "exe:signal:run_a:task-001#9@t"),
    ]
    assert next_intents(state) == next_intents(replay("exe", HISTORY))


def test_a_signal_pending_across_a_retry_is_a_new_proposal_not_a_conflicting_one() -> None:
    """task-419: the same id with a new run in its input wedged every later replay."""
    retried = replay(
        "exe",
        HISTORY
        + [
            Event(
                5,
                "concluded",
                {
                    "run_id": "run_a",
                    "outcome": "interrupted",
                    "retry_owed": True,
                    "failure_class": "worker_gone",
                },
            ),
            Event(6, "admitted", {"run_id": "run_b", "attempt_no": 2}),
            Event(7, "launched", {"run_id": "run_b", "session_id": "s-2"}),
        ],
    )
    first = {i.activity_id: i.input for i in next_intents(replay("exe", HISTORY))}
    second = {i.activity_id: i.input for i in next_intents(retried)}
    for activity_id, payload in second.items():
        assert activity_id not in first or first[activity_id] == payload


def test_a_stop_supersedes_every_other_proposal() -> None:
    stopped = replay(
        "exe", HISTORY + [Event(5, "stop_requested", {"generation": 1, "requester": "p"})]
    )
    assert [i.kind for i in next_intents(stopped)] == ["stop"]
    assert stopped.state == "stopping"


def test_a_concluded_execution_proposes_nothing_and_nothing_reopens_it() -> None:
    ended = replay(
        "exe",
        HISTORY
        + [
            Event(5, "concluded", {"run_id": "run_a", "outcome": "cancelled"}),
            Event(6, "admitted", {"run_id": "run_b", "attempt_no": 2}),
        ],
    )
    assert ended.state == "cancelled" and ended.run_id == "run_a"
    assert next_intents(ended) == ()


def test_snapshot_round_trips() -> None:
    state = replay("exe", HISTORY)
    assert ExecutionState.from_data(state.as_data()) == state
    resumed = replay("exe", [Event(5, "observed", {"phase": "running"})], snapshot=state.as_data())
    assert resumed == replay("exe", HISTORY + [Event(5, "observed", {"phase": "running"})])


def test_an_unknown_event_kind_is_refused_not_skipped() -> None:
    with pytest.raises(HistoryIncompatible):
        replay("exe", HISTORY + [Event(5, "teleported", {})])


def test_an_unknown_workflow_version_is_refused_not_guessed() -> None:
    with pytest.raises(HistoryIncompatible):
        replay("exe", HISTORY, version=WORKFLOW_VERSION + 1)
    with pytest.raises(HistoryIncompatible):
        replay("exe", [Event(1, "accepted", {"workflow_version": WORKFLOW_VERSION + 1})])
    with pytest.raises(HistoryIncompatible):
        ExecutionState.from_data({**replay("exe", HISTORY).as_data(), "version": 99})
