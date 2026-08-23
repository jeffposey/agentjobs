"""Read-only endpoints for a project's playbooks.

``docs/playbooks-design.md`` §7.1 and decision P10. There is a collection route and a
single-playbook route, and **that is the whole surface**: running a playbook is
human-gated on every surface and arrives with instantiation (task-215), so nothing here
is a POST.

Mounted like every other project-facing router -- unscoped at ``/api`` and again under
``/api/projects/{project_id}`` -- so the two forms cannot drift apart.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from agentjobs.playbooks import (
    PlaybookContract,
    PlaybookError,
    UnknownPlaybookError,
    list_playbooks,
    read_playbook,
    validate_playbook_name,
)
from agentjobs.projects import Project

from .status import get_acting_project

router = APIRouter(tags=["playbooks"])


class PlaybookRead(PlaybookContract):
    """A playbook's contract, plus where it was read from and (optionally) its brief.

    Subclasses the contract rather than restating it, so a field added to the
    frontmatter model reaches the API without a second edit that can be forgotten.
    """

    filename: str = Field(..., description="The file this was read from, e.g. groom.md.")
    body: Optional[str] = Field(
        default=None,
        description=(
            "The brief: opaque markdown prose an agent reads. Present on the "
            "single-playbook route and omitted from the collection, which is a "
            "discovery listing rather than a way to fetch every brief at once."
        ),
    )


class PlaybookProblemRead(BaseModel):
    """One reason a file in the playbooks directory is not a valid playbook."""

    filename: str
    field: Optional[str] = None
    message: str


class PlaybookCollection(BaseModel):
    """What a project's playbooks directory holds.

    Invalid files are reported beside valid ones for the reason ``/tasks/broken``
    exists: a file that fails validation and then vanishes from the listing reads as a
    playbook nobody ever wrote.
    """

    directory: str
    exists: bool = Field(
        ...,
        description=(
            "False when the directory has not been created. Not an error: it is the "
            "state every project starts in, and `agentjobs playbook init` leaves it."
        ),
    )
    playbooks: List[PlaybookRead] = Field(default_factory=list)
    problems: List[PlaybookProblemRead] = Field(default_factory=list)


@router.get("/playbooks", response_model=PlaybookCollection, response_model_exclude_none=True)
async def get_playbooks(
    project: Project = Depends(get_acting_project),
) -> PlaybookCollection:
    """Every playbook this project holds, with the files that would not load."""
    listing = list_playbooks(project.playbooks_dir())
    return PlaybookCollection(
        directory=str(listing.directory),
        exists=listing.exists,
        playbooks=[
            PlaybookRead(**playbook.contract.model_dump(), filename=playbook.path.name)
            for playbook in listing.playbooks
        ],
        problems=[
            PlaybookProblemRead(
                filename=problem.filename, field=problem.field, message=problem.message
            )
            for problem in listing.problems
        ],
    )


@router.get("/playbooks/{name}", response_model=PlaybookRead)
async def get_playbook(
    name: str,
    project: Project = Depends(get_acting_project),
) -> PlaybookRead:
    """One playbook by name, including its brief.

    422 rather than 404 for a file that exists and does not validate, matching how a
    stored task that will not parse is reported: the thing is there and repairable,
    and "not found" would send its author looking for something already on disk.
    """
    try:
        validate_playbook_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    try:
        playbook = read_playbook(project.playbooks_dir(), name)
    except UnknownPlaybookError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PlaybookError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return PlaybookRead(
        **playbook.contract.model_dump(),
        filename=playbook.path.name,
        body=playbook.body,
    )
