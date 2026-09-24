"""Retrying a stopped finish on the approval already given (task-575).

**The failure.** A scripted finish stops -- a red gate, a conflicting rebase -- and hands
the ball to an agent, who fixes it. Until this module there were two ways forward and
both were wrong. The agent could run ``agentjobs finish`` itself, which the auto-mode
classifier refuses as a merge without review, and it is right to: from inside the
agent's shell, nothing shows that a person approved. Or the agent could hand back to
review and the owner could approve a second time -- task-563 was approved three times
for one change, and nothing he had reviewed had moved.

**The remedy is a request, not a merge.** The repairing agent asks AgentJobs to retry.
AgentJobs -- this module, running in the server -- judges whether the repair touched
anything the approver judged. When it did not, it starts the finish with the same
detached ``spawn_finish`` the Approve button uses, on the approval that still stands.
When it did, the task goes to human/review with each offending path stated, as it did
before. The agent's shell never runs the merge.

**What qualifies** is decided per path, between the head the approver saw (A) and the
branch head now (N), with O and M the merge-bases of each with the base branch:

- ``rebase`` -- N's content is exactly what a clean three-way merge of O, A and M gives:
  main moved underneath and the repair added nothing there.
- ``test`` / ``generated`` -- evidence and derived output, not reviewed behaviour. The
  finish re-runs the full gate over them either way.
- ``conflict_resolution`` -- both sides changed the path, and N keeps every line either
  side added while adding no line of its own.

Anything else is a change nobody reviewed, and it goes to a person. The decision entry on
task-575 names the alternatives rejected.

**Bounded and recorded.** At most :data:`MAX_RETRIES_PER_APPROVAL` retries ride on one
approval, counted from the entries this module writes, and each entry names the approval,
both heads, both bases and every path's classification.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from agentjobs.actors import FINISHER
from agentjobs.models_v2 import Ball, BallReason, LogEntryType, Task
from agentjobs.projects import Project
from agentjobs.store_factory import TaskManagerLike

__all__ = [
    "MAX_RETRIES_PER_APPROVAL",
    "RETRY_KEY",
    "PathVerdict",
    "RepairVerdict",
    "RetryOutcome",
    "classify_path",
    "judge_repair",
    "request_finish_retry",
]

RETRY_KEY = "finish_retry"
"""The key a retry's facts ride under on its log entry's ``data``."""

REFUSAL_KEY = "finish_retry_refused"
"""The key on the handoff written when a requested retry goes to a person instead."""

MAX_RETRIES_PER_APPROVAL = 2
"""How many automatic retries one approval can carry.

Two, because the observed cases need at most two: task-563 stopped once at the gate and
once at the rebase. A third stop on the same approval says the repair is not converging,
and that is worth a person's look whatever the diff says."""

RETRYING = "retrying"
HANDED_BACK = "handed_back"
DECLINED = "declined"

GIT_TIMEOUT_SECONDS = 30

TEST_PREFIXES = ("tests/", "frontend/e2e/", "frontend/e2e-bench/", "frontend/src/test/")
TEST_MARKERS = (".test.", ".spec.")
TEST_NAMES = ("conftest.py",)

GENERATED_PATHS = ("openapi.json", "frontend/openapi.json", "frontend/src/api/apiDigest.ts")
GENERATED_PREFIXES = ("frontend/src/api/generated/", "frontend/public/icons/")


# ----- judging a repair -----------------------------------------------------------


@dataclass(frozen=True)
class PathVerdict:
    """One path the repair changed, and what it was judged to be."""

    path: str
    kind: str
    qualifies: bool
    why: str

    def as_data(self) -> Dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "qualifies": self.qualifies, "why": self.why}


@dataclass(frozen=True)
class RepairVerdict:
    """Whether the whole repair qualifies, with every path's verdict."""

    approved_head: str
    new_head: str
    old_base: str
    new_base: str
    paths: Tuple[PathVerdict, ...]

    @property
    def qualifies(self) -> bool:
        return all(item.qualifies for item in self.paths)

    @property
    def offending(self) -> List[PathVerdict]:
        return [item for item in self.paths if not item.qualifies]

    def as_data(self) -> Dict[str, Any]:
        return {
            "approved_head": self.approved_head,
            "new_head": self.new_head,
            "old_base": self.old_base,
            "new_base": self.new_base,
            "paths": [item.as_data() for item in self.paths],
        }


def is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        path.startswith(TEST_PREFIXES)
        or any(marker in name for marker in TEST_MARKERS)
        or (name.startswith("test_") and name.endswith(".py"))
        or name in TEST_NAMES
    )


def is_generated_path(path: str) -> bool:
    return path in GENERATED_PATHS or path.startswith(GENERATED_PREFIXES)


def _git(root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )


def _git_text(root: Path, args: Sequence[str]) -> str:
    completed = _git(root, args)
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _content(root: Path, commit: str, path: str) -> Optional[bytes]:
    """The bytes of ``path`` at ``commit``, or ``None`` when it does not exist there."""
    completed = _git(root, ["show", f"{commit}:{path}"])
    return completed.stdout if completed.returncode == 0 else None


def _merge3(
    base: Optional[bytes], ours: Optional[bytes], theirs: Optional[bytes]
) -> Optional[bytes]:
    """A clean three-way merge of one file, or ``None`` when it conflicts or cannot run.

    Absent files: when either side equals the base, the other side wins, which also
    covers additions and deletions made by one side only. Both sides touching a file
    that one of them deleted is a conflict, and so ``None``.
    """
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if base is None or ours is None or theirs is None:
        return None
    with tempfile.TemporaryDirectory() as scratch:
        folder = Path(scratch)
        files = []
        for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
            target = folder / name
            target.write_bytes(data)
            files.append(str(target))
        try:
            completed = subprocess.run(
                ["git", "merge-file", "-p", "--quiet", *files],
                capture_output=True,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return None
    return completed.stdout if completed.returncode == 0 else None


def _lines(data: Optional[bytes]) -> List[str]:
    if data is None:
        return []
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n")


def keeps_both_sides(
    base: Optional[bytes], ours: Optional[bytes], theirs: Optional[bytes], result: Optional[bytes]
) -> Tuple[bool, str]:
    """Whether ``result`` resolves a conflict between ``ours`` and ``theirs`` by keeping both.

    Line sets, deliberately: a resolution interleaves lines, so order is exactly what it
    is allowed to change. What it may not do is drop a line either side added, or write
    a line neither side had -- that line is new code, and nobody reviewed it.
    """
    if result is None:
        return False, "the resolution deletes a file both sides changed"
    before = set(_lines(base))
    mine = set(_lines(ours))
    other = set(_lines(theirs))
    resolved = set(_lines(result))
    dropped = sorted(line for line in (mine - before) | (other - before) if line not in resolved)
    invented = sorted(line for line in resolved if line not in mine and line not in other)
    if dropped:
        return False, f"drops {len(dropped)} line(s) a side added, e.g. {dropped[0].strip()!r}"
    if invented:
        return False, f"adds {len(invented)} line(s) neither side had, e.g. {invented[0].strip()!r}"
    return True, "keeps every line both sides added and writes none of its own"


def classify_path(
    path: str,
    *,
    base: Optional[bytes],
    approved: Optional[bytes],
    main: Optional[bytes],
    new: Optional[bytes],
) -> PathVerdict:
    """Judge one changed path. See the module docstring for the rule."""
    merged = _merge3(base, approved, main)
    if merged is not None and merged == new:
        return PathVerdict(path, "rebase", True, "exactly the clean merge of the branch onto main")
    if is_test_path(path):
        return PathVerdict(path, "test", True, "a test file")
    if is_generated_path(path):
        return PathVerdict(path, "generated", True, "generated output; the gate re-derives it")
    branch_touched = approved != base
    main_touched = main != base
    if branch_touched and main_touched:
        kept, why = keeps_both_sides(base, approved, main, new)
        if kept:
            return PathVerdict(path, "conflict_resolution", True, why)
        return PathVerdict(path, "conflict_resolution", False, f"a conflict resolution that {why}")
    if branch_touched:
        return PathVerdict(path, "reviewed_source", False, "changes source the approver reviewed")
    return PathVerdict(path, "new_source", False, "changes source outside the approved branch")


def judge_repair(root: Path, base_branch: str, approved_head: str, new_head: str) -> RepairVerdict:
    """Judge every path that differs between the approved head and the new one."""
    old_base = _git_text(root, ["merge-base", approved_head, base_branch]) or approved_head
    new_base = _git_text(root, ["merge-base", new_head, base_branch]) or new_head
    changed = [
        line
        for line in _git_text(
            root, ["diff", "--name-only", "--no-renames", approved_head, new_head]
        )
        .replace("\r\n", "\n")
        .split("\n")
        if line
    ]
    verdicts = tuple(
        classify_path(
            path,
            base=_content(root, old_base, path),
            approved=_content(root, approved_head, path),
            main=_content(root, new_base, path),
            new=_content(root, new_head, path),
        )
        for path in changed
    )
    return RepairVerdict(
        approved_head=approved_head,
        new_head=new_head,
        old_base=old_base,
        new_base=new_base,
        paths=verdicts,
    )


# ----- the request ----------------------------------------------------------------


@dataclass(frozen=True)
class RetryOutcome:
    """What a retry request did. ``outcome`` is retrying, handed_back or declined."""

    outcome: str
    reason: str
    detail: str
    task: Optional[Task] = None
    data: Dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "detail": self.detail,
            "data": self.data,
        }


def _decline(reason: str, detail: str, task: Optional[Task] = None) -> RetryOutcome:
    return RetryOutcome(outcome=DECLINED, reason=reason, detail=detail, task=task)


def newest_escalation_after(task: Task, entry_id: int) -> Optional[Any]:
    """The newest finisher escalation written after ``entry_id``, if any."""
    for entry in reversed(task.log):
        if entry.id <= entry_id:
            return None
        data = entry.data if isinstance(entry.data, Mapping) else {}
        if entry.actor == FINISHER and data.get("finish_step") and not data.get("withdrawn"):
            return entry
    return None


def retries_on(task: Task, approval_entry: int) -> int:
    """How many retries have already ridden on this approval, counted on the record."""
    count = 0
    for entry in task.log:
        data = entry.data if isinstance(entry.data, Mapping) else {}
        retry = data.get(RETRY_KEY)
        if isinstance(retry, Mapping) and retry.get("approval_entry") == approval_entry:
            count += 1
    return count


def last_retry_entry(task: Task, approval_entry: int) -> int:
    """The id of the newest retry entry riding on this approval, or 0."""
    newest = 0
    for entry in task.log:
        data = entry.data if isinstance(entry.data, Mapping) else {}
        retry = data.get(RETRY_KEY)
        if isinstance(retry, Mapping) and retry.get("approval_entry") == approval_entry:
            newest = max(newest, int(entry.id))
    return newest


def _replayed(task: Task, operation_id: str) -> Optional[Any]:
    for entry in task.log:
        data = entry.data if isinstance(entry.data, Mapping) else {}
        for key in (RETRY_KEY, REFUSAL_KEY):
            payload = data.get(key)
            if isinstance(payload, Mapping) and payload.get("operation_id") == operation_id:
                return entry
    return None


def _render_paths(verdicts: Sequence[PathVerdict]) -> str:
    if not verdicts:
        return "- no path differs: the branch is exactly what was approved"
    return "\n".join(f"- `{item.path}` -- {item.kind}: {item.why}" for item in verdicts)


def _hand_back(
    manager: TaskManagerLike,
    task_id: str,
    *,
    actor: str,
    reason: str,
    detail: str,
    summary: str,
    facts: Dict[str, Any],
) -> RetryOutcome:
    """Send the task to a person, saying why the retry could not ride the approval."""
    said = f"\n\nThe agent's account of its repair:\n\n{summary}" if summary else ""
    prompt = (
        "The agent repaired a stopped finish and asked to retry it on your approval, "
        f"but that approval does not cover the repair: {detail}\n\n"
        "Review the branch as it stands now. Approve to merge it, or request changes."
        f"{said}"
    )
    task = manager.handoff(
        task_id,
        actor=actor,
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt=prompt,
        body=f"A finish retry was requested and needs a review instead ({reason}): {detail}",
        data={REFUSAL_KEY: {"reason": reason, **facts}},
    )
    return RetryOutcome(
        outcome=HANDED_BACK,
        reason=reason,
        detail=detail,
        task=task,
        data={"reason": reason, **facts},
    )


def request_finish_retry(
    *,
    manager: TaskManagerLike,
    project: Project,
    task_id: str,
    actor: str,
    summary: str = "",
    operation_id: str = "",
    home: Optional[Path] = None,
    spawn: Optional[Callable[..., Optional[str]]] = None,
) -> RetryOutcome:
    """Judge a repaired finish and retry it on the standing approval, or say why not.

    ``spawn`` is ``spawn_finish`` unless a test substitutes it. Declines write nothing;
    a hand-back writes the handoff; a retry writes one ``finisher`` progress entry and
    then spawns. Never merges anything itself.
    """
    from agentjobs.dispatch.approval import standing_approval_for
    from agentjobs.dispatch.config import DispatchError, assert_dispatch_permitted
    from agentjobs.dispatch.finish import already_in_flight, spawn_finish
    from agentjobs.projects import default_home

    resolved_home = home or default_home()
    task = manager.get_task(task_id)
    if task is None or not task.is_open:
        return _decline("not_open", f"{task_id} is closed or missing; there is nothing to retry.")
    if operation_id:
        earlier = _replayed(task, operation_id)
        if earlier is not None:
            return RetryOutcome(
                outcome="replayed",
                reason="replayed",
                detail=f"This request was already acted on in entry {earlier.id}.",
                task=task,
            )
    try:
        settings = assert_dispatch_permitted(project.id, resolved_home).settings.finish
    except DispatchError as exc:
        return _decline(getattr(exc, "reason", "dispatch_error"), str(exc), task)
    if not settings.enabled:
        return _decline(
            "not_enabled",
            f"{project.id} has no `finish.enabled: true`, so there is no scripted finish "
            "to retry. Hand the task to human/review.",
            task,
        )
    try:
        config = project.load_config()
    except Exception:  # noqa: BLE001 - an unreadable config is no approval to act on
        config = {}
    receipt = standing_approval_for(resolved_home, task, config, project_id=project.id)
    if receipt is None:
        return _decline(
            "no_standing_approval",
            "No approval stands on this task, so there is nothing to retry on. A retry "
            "rides an approval a person already gave; without one, hand off for review.",
            task,
        )
    # After the newest retry on this approval, not merely after the approval: a retry
    # whose finish has not stopped yet is not a second stop to retry.
    escalation = newest_escalation_after(
        task, max(receipt.entry_id, last_retry_entry(task, receipt.entry_id))
    )
    if escalation is None:
        return _decline(
            "no_escalation",
            f"No scripted finish has stopped since the approval in entry {receipt.entry_id}, "
            "so there is nothing to retry. If the finish is still running, wait for it.",
            task,
        )
    in_flight = already_in_flight(resolved_home, task_id, project_id=project.id)
    if in_flight is not None:
        return _decline(
            "finish_in_flight",
            f"{in_flight.get('finish_id') or 'A finish'} is already running for {task_id}.",
            task,
        )

    base_facts: Dict[str, Any] = {
        "approval_entry": receipt.entry_id,
        "approver": receipt.approver,
        "escalation_entry": escalation.id,
        "requested_by": actor,
        "operation_id": operation_id or None,
    }
    done = retries_on(task, receipt.entry_id)
    if done >= MAX_RETRIES_PER_APPROVAL:
        return _hand_back(
            manager,
            task_id,
            actor=FINISHER,
            reason="retry_limit",
            detail=(
                f"{done} automatic retries have already ridden on the approval in entry "
                f"{receipt.entry_id}, the most one approval carries "
                f"({MAX_RETRIES_PER_APPROVAL}). A finish that keeps stopping needs a person."
            ),
            summary=summary,
            facts={**base_facts, "retries": done, "limit": MAX_RETRIES_PER_APPROVAL},
        )

    branches = [
        branch.name
        for branch in task.branches
        if getattr(branch.status, "value", branch.status) == "active"
    ]
    if len(branches) != 1:
        return _hand_back(
            manager,
            task_id,
            actor=FINISHER,
            reason="branches_unclear",
            detail=(
                f"The task names {len(branches)} active branches; a retry can judge exactly "
                "one against what was approved."
            ),
            summary=summary,
            facts=base_facts,
        )
    branch = branches[0]
    approved_head = receipt.head_of(branch)
    if not approved_head:
        return _hand_back(
            manager,
            task_id,
            actor=FINISHER,
            reason="approved_head_unknown",
            detail=(
                f"The approval in entry {receipt.entry_id} did not record the head of "
                f"`{branch}`, so there is nothing to compare the repair against."
            ),
            summary=summary,
            facts={**base_facts, "branch": branch},
        )
    new_head = _git_text(project.root, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    if not new_head:
        return _hand_back(
            manager,
            task_id,
            actor=FINISHER,
            reason="branch_missing",
            detail=f"`{branch}` does not exist in {project.root}.",
            summary=summary,
            facts={**base_facts, "branch": branch},
        )

    verdict = judge_repair(project.root, settings.base_branch, approved_head, new_head)
    facts = {**base_facts, "branch": branch, **verdict.as_data()}
    if not verdict.qualifies:
        offending = verdict.offending
        return _hand_back(
            manager,
            task_id,
            actor=FINISHER,
            reason="repair_changes_reviewed_work",
            detail=(
                f"{len(offending)} path(s) changed since `{approved_head[:8]}` in a way the "
                f"approval did not see:\n\n{_render_paths(offending)}"
            ),
            summary=summary,
            facts=facts,
        )

    attempt = done + 1
    said = f"\n\nThe agent's account of its repair:\n\n{summary}" if summary else ""
    task = manager.add_log_entry(
        task_id,
        actor=FINISHER,
        type=LogEntryType.PROGRESS,
        body=(
            f"Retrying the scripted finish on the approval in entry {receipt.entry_id} "
            f"({receipt.approver}), attempt {attempt} of {MAX_RETRIES_PER_APPROVAL}, as "
            f"{actor} asked after the stop in entry {escalation.id}. `{branch}` moved from "
            f"`{approved_head[:8]}` to `{new_head[:8]}`, and nothing the approver reviewed "
            f"changed:\n\n{_render_paths(verdict.paths)}{said}"
        ),
        data={RETRY_KEY: {**facts, "attempt": attempt, "limit": MAX_RETRIES_PER_APPROVAL}},
    )
    started = (spawn or spawn_finish)(
        project=project, task_id=task_id, approver=receipt.approver, home=resolved_home
    )
    if started is None:
        return _hand_back(
            manager,
            task_id,
            actor=FINISHER,
            reason="spawn_failed",
            detail="The repair qualified, but the finish process could not be started.",
            summary=summary,
            facts=facts,
        )
    return RetryOutcome(
        outcome=RETRYING,
        reason="retrying",
        detail=(
            f"AgentJobs is retrying the finish on the approval in entry {receipt.entry_id} "
            f"(attempt {attempt} of {MAX_RETRIES_PER_APPROVAL}). It will stand this session "
            "down, rebase, gate and merge. End your turn; do not merge or run the finish."
        ),
        task=manager.get_task(task_id) or task,
        data={**facts, "attempt": attempt, "limit": MAX_RETRIES_PER_APPROVAL, "log": started},
    )
