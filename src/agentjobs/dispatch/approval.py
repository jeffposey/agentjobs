"""What an approval is, once it has been clicked, and when it stops being one (task-312).

Until this module an approval was a sentence. The approve route wrote
``APPROVAL_CLEARANCE`` into the ball prompt and every later reader had to infer from prose
whether that sentence still stood. Two failures on task-312 are what the inference cost:

- **The finish could not tell an approved task from any other.** It took the task's run
  lock, lost it to the live session that had asked for the review, and declined. Nothing
  distinguished "a human cleared this to merge and a session is merely still attached"
  from "somebody is working this task", so the only safe answer was no.
- **A cancelled run overwrote the approval with a request to decide.** ``_conclude`` saw
  ``agent``/``work`` and wrote *nobody was told what this task needs* over a prompt that
  said exactly what it needed.

So an approval is now a **receipt** on the handoff entry itself -- who approved, their
note verbatim, and the branch heads they were looking at -- and ``standing_approval`` is
the one answer to whether it still authorises anything. It stands while it is the newest
human handoff on an open task and no Stop has been requested since. A later human
handoff replaces it (Request Changes, a hold, another approval); a Stop withdraws it.
Machine narration does not: a finisher's progress note or a dispatcher's result is not a
human changing their mind.

**What it deliberately does not decide** is what a note *means*. The note is carried
verbatim and preserved in the inbox; whether a note should suppress a finish or change a
merge message is task-343's question, and nothing here reads it.

The inbox half keeps the same facts in the execution journal, so an approval accepted by
a server that dies a second later is still owed to somebody after the restart, and so a
superseded one is marked rather than erased.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from agentjobs.dispatch.guards import actor_kind
from agentjobs.models_v2 import Ball, BallReason, LogEntry, LogEntryType, Task

APPROVAL_KEY = "approval"
"""The key an approval's receipt rides under on its handoff entry's ``data``."""

LEGACY_APPROVAL_PREFIX = "Approved by "
"""How an approval written before task-312 is recognised: the approve route's own body.

Read only when an entry carries no receipt, so a pre-upgrade approval still stands as the
approval it was. It has no reviewed heads, and says so (``legacy``)."""

GIT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class ReviewedBranch:
    """A branch the approver was looking at, and the commit it pointed to at the click."""

    name: str
    head: Optional[str]

    def as_data(self) -> Dict[str, Optional[str]]:
        return {"name": self.name, "head": self.head}


@dataclass(frozen=True)
class ApprovalReceipt:
    """One accepted approval: the source event and what it authorised."""

    project_id: str
    task_id: str
    entry_id: int
    ts: datetime
    approver: str
    note: str
    reviewed: Tuple[ReviewedBranch, ...]
    legacy: bool = False

    @property
    def source_event_id(self) -> str:
        """The inbox identity of the handoff, identical to the feed import's."""
        return f"{self.task_id}#{self.entry_id}@{feed_timestamp(self.ts)}"

    def head_of(self, branch: str) -> Optional[str]:
        for reviewed in self.reviewed:
            if reviewed.name == branch:
                return reviewed.head
        return None


def feed_timestamp(moment: datetime) -> str:
    """A log entry's timestamp exactly as the task store's feed spells it.

    The inbox deduplicates on ``task#entry@ts``, so a synchronous acceptance and the
    later feed import only land on one row if both spell the moment the same way. The
    store writes UTC with a ``Z``; so does this.
    """
    value = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ----- writing one --------------------------------------------------------------


def reviewed_branches(root: Path, task: Task) -> List[ReviewedBranch]:
    """The task's active branches and their heads in ``root``, at this instant.

    A head that cannot be read is recorded as ``None`` rather than guessed: the receipt
    says what was known when the human clicked, and "unknown" is a fact a later reader
    can act on where an invented SHA is not.
    """
    reviewed: List[ReviewedBranch] = []
    for branch in task.branches:
        if getattr(branch.status, "value", branch.status) != "active":
            continue
        reviewed.append(ReviewedBranch(branch.name, _branch_head(root, branch.name)))
    return reviewed


def _branch_head(root: Path, name: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    head = (completed.stdout or "").strip()
    return head if completed.returncode == 0 and head else None


def approval_data(
    *, approver: str, note: str, reviewed: Sequence[ReviewedBranch]
) -> Dict[str, Any]:
    """The ``data`` an approve handoff carries. The note is verbatim, never summarised."""
    return {
        APPROVAL_KEY: {
            "approver": approver,
            "note": note,
            "reviewed": [branch.as_data() for branch in reviewed],
        }
    }


# ----- reading one --------------------------------------------------------------


def approval_in(
    entry: Optional[LogEntry], *, project_id: str, task_id: str
) -> Optional[ApprovalReceipt]:
    """The approval this entry records, or ``None`` when it records none."""
    if entry is None or entry.type is not LogEntryType.HANDOFF:
        return None
    data = entry.data or {}
    if data.get("ball") != Ball.AGENT.value or data.get("ball_reason") != BallReason.WORK.value:
        return None
    raw = data.get(APPROVAL_KEY)
    if isinstance(raw, Mapping):
        reviewed = tuple(
            ReviewedBranch(str(item.get("name") or ""), item.get("head") or None)
            for item in raw.get("reviewed") or []
            if isinstance(item, Mapping) and item.get("name")
        )
        return ApprovalReceipt(
            project_id=project_id,
            task_id=task_id,
            entry_id=int(entry.id),
            ts=_utc(entry.ts),
            approver=str(raw.get("approver") or entry.actor),
            note=str(raw.get("note") or ""),
            reviewed=reviewed,
        )
    if (entry.body or "").startswith(LEGACY_APPROVAL_PREFIX):
        return ApprovalReceipt(
            project_id=project_id,
            task_id=task_id,
            entry_id=int(entry.id),
            ts=_utc(entry.ts),
            approver=entry.actor,
            note="",
            reviewed=(),
            legacy=True,
        )
    return None


def newest_human_handoff(task: Task, project_config: Mapping[str, object]) -> Optional[LogEntry]:
    """The most recent handoff a person wrote on this task."""
    for entry in reversed(task.log):
        if entry.type is not LogEntryType.HANDOFF:
            continue
        kind = actor_kind(dict(project_config), entry.actor)
        if kind is not None and kind.is_human:
            return entry
    return None


def ball_prompt_author(task: Task) -> str:
    """The actor of the newest entry that can have written the current ``ball_prompt``.

    Empty when the log names no such entry, which every caller must read as *"who wrote
    this is not recorded"* and never as *"nobody"* -- the field is writable and the
    absence of an entry explaining it is exactly the case worth being careful about.

    Three kinds of entry move the prompt and no others. ``handoff`` sets it and is the
    ordinary case: an Approve or a Request Changes click writes one, and the feedback
    rides into the field and the entry body together. Every ``transition`` moves it too
    -- ``claim`` overwrites it with the work prompt, ``promote`` with the drafting one,
    ``release`` and ``close`` clear it -- so a transition after a handoff means the
    handoff's text is gone and attributing the field to that person would be wrong. And
    ``update_content`` can set the field directly, recording a ``note`` whose ``data``
    lists ``ball_prompt`` among the fields it changed.

    Newest first, because the last writer is the one whose text is in the field.
    """
    for entry in reversed(task.log):
        if entry.type in (LogEntryType.HANDOFF, LogEntryType.TRANSITION):
            return entry.actor
        data = entry.data if isinstance(entry.data, Mapping) else {}
        fields = data.get("fields")
        if isinstance(fields, (list, tuple)) and "ball_prompt" in fields:
            return entry.actor
    return ""


def author_is_human(project_config: Mapping[str, object], actor_id: str) -> bool:
    """Whether the project's vocabulary says this actor is a person.

    **False for every doubt**: a blank id, an actor the vocabulary does not know, a
    project that has configured no actors at all. A reader of this asks it in order to
    decide whether to tell an agent that a human said something, and the honest answer
    to "we cannot tell" is "do not claim it".
    """
    if not actor_id:
        return False
    kind = actor_kind(dict(project_config), actor_id)
    return kind is not None and kind.is_human


def human_handoffs_since(
    task: Task, project_config: Mapping[str, object], *, after_entry: Optional[int]
) -> List[LogEntry]:
    """Every handoff a person wrote after ``after_entry``, oldest first (durable-1).

    What a woken session is owed. Two Request Changes clicks while a session was busy are
    two messages, and delivering only the newest ball prompt silently dropped the first --
    the newest-only loss the design forbids.
    """
    owed: List[LogEntry] = []
    for entry in task.log:
        if after_entry is not None and entry.id <= after_entry:
            continue
        if entry.type is not LogEntryType.HANDOFF:
            continue
        kind = actor_kind(dict(project_config), entry.actor)
        if kind is not None and kind.is_human:
            owed.append(entry)
    return owed


def withdrawing_stop(
    receipt: ApprovalReceipt, stops: Sequence[Mapping[str, Any]]
) -> Optional[Mapping[str, Any]]:
    """The first Stop requested after this approval, which withdraws it."""
    for stop in stops:
        raw = stop.get("requested_at")
        if not isinstance(raw, str):
            continue
        try:
            moment = _utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            continue
        if moment > receipt.ts:
            return stop
    return None


def standing_approval(
    task: Optional[Task],
    project_config: Mapping[str, object],
    *,
    project_id: str,
    stops: Sequence[Mapping[str, Any]] = (),
) -> Optional[ApprovalReceipt]:
    """The approval that still authorises a merge of this task, or ``None``.

    Pure: ``stops`` is the journal's list of Stops for the task (``ExecutionStore.
    stop_requests``), passed in so this can be answered identically by the route, the
    finish, the poller and a test.
    """
    if task is None or not task.is_open:
        return None
    receipt = approval_in(
        newest_human_handoff(task, project_config), project_id=project_id, task_id=task.id
    )
    if receipt is None:
        return None
    if withdrawing_stop(receipt, stops) is not None:
        return None
    return receipt


# ----- the journal half ---------------------------------------------------------


def _source(project_id: str) -> str:
    from agentjobs.execution.coordinator import task_feed_source

    return task_feed_source(project_id)


def source_event(project_id: str, task_id: str, entry: LogEntry) -> Any:
    """``entry`` as the feed would hand it to the inbox."""
    from agentjobs.execution.store import SourceEvent

    return SourceEvent(
        position=0,
        project_id=project_id,
        task_id=task_id,
        entry_id=int(entry.id),
        ts=feed_timestamp(entry.ts),
        type=entry.type.value,
        actor=entry.actor,
        data=dict(entry.data or {}),
    )


def stop_requests(home: Path, project_id: str, task_id: str) -> List[Dict[str, Any]]:
    """Every Stop the journal holds for this task. Empty when the journal cannot be read.

    Empty on failure is the direction that keeps an approval standing, and that is
    deliberate: a Stop is also on the task record as a ``cancelled`` result and a human
    decision prompt, so an unreadable journal cannot turn a withdrawn approval into a
    merge without the record contradicting it -- whereas refusing every approval while
    the journal is busy would strand every finish on the machine.
    """
    from agentjobs.dispatch.journal import journal
    from agentjobs.execution.errors import ExecutionStoreError

    try:
        return journal(home).stop_requests(project_id, task_id)
    except ExecutionStoreError:
        return []


def standing_approval_for(
    home: Path,
    task: Optional[Task],
    project_config: Mapping[str, object],
    *,
    project_id: str,
) -> Optional[ApprovalReceipt]:
    """``standing_approval`` with the Stops read from this machine's journal."""
    if task is None:
        return None
    return standing_approval(
        task,
        project_config,
        project_id=project_id,
        stops=stop_requests(home, project_id, task.id),
    )


def project_config_for(home: Path, project_id: str) -> Dict[str, object]:
    """A registered project's config, or an empty one when it cannot be resolved."""
    from agentjobs.projects import ProjectError, ProjectRegistry

    try:
        return dict(ProjectRegistry(home=home).get(project_id).load_config())
    except (ProjectError, OSError, ValueError, TypeError):
        return {}


def approval_standing_on(
    home: Path, project_id: str, task: Optional[Task]
) -> Optional[ApprovalReceipt]:
    """``standing_approval_for``, resolving the project's config from the registry.

    For the conclusion paths, which hold a run record and a manager but no config. Never
    raises: those paths must still write the run's terminal result whatever this says.
    """
    if task is None or not project_id:
        return None
    try:
        return standing_approval_for(
            home, task, project_config_for(home, project_id), project_id=project_id
        )
    except Exception:  # noqa: BLE001 - see the docstring
        return None


def accept_signals(home: Path, project_id: str, task: Task, entries: Sequence[LogEntry]) -> None:
    """Record these entries in the inbox now. Best effort; the feed import is the backstop.

    The route calls this after the handoff has committed, so an approval is in the
    journal before the response leaves -- and a journal that cannot be written costs
    nothing but the synchronous half, because the poller's import reads the same entry
    from the task store on its next tick.
    """
    from agentjobs.dispatch.journal import journal
    from agentjobs.execution.errors import ExecutionStoreError

    try:
        store = journal(home)
        for entry in entries:
            store.accept_signal(_source(project_id), source_event(project_id, task.id, entry))
    except ExecutionStoreError:
        return


def dispose(
    home: Path,
    project_id: str,
    task: Task,
    entry: LogEntry,
    *,
    status: str,
    disposition: Mapping[str, Any],
) -> None:
    """Give one handoff its explicit inbox disposition, importing it first if need be.

    Best effort, like every journal write on a path a person is waiting on: the task log
    already says what happened, and the disposition is the journal's copy of that fact.
    """
    from agentjobs.dispatch.journal import journal
    from agentjobs.execution.errors import ExecutionStoreError

    try:
        store = journal(home)
        event = source_event(project_id, task.id, entry)
        store.accept_signal(_source(project_id), event)
        store.dispose_signal(
            _source(project_id),
            event.source_event_id,
            status=status,
            disposition=disposition,
        )
    except ExecutionStoreError:
        return


def consuming_finish(home: Path, project_id: str, receipt: ApprovalReceipt) -> str:
    """The finish that has already acted on this approval, or ``""`` when none has.

    ``dispose`` marks an approval ``consumed`` naming the finish that spent it, and
    until task-514 nothing ever read that back. It is the one piece of evidence that
    survives the consuming finish itself: a second finish spawned on the same approval
    can meet a lock only while the first is alive, whereas the disposition says the
    approval was spent whether or not anything is still running.

    Never raises. An unreadable journal answers ``""`` -- the direction that lets a
    finish proceed -- for the reason ``stop_requests`` gives: the refusals that protect
    a merge are on the task record too, and refusing every finish while the journal is
    busy would strand the feature.
    """
    from agentjobs.dispatch.journal import journal
    from agentjobs.execution.errors import ExecutionStoreError

    try:
        store = journal(home)
        item = store.signal(_source(project_id), receipt.source_event_id)
        if item is None or item.status != "consumed":
            return ""
        disposition = store.signal_disposition(_source(project_id), receipt.source_event_id)
    except ExecutionStoreError:
        return ""
    by = str((disposition or {}).get("by") or "")
    return by[len("finish:") :] if by.startswith("finish:") else by


def supersede_earlier_approvals(
    home: Path,
    project_id: str,
    task: Task,
    *,
    by: LogEntry,
) -> None:
    """Mark every earlier approval on this task superseded by the human handoff ``by``.

    A person who approved and then requested changes has made a newer decision, and the
    approval must stop authorising a merge **without disappearing** -- it stays in the
    inbox, ``superseded``, pointing at the entry that replaced it.
    """
    for entry in task.log:
        if entry.id >= by.id:
            break
        if approval_in(entry, project_id=project_id, task_id=task.id) is None:
            continue
        dispose(
            home,
            project_id,
            task,
            entry,
            status="superseded",
            disposition={
                "by": source_event(project_id, task.id, by).source_event_id,
                "reason": "a later human handoff replaced this approval",
            },
        )


def withdraw_approval_on_stop(
    home: Path,
    project_id: str,
    task: Optional[Task],
    project_config: Mapping[str, object],
    *,
    run_id: str,
    requester: str,
    source: str,
) -> Optional[ApprovalReceipt]:
    """Mark the approval a Stop just withdrew, and return it so the prompt can say so.

    Called with the Stop already recorded, so ``standing_approval_for`` would now say no;
    this therefore reads the approval *as it stood* -- the newest human handoff, ignoring
    Stops -- because what the prompt has to tell a person is which decision their Stop
    replaced.
    """
    if task is None or not task.is_open:
        return None
    receipt = standing_approval(task, project_config, project_id=project_id)
    if receipt is None:
        return None
    entry = next((item for item in task.log if item.id == receipt.entry_id), None)
    if entry is not None:
        dispose(
            home,
            project_id,
            task,
            entry,
            status="superseded",
            disposition={
                "by": f"stop:{run_id}",
                "requester": requester,
                "source": source,
                "reason": "a Stop was requested after this approval",
            },
        )
    return receipt


__all__ = [
    "APPROVAL_KEY",
    "ApprovalReceipt",
    "LEGACY_APPROVAL_PREFIX",
    "ReviewedBranch",
    "accept_signals",
    "approval_data",
    "approval_standing_on",
    "project_config_for",
    "approval_in",
    "author_is_human",
    "ball_prompt_author",
    "dispose",
    "feed_timestamp",
    "human_handoffs_since",
    "newest_human_handoff",
    "reviewed_branches",
    "source_event",
    "standing_approval",
    "standing_approval_for",
    "stop_requests",
    "supersede_earlier_approvals",
    "withdraw_approval_on_stop",
    "withdrawing_stop",
]
