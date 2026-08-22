"""Finishing an approved task without a model: the fixed half of ALLAGENTS.md steps 6-7.

Task-234 measured what an approval costs. A task's second dispatched run exists to do
five commands -- rebase, merge ``--no-ff``, mark the branch merged, close the task,
rebuild and restart -- and it averaged **about eleven minutes**, almost none of which was
those commands. Task-234 removed the cold boot from that run by resuming the session that
did the work. Its decision entry named the shape that removes the run itself, called it
option B, and deliberately declined to build it, because B's answer to a conflicting
rebase or a red gate is exactly the wake it was building -- and a mechanism whose fallback
does not exist yet is how a merge half-happens with nobody left to finish it.

The fallback exists. This is option B.

**The whole design is one sentence: do the determined part, and hand the undetermined
part to somebody who can think.** Every step below either succeeds on evidence or stops.
Nothing here resolves a conflict, retries a failed check, forces anything, or decides
that a difference is probably fine. When it stops it writes down exactly how far it got
and hands the ball back to the agent, which is the state a dispatch turns into a woken
session with its own memory of the branch.

Three properties are load-bearing, and each has a failure this repository has already
paid for at least once:

**The record is never ambiguous about the merge.** The merge is the one irreversible act
here, so the write that records it is the very next thing that happens, before the
rebuild, before the restart, and before anything that can fail. A reader of the task can
always tell whether ``main`` moved.

**Closing comes last, after delivery is verified -- not at step 4.** The task's spec
lists closing before the rebuild, and this deviates deliberately. ENGINEERING.md's
sharpest warning about this sequence is that a merged frontend change is invisible until
``npm run build`` runs in the serving clone, so the human ends up looking at the version
they approved you to replace. If closing came first, that exact failure would end with a
task marked ``completed``. Here it ends with an open task, the merge recorded, and a
prompt naming what remains.

**The restart is told, never assumed.** ``agentjobs restart`` binds the default port and
reports success while a dashboard on another port stays stale. That is silent, and it is
the reason ``FinishSettings.restart`` is machine-local configuration rather than a
default: with nothing configured and served code in the merge, this escalates rather than
claiming a delivery it has no way to make.

Nothing in this module may run unless a person put ``finish: {enabled: true}`` in
``~/.agentjobs/dispatch.yaml`` for that project, which no browser can write.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from agentjobs.actors import FINISHER
from agentjobs.dispatch.config import (
    DispatchError,
    FinishSettings,
    assert_dispatch_permitted,
)
from agentjobs.dispatch.ledger import RunLockTimeout, acquire_run_lock
from agentjobs.dispatch.phases import record_phase
from agentjobs.dispatch.record_commit import commit_task_record
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    BranchStatus,
    LogEntryType,
    Outcome,
    Task,
)
from agentjobs.projects import Project, default_home

FINISHES_DIRNAME = "finishes"
"""Where a finish's own record lives, beside ``runs/`` and deliberately not inside it.

A finish is not a run: no agent, no session, no tokens. Filing it under ``runs/`` would
make every runs-per-task figure in ``scripts/run_report.py`` count the thing this feature
exists to *remove* as another instance of it.
"""

GIT_TIMEOUT_SECONDS = 120
"""Ceiling on one git invocation. Generous: a merge in a large clone is not instant."""

NPM_TIMEOUT_SECONDS = 900
RESTART_TIMEOUT_SECONDS = 300

VERIFY_TIMEOUT_SECONDS = 120
VERIFY_POLL_SECONDS = 1.0

ACTIVATION_VARS = ("VIRTUAL_ENV", "POETRY_ACTIVE")
"""What tells Poetry to use an already-activated environment instead of this checkout's.

Scrubbed from every subprocess this module starts, for the reason ALLAGENTS.md gives at
length: a dispatched session inherits ``VIRTUAL_ENV`` pointing at the main clone, so
Poetry asked from a worktree answers with the main clone's environment and the gate then
runs against the wrong source. The server that spawns a finish inherits it too.
"""

SERVED_PREFIXES = ("src/agentjobs/", "frontend/")
"""Paths whose contents a running server is holding in memory or serving from a bundle.

A merge that touches none of them changes nothing a restart would fix, which is the only
case where finishing without a restart is honest.
"""

FRONTEND_PREFIX = "frontend/"


# ----- what happened ----------------------------------------------------------


@dataclass(frozen=True)
class StepResult:
    """One step of the sequence, and what it cost."""

    step: str
    ok: bool
    detail: str
    seconds: float = 0.0
    skipped: bool = False

    def render(self) -> str:
        mark = "skipped" if self.skipped else ("ok" if self.ok else "STOPPED")
        return f"  {self.step:<10} {mark:<8} {self.seconds:5.1f}s  {self.detail}"


FINISHED = "finished"
ESCALATED = "escalated"
DECLINED = "declined"


@dataclass(frozen=True)
class FinishResult:
    """The whole attempt, for a caller that wants to say what happened.

    ``DECLINED`` is not a failure and does not escalate. It means this task was never a
    candidate -- no branch to merge, the feature switched off, dispatch not permitted --
    so the approval behaves exactly as it did before this module existed.
    """

    task_id: str
    outcome: str
    reason: str
    detail: str
    steps: List[StepResult] = field(default_factory=list)
    finish_id: str = ""
    directory: Optional[Path] = None
    merge_commit: Optional[str] = None
    dispatched_run_id: Optional[str] = None

    @property
    def finished(self) -> bool:
        return self.outcome == FINISHED

    @property
    def merged(self) -> bool:
        return self.merge_commit is not None

    def render(self) -> str:
        lines = [f"{self.task_id}: {self.outcome} ({self.reason})", self.detail, ""]
        lines += [step.render() for step in self.steps]
        return "\n".join(line for line in lines if line is not None)


class Declined(Exception):
    """This task is not a finish candidate. The approval proceeds as it always did."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class Escalate(Exception):
    """A step could not be completed safely. A person or an agent takes it from here."""

    def __init__(self, step: str, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.step = step
        self.reason = reason
        self.detail = detail


# ----- subprocess plumbing ----------------------------------------------------


def detached_environment(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """This process's environment with any activated virtualenv removed."""
    env = {key: value for key, value in os.environ.items() if key not in ACTIVATION_VARS}
    if extra:
        env.update(extra)
    return env


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    env: Optional[Dict[str, str]] = None,
    log: Optional[Path] = None,
) -> "subprocess.CompletedProcess[str]":
    """Run one command, decoded as UTF-8 whatever the machine's codepage says.

    Output is captured and optionally teed to a file. A finish is unattended, so its
    only account of what a subprocess said is what it wrote down; ``gate.log`` beside
    the finish record is what a person reads when the gate went red.
    """
    result = subprocess.run(
        list(argv),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env if env is not None else detached_environment(),
    )
    if log is not None:
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(
                f"$ {' '.join(argv)}\n(in {cwd})\n\n"
                f"{result.stdout or ''}\n{result.stderr or ''}\n"
                f"\nexit {result.returncode}\n",
                encoding="utf-8",
            )
        except OSError:  # pragma: no cover - a log that cannot be written is not fatal
            pass
    return result


def git(
    root: Path, args: Sequence[str], *, timeout: int = GIT_TIMEOUT_SECONDS
) -> "subprocess.CompletedProcess[str]":
    """One git invocation in ``root``. Never raises for a non-zero exit."""
    return run_command(["git", "-C", str(root), *args], cwd=root, timeout=timeout)


def git_out(root: Path, args: Sequence[str]) -> str:
    """The stripped stdout of a git command, or "" when it failed."""
    result = git(root, args)
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def tail(text: str, lines: int = 25) -> str:
    """The last few lines of a command's output, for a log entry that must stay readable."""
    kept = [line for line in (text or "").splitlines() if line.strip()][-lines:]
    return "\n".join(kept)


# ----- reading the world ------------------------------------------------------


def active_branches(task: Task) -> List[str]:
    """Branch names this task says are still open."""
    return [branch.name for branch in task.branches if branch.status is BranchStatus.ACTIVE]


def worktree_paths(root: Path) -> Dict[str, Path]:
    """Every branch checked out in a worktree of this repository, keyed by branch name.

    Read from ``git worktree list --porcelain`` rather than guessed from a naming
    convention, because a worktree somewhere unexpected is exactly the case where a
    guess merges the wrong thing.
    """
    listing = git_out(root, ["worktree", "list", "--porcelain"])
    found: Dict[str, Path] = {}
    current: Optional[Path] = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree ") :].strip())
        elif line.startswith("branch ") and current is not None:
            ref = line[len("branch ") :].strip()
            found[ref.replace("refs/heads/", "", 1)] = current
    return found


def worktree_interpreter(worktree: Path) -> Optional[Path]:
    """The Python that imports *this worktree's* source, asked of Poetry.

    ``poetry run`` is not used to run the gate for the reason ALLAGENTS.md spells out:
    it prefers an activated virtualenv over the one keyed on the project path, and a
    finish spawned by the server inherits whatever the server's shell activated. Asking
    for the path once, with the activation scrubbed, and then naming the interpreter is
    the only form of this that cannot resolve to a neighbouring checkout.
    """
    poetry = shutil.which("poetry")
    if poetry is None:
        return None
    result = run_command([poetry, "env", "info", "--path"], cwd=worktree, timeout=120)
    if result.returncode != 0:
        return None
    venv = (result.stdout or "").strip()
    if not venv:
        return None
    python = Path(venv) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return python if python.is_file() else None


def dirty_paths(root: Path, *, untracked: bool = False) -> List[str]:
    """Paths with uncommitted changes, as repository-relative posix strings.

    ``untracked`` is off by default and on for exactly one caller. A worktree with a
    stray untracked file is ordinary and does not stop a rebase, so counting those in
    the preflight check would escalate a great deal of nothing. A *merge*, on the other
    hand, refuses outright rather than overwriting an untracked file it is bringing in --
    so for that question the answer has to include them, with ``-uall`` because the
    default collapses an untracked directory to its name and the whole point is which
    individual file collides.
    """
    flag = "--untracked-files=" + ("all" if untracked else "no")
    result = git(root, ["status", "--porcelain", flag])
    if result.returncode != 0:
        return []
    # Deliberately not `git_out`, which strips. Porcelain's status is two *columns* and
    # an unstaged modification leaves the first one blank, so stripping the output eats
    # a leading space and every path afterwards comes out a character short. That read as
    # "nothing clashes" -- the exact wrong direction for a check whose job is to stop a
    # merge from overwriting somebody's uncommitted work.
    paths: List[str] = []
    for line in (result.stdout or "").splitlines():
        if len(line) > 3:
            # A rename reads "old -> new"; the new name is the one a merge would touch.
            paths.append(line[3:].split(" -> ")[-1].strip().strip('"'))
    return paths


def changed_between(root: Path, start: str, end: str) -> List[str]:
    """Paths that differ between two commits."""
    listing = git_out(root, ["diff", "--name-only", f"{start}..{end}"])
    return [line.strip() for line in listing.splitlines() if line.strip()]


def touches(paths: Sequence[str], prefixes: Sequence[str]) -> bool:
    return any(path.startswith(prefix) for path in paths for prefix in prefixes)


# ----- the finish record ------------------------------------------------------


def finishes_root(home: Path) -> Path:
    return home / FINISHES_DIRNAME


@dataclass
class FinishDirectory:
    """Where one attempt writes itself down. Read by ``scripts/run_report.py``."""

    path: Path
    finish_id: str

    @classmethod
    def create(cls, home: Path, task_id: str, project_id: str) -> "FinishDirectory":
        finish_id = f"fin_{uuid.uuid4().hex[:8]}"
        path = finishes_root(home) / finish_id
        path.mkdir(parents=True, exist_ok=True)
        directory = cls(path=path, finish_id=finish_id)
        directory.write_meta(
            finish_id=finish_id,
            task_id=task_id,
            project_id=project_id,
            outcome="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        return directory

    @property
    def meta_path(self) -> Path:
        return self.path / "meta.yaml"

    def write_meta(self, **fields: Any) -> None:
        """Merge fields into ``meta.yaml``. Never raises: a finish outlives its record."""
        try:
            import yaml

            existing: Dict[str, Any] = {}
            if self.meta_path.is_file():
                loaded = yaml.safe_load(self.meta_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            existing.update(fields)
            self.meta_path.write_text(
                yaml.safe_dump(existing, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
        except Exception:  # pragma: no cover - writing a record must not fail a finish
            pass

    def record(self, kind: str, **fields: Any) -> None:
        record_phase(self.path, kind, finish_id=self.finish_id, **fields)


# ----- the steps --------------------------------------------------------------


@dataclass
class Plan:
    """What preflight established, passed between steps rather than re-derived."""

    root: Path
    branch: str
    worktree: Path
    interpreter: Path
    base: str
    branch_head_before: str
    base_head_before: str


def preflight(task: Task, root: Path, settings: FinishSettings) -> Plan:
    """Establish that every precondition holds, or decline / escalate saying which.

    The distinction matters and is drawn once, here. **Declining** means this was never
    a finish candidate -- no branch, a branch that no longer exists -- and the approval
    should behave exactly as it did before. **Escalating** means it is a candidate and
    something is wrong with the world: the shared clone is on the wrong branch, or a
    worktree is missing. The second wants a person; the first wants nothing.
    """
    branches = active_branches(task)
    if not branches:
        raise Declined(
            "no_active_branch",
            f"{task.id} lists no active branch, so there is nothing to merge.",
        )
    if len(branches) > 1:
        raise Declined(
            "several_active_branches",
            f"{task.id} lists {len(branches)} active branches ({', '.join(branches)}). "
            "Which one is the deliverable is a judgement, not a lookup.",
        )
    branch = branches[0]

    if not (root / ".git").exists():
        raise Declined("not_a_repository", f"{root} is not a git checkout.")

    if not git_out(root, ["rev-parse", "--verify", f"refs/heads/{branch}"]):
        raise Declined(
            "branch_missing",
            f"Branch {branch!r} does not exist in {root}. It was merged and deleted, or "
            "it was never pushed to this clone.",
        )

    base = settings.base_branch
    base_head = git_out(root, ["rev-parse", "--verify", f"refs/heads/{base}"])
    if not base_head:
        raise Declined("base_missing", f"There is no {base!r} branch in {root} to merge into.")

    checked_out = git_out(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if checked_out != base:
        raise Escalate(
            "preflight",
            "clone_not_on_base",
            f"The shared clone at {root} has {checked_out!r} checked out, not {base!r}. "
            "Checking it out from here would replace the files under whoever is working "
            "in it, which is the failure worktrees exist to prevent -- so nothing was "
            "touched.",
        )

    worktree = worktree_paths(root).get(branch)
    if worktree is None or not worktree.is_dir():
        raise Escalate(
            "preflight",
            "worktree_missing",
            f"Branch {branch!r} has no worktree in this repository, so there is nowhere "
            "to rebase it and nowhere to run the gate. Nothing was touched.",
        )

    unclean = dirty_paths(worktree)
    if unclean:
        raise Escalate(
            "preflight",
            "worktree_dirty",
            f"The worktree at {worktree} has uncommitted changes to "
            f"{', '.join(unclean[:5])}{' and more' if len(unclean) > 5 else ''}. A "
            "rebase would refuse or would carry them, and neither is this script's call.",
        )

    interpreter = worktree_interpreter(worktree)
    if interpreter is None:
        raise Escalate(
            "preflight",
            "no_interpreter",
            f"Poetry could not name an interpreter for {worktree}, so the gate cannot be "
            "run against this branch's own source. Run `python scripts/bootstrap.py` "
            "there. Nothing was touched.",
        )

    return Plan(
        root=root,
        branch=branch,
        worktree=worktree,
        interpreter=interpreter,
        base=base,
        branch_head_before=git_out(root, ["rev-parse", branch]),
        base_head_before=base_head,
    )


def rebase(plan: Plan) -> str:
    """Rebase the branch onto the base, in its own worktree. A conflict is never resolved.

    On any failure the rebase is aborted and the branch's tip is read back and compared
    with what it was. Reporting "the branch is untouched" without checking would be the
    one sentence in an escalation that must not be a guess -- the woken session decides
    what to do next on the strength of it.
    """
    result = run_command(
        ["git", "rebase", plan.base],
        cwd=plan.worktree,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        return git_out(plan.root, ["rev-parse", plan.branch])

    abort = run_command(
        ["git", "rebase", "--abort"], cwd=plan.worktree, timeout=GIT_TIMEOUT_SECONDS
    )
    after = git_out(plan.root, ["rev-parse", plan.branch])
    restored = after == plan.branch_head_before
    state = (
        f"The branch is exactly where it was ({plan.branch_head_before[:8]})."
        if restored
        else (
            f"**The abort did not restore it.** {plan.branch} was "
            f"{plan.branch_head_before[:8]} and is now {after[:8] or 'unreadable'}; "
            f"`git rebase --abort` exited {abort.returncode}. Do not assume the "
            "branch is clean -- look before you do anything else."
        )
    )
    raise Escalate(
        "rebase",
        "rebase_conflict" if restored else "rebase_abort_failed",
        f"Rebasing {plan.branch} onto {plan.base} did not apply cleanly, and a conflict "
        f"is never resolved here. {state} Nothing was merged.\n\n"
        f"```\n{tail(result.stdout + result.stderr)}\n```",
    )


def run_gate(plan: Plan, directory: FinishDirectory, settings: FinishSettings) -> float:
    """Run the full gate on the rebased branch, with that worktree's own interpreter.

    The unqualified command, never ``--only`` or ``--from`` or ``--since-gate``. The
    partial forms exist for a person iterating on a late failure and print ``PARTIAL
    RUN`` precisely so their green cannot be reported as the gate's; a merge made on one
    would be doing exactly that.
    """
    started = time.monotonic()
    log = directory.path / "gate.log"
    result = run_command(
        [str(plan.interpreter), "scripts/check.py"],
        cwd=plan.worktree,
        timeout=settings.gate_timeout_seconds,
        env=detached_environment(
            {"AGENTJOBS_RUN_ID": directory.finish_id, "AGENTJOBS_RUN_DIR": str(directory.path)}
        ),
        log=log,
    )
    seconds = time.monotonic() - started
    if result.returncode != 0:
        raise Escalate(
            "gate",
            "gate_failed",
            f"`scripts/check.py` failed on {plan.branch} after the rebase "
            f"({seconds:.0f}s, exit {result.returncode}). A red gate never merges. The "
            f"branch is rebased onto {plan.base} and is otherwise untouched; nothing was "
            f"merged. Full output: {log}\n\n"
            f"```\n{tail(result.stdout + result.stderr, 30)}\n```",
        )
    return seconds


def previous_merge_commit(task: Task) -> Optional[str]:
    """The merge this task's own record says a finish already made, if any.

    Read so that ``merge`` can tell its two "nothing to merge" cases apart. They look
    identical to git and mean opposite things: a **retry** of a finish that merged and
    then stopped at the restart is finishing its own work, and a branch somebody else
    merged by hand is a fact about the world that nothing here should quietly close a
    task over.
    """
    for entry in reversed(task.log):
        if entry.data.get("finish_step") == "merge":
            commit = entry.data.get("merge_commit")
            return str(commit) if commit else None
    return None


def merge(plan: Plan, task: Task, approver: str) -> str:
    """``git merge --no-ff`` into the base, in the shared clone. The irreversible step.

    Two things are checked first that git would otherwise turn into a mess rather than a
    refusal: that nothing dirty in the clone is also in the merge, and that the base has
    not moved since preflight read it. The second is the race a rebase cannot close --
    somebody else merging between the gate and this -- and the answer to it is to
    escalate, not to rebase again in a loop.

    A merge that moves nothing is the third case, and it is not a success. ``git merge
    --no-ff`` of a branch already contained in the base prints "Already up to date" and
    exits **zero**, so taking the exit code at face value would close a task on the
    strength of a merge that did not happen. Which of the two meanings it has is decided
    from the task's own record rather than guessed at.
    """
    base_now = git_out(plan.root, ["rev-parse", plan.base])
    if base_now != plan.base_head_before:
        raise Escalate(
            "merge",
            "base_moved",
            f"{plan.base} moved from {plan.base_head_before[:8]} to {base_now[:8]} while "
            f"the gate was running, so what was verified is no longer what would be "
            "merged. The branch is rebased onto the older base and nothing was merged.",
        )

    incoming = changed_between(plan.root, plan.base, plan.branch)
    clashing = sorted(set(dirty_paths(plan.root, untracked=True)) & set(incoming))
    if clashing:
        raise Escalate(
            "merge",
            "clone_dirty_in_merge",
            f"The shared clone has uncommitted changes to {', '.join(clashing)}, which "
            "this merge would overwrite. Somebody is working in there. Nothing was "
            "merged and nothing was reverted.",
        )

    message = (
        f"Merge branch '{plan.branch}' ({task.id})\n\n"
        f"{task.title}\n\n"
        f"Approved by {approver} in the AgentJobs web UI. Merged by the AgentJobs "
        f"finisher after rebasing onto {plan.base} and running scripts/check.py green "
        f"in that branch's worktree (task-241). No model was in this loop; a person "
        f"still approved the work."
    )
    result = git(plan.root, ["merge", "--no-ff", "--no-edit", "-m", message, plan.branch])
    if result.returncode != 0:
        git(plan.root, ["merge", "--abort"])
        head_now = git_out(plan.root, ["rev-parse", "HEAD"])
        raise Escalate(
            "merge",
            "merge_failed",
            f"`git merge --no-ff {plan.branch}` failed and was aborted. {plan.base} is at "
            f"{head_now[:8]} (it was {plan.base_head_before[:8]}).\n\n"
            f"```\n{tail(result.stdout + result.stderr)}\n```",
        )

    head_after = git_out(plan.root, ["rev-parse", "HEAD"])
    if head_after == plan.base_head_before:
        already = previous_merge_commit(task)
        if already:
            # This finish's own earlier attempt merged and then stopped after it. Picking
            # up where it left off is the whole reason `agentjobs finish` can be re-run.
            return already
        raise Escalate(
            "merge",
            "already_merged",
            f"`git merge --no-ff {plan.branch}` moved nothing: the branch is already "
            f"contained in {plan.base}, and this task's record has no merge on it. "
            "Somebody merged it by hand. Nothing here will close a task on the strength "
            "of a merge it cannot account for -- check who did it and why before "
            "finishing this.",
        )
    return head_after


def contains_commit(root: Path, ancestor: str, descendant: str) -> bool:
    """Whether ``descendant`` contains ``ancestor``, as git reckons containment.

    Asked instead of comparing two commits for equality, because by the time anything is
    verified the base has legitimately moved on: the finisher commits the task record to
    the base right after merging, so a server restarted afterwards reports *that* commit
    and not the merge. Equality would call every successful delivery a failure.
    """
    result = git(root, ["merge-base", "--is-ancestor", ancestor, descendant])
    return result.returncode == 0


def rebuild_frontend(
    plan: Plan, merged_paths: Sequence[str], directory: FinishDirectory
) -> StepResult:
    """``npm run build`` in the serving clone, if and only if the merge touched frontend/.

    ``frontend_dist/`` is gitignored, so a merged React change is committed and invisible
    until this runs. It is the step most likely to be skipped and the one whose failure
    the human sees first.
    """
    started = time.monotonic()
    if not touches(merged_paths, [FRONTEND_PREFIX]):
        return StepResult("rebuild", True, "the merge touched no frontend/ path", 0.0, skipped=True)
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        raise Escalate(
            "rebuild",
            "no_npm",
            "The merge changed the frontend and npm is not on this machine's PATH, so "
            "the bundle cannot be rebuilt. **The merge is done**; what is missing is the "
            "build, so the server is still serving the pre-merge bundle.",
        )
    result = run_command(
        [npm, "run", "build"],
        cwd=plan.root / "frontend",
        timeout=NPM_TIMEOUT_SECONDS,
        log=directory.path / "build.log",
    )
    seconds = time.monotonic() - started
    if result.returncode != 0:
        raise Escalate(
            "rebuild",
            "build_failed",
            f"`npm run build` failed after the merge (exit {result.returncode}). **The "
            "merge is done**; the server is still serving the pre-merge bundle. Full "
            f"output: {directory.path / 'build.log'}\n\n"
            f"```\n{tail(result.stdout + result.stderr)}\n```",
        )
    return StepResult("rebuild", True, f"rebuilt the bundle in {plan.root / 'frontend'}", seconds)


def restart_server(
    plan: Plan, merged_paths: Sequence[str], settings: FinishSettings, directory: FinishDirectory
) -> StepResult:
    """Restart the server the way this machine says it was started, or say why not.

    **A configured restart always runs, whatever the merge touched.** ``SERVED_PREFIXES``
    is a guess about what a process is holding -- it would have to be right about
    templates, static files, the bundle and the package metadata all at once, and being
    wrong about any of them leaves the human on stale code, which is the one failure this
    step exists to prevent. A few seconds of downtime on a local dashboard is a much
    smaller cost than being wrong about that, so the prefixes are used only to decide
    whether an *unconfigured* restart may be skipped.

    An empty ``restart`` is not "no restart needed". It is "nobody has told this machine
    how", and the difference decides whether a merge that changed served code can be
    reported as delivered. Guessing ``agentjobs restart`` here is the specific silent
    failure ENGINEERING.md warns about.
    """
    started = time.monotonic()
    if not settings.restart:
        if touches(merged_paths, SERVED_PREFIXES):
            raise Escalate(
                "restart",
                "no_restart_command",
                "The merge changed code the server holds in memory, and no restart "
                "command is configured for this project in ~/.agentjobs/dispatch.yaml "
                "(`finish.restart`). **The merge is done** and the running server is "
                "still on the old code. Restart it the way it was started.",
            )
        return StepResult("restart", True, "the merge touched no served code", 0.0, skipped=True)
    result = run_command(
        settings.restart,
        cwd=plan.root,
        timeout=RESTART_TIMEOUT_SECONDS,
        log=directory.path / "restart.log",
    )
    seconds = time.monotonic() - started
    if result.returncode != 0:
        raise Escalate(
            "restart",
            "restart_failed",
            f"The configured restart command exited {result.returncode}. **The merge is "
            f"done** and the server may be down. Command: {' '.join(settings.restart)}. "
            f"Full output: {directory.path / 'restart.log'}\n\n"
            f"```\n{tail(result.stdout + result.stderr)}\n```",
        )
    return StepResult("restart", True, f"ran {' '.join(settings.restart)}", seconds)


def fetch_version(base_url: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """``GET /api/version``, or None when nothing answered or the answer was not JSON."""
    try:
        with urllib.request.urlopen(
            f"{base_url.rstrip('/')}/api/version", timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return payload if isinstance(payload, dict) else None


def _same_checkout(reported: Path, root: Path) -> bool:
    """Whether a reported source root is this clone, tolerating the ``src`` it may name.

    ``/api/version`` reports the checkout root for a source install and the package
    directory for an ordinary one, and either may be reported with a different case or
    through a different symlink on Windows. Both readings of "this clone" are accepted;
    an unrelated directory is not.
    """
    try:
        resolved = reported.resolve()
        here = root.resolve()
    except OSError:  # pragma: no cover - an unresolvable path is not this clone
        return False
    return resolved == here or here in resolved.parents


def verify_live(
    plan: Plan,
    merge_commit: str,
    base_url: str,
    restarted: bool,
    *,
    timeout: float = VERIFY_TIMEOUT_SECONDS,
    sleep: float = VERIFY_POLL_SECONDS,
) -> StepResult:
    """Prove the running process is the merged code. Not that the port answers.

    Two independent facts, both from ``/api/version``, both fixed at the serving
    process's startup and neither recomputable by a stale process:

    - ``source_root`` is the checkout it imported. A different clone answering on the
      same address is a real deployment mistake in a repository with several worktrees,
      and it looks identical to success if only the commit is compared. It is checked
      first and it never retries: waiting cannot turn the wrong clone into the right one.
    - ``source_commit`` is the commit its source was at when it started. A server that
      came up before the merge reports the pre-merge commit however new the files on disk
      now are -- which is exactly what "the port answers" cannot tell you. This is the
      one that is retried, because a restarting server legitimately answers as its old
      self for a moment, or not at all.

    **Containment, not equality.** The merge commit must be an *ancestor of* what the
    server reports. The finisher commits the task record to the base immediately after
    merging, so the base has already moved past the merge by the time anything restarts;
    demanding equality would fail every successful delivery. Containment says the thing
    that actually matters -- the running code includes this merge -- and keeps saying it
    when somebody else's commit lands in between.

    ``started_at`` is reported rather than tested. It is the floor the task's spec calls
    a floor, and it is worth having in the record for a source that is not a checkout and
    has no commit to compare.
    """
    if not restarted:
        return StepResult(
            "verify",
            True,
            "no restart was needed, so nothing changed under the server",
            0.0,
            skipped=True,
        )

    deadline = time.monotonic() + timeout
    last = "nothing answered"
    while time.monotonic() < deadline:
        payload = fetch_version(base_url)
        if payload is None:
            last = f"nothing is answering {base_url}"
            time.sleep(sleep)
            continue
        root = str(payload.get("source_root") or "")
        if root and not _same_checkout(Path(root), plan.root):
            raise Escalate(
                "verify",
                "wrong_checkout_serving",
                f"{base_url} is served from the wrong checkout: it imported {root}, and "
                f"the merge went into {plan.root}. **The merge is done.** Do not restart "
                "anything until you know which clone that process belongs to -- this is "
                "how a dashboard ends up serving an unmerged branch from correct-looking "
                "task files.",
            )
        commit = payload.get("source_commit")
        if commit is None:
            last = (
                f"{base_url} answers but reports no source_commit, so it cannot be shown "
                "to be running this merge"
            )
            time.sleep(sleep)
            continue
        if not contains_commit(plan.root, merge_commit, str(commit)):
            last = (
                f"{base_url} is serving commit {str(commit)[:8]}, which does not contain "
                f"the merge {merge_commit[:8]} -- it is the process that was already "
                "running"
            )
            time.sleep(sleep)
            continue
        return StepResult(
            "verify",
            True,
            f"{base_url} is serving {str(commit)[:8]}, which contains the merge "
            f"{merge_commit[:8]}, from {root or plan.root} (started "
            f"{payload.get('started_at')})",
            0.0,
        )

    raise Escalate(
        "verify",
        "not_live",
        f"After the restart, the merged code could not be shown to be live within "
        f"{timeout:.0f}s: {last}. **The merge is done** and the rebuild and restart "
        "both ran, so this is about the serving process, not about the branch.",
    )


def remove_worktree(plan: Plan) -> StepResult:
    """Retire the branch's worktree. A failure here is litter, not a half-finished merge.

    Deliberately not an escalation. The merge is in, the delivery is verified and the
    task is closed; waking a session to delete a directory would cost more than the
    directory does. It is written down instead, which is what makes it findable.
    """
    result = git(plan.root, ["worktree", "remove", str(plan.worktree)])
    if result.returncode != 0:
        return StepResult(
            "worktree",
            True,
            f"could not remove {plan.worktree}: {tail(result.stderr, 3)} -- left in place",
            0.0,
        )
    return StepResult("worktree", True, f"removed {plan.worktree}", 0.0)


# ----- writing it down --------------------------------------------------------


def mark_branch_merged(manager: TaskManager, task_id: str, branch: str) -> None:
    """Set this branch ``merged`` in ``branches[]``, leaving every other entry alone.

    Re-reads the task rather than patching the copy preflight was given. Several log
    entries have been appended since then, and a patch computed from a stale record is
    how a concurrent write gets silently discarded.
    """
    current = manager.get_task(task_id)
    if current is None:  # pragma: no cover - the task was read moments ago
        return
    branches: List[Dict[str, Any]] = []
    for entry in current.branches:
        item = entry.model_dump(mode="python")
        if entry.name == branch:
            item["status"] = BranchStatus.MERGED.value
            item["merged_at"] = datetime.now(timezone.utc)
        branches.append(item)
    manager.update_task(task_id, actor=FINISHER, branches=branches)


def announce_start(
    manager: TaskManager, task_id: str, plan: Plan, directory: FinishDirectory
) -> None:
    """Say on the record that a finish is running, before the part that takes minutes.

    The gate is the expensive step and it is silent. Without this the task reads
    ``agent``/``work`` with the approval's own prompt for two or three minutes while a
    rebase and a full gate happen underneath it, and somebody watching the dashboard has
    no way to tell a finish in progress from an approval nothing picked up.

    It also covers the case a log entry cannot be written for afterwards: a machine that
    reboots mid-gate leaves this entry and nothing else, which is a much better record
    than none.
    """
    manager.add_log_entry(
        task_id,
        actor=FINISHER,
        type=LogEntryType.PROGRESS,
        body=(
            f"Scripted finish started on `{plan.branch}` "
            f"({plan.branch_head_before[:8]}).\n\n"
            f"Rebasing onto `{plan.base}` and running the full gate in {plan.worktree}. "
            "**Nothing is merged yet** and nothing will be unless the gate is green. "
            f"Its output will be at {directory.path / 'gate.log'}."
        ),
        data={"finish_step": "started", "branch": plan.branch, "finish_id": directory.finish_id},
    )
    commit_task_record(
        manager,
        task_id,
        subject=f"note the scripted finish starting on {plan.branch}",
        actor=FINISHER,
    )


def record_merge(
    manager: TaskManager, task_id: str, plan: Plan, merge_commit: str, approver: str
) -> None:
    """Write the merge onto the record immediately, before anything that can fail.

    This is the ordering rule the whole module is arranged around. Between ``git merge``
    and this write there is nothing but a function call; every later failure escalates
    with the merge already stated, so no reader of the task ever has to work out from a
    prompt whether ``main`` moved.
    """
    manager.add_log_entry(
        task_id,
        actor=FINISHER,
        type=LogEntryType.PROGRESS,
        body=(
            f"Merged `{plan.branch}` into `{plan.base}` as `{merge_commit[:8]}`.\n\n"
            f"Rebased onto {plan.base} and `scripts/check.py` ran green in "
            f"{plan.worktree} before the merge. Approved by {approver}; no agent was "
            "in this loop. Delivery -- rebuild, restart, verification -- comes next, "
            "and this task stays open until it is verified."
        ),
        data={
            "finish_step": "merge",
            "merge_commit": merge_commit,
            "branch": plan.branch,
            "base": plan.base,
        },
    )


def escalate_on_record(
    manager: TaskManager,
    task_id: str,
    failure: Escalate,
    steps: Sequence[StepResult],
    merge_commit: Optional[str],
) -> None:
    """Say exactly how far the finish got, then hand the ball to the agent.

    The prompt is written for a session that may be *resumed* -- it remembers its branch
    and its worktree and is confident about both -- so it leads with what changed
    underneath that memory rather than with a request.
    """
    account = "\n".join(step.render() for step in steps)
    merged = (
        f"**The merge is done: `{merge_commit[:8]}`.**"
        if merge_commit
        else "**Nothing was merged.**"
    )
    body = (
        f"The scripted finish stopped at `{failure.step}` ({failure.reason}).\n\n"
        f"{merged}\n\n{failure.detail}\n\n```\n{account}\n```"
    )
    manager.add_log_entry(
        task_id,
        actor=FINISHER,
        type=LogEntryType.PROGRESS,
        body=body,
        data={
            "finish_step": failure.step,
            "finish_reason": failure.reason,
            "merge_commit": merge_commit,
            "merged": merge_commit is not None,
        },
    )
    manager.handoff(
        task_id,
        actor=FINISHER,
        ball=Ball.AGENT,
        ball_reason=BallReason.WORK,
        ball_prompt=(
            f"The approval ran the scripted finish and it stopped at `{failure.step}`. "
            f"{merged}\n\n{failure.detail}\n\n"
            "Take it from here by hand. Check the tree against what is written above "
            "before acting on anything you remember: if your worktree, your branch or "
            "your account of this task no longer matches what is on disk, say so on the "
            "record and hand the ball back rather than improvising a recovery."
        ),
    )


# ----- the orchestrator -------------------------------------------------------


def finish_task(
    *,
    manager: TaskManager,
    project: Project,
    task_id: str,
    approver: str,
    home: Optional[Path] = None,
    api_base: Optional[str] = None,
    settings: Optional[FinishSettings] = None,
) -> FinishResult:
    """Do the fixed part of ALLAGENTS.md steps 6 and 7, or stop and say where.

    Takes the task's run lock for the whole attempt, so a dispatch cannot start a
    session into a tree this is rebasing, and two approvals cannot merge the same branch
    twice. It is the same lock a run takes, which is the point: the two are alternatives.
    """
    resolved_home = home or default_home()
    if settings is None:
        try:
            resolution = assert_dispatch_permitted(project.id, resolved_home)
        except DispatchError as exc:
            return FinishResult(
                task_id=task_id,
                outcome=DECLINED,
                reason=getattr(exc, "reason", "dispatch_error"),
                detail=str(exc),
            )
        settings = resolution.settings.finish
        api_base = api_base or resolution.config.api_base

    if not settings.enabled:
        return FinishResult(
            task_id=task_id,
            outcome=DECLINED,
            reason="not_enabled",
            detail=f"{project.id} has no `finish.enabled: true` in this machine's dispatch config.",
        )

    task = manager.get_task(task_id)
    if task is None or not task.is_open:
        return FinishResult(
            task_id=task_id,
            outcome=DECLINED,
            reason="not_open",
            detail=f"{task_id} is closed or missing; there is nothing to finish.",
        )

    try:
        lock = acquire_run_lock(resolved_home, task_id)
    except RunLockTimeout as exc:
        return FinishResult(task_id=task_id, outcome=DECLINED, reason="locked", detail=str(exc))

    directory = FinishDirectory.create(resolved_home, task_id, project.id)
    started = time.monotonic()
    steps: List[StepResult] = []
    try:
        result = _guarded_sequence(
            manager=manager,
            project=project,
            task=task,
            approver=approver,
            settings=settings,
            api_base=api_base,
            directory=directory,
            steps=steps,
        )
        directory.write_meta(
            outcome=FINISHED,
            finished_at=datetime.now(timezone.utc).isoformat(),
            seconds=round(time.monotonic() - started, 2),
            merge_commit=result.merge_commit,
        )
        return result
    except Declined as exc:
        directory.write_meta(
            outcome=DECLINED,
            reason=exc.reason,
            finished_at=datetime.now(timezone.utc).isoformat(),
            seconds=round(time.monotonic() - started, 2),
        )
        return FinishResult(
            task_id=task_id,
            outcome=DECLINED,
            reason=exc.reason,
            detail=exc.detail,
            steps=steps,
            finish_id=directory.finish_id,
            directory=directory.path,
        )
    except Escalate as exc:
        merge_commit = _merge_commit_of(steps)
        steps.append(StepResult(exc.step, False, exc.detail.splitlines()[0], 0.0))
        directory.write_meta(
            outcome=ESCALATED,
            reason=exc.reason,
            stopped_at=exc.step,
            merged=merge_commit is not None,
            merge_commit=merge_commit,
            finished_at=datetime.now(timezone.utc).isoformat(),
            seconds=round(time.monotonic() - started, 2),
        )
        escalate_on_record(manager, task_id, exc, steps, merge_commit)
        commit_task_record(
            manager,
            task_id,
            subject=f"escalate the scripted finish at {exc.step}",
            actor=FINISHER,
        )
        # The lock is released before dispatching: the run this may start takes the same
        # per-task lock, and holding it while asking for it would refuse every
        # escalation on this machine for the reason "an escalation is in progress".
        lock.release()
        run_id = dispatch_after_escalation(
            manager=manager,
            project=project,
            project_config=project.load_config(),
            task_id=task_id,
            home=resolved_home,
            api_base=api_base,
        )
        directory.write_meta(dispatched_run_id=run_id)
        return FinishResult(
            task_id=task_id,
            outcome=ESCALATED,
            reason=exc.reason,
            detail=exc.detail,
            steps=steps,
            finish_id=directory.finish_id,
            directory=directory.path,
            merge_commit=merge_commit,
            dispatched_run_id=run_id,
        )
    finally:
        lock.release()


def _merge_commit_of(steps: Sequence[StepResult]) -> Optional[str]:
    """The merge commit, if the merge step got as far as recording one."""
    for step in steps:
        if step.step == "merge" and step.ok:
            return step.detail.split()[-1]
    return None


def _guarded_sequence(**kwargs: Any) -> FinishResult:
    """``_sequence``, with every unanticipated failure turned into an escalation.

    A traceback out of here would be the exact failure this task must not introduce: a
    merge that happened, a record that does not say so, and a process that died before
    it could hand the ball to anybody. So anything the sequence does not model becomes
    a stop like any other -- named ``unexpected``, carrying the exception verbatim,
    landing on the record with the steps that did complete. An ugly escalation is worth
    a great deal more than a silent one.
    """
    try:
        return _sequence(**kwargs)
    except (Escalate, Declined):
        raise
    except Exception as unexpected:
        raise Escalate(
            "unexpected",
            "unexpected_error",
            f"The scripted finish hit something it does not handle: "
            f"`{type(unexpected).__name__}: {unexpected}`. The steps below say how far "
            "it got; check the tree against them before doing anything else.",
        ) from unexpected


def _sequence(
    *,
    manager: TaskManager,
    project: Project,
    task: Task,
    approver: str,
    settings: FinishSettings,
    api_base: Optional[str],
    directory: FinishDirectory,
    steps: List[StepResult],
) -> FinishResult:
    """The sequence itself, with every stop expressed as an exception.

    Written as one straight line on purpose. The ordering *is* the safety argument -- see
    the module docstring -- and a version of this with early returns hides it.
    """
    root = project.root
    began = time.monotonic()
    plan = preflight(task, root, settings)
    steps.append(
        StepResult(
            "preflight",
            True,
            f"{plan.branch} at {plan.branch_head_before[:8]} in {plan.worktree}",
            time.monotonic() - began,
        )
    )
    directory.record("finish_preflight", branch=plan.branch, worktree=str(plan.worktree))
    announce_start(manager, task.id, plan, directory)

    # Re-read the base *after* the announcement, because the announcement commits a task
    # record onto it. `merge` refuses when the base moved between here and the gate
    # finishing -- that check is for somebody else's merge landing mid-gate, and it does
    # not get to fire on this function's own bookkeeping. Caught by a test the moment the
    # announcement was added: every finish escalated with `base_moved`.
    plan.base_head_before = git_out(plan.root, ["rev-parse", plan.base])

    began = time.monotonic()
    rebased = rebase(plan)
    steps.append(
        StepResult(
            "rebase",
            True,
            f"{plan.branch} onto {plan.base}: {plan.branch_head_before[:8]} -> {rebased[:8]}",
            time.monotonic() - began,
        )
    )

    gate_seconds = run_gate(plan, directory, settings)
    steps.append(StepResult("gate", True, "scripts/check.py green", gate_seconds))

    began = time.monotonic()
    merge_commit = merge(plan, task, approver)
    steps.append(
        StepResult(
            "merge", True, f"--no-ff into {plan.base} as {merge_commit}", time.monotonic() - began
        )
    )
    # Immediately, before anything that can fail. See `record_merge`.
    record_merge(manager, task.id, plan, merge_commit, approver)
    commit_task_record(
        manager, task.id, subject=f"record the merge of {plan.branch}", actor=FINISHER
    )

    merged_paths = changed_between(plan.root, plan.base_head_before, merge_commit)
    steps.append(rebuild_frontend(plan, merged_paths, directory))
    restart = restart_server(plan, merged_paths, settings, directory)
    steps.append(restart)
    steps.append(
        verify_live(
            plan,
            merge_commit,
            (settings.verify_base or api_base or "http://127.0.0.1:8765"),
            restarted=not restart.skipped,
            timeout=settings.verify_timeout_seconds,
        )
    )

    mark_branch_merged(manager, task.id, plan.branch)
    manager.close_task(
        task.id,
        actor=FINISHER,
        outcome=Outcome.COMPLETED,
        body=(
            f"Merged `{plan.branch}` into `{plan.base}` as `{merge_commit[:8]}` and "
            f"verified live. Approved by {approver}; finished by the scripted path with "
            "no agent session (task-241).\n\n"
            # The step table, on the successful path as well as the escalating one. An
            # escalation has to say how far it got or the record is ambiguous; a success
            # has to say the same thing for a different reason -- "verified live" is a
            # claim, and this is the evidence for it, including what verification
            # actually asked and what answered. It stops at verification because this
            # entry *is* the close; the worktree is retired immediately after it.
            "Everything up to and including verification:\n\n"
            "```\n" + "\n".join(step.render() for step in steps) + "\n```"
        ),
    )
    steps.append(StepResult("close", True, "closed completed", 0.0))
    steps.append(remove_worktree(plan))
    commit_task_record(
        manager, task.id, subject=f"close after merging {plan.branch}", actor=FINISHER
    )

    return FinishResult(
        task_id=task.id,
        outcome=FINISHED,
        reason="finished",
        detail=f"Merged {plan.branch} as {merge_commit[:8]} and verified it live.",
        steps=steps,
        finish_id=directory.finish_id,
        directory=directory.path,
        merge_commit=merge_commit,
    )


# ----- escalating into a dispatched (and therefore woken) session --------------


def newest_human_entry(task: Task, project_config: Dict[str, Any]) -> Optional[int]:
    """The id of the newest log entry a configured human wrote, or None.

    A finish only ever runs as a consequence of an approval, so this is that approval.
    It is looked up rather than passed in because the alternative -- threading an entry
    id from the HTTP handler, through a detached process, to here -- is a parameter that
    can be wrong, and being wrong about which human act authorised a run is the one
    thing `assert_human_clocked` exists to prevent.

    Naming it explicitly matters: by the time a finish escalates it has written several
    entries of its own, so the *newest* entry is the finisher's, and a dispatch that
    defaulted to it would be refused as not human-clocked -- correctly, and uselessly.
    """
    from agentjobs.dispatch.guards import actor_kind

    for entry in reversed(task.log):
        actor = actor_kind(project_config, entry.actor)
        if actor is not None and actor.is_human:
            return entry.id
    return None


def dispatch_after_escalation(
    *,
    manager: TaskManager,
    project: Project,
    project_config: Dict[str, Any],
    task_id: str,
    home: Optional[Path],
    api_base: Optional[str],
) -> Optional[str]:
    """Start the session that takes over, when this machine allows an approval to.

    Gated on ``auto_dispatch``, which is the same switch and the same meaning it has
    everywhere else: *may a human's approval start a run without a second click*. It is
    not widened here. With it off -- which is this repository's own setting -- an
    escalation leaves the task at ``agent``/``work`` carrying a prompt that names the
    step that stopped, and the human's existing Dispatch click resumes the session that
    did the work. That is the pre-task-241 flow with a much better prompt in it, and it
    costs nothing to fall back to.

    Never raises. An escalation that has already been written to the record must not
    turn into a crash because a run could not start.
    """
    from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
    from agentjobs.dispatch.runner import DispatchRunError
    from agentjobs.models_v2 import DispatchTrigger

    try:
        resolution = assert_dispatch_permitted(project.id, home)
    except DispatchError:
        return None
    if not resolution.settings.auto_dispatch:
        return None

    task = manager.get_task(task_id)
    if task is None or not task.is_open:
        return None
    caused_by = newest_human_entry(task, project_config)
    if caused_by is None:
        return None

    try:
        handle = dispatch_task(
            manager=manager,
            project=project,
            project_config=project_config,
            request=DispatchRequest(
                task_id=task_id, caused_by=caused_by, trigger=DispatchTrigger.AUTO
            ),
            home=home,
            api_base=api_base,
        )
    except (DispatchError, DispatchRunError):
        return None
    return handle.run_id


# ----- starting one from a request that must not wait for it ------------------


def spawn_finish(
    *,
    project: Project,
    task_id: str,
    approver: str,
    home: Optional[Path] = None,
) -> Optional[str]:
    """Start a finish in a detached process, and return immediately.

    **Not a thread in the server, and that is not a style preference.** Step five of the
    sequence restarts the server. A finish running inside it would be killed by its own
    restart, half way through the one part of the job whose whole purpose is to verify
    that the restart worked -- leaving a merged branch, a live server and nobody to say
    so. A separate process outlives the thing it restarts.

    Returns the log path it will write, or None if the process could not be started.
    Never raises: an approval that has already been recorded must not fail because a
    convenience did not start.
    """
    import sys

    resolved_home = home or default_home()
    log_dir = finishes_root(resolved_home) / "spawn"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / f"{task_id}.log"
        handle = log.open("w", encoding="utf-8")
    except OSError:
        return None

    argv = [
        sys.executable,
        "-m",
        "agentjobs.cli",
        "finish",
        task_id,
        "--project",
        project.id,
        "--approver",
        approver,
    ]
    try:
        if os.name == "nt":
            subprocess.Popen(
                argv,
                cwd=str(project.root),
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            subprocess.Popen(
                argv,
                cwd=str(project.root),
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except (OSError, subprocess.SubprocessError):
        handle.close()
        return None
    return str(log)


def finish_is_offered(project_id: str, home: Optional[Path] = None) -> bool:
    """Whether this machine would let an approval of ``project_id`` finish itself.

    Asked by the approve route *before* spawning anything, so a machine with the feature
    off does not pay for a process that would immediately decline -- and, more to the
    point, so the route can fall through to ordinary auto-dispatch instead of leaving
    the approval with nothing at all behind it.
    """
    try:
        resolution = assert_dispatch_permitted(project_id, home)
    except DispatchError:
        return False
    return resolution.settings.finish.enabled
