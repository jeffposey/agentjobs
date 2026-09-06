"""The board's cell count and its supply of tasks come from one reading (task-092).

The Dashboard now draws one cell per run slot, and every free cell has to offer a
*different* claimable task. That makes the queue preview's length a **machine** fact
rather than a project one: a machine whose ``max_concurrent_runs`` is six needs six
tasks to fill an idle board, and one that never configured dispatch at all needs the
number the panel has always shown.

The failure this pins is quiet and would look like a design choice: a ceiling of six, an
idle machine, and the board repeating the third task into cells four, five and six --
or, worse, drawing three empty cells beside three real ones and inviting the reader to
conclude the backlog is exhausted.

Both surfaces read :func:`machine_ceiling`, so the count and the supply cannot come from
two different readings of one file. That is asserted here as well: the ceiling
``GET /api/runs/live`` reports and the length the dashboard answers with move together.
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
from agentjobs.dashboard import QUEUE_PREVIEW_LIMIT
from agentjobs.dispatch.config import machine_ceiling
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Spec, Task
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)

MINIMAL_DISPATCH = {
    "version": 1,
    "enabled": True,
    "runners": {
        "sleeper": {"argv": ["python", "-c", "pass", "{prompt}"], "mode": "batch"},
    },
    "projects": {},
}


def ready_task(index: int) -> Task:
    """One claimable task, at a queue position unique within its band.

    Since task-205 two open tasks sharing a position is a corrupt queue and selection
    refuses to answer over one, so the position is derived from the index rather than
    left to a default.
    """
    return Task(
        id=f"task-9{index:02d}-ready",
        title=f"Ready task {index}",
        created=NOW,
        updated=NOW,
        lifecycle=Lifecycle.READY,
        ball=Ball.AGENT,
        ball_reason=BallReason.AVAILABLE,
        queue_position=(index + 1) * 100,
        category="general",
        spec=Spec(summary=f"Claimable task {index}.", description="Enough to brief."),
    )


@pytest.fixture()
def board_server(tmp_path: Path, monkeypatch):
    """A one-project server whose machine has whatever ceiling the test asks for."""

    def build(ceiling: int | None, task_count: int) -> Tuple[TestClient, Path]:
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("AGENTJOBS_HOME", str(home))
        monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
        monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        reset_dependency_cache()

        if ceiling is not None:
            config = dict(MINIMAL_DISPATCH, limits={"max_concurrent_runs": ceiling})
            (home / "dispatch.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

        root = tmp_path / "inbox"
        (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
        (root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump({"project_name": "Inbox", "tasks_directory": "tasks"}),
            encoding="utf-8",
        )
        storage = TaskStorage(root / "tasks")
        for index in range(task_count):
            storage.save_task(ready_task(index))
        ProjectRegistry(home=home).add(root, project_id="inbox")
        return TestClient(app), home

    yield build
    reset_dependency_cache()


class TestTheSupplyFollowsTheCeiling:
    @pytest.mark.parametrize("ceiling", [4, 6])
    def test_a_bigger_board_is_offered_a_bigger_frontier(self, board_server, ceiling) -> None:
        client, _ = board_server(ceiling, task_count=8)

        preview = client.get("/api/projects/inbox/dashboard").json()["queue_preview"]

        assert len(preview) == ceiling
        # Different tasks, not the same one repeated: this is the whole point.
        assert len({task["id"] for task in preview}) == ceiling

    def test_a_small_ceiling_never_shrinks_the_panel_below_its_floor(self, board_server) -> None:
        """A ceiling of one is one cell, and the preview is still what it always was.

        ``QUEUE_PREVIEW_LIMIT`` is a floor rather than the answer (task-092). Letting the
        ceiling drive it downwards would mean a single-slot machine could no longer see
        what is behind the task it is about to start, which is a regression dressed as
        consistency.
        """
        client, _ = board_server(1, task_count=8)

        preview = client.get("/api/projects/inbox/dashboard").json()["queue_preview"]

        assert len(preview) == QUEUE_PREVIEW_LIMIT

    def test_an_unconfigured_machine_gets_the_floor_and_says_so(self, board_server) -> None:
        client, home = board_server(None, task_count=8)

        assert machine_ceiling(home) == (1, False)
        live = client.get("/api/runs/live").json()
        assert live["dispatch_configured"] is False
        assert len(client.get("/api/projects/inbox/dashboard").json()["queue_preview"]) == (
            QUEUE_PREVIEW_LIMIT
        )

    def test_the_two_surfaces_read_one_ceiling(self, board_server) -> None:
        """The board's cell count and its supply cannot disagree about the machine.

        `GET /api/runs/live` reports the number of cells; the dashboard supplies the
        tasks for them. Both go through ``machine_ceiling``, so this asserts the two
        halves of one board against each other rather than each against a literal.
        """
        client, _ = board_server(5, task_count=8)

        cells = client.get("/api/runs/live").json()["max_concurrent_runs"]
        preview = client.get("/api/projects/inbox/dashboard").json()["queue_preview"]

        assert cells == 5
        assert len(preview) == cells

    def test_the_frontier_is_short_when_the_queue_is(self, board_server) -> None:
        """Two claimable tasks and six slots is two cards and four empty ones.

        The server does not pad. What an empty cell says is decided in the browser --
        there is nothing claimable to put in it -- and inventing a filler task here would
        put a task on the board that ``claimable_tasks`` deliberately excluded.
        """
        client, _ = board_server(6, task_count=2)

        preview = client.get("/api/projects/inbox/dashboard").json()["queue_preview"]

        assert len(preview) == 2
