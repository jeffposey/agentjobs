"""Starting an agent, in two modes.

``session`` is the primary path: ``--bg --remote-control`` hands the process to Claude
Code's own session manager, which already does the hard parts, and AgentJobs owns the
*record* and the polling. ``batch`` is the retained original: a blocking supervisor
thread around ``subprocess.Popen``, for bounded runs and for any CLI with no session
manager. Neither is a degraded version of the other; see design section 4.

Three rules govern everything here.

**Nothing is ever run through a shell.** argv is a list, built element by element, handed
to ``subprocess`` as a list. There is no ``shell=True`` in this subsystem.

**The supervisor may not die quietly.** ``WebhookManager._dispatch`` runs in a detached
asyncio task and a ``NameError`` inside it was invisible for months (task-047). The batch
supervisor is a plain thread whose body is wrapped so a terminal ``dispatch_result`` is
written on *every* exit path, including its own unexpected exception. There is no
``asyncio`` in this module and no ``except`` that logs and returns.

**Structured state comes from the ledger, never from a transcript.** ``claude agents
--json`` is parsed; ``claude logs`` is a terminal rendering with ANSI in it, and is only
ever passed through verbatim for a human to read.

Every subprocess here decodes as UTF-8 with ``errors="replace"`` rather than taking
``text=True``'s default, which is the locale codepage -- cp1252 on a stock Windows
install. A real transcript is full of box-drawing characters, and the default raises
``UnicodeDecodeError`` *inside subprocess's reader thread*, where it surfaces as
``stdout`` being ``None`` rather than as an error anyone can attribute. Observed
2026-08-18 against a live session, having passed every unit test.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import IO, Callable, Dict, List, Optional, Sequence

import yaml

from agentjobs.dispatch.config import (
    DispatchResolution,
    DispatchRunner as RunnerConfig,
    Posture,
    RunnerMode,
    RunnerSelection,
    sentinel_active,
    substitute_argv,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    DispatchCandidateData,
    DispatchMode,
    DispatchOutcome,
    DispatchPosture,
    DispatchSelectionData,
    DispatchTrigger,
    Task,
    utcnow,
)

RUNS_DIRNAME = "runs"
META_FILENAME = "meta.yaml"

TERMINAL_STATUSES = frozenset({"finished", "cancelled", "failed"})
"""Statuses meaning nothing is executing. Everything else counts as live."""

STDOUT_FILENAME = "stdout.log"
STDERR_FILENAME = "stderr.log"
TRANSCRIPT_FILENAME = "transcript.log"
"""Where a session run's own output is kept, beside the launcher's ``stdout.log``.

A session's work is not in ``stdout.log`` and structurally cannot be: under ``--bg`` the
launcher prints a backgrounding banner and exits, and the session's transcript lives in
the runner's own store. That store is also transient -- reaping a finished session
discards it -- so the transcript is copied here while the session is alive, by whatever
polls it. A run directory is then a complete account of the run on its own, which is
what every other reader of these directories already assumes.
"""

GUIDE_PATH = "docs/agent-workflow.md"
"""The operational guide the prompt stub points at.

Pinned by a test that asserts the file exists and links to the resumption contract. The
stub originally named this file when it was entirely v1-era, so every dispatched agent
would have been sent to a stale document as its first instruction -- found by the
read-only dispatch experiment on 2026-08-11, which is to say the first headless run under
this design found the bug in the prompt that dispatched it.
"""

PROMPT_STUB = (
    "You are the agent `{agent}` working task `{task_id}` in project `{project_id}` "
    "(root: {project_root}). AgentJobs is serving at {api_base}. Read the task record "
    "and follow the resumption contract in " + GUIDE_PATH + ". Dispatch run id: {run_id}."
)
"""Fixed text plus five substitutions, and deliberately nothing more.

The resumption contract already guarantees the record is sufficient to resume from, so
the payload is a pointer to where the context is, not a copy of it. Composing a richer
prompt would put the contract in a second place and guarantee the two disagree.
"""

GRACE_SECONDS = 30.0
"""How long a cancelled batch run gets to finish a ``git commit`` before it is killed."""

OUTPUT_TAIL_LINES = 40
"""Lines of run output inlined into a non-success ``dispatch_result``.

On success the body stays empty: the agent's own entries carry the substance. On any
other outcome the machine-local logs are the only account of what happened, and they are
not in git, so a tail of them goes into the entry that is.
"""


# ----- the permission posture -------------------------------------------------


ALLOW_PREFIXES = (
    "poetry run pytest",
    "poetry run ruff",
    "poetry run black",
    "poetry run mypy",
    "npm run",
    "git status",
    "git diff",
    "git add",
    "git commit",
)
"""The seed allow-list from task-076: deliberately boring commands.

This list is a maintenance surface that will be widened under pressure. What the design
buys is that widening it becomes a *visible act* -- someone answering a parked prompt
with "don't ask again" -- rather than a config edit nobody reviews.
"""

ALLOW_TOOLS = ("Bash", "PowerShell")
"""Both shells are emitted because Windows runs commands through either."""


def allow_rules() -> List[str]:
    """The allow-list rules, in the only form that matches anything.

    ``Tool(prefix:*)``. **The colon is mandatory.** A rule written as
    ``PowerShell(python -m pytest*)`` matches nothing at all, and a run under it looks
    exactly like the feature working right up until the session parks. That cost an hour
    on 2026-08-18, which is why a test asserts the colon rather than trusting this
    comment.
    """
    return [f"{tool}({prefix}:*)" for prefix in ALLOW_PREFIXES for tool in ALLOW_TOOLS]


def allow_list_settings() -> str:
    """The allow-list as the JSON blob ``--settings`` takes."""
    return json.dumps({"permissions": {"allow": allow_rules()}})


def posture_flags(posture: Posture, task_id: str) -> List[str]:
    """The flags that decide what a run may do, per task-076.

    **AgentJobs owns these, not the operator.** Mechanically they are just more argv,
    which makes them look like the runner's business; they are the actual risk boundary
    of the whole feature, and burying them in a config example means they get chosen by
    whoever copies the example first.

    ``read_only`` gets no worktree because it cannot write anything to one.
    """
    if posture is Posture.READ_ONLY:
        return ["--tools", "Read,Glob,Grep,WebFetch"]
    if posture is Posture.AUTONOMOUS:
        return ["--permission-mode", "bypassPermissions", "-w", task_id]
    return [
        "--permission-mode",
        "acceptEdits",
        "-w",
        task_id,
        "--settings",
        allow_list_settings(),
    ]


def compose_argv(
    template: Sequence[str], values: Dict[str, str], flags: Sequence[str]
) -> List[str]:
    """Render a runner's argv template and splice the posture flags into it.

    The split of responsibility, stated once here because it is the thing a reader will
    otherwise have to infer: **the operator's template supplies the executable, the mode
    flags and where the prompt goes; AgentJobs supplies the posture flags.** A CLI that
    is not Claude Code can therefore be driven by editing a template, while the flags
    that decide what a run may do stay out of a file the operator is invited to copy
    from an example.

    Flags are spliced immediately *before* the element carrying the prompt, so they land
    where a CLI expects options rather than after a positional argument.
    """
    rendered = substitute_argv(template, values)
    prompt = values.get("prompt")
    insert_at = len(rendered)
    if prompt:
        for index, element in enumerate(rendered):
            if prompt in element:
                insert_at = index
                break
    return [*rendered[:insert_at], *flags, *rendered[insert_at:]]


# ----- runs on disk -----------------------------------------------------------


_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
"""CSI and OSC escape sequences."""

_FRAME_ONLY = re.compile(r"^[\s←-⇿─-▟■-◿⬀-⯿]*$")
"""A line of nothing but box-drawing, arrows and whitespace: frame, not content."""

REMOTE_CONTROL_URL = re.compile(r"https://claude\.ai/code/\S+")
"""The Remote Control link a session prints when it starts.

It appears **only** in the transcript and never as a ledger field -- the design flagged
that and left it to this task. Matching one self-describing URL is the smallest possible
dependency on a terminal rendering: it either matches or it does not, no control flow
turns on the answer, and the alternative is handing someone a parked session with no way
to reach it from the device they are holding.
"""


def strip_ansi(text: str) -> str:
    """Remove escape sequences so a transcript can be read by a person."""
    return _ANSI.sub("", text)


def readable_tail(text: str, lines: int) -> str:
    """The last meaningful lines of a terminal rendering, with the frame taken off.

    Rendering, not parsing. Nothing here decides anything -- but a ball prompt full of
    raw CSI sequences is unusable in the place it is meant to be answered from, which
    makes the handoff worthless in practice even though it is technically correct.
    Observed against a live parked session on 2026-08-18.
    """
    kept = [
        line.rstrip()
        for line in strip_ansi(text).splitlines()
        if line.strip() and not _FRAME_ONLY.match(line)
    ]
    return "\n".join(kept[-lines:])


def drop_repainted_lines(text: str) -> str:
    """Collapse a terminal scrape's repeated screens, keeping the last of each line.

    ``<runner> logs`` returns the session's pty stream, and a full-screen TUI repaints
    its whole screen on every update. So the capture holds the same frame over and over:
    forty lines of a real session were thirteen distinct lines painted three times, with
    the newest work pushed off the end by copies of itself.

    Rendering, and only rendering. Nothing decides anything on this, it is applied to a
    session transcript alone -- a batch run that legitimately prints the same line twice
    is showing two things happening, and its output is passed through untouched.
    """
    seen: set[str] = set()
    kept: List[str] = []
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            if stripped in seen:
                continue
            seen.add(stripped)
        kept.append(line)
    return "\n".join(reversed(kept))


def working_tree_clean(project_root: Path) -> bool:
    """True when a project's working tree has nothing uncommitted.

    A failed or missing git is reported as *not* clean. Dispatch's default is to refuse
    on a dirty tree, and "we could not tell" must fall on the refusing side of that.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and not (result.stdout or "").strip()


def git_head(project_root: Path) -> str:
    """The commit a project is on, so the diff a run produced stays attributable."""
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return (result.stdout or "").strip() or "unknown"


def resolve_executable(name: str) -> str:
    """Turn a program name into a path ``subprocess`` can actually start.

    On Windows most npm-installed CLIs are ``.CMD`` shims rather than ``.exe``, and
    ``Popen(["claude", ...])`` without a shell fails with ``WinError 2`` -- the file
    genuinely is not there under that name. The obvious fix, ``shell=True``, is exactly
    what this subsystem refuses: it would turn a prompt full of quotes and semicolons
    into a command string.

    So the lookup PATHEXT would have done is done explicitly instead. argv stays a list
    all the way to ``CreateProcess``; only element zero becomes a full path. (Python
    applies batch-file-specific quoting to the remaining arguments when the resolved
    target is a ``.cmd``, which is what keeps the ``--settings`` JSON intact through the
    shim.)

    Found on 2026-08-18 by running the real thing: every unit test passed against fake
    runners spawned as ``sys.executable``, which is an absolute path and therefore never
    exercised this.
    """
    return shutil.which(name) or name


def new_run_id() -> str:
    """A run id. Distinct from a session id, which the CLI assigns and we cannot pick."""
    return f"run_{uuid.uuid4().hex[:8]}"


def runs_root(home: Path) -> Path:
    """Directory holding one subdirectory per run."""
    return home / RUNS_DIRNAME


def finish_stamped(meta: Dict[str, object], fields: Dict[str, object]) -> Dict[str, object]:
    """Merged run metadata, with ``finished_at`` recorded by the write that ends a run.

    A concluded run's duration is ``finished_at - started_at``. Without a finish time the
    only thing left to subtract from is the clock you happen to read it at, which is why
    every terminal run's duration grew without bound: a run that took 42 seconds reported
    11.6 hours the next morning (task-158).

    Stamped here rather than at each of the several call sites that can end a run, so
    none of them can forget -- including ones written later. An explicit ``finished_at``
    in ``fields`` wins, because the two finishing paths pass the same instant they
    computed the task log's ``duration_seconds`` from, and that agreement is worth more
    than the fraction of a second a meta write costs.
    """
    merged = {**meta, **fields}
    status = merged.get("status")
    if isinstance(status, str) and status in TERMINAL_STATUSES and not merged.get("finished_at"):
        merged["finished_at"] = datetime.now(timezone.utc).isoformat()
    return merged


@dataclass
class RunDirectory:
    """One run's machine-local directory.

    Written *before* ``Popen``, so a supervisor that dies mid-spawn still leaves a row
    for someone to find. A supervisor that dies before writing anything never started a
    process, which is the only other case.
    """

    path: Path

    @classmethod
    def create(cls, home: Path, run_id: str, meta: Dict[str, object]) -> "RunDirectory":
        """Create the directory and write its initial metadata."""
        path = runs_root(home) / run_id
        path.mkdir(parents=True, exist_ok=True)
        directory = cls(path=path)
        directory.write_meta(meta)
        return directory

    def write_meta(self, meta: Dict[str, object]) -> None:
        """Replace meta.yaml. Small enough that a rewrite is simpler than a patch."""
        (self.path / META_FILENAME).write_text(
            yaml.safe_dump(meta, sort_keys=False, allow_unicode=False), encoding="utf-8"
        )

    def read_meta(self) -> Dict[str, object]:
        """Read meta.yaml, or an empty mapping if it is missing or unreadable."""
        meta_path = self.path / META_FILENAME
        if not meta_path.is_file():
            return {}
        try:
            loaded = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def update_meta(self, **fields: object) -> None:
        """Merge fields into meta.yaml, stamping the finish time when the run ends."""
        self.write_meta(finish_stamped(self.read_meta(), fields))

    def output_tail(self, lines: int = OUTPUT_TAIL_LINES) -> str:
        """The last lines of combined output, for inlining into a failure entry."""
        collected: List[str] = []
        for name in (STDOUT_FILENAME, STDERR_FILENAME):
            candidate = self.path / name
            if not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover - unreadable mid-write
                continue
            if text.strip():
                collected.append(f"--- {name} ---")
                collected.extend(text.splitlines()[-lines:])
        return "\n".join(collected)


# ----- session state ----------------------------------------------------------


class SessionPhase(Enum):
    """What the ledger says about a session, reduced to what dispatch acts on."""

    RUNNING = "running"
    PARKED = "parked"
    FINISHED = "finished"
    STOPPED = "stopped"
    GONE = "gone"


def classify_session(status: Optional[str], state: Optional[str]) -> SessionPhase:
    """Reduce a ledger ``status``/``state`` pair to a phase.

    The observed pairs, verified on 2.1.228: ``busy``/``working``, ``waiting``/``blocked``,
    ``idle``/``done``, ``idle``/``blocked``, and ``stopped``. ``idle``/``blocked`` is a
    session that *finished* after a denial, not one waiting for an answer -- reading it
    as parked would hand a human a prompt nobody is waiting on.

    Anything unrecognised is treated as still running rather than as finished: declaring
    a live run over would write a terminal entry for a session that then keeps working.
    """
    if state == "stopped" or status == "stopped":
        return SessionPhase.STOPPED
    if status == "waiting" and state == "blocked":
        return SessionPhase.PARKED
    if status == "idle":
        return SessionPhase.FINISHED
    return SessionPhase.RUNNING


class DispatchRunError(Exception):
    """A run could not be started. Distinct from a run that started and then failed."""


def selection_data(selection: Optional[RunnerSelection]) -> Optional[DispatchSelectionData]:
    """Turn a resolver selection into the git-tracked payload, or nothing.

    ``None`` in, ``None`` out, which is the whole compatibility story: a flat
    configuration produces no selection, so its ``dispatch`` entry is byte-identical to
    the one it produced before groups existed.

    This is the only place the two vocabularies meet. The resolver's enums stay inside
    the dispatch package; the log entry stores their values as plain strings, because a
    task file outlives any particular build's idea of what the enum members are.
    """
    if selection is None or selection.group is None:
        return None
    return DispatchSelectionData(
        group=selection.group,
        source=selection.source.value,
        candidates=[
            DispatchCandidateData(
                runner=candidate.runner,
                eligible=candidate.eligible,
                skipped_because=(
                    candidate.skipped_because.value if candidate.skipped_because else None
                ),
                detail=candidate.detail,
            )
            for candidate in selection.candidates
        ],
    )


# ----- the runner -------------------------------------------------------------


@dataclass
class RunHandle:
    """A started run: what the caller needs to track it."""

    run_id: str
    task_id: str
    mode: DispatchMode
    directory: RunDirectory
    pid: Optional[int] = None
    session_id: Optional[str] = None
    dispatch_entry_id: Optional[int] = None
    runner: Optional[str] = None
    """Which runner was started. Surfaced so a caller can say what it got.

    With groups, the answer is no longer "the one you configured": the caller asked for
    a group and the dispatcher chose within it, so a response that omits this leaves the
    person who clicked unable to tell which model they are paying for.
    """
    group: Optional[str] = None
    """The group it was chosen from, when one participated."""
    supervisor: Optional[threading.Thread] = field(default=None, repr=False)
    lock: Optional[object] = field(default=None, repr=False)
    """The per-task run lock, held for this run's lifetime and released when it ends."""

    def release_lock(self) -> None:
        """Release the run lock, if this run took one. Safe to call twice."""
        if self.lock is not None:
            self.lock.release()  # type: ignore[attr-defined]
            self.lock = None


class DispatchRunner:
    """Starts and follows one project's runs.

    Holds no state between runs beyond what is on disk and in the task record, so a
    restart loses nothing that mattered.
    """

    def __init__(
        self,
        *,
        manager: TaskManager,
        resolution: DispatchResolution,
        project_root: Path,
        home: Path,
        api_base: str = "http://localhost:8765",
        grace_seconds: float = GRACE_SECONDS,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.manager = manager
        self.resolution = resolution
        self.project_root = Path(project_root)
        self.home = Path(home)
        self.api_base = api_base
        self.grace_seconds = grace_seconds
        self.clock = clock

    # ----- shared ------------------------------------------------------------

    @property
    def runner(self) -> RunnerConfig:
        """The resolved runner definition."""
        return self.resolution.runner

    def _group_name(self) -> Optional[str]:
        """The group this run's runner came from, or ``None`` on a flat configuration."""
        selection = self.resolution.selection
        return selection.group if selection else None

    def build_prompt(self, task_id: str, run_id: str) -> str:
        """The prompt stub. A pointer to the record, never a copy of it."""
        return PROMPT_STUB.format(
            agent=self.runner.actor_id,
            task_id=task_id,
            project_id=self.resolution.project_id,
            project_root=self.project_root,
            api_base=self.api_base,
            run_id=run_id,
        )

    def build_argv(self, task_id: str, run_id: str) -> List[str]:
        """The full argv for a run, posture flags included."""
        values = {
            "prompt": self.build_prompt(task_id, run_id),
            "task_id": task_id,
            "project_id": self.resolution.project_id,
            "project_root": str(self.project_root),
            "run_id": run_id,
            "agent": self.runner.actor_id,
            "api_base": self.api_base,
        }
        flags = posture_flags(self.resolution.settings.posture, task_id)
        argv = compose_argv(self.runner.argv, values, flags)
        # Resolved before it is recorded, because the dispatch entry claims to say what
        # actually ran.
        argv[0] = resolve_executable(argv[0])
        return argv

    def _environment(self) -> Dict[str, str]:
        """The child's environment: ours, plus the runner's additions.

        Additive rather than replacing, so a runner does not have to restate PATH. Never
        logged -- this is where a runner's secrets belong, precisely because argv is
        recorded verbatim.
        """
        environment = dict(os.environ)
        environment.update(self.runner.env)
        return environment

    def _assert_spawnable(self, task: Task) -> None:
        """The two preconditions checked immediately before every spawn.

        The sentinel is re-checked here rather than trusted from resolution time: it is
        the panic button, and the whole point is that creating the file stops the *next*
        run, not the next configuration reload.
        """
        if sentinel_active(self.home):
            raise DispatchRunError(f"Refusing to spawn: {self.home / 'DISPATCH_DISABLED'} exists.")
        if self.resolution.settings.require_clean_tree and not self._tree_is_clean():
            raise DispatchRunError(
                f"Refusing to spawn for {task.id}: {self.project_root} has uncommitted "
                "changes. An autonomous agent committing on top of them entangles the "
                "two, and unpicking that is hardest exactly when you least expect it."
            )

    def _tree_is_clean(self) -> bool:
        """True when the project's working tree has nothing uncommitted."""
        return working_tree_clean(self.project_root)

    def _git_head(self) -> str:
        """The commit the working tree is on, so a run's diff stays attributable."""
        return git_head(self.project_root)

    def _record_dispatch(
        self,
        task: Task,
        run_id: str,
        argv: List[str],
        *,
        actor: str,
        caused_by: int,
        trigger: DispatchTrigger,
        mode: DispatchMode,
        session_id: Optional[str],
    ) -> int:
        """Append the dispatch entry and return its id."""
        updated = self.manager.record_dispatch(
            task.id,
            actor=actor,
            run_id=run_id,
            agent=self.runner.actor_id,
            runner=self.runner.name,
            mode=mode,
            posture=DispatchPosture(self.resolution.settings.posture.value),
            trigger=trigger,
            caused_by=caused_by,
            argv=argv,
            cwd=str(self.project_root),
            git_head=self._git_head(),
            session_id=session_id,
            selection=selection_data(self.resolution.selection),
        )
        return updated.log[-1].id

    # ----- entry point -------------------------------------------------------

    def start(
        self,
        task: Task,
        *,
        actor: str,
        caused_by: int,
        trigger: DispatchTrigger = DispatchTrigger.MANUAL,
    ) -> RunHandle:
        """Start a run for ``task`` in whichever mode the runner declares."""
        self._assert_spawnable(task)
        if self.runner.mode is RunnerMode.SESSION:
            return self._start_session(task, actor=actor, caused_by=caused_by, trigger=trigger)
        return self._start_batch(task, actor=actor, caused_by=caused_by, trigger=trigger)

    # ----- session mode ------------------------------------------------------

    _SHORT_ID = re.compile(r"\b([0-9a-f]{8})\b")

    def _start_session(
        self, task: Task, *, actor: str, caused_by: int, trigger: DispatchTrigger
    ) -> RunHandle:
        """Spawn a background session and capture the id the CLI assigned it.

        ``--bg`` returns immediately and **ignores ``--session-id``**, warning that it
        manages the id itself. So a run id and a session id are two different values and
        the record stores both; anything that passes ``--session-id`` alongside ``--bg``
        is wrong.
        """
        run_id = new_run_id()
        argv = self.build_argv(task.id, run_id)
        directory = RunDirectory.create(
            self.home,
            run_id,
            {
                "run_id": run_id,
                "task_id": task.id,
                "project_id": self.resolution.project_id,
                "mode": DispatchMode.SESSION.value,
                "posture": self.resolution.settings.posture.value,
                "status": "starting",
                "started_at": self.clock().isoformat(),
                "caused_by": caused_by,
                "argv": argv,
            },
        )

        try:
            completed = subprocess.run(
                argv,
                cwd=str(self.project_root),
                env=self._environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            directory.update_meta(status="failed", error=str(exc))
            raise DispatchRunError(f"Could not start a session for {task.id}: {exc}") from exc

        output = f"{completed.stdout}\n{completed.stderr}"
        (directory.path / STDOUT_FILENAME).write_text(output, encoding="utf-8")
        if completed.returncode != 0:
            directory.update_meta(status="failed", exit_code=completed.returncode)
            raise DispatchRunError(
                f"Session launch for {task.id} exited {completed.returncode}: "
                f"{output.strip()[:500]}"
            )

        session_id = self.capture_session_id(completed.stdout or "")
        if session_id is None:
            directory.update_meta(status="failed", error="no session id in launcher output")
            raise DispatchRunError(
                f"Started a session for {task.id} but could not read its id from the "
                f"launcher's output, so nothing could follow it: {output.strip()[:500]}"
            )

        entry_id = self._record_dispatch(
            task,
            run_id,
            argv,
            actor=actor,
            caused_by=caused_by,
            trigger=trigger,
            mode=DispatchMode.SESSION,
            session_id=session_id,
        )
        directory.update_meta(status="running", session_id=session_id, dispatch_entry_id=entry_id)
        return RunHandle(
            run_id=run_id,
            task_id=task.id,
            mode=DispatchMode.SESSION,
            directory=directory,
            session_id=session_id,
            dispatch_entry_id=entry_id,
            runner=self.runner.name,
            group=self._group_name(),
        )

    @classmethod
    def capture_session_id(cls, stdout: str) -> Optional[str]:
        """Read the short id out of ``backgrounded · b55b35ad · name``.

        Positional rather than regex-over-the-whole-line on purpose: the separator and
        the trailing name are cosmetic and will change; an 8-hex token on the launcher's
        first line is the stable part.
        """
        for line in stdout.splitlines():
            match = cls._SHORT_ID.search(line)
            if match:
                return match.group(1)
        return None

    def executable_prefix(self) -> List[str]:
        """The part of the runner's argv that names the program, without its flags.

        The session subcommands (``agents``, ``logs``, ``stop``) have to be invoked as
        the same program the run was started with, minus whatever flags start that run.
        Taking only ``argv[0]`` is wrong for any launcher that needs more than one
        element to name itself -- ``python script.py``, ``npx something``, a wrapper -- so
        the rule is: the leading elements up to the first flag or substitution.
        """
        prefix: List[str] = []
        for element in self.runner.argv:
            if element.startswith("-") or "{" in element:
                break
            prefix.append(element)
        if not prefix:
            prefix = [self.runner.argv[0]]
        return [resolve_executable(prefix[0]), *prefix[1:]]

    def ledger(self) -> List[Dict[str, object]]:
        """Background sessions this project owns, from ``<runner> agents --json --cwd``.

        ``--cwd`` scopes the listing to one project root, so an unrelated session
        elsewhere on the machine is never mistaken for a dispatched run.

        The ledger command is derived from the runner's own executable rather than
        hardcoded to ``claude``. That is what "session mode" means operationally: a
        runner whose executable answers ``agents --json``. It also makes the path
        testable without a real Claude Code install.
        """
        argv = [*self.executable_prefix(), "agents", "--json", "--cwd", str(self.project_root)]
        try:
            completed = subprocess.run(
                argv,
                cwd=str(self.project_root),
                env=self._environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DispatchRunError(f"Could not read the session ledger: {exc}") from exc
        if completed.returncode != 0:
            raise DispatchRunError(
                f"Session ledger command failed ({completed.returncode}): "
                f"{(completed.stderr or '').strip()[:300]}"
            )
        try:
            loaded = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise DispatchRunError(f"Session ledger was not JSON: {exc}") from exc
        if isinstance(loaded, dict):
            loaded = loaded.get("agents") or loaded.get("sessions") or []
        return [row for row in loaded if isinstance(row, dict)]

    def _ledger_row(self, session_id: str) -> Optional[Dict[str, object]]:
        """The ledger row for one session, or None when it is gone."""
        for row in self.ledger():
            if row.get("id") == session_id or row.get("sessionId") == session_id:
                return row
        return None

    def display_command(self) -> str:
        """The runner as a person would type it, not as it was resolved for exec.

        The recorded argv carries the resolved path, because that entry claims to say
        what actually ran. A ball prompt telling someone to type
        ``C:\\Users\\...\\npm\\claude.CMD attach ba6d5845`` is technically true and useless.
        """
        return self.runner.argv[0]

    def transcript(self, session_id: str) -> str:
        """The raw output of ``<runner> logs <id>``, for a human to read.

        **This is not parsing.** No control flow in this module depends on it; state
        comes from the ledger. It is fetched because the ledger does not carry the
        pending command, and a human answering a parked permission prompt from a phone
        needs to see what is being asked. Showing someone the terminal is a different
        act from deriving structured state out of a terminal rendering.
        """
        try:
            completed = subprocess.run(
                [*self.executable_prefix(), "logs", session_id],
                cwd=str(self.project_root),
                env=self._environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return completed.stdout or ""

    def capture_transcript(self, handle: RunHandle) -> str:
        """Copy the session's current output into its run directory. Returns what it read.

        Called on every poll rather than on demand, for two reasons that both come from
        the transcript living somewhere AgentJobs does not own:

        - **A finished session's transcript does not survive being reaped.** Fetching it
          when a human clicks would show nothing for exactly the runs worth reading.
        - **Reading it costs a subprocess.** Serving a browser from this file means the
          only clock that spawns processes is the poller's, however many people watch.

        Nothing here decides anything -- an empty read leaves the previous capture in
        place, because "the transcript could not be read right now" is not evidence that
        the session produced nothing.
        """
        if not handle.session_id:
            return ""
        text = self.transcript(handle.session_id)
        if not text.strip():
            return ""
        try:
            (handle.directory.path / TRANSCRIPT_FILENAME).write_text(text, encoding="utf-8")
        except OSError:  # pragma: no cover - the run directory went away underneath us
            pass
        return text

    def stop_session(self, session_id: str) -> bool:
        """Reap a session, which otherwise holds its pid indefinitely.

        ``stop`` and not ``rm``: ``rm`` deletes the worktree and refuses when it holds
        uncommitted changes, so reaping with it would either destroy work or fail exactly
        when a run had produced something.
        """
        try:
            completed = subprocess.run(
                [*self.executable_prefix(), "stop", session_id],
                cwd=str(self.project_root),
                env=self._environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    def poll_session(self, handle: RunHandle) -> SessionPhase:
        """Read one session's state and act on it. Returns the phase observed.

        Called by whatever schedules polling (task-072); this function decides, it does
        not loop.
        """
        if handle.session_id is None:  # pragma: no cover - a session handle always has one
            raise DispatchRunError(f"Run {handle.run_id} has no session id to poll.")

        row = self._ledger_row(handle.session_id)
        if row is None:
            self._finish_session(
                handle,
                DispatchOutcome.INTERRUPTED,
                body=(
                    "The session is no longer in the ledger, so it cannot be followed or "
                    "resumed. Whatever it did is in its own transcript, not here."
                ),
            )
            return SessionPhase.GONE

        phase = classify_session(
            str(row.get("status")) if row.get("status") is not None else None,
            str(row.get("state")) if row.get("state") is not None else None,
        )

        # Before acting, not after: settling a finished session reaps it, and a reaped
        # session has no transcript left to read.
        self.capture_transcript(handle)

        if phase is SessionPhase.PARKED:
            self._park_session(handle)
        elif phase is SessionPhase.STOPPED:
            self._finish_session(handle, DispatchOutcome.CANCELLED)
        elif phase is SessionPhase.FINISHED:
            self._settle_finished_session(handle)
        return phase

    def _park_session(self, handle: RunHandle) -> None:
        """Turn a parked session into a question a human can answer from anywhere.

        This is the mechanism the ``supervised`` posture depends on. Without it that
        posture is not a safety property, it is a hang.

        A parked run is never escalated to a more permissive posture, here or by any
        timeout. Design section 2 requires every grant of autonomy to trace to a human
        act, and a deadline passing is not one.
        """
        # Keyed on this run's own state rather than on the ball, because a task whose
        # ball is already human for some unrelated reason still needs its permission
        # prompt surfaced -- and polling is repeated, so it must be idempotent.
        if handle.directory.read_meta().get("status") == "parked":
            return
        if self.manager.get_task(handle.task_id) is None:  # pragma: no cover - deleted
            return
        transcript = self.transcript(handle.session_id or "")
        tail = readable_tail(transcript, OUTPUT_TAIL_LINES)
        url = REMOTE_CONTROL_URL.search(strip_ansi(transcript))
        where = (
            f"Answer it at {url.group(0)} — that link works from a phone."
            if url
            else "Answer it wherever the session is open."
        )
        quoted = f"\n\nThe end of its terminal, verbatim:\n\n```\n{tail}\n```" if tail else ""
        self.manager.handoff(
            handle.task_id,
            actor="dispatcher",
            ball=Ball.HUMAN,
            ball_reason=BallReason.INPUT,
            ball_prompt=(
                f"Dispatched session `{handle.session_id}` is parked on a permission "
                f"prompt and will wait indefinitely. {where} Or attach locally with "
                f"`{self.display_command()} attach {handle.session_id}`.{quoted}"
            ),
        )
        handle.directory.update_meta(status="parked")

    def _settle_finished_session(self, handle: RunHandle) -> None:
        """Decide whether a finished session concluded or merely stopped.

        The ledger cannot tell these apart -- both are ``idle``/``done``, and there is no
        exit code in it -- so the question is asked of the task record instead: did the
        ball move? That is where the resumption contract always got it.

        A session whose ball has not moved is given the staleness window before being
        called ``finished_without_handoff``, because an agent that pauses mid-work looks
        identical to one that stopped for good until enough time has passed.
        """
        task = self.manager.get_task(handle.task_id)
        if task is None:  # pragma: no cover - the task was deleted underneath the run
            return
        if self._ball_moved(task, handle):
            self._finish_session(handle, DispatchOutcome.COMPLETED, reap=True)
            return

        started = self._started_at(handle)
        stale_after = timedelta(seconds=self.resolution.limits.session_stale_seconds)
        if started is not None and self.clock() - started < stale_after:
            return

        self._finish_session(
            handle,
            DispatchOutcome.FINISHED_WITHOUT_HANDOFF,
            body=(
                "The session finished its turn and the ball never moved, so it stopped "
                "without saying what it needs. It was **not** killed and is still "
                f"attachable: `{self.display_command()} attach {handle.session_id}`."
            ),
            hand_to_human=(
                f"A dispatched session ({handle.session_id}) finished without handing "
                "off, so nobody was told what it needs. Read what it did, then either "
                "attach to it or move this task on yourself."
            ),
            reap=False,
        )

    def _ball_moved(self, task: Task, handle: RunHandle) -> bool:
        """True when the task's ball moved after the dispatch entry was written."""
        if handle.dispatch_entry_id is None:
            return False
        return any(
            entry.id > handle.dispatch_entry_id and entry.type.value in {"handoff", "transition"}
            for entry in task.log
        )

    def _started_at(self, handle: RunHandle) -> Optional[datetime]:
        """When the run started, from its own metadata."""
        raw = handle.directory.read_meta().get("started_at")
        if not isinstance(raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:  # pragma: no cover - meta written by us
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def _finish_session(
        self,
        handle: RunHandle,
        outcome: DispatchOutcome,
        *,
        body: Optional[str] = None,
        hand_to_human: Optional[str] = None,
        reap: bool = True,
    ) -> None:
        """Write the terminal entry for a session, reap it, and move the ball if needed."""
        if handle.directory.read_meta().get("status") in {"finished", "cancelled", "failed"}:
            return
        finished = self.clock()
        duration = None
        started = self._started_at(handle)
        if started is not None:
            duration = (finished - started).total_seconds()

        self.manager.record_dispatch_result(
            handle.task_id,
            actor="dispatcher",
            run_id=handle.run_id,
            outcome=outcome,
            re=handle.dispatch_entry_id,
            duration_seconds=duration,
            log_path=str(handle.directory.path),
            body=body,
        )
        handle.directory.update_meta(
            status="finished", outcome=outcome.value, finished_at=finished.isoformat()
        )
        handle.release_lock()

        if reap and handle.session_id:
            self.stop_session(handle.session_id)

        if hand_to_human:
            task = self.manager.get_task(handle.task_id)
            if task is not None and task.is_open and task.ball is not Ball.HUMAN:
                self.manager.handoff(
                    handle.task_id,
                    actor="dispatcher",
                    ball=Ball.HUMAN,
                    ball_reason=BallReason.DECISION,
                    ball_prompt=hand_to_human,
                )

    # ----- batch mode --------------------------------------------------------

    def _start_batch(
        self, task: Task, *, actor: str, caused_by: int, trigger: DispatchTrigger
    ) -> RunHandle:
        """Spawn a batch run and supervise it from a dedicated blocking thread."""
        run_id = new_run_id()
        argv = self.build_argv(task.id, run_id)
        directory = RunDirectory.create(
            self.home,
            run_id,
            {
                "run_id": run_id,
                "task_id": task.id,
                "project_id": self.resolution.project_id,
                "mode": DispatchMode.BATCH.value,
                "posture": self.resolution.settings.posture.value,
                "status": "starting",
                "started_at": self.clock().isoformat(),
                "caused_by": caused_by,
                "argv": argv,
            },
        )

        entry_id = self._record_dispatch(
            task,
            run_id,
            argv,
            actor=actor,
            caused_by=caused_by,
            trigger=trigger,
            mode=DispatchMode.BATCH,
            session_id=None,
        )

        stdout_file = (directory.path / STDOUT_FILENAME).open("w", encoding="utf-8")
        stderr_file = (directory.path / STDERR_FILENAME).open("w", encoding="utf-8")
        try:
            # argv is a list and there is no shell. The two branches are written out
            # rather than unpacked from a dict so the platform difference stays legible.
            if os.name == "nt":
                process = subprocess.Popen(
                    argv,
                    cwd=str(self.project_root),
                    env=self._environment(),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                process = subprocess.Popen(
                    argv,
                    cwd=str(self.project_root),
                    env=self._environment(),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            stdout_file.close()
            stderr_file.close()
            directory.update_meta(status="failed", error=str(exc))
            # The dispatch entry is already written, so it must not be left without a
            # terminal partner -- an unfinished dispatch is indistinguishable from a run
            # still going.
            self.manager.record_dispatch_result(
                task.id,
                actor="dispatcher",
                run_id=run_id,
                outcome=DispatchOutcome.CRASHED,
                re=entry_id,
                log_path=str(directory.path),
                body=f"The run never started: {exc}",
            )
            raise DispatchRunError(f"Could not start a batch run for {task.id}: {exc}") from exc

        directory.update_meta(status="running", pid=process.pid, dispatch_entry_id=entry_id)
        handle = RunHandle(
            run_id=run_id,
            task_id=task.id,
            mode=DispatchMode.BATCH,
            directory=directory,
            pid=process.pid,
            dispatch_entry_id=entry_id,
            runner=self.runner.name,
            group=self._group_name(),
        )
        handle.supervisor = threading.Thread(
            target=self._supervise_batch,
            args=(handle, process, stdout_file, stderr_file),
            name=f"dispatch-{run_id}",
            daemon=True,
        )
        handle.supervisor.start()
        return handle

    def _supervise_batch(
        self,
        handle: RunHandle,
        process: "subprocess.Popen[bytes]",
        stdout_file: IO[str],
        stderr_file: IO[str],
    ) -> None:
        """Block on one run and guarantee it gets exactly one terminal entry.

        A plain thread doing a blocking ``wait()``. Not an asyncio task, not a
        fire-and-forget coroutine: a detached coroutine whose exception nobody awaits is
        a silence generator, and this repository has already paid for one.

        Every path through this function ends in a ``dispatch_result``, including the
        ``except`` clause, which writes ``crashed`` with the traceback rather than
        logging a warning and returning.
        """
        outcome = DispatchOutcome.CRASHED
        body: Optional[str] = None
        exit_code: Optional[int] = None
        try:
            timeout = self.resolution.limits.run_timeout_seconds
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.terminate_group(process)
                exit_code = process.poll()
                outcome = DispatchOutcome.TIMEOUT
                body = f"Terminated after the {timeout}s wall-clock limit."
            else:
                outcome, body = self._classify_batch_exit(handle, exit_code)
        except BaseException:  # noqa: BLE001 - deliberately total; see the docstring
            outcome = DispatchOutcome.CRASHED
            body = (
                "The supervisor itself raised, so this run is reported by the code that "
                f"was watching it rather than by the run:\n\n```\n{traceback.format_exc()}\n```"
            )
            try:
                self.terminate_group(process)
            except BaseException:  # noqa: BLE001 - nothing useful remains to try
                pass
        finally:
            for stream in (stdout_file, stderr_file):
                try:
                    stream.close()
                except OSError:  # pragma: no cover - already closed
                    pass
            self._finish_batch(handle, outcome, exit_code, body)

    def _classify_batch_exit(
        self, handle: RunHandle, exit_code: Optional[int]
    ) -> tuple[DispatchOutcome, Optional[str]]:
        """Decide what a finished batch run's exit code means.

        Exit 0 with an unmoved ball is a **failure**, not a success: the agent stopped
        without saying what it needs, which is exactly the limbo the ball model exists to
        make unrepresentable. Treating a clean exit as success regardless would reproduce
        that limbo at the process level.
        """
        if exit_code != 0:
            return DispatchOutcome.FAILED, f"The run exited {exit_code}."
        task = self.manager.get_task(handle.task_id)
        if task is not None and self._ball_moved(task, handle):
            return DispatchOutcome.COMPLETED, None
        return (
            DispatchOutcome.FINISHED_WITHOUT_HANDOFF,
            "The run exited cleanly and the ball never moved, so it stopped without "
            "saying what it needs.",
        )

    def _finish_batch(
        self,
        handle: RunHandle,
        outcome: DispatchOutcome,
        exit_code: Optional[int],
        body: Optional[str],
    ) -> None:
        """Write the one terminal entry for a batch run, and hand off on failure.

        Guarded so the supervisor cannot write two: if this raises, the run is left
        marked running and startup reconciliation will call it ``interrupted``, which is
        wrong but recoverable. Writing two contradictory terminal entries would not be.

        ``cancel_requested`` is checked separately from the terminal statuses because it
        settles a race the status alone cannot. Killing a batch run wakes this
        supervisor, which sees a non-zero exit and quite reasonably calls it ``failed``;
        the ledger meanwhile writes ``cancelled``. Both are read-modify-writes of the
        same meta file and either can land last, so a run the human cancelled was
        reported as failed roughly half the time -- observed while building the GUI's
        cancel button, which shows that word to a human who has just pressed Cancel.
        The ledger sets the flag **before** it kills, and this supervisor is blocked in
        ``wait()`` until then, so the flag is always visible here by the time it matters.
        """
        meta = handle.directory.read_meta()
        if meta.get("cancel_requested"):
            # Someone asked for this to stop and owns the terminal entry. Release the
            # lock anyway: the run is over either way, and a lock left behind refuses
            # every future dispatch at this task with "a run is already live".
            handle.release_lock()
            return
        if meta.get("status") in {"finished", "cancelled", "failed"}:
            handle.release_lock()
            return

        finished = self.clock()
        duration = None
        started = self._started_at(handle)
        if started is not None:
            duration = (finished - started).total_seconds()

        if outcome is not DispatchOutcome.COMPLETED:
            tail = handle.directory.output_tail()
            if tail.strip():
                body = f"{body or ''}\n\nLast output:\n\n```\n{tail}\n```".strip()

        handle.directory.update_meta(
            status="finished",
            outcome=outcome.value,
            exit_code=exit_code,
            finished_at=finished.isoformat(),
        )
        handle.release_lock()
        self.manager.record_dispatch_result(
            handle.task_id,
            actor="dispatcher",
            run_id=handle.run_id,
            outcome=outcome,
            re=handle.dispatch_entry_id,
            exit_code=exit_code,
            duration_seconds=duration,
            log_path=str(handle.directory.path),
            body=body,
        )

        if outcome is DispatchOutcome.COMPLETED:
            return
        task = self.manager.get_task(handle.task_id)
        if task is not None and task.is_open and task.ball is not Ball.HUMAN:
            self.manager.handoff(
                handle.task_id,
                actor="dispatcher",
                ball=Ball.HUMAN,
                ball_reason=BallReason.DECISION,
                ball_prompt=(
                    f"A dispatched batch run ended `{outcome.value}` and nobody was told "
                    "what the task needs. The run's last output is in the "
                    "dispatch_result entry; decide whether to re-dispatch or take it on."
                ),
            )

    def terminate_group(self, process: "subprocess.Popen[bytes]") -> None:
        """Signal the whole process tree, then kill what is left.

        The tree, not the process: an agent that shelled out to ``pytest`` must not leave
        the ``pytest`` behind. The grace period exists so an agent can finish a
        ``git commit`` rather than being killed mid-write.

        Windows is the reference implementation here, not the port.
        """
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)  # type: ignore[attr-defined]
        except (OSError, ValueError, ProcessLookupError):
            pass

        try:
            process.wait(timeout=self.grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass

        # Only now, and only while the parent is still alive: `taskkill /T` walks the
        # tree by parent pid, so calling it after the parent exited would aim at a pid
        # the OS may have handed to something else.
        _kill_tree(process.pid)
        try:
            process.wait(timeout=self.grace_seconds)
        except subprocess.TimeoutExpired:  # pragma: no cover - the OS refused to kill it
            pass


def _kill_tree(pid: int) -> None:
    """Kill a process and everything it started.

    ``taskkill /T`` walks the tree by parent pid, which is what makes an orphaned
    ``pytest`` reachable; ``killpg`` does the equivalent on POSIX.
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)  # type: ignore[attr-defined]
    except (OSError, ProcessLookupError):
        pass
