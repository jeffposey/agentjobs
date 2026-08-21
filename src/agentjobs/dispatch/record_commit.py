"""Committing the task records the dispatcher writes for itself.

Every other write to a task record is made by somebody who then commits it: a human
through the UI and their own git, an agent following the task lifecycle. The dispatcher
is the one writer with no such follow-up. Its terminal ``dispatch_result`` carries
``outcome`` and ``duration_seconds``, which are only knowable once the run process has
exited -- by definition after the session's last commit -- so the entry lands in a
working tree nobody is coming back to. Observed twice in one evening on job-hunting
task-016 (task-203): each dispatched run left the shared clone dirty, and the person who
found it was always the human.

The write is correctly timed. What was missing is that whoever performs it takes
responsibility for committing it. That is all this module does.

Three properties matter more than the mechanism:

**Only the one file.** ``git commit --only -- <path>`` commits that path from the
working tree and ignores the index entirely, so a colleague's ``git add``-ed but
uncommitted work in the same clone is still staged and still theirs afterwards. Never
``-A``, and never a bare ``git commit`` that would sweep whatever the index happens to
hold. This is not a stylistic preference: the clone is worked by people and other agents
at the same time, and a broad commit here would turn a dirty file into somebody's lost
afternoon.

**It never pushes.** See :func:`commit_task_record` for the reasoning; the short version
is that publishing to a shared remote is a different grant of authority from tidying
your own write, and an unattended daemon does not have it.

**It never raises.** This runs on the terminal path of a run that has already finished.
A git failure -- no repository, no git on PATH, a pre-commit hook that refuses, an
``index.lock`` held by somebody else -- must leave the run reported exactly as it would
have been, with the file dirty as it is today. The outcome is returned and logged, not
thrown.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from agentjobs.manager import TaskManager

GIT_TIMEOUT_SECONDS = 30
"""Ceiling on any one git invocation here, so a wedged git cannot hang a poller tick."""

LOCK_RETRIES = 3
"""Attempts at a commit that lost a race for ``index.lock``."""

LOCK_BACKOFF_SECONDS = 0.5
"""Pause between those attempts. Short: the competing operation is a commit, not a clone."""

_LOCK_MARKERS = ("index.lock", "another git process")
"""Substrings identifying a contended index, which is worth retrying rather than reporting."""


@dataclass(frozen=True)
class CommitOutcome:
    """What the attempt did, for the caller to log rather than to branch on."""

    committed: bool
    detail: str
    path: Optional[Path] = None

    def __str__(self) -> str:
        return self.detail


def _run_git(root: Path, args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    """One git invocation in *root*, decoded as UTF-8 whatever the machine's codepage."""
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_TIMEOUT_SECONDS,
    )


def _repository_root(path: Path) -> Optional[Path]:
    """The work tree containing *path*, or ``None`` when it is not in a repository."""
    try:
        result = _run_git(path.parent, ["rev-parse", "--show-toplevel"])
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    top = (result.stdout or "").strip()
    return Path(top) if top else None


def _porcelain_status(root: Path, relative: str) -> Optional[str]:
    """The two-character porcelain status of one path, or ``None`` when it is clean.

    ``--`` separates the pathspec from the options so a task file whose name began with
    a dash could not be read as one, which is cheap insurance for a path composed from a
    task id.
    """
    try:
        result = _run_git(root, ["status", "--porcelain", "--", relative])
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        if line.strip():
            return line[:2]
    return None


def _is_lock_contention(stderr: str) -> bool:
    """True when git refused because somebody else held the index."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in _LOCK_MARKERS)


def commit_task_record(
    manager: TaskManager,
    task_id: str,
    *,
    subject: str,
    actor: str = "dispatcher",
) -> CommitOutcome:
    """Commit the one task file the dispatcher just wrote, and nothing else.

    *subject* becomes the commit subject after a ``chore(task-nnn):`` prefix, so a
    reader of ``git log`` can tell a dispatcher commit from an agent's without opening
    it.

    **This does not push, and that is deliberate.** A commit is local and reversible and
    repairs exactly the problem in hand -- a working tree left dirty by a write nobody
    owned. A push is none of those things. It publishes to a shared remote, it can be
    rejected non-fast-forward, it can want credentials that a background process does
    not have and should not be taught to supply, and it can start CI. AgentJobs also
    cannot know that a remote is safe to push to: projects exist here whose standing
    rule is that they must never acquire one. Handling a rejected push would mean
    fetching, rebasing or forcing, unattended, in a clone somebody else is working --
    which is how automation destroys work rather than tidying it. So the dispatcher
    stops at the commit and leaves publishing to whoever is holding the ball. If
    unpushed dispatcher commits are ever observed piling up across days, the answer is
    an explicit per-project opt-in, not a changed default.

    Returns rather than raises on every failure. The caller is finishing a run.
    """
    path = manager.storage.task_path(task_id)
    if not path.exists():
        return CommitOutcome(False, f"no task file on disk for {task_id}", path)

    root = _repository_root(path)
    if root is None:
        return CommitOutcome(False, f"{path.parent} is not inside a git repository", path)

    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:  # pragma: no cover - a task dir symlinked out of its own repo
        return CommitOutcome(False, f"{path} is not inside {root}", path)

    status = _porcelain_status(root, relative)
    if status is None:
        # Either genuinely clean or git could not be read. Both mean there is nothing
        # this function should do, and neither is a failure worth alarming about.
        return CommitOutcome(False, f"nothing uncommitted in {relative}", path)

    prelude: List[str] = []
    if status == "??":
        # ``--only`` resolves its pathspec against what git knows, and an untracked file
        # is not that: the commit fails with "pathspec did not match any file(s)".
        # ``--intent-to-add`` registers the path without staging its content, which is
        # the smallest thing that makes the pathspec resolve.
        prelude = ["add", "--intent-to-add", "--", relative]

    message = f"chore({task_id}): {subject}"
    body = (
        f"Written by the AgentJobs {actor} after the run that produced it had exited, "
        "so no session was left to commit it (task-203)."
    )

    for attempt in range(LOCK_RETRIES):
        try:
            if prelude:
                intent = _run_git(root, prelude)
                if intent.returncode != 0:
                    return CommitOutcome(
                        False, f"git add -N refused {relative}: {intent.stderr.strip()}", path
                    )
            result = _run_git(
                root,
                ["commit", "--only", "-m", message, "-m", body, "--", relative],
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return CommitOutcome(False, f"git could not be run: {exc}", path)

        if result.returncode == 0:
            return CommitOutcome(True, f"committed {relative} as {message!r}", path)

        stderr = (result.stderr or "") + (result.stdout or "")
        if _is_lock_contention(stderr) and attempt + 1 < LOCK_RETRIES:
            time.sleep(LOCK_BACKOFF_SECONDS)
            continue
        return CommitOutcome(False, f"git commit refused {relative}: {stderr.strip()}", path)

    return CommitOutcome(False, f"git commit could not get the index lock for {relative}", path)
