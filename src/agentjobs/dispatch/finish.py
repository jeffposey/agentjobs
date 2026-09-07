"""Finishing an approved task without a model: the fixed half of the merge gate, steps 3-6.

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

**Since task-021 there is a second caller, and only one of the two is a person.** A
dispatched run at a posture whose merge policy is ``automatic`` runs this itself, with
``authority=POSTURE``, and no human reviews the branch. Every step is the same, in the
same order, for the same reasons -- which is the point of routing it here rather than
letting an agent run ``git merge``: the gate that authorises the merge is run *by this
module, on the rebased branch, unqualified*, so the merge does not rest on the agent's
own account of whether its work is sound. The authority is checked against the project's
machine-local posture before anything happens, and every record this writes says which
of the two authorities it ran under.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from agentjobs.actors import FINISHER
from agentjobs.dispatch.config import (
    DispatchError,
    FinishSettings,
    MergePolicy,
    Posture,
    PostureSource,
    ProjectDispatchSettings,
    ResolvedPosture,
    assert_dispatch_permitted,
    resolve_posture,
)
from agentjobs.dispatch.ledger import (
    KIND_FINISH,
    LedgerError,
    LockHolder,
    RunLock,
    RunLockTimeout,
    acquire_run_lock,
    acquire_runway_lock,
    find_run,
    live_runs,
    read_lock_holder,
    run_lock_path,
)
from agentjobs.dispatch.phases import RUN_ID_ENV, record_phase
from agentjobs.dispatch.record_commit import commit_task_record
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    BranchStatus,
    LogEntryType,
    Outcome,
    Task,
)
from agentjobs.projects import Project, default_home
from agentjobs.store_factory import TaskManagerLike

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

CATCH_UP_ROUNDS = 2
"""How many times a finish will rebase onto a moved base and re-verify before refusing.

Bounded rather than "until it stops moving", because the thing moving the base is other
sessions committing task records and there is no moment at which they are guaranteed to
stop. Two is enough for the observed case -- a record commit or three landing during one
gate -- and a third move means the machine is busier than a finish can usefully chase, at
which point saying so and waking somebody beats spinning.
"""

GATE_SCOPE_RELATIVE = ("scripts", "gate_scope.py")
"""Where a repository publishes what a changed path can reach, if it publishes it at all.

Read from *the repository being finished*, never from this package. Two consequences,
both wanted. A project with no such file gets the unconditional refusal this module has
always made -- the exemption is opt-in by the repository, in a file that goes through
review like any other. And the table cannot drift away from the one ``--since-gate``
uses, because it is the same file.
"""


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


class StepLog(List[StepResult]):
    """The steps so far, written to the finish record as each one lands.

    A list, because that is what every caller treats it as and what the two failure
    paths read back. The addition is that appending also writes a ``finish_step`` phase
    record -- which is deliberately not a separate call the sequence has to remember to
    make beside each ``append``. A step recorded in one place and not the other would
    make the live view disagree with the table on the task, and the sequence is written
    as one straight line precisely so nothing has to be remembered twice (task-321).

    Before this, the only account of which steps had run was the table written at the
    end, so a watcher could see that a finish had started and that it had ended, and
    nothing in between -- which for the three or four minutes of a real finish is
    everything.
    """

    def __init__(self, directory: Optional["FinishDirectory"] = None) -> None:
        super().__init__()
        self.directory = directory

    def append(self, step: StepResult) -> None:
        super().append(step)
        if self.directory is not None:
            self.directory.record(
                "finish_step",
                step=step.step,
                ok=step.ok,
                skipped=step.skipped,
                detail=step.detail,
                seconds=round(step.seconds, 2),
            )

    def extend(self, steps: Iterable[StepResult]) -> None:
        """Overridden because ``list.extend`` does not go through ``append``.

        A step added by the many-at-once form would otherwise be in the table at the end
        and absent from the live view, which is the exact disagreement the class exists
        to prevent -- and it would be silent. ``catch_up`` returns nought, one or two
        steps, so the plural form is a real caller and not a hypothetical one.
        """
        for step in steps:
            self.append(step)


FINISHED = "finished"
ESCALATED = "escalated"
DECLINED = "declined"

APPROVAL = "approval"
POSTURE = "posture"
"""What authorised this finish. ``APPROVAL`` is a person; ``POSTURE`` is task-021.

Two callers, two authorities, one sequence. The Approve button and a human at a shell
carry ``APPROVAL`` and nothing about them changed. A dispatched run whose posture
releases the merge gate carries ``POSTURE``, and is refused unless the project's
machine-local posture really does release it -- checked here, in code, rather than
trusted to the prompt that told the agent so.

That check is worth stating honestly about what it is and is not. It is not containment
against a *misbehaving* agent: ``autonomous`` is ``bypassPermissions``, and a run that
decided to ignore its instructions could run ``git merge`` itself with nothing in its
way. It is a guarantee about the **sanctioned** path -- that the command an agent is
told to run cannot merge anything at a posture that did not release it, so a prompt
that is wrong, stale, or copied from another project's run fails closed instead of
merging. Containment of the other kind was already given up when the posture was chosen.
"""


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
    escalation_dispatch: str = ""
    """Why an escalation started a run, or did not (task-340). ``EscalationDispatch.reason``.

    Empty on every path that did not escalate. Carried out of the finish rather than only
    written to the finish directory because the CLI's exit message is the one place a
    person running this by hand learns whether anything is now going to happen.
    """

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
    """A step could not be completed safely. A person or an agent takes it from here.

    ``frames`` is set only for a stop nobody modelled -- see :func:`_guarded_sequence`.
    Every other stop already knows where it is and says so in ``detail``; an unmodelled
    one knows only its own message, and without the frames placing it means re-deriving
    a call path from which step did not run (task-388).
    """

    def __init__(self, step: str, reason: str, detail: str, frames: str = "") -> None:
        super().__init__(detail)
        self.step = step
        self.reason = reason
        self.detail = detail
        self.frames = frames


# ----- subprocess plumbing ----------------------------------------------------


def authorisation_phrase(authority: str, approver: str, posture: str = "", source: str = "") -> str:
    """One sentence naming what made this merge legitimate. Never decorative.

    Every place the finish writes down a merge -- the merge message, the log entry, the
    close -- has to say which of the two authorities it ran under, because the sentence
    "a person approved this" is the whole difference between them and a reader six
    months later has no other way to tell. Rendered in one function so the three cannot
    drift into saying different things about the same merge.

    ``source`` names which of the three places the posture came from (task-315). It used
    to say "for this project" unconditionally, which stopped being true the moment a
    posture could be chosen for one dispatch: a reader looking up why an unreviewed merge
    happened would have gone to ``dispatch.yaml``, found `auto`, and concluded the record
    was lying to them.
    """
    if authority == POSTURE:
        whence = f" (from the {source})" if source else ""
        return (
            f"No human reviewed this merge: posture `{posture or 'autonomous'}`{whence} "
            f"releases the merge gate (task-021), and {approver} ran the finish. "
            "What authorised it is the gate -- `scripts/check.py` green on the rebased "
            "branch, run here rather than reported by the agent."
        )
    return f"Approved by {approver}; no model was in this loop."


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


STAGE_MARKER = "Failed at stage '"
"""What ``scripts/check.py`` prints when a stage goes red, verbatim.

Read rather than re-derived because the gate is the only thing that knows which of its
ten stages it was in, and it already says so in one unambiguous line. Recognising its
per-stage banners instead would be a second implementation of the gate's own bookkeeping,
free to disagree with it.
"""

FAILURE_PREFIXES = ("FAILED ", "ERROR ")
"""pytest's short-summary prefixes. One line per failing test, each naming it in full.

Deliberately not the ``E   `` traceback lines. A short-summary line already carries the
first line of the exception after ``- ``, which is the assertion in the overwhelmingly
common case, and it carries it *with* the test's own id -- which is the thing a reader
needs and a bare ``E   assert 3 == 4`` does not have.
"""

SALIENT_LIMIT = 12
"""How many failing tests to name before saying only that there are more.

A suite that goes red in fifty places is a different kind of problem from one that goes
red in two, and reaching this limit says which; naming all fifty would rebuild the wall
of output this exists to replace.
"""


def failing_stage(output: str) -> Optional[str]:
    """Which gate stage went red, taken from the gate's own sentence about it."""
    for line in reversed((output or "").splitlines()):
        start = line.find(STAGE_MARKER)
        if start == -1:
            continue
        rest = line[start + len(STAGE_MARKER) :]
        end = rest.find("'")
        if end > 0:
            return rest[:end]
    return None


def failing_tests(output: str, limit: int = SALIENT_LIMIT) -> List[str]:
    """pytest's short-summary lines, in order, without repeats.

    Deduplicated because a rerun or a second reporting section prints the same line
    again, and a list that names one test twice reads as two failures.
    """
    seen: List[str] = []
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line.startswith(FAILURE_PREFIXES):
            continue
        if line not in seen:
            seen.append(line)
        if len(seen) >= limit:
            break
    return seen


def lead_with_the_cause(output: str, *, log: Path) -> str:
    """The dispositive lines of a red gate first, then the pointer to the rest.

    **What this repairs is a reading failure** (task-340). Until now an escalation
    embedded ``tail(output, 30)``, and for a red pytest those thirty lines are thirty
    ``DeprecationWarning``s: the ``FAILED`` line naming the test sits above them, outside
    the window. Jeff's first question about the task-337 incident was "is it stuck?",
    which is what somebody asks when the record does not tell them what broke.

    So the stage and the failing tests go first, then the log path. **The raw tail is
    kept only when nothing could be extracted**, because that is the only case where it
    is the best available answer. Once a test has been named, thirty further lines of
    warnings are exactly the noise this removes, and the whole output is on disk.
    """
    stage = failing_stage(output)
    tests = failing_tests(output)
    pointer = "Full output: " + str(log)
    fence = "```" + chr(10) + tail(output, 30) + chr(10) + "```"
    if stage is None and not tests:
        return pointer + chr(10) * 2 + fence

    headline = f"It stopped in the `{stage}` stage." if stage else "It did not name a stage."
    if not tests:
        return headline + " " + pointer + chr(10) * 2 + fence

    more = ""
    if len(tests) >= SALIENT_LIMIT:
        more = chr(10) + f"... and possibly more; {log.name} has all of them."
    listed = chr(10).join(tests)
    return (
        headline
        + " What failed:"
        + chr(10) * 2
        + "```"
        + chr(10)
        + listed
        + more
        + chr(10)
        + "```"
        + chr(10) * 2
        + pointer
    )


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


def gate_scope_module(root: Path) -> Optional[Any]:
    """The repository's own ``scripts/gate_scope.py``, imported, or ``None``.

    Loaded by path rather than imported by name: it deliberately does not live in any
    package (``scripts/check.py`` says why), and the copy that matters is the one on the
    base being merged into, not whatever happens to be importable in this process.

    Every failure answers ``None``, and every caller treats ``None`` as "classify
    nothing". So a repository without the file, with an unreadable one, or with one that
    raises on import gets exactly the behaviour this module had before catching up
    existed.
    """
    path = root.joinpath(*GATE_SCOPE_RELATIVE)
    if not path.is_file():
        return None
    # A name of its own per call. It has to be in ``sys.modules`` while the body runs --
    # a module using ``from __future__ import annotations`` and ``@dataclass`` resolves
    # its own annotations through ``sys.modules`` and raises without it -- and it must not
    # still be there afterwards, because several projects can be finished in one process
    # and each is entitled to its own table.
    name = f"agentjobs_finish_gate_scope_{uuid.uuid4().hex}"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
    except Exception:
        return None
    return module if callable(getattr(module, "classify", None)) else None


def reachable_stages(root: Path, paths: Sequence[str]) -> Optional[List[str]]:
    """Which gate stages these changed paths can affect, or ``None`` for "all of them".

    The whole judgement in :func:`catch_up`, and none of it is made here -- it is
    delegated to the table ``--since-gate`` already runs on, for the reason task-297's
    constraints give: a second notion of what a path can reach would be a second thing to
    keep true, and the first one is already argued and audited in ``scripts/gate_scope``.

    ``None`` means *default-deny*, and it is returned for three situations that all
    deserve the same answer: no table, a base that moved without changing a single path
    (strange enough to be worth a person's eye rather than a rule), and -- the important
    one -- a path the table does not claim. ``gate_scope.classify`` returns ``None`` for
    an unclassified path precisely because an incomplete table must cost time rather than
    coverage; here that same ``None`` costs a merge rather than coverage, which is the
    same trade in the same direction.

    An empty *list* is a different answer and a legitimate one: paths that are all
    classified, to classes that reach no stage at all. No such class exists today, and
    the caller handles it rather than conflating it with the refusal.
    """
    if not paths:
        return None
    module = gate_scope_module(root)
    if module is None:
        return None
    stages: List[str] = []
    for path in paths:
        matched = module.classify(path)
        if matched is None:
            return None
        for name in matched.stages:
            if name not in stages:
                stages.append(name)
    return stages


# ----- the finish record ------------------------------------------------------


def finishes_root(home: Path) -> Path:
    return home / FINISHES_DIRNAME


SPAWN_DIRNAME = "spawn"
"""Where a spawned finish's stdout and its start marker go. One pair per task.

Not per finish: the two files answer "what is happening to this task", which is the
question the task page asks, and the newest spawn is the only one that can be the
answer. A finish's own directory keeps the per-attempt record.
"""


def spawn_root(home: Path) -> Path:
    return finishes_root(home) / SPAWN_DIRNAME


def spawn_log_path(home: Path, task_id: str) -> Path:
    """Where a spawned finish's stdout goes -- the step table, once it has ended."""
    return spawn_root(home) / f"{task_id}.log"


def spawn_marker_path(home: Path, task_id: str) -> Path:
    """Where the fact that a finish was started for this task is written down."""
    return spawn_root(home) / f"{task_id}.json"


def write_spawn_marker(home: Path, task_id: str, *, project_id: str, approver: str) -> None:
    """Record that a finish is being started for this task, before it is.

    Written by ``spawn_finish`` while the approve request is still open, which is the
    whole point -- see the ordering note there. Never raises: an approval that has
    already been recorded must not fail because a convenience could not be written.
    """
    payload = {
        "task_id": task_id,
        "project_id": project_id,
        "approver": approver,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        path = spawn_marker_path(home, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except (OSError, TypeError, ValueError):  # pragma: no cover - a marker is optional
        pass


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
            f"`scripts/check.py` went red on {plan.branch}. "
            f"{lead_with_the_cause(result.stdout + result.stderr, log=log)}\n\n"
            f"That took {seconds:.0f}s and exited {result.returncode}. A red gate never "
            f"merges. The branch is rebased onto {plan.base} and is otherwise untouched; "
            "nothing was merged.",
        )
    return seconds


def run_reduced_gate(
    plan: Plan,
    directory: FinishDirectory,
    settings: FinishSettings,
    stages: Sequence[str],
    round_number: int,
) -> float:
    """``scripts/check.py --only <stages>`` on the re-rebased branch. Not the gate.

    Deliberately the partial form ``run_gate``'s docstring forbids, and the distinction
    is worth stating rather than looking like an inconsistency. ``run_gate`` runs the
    unqualified gate because *nothing else has verified this branch*: a merge made on a
    partial run would be reporting a green the gate never gave. This runs after that, and
    the question it asks is much smaller -- **what did the base's own movement change,
    and can that reach anything?** The full gate's green still stands for the branch; the
    only thing without a green is the delta, and the delta is exactly what this runs.
    """
    started = time.monotonic()
    log = directory.path / f"catch-up-{round_number}.log"
    result = run_command(
        [str(plan.interpreter), "scripts/check.py", "--only", ",".join(stages)],
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
            "catch_up",
            "catch_up_gate_failed",
            f"`scripts/check.py --only {','.join(stages)}` went red on {plan.branch} "
            f"after rebasing onto the moved {plan.base}. "
            f"{lead_with_the_cause(result.stdout + result.stderr, log=log)}\n\n"
            f"That took {seconds:.0f}s and exited {result.returncode}. The full gate had "
            "been green; what the base moved under it was not. Nothing was merged. This "
            "is the case the catch-up exists to find, so read it before assuming it is "
            "noise.",
        )
    return seconds


def catch_up(plan: Plan, directory: FinishDirectory, settings: FinishSettings) -> List[StepResult]:
    """Absorb a base that moved during the gate, when what moved it can be re-verified.

    **The problem this solves is structural, not incidental** (task-297). ENGINEERING.md
    requires every session to commit its task records to the base branch, so the base
    moves every couple of minutes whenever anything is happening; a full gate takes
    minutes. Under load ``merge``'s ``base_moved`` check therefore fired on almost every
    finish, each refusal costing a whole dispatched run, and the refusals were of commits
    that were nothing but other people's bookkeeping.

    **The answer is not to declare that bookkeeping inert.** ``tasks/`` genuinely can turn
    the suite red -- ``tests/test_validate.py::TestRealCorpus`` reads the corpus of the
    checkout it runs in, and the gate ran in the branch's worktree, whose corpus is the
    pre-move one. So the delta really is unverified, and this verifies it: rebase onto
    the new tip and re-run precisely the stages :func:`reachable_stages` says the moved
    paths can affect. A ``tasks/``-only move costs one ``pytest`` instead of one wasted
    run, and ``TestRealCorpus`` is in it.

    **A code commit still refuses**, with the message it always had. This returns having
    done nothing when the move cannot be classified, and ``merge`` -- which re-reads the
    base itself -- raises ``base_moved`` a moment later. The refusal is not weakened; it
    is narrowed to the case it was written for.

    Nothing here writes to the task record, though every other step in this module does.
    A progress note would be committed to the base, which would move the base, which is
    the very thing being caught up with. The step table in the closing entry carries it,
    and so does the finish directory.
    """
    steps: List[StepResult] = []
    for round_number in range(1, CATCH_UP_ROUNDS + 1):
        base_now = git_out(plan.root, ["rev-parse", plan.base])
        if base_now == plan.base_head_before:
            # Reported rather than omitted, like a merge that needs no restart. A step
            # that vanishes when it does nothing makes the live view guess what is
            # happening next, and the common case -- a quiet base -- is the one worth
            # being explicit about.
            if not steps:
                steps.append(
                    StepResult(
                        "catch_up",
                        True,
                        f"{plan.base} did not move during the gate",
                        0.0,
                        skipped=True,
                    )
                )
            return steps
        moved = changed_between(plan.root, plan.base_head_before, base_now)
        stages = reachable_stages(plan.root, moved)
        if stages is None:
            directory.record(
                "finish_catch_up_declined",
                round=round_number,
                base=base_now,
                paths=moved,
            )
            steps.append(
                StepResult(
                    "catch_up",
                    True,
                    f"{plan.base} moved to {base_now[:8]} in {len(moved)} path(s) that "
                    "nothing classifies as reachable-from, so this is not absorbable "
                    "and the merge will refuse it",
                    0.0,
                    skipped=True,
                )
            )
            return steps

        began = time.monotonic()
        from_base = plan.base_head_before
        plan.base_head_before = base_now
        # Re-read before rebasing, so a conflict reports whether *this* rebase left the
        # branch where it found it rather than comparing against preflight's reading.
        plan.branch_head_before = git_out(plan.root, ["rev-parse", plan.branch])
        rebased = rebase(plan)
        seconds = (
            run_reduced_gate(plan, directory, settings, stages, round_number) if stages else 0.0
        )
        directory.record(
            "finish_catch_up",
            round=round_number,
            base_from=from_base,
            base_to=base_now,
            paths=moved,
            stages=list(stages),
            seconds=round(seconds, 2),
        )
        steps.append(
            StepResult(
                "catch_up",
                True,
                f"{plan.base} moved {from_base[:8]} -> {base_now[:8]} in "
                f"{len(moved)} classified path(s); rebased to {rebased[:8]} and re-ran "
                f"{', '.join(stages) if stages else 'nothing those paths can reach'}",
                time.monotonic() - began,
            )
        )

    base_now = git_out(plan.root, ["rev-parse", plan.base])
    if base_now != plan.base_head_before:
        raise Escalate(
            "catch_up",
            "base_moves_repeatedly",
            f"{plan.base} moved again after {CATCH_UP_ROUNDS} catch-up rounds (now "
            f"{base_now[:8]}). Each round rebased and re-verified what the move could "
            "reach, and each time something else landed before the merge. Nothing was "
            "merged. The machine is busier than a finish can chase: retry it when the "
            "base is quieter, or find out what is committing to it every few seconds.",
        )
    return steps


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


def merge(plan: Plan, task: Task, authorisation: str) -> str:
    """``git merge --no-ff`` into the base, in the shared clone. The irreversible step.

    Two things are checked first that git would otherwise turn into a mess rather than a
    refusal: that nothing dirty in the clone is also in the merge, and that the base has
    not moved since preflight read it. The second is the race a rebase cannot close --
    somebody else merging between the gate and this -- and the answer to it is to
    escalate, not to rebase again in a loop.

    Since task-297 the second check is *narrower than it looks*, and the narrowing happens
    before this is called rather than here. :func:`catch_up` has already rebased onto the
    moved base and re-verified it if what moved it was classifiable -- other sessions'
    task records, prose -- updating ``plan.base_head_before`` when it did. So a base that
    is still wrong by the time this reads it moved in a way nothing could re-verify
    cheaply, which is what this refusal was always for.

    A merge that moves nothing is the third case, and it is not a success. ``git merge
    --no-ff`` of a branch already contained in the base prints "Already up to date" and
    exits **zero**, so taking the exit code at face value would close a task on the
    strength of a merge that did not happen. Which of the two meanings it has is decided
    from the task's own record rather than guessed at.
    """
    base_now = git_out(plan.root, ["rev-parse", plan.base])
    if base_now != plan.base_head_before:
        moved = changed_between(plan.root, plan.base_head_before, base_now)
        listed = ", ".join(moved[:6]) + (" and more" if len(moved) > 6 else "")
        raise Escalate(
            "merge",
            "base_moved",
            f"{plan.base} moved from {plan.base_head_before[:8]} to {base_now[:8]} while "
            f"the gate was running, so what was verified is no longer what would be "
            "merged. The branch is rebased onto the older base and nothing was merged.\n\n"
            f"It moved in {len(moved)} path(s): {listed or '(none reported)'}. The "
            "catch-up step re-verifies a move it can classify and rebases onto it, so "
            "reaching this means at least one of those paths could affect any stage of "
            "the gate -- code, in other words, and this refusal is the one it was "
            "written for. Rerun the finish; it will gate against the new base.",
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
        f"Merged by the AgentJobs finisher after rebasing onto {plan.base} and running "
        f"scripts/check.py green in that branch's worktree (task-241).\n\n"
        f"{authorisation}"
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


def delete_branch(plan: Plan) -> StepResult:
    """Delete the merged branch -- the half of ENGINEERING.md step 5 nothing implemented.

    The rule has been written down in three places since the repository had three
    contract files, and until task-293 no code ran it: every task the finisher merged
    left its branch behind by construction. That is harmless one at a time, which is
    exactly why six accumulated in a single night before anybody noticed. The cost is
    that ``git branch --list`` stops reading as a statement of what is in flight.

    **``-d``, never ``-D``.** ``-d`` refuses a branch the base does not contain, and that
    refusal is the whole safety argument for doing this unattended: if git will not
    delete it, the assumption that this branch was merged is wrong, and forcing past a
    refusal would destroy the one case worth keeping.

    **The ordering is a real constraint, not a preference.** A branch checked out in a
    worktree cannot be deleted, so this runs after ``remove_worktree`` -- and it asks git
    whether the worktree is really gone rather than assuming the previous step worked.
    Without that check a failed worktree removal would come back here as a refusal
    phrased in terms of checkout, which reads exactly like the unmerged case and means
    something completely different.

    **A refusal is reported, not escalated**, which is the same trade ``remove_worktree``
    makes and for the same reason. By the time this runs the merge has landed, the
    delivery has been verified and the task is closed; waking a session to delete a ref
    would cost more than the ref does. It is written into the step table instead, which
    is what makes it findable.
    """
    holder = worktree_paths(plan.root).get(plan.branch)
    if holder is not None:
        return StepResult(
            "branch",
            True,
            f"{plan.branch} is still checked out at {holder}, so it cannot be deleted "
            "-- left in place",
            0.0,
        )
    result = git(plan.root, ["branch", "-d", plan.branch])
    if result.returncode != 0:
        return StepResult(
            "branch",
            True,
            f"`git branch -d {plan.branch}` refused: {tail(result.stderr, 3)} "
            "-- left in place, never forced",
            0.0,
        )
    return StepResult("branch", True, f"deleted {plan.branch}", 0.0)


# ----- writing it down --------------------------------------------------------


def mark_branch_merged(manager: TaskManagerLike, task_id: str, branch: str) -> None:
    """Set this branch ``merged`` in ``branches[]``, leaving every other entry alone.

    Re-reads the task rather than patching the copy preflight was given. Several log
    entries have been appended since then, and a patch computed from a stale record is
    how a concurrent write gets silently discarded.

    **The patch is built in JSON terms, not Python ones** (task-388). ``manager`` here is
    a :class:`~agentjobs.remote_manager.RemoteTaskManager` whenever the project is on
    SQLite, so this dict is about to become an HTTP request body -- and a
    ``datetime.datetime`` in it is a ``TypeError`` at ``httpx``'s encoder, thrown after
    the merge, the rebuild and the restart have all happened. ``mode="json"`` rather than
    ``mode="python"``, and an ISO string rather than a ``datetime``, for the same reason
    the status has always been written as ``.value``: the wire is the destination.
    """
    current = manager.get_task(task_id)
    if current is None:  # pragma: no cover - the task was read moments ago
        return
    branches: List[Dict[str, Any]] = []
    for entry in current.branches:
        item = entry.model_dump(mode="json")
        if entry.name == branch:
            item["status"] = BranchStatus.MERGED.value
            item["merged_at"] = datetime.now(timezone.utc).isoformat()
        branches.append(item)
    manager.update_task(task_id, actor=FINISHER, branches=branches)


@dataclass
class Runway:
    """This repository's one landing strip, held across rebase, gate and merge.

    **Takeoff and landing are different resources** (task-223). An epic's children work in
    parallel because their worktrees are independent; they merge one at a time because
    ``main`` is not. Without this, N concurrent finishers each rebase, gate and merge
    against a base the others are moving, and the property the merge gate exists to
    guarantee -- *the commit that landed is the commit the gate verified* -- stops
    holding. ``merge``'s ``base_moved`` check is what catches that, and it costs a whole
    dispatched run each time it fires; under concurrency it would fire routinely.

    Held from just after preflight until the finish is over, which is longer than the
    merge and deliberately so. See :func:`agentjobs.dispatch.ledger.acquire_runway_lock`
    for the arithmetic and for why taking it at merge time only was rejected.

    A finish that never gets as far as preflight -- no branch, a branch that does not
    exist -- never takes it. Declining is not landing.
    """

    home: Path
    root: Path
    finish_id: str
    timeout: float
    lock: Optional[RunLock] = None
    waited_seconds: float = 0.0

    def take(self, manager: TaskManagerLike, task_id: str) -> StepResult:
        """Queue for the runway, saying so on the record if the queue is real."""
        began = time.monotonic()

        def announce(holder: LockHolder) -> None:
            # Only when it is actually contended, and only once. A note on every finish
            # would be noise; a task that sits for ten minutes with no explanation is the
            # thing this prevents -- a child queued behind three others must not read as
            # hung to whoever opens it.
            manager.add_log_entry(
                task_id,
                actor=FINISHER,
                type=LogEntryType.PROGRESS,
                body=(
                    f"Queued for this repository's merge runway, held by "
                    f"{holder.describe()}{holder.since_phrase()}.\n\n"
                    "**This is the queue working, not a stall.** Children of an epic fly "
                    "in parallel and land one at a time, because a gate is only evidence "
                    "about the base it ran against. Nothing here is rebased, gated or "
                    "merged until the runway is free."
                ),
                data={"finish_step": "runway_queued", "finish_id": self.finish_id},
            )
            # Written, and deliberately **not committed** (task-297). This is the one
            # thing the finisher does while *another* finish holds the runway -- and that
            # other finish is very likely mid-gate, where a commit to the base is what
            # escalates it with `base_moved`. The note is on disk, which is what the
            # dashboard reads; the commit it would have made lands a minute later anyway,
            # from `announce_start` on this same file once the runway comes free, or from
            # the escalation if the wait times out. `commit_task_record` commits one path,
            # and that path is this record, so nothing is orphaned by dropping this call.

        try:
            self.lock = acquire_runway_lock(
                self.home,
                self.root,
                finish_id=self.finish_id,
                timeout=self.timeout,
                on_wait=announce,
            )
        except RunLockTimeout as exc:
            raise Escalate("runway", "runway_busy", str(exc)) from exc
        self.waited_seconds = time.monotonic() - began
        held = "taken immediately" if self.waited_seconds < 1.0 else "taken after queuing"
        return StepResult("runway", True, held, self.waited_seconds)

    def release(self) -> None:
        if self.lock is not None:
            self.lock.release()
            self.lock = None


def announce_start(
    manager: TaskManagerLike, task_id: str, plan: Plan, directory: FinishDirectory
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
    manager: TaskManagerLike, task_id: str, plan: Plan, merge_commit: str, authorisation: str
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
            f"{plan.worktree} before the merge. {authorisation} Delivery -- rebuild, "
            "restart, verification -- comes next, and this task stays open until it is "
            "verified."
        ),
        data={
            "finish_step": "merge",
            "merge_commit": merge_commit,
            "branch": plan.branch,
            "base": plan.base,
        },
    )


def where_the_work_is(task: Task, root: Path) -> str:
    """One sentence naming the branch and the worktree this task's work lives in.

    Written for the session that is dispatched *after* the escalation (task-340). A woken
    session already knows both and is told below not to trust that memory; a cold one is
    handed ``PROMPT_STUB``, which tells every agent to take a fresh worktree -- correct
    for a task being started and wrong for one that already has a branch in flight. The
    record is the only place that difference can be stated, so it is stated here rather
    than by making the generic prompt conditional on something it cannot see.

    Read from git and the task's own ``branches[]``, never guessed from the naming
    convention: a worktree somewhere unexpected is exactly the case where a guess sends
    an agent to the wrong directory.
    """
    branches = active_branches(task)
    if not branches:
        return "This task's record names no active branch."
    located = worktree_paths(root)
    described = []
    for name in branches:
        path = located.get(name)
        described.append(f"`{name}` in {path}" if path else f"`{name}` (no worktree checked out)")
    return "The work is on " + ", ".join(described) + ". Use it; do not take a new one."


def escalate_on_record(
    manager: TaskManagerLike,
    task_id: str,
    failure: Escalate,
    steps: Sequence[StepResult],
    merge_commit: Optional[str],
    root: Optional[Path] = None,
) -> None:
    """Say exactly how far the finish got, then hand the ball to the agent.

    The prompt is written for a session that may be *resumed* -- it remembers its branch
    and its worktree and is confident about both -- so it leads with what changed
    underneath that memory rather than with a request.

    **The stop leads, on both surfaces** (task-340). ``failure.detail`` now begins with
    the dispositive lines -- for a red gate, the stage and the failing tests -- so putting
    it second on a two-line preamble puts the cause inside the first screen of the
    dashboard. The step table goes last: it is evidence for a reader who is already
    oriented, and it was previously between the reader and the answer.
    """
    account = "\n".join(step.render() for step in steps)
    merged = (
        f"**The merge is done: `{merge_commit[:8]}`.**"
        if merge_commit
        else "**Nothing was merged.**"
    )
    # Only an unmodelled stop carries frames, and when it does they go here rather than
    # into the prompt: this entry is what a reader opens to place the failure, and the
    # prompt is what an agent is woken with (task-388).
    where = f"\n\nIt was raised here:\n\n```\n{failure.frames}\n```" if failure.frames else ""
    body = (
        f"The scripted finish stopped at `{failure.step}` ({failure.reason}). {merged}\n\n"
        f"{failure.detail}{where}\n\nEverything it did get through:\n\n```\n{account}\n```"
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
            **({"traceback": failure.frames} if failure.frames else {}),
        },
    )
    task = manager.get_task(task_id)
    whereabouts = ""
    if task is not None and root is not None:
        whereabouts = "\n\n" + where_the_work_is(task, root)
    manager.handoff(
        task_id,
        actor=FINISHER,
        ball=Ball.AGENT,
        ball_reason=BallReason.WORK,
        ball_prompt=(
            f"The approval ran the scripted finish and it stopped at `{failure.step}`. "
            f"{merged}\n\n{failure.detail}\n\n"
            "Take it from here. Check the tree against what is written above "
            "before acting on anything you remember: if your worktree, your branch or "
            "your account of this task no longer matches what is on disk, say so on the "
            "record and hand the ball back rather than improvising a recovery."
            f"{whereabouts}"
        ),
    )


# ----- the orchestrator -------------------------------------------------------


def released_posture(
    *,
    settings: ProjectDispatchSettings,
    task_id: str,
    task_posture: Optional[Posture],
    home: Path,
    run_id: str = "",
) -> ResolvedPosture:
    """Which posture decides this merge, and where it came from (task-315).

    **The rule: the posture the run was dispatched at; with no run, the posture a
    dispatch would resolve right now.** Both clamped by this machine's ceiling.

    Until task-315 this was `settings.posture` and nothing else -- the project default,
    which is the *least* specific of the three sources ``resolve_posture`` weighs. So a
    posture chosen for one dispatch (task-307's pulldown, or ``--posture``) reached the
    agent's prompt, told it the merge gate was released, and was then contradicted by the
    command that prompt named. Observed on task-298: dispatched ``autonomous``, refused
    ``posture_requires_review``, ball parked on a human who had chosen not to be one.

    Two sources, in this order, and the order is the point:

    1. **The run's own record**, when ``run_id`` names one for *this task*. That posture
       was resolved once, at dispatch, from the human's choice, and written down with the
       source that won -- so it is read back rather than re-derived. Re-deriving would be
       a second implementation of ``resolve_posture``'s precedence, free to disagree with
       the first, and it could not see a ``requested`` value that only ever existed as an
       argument to that one call.
    2. **``resolve_posture``**, for a person running the command from a shell. That
       honours a ``posture:`` field on the task record and the project default, in the
       same precedence a dispatch of this task would apply.

    **The ceiling is re-applied here, from the config as it stands now.** Not the
    ``posture_ceiling`` the run recorded -- that is history, and the useful question is
    what this machine permits today, which is lower if somebody has since lowered it. It
    is also what keeps this honest: a run record under ``~/.agentjobs/runs/`` is no
    harder for an agent to edit than a task record is, and ``max_posture`` in
    machine-local ``dispatch.yaml`` is the one line in the system that nothing reachable
    over the network writes. An edited run record buys at most the ceiling.

    Nothing here reads an argument the *caller* chose. ``--posture-release`` says which
    authority is being claimed; it never says that the claim is granted.
    """
    ceiling = settings.ceiling
    if run_id:
        record = None
        try:
            record = find_run(home, run_id)
        except LedgerError:
            record = None
        # A run may only vouch for the task it was dispatched against. Without this, a
        # run at `autonomous` could name any other open task on the command line and
        # merge its branch on a posture nobody granted for it.
        if record is not None and record.task_id and record.task_id != task_id:
            record = None
        if record is not None and record.posture:
            try:
                dispatched = Posture(record.posture)
            except ValueError:
                dispatched = None  # a meta written by hand; fall through to re-resolving
            if dispatched is not None:
                try:
                    source = PostureSource(record.posture_source)
                except ValueError:
                    # Recorded before task-308, or unparseable. The posture is still a
                    # fact; only the account of where it came from is missing.
                    source = PostureSource.DISPATCH
                if dispatched.within(ceiling):
                    return ResolvedPosture(posture=dispatched, source=source, ceiling=ceiling)
                return ResolvedPosture(
                    posture=ceiling, source=source, ceiling=ceiling, requested=dispatched
                )
    return resolve_posture(settings, task=task_posture)


def finish_task(
    *,
    manager: TaskManagerLike,
    project: Project,
    task_id: str,
    approver: str,
    home: Optional[Path] = None,
    api_base: Optional[str] = None,
    settings: Optional[FinishSettings] = None,
    authority: str = APPROVAL,
) -> FinishResult:
    """Do the fixed part of ENGINEERING.md merge-gate steps 3 to 6, or stop and say where.

    Takes the task's run lock for the whole attempt, so a dispatch cannot start a
    session into a tree this is rebasing, and two approvals cannot merge the same branch
    twice. It is the same lock a run takes, which is the point: the two are alternatives.

    ``authority`` says what makes the merge legitimate, and defaults to the only thing
    that ever made one before task-021: a person approved it. ``POSTURE`` is the other
    one, and it is checked rather than believed -- the project's machine-local posture
    has to actually release the merge gate, or this declines without touching anything.
    """
    resolved_home = home or default_home()
    posture_name = ""
    posture_source = ""
    if authority == POSTURE:
        try:
            released = assert_dispatch_permitted(project.id, resolved_home)
        except DispatchError as exc:
            return FinishResult(
                task_id=task_id,
                outcome=DECLINED,
                reason=getattr(exc, "reason", "dispatch_error"),
                detail=str(exc),
            )
        # The posture *this run* was authorised at, not the project's default. See
        # `released_posture`; before task-315 this line read `released.settings.posture`
        # and a dispatch-time choice never reached the merge.
        candidate = manager.get_task(task_id)
        merge_posture = released_posture(
            settings=released.settings,
            task_id=task_id,
            task_posture=(
                Posture(candidate.posture.value)
                if candidate is not None and candidate.posture is not None
                else None
            ),
            home=resolved_home,
            # Not `os.environ[RUN_ID_ENV]` directly: a `--bg` session can come up holding
            # another run's identity, and that stripped task-316 of the posture a human
            # had granted it. See `own_run_id` (task-249).
            run_id=own_run_id(resolved_home, task_id),
        )
        posture = merge_posture.posture
        posture_name = posture.value
        posture_source = merge_posture.source.value
        if posture.merge_policy is not MergePolicy.AUTOMATIC:
            clamped = (
                f" It asked for `{merge_posture.requested.value}` and this machine caps "
                f"{project.id} at `{merge_posture.ceiling.value}` "
                f"(`projects.{project.id}.max_posture`)."
                if merge_posture.requested is not None
                else ""
            )
            return FinishResult(
                task_id=task_id,
                outcome=DECLINED,
                reason="posture_requires_review",
                detail=(
                    f"This finish runs at {merge_posture.describe()}, whose merge policy is "
                    f"`{posture.merge_policy.value}`. Only `autonomous` releases the merge "
                    f"gate.{clamped} Hand the ball to human/review and stop; nothing was "
                    "touched."
                ),
            )
        if settings is None:
            settings = released.settings.finish
            api_base = api_base or released.config.api_base

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

    lock: Optional[RunLock] = None
    if not _own_run_holds_lock(resolved_home, task_id):
        try:
            lock = acquire_run_lock(resolved_home, task_id, kind=KIND_FINISH)
        except RunLockTimeout as exc:
            return FinishResult(task_id=task_id, outcome=DECLINED, reason="locked", detail=str(exc))

    directory = FinishDirectory.create(resolved_home, task_id, project.id)
    if lock is not None:
        # The lock is taken before the directory exists -- taking it is what decides
        # whether this attempt happens at all -- so the finish id is written a moment
        # later, exactly as a dispatch adopts its run id. Until this line, anyone refused
        # can be told a finish holds the task but not which one (task-298).
        lock.adopt_finish(directory.finish_id)
    started = time.monotonic()
    steps: List[StepResult] = StepLog(directory)
    runway = Runway(
        home=resolved_home,
        root=project.root,
        finish_id=directory.finish_id,
        timeout=float(settings.runway_timeout_seconds),
    )
    try:
        result = _guarded_sequence(
            manager=manager,
            project=project,
            task=task,
            authorisation=authorisation_phrase(authority, approver, posture_name, posture_source),
            settings=settings,
            api_base=api_base,
            directory=directory,
            steps=steps,
            runway=runway,
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
        escalate_on_record(manager, task_id, exc, steps, merge_commit, root=project.root)
        commit_task_record(
            manager,
            task_id,
            subject=f"escalate the scripted finish at {exc.step}",
            actor=FINISHER,
        )
        # The lock is released before dispatching: the run this may start takes the same
        # per-task lock, and holding it while asking for it would refuse every
        # escalation on this machine for the reason "an escalation is in progress".
        #
        # `lock` is None when this finish is a run finishing *itself* (task-022) -- the
        # lock is that run's and stays its until the run ends. An escalation from there
        # cannot start a second run for the same task either way, and it does not need
        # to: the run asking is still alive and holds the ball.
        if lock is not None:
            lock.release()
        # And the runway, for the same reason and one more: a session dispatched to fix
        # whatever stopped this finish will want to merge, and holding the strip while
        # asking somebody to land on it is a deadlock with a one-hour timeout on it.
        runway.release()
        taken_over = dispatch_after_escalation(
            manager=manager,
            project=project,
            project_config=project.load_config(),
            settings=settings,
            task_id=task_id,
            home=resolved_home,
            api_base=api_base,
        )
        # The one thing that must not survive this handler: an open task whose ball says
        # `agent` with no agent anywhere (task-340). `escalate_on_record` wrote that ball
        # a moment ago, correctly -- it is what a woken session is handed -- and this is
        # where it is taken back if nothing was woken.
        if taken_over.unattended:
            park_for_human(manager, task_id, project.id, taken_over)
            commit_task_record(
                manager,
                task_id,
                subject=f"hand {task_id} to a human: the escalation started no run",
                actor=FINISHER,
            )
        directory.write_meta(
            dispatched_run_id=taken_over.run_id,
            escalation_dispatch=taken_over.reason,
        )
        return FinishResult(
            task_id=task_id,
            outcome=ESCALATED,
            reason=exc.reason,
            detail=exc.detail,
            steps=steps,
            finish_id=directory.finish_id,
            directory=directory.path,
            merge_commit=merge_commit,
            dispatched_run_id=taken_over.run_id,
            escalation_dispatch=taken_over.reason,
        )
    finally:
        runway.release()
        if lock is not None:
            lock.release()


def _run_vouching_for(home: Path, run_id: str, task_id: str) -> bool:
    """Whether ``run_id`` is a live run that was dispatched against ``task_id``.

    Both halves matter. A run that has finished is not the one calling this, and a run
    dispatched against another task may not speak for this one -- that second rule is
    ``released_posture``'s own guard, applied here so the two cannot disagree.
    """
    if not run_id:
        return False
    try:
        record = find_run(home, run_id)
    except LedgerError:
        return False
    return record.is_live and bool(record.task_id) and record.task_id == task_id


def _is_a_run_at_all(home: Path, run_id: str) -> bool:
    """Whether the ledger has ever heard of ``run_id``, whatever became of it.

    Deliberately weaker than ``_run_vouching_for``: a leaked identity is a *finished* run
    against *another* task, so nothing about being current or relevant can be required
    here. All this asks is whether dispatch created the run, which is the difference
    between a stale value and an invented one.
    """
    try:
        find_run(home, run_id)
    except LedgerError:
        return False
    return True


def own_run_id(home: Path, task_id: str, *, environ: Optional[Mapping[str, str]] = None) -> str:
    """Which run this process actually belongs to, when the environment may be lying.

    ``AGENTJOBS_RUN_ID`` is set on the launcher, and for a ``--bg`` run the launcher is
    not the worker: a persistent daemon spawns the worker from its own environment, so a
    session can come up holding the identity of whichever run started that daemon. See
    ``dispatch.session_env``, which stops that happening for runs dispatched from now on.
    This is what protects the ones that still arrive wrong.

    **The failure this repairs is not a lost measurement.** ``run_68ea396e`` was
    dispatched against task-316 at ``autonomous`` and came up holding ``run_12b2675c`` --
    the task-269 supervisor, fourteen hours earlier. Both consumers below then read the
    wrong run: ``released_posture`` correctly refused a record naming another task and
    fell through to the project default, so the run was told a human must review work a
    human had already released; and the lock check compared against a run that holds no
    lock, so the next stop would have been ``locked``. Two individually correct guards,
    one wrong input.

    The repair is deliberately narrow, and each condition is load-bearing:

    - **It fires only when the variable is set and cannot vouch for this task.** An
      absent variable means nothing dispatched this process -- a person at a shell --
      and their behaviour is unchanged. Only the exact leak signature is treated.
    - **The declared value has to name a run the ledger has heard of** (task-318). A
      leaked identity is always a real one: it is whichever run started the daemon, and
      that run has a directory. A value naming no run is not the leak signature, so it is
      left alone. Without this, typing a nonsense value bought strictly more than setting
      nothing did -- the ordinary refusals for an empty variable, skipped -- and
      ``_own_run_holds_lock`` below could no longer keep the promise it makes in as many
      words, that *a process that invents the variable matches no lock and gets the
      ordinary refusal*. Two of the three conditions here were added by task-249 and one
      by task-318, and no one of them is sufficient alone.
    - **The replacement comes from the task's run lock, not from a search.** The lock
      file was written by the dispatch that started the run and names it; nothing a
      caller controls forges one. Guessing from ``live_runs`` was the original plan and
      is worse: it has to pick, and at several points on 2026-08-25 there were two
      candidates.
    - **The lock holder still has to be a live run for this task**, or the declared
      value is returned unchanged and the existing refusals stand.

    Nothing here can widen an envelope. ``released_posture`` re-applies the machine's
    ceiling to whatever this returns, so the worst a wrong answer buys is the posture a
    human already granted the run that holds this task's lock.
    """
    source = os.environ if environ is None else environ
    declared = (source.get(RUN_ID_ENV) or "").strip()
    if not declared or _run_vouching_for(home, declared, task_id):
        return declared
    if not _is_a_run_at_all(home, declared):
        return declared
    holder = read_lock_holder(run_lock_path(home, task_id))
    if holder is None or not _run_vouching_for(home, holder.run_id, task_id):
        return declared
    return holder.run_id


def _own_run_holds_lock(home: Path, task_id: str) -> bool:
    """Whether the lock on this task is held by *the run calling this* (task-022).

    The autonomous merge path is a run being told, in its own prompt, to run
    ``agentjobs finish <its own task> --posture-release``. That run holds this task's
    run lock for its whole lifetime -- deliberately, because the lock is what stops a
    *second* run being started against a task somebody is already working. Asking for it
    again from inside is not contention; it is the same run, and treating it as
    contention made the sanctioned autonomous merge unreachable. Found 2026-08-24 on the
    first end-to-end walk: every child got as far as a green gate and a commit, and every
    one of them was declined ``locked`` by the command its prompt named.

    Two things establish identity and both have to hold. ``AGENTJOBS_RUN_ID`` is in the
    environment because dispatch put it there, so only something dispatch started can
    claim to be a run at all; and the lock file has to *name that run*, which only the
    dispatch that took it could have written. A process that invents the variable matches
    no lock and gets the ordinary refusal.

    Nothing is released on this path. The lock belongs to the run, not to the finish, and
    the run's own supervisor releases it when the run ends. A finish that released it
    would free the task while its agent was still executing, which is the state the lock
    exists to make impossible.
    """
    own_run = own_run_id(home, task_id)
    if not own_run:
        return False
    holder = read_lock_holder(run_lock_path(home, task_id))
    return holder is not None and holder.run_id == own_run


def _merge_commit_of(steps: Sequence[StepResult]) -> Optional[str]:
    """The merge commit, if the merge step got as far as recording one."""
    for step in steps:
        if step.step == "merge" and step.ok:
            return step.detail.split()[-1]
    return None


def where_it_was_raised(error: BaseException, limit: int = 8) -> str:
    """The tail of ``error``'s traceback, as a reader of the task record can use it.

    An ``unexpected`` stop used to reach the record as a type and a message, and nothing
    else. Task-388 is what that costs: ``TypeError: Object of type datetime is not JSON
    serializable`` names no field, no file and no line, so placing it meant reasoning
    backwards from which step had *not* run, then re-deriving a call path by hand. The
    frames were sitting in the spawn log the whole time; they simply never got onto the
    task, which is the only artefact the next reader is guaranteed to have.

    The last few frames rather than the whole stack, because the top of a finish's stack
    is always the same orchestration and the bottom is always where the answer is. Paths
    are cut at the package root: ``src/agentjobs/...`` places a frame in this repository
    exactly as well as an absolute path does, and this record has a public remote.
    """
    rendered: List[str] = []
    for frame in traceback.extract_tb(error.__traceback__)[-limit:]:
        rendered.append(f"{_placeable_path(frame.filename)}:{frame.lineno} in {frame.name}")
    return "\n".join(rendered)


def _placeable_path(filename: str) -> str:
    """``filename`` cut down to the part that places it, without naming a home directory."""
    parts = Path(filename).as_posix().split("/")
    for marker in ("src", "site-packages", "tests", "scripts"):
        if marker in parts:
            return "/".join(parts[parts.index(marker) :])
    return parts[-1]


def _guarded_sequence(**kwargs: Any) -> FinishResult:
    """``_sequence``, with every unanticipated failure turned into an escalation.

    A traceback out of here would be the exact failure this task must not introduce: a
    merge that happened, a record that does not say so, and a process that died before
    it could hand the ball to anybody. So anything the sequence does not model becomes
    a stop like any other -- named ``unexpected``, carrying the exception verbatim,
    landing on the record with the steps that did complete. An ugly escalation is worth
    a great deal more than a silent one.

    **With the frames, since task-388.** They ride on ``Escalate.frames`` rather than in
    the detail, because the detail is also the ``ball_prompt`` an agent is woken with and
    that has to stay short. The frames belong on the log entry, which is where a reader
    goes when the one-line message is not enough -- and for an unmodelled error it never
    is: "how far did it get" and "where did it throw" are different questions, and the
    step table only ever answered the first.
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
            frames=where_it_was_raised(unexpected),
        ) from unexpected


def _sequence(
    *,
    manager: TaskManagerLike,
    project: Project,
    task: Task,
    authorisation: str,
    settings: FinishSettings,
    api_base: Optional[str],
    directory: FinishDirectory,
    steps: List[StepResult],
    runway: Runway,
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
    # After preflight so a decline never queues, and before everything else so that the
    # base this rebases onto, gates against and merges into is one that cannot move
    # underneath it (task-223). Everything from here to the end of the finish is inside
    # the runway.
    steps.append(runway.take(manager, task.id))
    directory.record("finish_runway", seconds=round(runway.waited_seconds, 2))
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

    # The gate takes minutes and the base moves every couple of them, so by here it very
    # often has. This absorbs the moves it can re-verify and leaves the rest to `merge`,
    # which refuses them exactly as it always did (task-297).
    steps.extend(catch_up(plan, directory, settings))

    began = time.monotonic()
    merge_commit = merge(plan, task, authorisation)
    steps.append(
        StepResult(
            "merge", True, f"--no-ff into {plan.base} as {merge_commit}", time.monotonic() - began
        )
    )
    # Immediately, before anything that can fail. See `record_merge`.
    record_merge(manager, task.id, plan, merge_commit, authorisation)
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
            f"verified live. Finished by the scripted path with no agent session "
            f"(task-241). {authorisation}\n\n"
            # The step table, on the successful path as well as the escalating one. An
            # escalation has to say how far it got or the record is ambiguous; a success
            # has to say the same thing for a different reason -- "verified live" is a
            # claim, and this is the evidence for it, including what verification
            # actually asked and what answered. It stops at verification because this
            # entry *is* the close; the worktree and the branch are retired
            # immediately after it.
            "Everything up to and including verification:\n\n"
            "```\n" + "\n".join(step.render() for step in steps) + "\n```"
        ),
    )
    steps.append(StepResult("close", True, "closed completed", 0.0))
    # In this order because a branch checked out in a worktree cannot be deleted. See
    # `delete_branch`, which re-reads git rather than trusting the step above it.
    steps.append(remove_worktree(plan))
    steps.append(delete_branch(plan))
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


@dataclass(frozen=True)
class EscalationDispatch:
    """What became of the escalation's attempt to put an agent on the task.

    Three-valued rather than two, and the third value is the one that matters. A run
    started and *an agent is already there* are both fine; **no agent at all** is the
    state task-340 exists to remove, and it is the only one that has to move the ball.
    """

    run_id: Optional[str] = None
    reason: str = "dispatched"
    detail: str = ""
    #: Whether nobody is going to pick this up, so the ball must leave ``agent``.
    unattended: bool = False


def dispatch_after_escalation(
    *,
    manager: TaskManagerLike,
    project: Project,
    project_config: Dict[str, Any],
    settings: FinishSettings,
    task_id: str,
    home: Optional[Path],
    api_base: Optional[str],
) -> EscalationDispatch:
    """Start the session that takes over, spending the approval that started this finish.

    **Gated on the finish's own switches, not on ``auto_dispatch``** (task-340). The two
    questions are genuinely different. ``auto_dispatch`` asks whether an approval may
    start a run with no second click; this asks whether machinery *the approval already
    started* may continue after it could not finish. Conflating them is what left
    task-337 at ``agent``/``work`` for an evening with no agent: the finish escalated
    correctly, wrote a good record, called this, and this declined on a switch about a
    different act. Jeff's ruling, 2026-09-05 -- *the fix is the process, not running
    `agentjobs finish` by hand*.

    So the gate is ``finish.enabled`` and ``finish.dispatch_on_escalation``, the second
    defaulting to on. Nothing else moves: ``assert_dispatch_permitted`` still has to pass,
    the machine's concurrency ceiling and the per-task spend caps are still enforced
    inside ``dispatch_task``, and the run is still attributed to the human's own approval
    entry rather than to anything the finisher wrote. This widens one gate; it does not
    touch the safety argument, and it cannot loop: the run it starts ends at a review
    handoff, and the next finish needs another approval.

    Never raises, and never returns silently. An escalation already written to the record
    must not become a crash because a run could not start -- and it must not become a
    task that reads ``agent`` with nobody on it either, which is why every path out of
    here says which of the three things happened.
    """
    from agentjobs.dispatch.guards import DispatchRequest, dispatch_task, resolve_machine_home
    from agentjobs.dispatch.runner import DispatchRunError
    from agentjobs.models_v2 import DispatchTrigger

    if not settings.enabled:  # pragma: no cover - a finish cannot escalate with it off
        return EscalationDispatch(
            reason="finish_disabled",
            detail=f"{project.id} has no `finish.enabled: true` on this machine.",
            unattended=True,
        )
    if not settings.dispatch_on_escalation:
        return EscalationDispatch(
            reason="escalation_dispatch_off",
            detail=(
                f"This machine sets `finish.dispatch_on_escalation: false` for "
                f"{project.id}, so a stopped finish deliberately starts nothing."
            ),
            unattended=True,
        )

    try:
        resolution = assert_dispatch_permitted(project.id, home)
    except DispatchError as exc:
        return EscalationDispatch(
            reason="dispatch_not_permitted",
            detail=f"This machine will not dispatch {project.id}: {exc}",
            unattended=True,
        )

    task = manager.get_task(task_id)
    if task is None or not task.is_open:
        # Nothing to hand to anybody: a closed task has no ball. Reachable when the
        # escalation happened after the close, which is the post-delivery stops.
        return EscalationDispatch(
            reason="not_open", detail=f"{task_id} is closed or missing.", unattended=False
        )

    # Before asking, because asking would be refused for this reason anyway and the
    # refusal is the one case where the ball must *not* move. `finish_task` calls this
    # with the lock still held when a run is finishing itself (task-022): that run is
    # alive, holds the ball, and is the agent the handoff is addressed to.
    for run in live_runs(resolve_machine_home(home, resolution)):
        if run.task_id == task_id:
            return EscalationDispatch(
                run_id=run.run_id,
                reason="live_run",
                detail=(
                    f"{run.run_id} is already live on {task_id}; it is the session this "
                    "escalation is addressed to."
                ),
                unattended=False,
            )

    caused_by = newest_human_entry(task, project_config)
    if caused_by is None:
        return EscalationDispatch(
            reason="no_human_entry",
            detail=(
                f"{task_id} has no log entry written by anyone this project configures "
                "as a human, so there is no approval to attribute a run to."
            ),
            unattended=True,
        )

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
    except (DispatchError, DispatchRunError) as exc:
        return EscalationDispatch(
            reason="dispatch_refused",
            detail=f"Dispatch was refused: {exc}",
            unattended=True,
        )
    return EscalationDispatch(run_id=handle.run_id, reason="dispatched")


def park_for_human(
    manager: TaskManagerLike, task_id: str, project_id: str, outcome: EscalationDispatch
) -> None:
    """Move the ball off an agent that does not exist, and say what the human's move is.

    **An open task names who acts next, and ``agent`` with nobody dispatched is a lie the
    schema cannot catch** (task-340). Every earlier escalation wrote ``agent``/``work``
    unconditionally, on the assumption that a Dispatch click was coming; task-337 spent an
    evening proving that nothing tells anybody the click is needed. A task at
    ``human``/``decision`` is on the review surface the moment it is written.

    Deliberately a second handoff rather than a branch inside ``escalate_on_record``. The
    escalation's own prompt is what a woken session is handed verbatim, so it has to be
    written before the dispatch is attempted -- and the attempt is what decides this. The
    extra entry is not noise: it is the record of an attempt that failed, which is the
    thing that was missing.
    """
    manager.handoff(
        task_id,
        actor=FINISHER,
        ball=Ball.HUMAN,
        ball_reason=BallReason.DECISION,
        ball_prompt=(
            "The approval ran the scripted finish, it stopped, and **no agent was "
            f"started to take it from there**: {outcome.detail}\n\n"
            "The finisher's newest progress entry says what stopped it and how far it "
            "got. Nothing further will happen to this task until somebody acts, so this "
            "is here to make sure somebody knows. Either fix the cause and re-run the "
            "finish:\n\n"
            f"```\nagentjobs finish {task_id} --project {project_id}\n```\n\n"
            "or click Dispatch on the task page to put a session on the repair."
        ),
    )


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

    **A marker is written before the spawn, and that ordering is the feature** (task-321).
    The child takes one to two seconds to import Python and create its finish directory,
    and the approve request answers well inside that window. A page that reloaded on the
    answer and found nothing would conclude no finish was happening and stop looking --
    for the whole three minutes of the one it had just started. Writing the marker here,
    synchronously, makes that impossible: the request cannot return before the evidence
    exists.
    """
    import sys

    resolved_home = home or default_home()
    log_dir = spawn_root(resolved_home)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log = spawn_log_path(resolved_home, task_id)
        handle = log.open("w", encoding="utf-8")
    except OSError:
        return None
    write_spawn_marker(resolved_home, task_id, project_id=project.id, approver=approver)

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
