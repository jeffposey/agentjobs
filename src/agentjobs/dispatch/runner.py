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

from agentjobs.dispatch.address import resolve_api_base
from agentjobs.dispatch.auth import AuthStall, read_auth_stall
from agentjobs.dispatch.config import (
    DispatchResolution,
    DispatchRunner as RunnerConfig,
    Posture,
    RunnerMode,
    RunnerSelection,
    sentinel_active,
    substitute_argv,
)
from agentjobs.dispatch.record_commit import CommitOutcome, commit_task_record
from agentjobs.dispatch.wake import (
    WakeError,
    WakeTarget,
    build_wake_prompt,
    find_wake_target,
    wake_argv,
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
from agentjobs.dispatch.phases import RUN_DIR_ENV, RUN_ID_ENV
from agentjobs.project_setup import MCP_CONFIG_FILENAME

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
    "(root: {project_root}). You are running in that project's shared working tree and "
    "are NOT isolated. Before writing anything, run `git worktree add "
    "../worktrees/<repo>-<nnn> "
    "-b <type>/{task_id}-<slug>` and work from that path. Use that shell command, not "
    "a built-in worktree tool: those relocate the session's permission root, which "
    "parks a background run on a prompt nobody can answer. AgentJobs is serving at "
    "{api_base}. Read the task record and follow the resumption contract in "
    + GUIDE_PATH
    + ". Dispatch run id: {run_id}."
)
"""Fixed text plus five substitutions, and deliberately almost nothing more.

The resumption contract already guarantees the record is sufficient to resume from, so
the payload is a pointer to where the context is, not a copy of it. Composing a richer
prompt would put the contract in a second place and guarantee the two disagree.

The worktree paragraph is the one exception, and it is a considered one (task-186). Until
2026-08-19 ``posture_flags`` passed ``-w`` and containment was mechanical, so the stub
had nothing to say about it. It cannot pass ``-w`` any more -- the isolation that flag
buys carries a guard refusing every git operation aimed at the shared clone, which is
where this project requires task records to be committed and where the merge gate runs.
Containment is therefore the agent's own act, and it is the **only** instruction that
must be obeyed before the agent reads anything, the guide included. A pointer cannot
carry an instruction that has to precede following the pointer, so this one line is
stated here as well as in the guide.

**It names the shell command and forbids the built-in tool, and that phrasing is the
whole of task-192.** The clause first shipped as prose -- "take your own git worktree" --
which a model satisfies with Claude Code's ``EnterWorktree`` tool, the tool built for
exactly that sentence. That tool asks to relocate the session's permission root outside
``.claude/worktrees/``; under ``--permission-mode auto`` the classifier declines the
escalation, defensibly, and a ``--bg`` session has no terminal to answer with, so the run
parks indefinitely. Observed 2026-08-20 on run_6f1f0741, the first dispatch after
task-186 merged, which parked before it wrote a line. The posture cannot fix it -- see
``posture_flags`` for why neither ``-w`` nor ``bypassPermissions`` is the answer -- so
the prompt has to be specific about *how*: ``git worktree add`` needs no relocation at
all, and it is what ALLAGENTS.md already tells every other agent in this repository to
do. This is why the stub is longer than a pointer ought to be; brevity that reintroduces
a hang is not economy.

The rendered prompt is still asserted to be short and to not restate the record, which
is the property that matters. It is not asserted to be minimal."""

SUPERVISOR_STUB = (
    "You are the agent `{agent}` supervising parent task `{task_id}` in project "
    "`{project_id}` (root: {project_root}). It has open children: {children}. "
    "You are the supervisor, not the worker: start a separate session for one eligible "
    "child at a time and let that session do the child's work. Do not work a child "
    "yourself, do not take a worktree, and check nothing out -- you stay in the shared "
    "working tree as it is, and each child session takes its own. AgentJobs is serving "
    "at {api_base}. Read the parent record, then follow the parent-task protocol in "
    + GUIDE_PATH
    + ". Dispatch run id: {run_id}."
)
"""The stub for a task that has open children. Task-164.

**Which stub a run gets is decided by the record, not by the dispatcher's opinion**: a
task with an open child is an epic, and an epic's worker is a supervisor. That is the
one checkable property Jeff's formulation reduces to -- "anything that is starting with
a new worktree should be in a new session" -- and it needs no new field, no label
somebody has to remember to set, and no judgement at spawn time.

It says the opposite of ``PROMPT_STUB`` about worktrees, and that inversion is the whole
reason this is a second stub rather than an extra sentence. A supervisor that obeyed the
worktree paragraph would check out a branch in the shared clone, which is the collision
ALLAGENTS.md's worktree rule exists to prevent, and would then commit the parent's task
records somewhere the dashboard cannot see them. A supervisor writes no code, so it
needs no isolation; what it needs is to be told, before it reads anything, that the
first act the other stub demands is not its act. That is the same argument task-192 made
for stating the worktree command here, applied in reverse.

The children are named rather than counted because the supervisor's first decision is
which one is eligible, and a count sends it back to the API for something the prompt
could have carried for nothing. Named, not described: what each child *is* stays in its
own record, per the pointer-not-composition rule above."""

CHILDREN_NAMED = 8
"""How many child ids the supervisor stub lists before it summarises the rest.

A ceiling on prompt length rather than a considered number. Eight covers every parent in
this repository's corpus; a wider epic gets ``and N more``, and the supervisor reads the
rest from the record it is about to open anyway.
"""

GRACE_SECONDS = 30.0
"""How long a cancelled batch run gets to finish a ``git commit`` before it is killed."""

OUTPUT_TAIL_LINES = 40
"""Lines of run output inlined into a non-success ``dispatch_result``.

On success the body stays empty: the agent's own entries carry the substance. On any
other outcome the machine-local logs are the only account of what happened, and they are
not in git, so a tail of them goes into the entry that is.
"""


def describe_children(child_ids: Sequence[str]) -> str:
    """The children clause of the supervisor stub: ids, capped, in one phrase.

    Returns ``"none"`` for an empty sequence. Nothing renders that today -- the empty
    case picks the other stub -- but a helper that returns ``""`` for "no children"
    produces a sentence reading "It has open children: ." the first time somebody calls
    it from anywhere else.
    """
    ids = list(child_ids)
    if not ids:
        return "none"
    if len(ids) <= CHILDREN_NAMED:
        return ", ".join(ids)
    shown = ", ".join(ids[:CHILDREN_NAMED])
    return f"{shown} and {len(ids) - CHILDREN_NAMED} more"


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
    "git merge",
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


def supervisor_allow_rules(mcp_servers: Sequence[str]) -> List[str]:
    """Pre-approve a project's own MCP servers, for the supervisor role only (task-220).

    **Server-level, not per-tool.** ``mcp__agentjobs`` matches every tool that server
    provides, including ones it grows later, so this list cannot go stale the way an
    enumeration would -- the failure mode ``allow_rules()`` records as the colon lesson,
    arrived at from the other direction.

    **Why a supervisor needs it and an ordinary run does not**, from evidence rather
    than caution. run_d5ab5caf parked on 2026-08-21 before it ever launched a child; the
    three consecutive classifier blocks that armed the breaker were two identical
    ``task_log_append`` calls and one unrelated help command. Those two were the
    supervisor writing a *brief* into a child's log -- which child runs, under what merge
    posture, on whose authority. An agent instructing another agent to skip human review
    and merge to ``main`` is exactly what a content classifier should decline when the
    authorisation it is acting on lives somewhere the classifier cannot see: on the
    parent's task record. An ordinary run writes progress notes and never says any such
    thing, which is why six of eight runs that night completed untouched.

    So this grants the coordination channel to the role whose entire job is coordination,
    and to nothing else. It is not a widening of what a dispatched agent may *do* -- MCP
    writes are append-only and audited, and the same tools are already the sanctioned
    mutation interface -- it is a statement that the project's own task tracker does not
    need adjudicating call by call.
    """
    return [f"mcp__{name}" for name in mcp_servers]


def mcpjson_server_names(project_root: Path) -> List[str]:
    """The server names a project declares in its own ``.mcp.json``, in file order.

    Claude Code prompts the first time it finds a project-scoped MCP server, and a
    ``--bg`` session has no terminal to answer with. It renders *"New MCP server found
    in this project"* with three numbered choices and sits there: ``claude agents
    --json`` reports ``state: "blocked"``. run_08ddfa02, the first real dispatch ever
    attempted, burned 913 seconds that way and did no work. There is no CLI verb to
    approve one non-interactively -- ``claude mcp`` has add/remove/list/get/
    reset-project-choices and nothing else.

    So the names are read here and travel with the run in ``--settings``, which is one
    of the three approval sources that apply regardless of folder trust. **Read from the
    project, never hardcoded**: dispatch runs against whatever project it was configured
    for, and naming AgentJobs' own server here would fix exactly one of them.

    Returns ``[]`` for a missing, unreadable, non-JSON or serverless file. Dispatch does
    not own this file and refusing to spawn over it would turn a cosmetic problem into
    an outage; a file no name can be read out of also yields no name Claude Code could
    prompt about, so argv is left exactly as it was.
    """
    try:
        raw = (project_root / MCP_CONFIG_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    servers = parsed.get("mcpServers") if isinstance(parsed, dict) else None
    if not isinstance(servers, dict):
        return []
    return [name for name in servers if isinstance(name, str) and name]


def settings_json(*, allow_list: bool, mcp_servers: Sequence[str], supervisor: bool = False) -> str:
    """The blob ``--settings`` takes, carrying only the keys a run actually needs.

    Two independent things end up here and they are wanted by different postures. The
    allow-list pre-approves commands; ``enabledMcpjsonServers`` pre-approves the
    project's MCP servers. ``read_only`` needs the second and must not be given the
    first, so neither key is unconditional.

    ``supervisor`` adds ``supervisor_allow_rules()`` to the allow-list, and does so
    **only inside the branch that already carries one**. That placement is the whole
    safety property: ``read_only`` and ``autonomous`` never reach it, so an epic cannot
    quietly widen a posture chosen to be narrow.

    An empty ``mcp_servers`` produces exactly the JSON this emitted before the MCP key
    existed, byte for byte, which is what keeps a project without a ``.mcp.json`` on
    unchanged argv -- and a supervisor of such a project emits that same blob, because
    there is no server to name.
    """
    settings: Dict[str, object] = {}
    if allow_list:
        rules = allow_rules()
        if supervisor:
            rules = rules + supervisor_allow_rules(mcp_servers)
        settings["permissions"] = {"allow": rules}
    if mcp_servers:
        settings["enabledMcpjsonServers"] = list(mcp_servers)
    return json.dumps(settings)


def posture_flags(
    posture: Posture, mcp_servers: Sequence[str], *, supervisor: bool = False
) -> List[str]:
    """The flags that decide what a run may do, per task-076.

    **AgentJobs owns these, not the operator.** Mechanically they are just more argv,
    which makes them look like the runner's business; they are the actual risk boundary
    of the whole feature, and burying them in a config example means they get chosen by
    whoever copies the example first.

    **No posture passes ``-w``, and that is the whole of task-186.** Until 2026-08-19
    every writing posture did, on the reasoning that a dispatched run should not be able
    to *forget* to take a worktree. What that reasoning did not know is that the
    isolation ``-w`` grants is enforced by a guard which refuses any git operation aimed
    at the shared checkout -- by ``-C`` and by ``cd`` alike, both probed on 2.1.235, with
    no flag or setting that lifts either. This project commits every task record to
    ``main`` in that shared checkout and runs its merge gate there, so a ``-w`` run could
    do the work and then not record or merge it. Containment that guarantees the run
    cannot finish is not containment. It is now the agent's own act -- ``PROMPT_STUB``
    gives the ``git worktree add`` command in the first lines guaranteed to be read, and
    the guide it points at says it in full.

    **Nor does any posture pre-approve a permission-root relocation, and that is
    task-192.** Claude Code's ``EnterWorktree`` tool -- which a prose instruction to
    "take a worktree" invites -- asks to move the session's permission root outside
    ``.claude/worktrees/``. ``auto``'s classifier declines that escalation and a ``--bg``
    run cannot answer, so it parks. Rejected pre-approving it in the ``--settings`` blob
    beside ``enabledMcpjsonServers``: it would need a rule for a gate that is an
    escalation rather than an ordinary tool call, and ``allow_rules()`` records what a
    rule that silently matches nothing costs. ``bypassPermissions`` is likewise rejected
    -- it removes the gate for everything to fix one prompt. The prompt names the shell
    command instead, which needs no approval at all.

    ``read_only`` still gets no worktree flag, for the reason it never had one: it cannot
    write anything to one.

    **Every posture that can hit the ``.mcp.json`` approval dialog carries the project's
    server names (task-019).** Probed on 2.1.235 against a project declaring one
    otherwise-unknown server: ``auto`` and ``read_only`` both reach ``state: "blocked"``
    on *"New MCP server found in this project"*, and ``bypassPermissions`` does not see
    the gate at all. So ``read_only`` gains a ``--settings`` blob it never had -- holding
    ``enabledMcpjsonServers`` and nothing else, because giving it an allow-list would be
    a posture change -- ``auto`` and ``supervised`` gain the key in the blob they already
    had, and ``autonomous`` is untouched, since adding an approval it demonstrably does
    not need would only imply a limit that is not there. A project with no ``.mcp.json``
    yields no names and every posture's argv is unchanged.

    ``auto`` is the default (task-020). Its mode has a classifier review each action
    instead of a human. ``supervised`` was the default until 2026-08-19 and could not
    finish work: ``acceptEdits`` still prompts for Bash, the allow-list covers nine
    prefixes, and the first command outside them parks a session nobody can answer.

    This paragraph used to end that first sentence with "which is the only one of these
    that both keeps a gate and never needs a terminal". **That was false**, and it is the
    assumption task-220 was spent discovering. ``auto`` needs a terminal too, just later
    and more rarely: a single classifier block is deny-and-continue, but *three
    consecutive* blocks arm a breaker that turns the next call into an interactive prompt
    -- and a ``--bg`` run has nobody to answer it. Six of eight runs on 2026-08-20/21
    never came near that, which is why the belief survived as long as it did. The fix is
    not a different mode; it is making sure the streak cannot form for the role that
    provokes it.

    ``supervisor`` says this run drives an epic. It is derived from the record -- the task
    has an open child -- by the same property that chooses ``SUPERVISOR_STUB``, so the two
    cannot disagree and nothing has to be remembered at spawn time. It reaches only the
    allow-list branch below, so ``read_only`` and ``autonomous`` are untouched by it.

    ``auto`` keeps the allow-list. The rules can only pre-approve, never widen beyond
    what the classifier would already permit, and every one of them names a command the
    run is certain to need -- so they cost nothing and spare the classifier the whole
    test suite. Rejected the alternative of dropping it, which would have made ``auto``
    differ from ``supervised`` in two ways at once and left the first ``pytest`` of every
    run waiting on a classifier round-trip for no benefit.
    """
    if posture is Posture.READ_ONLY:
        flags = ["--tools", "Read,Glob,Grep,WebFetch"]
        if mcp_servers:
            flags += ["--settings", settings_json(allow_list=False, mcp_servers=mcp_servers)]
        return flags
    if posture is Posture.AUTONOMOUS:
        return ["--permission-mode", "bypassPermissions"]
    return [
        "--permission-mode",
        "auto" if posture is Posture.AUTO else "acceptEdits",
        "--settings",
        settings_json(allow_list=True, mcp_servers=mcp_servers, supervisor=supervisor),
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


def _git(project_root: Path, *args: str) -> Optional[str]:
    """Run a read-only git command in a project, or ``None`` if git could not answer."""
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""


def _is_within(path: Path, parents: Sequence[Path]) -> bool:
    """True when *path* is one of *parents* or sits underneath one of them.

    Compared as normalised case, because this runs on Windows as often as not and
    ``Tasks`` and ``tasks`` are the same directory there.
    """
    candidate = os.path.normcase(str(path))
    for parent in parents:
        base = os.path.normcase(str(parent))
        if candidate == base or candidate.startswith(base + os.sep):
            return True
    return False


def uncommitted_paths(project_root: Path, *, ignore: Sequence[Path] = ()) -> Optional[List[str]]:
    """Repo-relative paths git reports as uncommitted, minus anything under *ignore*.

    ``None`` means git could not be read at all -- no repository, no git on PATH, a
    timeout. That is a different answer from "nothing is uncommitted", and callers must
    treat it as unclean: dispatch's default is to refuse on a dirty tree, and "we could
    not tell" belongs on the refusing side of that.

    *ignore* exists because AgentJobs writes into the very tree it is inspecting. A
    project that keeps its task records in the repository being dispatched -- this one
    does -- has its tasks directory dirtied by dispatch itself: the claim writes the task
    YAML before the spawn, and the terminal ``dispatch_result`` entry is written after the
    run's last commit. Counting those made the check refuse every dispatch on the strength
    of its own writes (task-182). Excluding them is the price of the check meaning anything
    at all; see the design doc for what that costs.
    """
    status = _git(project_root, "status", "--porcelain", "-z")
    if status is None:
        return None
    entries = _parse_porcelain_z(status)
    if not entries:
        return []
    if not ignore:
        return entries

    toplevel = _git(project_root, "rev-parse", "--show-toplevel")
    if toplevel is None:
        return None
    # Porcelain paths are relative to the repository root whatever directory git was run
    # from, so they are resolved against that rather than against ``project_root``.
    root = Path(toplevel.strip()).resolve()
    excluded = [Path(path).resolve() for path in ignore]
    return [entry for entry in entries if not _is_within((root / entry).resolve(), excluded)]


def _parse_porcelain_z(stdout: str) -> List[str]:
    """The paths out of ``git status --porcelain -z``.

    ``-z`` rather than the newline form because it is the only one that does not quote
    and escape unusual filenames, and a path this misparsed would be silently dropped
    from a safety check. Renames and copies emit the original path as a second
    NUL-terminated field, which is consumed and discarded: the new path already names the
    change.
    """
    fields = stdout.split("\0")
    paths: List[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry.strip():
            continue
        status, path = entry[:2], entry[3:]
        if status[:1] in {"R", "C"}:
            index += 1
        if path:
            paths.append(path)
    return paths


def working_tree_clean(project_root: Path, *, ignore: Sequence[Path] = ()) -> bool:
    """True when a project's working tree has nothing uncommitted outside *ignore*."""
    paths = uncommitted_paths(project_root, ignore=ignore)
    return paths is not None and not paths


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
    """What a poll concluded about a session, reduced to what dispatch acts on.

    All but one of these come from the ledger. ``AUTH_STALLED`` does not, and cannot:
    a session killed by an expired login reports ``idle``/``done`` like any session that
    finished its work, so the ledger cannot name it and the transcript has to. See
    ``dispatch.auth``.
    """

    RUNNING = "running"
    PARKED = "parked"
    AUTH_STALLED = "auth_stalled"
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
    api_base: Optional[str] = None
    """The AgentJobs address this run's agent was given.

    Surfaced because it is otherwise invisible until the agent fails to reach it: it is
    buried inside a prompt string, and the only symptom of a wrong one is a run that
    goes quiet. The CLI prints it for exactly that reason.
    """
    supervisor: Optional[threading.Thread] = field(default=None, repr=False)
    lock: Optional[object] = field(default=None, repr=False)
    """The per-task run lock, held for this run's lifetime and released when it ends.

    Typed ``object`` rather than ``RunLock`` only because ``ledger`` imports this
    module and the annotation would close the cycle.

    **A handle rebuilt from disk must set this too.** It is the one field on this class
    that is not recoverable from the run directory by reading, and a rebuilt handle that
    leaves it ``None`` silently declines to release -- see ``poller._handle_from``, which
    is where the leak that task-190 fixed actually lived. A ``RunLock`` is a task id and
    a path, so constructing one for a run you did not start is cheap and correct; the
    release itself refuses to delete a lock that has come to name a different run.
    """

    def release_lock(self) -> None:
        """Release the run lock this run holds. Safe to call twice, and safe to call
        from a handle that was rebuilt rather than the one that took the lock."""
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
        api_base: Optional[str] = None,
        grace_seconds: float = GRACE_SECONDS,
        clock: Callable[[], datetime] = utcnow,
        claude_home: Optional[Path] = None,
    ) -> None:
        self.manager = manager
        self.resolution = resolution
        self.project_root = Path(project_root)
        self.home = Path(home)
        self.api_base = resolve_api_base(api_base, home=self.home)
        """The address a dispatched agent is told to use, resolved exactly once.

        Resolved here rather than defaulted by every caller: this is the only object
        that puts the value into a prompt or an ``{api_base}`` argv element, so making
        it the single resolution point is what stops the manual, auto and CLI paths from
        drifting apart. ``None`` means "nobody upstream knows" -- the caller with a
        request to derive from passes a string, and everyone else passes nothing.
        """
        self.grace_seconds = grace_seconds
        self.clock = clock
        self.claude_home = claude_home
        """Where to look for session transcripts when checking for an expired login.

        ``None`` means "wherever Claude Code keeps them", which is what every caller
        outside a test wants -- see ``dispatch.auth.claude_home``. It is a parameter at
        all so a test can point the check at a directory it wrote itself, rather than at
        the machine's real session history.
        """

    # ----- shared ------------------------------------------------------------

    @property
    def runner(self) -> RunnerConfig:
        """The resolved runner definition."""
        return self.resolution.runner

    def _group_name(self) -> Optional[str]:
        """The group this run's runner came from, or ``None`` on a flat configuration."""
        selection = self.resolution.selection
        return selection.group if selection else None

    def open_child_ids(self, task_id: str) -> List[str]:
        """The ids of this task's still-open children, sorted, or ``[]``.

        Empty for a task with no children, for one whose children are all closed, and
        for an id storage cannot resolve. The last case is deliberate rather than
        careless: this is read to *decorate a prompt*, and a task whose children cannot
        be listed is dispatched as an ordinary task rather than not dispatched at all.
        """
        try:
            children = self.manager.get_subtasks(task_id)
        except Exception:  # pragma: no cover - a missing task cannot reach here
            return []
        return sorted(child.id for child in children if child.is_open)

    def build_prompt(
        self, task_id: str, run_id: str, children: Optional[Sequence[str]] = None
    ) -> str:
        """The prompt stub. A pointer to the record, never a copy of it.

        Two stubs, chosen by one property of the record: a task with an open child is an
        epic, so the agent sent at it is told to supervise rather than to work. See
        ``SUPERVISOR_STUB`` for why that cannot be one extra sentence on the other one.

        ``children`` is an optimisation with a correctness point behind it. ``build_argv``
        needs the same answer to decide the prompt *and* the permission grant, and the
        two must not be able to disagree -- a supervisor prompt paired with a worker's
        settings is the bug task-220 fixes, and reading the record twice is how that
        would eventually happen. Passing it in reads once. Omitted, this looks it up
        itself, so every other caller is unchanged.
        """
        if children is None:
            children = self.open_child_ids(task_id)
        stub = SUPERVISOR_STUB if children else PROMPT_STUB
        return stub.format(
            agent=self.runner.actor_id,
            task_id=task_id,
            project_id=self.resolution.project_id,
            project_root=self.project_root,
            api_base=self.api_base,
            run_id=run_id,
            children=describe_children(children),
        )

    def build_argv(self, task_id: str, run_id: str) -> List[str]:
        """The full argv for a run, posture flags included."""
        return self.build_argv_and_prompt(task_id, run_id)[0]

    def build_argv_and_prompt(self, task_id: str, run_id: str) -> tuple[List[str], str]:
        """The full argv, and the prompt string that is inside it.

        The open children are read **once** and used twice -- for the prompt stub and for
        the supervisor permission grant. One read, so the prompt and the settings cannot
        describe two different runs.

        The prompt is returned alongside rather than recovered from the argv afterwards
        because a wake has to *replace* that element (see ``dispatch.wake``), and
        searching an argv for "the one that looks like a prompt" is a guess. Handing back
        the exact string the caller put in is not.
        """
        children = self.open_child_ids(task_id)
        values = {
            "prompt": self.build_prompt(task_id, run_id, children),
            "task_id": task_id,
            "project_id": self.resolution.project_id,
            "project_root": str(self.project_root),
            "run_id": run_id,
            "agent": self.runner.actor_id,
            "api_base": self.api_base,
        }
        flags = posture_flags(
            self.resolution.settings.posture,
            mcpjson_server_names(self.project_root),
            supervisor=bool(children),
        )
        argv = compose_argv(self.runner.argv, values, flags)
        # Resolved before it is recorded, because the dispatch entry claims to say what
        # actually ran.
        argv[0] = resolve_executable(argv[0])
        return argv, values["prompt"]

    def _environment(self, run: Optional[RunDirectory] = None, run_id: str = "") -> Dict[str, str]:
        """The child's environment: ours, plus the runner's additions.

        Additive rather than replacing, so a runner does not have to restate PATH. Never
        logged -- this is where a runner's secrets belong, precisely because argv is
        recorded verbatim.

        A spawn passes the run it is starting, which puts the run id and directory into
        the environment. That is how anything downstream of the agent -- the gate, the
        CLI, the MCP server, all children of the session -- can append a phase record
        without being told which run it belongs to (``dispatch.phases``). The two
        polling helpers that also call this do not pass a run, and must not: they are
        asking the runner about a session, not doing work inside one.
        """
        environment = dict(os.environ)
        environment.update(self.runner.env)
        # Granted, never inherited. A dispatcher can itself be running inside a
        # dispatched run -- an agent supervising a child is the ordinary case -- and an
        # inherited pair would file the child's gate under the parent's run. Popping
        # first makes the invariant hold whatever the ambient environment says.
        environment.pop(RUN_DIR_ENV, None)
        environment.pop(RUN_ID_ENV, None)
        if run is not None:
            environment[RUN_DIR_ENV] = str(run.path)
            if run_id:
                environment[RUN_ID_ENV] = run_id
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
                "changes outside its tasks directory. An autonomous agent committing on "
                "top of them entangles the two, and unpicking that is hardest exactly "
                "when you least expect it."
            )

    def _tree_is_clean(self) -> bool:
        """True when the project's working tree has nothing uncommitted.

        AgentJobs' own tasks directory is excluded. This check runs *after* the claim,
        and the claim's whole effect is a write to a task record; without the exclusion
        the re-check refuses on the file dispatch just wrote (task-182).
        """
        return working_tree_clean(self.project_root, ignore=[self.manager.storage.tasks_dir])

    def _commit_record(
        self, task_id: str, subject: str, *, directory: Optional[RunDirectory] = None
    ) -> CommitOutcome:
        """Commit the task record this dispatcher just wrote, and say so in the run's meta.

        Called at the end of each terminal write rather than after each individual
        manager call, so a settle that writes a ``dispatch_result`` and then hands the
        ball to a human produces one commit describing one event, not two.

        The outcome is recorded rather than acted on. There is no logger in this
        subsystem and a run directory is meant to be a complete account of its own run,
        so ``record_commit`` in ``meta.yaml`` is where a later reader finds out that git
        refused -- which is the only way that fact would otherwise be invisible.
        """
        outcome = commit_task_record(self.manager, task_id, subject=subject)
        if directory is not None:
            directory.update_meta(record_commit=outcome.detail)
        return outcome

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
        body: Optional[str] = None,
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
            body=body,
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

    def _plan_wake(
        self, task: Task, run_id: str, argv: List[str], prompt: str
    ) -> tuple[Optional[WakeTarget], List[str], Optional[str]]:
        """Decide whether this dispatch resumes the task's previous session.

        Returns the target (``None`` for a cold start), the argv to run, and what to put
        on the child's stdin. A cold start returns the argv untouched and ``None`` for
        stdin, so nothing about the existing path moves.

        **Every failure here is a cold start, never an exception.** Reading the session
        ledger spawns a subprocess and parses its output; a runner that is not Claude
        Code will not answer at all, and a runner whose argv carries no single prompt
        element cannot be rewritten. None of those is a reason to refuse to dispatch a
        task, because starting cold is a correct -- merely slower -- answer to all of
        them. That asymmetry is the entire safety argument for this feature and it is
        why the `except` below is broad rather than precise.
        """
        if not self.resolution.settings.resume_sessions:
            return None, argv, None
        try:
            rows = self.ledger(include_finished=True)
            target = find_wake_target(self.home, task.id, rows=rows)
        except Exception:  # noqa: BLE001 - see the docstring; a cold start is the fallback
            return None, argv, None
        if target is None:
            return None, argv, None
        try:
            resumed = wake_argv(argv, prompt, target.session_uuid)
        except WakeError:
            return None, argv, None
        return (
            target,
            resumed,
            build_wake_prompt(
                agent=self.runner.actor_id,
                task_id=task.id,
                ball_prompt=task.ball_prompt or "",
                api_base=self.api_base,
                run_id=run_id,
                previous_run_id=target.previous_run_id,
            ),
        )

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
        argv, prompt = self.build_argv_and_prompt(task.id, run_id)
        wake, argv, stdin_text = self._plan_wake(task, run_id, argv, prompt)
        meta: Dict[str, object] = {
            "run_id": run_id,
            "task_id": task.id,
            "project_id": self.resolution.project_id,
            "mode": DispatchMode.SESSION.value,
            "posture": self.resolution.settings.posture.value,
            "status": "starting",
            "started_at": self.clock().isoformat(),
            "caused_by": caused_by,
            "argv": argv,
        }
        if wake is not None:
            # Recorded on the run rather than only in the task entry, because this is
            # what `scripts/run_report.py` reads to tell a woken run from a cold one --
            # which is the whole before/after measurement task-234 has to produce.
            meta["resumed"] = True
            meta["resumed_from"] = wake.previous_run_id
            meta["resumed_session"] = wake.session_uuid
        directory = RunDirectory.create(self.home, run_id, meta)

        try:
            completed = subprocess.run(
                argv,
                cwd=str(self.project_root),
                env=self._environment(directory, run_id),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                # A cold start carries its prompt in argv and inherits stdin, exactly as
                # it always has. A wake **must** deliver its prompt here instead: see
                # `wake_argv` for why the positional form fails silently.
                input=stdin_text,
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
            body=(
                None
                if wake is None
                else (
                    f"Resumed the session from run `{wake.previous_run_id}` rather than "
                    "starting a cold one, so this agent still has the worktree, the "
                    "branch and the verification it established there. The ball prompt "
                    "was delivered to it as its next turn."
                )
            ),
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
            api_base=self.api_base,
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

    def ledger(self, *, include_finished: bool = False) -> List[Dict[str, object]]:
        """Background sessions this project owns, from ``<runner> agents --json --cwd``.

        ``--cwd`` scopes the listing to one project root, so an unrelated session
        elsewhere on the machine is never mistaken for a dispatched run.

        The ledger command is derived from the runner's own executable rather than
        hardcoded to ``claude``. That is what "session mode" means operationally: a
        runner whose executable answers ``agents --json``. It also makes the path
        testable without a real Claude Code install.

        **``include_finished`` adds ``--all``, and without it a stopped session is not
        in the answer at all.** ``--json`` prints *active* sessions; ``--all`` is
        documented as "also include completed background sessions", and the difference
        is total rather than cosmetic -- measured on 2.1.238 against one stopped
        session, ``--json --cwd`` returned **zero** rows and ``--json --all --cwd``
        returned it with its ``sessionId``. Polling wants the active view, because a
        session missing from it is a session that is gone. Waking wants the other one,
        because the whole population it looks at is stopped by definition. Handing
        polling the ``--all`` view would make ``poll_session`` unable to ever conclude
        ``GONE``, so this stays a parameter rather than becoming the default.
        """
        argv = [*self.executable_prefix(), "agents", "--json"]
        if include_finished:
            argv.append("--all")
        argv += ["--cwd", str(self.project_root)]
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

        # Before the phase branches, because it *contradicts* them. A session killed by
        # an expired login reads `idle`/`done`, so `_settle_finished_session` would write
        # a terminal entry for a run that did not finish -- `completed` when the ball had
        # moved earlier in the run, which is precisely how run_a1e35ca5 came to be
        # recorded as a success after dying (task-224).
        stall = self.auth_stall(handle)
        if stall is not None:
            self._park_auth_stall(handle, stall)
            return SessionPhase.AUTH_STALLED

        if phase is SessionPhase.PARKED:
            self._park_session(handle)
        elif phase is SessionPhase.STOPPED:
            self._finish_session(handle, DispatchOutcome.CANCELLED)
        elif phase is SessionPhase.FINISHED:
            self._settle_finished_session(handle)
        return phase

    def auth_stall(self, handle: RunHandle) -> Optional[AuthStall]:
        """Whether this run's session is sitting dead on an expired login.

        ``None`` for every ordinary state, and ``None`` for every runner that is not
        Claude Code, which is what makes this safe to call on every poll of every run.
        Errors are swallowed rather than raised: a transcript that cannot be read right
        now is not evidence of anything, and a poll that throws stops the run being
        followed at all.
        """
        if not handle.session_id:
            return None
        try:
            return read_auth_stall(
                handle.session_id,
                home=self.claude_home,
                since=self._started_at(handle),
            )
        except OSError:  # pragma: no cover - the Claude home went away underneath us
            return None

    def _park_auth_stall(self, handle: RunHandle, stall: AuthStall) -> None:
        """Turn a dead credential into the one instruction that fixes it.

        The run is **parked, not finished**. Recovery is in place and verified: after a
        re-auth the already-running session picks up where it stopped, with no restart
        and no re-dispatch, so reaping it here would destroy the cheap recovery and turn
        six lost minutes into a lost night. Parking also keeps the run's lock, which is
        the correct posture while auth is down -- a fresh dispatch at the same task would
        die exactly as this one did.

        Keyed on the failure's own timestamp rather than on a flag, so a session that
        recovers and later stalls again is reported again rather than silently the once.
        """
        if handle.directory.read_meta().get("auth_stalled_at") == stall.at.isoformat():
            return
        if self.manager.get_task(handle.task_id) is None:  # pragma: no cover - deleted
            return
        said = f' It said: "{stall.message}".' if stall.message else ""
        self.manager.handoff(
            handle.task_id,
            actor="dispatcher",
            ball=Ball.HUMAN,
            ball_reason=BallReason.INPUT,
            ball_prompt=(
                f"Dispatched session `{handle.session_id}` stopped on an expired login "
                f"at {stall.at.isoformat()} and will not resume by itself.{said}\n\n"
                "**Run `claude auth login` in a terminal on that machine.** Answering "
                "inside the session cannot work: Claude Code's background auth daemon "
                "has already discarded the credential, so anything sent to the session "
                "is retried against a token that no longer exists and fails instantly.\n\n"
                "Then send the session a message to wake it, or attach with "
                f"`{self.display_command()} attach {handle.session_id}`. It resumes in "
                "place -- nothing is lost and this task does not need re-dispatching."
            ),
        )
        handle.directory.update_meta(status="parked", auth_stalled_at=stall.at.isoformat())
        # The session is dead until someone logs in, so it will not be committing this
        # handoff on its way past -- and a handoff nobody commits is a handoff the
        # dashboard never shows.
        self._commit_record(
            handle.task_id,
            f"park run {handle.run_id} on an expired login",
            directory=handle.directory,
        )

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
        # A parked session is alive but will not act again until a human answers its
        # prompt, so it is not going to commit this handoff on its way past.
        self._commit_record(
            handle.task_id,
            f"park run {handle.run_id} on a permission prompt",
            directory=handle.directory,
        )

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

        # Last, so one commit covers the result entry and any handoff that followed it.
        # The session has exited by now; nobody else is coming back for this file.
        self._commit_record(
            handle.task_id,
            f"record run {handle.run_id} as {outcome.value}",
            directory=handle.directory,
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
                    env=self._environment(directory, run_id),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                process = subprocess.Popen(
                    argv,
                    cwd=str(self.project_root),
                    env=self._environment(directory, run_id),
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
            # No session ever existed here, so this commit also carries the dispatch
            # entry written moments earlier -- the one case where that entry has no
            # session to sweep it up.
            self._commit_record(
                task.id, f"record run {run_id} as crashed before it started", directory=directory
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
            api_base=self.api_base,
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

        if outcome is not DispatchOutcome.COMPLETED:
            task = self.manager.get_task(handle.task_id)
            if task is not None and task.is_open and task.ball is not Ball.HUMAN:
                self.manager.handoff(
                    handle.task_id,
                    actor="dispatcher",
                    ball=Ball.HUMAN,
                    ball_reason=BallReason.DECISION,
                    ball_prompt=(
                        f"A dispatched batch run ended `{outcome.value}` and nobody was "
                        "told what the task needs. The run's last output is in the "
                        "dispatch_result entry; decide whether to re-dispatch or take "
                        "it on."
                    ),
                )

        # Both outcomes, and after the handoff rather than before it: the process is
        # gone on either path and its last commit is already behind us.
        self._commit_record(
            handle.task_id,
            f"record run {handle.run_id} as {outcome.value}",
            directory=handle.directory,
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
