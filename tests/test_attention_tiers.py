"""The human inbox has two tiers, and the task list defaults to open work.

The defect these cover: every surface counted ``ball: human`` with one predicate, so a
draft parked on an unmade design decision raised the same red badge as a finished branch
sitting at the merge gate. A badge that never reaches zero can only be read by opening
it, which is the work the badge existed to save -- and the observed consequence was a
user reaching to archive a task he wanted to keep, purely to clear the number.

Assertions here are on rendered values a browser acts on (the badge number, the argument
handed to the Alpine component), not on the presence of markup.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.api.routes.web import awaits_human_input, blocks_human
from agentjobs.models_v2 import (
    Assignment,
    Ball,
    BallReason,
    Lifecycle,
    Outcome,
    Priority,
    Spec,
    Task,
)
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage

NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)


def make_task(
    task_id: str,
    *,
    lifecycle: Lifecycle,
    ball: Ball | None,
    ball_reason: BallReason | None,
    outcome: Outcome | None = None,
) -> Task:
    """A minimally-valid task pinned to the state axes under test.

    An active task must name an owner, so one is supplied whenever the lifecycle
    requires it rather than at every call site.
    """
    return Task(
        id=task_id,
        assignment=Assignment(owner="claude" if lifecycle is Lifecycle.ACTIVE else None),
        title=f"Title of {task_id}",
        created=NOW,
        updated=NOW,
        lifecycle=lifecycle,
        ball=ball,
        ball_reason=ball_reason,
        ball_prompt="Do the thing." if ball else None,
        outcome=outcome,
        priority=Priority.MEDIUM,
        category="general",
        spec=Spec(summary=f"Summary of {task_id}", description="Body."),
    )


BLOCKED_ON_HUMAN = make_task(
    "task-901-at-the-merge-gate",
    lifecycle=Lifecycle.ACTIVE,
    ball=Ball.HUMAN,
    ball_reason=BallReason.REVIEW,
)
PARKED_DRAFT = make_task(
    "task-902-parked-draft",
    lifecycle=Lifecycle.DRAFT,
    ball=Ball.HUMAN,
    ball_reason=BallReason.SPEC,
)
CLAIMABLE = make_task(
    "task-903-claimable",
    lifecycle=Lifecycle.READY,
    ball=Ball.AGENT,
    ball_reason=BallReason.AVAILABLE,
)
FINISHED = make_task(
    "task-904-finished",
    lifecycle=Lifecycle.CLOSED,
    ball=None,
    ball_reason=None,
    outcome=Outcome.COMPLETED,
)


class TestTheTwoPredicates:
    """One definition of each tier, so no surface can drift from another."""

    def test_a_non_draft_task_held_by_a_human_blocks(self) -> None:
        assert blocks_human(BLOCKED_ON_HUMAN)
        assert not awaits_human_input(BLOCKED_ON_HUMAN)

    def test_a_draft_held_by_a_human_is_backlog_not_a_blockage(self) -> None:
        assert awaits_human_input(PARKED_DRAFT)
        assert not blocks_human(PARKED_DRAFT)

    @pytest.mark.parametrize("task", [CLAIMABLE, FINISHED], ids=["claimable", "finished"])
    def test_tasks_no_human_holds_are_in_neither_tier(self, task: Task) -> None:
        assert not blocks_human(task)
        assert not awaits_human_input(task)

    def test_a_ready_task_handed_back_to_a_human_still_blocks(self) -> None:
        """Only ``draft`` is quiet. A ready task on a human is stalled work."""
        stalled = make_task(
            "task-905-stalled",
            lifecycle=Lifecycle.READY,
            ball=Ball.HUMAN,
            ball_reason=BallReason.DECISION,
        )

        assert blocks_human(stalled)
        assert not awaits_human_input(stalled)


def build_project(root: Path, tasks: list[Task]) -> None:
    """A registered project directory holding exactly the given tasks."""
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": "Inbox", "tasks_directory": "tasks"}),
        encoding="utf-8",
    )
    storage = TaskStorage(root / "tasks")
    for task in tasks:
        storage.save_task(task)


@pytest.fixture()
def client_for(tmp_path: Path, monkeypatch):
    """Build a one-project server around a given set of tasks."""

    def build(tasks: list[Task]) -> Tuple[TestClient, str]:
        monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
        monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
        monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        reset_dependency_cache()

        build_project(tmp_path / "inbox", tasks)
        ProjectRegistry(home=tmp_path / "home").add(tmp_path / "inbox", project_id="inbox")
        return TestClient(app), "/p/inbox"

    yield build
    reset_dependency_cache()


def badge_number(html: str) -> int | None:
    """The number a user actually sees on the Tasks badge, or None when absent.

    Rendered as ``<span class="absolute -top-1 ...">N</span>``; the class is only a
    handle for finding it, and the assertion is on N.
    """
    marker = 'class="absolute -top-1'
    if marker not in html:
        return None
    tail = html.split(marker, 1)[1]
    return int(tail.split(">", 1)[1].split("<", 1)[0].strip())


class TestTheBadgeCountsOnlyBlockedWork:
    def test_a_parked_draft_raises_no_badge(self, client_for) -> None:
        """The whole point: nothing is stopped, so nothing should be alarming."""
        client, base = client_for([PARKED_DRAFT, CLAIMABLE])

        assert badge_number(client.get(f"{base}/").text) is None

    def test_a_task_at_the_merge_gate_raises_a_badge_of_one(self, client_for) -> None:
        client, base = client_for([BLOCKED_ON_HUMAN, PARKED_DRAFT, CLAIMABLE])

        assert badge_number(client.get(f"{base}/").text) == 1

    def test_the_badge_agrees_across_pages(self, client_for) -> None:
        """Dashboard, task list and task detail share one number or none is trustworthy."""
        client, base = client_for([BLOCKED_ON_HUMAN, PARKED_DRAFT])

        counts = {
            path: badge_number(client.get(f"{base}{path}").text)
            for path in ("/", "/tasks", f"/tasks/{BLOCKED_ON_HUMAN.id}")
        }

        assert set(counts.values()) == {1}, counts


# The alert panel's own sentence. "Blocked on You" is not usable as a handle: it is
# also the label of a stat tile, which renders whether or not the panel does.
ALERT_PANEL = "Work has stopped on these until you act."
BACKLOG_PANEL = "Backlog awaiting your input"


class TestTheDashboardSeparatesTheTiers:
    def test_the_alert_panel_omits_parked_drafts(self, client_for) -> None:
        client, base = client_for([BLOCKED_ON_HUMAN, PARKED_DRAFT])

        page = client.get(f"{base}/").text
        alert = page.split(ALERT_PANEL, 1)[1].split(BACKLOG_PANEL, 1)[0]

        assert BLOCKED_ON_HUMAN.id in alert
        assert PARKED_DRAFT.id not in alert

    def test_the_backlog_section_lists_the_parked_draft(self, client_for) -> None:
        client, base = client_for([BLOCKED_ON_HUMAN, PARKED_DRAFT])

        page = client.get(f"{base}/").text
        backlog = page.split(BACKLOG_PANEL, 1)[1]

        assert PARKED_DRAFT.id in backlog

    def test_a_parked_draft_is_still_visible_with_no_alert(self, client_for) -> None:
        """Quieting the backlog must not hide it -- a rotting draft is the worse bug."""
        client, base = client_for([PARKED_DRAFT])

        page = client.get(f"{base}/").text

        assert BACKLOG_PANEL in page
        assert PARKED_DRAFT.id in page
        assert ALERT_PANEL not in page
