"""Search endpoint for AgentJobs API."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

from agentjobs.manager import TaskManager

from ..dependencies import get_task_manager
from ..models import TaskRead, TaskSummaryRead

router = APIRouter(prefix="", tags=["search"])


def _query(q: str) -> str:
    """The search text, refusing a blank one rather than answering with the corpus."""
    if not q.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query parameter 'q' must be provided",
        )
    return q


@router.get("/search", response_model=List[TaskSummaryRead])
async def search_tasks(
    q: str, manager: TaskManager = Depends(get_task_manager)
) -> List[TaskSummaryRead]:
    """Search tasks using a case-insensitive substring query.

    Rows, in the same shape and for the same reason ``GET /tasks`` returns rows: a
    result list draws a title, a badge and a place in line. It answered with whole
    ``TaskRead`` records until task-495 -- every matched task's spec prose, acceptance
    criteria and complete log -- which was 10,781 bytes per record and 5.2 MB for 480
    matches. Whole records are at ``GET /search/full``.

    Rows carry the same computed dependency facts as ``GET /tasks`` -- ``actionable``,
    ``unmet_needs``, ``open_children_count`` -- so a result can be acted on without a
    second request per row.
    """
    # Those facts are why this returns a read model rather than the bare stored record.
    # It answered with the stored record until task-180, so the computed fields were
    # absent from every search row, and the MCP summary layer turned the absence of
    # open_children_count into 0 -- a parent with six open children reported as having
    # none.
    #
    # The facts are computed over the whole corpus, never over the hits: a count scoped
    # to what the query matched would report 0 for a parent whose children it did not.
    summaries = manager.search_task_summaries(_query(q))
    facts = manager.dependency_facts(summaries, corpus=manager.list_task_summaries())
    return TaskSummaryRead.from_summaries(facts, summaries)


@router.get("/search/full", response_model=List[TaskRead])
async def search_tasks_full(
    q: str, manager: TaskManager = Depends(get_task_manager)
) -> List[TaskRead]:
    """The same hits, in the same order, as whole records.

    Named so the expensive shape is the one a caller asks for by name -- task-484's
    decision, applied to the second endpoint that had it by default.
    ``TaskClient.search_tasks`` is the caller that needs it: it parses each item into a
    ``Task``, and a row would parse with an empty log, which is a wrong answer rather
    than an error.
    """
    return TaskRead.from_tasks(manager, manager.search_tasks(_query(q)))
