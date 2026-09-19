"""The history index: where a finish and a gate record themselves (task-472).

Two ``PUT`` routes, each an upsert. The finisher writes its ``finish`` row every time it
writes ``meta.yaml`` and again with every step; the gate writes its ``gate_run`` row when
it starts, after each stage and when it ends. Both send the whole record every time, so
a lost or repeated request changes nothing -- which is the property that lets a writer
that must never fail the thing it measures fire and forget.

Neither writer is the server. The scripted finish is a CLI process holding a
:class:`~agentjobs.remote_manager.RemoteTaskManager`, and the gate is a script in a
worktree; only the server opens the database (task-273), so this is the door. The rows
these write are read by nothing here yet -- the analytics series over them are the API
child's (analytics design section 21).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from agentjobs.manager import TaskManager

from ..dependencies import get_task_manager
from ..models import FinishHistoryWrite, GateHistoryWrite, HistoryWriteResult

router = APIRouter(prefix="/history", tags=["history"])


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
    return HistoryWriteResult(written=outcome.written, reason=outcome.reason)
