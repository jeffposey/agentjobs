"""The history index: where a finish and a gate record themselves (task-472).

Two ``PUT`` routes, each an upsert. The finisher writes its ``finish`` row every time it
writes ``meta.yaml`` and again with every step; the gate writes its ``gate_run`` row when
it starts, after each stage and when it ends. Both send the whole record every time, so
a lost or repeated request changes nothing -- which is the property that lets a writer
that must never fail the thing it measures fire and forget.

Neither writer is the server. The scripted finish is a CLI process holding a
:class:`~agentjobs.remote_manager.RemoteTaskManager`, and the gate is a script in a
worktree; only the server opens the database (task-273), so this is the door. The rows
these write are read by the analytics series (analytics design section 21) and by the
landing estimate.

**Each write is also a checkpoint for the landing estimate (task-586).** A finish writes
when a step lands, which is the next step starting; a gate writes when a stage lands,
which is the next stage starting. So after a native write for a running finish, the
server records the time remaining it predicts from there, and once the finish ends the
prediction's error is known. That recording may never fail the write it follows: a
missing prediction is a gap in the accuracy chart, a failed write is a gap in history.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from agentjobs.manager import TaskManager

from ..dependencies import get_task_manager
from ..models import FinishHistoryWrite, GateHistoryWrite, HistoryWriteResult

router = APIRouter(prefix="/history", tags=["history"])

logger = logging.getLogger(__name__)


def _checkpoint(manager: TaskManager, finish_id: str) -> None:
    """Record the landing estimate's prediction for this finish, if it is at one."""
    try:
        manager.record_finish_checkpoint(finish_id)
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.warning("could not record a landing prediction for %s", finish_id, exc_info=True)


@router.put("/finishes/{finish_id}", response_model=HistoryWriteResult)
async def record_finish_history(
    finish_id: str,
    payload: FinishHistoryWrite,
    manager: TaskManager = Depends(get_task_manager),
) -> HistoryWriteResult:
    """Index one scripted finish and the steps it has taken so far.

    ``written`` is false, with a reason, when an imported record met a row already there
    or when the finish names a task this project does not have. Both are answers rather
    than errors: the importer reports them, and the finisher never sends either.
    """
    outcome = manager.record_finish(
        finish_id,
        payload.record.model_dump(mode="json"),
        [step.model_dump(mode="json") for step in payload.steps],
    )
    record = payload.record
    if outcome.written and record.source == "native" and record.outcome == "running":
        _checkpoint(manager, finish_id)
    return HistoryWriteResult(written=outcome.written, reason=outcome.reason)


@router.put("/gates/{gate_id}", response_model=HistoryWriteResult)
async def record_gate_history(
    gate_id: str,
    payload: GateHistoryWrite,
    manager: TaskManager = Depends(get_task_manager),
) -> HistoryWriteResult:
    """Index one gate run and the stages it has finished so far."""
    outcome = manager.record_gate_run(
        gate_id,
        payload.record.model_dump(mode="json"),
        [stage.model_dump(mode="json") for stage in payload.stages],
    )
    record = payload.record
    if (
        outcome.written
        and record.source == "native"
        and record.origin == "finish"
        and record.finish_id
        and not record.finished_at
    ):
        _checkpoint(manager, record.finish_id)
    return HistoryWriteResult(written=outcome.written, reason=outcome.reason)
