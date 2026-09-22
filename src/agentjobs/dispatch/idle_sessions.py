"""Idle Claude Code sessions: what is running, which are idle, and the sweep that stops them.

task-447. Every long-lived Claude Code process on this machine shares one OAuth store, and
each is another refresher in the race that blanks ``~/.claude/.credentials.json`` when two
of them rotate the refresh token at once (task-417 entry 14). task-442 counted eleven such
processes on 2026-09-13; the owner stopped the idle ones by hand, leaving four. This module
keeps that true without the hand.

Three parts, in the order the safety argument needs them:

1. **An inventory** (:func:`take_inventory`). Every Claude Code process is classified from
   its command line, its parent and the session ledger (``claude agents --json``), and a
   background session's idle time is **its transcript's last write**, gated on the ledger
   saying ``idle``. Process age is never idle time: a session started two days ago may
   have written a line a minute ago.
2. **A verdict per process** (:func:`judge`), default-deny. A process is a stop candidate
   only when every one of these is proven: it is a background session; the ledger lists it
   as ``idle``; it is not attached, not an AgentJobs run and not an ancestor of the process
   doing the sweep; it has a transcript, so it can be resumed; and that transcript has been
   quiet past the threshold. Anything this module cannot place is protected, with the
   reason it could not place it.
3. **The sweep** (:func:`sweep`), which stops nothing unless ``idle_sessions.enforce`` is
   true. Immediately before each stop it re-checks the pid's creation time and command
   line (the pid-reuse hazard, task-419/task-444), the ledger status and the transcript's
   mtime, and it records every stop -- with the commands that bring the session back -- in
   the execution journal's ``idle_session_event`` table, where the React UI reads it.

**Never stopped, whatever the settings:** Remote Control hosts (``claude rc``), the daemon,
the Claude desktop app and anything it started, interactive sessions, busy, waiting or
attached sessions, any session with an AgentJobs run record, and the session running the
sweep. Remote Control *children* (``--print --sdk-url``) are reported and never stopped:
killing one makes its host call ``stopWork`` on that conversation's work item (agentjobs
rc log, 2026-09-13T23:04:57Z), and whether the phone can continue it afterwards has not been
verified (task-447 a5).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from agentjobs import clock as dispatch_clock
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SWEEP_INTERVAL_SECONDS = 300
"""How often the poller's tick runs the sweep. The tick itself is ten seconds; enumerating
every process and reading the ledger is not worth doing that often for a threshold measured
in hours."""

STOP_QUIESCE_SECONDS = 30
EVENT_LIMIT = 50

# ----- kinds, verdicts ----------------------------------------------------------------

KIND_DAEMON = "daemon"
KIND_PTY_HOST = "pty_host"
KIND_BACKGROUND = "background"
KIND_RC_HOST = "remote_control_host"
KIND_RC_CHILD = "remote_control_child"
KIND_INTERACTIVE = "interactive"
KIND_DESKTOP = "desktop"
KIND_COMMAND = "command"

VERDICT_PROTECTED = "protected"
"""Never stopped, because of what it is."""
VERDICT_IN_USE = "in_use"
"""Never stopped, because of what it is doing."""
VERDICT_IDLE = "idle"
"""Idle, but not yet past the threshold."""
VERDICT_REPORT_ONLY = "report_only"
"""Idle past the threshold, in a class the sweep reports and does not stop."""
VERDICT_CANDIDATE = "candidate"
"""Would be stopped with enforcement on."""

COMMAND_VERBS = frozenset(
    {
        "agents",
        "attach",
        "auth",
        "config",
        "doctor",
        "install",
        "logs",
        "mcp",
        "plugin",
        "rm",
        "setup-token",
        "stop",
        "update",
        "--version",
        "-v",
        "--help",
        "-h",
    }
)
"""First arguments that make a short-lived command rather than a session."""

AGENTJOBS_SESSION_NAME = re.compile(r"^(?:[^/\s]+/)?task-\d+(?:[@/][0-9a-f]{8}|#\d+)?(?:\s.*)?$")
"""A session name this module should recognise as a dispatch and leave to the dispatcher.

Every shape ``runner.session_name`` has ever produced, because the sweep meets *live*
sessions and a long-running one outlives the release that named it:

* ``task-499 nav breakpoint`` and ``task-499#2 nav breakpoint`` -- since task-500, where
  the project prefix became conditional and a slug from the title was added.
* ``agentjobs/task-324`` and ``agentjobs/task-324#2`` -- since task-452, and still what
  task-500 emits when another project holds a live session for the same task id.
* ``agentjobs/task-324@11085a50`` -- task-324's original, carried by any run started
  before task-452.
* ``agentjobs/task-324/11085a50`` -- the ``/`` separator, matched because task-451 was in
  flight alongside task-452 and either could have landed first. Costless to allow and a
  protection lost if it is not.

Matching too widely is the safe direction: a name this matches is *protected* from the
sweep, never stopped by it. It is deliberately a *recogniser* and not the grammar --
``runner.SESSION_NAME_PATTERN`` is that, and it captures the parts a caller needs to act
on, which this never does.
"""
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class ProcessRow:
    """One process, as the operating system reports it. ``exe``/``cmdline`` may be empty
    for processes that are not candidates to be Claude Code at all."""

    pid: int
    ppid: int
    name: str = ""
    exe: str = ""
    cmdline: str = ""


@dataclass
class SessionView:
    """One Claude Code process and what the sweep concluded about it."""

    pid: int
    kind: str
    verdict: str
    reason: str
    ppid: int = 0
    cmdline: str = ""
    identity: Optional[str] = None
    session_id: Optional[str] = None
    """The full session uuid, when the process has one."""
    short_id: Optional[str] = None
    name: str = ""
    cwd: str = ""
    ledger_status: Optional[str] = None
    ledger_state: Optional[str] = None
    run_id: Optional[str] = None
    transcript: Optional[str] = None
    last_activity: Optional[str] = None
    idle_seconds: Optional[int] = None
    resume_commands: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Inventory:
    generated_at: str
    sessions: List[SessionView]
    idle_minutes: int
    errors: List[str] = field(default_factory=list)

    @property
    def candidates(self) -> List[SessionView]:
        return [view for view in self.sessions if view.verdict == VERDICT_CANDIDATE]


# ----- parsing a command line ---------------------------------------------------------


def split_command_line(text: str) -> List[str]:
    """Tokens of a Windows-or-POSIX command line: whitespace-separated, double quotes group.

    Not a faithful ``CommandLineToArgvW``; it only has to find flags and their values in a
    Claude Code command line, whose values are paths, names and uuids.
    """
    tokens: List[str] = []
    current: List[str] = []
    quoted = False
    started = False
    for char in text or "":
        if char == '"':
            quoted = not quoted
            started = True
        elif char.isspace() and not quoted:
            if started:
                tokens.append("".join(current))
                current, started = [], False
        else:
            current.append(char)
            started = True
    if started:
        tokens.append("".join(current))
    return tokens


def _normal(path: str) -> str:
    return (path or "").replace(chr(92), "/").lower()


def is_desktop_executable(exe: str) -> bool:
    """The Claude desktop app, or the Claude Code build it bundles and authenticates itself."""
    text = _normal(exe)
    return (
        "/windowsapps/claude_" in text
        or "/appdata/roaming/claude/claude-code/" in text
        or "/claude.app/" in text
    )


def is_claude_code(row: ProcessRow) -> bool:
    """Whether this process is a Claude Code CLI (or the desktop app) at all."""
    exe = _normal(row.exe) or _normal(split_command_line(row.cmdline)[:1][0] if row.cmdline else "")
    base = exe.rsplit("/", 1)[-1]
    if base in {"claude.exe", "claude"}:
        return True
    if base in {"node.exe", "node"} and "claude-code" in _normal(row.cmdline):
        return True
    return False


def _arguments(row: ProcessRow) -> List[str]:
    """The arguments after the executable (and after the script, for a node install)."""
    tokens = split_command_line(row.cmdline)
    if not tokens:
        return []
    rest = tokens[1:]
    if _normal(tokens[0]).rsplit("/", 1)[-1] in {"node.exe", "node"} and rest:
        rest = rest[1:]
    return rest


def _flag_value(arguments: Sequence[str], flag: str) -> Optional[str]:
    for index, token in enumerate(arguments):
        if token == flag and index + 1 < len(arguments):
            return arguments[index + 1]
        if token.startswith(flag + "="):
            return token.split("=", 1)[1]
    return None


def classify_process(row: ProcessRow, parents: Mapping[int, ProcessRow]) -> str:
    """What kind of Claude Code process this is, from its command line and its parent."""
    ancestor: Optional[ProcessRow] = row
    seen = set()
    while ancestor is not None and ancestor.pid not in seen:
        if is_desktop_executable(ancestor.exe):
            return KIND_DESKTOP
        seen.add(ancestor.pid)
        ancestor = parents.get(ancestor.ppid)
    arguments = _arguments(row)
    first = arguments[0] if arguments else ""
    if "--bg-pty-host" in arguments:
        return KIND_PTY_HOST
    if first == "daemon":
        return KIND_DAEMON
    if first in {"rc", "remote-control"}:
        return KIND_RC_HOST
    if "--sdk-url" in arguments:
        return KIND_RC_CHILD
    if first in COMMAND_VERBS:
        return KIND_COMMAND
    parent = parents.get(row.ppid)
    if _flag_value(arguments, "--session-id") and parent is not None:
        if "--bg-pty-host" in _arguments(parent):
            return KIND_BACKGROUND
    if "--bg" in arguments:
        return KIND_COMMAND  # the launcher, which exits once the daemon has the session
    return KIND_INTERACTIVE


# ----- judging ------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """Everything :func:`judge` reads, gathered once so that it is a pure function."""

    processes: Sequence[ProcessRow]
    ledger: Sequence[Mapping[str, Any]]
    ledger_error: Optional[str]
    run_sessions: Mapping[str, Tuple[str, str]]
    """Session id (short or full) -> (run id, run status), for every AgentJobs run record."""
    transcripts: Callable[[str, str], Optional[Tuple[str, float]]]
    """(session uuid, cwd) -> (transcript path, mtime) or None."""
    identities: Callable[[int], Optional[str]]
    now: float
    self_pid: int


def _ancestors(pid: int, parents: Mapping[int, ProcessRow]) -> List[int]:
    chain: List[int] = []
    current = parents.get(pid)
    while current is not None and current.pid not in chain:
        chain.append(current.pid)
        current = parents.get(current.ppid)
    return chain


def _ledger_row(
    ledger: Sequence[Mapping[str, Any]], pid: int, session_uuid: Optional[str]
) -> Optional[Mapping[str, Any]]:
    for row in ledger:
        if session_uuid and str(row.get("sessionId") or "") == session_uuid:
            return row
    for row in ledger:
        if row.get("pid") is not None and str(row.get("pid")) == str(pid):
            return row
    return None


def _attached_targets(views: Iterable[Tuple[ProcessRow, str]]) -> List[str]:
    targets = []
    for row, kind in views:
        if kind != KIND_COMMAND:
            continue
        arguments = _arguments(row)
        if len(arguments) >= 2 and arguments[0] == "attach":
            targets.append(arguments[1])
    return targets


def _run_for(
    run_sessions: Mapping[str, Tuple[str, str]], *ids: Optional[str]
) -> Optional[Tuple[str, str]]:
    for key, value in run_sessions.items():
        for candidate in ids:
            if candidate and key and (candidate.startswith(key) or key.startswith(candidate)):
                return value
    return None


def duration_phrase(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes:02d}m"
    return f"{hours // 24}d {hours % 24}h"


def resume_commands(short_id: Optional[str], session_uuid: Optional[str]) -> List[str]:
    """How a stopped background session is brought back.

    ``claude stop`` keeps the conversation and names ``claude attach <id>`` as the way
    back (its own help text, 2.1.270). ``--bg --resume <uuid>`` resumes it without a
    terminal, which is what task-417's wake uses (entry 8).
    """
    commands = []
    if short_id:
        commands.append(f"claude attach {short_id}")
    if session_uuid:
        commands.append(f"claude --bg --resume {session_uuid}")
    return commands


def judge(evidence: Evidence, *, idle_minutes: int) -> List[SessionView]:
    """A verdict for every Claude Code process. Pure: all reading happened in ``evidence``."""
    parents = {row.pid: row for row in evidence.processes}
    claude_rows = [row for row in evidence.processes if is_claude_code(row)]
    kinds = [(row, classify_process(row, parents)) for row in claude_rows]
    attached = _attached_targets(kinds)
    own_chain = set(_ancestors(evidence.self_pid, parents)) | {evidence.self_pid}
    threshold = idle_minutes * 60
    views: List[SessionView] = []

    for row, kind in kinds:
        view = SessionView(
            pid=row.pid,
            ppid=row.ppid,
            kind=kind,
            verdict=VERDICT_PROTECTED,
            reason="",
            cmdline=row.cmdline,
            identity=evidence.identities(row.pid),
        )
        views.append(view)
        arguments = _arguments(row)
        session_uuid = _flag_value(arguments, "--session-id")
        if session_uuid and UUID_PATTERN.match(session_uuid):
            view.session_id = session_uuid
            view.short_id = session_uuid[:8]
        ledger_row = _ledger_row(evidence.ledger, row.pid, view.session_id)
        if ledger_row is not None:
            view.ledger_status = _text(ledger_row.get("status"))
            view.ledger_state = _text(ledger_row.get("state"))
            view.name = str(ledger_row.get("name") or "")
            view.cwd = str(ledger_row.get("cwd") or "")
            if not view.session_id and UUID_PATTERN.match(str(ledger_row.get("sessionId") or "")):
                view.session_id = str(ledger_row.get("sessionId"))
                view.short_id = str(ledger_row.get("id") or view.session_id[:8])
            elif ledger_row.get("id"):
                view.short_id = str(ledger_row.get("id"))

        if kind == KIND_DESKTOP:
            view.reason = "The Claude desktop app, or a session it started. Out of scope."
            continue
        if kind == KIND_DAEMON:
            view.reason = "The background-session daemon every bg session runs under."
            continue
        if kind == KIND_RC_HOST:
            prefix = _flag_value(arguments, "--remote-control-session-name-prefix")
            view.name = view.name or (f"Remote Control: {prefix}" if prefix else "")
            view.reason = "A Remote Control host. The owner relies on Remote Control."
            continue
        if kind == KIND_PTY_HOST:
            view.reason = "The terminal host of a background session; stopped with it."
            continue
        if kind == KIND_COMMAND:
            view.reason = "A short-lived command, not a session."
            continue
        if kind == KIND_INTERACTIVE:
            view.reason = "An interactive session in someone's terminal."
            continue
        if row.pid in own_chain:
            view.reason = "This is the session running the sweep."
            continue

        if kind == KIND_RC_CHILD:
            resumed = _flag_value(arguments, "--resume") or _flag_value(arguments, "--session-id")
            _read_activity(view, evidence, resumed if resumed else None)
            if view.idle_seconds is not None and view.idle_seconds >= threshold:
                view.verdict = VERDICT_REPORT_ONLY
                view.reason = (
                    f"A Remote Control conversation, quiet for {duration_phrase(view.idle_seconds)}. "
                    "Reported only: stopping one ends its work item on claude.ai, and whether "
                    "the phone can continue it has not been verified."
                )
            else:
                view.reason = (
                    "A Remote Control conversation"
                    + (
                        f", active {duration_phrase(view.idle_seconds)} ago"
                        if view.idle_seconds is not None
                        else ""
                    )
                    + ". Remote Control children are never stopped."
                )
            continue

        # A background session: every remaining rule has to be proven before it may stop.
        if evidence.ledger_error:
            view.reason = f"The session ledger could not be read: {evidence.ledger_error}"
            continue
        if ledger_row is None:
            view.reason = "Not in the session ledger, so its state is unknown."
            continue
        run = _run_for(evidence.run_sessions, view.session_id, view.short_id)
        if run is not None:
            view.run_id = run[0]
            view.reason = (
                f"An AgentJobs run ({run[0]}, {run[1]}); the dispatcher manages its session."
            )
            continue
        if AGENTJOBS_SESSION_NAME.match(view.name):
            view.reason = "Named like an AgentJobs dispatch; left to the dispatcher."
            continue
        if any(
            target
            and (
                (view.short_id or "-").startswith(target)
                or (view.session_id or "-").startswith(target)
            )
            for target in attached
        ):
            view.verdict = VERDICT_IN_USE
            view.reason = "Attached in a terminal right now."
            continue
        status = view.ledger_status
        if status == "busy":
            view.verdict = VERDICT_IN_USE
            view.reason = "Busy: the ledger says it is working."
            continue
        if status == "waiting":
            view.verdict = VERDICT_IN_USE
            view.reason = "Blocked: waiting for an answer."
            continue
        if status != "idle":
            view.reason = f"Ledger status {status or 'missing'!r} is not one the sweep trusts."
            continue
        if not view.session_id:
            view.reason = "No full session id, so it could not be resumed."
            continue
        _read_activity(view, evidence, view.session_id)
        if view.transcript is None or view.idle_seconds is None:
            view.reason = "No transcript found, so it could not be resumed."
            continue
        view.resume_commands = resume_commands(view.short_id, view.session_id)
        quiet = duration_phrase(view.idle_seconds)
        if view.idle_seconds < threshold:
            view.verdict = VERDICT_IDLE
            view.reason = (
                f"Idle; transcript quiet for {quiet}, under the "
                f"{duration_phrase(threshold)} threshold."
            )
            continue
        view.verdict = VERDICT_CANDIDATE
        view.reason = (
            f"Ledger says idle and the transcript has been quiet for {quiet}, past the "
            f"{duration_phrase(threshold)} threshold. Resumable from its transcript."
        )
    return views


def _read_activity(view: SessionView, evidence: Evidence, session_uuid: Optional[str]) -> None:
    if not session_uuid:
        return
    found = evidence.transcripts(session_uuid, view.cwd)
    if found is None:
        return
    path, mtime = found
    view.transcript = path
    view.last_activity = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    view.idle_seconds = max(0, int(evidence.now - mtime))


def _text(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


# ----- reading the machine ------------------------------------------------------------

_WINDOWS_PROCESS_SCRIPT = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process | ForEach-Object { "
    "$k = ($_.Name -eq 'claude.exe') -or ($_.Name -eq 'node.exe'); "
    "[pscustomobject]@{ p = $_.ProcessId; q = $_.ParentProcessId; n = $_.Name; "
    "e = $(if ($k) { $_.ExecutablePath } else { '' }); "
    "c = $(if ($k) { $_.CommandLine } else { '' }) } } | ConvertTo-Json -Compress"
)


def process_table() -> List[ProcessRow]:
    """Every process on the machine; command lines only for ones that could be Claude Code.

    Windows through ``Win32_Process`` (one PowerShell call), elsewhere through ``ps``. Only
    command lines are read, never environments: an environment can carry a token.
    """
    if os.name == "nt":
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_PROCESS_SCRIPT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            raise OSError(f"Win32_Process query failed: {(completed.stderr or '').strip()[:300]}")
        loaded = json.loads(completed.stdout)
        if isinstance(loaded, dict):
            loaded = [loaded]
        return [
            ProcessRow(
                pid=int(item.get("p") or 0),
                ppid=int(item.get("q") or 0),
                name=str(item.get("n") or ""),
                exe=str(item.get("e") or ""),
                cmdline=str(item.get("c") or ""),
            )
            for item in loaded
            if isinstance(item, dict)
        ]
    completed = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,args="],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(f"ps failed: {(completed.stderr or '').strip()[:300]}")
    rows = []
    for line in completed.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        cmdline = parts[2] if len(parts) > 2 else ""
        exe = split_command_line(cmdline)[:1]
        rows.append(
            ProcessRow(
                pid=int(parts[0]),
                ppid=int(parts[1]),
                name=(exe[0].rsplit("/", 1)[-1] if exe else ""),
                exe=exe[0] if exe else "",
                cmdline=cmdline,
            )
        )
    return rows


def command_line_of(
    pid: int, table: Callable[[], List[ProcessRow]] = process_table
) -> Optional[str]:
    """This pid's command line right now, or None when it is gone."""
    for row in table():
        if row.pid == pid:
            return row.cmdline
    return None


# There was a `live_session_names` here, and task-500 removed it rather than leaving it.
#
# It projected `peers.roster` down to a set of names for its one caller,
# `runner.choose_session_name`, on the argument that two readers of one undocumented file
# layout is one too many. That caller now needs a row's `cwd` as well as its name -- it is
# what says which project an unprefixed `task-499` belongs to -- so a name-only projection
# no longer serves it, and it reads `peers.roster` directly, which is the module that owns
# the layout anyway. A projection kept for nobody, with a docstring naming a caller that
# has stopped calling it, is worse than no projection.


def ledger_rows(executable: str = "claude") -> List[Dict[str, Any]]:
    """``claude agents --json``: live sessions, interactive ones included. Raises on failure.

    Without ``--all``, which adds finished sessions: a process that is running is by
    definition not finished, and the smaller listing is the cheaper read.
    """
    from agentjobs.dispatch.runner import resolve_executable

    completed = subprocess.run(
        [resolve_executable(executable), "agents", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if completed.returncode != 0 or not (completed.stdout or "").strip():
        raise OSError(
            "claude agents --json failed or printed nothing: "
            f"{(completed.stderr or '').strip()[:300]}"
        )
    loaded = json.loads(completed.stdout)
    if isinstance(loaded, dict):
        loaded = loaded.get("agents") or loaded.get("sessions") or []
    return [row for row in loaded if isinstance(row, dict)]


def transcript_activity(session_uuid: str, cwd: str) -> Optional[Tuple[str, float]]:
    from agentjobs.dispatch.transcript import find_session_transcript

    path = find_session_transcript(session_uuid, Path(cwd or "."))
    if path is None:
        return None
    try:
        return str(path), path.stat().st_mtime
    except OSError:
        return None


def run_sessions(home: Path) -> Dict[str, Tuple[str, str]]:
    from agentjobs.dispatch.ledger import list_runs

    found: Dict[str, Tuple[str, str]] = {}
    for record in list_runs(home):
        if record.session_id and record.session_id not in found:
            found[record.session_id] = (record.run_id, record.status)
    return found


def stop_background_session(short_id: str, executable: str = "claude") -> Tuple[bool, str]:
    """``claude stop <id>``: the conversation is kept (the command's own help text)."""
    from agentjobs.dispatch.runner import resolve_executable

    try:
        completed = subprocess.run(
            [resolve_executable(executable), "stop", short_id],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run claude stop: {exc}"
    detail = ((completed.stdout or "") + (completed.stderr or "")).strip()[:300]
    return completed.returncode == 0, detail or f"exit {completed.returncode}"


@dataclass
class SweepDeps:
    """The machine, injectable. Tests replace every field; production uses the defaults."""

    processes: Callable[[], List[ProcessRow]] = process_table
    ledger: Callable[[], List[Dict[str, Any]]] = ledger_rows
    transcripts: Callable[[str, str], Optional[Tuple[str, float]]] = transcript_activity
    identity: Callable[[int], Optional[str]] = field(default_factory=lambda: _identity)
    alive: Callable[[int], bool] = field(default_factory=lambda: _alive)
    stop: Callable[[str], Tuple[bool, str]] = stop_background_session
    runs: Optional[Callable[[], Mapping[str, Tuple[str, str]]]] = None
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    self_pid: int = field(default_factory=os.getpid)


def _identity(pid: int) -> Optional[str]:
    from agentjobs.dispatch.ledger import process_identity

    return process_identity(pid)


def _alive(pid: int) -> bool:
    from agentjobs.dispatch.ledger import process_alive

    return process_alive(pid)


def take_inventory(home: Path, *, idle_minutes: int, deps: Optional[SweepDeps] = None) -> Inventory:
    """Read the machine once and judge every Claude Code process on it."""
    deps = deps or SweepDeps()
    errors: List[str] = []
    processes = deps.processes()
    ledger: List[Dict[str, Any]] = []
    ledger_error: Optional[str] = None
    needs_ledger = any(is_claude_code(row) and "--session-id" in row.cmdline for row in processes)
    if needs_ledger:
        try:
            ledger = deps.ledger()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            ledger_error = str(exc)
            errors.append(f"ledger: {exc}")
    runs: Mapping[str, Tuple[str, str]] = {}
    try:
        runs = deps.runs() if deps.runs is not None else run_sessions(home)
    except OSError as exc:  # pragma: no cover - an unreadable runs directory
        errors.append(f"runs: {exc}")
        ledger_error = ledger_error or f"run records unreadable: {exc}"
    evidence = Evidence(
        processes=processes,
        ledger=ledger,
        ledger_error=ledger_error,
        run_sessions=runs,
        transcripts=deps.transcripts,
        identities=deps.identity,
        now=deps.now(),
        self_pid=deps.self_pid,
    )
    return Inventory(
        generated_at=datetime.fromtimestamp(evidence.now, tz=timezone.utc).isoformat(),
        sessions=judge(evidence, idle_minutes=idle_minutes),
        idle_minutes=idle_minutes,
        errors=errors,
    )


# ----- the record ---------------------------------------------------------------------


@dataclass
class IdleSessionEvent:
    event_id: str
    kind: str
    at: str
    session_id: Optional[str]
    outcome: str
    detail: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class IdleSessionBook:
    """The ``idle_session_event`` table: every stop, and every change of mode."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def events(self, *, limit: int = EVENT_LIMIT) -> List[IdleSessionEvent]:
        rows = self.store.read(
            "SELECT * FROM idle_session_event ORDER BY at DESC, event_id DESC LIMIT ?",
            (int(limit),),
        )
        return [_event(row) for row in rows]

    def last_mode(self) -> Optional[IdleSessionEvent]:
        rows = self.store.read(
            "SELECT * FROM idle_session_event WHERE kind = 'mode' "
            "ORDER BY at DESC, event_id DESC LIMIT 1"
        )
        return _event(rows[0]) if rows else None

    def record(
        self,
        kind: str,
        outcome: str,
        detail: Mapping[str, Any],
        *,
        session_id: Optional[str] = None,
        at: Optional[str] = None,
    ) -> IdleSessionEvent:
        event = IdleSessionEvent(
            event_id=f"ise_{uuid.uuid4().hex[:12]}",
            kind=kind,
            at=at or dispatch_clock.utcnow().isoformat(),
            session_id=session_id,
            outcome=outcome,
            detail=dict(detail),
        )
        with self.store.transaction("idle-session-event") as conn:
            conn.execute(
                "INSERT INTO idle_session_event(event_id, kind, at, session_id, outcome, "
                "detail_json) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.kind,
                    event.at,
                    event.session_id,
                    event.outcome,
                    json.dumps(event.detail, sort_keys=True),
                ),
            )
        return event


def _event(row: Any) -> IdleSessionEvent:
    try:
        detail = json.loads(row["detail_json"] or "{}")
    except (TypeError, ValueError):
        detail = {}
    return IdleSessionEvent(
        event_id=row["event_id"],
        kind=row["kind"],
        at=row["at"],
        session_id=row["session_id"],
        outcome=row["outcome"],
        detail=detail if isinstance(detail, dict) else {},
    )


def book_for(home: Path) -> IdleSessionBook:
    from agentjobs.dispatch.journal import journal

    return IdleSessionBook(journal(home))


def auth_incident_count(store: Any) -> Optional[int]:
    """How many auth incidents the journal holds: task-440's before/after measure."""
    try:
        rows = store.read("SELECT COUNT(*) AS n FROM auth_incident")
    except Exception:  # noqa: BLE001 - a count is evidence, never a reason to fail
        return None
    return int(rows[0]["n"]) if rows else None


def note_mode(
    book: IdleSessionBook, *, enforce: bool, idle_minutes: int
) -> Optional[IdleSessionEvent]:
    """Record a change of enforcement mode, with the auth incident count at that moment.

    Nothing is recorded while enforcement has never been on: off is the default, and a
    record of the default would say nothing. The first ``on`` and every later flip are the
    switch-over points task-440 compares login-loss frequency across.
    """
    last = book.last_mode()
    wanted = "enforce" if enforce else "report"
    if last is None and not enforce:
        return None
    if last is not None and last.outcome == wanted:
        return None
    return book.record(
        "mode",
        wanted,
        {"idle_minutes": idle_minutes, "auth_incidents": auth_incident_count(book.store)},
    )


# ----- stopping -----------------------------------------------------------------------


@dataclass
class StopReport:
    session: SessionView
    outcome: str
    detail: str


def stop_candidate(
    view: SessionView,
    *,
    book: Optional[IdleSessionBook],
    idle_minutes: int,
    deps: SweepDeps,
    trigger: str,
) -> StopReport:
    """Stop one candidate after proving, again and immediately, that it is still one.

    Four re-checks, each of which declines rather than stops:

    1. the pid still has the creation time the inventory saw -- a reused pid does not;
    2. the pid still has the same command line;
    3. the ledger still says ``idle`` for this session;
    4. the transcript has not been written since the inventory read it.

    The outcome is recorded whatever it was, so a declined stop is as visible as a real one.
    """

    def declined(reason: str) -> StopReport:
        return _recorded(book, view, "declined", reason, idle_minutes, trigger)

    if view.verdict != VERDICT_CANDIDATE or not view.short_id or not view.session_id:
        return declined("not a candidate")
    identity = deps.identity(view.pid)
    if view.identity is None or identity != view.identity:
        return declined(f"pid {view.pid} is no longer the process the inventory saw")
    current = command_line_of(view.pid, deps.processes)
    if current != view.cmdline:
        return declined(f"pid {view.pid}'s command line changed")
    try:
        row = _ledger_row(deps.ledger(), view.pid, view.session_id)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return declined(f"the ledger could not be re-read: {exc}")
    if row is None or row.get("status") != "idle":
        status = row.get("status") if row is not None else "absent"
        return declined(f"the ledger now says {status!r}")
    activity = deps.transcripts(view.session_id, view.cwd)
    if activity is None or view.last_activity is None:
        return declined("the transcript can no longer be read")
    if datetime.fromtimestamp(activity[1], tz=timezone.utc).isoformat() != view.last_activity:
        return declined("the transcript was written since the inventory")

    ok, detail = deps.stop(view.short_id)
    if not ok:
        return _recorded(book, view, "failed", detail, idle_minutes, trigger)
    deadline = deps.now() + STOP_QUIESCE_SECONDS
    while deps.alive(view.pid) and deps.identity(view.pid) == view.identity:
        if deps.now() >= deadline:
            return _recorded(
                book,
                view,
                "failed",
                f"claude stop succeeded but pid {view.pid} was still running after "
                f"{STOP_QUIESCE_SECONDS}s",
                idle_minutes,
                trigger,
            )
        deps.sleep(1.0)
    return _recorded(book, view, "stopped", detail, idle_minutes, trigger)


def _recorded(
    book: Optional[IdleSessionBook],
    view: SessionView,
    outcome: str,
    detail: str,
    idle_minutes: int,
    trigger: str,
) -> StopReport:
    if book is not None:
        book.record(
            "stop",
            outcome,
            {
                "detail": detail,
                "trigger": trigger,
                "name": view.name,
                "cwd": view.cwd,
                "pid": view.pid,
                "short_id": view.short_id,
                "last_activity": view.last_activity,
                "idle_seconds": view.idle_seconds,
                "idle_minutes": idle_minutes,
                "reason": view.reason,
                "transcript": view.transcript,
                "resume_commands": resume_commands(view.short_id, view.session_id),
            },
            session_id=view.session_id,
        )
    return StopReport(view, outcome, detail)


@dataclass
class SweepResult:
    inventory: Optional[Inventory]
    stops: List[StopReport]
    lines: List[str]


def sweep(
    home: Path,
    *,
    enforce: bool,
    idle_minutes: int,
    max_stops: int,
    book: Optional[IdleSessionBook] = None,
    deps: Optional[SweepDeps] = None,
) -> SweepResult:
    """One sweep. With ``enforce`` false it reads nothing and stops nothing."""
    deps = deps or SweepDeps()
    if book is not None:
        note_mode(book, enforce=enforce, idle_minutes=idle_minutes)
    if not enforce:
        return SweepResult(None, [], [])
    inventory = take_inventory(home, idle_minutes=idle_minutes, deps=deps)
    lines = [f"idle-sessions: {error}" for error in inventory.errors]
    stops = []
    for view in inventory.candidates[:max_stops]:
        report = stop_candidate(
            view, book=book, idle_minutes=idle_minutes, deps=deps, trigger="sweep"
        )
        stops.append(report)
        lines.append(
            f"idle-sessions: {report.outcome} {view.short_id} ({view.name or view.cwd}): "
            f"{report.detail}"
        )
    return SweepResult(inventory, stops, lines)


_last_sweep: Dict[str, float] = {}


def tick(home: Path, *, deps: Optional[SweepDeps] = None, force: bool = False) -> List[str]:
    """The poller's step: at most once per :data:`SWEEP_INTERVAL_SECONDS` per home."""
    from agentjobs.dispatch.config import load_dispatch_config

    key = str(home)
    now = time.monotonic()
    if not force and now - _last_sweep.get(key, -SWEEP_INTERVAL_SECONDS) < SWEEP_INTERVAL_SECONDS:
        return []
    _last_sweep[key] = now
    config = load_dispatch_config(home)
    if config is None:
        return []
    settings = config.idle_sessions
    book = book_for(home)
    result = sweep(
        home,
        enforce=settings.enforce,
        idle_minutes=settings.idle_minutes,
        max_stops=settings.max_stops_per_sweep,
        book=book,
        deps=deps,
    )
    return result.lines
