"""Waking the session that already has the context, instead of booting one that has not.

Nearly every task in this repository's run corpus runs twice: a long working run that
ends at the review handoff, and a second dispatched session -- mean about eleven minutes
-- whose whole job is to rebase, merge ``--no-ff``, close the task and put the result in
front of the human. Almost none of those eleven minutes is those commands. It is a cold
agent booting and rediscovering which branch and which worktree it owns, all of which
the *first* session had in memory when it handed off.

So dispatching a task whose previous session's conversation still exists **resumes that
conversation** rather than starting a new one. The session is the same agent, with its
own worktree, its own branch and its own account of what it verified.

**Waking is an optimisation and never a precondition.** Every uncertainty here resolves
to "no wake", and the caller starts a cold session exactly as it always did. A missing
conversation, an unreadable session ledger, a runner whose argv this cannot rewrite --
none of them may turn into a failed dispatch, because the cold path is a correct answer
to all of them and a refusal is not.

The one thing that is *not* softened is which conversation gets resumed: only the newest
session run for the task is ever a candidate. Reaching further back would hand the human
an agent whose picture of the branch is two runs out of date, which is worse than the
cold start it was trying to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Mapping, Optional, Sequence

if TYPE_CHECKING:  # pragma: no cover - `ledger` imports `runner`, which imports this
    from agentjobs.dispatch.ledger import RunRecord

RESUME_FLAG = "--resume"
"""The long form, deliberately. ``-r`` is the same flag and reads like a typo in argv
that is recorded verbatim into a task record and read back by people."""

WAKE_STUB = (
    "You are the agent `{agent}`, and this is the **same session you were already "
    "running on task `{task_id}`**, resumed -- not a new one. Everything you "
    "established earlier still stands and still applies: the worktree you took, the "
    "branch you are on, what you built, and what you verified. Do not start the task "
    "over, do not take a second worktree, and do not re-derive what you already know.\n\n"
    "A human has moved the ball back to you. What they said:\n\n"
    "{ball_prompt}\n\n"
    "The task record at {api_base} has the full entry if you need more of it. "
    "Dispatch run id: {run_id} (resumed from {previous_run_id}).\n\n"
    "**If you cannot account for the state you left behind** -- your worktree is gone, "
    "your branch is not where you left it, or your own account of this task no longer "
    "matches what is on disk -- do not guess and do not improvise a recovery. Say so on "
    "the task and hand the ball back."
)
"""What a woken session is told. Short, because the expensive context is already loaded.

It carries the ball prompt verbatim rather than pointing at it, which is the one place
this departs from ``PROMPT_STUB``'s pointer-not-a-copy rule. The reason is that the ball
prompt *is* the payload of the wake -- it is the sentence the human just wrote, and the
whole shape of this feature is "the approval arrives as the session's next prompt". A
pointer would make the agent's first act an API call to fetch the thing the wake was
delivering.

The last paragraph is the escalation clause, and it is doing real work. A resumed
conversation is confident by construction: it remembers a worktree and a branch, and it
will act on that memory. If the world moved underneath it -- somebody removed the
worktree, another agent merged the branch -- the failure mode of a confident agent is to
improvise, and this says not to.
"""

BALL_PROMPT_LIMIT = 4000
"""How much of the ball prompt rides in the wake, before it is truncated to a pointer.

Ball prompts in this repository run to thousands of characters and are occasionally much
longer. A wake prompt is passed on stdin so there is no argv length limit to respect;
this bound exists so one runaway prompt cannot dominate the resumed session's first turn.
"""


@dataclass(frozen=True)
class WakeTarget:
    """A conversation that can be resumed, and the run it belonged to."""

    #: The run whose session this was. Recorded on the new run so the chain is auditable.
    previous_run_id: str
    #: The short id the session manager listed it under.
    session_id: str
    #: The full UUID. ``--resume`` takes this and not the short id.
    session_uuid: str


class WakeError(Exception):
    """A wake was attempted and could not be built. Callers fall back to a cold start."""


def session_uuids(rows: Sequence[Mapping[str, object]]) -> Dict[str, str]:
    """Map short session id to full UUID, from the runner's own session ledger.

    Read at wake time rather than captured at spawn time, and that is deliberate. A
    stopped session keeps its row -- ``claude stop`` removes the ``pid`` and the
    ``status`` and leaves ``id`` and ``sessionId`` in place -- so the ledger is the live
    answer to "does this conversation still exist", which a field written at spawn time
    would only be a stale guess at. A conversation that has since been deleted simply
    has no row, and no wake is offered for it.
    """
    mapping: Dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            # `ledger()` already drops these, so this is belt to its braces. It is here
            # because the rows are parsed JSON from a subprocess and this function is
            # reachable from any caller, and a TypeError out of a *lookup* would turn
            # "no wake available" into a failed dispatch.
            continue
        short = row.get("id")
        full = row.get("sessionId")
        if isinstance(short, str) and isinstance(full, str) and short and full:
            mapping[short] = full
    return mapping


def newest_session_run(home: Path, task_id: str) -> Optional["RunRecord"]:
    """The most recent session run recorded for ``task_id``, live or not.

    ``list_runs`` is already newest-first. A run with no start time sorts to the bottom
    there, which is the right place for it: a run nothing timestamped cannot be shown to
    be the newest, and guessing that it is would be guessing about which conversation to
    resume.

    The import is local because ``ledger`` imports ``runner`` and ``runner`` imports this
    module. Keeping the cycle out of module scope is cheaper than either of the two
    alternatives -- moving ``list_runs``, or having this module rescan the runs directory
    itself and own a second reading of the same files.
    """
    from agentjobs.dispatch.ledger import list_runs

    for record in list_runs(home):
        if record.is_session and record.task_id == task_id:
            return record
    return None


def find_wake_target(
    home: Path,
    task_id: str,
    *,
    rows: Sequence[Mapping[str, object]],
) -> Optional[WakeTarget]:
    """The conversation a dispatch of ``task_id`` should resume, or ``None``.

    ``None`` is the ordinary answer for a task being dispatched for the first time, and
    it is also the answer to every doubt. Four things disqualify a candidate:

    - **There is no session run for this task.** Nothing to resume.
    - **The newest one is still live.** Something is already working this task, so the
      question is not "resume or start" but "should this dispatch happen at all", and
      that is the run lock's decision rather than this function's.
    - **It was reaped.** ``claude rm`` deletes the conversation; the run's own meta
      records that it happened, and no amount of ledger reading brings it back.
    - **The session manager no longer lists it.** The conversation is gone, whoever
      removed it.

    Only the newest session run is ever considered. If it is disqualified, this returns
    ``None`` rather than falling back to the one before it -- see the module docstring.
    """
    record = newest_session_run(home, task_id)
    if record is None or record.is_live or not record.session_id:
        return None
    if _was_reaped(record):
        return None
    uuid = session_uuids(rows).get(record.session_id)
    if not uuid:
        return None
    return WakeTarget(
        previous_run_id=record.run_id,
        session_id=record.session_id,
        session_uuid=uuid,
    )


def _was_reaped(record: "RunRecord") -> bool:
    """Whether the reaper already destroyed this run's conversation."""
    meta_path = record.path / "meta.yaml"
    if not meta_path.is_file():
        return False
    try:
        return "reaped: true" in meta_path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - unreadable meta is not evidence of a reap
        return False


def wake_argv(argv: Sequence[str], prompt: str, session_uuid: str) -> List[str]:
    """Rewrite a cold-start argv into a resume, with the prompt taken out of it.

    The element carrying the prompt becomes ``--resume <uuid>``; everything else -- the
    executable, ``--bg``, ``--remote-control``, the model, and every posture flag -- is
    left exactly where ``build_argv`` put it. So a wake and a cold start differ in one
    argument and are otherwise the same run, which is what keeps the posture, the
    permission grant and the MCP configuration from quietly diverging between them.

    **The prompt is removed from argv and must be delivered on stdin.** This is not a
    style choice and reversing it produces a silent failure, so it is stated here as
    well as at the call site. ``--remote-control`` and ``--resume`` do not compose over
    a positional prompt: the session comes up with its conversation correctly restored
    and the prompt argument **silently dropped**, sitting at ``idle``/``blocked`` with an
    empty prompt box. ``classify_session`` reads ``idle`` as ``FINISHED``, so dispatch
    would then settle a session that never received its instruction as one that finished
    without handing off. Verified on Claude Code 2.1.238, reproduced twice; the three-row
    table is in task-234's log.
    """
    rewritten: List[str] = []
    replaced = False
    for element in argv:
        if not replaced and prompt and prompt in element:
            rewritten.extend([RESUME_FLAG, session_uuid])
            replaced = True
            continue
        rewritten.append(element)
    if not replaced:
        raise WakeError(
            "This runner's argv does not carry the prompt in any single element, so "
            "there is nothing to replace with a resume. Start a cold session instead."
        )
    return rewritten


def build_wake_prompt(
    *,
    agent: str,
    task_id: str,
    ball_prompt: str,
    api_base: str,
    run_id: str,
    previous_run_id: str,
) -> str:
    """Render ``WAKE_STUB``. A blank ball prompt still produces a usable instruction."""
    stated = (ball_prompt or "").strip()
    if not stated:
        stated = (
            "(The task record carries no ball prompt. Read the newest handoff entry "
            "before doing anything.)"
        )
    elif len(stated) > BALL_PROMPT_LIMIT:
        stated = stated[:BALL_PROMPT_LIMIT].rstrip() + (
            "\n\n(truncated -- the whole entry is on the task record)"
        )
    return WAKE_STUB.format(
        agent=agent,
        task_id=task_id,
        ball_prompt=stated,
        api_base=api_base,
        run_id=run_id,
        previous_run_id=previous_run_id,
    )
