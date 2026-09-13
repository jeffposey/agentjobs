"""The task store's log feed: positions that only grow (task-264, migration 004).

The execution journal's inbox cursor is only as good as this: a position reused after a
delete, or renumbered by a vacuum, would let the cursor step silently past a handoff.
"""

from __future__ import annotations

from pathlib import Path

from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType
from support import task_store


def make_manager(tmp_path: Path, monkeypatch) -> TaskManager:
    monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
    return TaskManager(task_store(tmp_path / "proj" / "tasks", project_id="feedproj"))


def create(manager: TaskManager, title: str) -> str:
    created = manager.create_task(
        title=title,
        category="general",
        summary="s",
        description="d",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
    )
    # A note as well, so every task has an entry whatever creation itself logs.
    manager.add_log_entry(created.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go.")
    return created.id


def test_every_entry_appears_once_in_commit_order_with_growing_positions(
    tmp_path: Path, monkeypatch
) -> None:
    manager = make_manager(tmp_path, monkeypatch)
    first = create(manager, "One")
    second = create(manager, "Two")
    manager.claim_task(first, agent="claude")
    manager.handoff(
        first, actor="claude", ball=Ball.HUMAN, ball_reason=BallReason.REVIEW, ball_prompt="Look."
    )
    feed = manager.source_events(0, 1000)
    positions = [row["position"] for row in feed]
    assert positions == sorted(positions) and len(set(positions)) == len(positions)
    identities = [(row["task_id"], row["entry_id"]) for row in feed]
    assert len(identities) == len(set(identities))
    assert {task for task, _ in identities} == {first, second}
    notes = [row for row in feed if row["type"] == "note"]
    assert notes and all(row["data"] == {} for row in notes), "only signals carry data"
    assert [row["type"] for row in feed if row["task_id"] == first][-1] == "handoff"


def test_the_feed_is_bounded_and_resumes_after_a_position(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path, monkeypatch)
    for index in range(5):
        create(manager, f"Task {index}")
    everything = manager.source_events(0, 1000)
    page = manager.source_events(0, 2)
    rest = manager.source_events(page[-1]["position"], 1000)
    assert len(page) == 2
    assert page + rest == everything


def test_a_deleted_tasks_positions_are_never_handed_out_again(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path, monkeypatch)
    create(manager, "Kept")
    doomed = create(manager, "Doomed")
    before = manager.source_events(0, 1000)
    high_water = before[-1]["position"]
    doomed_positions = {row["position"] for row in before if row["task_id"] == doomed}
    assert manager.storage.delete_task(doomed)
    # The next id may well be the deleted one again; the positions must not be.
    later = create(manager, "Later")
    after = manager.source_events(0, 1000)
    assert not doomed_positions & {row["position"] for row in after}
    fresh = [row for row in after if row["task_id"] == later]
    assert fresh and min(row["position"] for row in fresh) > high_water
