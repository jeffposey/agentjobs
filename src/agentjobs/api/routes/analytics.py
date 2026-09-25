"""The analytics endpoint: one request, the whole page (docs/analytics-design.md §7).

Deliberately thin. Everything that decides a number lives in ``agentjobs.analytics``,
which is where it can be tested against a store without an HTTP client in the way; this
module resolves the project, names the range and validates the shape.

Two things it does **not** inherit from ``dashboard.py``, both on purpose. It does not
catch ``QueueCorruptionError``, because nothing here reads the queue order and offering
the repair command from a page that does not depend on it would be noise (§7.4). And it
does not go through ``TaskManager``: the projection reads three tables through indexed
scans, and routing it via the manager would mean assembling task documents to count
them, which is the cost the page waited for task-273's storage to avoid.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from agentjobs.analytics import DEFAULT_RANGE, build_analytics
from agentjobs.store_factory import TaskStoreBackend

from ..dependencies import get_task_storage
from ..models import AnalyticsResponse, EstimatorResetResult

router = APIRouter(tags=["analytics"])


@router.get("/analytics", response_model=AnalyticsResponse)
async def get_analytics(
    range: str = Query(
        default=DEFAULT_RANGE,
        pattern="^(30d|90d|12m|all)$",
        description="How far back to look. Four presets are the whole surface (§7.1).",
    ),
    storage: TaskStoreBackend = Depends(get_task_storage),
) -> AnalyticsResponse:
    """Backlog, throughput, aging and where work is stuck, over one window.

    **A project with no history is a 200, not an error** (§7.4): the totals are
    populated from ``task``, every series is empty and ``coverage.baseline_at`` is null,
    which is what lets the page tell "nothing has happened here" from "we do not know
    what happened". Those are different sentences and the page renders them differently.
    """
    return AnalyticsResponse.model_validate(build_analytics(storage, range))


@router.post("/analytics/finish-estimator/reset", response_model=EstimatorResetResult)
async def reset_finish_estimator(
    storage: TaskStoreBackend = Depends(get_task_storage),
) -> EstimatorResetResult:
    """Forget the landing estimate's learned correction (task-586).

    Deletes nothing: every prediction and every finish stays, and the accuracy chart
    keeps scoring them. Only landings that start after this moment teach the bias factor,
    so the estimate returns to the uncorrected medians until three of them have ended.
    An owner's act, because it changes what every Landing row shows.
    """
    return EstimatorResetResult(reset_at=storage.reset_finish_estimator())
