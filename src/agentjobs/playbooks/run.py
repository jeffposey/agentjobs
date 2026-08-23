"""Instantiating a playbook: a run is a task, and the task is dispatched.

``docs/playbooks-design.md`` §4 and the run half of §7. This module is the whole of
"make a playbook happen", and it is deliberately thin -- **it composes existing
machinery and adds no execution of its own**:

- ``target: project`` (groom, reorder): create a run task from the ``run_task``
  frontmatter, then dispatch it. One run task per invocation, dated by creation, and
  it accumulates in the corpus because it *is* the audit trail (§4.1).
- ``target: task`` (flesh-out): dispatch the named task itself, with the playbook
  supplying the brief. No run task, because the record belongs on the task being
  worked -- a flesh-out of task-123 is ordinary work *on* task-123 (§4.1).

Three rules bind and are worth stating where the code is rather than only in the
design:

**The brief is pointed at, never copied.** The prompt gains one line, the run task's
``spec.description`` is a short pointer, and its ``context[]`` names the file. A copy
would fork the moment somebody improves the playbook, which is the same drift argument
that stopped dispatch composing prompts (§4.2).

**Nothing here is a new authorisation surface** (§7.1). The two paths are task-188's,
unchanged: a caller that names the human clicking gets that human's authorising entry
written by the dispatcher, and everyone else is judged on the entry the log already
holds. What is new is only that a *project-target* run creates its task first, and the
creating human's own creation entry is then the newest stored entry -- so the
human-clocked rule is satisfied by a real row on disk, exactly as it always is. The
project's ``default_user`` is never substituted for a human nobody named.

**A playbook cannot widen what executes** (§6.3). Every dispatch gate is reached
identically whether or not a playbook is involved, because the pointer is inert: no
guard reads it. The one thing this module does *before* creating anything is ask
``assert_dispatch_permitted`` whether dispatch is possible at all, so a machine with
dispatch switched off refuses without leaving a run task nobody asked for behind.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ..dispatch.config import DispatchError, assert_dispatch_permitted
from ..dispatch.guards import DispatchRequest, assert_authorizer_is_human, dispatch_task
from ..dispatch.runner import RunHandle
from ..manager import TaskManager
from ..models_v2 import Lifecycle, Priority, Task
from ..projects import Project
from .library import UnknownPlaybookError, read_playbook, validate_playbook_name
from .model import Playbook, PlaybookError, PlaybookTarget
from .pointer import PlaybookPointer, pointer_for

TITLE_PLACEHOLDER = re.compile(r"\{(project|playbook)\}")
"""The substitutions a ``run_task.title`` may use, and the only ones.

A targeted substitution rather than ``str.format``: a title is authored prose, and
``format`` turns an unrecognised brace into a ``KeyError`` at instantiation time --
a run refused because somebody wrote a literal ``{`` in a title. Anything this pattern
does not name is left exactly as it was written.
"""


class PlaybookRunError(Exception):
    """A playbook run was refused before any agent started.

    ``reason`` is a stable code, in the shape the dispatch guards already use, so the
    API can map it to a status and the CLI can print it without matching on prose.
    """

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


class PlaybookDispatchRefused(Exception):
    """The run task was created and then the dispatch itself was refused.

    Separate from :class:`PlaybookRunError` because the two need different things said
    to whoever asked. This one has a **side effect that outlived the failure**: a run
    task exists, it is ``ready``, and it can be dispatched later from the dashboard once
    the machine is free. Saying only "concurrency_limit" would leave a task in the
    backlog that nobody knows the origin of.

    Nothing is deleted to tidy up. A run task records that a human asked for a groom
    run, which is true whether or not the machine had a slot, and deleting task records
    to make a failure look neater is not a trade this project makes.
    """

    def __init__(self, cause: DispatchError, run_task_id: str) -> None:
        self.cause = cause
        self.reason: str = getattr(cause, "reason", "dispatch_refused")
        self.run_task_id = run_task_id
        super().__init__(
            f"{cause} The run task {run_task_id} was created before this refusal and is "
            "on the record; dispatch it from the task page once the cause is cleared."
        )


@dataclass(frozen=True)
class PlaybookRunResult:
    """What a run produced: which brief, against which task, as which run."""

    playbook: Playbook
    pointer: PlaybookPointer
    task_id: str
    created_run_task: bool
    """True when this invocation created the task it dispatched (``target: project``)."""
    handle: RunHandle


def render_title(template: str, *, project_id: str, playbook_name: str) -> str:
    """Fill ``{project}`` and ``{playbook}`` in a ``run_task.title``, leaving the rest."""
    values = {"project": project_id, "playbook": playbook_name}
    return TITLE_PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def _load(project: Project, name: str) -> Playbook:
    """Read one playbook, translating every failure into a coded refusal."""
    try:
        validate_playbook_name(name)
    except ValueError as exc:
        raise PlaybookRunError("invalid_playbook_name", str(exc)) from exc
    try:
        return read_playbook(project.playbooks_dir(), name)
    except UnknownPlaybookError as exc:
        raise PlaybookRunError("unknown_playbook", str(exc)) from exc
    except PlaybookError as exc:
        # Includes the frontmatter a playbook may not have. `kind: reactive` is the case
        # worth naming: the reactive category was withdrawn (design decision P8), there
        # is no `kind` field, and the contract model forbids unknown keys -- so a file
        # declaring one never loads and therefore can never be run. That refusal is
        # structural rather than a check anybody had to remember to write.
        raise PlaybookRunError(
            "invalid_playbook", f"{name} does not validate, so nothing was run: {exc}"
        ) from exc


def _assert_shape_matches(playbook: Playbook, task_id: Optional[str]) -> None:
    """Refuse an invocation whose shape disagrees with the playbook's ``target``.

    Both directions, and neither is guessed past. A project-target playbook invoked at
    one task would silently dispatch that task with a brief written for the whole
    backlog; a task-target playbook invoked with no task has nothing to aim at, and
    inventing a run task for it would create the second record §4.1 exists to avoid.
    """
    target = playbook.contract.target
    if target is PlaybookTarget.PROJECT and task_id is not None:
        raise PlaybookRunError(
            "target_mismatch",
            f"Playbook {playbook.name!r} targets the whole project: a run creates its "
            f"own run task. It cannot be aimed at {task_id}, so nothing was started.",
        )
    if target is PlaybookTarget.TASK and task_id is None:
        raise PlaybookRunError(
            "target_mismatch",
            f"Playbook {playbook.name!r} targets one task and does not create a run "
            "task of its own. Name the task it should be run against.",
        )


def _run_task_payload(
    playbook: Playbook, pointer: PlaybookPointer, *, project_id: str
) -> Dict[str, Any]:
    """The run task's fields, from ``run_task`` frontmatter and the pointer.

    ``spec.description`` is the pointer's sentence and ``context[]`` names the file.
    Neither carries the brief, and a test asserts the brief's text appears in neither
    the task nor the prompt.
    """
    defaults = playbook.contract.run_task
    if defaults is None:
        raise PlaybookRunError(
            "no_run_task_defaults",
            f"Playbook {playbook.name!r} targets the whole project but declares no "
            "`run_task:` frontmatter, so there is nothing to create the run task from. "
            "Give it at least a title.",
        )
    acceptance = [
        {"id": f"ac-{index}", "text": criterion.text, "verify": criterion.verify}
        for index, criterion in enumerate(defaults.acceptance, start=1)
    ]
    return {
        "title": render_title(defaults.title, project_id=project_id, playbook_name=playbook.name),
        "description": pointer.spec_description(),
        "summary": f"Run of the `{playbook.name}` playbook: {playbook.description}",
        "category": defaults.category or "general",
        "priority": defaults.priority or Priority.MEDIUM,
        "tags": list(defaults.tags),
        "acceptance": acceptance,
        "spec": {
            "summary": f"Run of the `{playbook.name}` playbook: {playbook.description}",
            "description": pointer.spec_description(),
            "context": [{"path": pointer.path, "why": pointer.context_reason()}],
        },
    }


def create_run_task(
    manager: TaskManager,
    playbook: Playbook,
    pointer: PlaybookPointer,
    *,
    project_id: str,
    created_by: str,
) -> Task:
    """Create the ``ready`` run task a project-target playbook is dispatched against.

    ``created_by`` is the human who asked, and naming them is not bookkeeping: the
    manager writes a creation entry only when a creator is named, and that entry is what
    the human-clocked rule then reads. A run task created by nobody could never be
    dispatched, which is why this function has no unattributed path.

    Born ``ready`` rather than ``draft``: it is dispatched in the next breath, and a
    draft is not claimable. Its place in line is the bottom of its band, like every
    other task nobody placed.
    """
    payload = _run_task_payload(playbook, pointer, project_id=project_id)
    return manager.create_task(
        lifecycle=Lifecycle.READY,
        actor=created_by,
        **payload,
    )


def run_playbook(
    *,
    manager: TaskManager,
    project: Project,
    project_config: Dict[str, object],
    name: str,
    task_id: Optional[str] = None,
    group: Optional[str] = None,
    created_by: Optional[str] = None,
    authorized_by: Optional[str] = None,
    note: Optional[str] = None,
    surface: Optional[str] = None,
    home: Optional[Path] = None,
    api_base: Optional[str] = None,
) -> PlaybookRunResult:
    """Run one playbook: create its run task if it has one, then dispatch.

    ``created_by`` is the human whose creation entry authorises a **project-target**
    run, and it is validated as a configured human before anything is written. It is
    unused by a task-target run, which creates no task.

    ``authorized_by`` is task-188's browser path and is passed to ``dispatch_task``
    untouched: supplied, the dispatcher writes that human's authorising entry onto the
    task it is about to dispatch. A project-target run does **not** use it -- the
    creation entry it just wrote is the authorisation, and a second entry saying the
    same thing one second later is noise on the record. A caller with nobody to name
    passes neither and falls back to the stored-entry rule, which is what the CLI does.

    Raises :class:`PlaybookRunError` for anything decided here, ``DispatchError`` for a
    refusal reached before a run task existed, and :class:`PlaybookDispatchRefused` when
    a run task was created and the dispatch was then refused.
    """
    playbook = _load(project, name)
    _assert_shape_matches(playbook, task_id)
    pointer = pointer_for(playbook, project_root=project.root)

    # Gates 1-4, asked before anything is written. This is a pre-flight and not the
    # gate: `dispatch_task` re-checks all of it, along with every refusal that needs a
    # task to judge. It is here so that "dispatch is switched off on this machine" --
    # the refusal that says nothing could possibly have run -- does not first leave a
    # run task in the backlog. The refusals that survive this (a busy machine, a dirty
    # tree) are reported with the run task they left behind; see PlaybookDispatchRefused.
    assert_dispatch_permitted(project.id, home, group=group)

    created = False
    if playbook.contract.target is PlaybookTarget.PROJECT:
        author = (created_by or "").strip() or None
        if author is None:
            raise PlaybookRunError(
                "no_authorizing_human",
                f"A run of {playbook.name!r} creates a run task, and a task created by "
                "nobody carries no authorising entry and could never be dispatched. "
                "Name the human asking for this run.",
            )
        _assert_human(project_config, author)
        task = create_run_task(manager, playbook, pointer, project_id=project.id, created_by=author)
        task_id = task.id
        created = True
        # The creation entry above is the authorisation. See the docstring.
        authorized_by = None
    assert task_id is not None  # both branches have set it; mypy cannot see that

    request = DispatchRequest(
        task_id=task_id,
        group=group,
        authorized_by=authorized_by,
        authorization_note=note,
        surface=surface,
        playbook=pointer,
    )
    try:
        handle = dispatch_task(
            manager=manager,
            project=project,
            project_config=project_config,
            request=request,
            home=home,
            api_base=api_base,
        )
    except DispatchError as exc:
        if created:
            raise PlaybookDispatchRefused(exc, task_id) from exc
        raise

    return PlaybookRunResult(
        playbook=playbook,
        pointer=pointer,
        task_id=task_id,
        created_run_task=created,
        handle=handle,
    )


def _assert_human(project_config: Dict[str, object], actor_id: str) -> None:
    """Refuse a run task created by an identity this project does not call a human.

    The same check ``dispatch_task`` applies to ``authorized_by``, applied here because
    this path reaches it a moment too late to be useful: without it an agent's id would
    create the run task, the dispatch would then be refused as ``not_human_clocked``,
    and the task would already be on disk.
    """
    assert_authorizer_is_human(project_config, actor_id)


__all__ = [
    "PlaybookDispatchRefused",
    "PlaybookRunError",
    "PlaybookRunResult",
    "create_run_task",
    "render_title",
    "run_playbook",
]
