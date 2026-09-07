"""What local branches exist, which ones `main` already contains, and whose they are.

**Read-only, and deliberately so.** Task-293 closed the gap that produced the mess --
the scripted finish now deletes the branch it merged -- but a finish only ever retires
branches it merged itself. A branch a person merged by hand, a branch whose finish
escalated after the merge, or a branch from before that fix is still left behind, and
nothing tells anybody it is there.

This is the "tell somebody" half, and it stops there. **Nothing in this module deletes
anything**, and that is a decision rather than an omission: several agents work this
clone and none of them can see the others, so a branch that looks abandoned from here
is indistinguishable from one somebody is mid-task on. The information needed to tell
those apart -- is a session alive, does its author intend to come back -- is not in git.
A report costs a person one glance and one `git branch -d`; an automatic sweep that is
wrong once costs somebody a day's work.

Branch **age** is reported alongside, because the other half of task-293 is that a
branch's lifetime, not its size, is what turns into rebase conflicts. Age is measured
from the author date of the oldest commit the base does not contain, which is the only
figure that survives a rebase: `git rebase` rewrites committer dates and keeps author
dates, so a branch that has been rebased four times still reports how long it has really
been open.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from agentjobs.models_v2 import Task
from agentjobs.store_factory import TaskManagerLike


def _git(root: Path, args: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout if result.returncode == 0 else ""


def worktree_branches(root: Path) -> Dict[str, Path]:
    """Every branch checked out in a worktree of this repository, keyed by branch name.

    Read from ``git worktree list --porcelain`` rather than guessed from a naming
    convention, for the reason ``dispatch.finish`` reads it the same way: a worktree
    somewhere unexpected is exactly the case where guessing is wrong and expensive.
    """
    found: Dict[str, Path] = {}
    current: Optional[Path] = None
    for line in _git(root, ["worktree", "list", "--porcelain"]).splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree ") :].strip())
        elif line.startswith("branch ") and current is not None:
            found[line[len("branch refs/heads/") :].strip()] = current
    return found


@dataclass(frozen=True)
class BranchRow:
    """One local branch, and everything a person needs to decide what to do with it."""

    name: str
    merged: bool
    """Whether the base already contains it. `git branch -d` will refuse unless this."""

    worktree: Optional[Path]
    """The worktree holding it, if any. A branch checked out in one cannot be deleted."""

    task_id: Optional[str]
    """The task listing this branch in ``branches[]``, if a task does."""

    task_state: str
    """That task's state in one phrase, or why no task could be found."""

    age_days: Optional[float]
    """Days since the oldest commit the base does not contain. None once merged."""

    @property
    def deletable(self) -> bool:
        """Merged, and not checked out anywhere. The only rows worth acting on."""
        return self.merged and self.worktree is None


@dataclass(frozen=True)
class BranchReport:
    base: str
    rows: List[BranchRow]

    @property
    def litter(self) -> List[BranchRow]:
        """Merged branches nothing is using. What this report exists to surface."""
        return [row for row in self.rows if row.deletable]

    @property
    def in_flight(self) -> List[BranchRow]:
        """Branches the base does not contain -- somebody's live work, oldest first."""
        unmerged = [row for row in self.rows if not row.merged]
        return sorted(unmerged, key=lambda row: -(row.age_days or 0.0))


def _task_state(task: Task) -> str:
    if task.lifecycle.value == "closed":
        return f"closed {task.outcome.value if task.outcome else 'unresolved'}"
    ball = task.ball.value if task.ball else "?"
    reason = task.ball_reason.value if task.ball_reason else "?"
    return f"{task.lifecycle.value} {ball}/{reason}"


def _branches_by_task(manager: TaskManagerLike) -> Dict[str, Task]:
    """Branch name to the task that claims it, over every task including closed ones.

    Closed tasks included on purpose: a leftover branch belongs to a task that finished,
    so excluding them would leave the report unable to name the owner of precisely the
    rows it is there to show.
    """
    owners: Dict[str, Task] = {}
    for task in manager.list_tasks():
        for branch in task.branches:
            owners.setdefault(branch.name, task)
    return owners


def survey_branches(root: Path, manager: TaskManagerLike, base: str = "main") -> BranchReport:
    """Every local branch, with what the base and the task records say about it."""
    names = [
        line.strip() for line in _git(root, ["branch", "--format=%(refname:short)"]).splitlines()
    ]
    merged = {
        line.strip()
        for line in _git(
            root, ["branch", "--merged", base, "--format=%(refname:short)"]
        ).splitlines()
        if line.strip()
    }
    worktrees = worktree_branches(root)
    owners = _branches_by_task(manager)
    now = datetime.now(timezone.utc).timestamp()

    rows: List[BranchRow] = []
    for name in names:
        if not name or name == base:
            continue
        is_merged = name in merged
        owner = owners.get(name)
        rows.append(
            BranchRow(
                name=name,
                merged=is_merged,
                worktree=worktrees.get(name),
                task_id=owner.id if owner else None,
                task_state=_task_state(owner) if owner else "no task lists this branch",
                age_days=None if is_merged else _age_days(root, base, name, now),
            )
        )
    return BranchReport(base=base, rows=sorted(rows, key=lambda row: row.name))


def _age_days(root: Path, base: str, branch: str, now: float) -> Optional[float]:
    """How long this branch has really been open, in days, or None if it has no commits.

    ``%at`` -- the *author* date -- rather than ``%ct``. A rebase rewrites committer
    dates and preserves author dates, so ``%ct`` would report a branch rebased an hour
    ago as an hour old however long it has actually been in flight, which is the exact
    number this report must not get wrong.
    """
    stamps = [
        line.strip()
        for line in _git(root, ["log", "--format=%at", f"{base}..{branch}"]).splitlines()
        if line.strip()
    ]
    if not stamps:
        return None
    try:
        oldest = float(stamps[-1])
    except ValueError:  # pragma: no cover - git does not emit a non-numeric %at
        return None
    return max(0.0, (now - oldest) / 86400.0)
