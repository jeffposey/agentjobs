"""Drafting a task spec with a model: one status read and one call (task-175).

Two routes and one subject. ``GET /api/model`` says whether this machine can draft
right now; ``POST .../model/draft`` expands what a person typed into spec fields and
returns them. **Neither creates, edits or moves a task.** The draft goes back to the
form the person is standing in, they edit it, and they press create -- which is the
ordinary create route, unchanged, and is the only thing that writes a record.

**No run may call the draft route.** That is the ``model.draft`` capability, enforced in
:mod:`agentjobs.api.authorization` like every other route rather than asserted here, and
it is what stops an agent spending the operator's model budget. It also forecloses the
loop without a new argument: no amount of agent activity can spend a budget no agent can
reach.

**The status read is not on the capability table**, which is the one deliberate omission
in this module. It answers a boolean about this machine's own configuration and leaks
nothing -- there is no field on :class:`~agentjobs.api.models.ModelStatusResponse` that a
credential could occupy -- and gating it would make a run unable to discover that it may
not draft, which is worse than useless. A run that reads it still cannot call the route
it describes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from agentjobs.modelaccess import ModelCallError, availability, draft_spec
from agentjobs.projects import Project

from ..dependencies import get_project
from ..models import ModelStatusResponse, SpecDraftRequest, SpecDraftResponse

router = APIRouter(tags=["model"])


@router.get("/model", response_model=ModelStatusResponse)
async def get_model_status() -> ModelStatusResponse:
    """Whether a drafting call can be made on this machine, and why not when it cannot.

    Names no project: what is configured is a property of the machine, and a per-project
    answer would imply a per-project credential, which is the one arrangement the design
    rules out (``docs/model-access-design.md`` §4).
    """
    state = availability()
    return ModelStatusResponse(
        available=state.available,
        reason=state.reason,
        detail=state.detail,
        model=state.model,
        calls_per_hour=state.calls_per_hour,
        calls_used=state.calls_used,
    )


@router.post("/model/draft", response_model=SpecDraftResponse)
async def draft_task_spec(
    payload: SpecDraftRequest,
    project: Project = Depends(get_project),
) -> SpecDraftResponse:
    """Expand what a person typed into spec fields. Writes nothing.

    Project-scoped because the draft is told which project it is being filed into, which
    is context a spec legitimately reflects. It reads nothing else about the project and
    writes nothing to it.

    Every refusal comes back as a 200 with ``drafted: false``; see
    :class:`~agentjobs.api.models.SpecDraftResponse` for why that is the shape.
    """
    try:
        draft = draft_spec(payload.title, payload.description, project.id)
    except ModelCallError as exc:
        # `exc.reason` and `exc.detail` are both written in this repository --
        # `ModelCallError` never keeps a provider's response -- so there is no scrubbing
        # step here and there must never need to be.
        return SpecDraftResponse(drafted=False, reason=exc.reason, detail=exc.detail)
    return SpecDraftResponse(
        drafted=True,
        summary=draft.summary,
        intent=draft.intent,
        description=draft.description,
        constraints=draft.constraints,
        out_of_scope=draft.out_of_scope,
        acceptance=draft.acceptance,
        model=availability().model,
    )
