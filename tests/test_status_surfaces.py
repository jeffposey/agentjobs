"""A run whose task is being merged reads what the task reads (task-533).

The slot board rendered a run's ``health`` and the task page rendered the task's
``display_status``, and nothing made them agree: task-369 read "Working" on one and
"Finishing" on the other. ``tests/test_live_runs_api.py`` covers the endpoint; these pin
the two halves that make the agreement structural rather than coincidental -- the
ranking ``run_health`` states, and the word each side draws.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentjobs.dispatch.ledger import (
    HEALTH_FINISHING,
    HEALTH_HANDBACK,
    HEALTH_PARKED,
    HEALTH_SILENT,
    HEALTH_WORK_DONE,
    HEALTH_WORKING,
    RunRecord,
    run_health,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import STATUS_VOCABULARY, LiveFinishState, derived_display_status
from support import task_store

LIVE_RUNS_TSX = Path(__file__).resolve().parents[1] / "frontend/src/components/LiveRuns.tsx"


def _run(tmp_path: Path, **fields: object) -> RunRecord:
    base: dict = {"run_id": "run_a", "path": tmp_path, "mode": "session", "status": "running"}
    base.update(fields)
    return RunRecord(**base)


class TestTheRanking:
    """a3: a run with no live finish is unaffected, and the ranking is stated."""

    @pytest.mark.parametrize(
        ("fields", "without"),
        [
            ({}, HEALTH_WORKING),
            ({"status": "parked"}, HEALTH_PARKED),
            ({"status": "stalled"}, HEALTH_SILENT),
            ({"handback_pending": 3}, HEALTH_HANDBACK),
        ],
    )
    def test_a_live_finish_outranks_what_the_run_would_otherwise_say(
        self, tmp_path: Path, fields: dict, without: str
    ) -> None:
        record = _run(tmp_path, **fields)
        assert run_health(record) == without
        assert run_health(record, finishing=True) == HEALTH_FINISHING

    def test_a_closed_task_reads_work_done_even_with_a_finish_cleaning_up(
        self, tmp_path: Path
    ) -> None:
        """The task page says Completed there, so the tile does not say Finishing."""
        record = _run(tmp_path, slot_released_at=datetime.now(timezone.utc))
        assert run_health(record, finishing=True) == HEALTH_WORK_DONE


class TestOneWord:
    """The word the tile draws is the word the task's chip draws."""

    def test_the_run_label_for_finishing_is_the_task_s_finishing_word(self, tmp_path: Path) -> None:
        manager = TaskManager(task_store(tmp_path / "tasks"))
        manager.create_task(
            id="task-001", title="Merge me", summary="S.", description="D.", actor="claude"
        )
        task = manager.get_task("task-001")
        assert task is not None
        finish = LiveFinishState(
            finish_id="fin_a", state="running", started_at="", current_step="gate"
        )
        task_word = derived_display_status(task, None, finish)

        # Since task-562 the run's word comes from the status data file, which LiveRuns.tsx
        # imports; the file is what this reads, and LiveRuns must still import it.
        run_word = STATUS_VOCABULARY["run_health"]["finishing"]["label"]
        assert run_word == task_word == "Finishing"
        assert "RUN_HEALTH" in LIVE_RUNS_TSX.read_text(encoding="utf-8")
