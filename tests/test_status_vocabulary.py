"""One status vocabulary and one colour category per status (task-562).

The design table in docs/task-schema.md is the specification, and this file is its
executable copy: every row of the table is a case here, asserted as the exact label
and category the server sends. A browser draws the chip from those two values alone,
so this is the rendered-value rule applied at the source.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest

from agentjobs.models_v2 import (
    STATUS_VOCABULARY,
    LiveFinishState,
    Outcome,
    QueuedDispatchState,
    StatusCategory,
    StatusFacts,
    Task,
    closed_status,
    status_named,
    task_status,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _task(**overrides: Any) -> Task:
    base: Dict[str, Any] = {
        "schema": 2,
        "id": "task-001-example",
        "title": "Example",
        "created": NOW,
        "updated": NOW,
        "lifecycle": "ready",
        "ball": "agent",
        "ball_reason": "available",
        "queue_position": 100,
        "category": "ux",
        "spec": {"summary": "One line.", "description": "What to do."},
    }
    base.update(overrides)
    if base["lifecycle"] == "closed":
        base.pop("queue_position", None)
    if base["lifecycle"] == "active":
        base.setdefault("assignment", {"owner": "claude"})
    return Task.model_validate(base)


def _quota_park() -> Task:
    return _task(
        lifecycle="active",
        ball="external",
        ball_reason="service",
        ball_prompt="Parked on a usage limit.",
        log=[
            {
                "id": 1,
                "ts": NOW,
                "actor": "agentjobs",
                "type": "handoff",
                "body": "Parked.",
                "data": {
                    "auth_recovery": {
                        "action": "park",
                        "kind": "usage_limit",
                        "resets_at": "2026-09-24T21:30:00Z",
                    }
                },
            }
        ],
    )


QUEUED = QueuedDispatchState(queue_id="q_1", position=1, queued_at="2026-09-24T12:00:00Z")
STARTING = QueuedDispatchState(
    queue_id="q_1", position=1, queued_at="2026-09-24T12:00:00Z", status="starting"
)
FINISH = LiveFinishState(state="running", current_step="gate")

C = StatusCategory

# (id, task factory, queued, finish, facts, label, category)
CASES = [
    ("ready", lambda: _task(), None, None, StatusFacts(), "Ready", C.READY),
    ("queued", lambda: _task(), QUEUED, None, StatusFacts(), "Queued", C.QUEUED),
    ("starting", lambda: _task(), STARTING, None, StatusFacts(), "Starting", C.QUEUED),
    (
        "working",
        lambda: _task(lifecycle="active", ball_reason="work", ball_prompt="Go."),
        None,
        None,
        StatusFacts(),
        "Working",
        C.WORKING,
    ),
    (
        "revising",
        lambda: _task(lifecycle="active", ball_reason="revise", ball_prompt="Fix."),
        None,
        None,
        StatusFacts(),
        "Working",
        C.WORKING,
    ),
    (
        "finishing",
        lambda: _task(lifecycle="active", ball_reason="work", ball_prompt="Go."),
        None,
        FINISH,
        StatusFacts(),
        "Landing",
        C.FINISHING,
    ),
    *[
        (
            f"needs-{reason}",
            (lambda r=reason: _task(ball="human", ball_reason=r, ball_prompt="You.")),
            None,
            None,
            StatusFacts(),
            f"Needs {reason}",
            C.NEEDS_YOU,
        )
        for reason in ("spec", "review", "decision", "approval", "input")
    ],
    (
        "cycle",
        lambda: _task(),
        None,
        None,
        StatusFacts(needs_cycle=True),
        "Error",
        C.NEEDS_YOU,
    ),
    (
        "blocked-by-an-unmet-need",
        lambda: _task(),
        None,
        None,
        StatusFacts(unmet_needs=True),
        "Blocked",
        C.NOT_NOW,
    ),
    (
        "blocked-on-a-dependency",
        lambda: _task(
            lifecycle="active",
            ball="external",
            ball_reason="dependency",
            ball_prompt="Waiting on task-044.",
            dependencies=[{"task": "task-044", "type": "needs"}],
        ),
        None,
        None,
        StatusFacts(),
        "Blocked",
        C.NOT_NOW,
    ),
    (
        "blocked-on-a-service",
        lambda: _task(
            lifecycle="active", ball="external", ball_reason="service", ball_prompt="Down."
        ),
        None,
        None,
        StatusFacts(),
        "Blocked",
        C.NOT_NOW,
    ),
    (
        "on-hold",
        lambda: _task(lifecycle="active", ball_reason="hold", ball_prompt="Held."),
        None,
        None,
        StatusFacts(),
        "On hold",
        C.NOT_NOW,
    ),
    ("quota", _quota_park, None, None, StatusFacts(), "Quota", C.NOT_NOW),
    (
        "draft",
        lambda: _task(lifecycle="draft"),
        None,
        None,
        StatusFacts(),
        "Draft",
        C.DRAFT,
    ),
    (
        "draft-handed-to-an-agent",
        lambda: _task(lifecycle="draft", ball_reason="work", ball_prompt="Draft the spec."),
        None,
        None,
        StatusFacts(),
        "Draft",
        C.DRAFT,
    ),
    (
        "draft-waiting-on-its-spec",
        lambda: _task(lifecycle="draft", ball="human", ball_reason="spec", ball_prompt="Spec."),
        None,
        None,
        StatusFacts(),
        "Needs spec",
        C.NEEDS_YOU,
    ),
    *[
        (
            f"closed-{outcome}",
            (lambda o=outcome: _task(lifecycle="closed", ball=None, ball_reason=None, outcome=o)),
            None,
            None,
            StatusFacts(),
            outcome.capitalize(),
            C.CLOSED if outcome == "completed" else C.CLOSED_UNFINISHED,
        )
        for outcome in ("completed", "superseded", "cancelled", "duplicate")
    ],
]


@pytest.mark.parametrize(
    ("factory", "queued", "finish", "facts", "label", "category"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_every_state_has_exactly_the_table_s_label_and_category(
    factory: Any,
    queued: Optional[QueuedDispatchState],
    finish: Optional[LiveFinishState],
    facts: StatusFacts,
    label: str,
    category: StatusCategory,
) -> None:
    status = task_status(factory(), queued, finish, facts)
    assert (status.label, status.category) == (label, category)


def test_every_category_is_reached_by_some_state() -> None:
    """A category nothing produces is a colour the frontend maps for no reason."""
    assert {case[-1] for case in CASES} == set(StatusCategory)


def test_a_label_belongs_to_exactly_one_category() -> None:
    """The rule the table rests on: the colour is a function of the word."""
    seen: Dict[str, StatusCategory] = {}
    for case in CASES:
        label, category = case[-2], case[-1]
        assert seen.setdefault(label, category) is category, label


@pytest.mark.parametrize(
    "facts",
    [
        StatusFacts(unmet_needs=True),
        StatusFacts(needs_cycle=True),
    ],
    ids=["unmet-needs", "cycle"],
)
def test_a_task_that_cannot_start_never_reads_ready(facts: StatusFacts) -> None:
    """Before task-562 the list drew a non-actionable fallback as a grey "Ready"."""
    status = task_status(_task(), None, None, facts)
    assert status.label != "Ready"
    assert status.category is not StatusCategory.READY


@pytest.mark.parametrize("queued", [QUEUED, STARTING], ids=["queued", "starting"])
def test_a_queued_dispatch_does_not_hide_a_block(queued: QueuedDispatchState) -> None:
    status = task_status(_task(), queued, None, StatusFacts(unmet_needs=True))
    assert status.label == "Blocked"


def test_a_human_ball_outranks_an_unmet_need() -> None:
    """Red is for what a person must act on; the spec can be written while blocked."""
    task = _task(ball="human", ball_reason="spec", ball_prompt="Specify.")
    status = task_status(task, None, None, StatusFacts(unmet_needs=True))
    assert (status.label, status.category) == ("Needs spec", StatusCategory.NEEDS_YOU)


def test_a_closed_task_keeps_its_outcome_through_a_finish() -> None:
    task = _task(lifecycle="closed", ball=None, ball_reason=None, outcome="completed")
    assert task_status(task, None, FINISH).label == "Completed"


def test_archived_is_not_in_the_chip() -> None:
    task = _task(
        lifecycle="closed", ball=None, ball_reason=None, outcome="superseded", archived=True
    )
    assert task.display_status == "Superseded"


@pytest.mark.parametrize("outcome", list(Outcome))
def test_a_closure_row_and_a_task_read_say_the_same_thing(outcome: Outcome) -> None:
    """Recently finished draws from closed_status; a task read must agree with it."""
    task = _task(lifecycle="closed", ball=None, ball_reason=None, outcome=outcome.value)
    assert closed_status(outcome) == task_status(task)


def test_the_categories_are_exactly_the_data_file_s() -> None:
    """The enum and status_vocabulary.json must name the same categories."""
    assert set(STATUS_VOCABULARY["categories"]) == {category.value for category in StatusCategory}


def test_every_label_comes_from_the_data_file() -> None:
    """No word in CASES is spelled only in this module: the file is where they live."""
    labels = {entry["label"] for entry in STATUS_VOCABULARY["statuses"].values()}
    assert {case[-2] for case in CASES} <= labels


@pytest.mark.parametrize("section", ["statuses", "run_health", "walk"])
def test_every_entry_names_a_real_category(section: str) -> None:
    for key, entry in STATUS_VOCABULARY[section].items():
        assert entry["category"] in STATUS_VOCABULARY["categories"], (section, key)
        assert entry["label"] and entry["label"][0].isupper(), (section, key)


def test_every_category_has_a_colour_of_its_own() -> None:
    """One colour per category: no two categories share a fill and border."""
    looks = [
        (colours["fill"], colours["border"], colours.get("border_style", "solid"))
        for colours in STATUS_VOCABULARY["categories"].values()
    ]
    assert len(set(looks)) == len(looks)


def test_an_epic_nobody_holds_reads_ready() -> None:
    """The owner's decision (2026-09-24): no "Sub-tasks" chip; an epic is claimable."""
    status = task_status(_task(), None, None, StatusFacts())
    assert (status.label, status.category) == ("Ready", StatusCategory.READY)


def test_a_finishing_run_and_a_finishing_task_say_the_same_word() -> None:
    assert STATUS_VOCABULARY["run_health"]["finishing"]["label"] == status_named("finishing").label
    assert STATUS_VOCABULARY["run_health"]["working"]["label"] == status_named("working").label
