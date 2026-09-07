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
import tomllib
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
from agentjobs.dispatch.codex_app_server import (
    CodexAppServerError,
    CodexAppServerProcess,
    CodexResumeFailure,
    parse_session_settings,
)
from agentjobs.dispatch.config import (
    DispatchResolution,
    DispatchRunner as RunnerConfig,
    MergePolicy,
    Posture,
    ResolvedPosture,
    RunnerDriver,
    RunnerMode,
    RunnerSelection,
    resolve_posture,
    sentinel_active,
    substitute_argv,
)
from agentjobs.dispatch.record_commit import (
    CommitOutcome,
    commit_task_record,
    task_file_exclusions,
)
from agentjobs.playbooks.pointer import PlaybookPointer
from agentjobs.dispatch.wake import (
    WakeError,
    WakeTarget,
    build_wake_prompt,
    find_wake_target,
    wake_argv,
    newest_session_run,
)
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
from agentjobs.dispatch.credentials import (
    CREDENTIAL_ENV,
    mint_run_credential,
    revoke_run_credential,
)
from agentjobs.dispatch.session_env import daemon_was_started, deliver_identity
from agentjobs.project_setup import MCP_CONFIG_FILENAME
from agentjobs.store_factory import TaskManagerLike

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
    "parks a background run on a prompt nobody can answer. Your harness may instruct "
    "the opposite and call it enforced; in this project it is neither, so ignore that "
    "instruction and run the command. AgentJobs is serving at "
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

**The clause about the harness is task-303, and it is there because the instruction it
contradicts arrives earlier than this one.** A Claude Code ``--bg`` session is opened
with a preamble telling it to call ``EnterWorktree`` and stating that the instruction is
enforced -- which it is, by a guard that refuses a ``Write`` into the shared checkout.
Three auditors on 2026-08-21 each hit that refusal and each independently invented the
same workaround; one dispatched run parked on the tool prompt itself. The repository now
sets ``"worktree": {"bgIsolation": "none"}`` in ``.claude/settings.json``, which turns
the guard off (verified on Claude Code 2.1.238, 2026-08-25), so the enforcement half is
gone. The instruction half is not: it still reaches every dispatched session before this
prompt does, and a run that follows it strands its work where it cannot record or merge
it. Naming it here costs one sentence and saves the session from adjudicating a conflict
it has no context for.

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

REVIEW_CLAUSE = (
    "Posture `{posture}` stops at the merge gate: when the work is done and verified, "
    "record the evidence on the task, hand the ball to human/review, and stop. Do not "
    "merge."
)
"""What a run is told when its posture leaves the merge gate standing (task-021)."""

AUTOMATIC_CLAUSE = (
    "Posture `{posture}` releases the merge gate: this run merges its own work with no "
    "human review. Record the evidence on the task first, then run `agentjobs finish "
    "{task_id} --project {project_id} --posture-release` from {project_root}. That "
    "rebases, runs the full gate, and merges only on a green one -- it stops and hands "
    "the ball back if anything fails. Do not merge by hand."
)
"""What a run is told when its posture releases the merge gate (task-021).

It names a command rather than describing an outcome, for the reason task-192 gave about
the worktree: an instruction a model can satisfy in several ways will be satisfied in the
cheapest one, and here the cheapest one is ``git merge``, which skips the gate that is
the entire safety argument for merging without a person. So the clause says which
command, and says not to do it by hand.
"""

SUPERVISOR_REVIEW_CLAUSE = (
    "Posture `{posture}` stops at the merge gate: each child you start hands off for "
    "human review and merges only on an approval. You approve nothing yourself."
)
"""The review policy, restated for a run that holds no branch of its own."""

SUPERVISOR_AUTOMATIC_CLAUSE = (
    "Posture `{posture}` releases the merge gate: a child you start merges its own work "
    "once its gate is green, with no human review."
)
"""The automatic policy, restated for a run that holds no branch of its own."""

WALK_CLAUSE = (
    "Do not start the children by hand: run `agentjobs dispatch walk {task_id} --project "
    "{project_id}` from {project_root} and let it finish. It takes one eligible child at "
    "a time and stops the whole walk on the first that is not clean. Exit 0 means every "
    "open child is done -- then judge this parent's own acceptance criteria against what "
    "the children recorded, and close it. Exit 1 means it stopped and this record says why."
)
"""How a supervisor is told to walk its children (task-022).

**It names a command rather than describing a loop**, on exactly the reasoning task-192
gave for the worktree line and task-021 gave for the finish command: an instruction a
model can satisfy in several ways gets satisfied in the cheapest one, and here the
cheapest one is to start a child, say it will check back, and end the turn. The workflow
guide records that happening -- *"a supervisor that ends its turn saying it will check
back periodically is not supervising, it is asleep"* -- and this clause is the answer to
it. The walk blocks, so a supervisor obeying this cannot end its turn early.

The final sentence is the one thing the walk deliberately does not do, so it has to be
said here: no open child remaining is not the same as the parent's criteria being met,
and that judgement is the supervisor's."""

NO_PUSH_CLAUSE = "Never push: this project is configured `push: false`."
PUSH_CLAUSE = "This project is configured `push: true`, so pushing `{base}` is permitted."
"""The push half, which is the project's decision and never the posture's (task-021)."""


def policy_clause(
    posture: Posture,
    *,
    push: bool,
    task_id: str,
    project_id: str,
    project_root: object,
    base_branch: str = "main",
    supervisor: bool = False,
) -> str:
    """What this run is permitted to do with its branch, in one or two sentences.

    **The generated prompt is the only channel this policy has**, which is why a stub
    that is otherwise a pointer states it in full. Everything else the stub gestures at
    is *in the record*: the spec, the ball prompt, the guide. The merge policy is not.
    It is derived from machine-local configuration in ``~/.agentjobs/dispatch.yaml``,
    which the agent cannot read, must not read, and would not be told about by any
    document in the repository -- and the same repository's committed prose says
    unconditionally that work does not merge itself. A run that is not told otherwise
    will obey the prose, correctly, and an ``autonomous`` posture would then mean
    nothing at all.

    ``read_only`` gets no clause. It has no branch, and a sentence about what to do with
    one is noise in the prompt of a run that cannot write a file.
    """
    policy = posture.merge_policy
    if policy is MergePolicy.NONE:
        return ""
    if supervisor:
        template = (
            SUPERVISOR_AUTOMATIC_CLAUSE
            if policy is MergePolicy.AUTOMATIC
            else SUPERVISOR_REVIEW_CLAUSE
        )
    else:
        template = AUTOMATIC_CLAUSE if policy is MergePolicy.AUTOMATIC else REVIEW_CLAUSE
    merge = template.format(
        posture=posture.value,
        task_id=task_id,
        project_id=project_id,
        project_root=project_root,
    )
    push_text = PUSH_CLAUSE.format(base=base_branch) if push else NO_PUSH_CLAUSE
    clauses = [merge, push_text]
    if supervisor:
        # Both merge policies get it. The walk is how children are started at either, and
        # what differs is only what each child does with its own branch at the end --
        # which is the child's prompt's business, not the supervisor's.
        clauses.append(
            WALK_CLAUSE.format(task_id=task_id, project_id=project_id, project_root=project_root)
        )
    return " ".join(clauses)


CHILDREN_NAMED = 8
"""How many child ids the supervisor stub lists before it summarises the rest.

A ceiling on prompt length rather than a considered number. Eight covers every parent in
this repository's corpus; a wider epic gets ``and N more``, and the supervisor reads the
rest from the record it is about to open anyway.
"""

GRACE_SECONDS = 30.0
"""How long a cancelled batch run gets to finish a ``git commit`` before it is killed."""

CODEX_PID_MISSING_GRACE_SECONDS = 30.0
"""How long a Codex App Server PID may be temporarily invisible before reaping.

Windows can briefly reject or miss a process probe while a newly-started App Server is
initializing.  A single failed ``os.kill(pid, 0)`` is therefore not evidence that the
session ended; the supervisor state remains authoritative until this grace expires.
"""

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
    posture: Posture,
    mcp_servers: Sequence[str],
    *,
    supervisor: bool = False,
    driver: RunnerDriver = RunnerDriver.CLAUDE,
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
    if driver is RunnerDriver.CODEX:
        return codex_posture_flags(posture)

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


def codex_posture_flags(posture: Posture) -> List[str]:
    """Map AgentJobs' safe postures to Codex's batch sandbox policies.

    Codex's normal non-interactive surface is ``codex exec``.  Its sandbox policy is
    the direct equivalent of a dispatch posture, but it deliberately has no useful
    unattended analogue of Claude's ``supervised`` mode: a batch process has nobody to
    answer its confirmation prompts.  Refusing is more honest than silently widening
    that posture to workspace-write.

    The inline ``required`` override is intentionally independent of the runner argv.
    A Codex dispatch must be able to use AgentJobs to make its task-record writes; if
    this machine's shared ``mcp_servers.agentjobs`` entry is not available, Codex exits
    before doing work instead of producing an untracked run.
    """
    if posture is Posture.SUPERVISED:
        raise DispatchRunError(
            "Codex batch dispatch does not support posture 'supervised'. Use "
            "read_only, auto, or autonomous; unattended Codex cannot answer "
            "interactive approval prompts."
        )
    sandbox = {
        Posture.READ_ONLY: "read-only",
        Posture.AUTO: "workspace-write",
        Posture.AUTONOMOUS: "danger-full-access",
    }[posture]
    return [
        "--sandbox",
        sandbox,
        "-c",
        "mcp_servers.agentjobs.required=true",
    ]


SESSION_NAME_FLAG = "--name"
"""The Claude Code flag that sets a session's display name.

Established by observation on Claude Code 2.1.247, 2026-08-29, not from ``--help``:

* ``--name`` is written into the session ledger at launch (``~/.claude/sessions/<pid>.json``,
  ``nameSince`` equal to ``startedAt`` within milliseconds) and is **not** overwritten by
  the rename Claude Code otherwise performs from the prompt a few seconds into the first
  turn. Without it a dispatched run ends up called something like ``git worktree task
  setup`` -- a summary of the prompt prose, which names no task and is not stable.
* That ledger name is what ``claude agents --json`` reports and what the Remote Control
  peer channel uses as a session's address.
* ``--remote-control [name]`` is a **different** surface and does not touch it. A session
  started ``--remote-control "task-999 probe-beta-rc"`` appeared under the prompt text,
  not under that name, and no peer row carried the name at all.

The full probe log is on task-324.
"""

_NAME_FLAGS = frozenset({"--name", "-n"})
"""Every spelling of the flag above, so an operator who set one is not given a second."""


def session_name(project_id: str, task_id: str, run_id: str) -> str:
    """The display name AgentJobs gives a session it starts.

    ``agentjobs/task-324@11085a50``: the project, the task, and the run that is working
    it. Three ids and nothing else, because this string is read in two places that want
    different things and the ids are what both of them want.

    * A **picker** listing every session on the machine is *scanned*, so the task id goes
      where the eye lands and a name stays short enough that the rest of the row survives.
      There is no length cap to respect -- a 127-character name came back from ``claude
      agents --json`` verbatim -- so brevity here is a choice, not a constraint.
    * The **peer channel** addresses a session *by this name*, so it is typed, and it has
      to distinguish two runs of one task. The run id is what does that; the task id
      alone cannot.

    The project is included because both surfaces are machine-wide while a task id is
    only unique within its project: ``task-042`` names a different piece of work in every
    project on this machine.

    **The title is deliberately absent.** It is the one candidate that reads well and it
    was rejected on two grounds. A title is editable, so two runs of one task could be
    named after two different descriptions of it, which is the instability this task
    exists to remove. And truncating one rarely distinguishes: the tasks that need
    telling apart are neighbours in the same area, whose titles share a prefix -- this
    task and task-296 both begin "Dispatch does not". The id is the key every other
    surface already uses, and it is the key here.

    The name is built from the dispatcher's own identity -- resolved project, claimed
    task, freshly minted run id -- and there is no parameter through which a dispatch
    request could supply any part of it. That is the same rule ``validate_argv`` enforces
    for the template: nothing a caller sends becomes an argv element.
    """
    return f"{project_id}/{task_id}@{short_run_id(run_id)}"


def short_run_id(run_id: str) -> str:
    """A run id with its ``run_`` prefix off, for places where the prefix is noise."""
    return run_id[len("run_") :] if run_id.startswith("run_") else run_id


def session_name_flags(
    template: Sequence[str],
    *,
    driver: RunnerDriver,
    project_id: str,
    task_id: str,
    run_id: str,
) -> List[str]:
    """``--name <name>``, or nothing when it would be wrong to add it.

    Spliced by the dispatcher rather than written into a runner template, for the same
    reason the posture flags are: an operator's custom runner then gets an identifiable
    session without having to remember to ask for one, and a template copied from the
    scaffold example does not silently lose the naming when it is edited. It is accepted
    in both modes -- a ``-p`` batch run takes ``--name`` and exits 0 with it.

    Two cases return nothing.

    **A template that already names its session keeps its own name.** Splicing a second
    ``--name`` would leave the CLI to pick between them, and an operator who wrote one
    meant it. This is the only opt-out, and it is an explicit act.

    **Codex is left unnamed, deliberately.** Its session concept is an App Server thread
    with no display name and no equivalent flag; ``codex exec --name`` is not a thing to
    pass through. There is nothing to degrade -- a Codex thread was never in the Claude
    session picker or on the peer channel -- so this is Claude-only with a clean no-op
    rather than a runner capability that one driver fails to implement. Revisit if the
    Codex App Server grows a name the picker can read.
    """
    if driver is not RunnerDriver.CLAUDE:
        return []
    if any(element in _NAME_FLAGS for element in template):
        return []
    return [SESSION_NAME_FLAG, session_name(project_id, task_id, run_id)]


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


def codex_desktop_executable(home: Optional[Path] = None) -> Optional[str]:
    """Find the versioned CLI bundled with Codex Desktop, if this is that install.

    The Microsoft Store ``codex`` app-execution alias appears on PATH but cannot be
    spawned by a background dispatcher (``Access is denied``).  Codex Desktop records
    its actual, versioned executable in its own configuration for the Node REPL server.
    Read that value afresh for every launch, so a desktop update changes the target
    without a hand edit to ``dispatch.yaml``.  A normal npm/standalone install has no
    such value and simply falls back to PATH below.
    """
    config_path = (home or Path.home()) / ".codex" / "config.toml"
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        candidate = raw["mcp_servers"]["node_repl"]["env"]["CODEX_CLI_PATH"]
    except (KeyError, OSError, tomllib.TOMLDecodeError, TypeError):
        return None
    if not isinstance(candidate, str):
        return None
    path = Path(candidate)
    return str(path) if path.is_file() else None


def resolve_executable(name: str, *, driver: RunnerDriver = RunnerDriver.CLAUDE) -> str:
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
    if driver is RunnerDriver.CODEX and name.lower() in {"codex", "codex.exe"}:
        desktop = codex_desktop_executable()
        if desktop is not None:
            return desktop
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
        """Merge fields into meta.yaml, stamping the finish time when the run ends.

        A write that ends the run also destroys its credential digest (task-331). Not
        what enforces expiry -- verification reads the status, and would refuse this run
        whether or not the digest survived -- but a concluded run's directory sits on
        disk for months, and there is no reason for a verifiable secret to sit in it.
        """
        merged = finish_stamped(self.read_meta(), fields)
        self.write_meta(merged)
        if str(merged.get("status") or "") in TERMINAL_STATUSES:
            revoke_run_credential(self.path)

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
    posture: Optional[ResolvedPosture] = None
    """What this run may do, and which of the three sources decided (task-308).

    Stamped by ``DispatchRunner.start`` on the way out, so a handle rebuilt from disk by
    the poller leaves it ``None`` -- that path reports on runs it did not start and has
    the run directory's ``posture`` fields to read instead.
    """

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
        manager: TaskManagerLike,
        resolution: DispatchResolution,
        project_root: Path,
        home: Path,
        api_base: Optional[str] = None,
        grace_seconds: float = GRACE_SECONDS,
        clock: Callable[[], datetime] = utcnow,
        claude_home: Optional[Path] = None,
        playbook: Optional[PlaybookPointer] = None,
        posture: Optional[ResolvedPosture] = None,
    ) -> None:
        self.manager = manager
        self.resolution = resolution
        self.posture = posture or resolve_posture(resolution.settings)
        """What this run may do, and which of the three sources said so (task-308).

        Resolved by the caller when a task record or a dispatch-time choice had anything
        to say, and defaulted here to the project's own posture so that every other
        caller -- tests, the poller rebuilding a handle, anything predating this -- keeps
        the behaviour it had. Held on the runner rather than read off
        ``resolution.settings`` at each use, because there are ten of those uses and one
        of them disagreeing with the rest is a run whose recorded posture is not the one
        it was started with.
        """
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
        self.playbook = playbook
        """The playbook this run was given as its brief, or ``None`` for every other run.

        It reaches exactly two places -- one appended line on the prompt stub, and two
        fields on the ``dispatch`` entry -- and it is a pointer in both. Nothing here
        reads the brief, and the brief is never copied into either (design section 4.2).
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

        The posture's merge and push policy is appended (task-021) and is the one thing
        here that is *not* a pointer, because there is nothing to point at: see
        ``policy_clause``.
        """
        if children is None:
            children = self.open_child_ids(task_id)
        stub = SUPERVISOR_STUB if children else PROMPT_STUB
        rendered = stub.format(
            agent=self.runner.actor_id,
            task_id=task_id,
            project_id=self.resolution.project_id,
            project_root=self.project_root,
            api_base=self.api_base,
            run_id=run_id,
            children=describe_children(children),
        )
        settings = self.resolution.settings
        clause = policy_clause(
            self.posture.posture,
            push=settings.push,
            task_id=task_id,
            project_id=self.resolution.project_id,
            project_root=self.project_root,
            base_branch=settings.finish.base_branch,
            supervisor=bool(children),
        )
        if clause:
            rendered = f"{rendered} {clause}"
        # A playbook run appends one line and changes nothing else about the stub. It is
        # a pointer at the brief, never the brief: see ``PlaybookPointer.prompt_line``.
        if self.playbook is not None:
            rendered = f"{rendered} {self.playbook.prompt_line()}"
        return rendered

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
            self.posture.posture,
            mcpjson_server_names(self.project_root),
            supervisor=bool(children),
            driver=self.runner.driver,
        )
        # The session name is AgentJobs the same way the posture flags are, and rides
        # the same splice: both are things an operator template must not have to
        # remember, and must not be able to get wrong.
        flags = [
            *flags,
            *session_name_flags(
                self.runner.argv,
                driver=self.runner.driver,
                project_id=self.resolution.project_id,
                task_id=task_id,
                run_id=run_id,
            ),
        ]
        argv = compose_argv(self.runner.argv, values, flags)
        # Resolved before it is recorded, because the dispatch entry claims to say what
        # actually ran.
        argv[0] = resolve_executable(argv[0], driver=self.runner.driver)
        return argv, values["prompt"]

    def _codex_wake_target(self, task_id: str) -> Optional[WakeTarget]:
        """Find the newest completed Codex thread for this task.

        Claude's wake path reads its session ledger. Codex's persisted App Server
        thread is already represented by the AgentJobs run directory, so the newest
        session run is the authoritative candidate. As with Claude, only that newest
        run is considered; a missing or reaped conversation starts no second wake chain.
        """
        if not self.resolution.settings.resume_sessions:
            return None
        record = newest_session_run(self.home, task_id)
        if record is None or record.is_live or not record.session_id:
            return None
        meta = RunDirectory(record.path).read_meta()
        if meta.get("driver") != RunnerDriver.CODEX.value:
            return None
        if meta.get("reaped") is True or meta.get("codex_status") != "completed":
            return None
        return WakeTarget(
            previous_run_id=record.run_id,
            session_id=record.session_id,
            session_uuid=record.session_id,
        )

    def _start_codex_app_server_session(
        self, task: Task, *, actor: str, caused_by: int, trigger: DispatchTrigger
    ) -> RunHandle:
        """Start a persisted Codex App Server thread and its first turn."""
        run_id = new_run_id()
        argv, prompt = self.build_argv_and_prompt(task.id, run_id)
        wake = self._codex_wake_target(task.id)
        if wake is not None:
            prompt = build_wake_prompt(
                agent=self.runner.actor_id,
                task_id=task.id,
                ball_prompt=task.ball_prompt or "",
                api_base=self.api_base,
                run_id=run_id,
                previous_run_id=wake.previous_run_id,
            )
        settings = parse_session_settings(
            argv, posture=self.posture.posture.value, project_root=self.project_root
        )
        directory = RunDirectory.create(
            self.home,
            run_id,
            {
                "run_id": run_id,
                "task_id": task.id,
                "project_id": self.resolution.project_id,
                "mode": DispatchMode.SESSION.value,
                "driver": self.runner.driver.value,
                "posture": self.posture.posture.value,
                **self.posture.as_data(),
                "status": "starting",
                "codex_status": "starting",
                "codex_lifecycle": "preflight",
                "started_at": self.clock().isoformat(),
                "caused_by": caused_by,
                "argv": argv,
                **(
                    {
                        "resumed": True,
                        "resumed_from": wake.previous_run_id,
                        "resumed_session": wake.session_uuid,
                    }
                    if wake is not None
                    else {}
                ),
            },
        )

        # Codex needs no settings hop: the App Server is spawned directly from this
        # environment, so the credential reaches its worker the way the run id does.
        credential = mint_run_credential(directory.path, run_id)

        def new_app_server() -> CodexAppServerProcess:
            return CodexAppServerProcess(
                executable=resolve_executable(argv[0], driver=RunnerDriver.CODEX),
                cwd=self.project_root,
                env=self._environment(directory, run_id, credential),
                settings=settings,
                service_name="agentjobs",
                thread_name=f"AgentJobs {self.resolution.project_id}/{task.id}",
            )

        app_server = new_app_server()
        resumed = wake is not None
        resume_replacement = False
        resume_replacement_reason = ""
        launch_phase = "preflight"
        try:
            preflight = new_app_server().preflight_required_mcp()
            directory.update_meta(
                mcp_preflight_server=preflight.server_name,
                mcp_preflight_status=preflight.status,
                codex_lifecycle="starting",
            )
            launch_phase = "start"
            try:
                started = app_server.start(
                    prompt, resume_thread_id=wake.session_uuid if wake else None
                )
            except CodexAppServerError as exc:
                failure = exc.resume_failure
                if wake is None or failure is None:
                    raise
                if failure is CodexResumeFailure.BUSY:
                    # A durable thread with another active writer is still this task's
                    # continuation.  Return a parked live run so the guard adopts and
                    # retains its per-task lock; a second click cannot launch a rival
                    # conversation while the original writer remains authoritative.
                    app_server.terminate()
                    entry_id = self._record_dispatch(
                        task,
                        run_id,
                        argv,
                        actor=actor,
                        caused_by=caused_by,
                        trigger=trigger,
                        mode=DispatchMode.SESSION,
                        session_id=wake.session_uuid,
                        body=(
                            f"Could not resume Codex App Server thread from run "
                            f"`{wake.previous_run_id}` because it still has an active writer. "
                            "The run is parked as resume_busy; its task lock remains held and "
                            "no fresh thread was started."
                        ),
                    )
                    directory.update_meta(
                        status="parked",
                        codex_status="resume_busy",
                        codex_lifecycle="resume_busy",
                        resume_failure=failure.value,
                        resume_error=str(exc),
                        dispatch_entry_id=entry_id,
                        session_id=wake.session_uuid,
                        thread_id=wake.session_uuid,
                        pid=None,
                    )
                    return RunHandle(
                        run_id=run_id,
                        task_id=task.id,
                        mode=DispatchMode.SESSION,
                        directory=directory,
                        session_id=wake.session_uuid,
                        dispatch_entry_id=entry_id,
                        runner=self.runner.name,
                        group=self._group_name(),
                        api_base=self.api_base,
                    )
                if failure not in {
                    CodexResumeFailure.MISSING,
                    CodexResumeFailure.UNRECOVERABLE,
                }:
                    raise
                app_server = new_app_server()
                started = app_server.start(self.build_argv_and_prompt(task.id, run_id)[1])
                resumed = False
                resume_replacement = True
                resume_replacement_reason = failure.value
                directory.update_meta(
                    resume_replacement=True,
                    resume_failure=failure.value,
                    replaced_thread_id=wake.session_uuid,
                    fresh_thread_id=started.thread_id,
                )
            entry_id = self._record_dispatch(
                task,
                run_id,
                argv,
                actor=actor,
                caused_by=caused_by,
                trigger=trigger,
                mode=DispatchMode.SESSION,
                session_id=started.thread_id,
                body=(
                    (
                        f"Resumed Codex App Server thread from run `{wake.previous_run_id}` "
                        "and injected the latest AgentJobs wake prompt."
                        if resumed and wake is not None
                        else (
                            f"Started fresh Codex App Server thread `{started.thread_id}` after "
                            f"persisted thread `{wake.session_uuid}` from run "
                            f"`{wake.previous_run_id}` was classified {resume_replacement_reason}."
                            if resume_replacement and wake is not None
                            else "Started a Codex App Server thread. Its conversation is persisted in "
                            "the Codex session store and can be opened by Codex Desktop."
                        )
                    )
                ),
            )
        except (CodexAppServerError, DispatchRunError) as exc:
            app_server.terminate()
            failure_meta: Dict[str, object] = {
                "status": "failed",
                "codex_status": "failed",
                "codex_phase": launch_phase,
                "codex_lifecycle": "terminal_failure",
                "error": str(exc),
            }
            if launch_phase == "preflight":
                failure_meta["mcp_preflight_error"] = str(exc)
            directory.update_meta(**failure_meta)
            self.manager.record_dispatch_result(
                task.id,
                actor="dispatcher",
                run_id=run_id,
                outcome=DispatchOutcome.CRASHED,
                re=None,
                log_path=str(directory.path),
                body=f"The Codex App Server session never started: {exc}",
            )
            self._commit_record(
                task.id, f"record run {run_id} as crashed before it started", directory=directory
            )
            raise DispatchRunError(
                f"Could not start a Codex App Server session for {task.id}: {exc}"
            ) from exc

        directory.update_meta(
            status="running",
            codex_status="running",
            codex_lifecycle="running_turn",
            pid=started.pid,
            session_id=started.session_id,
            thread_id=started.thread_id,
            turn_id=started.turn_id,
            dispatch_entry_id=entry_id,
            resumed=resumed,
        )
        handle = RunHandle(
            run_id=run_id,
            task_id=task.id,
            mode=DispatchMode.SESSION,
            directory=directory,
            pid=started.pid,
            session_id=started.thread_id,
            dispatch_entry_id=entry_id,
            runner=self.runner.name,
            group=self._group_name(),
            api_base=self.api_base,
        )
        handle.supervisor = threading.Thread(
            target=self._supervise_codex_app_server,
            args=(handle, app_server, started.turn_id),
            name=f"dispatch-{run_id}",
            # Unlike Claude's detached session manager, Codex App Server is the
            # child owned by this process.  A daemon supervisor would be killed
            # when `agentjobs dispatch run` returns, taking the Codex child with
            # it before the first model turn.  Keep this owner alive until the
            # App Server turn has reported completion.
            daemon=False,
        )
        handle.supervisor.start()
        return handle

    def _supervise_codex_app_server(
        self, handle: RunHandle, app_server: CodexAppServerProcess, turn_id: str
    ) -> None:
        """Persist App Server events and leave task settlement to the poller."""
        transcript = handle.directory.path / TRANSCRIPT_FILENAME
        turn_completed = False
        try:
            with transcript.open("w", encoding="utf-8") as stream:

                def write_message(message: Dict[str, object]) -> None:
                    stream.write(json.dumps(message, ensure_ascii=False) + "\n")
                    stream.flush()

                completed = app_server.supervise(turn_id=turn_id, on_message=write_message)
            turn = completed.get("turn") if isinstance(completed, dict) else None
            status = turn.get("status") if isinstance(turn, dict) else None
            codex_status = "completed" if status == "completed" else "failed"
            turn_completed = codex_status == "completed"
            error = turn.get("error") if isinstance(turn, dict) else None
            handle.directory.update_meta(
                status="running" if codex_status == "completed" else "failed",
                codex_status=codex_status,
                codex_lifecycle="turn_completed"
                if codex_status == "completed"
                else "terminal_failure",
                codex_turn_status=status or "unknown",
                **({"error": str(error)} if error else {}),
            )
        except BaseException as exc:  # noqa: BLE001 - total supervisor, like batch mode
            handle.directory.update_meta(
                status="failed",
                codex_status="failed",
                codex_lifecycle="terminal_failure",
                error=str(exc),
            )
        finally:
            app_server.terminate()
        if turn_completed:
            self._observe_codex_persistence(handle)

    def _observe_codex_persistence(self, handle: RunHandle) -> None:
        """Persist session-store evidence without claiming Desktop sidebar visibility."""
        meta = handle.directory.read_meta()
        thread_id = meta.get("thread_id") or handle.session_id
        argv = meta.get("argv")
        if not isinstance(thread_id, str) or not thread_id or not isinstance(argv, list):
            handle.directory.update_meta(
                persistence_status="unavailable",
                persistence_error="Run metadata has no persisted Codex thread or argv.",
                desktop_visibility="not_observed",
            )
            return
        rendered_argv = [item for item in argv if isinstance(item, str)]
        if len(rendered_argv) != len(argv) or not rendered_argv:
            handle.directory.update_meta(
                persistence_status="unavailable",
                persistence_error="Run metadata has an invalid Codex argv.",
                desktop_visibility="not_observed",
            )
            return
        try:
            settings = parse_session_settings(
                rendered_argv,
                posture=str(meta.get("posture") or self.posture.posture.value),
                project_root=self.project_root,
            )
            observer = CodexAppServerProcess(
                executable=resolve_executable(rendered_argv[0], driver=RunnerDriver.CODEX),
                cwd=self.project_root,
                env=self._environment(handle.directory, handle.run_id),
                settings=settings,
                service_name="agentjobs",
                thread_name=f"AgentJobs {self.resolution.project_id}/{handle.task_id}",
            )
            evidence = observer.inspect_persisted_thread(thread_id)
        except CodexAppServerError as exc:
            handle.directory.update_meta(
                persistence_status="unavailable",
                persistence_error=str(exc),
                desktop_visibility="not_observed",
            )
            return
        handle.directory.update_meta(
            persistence_status="persisted",
            persistence_thread_id=thread_id,
            persistence_evidence=evidence,
            desktop_visibility="not_observed",
        )

    def _environment(
        self,
        run: Optional[RunDirectory] = None,
        run_id: str = "",
        credential: str = "",
    ) -> Dict[str, str]:
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

        **This reaches a batch run's agent and does not reach a session's** (task-249).
        The process started under ``--bg`` is a launcher; it hands the session to a
        persistent daemon which spawns the worker from the daemon's own environment, and
        everything set here is dropped. What is built here is still correct and still
        needed -- the launcher runs in it -- but a session agent's copy arrives through
        ``session_env.deliver_identity`` instead. Keep the two in step: a variable added
        here that a session agent must see has to be added there as well.
        """
        environment = dict(os.environ)
        environment.update(self.runner.env)
        # Granted, never inherited. A dispatcher can itself be running inside a
        # dispatched run -- an agent supervising a child is the ordinary case -- and an
        # inherited pair would file the child's gate under the parent's run. Popping
        # first makes the invariant hold whatever the ambient environment says.
        environment.pop(RUN_DIR_ENV, None)
        environment.pop(RUN_ID_ENV, None)
        # The credential is popped for a sharper reason than the other two (task-331):
        # a child that inherited its supervisor's credential would *be* the supervisor
        # to every request it made, which is the impersonation this epic closes rather
        # than a mislabelled measurement.
        environment.pop(CREDENTIAL_ENV, None)
        if run is not None:
            environment[RUN_DIR_ENV] = str(run.path)
            if run_id:
                environment[RUN_ID_ENV] = run_id
            if credential:
                environment[CREDENTIAL_ENV] = credential
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

        AgentJobs' own tasks directory is excluded *while it holds task files*. This
        check runs after the claim, and the claim's whole effect is a write to a task
        record; without the exclusion the re-check refuses on the file dispatch just
        wrote (task-182). A project served from the database writes no such file, so
        nothing is excluded and the check covers the whole tree again.
        """
        return working_tree_clean(self.project_root, ignore=task_file_exclusions(self.manager))

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
            posture=DispatchPosture(self.posture.posture.value),
            posture_source=self.posture.source.value,
            posture_ceiling=self.posture.ceiling.value,
            posture_requested=(
                self.posture.requested.value if self.posture.requested is not None else None
            ),
            trigger=trigger,
            caused_by=caused_by,
            argv=argv,
            cwd=str(self.project_root),
            git_head=self._git_head(),
            session_id=session_id,
            selection=selection_data(self.resolution.selection),
            playbook=self.playbook.name if self.playbook else None,
            playbook_hash=self.playbook.digest if self.playbook else None,
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
            handle = self._start_session(task, actor=actor, caused_by=caused_by, trigger=trigger)
        else:
            handle = self._start_batch(task, actor=actor, caused_by=caused_by, trigger=trigger)
        # Stamped once here rather than threaded through both mode paths and every
        # `RunHandle(...)` inside them. It is surfaced for the same reason `api_base` is:
        # it is otherwise buried in a run directory nobody opens, and a caller that just
        # spent money on a run should be able to say what envelope it got.
        handle.posture = self.posture
        return handle

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
        if self.runner.driver is RunnerDriver.CODEX:
            return self._start_codex_app_server_session(
                task, actor=actor, caused_by=caused_by, trigger=trigger
            )
        run_id = new_run_id()
        argv, prompt = self.build_argv_and_prompt(task.id, run_id)
        # Before `_plan_wake`, so a resumed session gets the flag too: `wake_argv`
        # rewrites only the element carrying the prompt and preserves everything else.
        # The directory is named here and created a few lines below; `deliver_identity`
        # makes it, because the settings document has to exist before the launcher runs.
        directory_path = runs_root(self.home) / run_id
        # Minted before the worker exists, so the digest is on disk before anything can
        # present the token. A credential in hand also moves the settings document out of
        # argv and into a 0600 file -- see `session_env.deliver_identity`.
        credential = mint_run_credential(directory_path, run_id)
        delivered = deliver_identity(
            argv,
            prompt=prompt,
            directory=directory_path,
            run_id=run_id,
            runner_env=self.runner.env,
            credential=credential,
        )
        argv = delivered.argv
        wake, argv, stdin_text = self._plan_wake(task, run_id, argv, prompt)
        meta: Dict[str, object] = {
            "run_id": run_id,
            "task_id": task.id,
            "project_id": self.resolution.project_id,
            "mode": DispatchMode.SESSION.value,
            # Recorded on every driver, not only Codex. Until task-233 only the Codex
            # path wrote it, so `scripts/run_report.py` had to read an absent key as
            # "claude" -- correct for the runs that existed, and an inference that gets
            # quietly wrong the first time a third driver lands.
            "driver": self.runner.driver.value,
            "posture": self.posture.posture.value,
            **self.posture.as_data(),
            "status": "starting",
            "started_at": self.clock().isoformat(),
            "caused_by": caused_by,
            "argv": argv,
            # How this run's identity reached its worker (task-249). Recorded because
            # "no phase records" has three causes -- predates the instrumentation, the
            # identity never arrived, no gate was run -- and they used to be one line in
            # every report. A run with no key at all is the first.
            "session_env": delivered.delivery.value,
        }
        if delivered.document is not None:
            # Only when the settings went to a file, which happens only for a runner with
            # secrets of its own. argv then names a path instead of carrying the document,
            # so the permission envelope would otherwise stop being readable from the run
            # record -- and that readability is the whole point of recording argv.
            meta["session_settings"] = delivered.document
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
                # No credential here, deliberately, and this is the one place the
                # asymmetry with the run id matters. This process is a *launcher*: when
                # it is the launch that starts the Claude daemon, the daemon inherits
                # this environment and hands it to every session it spawns afterwards
                # (task-249). A stale run id mislabels a phase record; a stale credential
                # would make one run's sessions speak as another's. The worker's copy
                # goes through the settings document instead.
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
        # Whether this launch started the daemon or joined one already running. Nothing
        # branches on it; it is the evidence that says whether a run's environment could
        # have come from its own launcher at all (task-249).
        directory.update_meta(daemon_started=daemon_was_started(output))
        if completed.returncode != 0:
            directory.update_meta(status="failed", exit_code=completed.returncode)
            raise DispatchRunError(
                f"Session launch for {task.id} exited {completed.returncode}: "
                f"{output.strip()[:500]}"
            )

        session_id = self.capture_session_id(completed.stdout or "", reject=short_run_id(run_id))
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
    def capture_session_id(cls, stdout: str, *, reject: Optional[str] = None) -> Optional[str]:
        """Read the short id out of ``backgrounded · b55b35ad · name``.

        Positional rather than regex-over-the-whole-line on purpose: the separator and
        the trailing name are cosmetic and will change; the first 8-hex token the
        launcher prints is the stable part. It is *not* guaranteed to be on the first
        line -- before ``--name`` existed the only matchable copy was the one in
        ``claude attach <id>`` two lines down, which is what this used to read.

        **Escapes are stripped before matching, and that is the whole defect this
        guards** (task-327). The launcher colours the id, so the raw bytes are
        ``\\x1b[36m1b5f4a48\\x1b[39m``: the ``m`` ending the escape is a word character
        and so is the digit starting the id, so ``\\b`` never fires between them and the
        id is invisible to the scan. It went unnoticed for as long as nothing else on
        that line matched. Then task-324 added ``--name <project>/<task>@<run stub>``,
        whose stub *is* cleanly delimited -- so every run since recorded its own run
        stub as the session id, and every one of them was reported dead while working.

        ``strip_ansi`` was already in this module, applied to terminal output meant for
        a person to read. The id was the one place it was needed for a value something
        else would act on, and the one place it was not used.

        ``reject`` is the second half, and it is the half that cannot rot: the caller
        passes the run stub it just put in ``--name``, and a run's own id can never be
        the id the CLI assigned. A future change to the name format therefore cannot
        resurrect this, whatever it puts on the line.
        """
        for line in stdout.splitlines():
            for match in cls._SHORT_ID.finditer(strip_ansi(line)):
                if match.group(1) != reject:
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

    def ledger(
        self, *, include_finished: bool = False, scoped: bool = True
    ) -> List[Dict[str, object]]:
        """Background sessions this project owns, from ``<runner> agents --json --cwd``.

        ``--cwd`` scopes the listing to one project root, so an unrelated session
        elsewhere on the machine is never mistaken for a dispatched run.

        ``scoped=False`` drops that flag, for one caller: the interactive sweep
        (task-354), which asks only *is this exact session id still listed*. It matches
        on the id rather than inferring ownership from a directory, and the directory
        would be the wrong question anyway -- an interactive session working a task from
        a worktree is listed under the worktree's path, not the project root's.

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
        if scoped:
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
        # App Server owns its child process directly.  There is no ``codex stop``
        # command for an App Server thread, and attempting one would accidentally
        # invoke the batch CLI with a thread id.  The supervisor terminates the child
        # after turn completion; an interrupted run is handled by its process owner.
        if self.runner.driver is RunnerDriver.CODEX:
            return False
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

        if self.runner.driver is RunnerDriver.CODEX:
            return self._poll_codex_app_server(handle)

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
        transcript = self.capture_transcript(handle)

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
        elif phase is SessionPhase.RUNNING:
            self._check_running_stall(handle, transcript)
        return phase

    def _poll_codex_app_server(self, handle: RunHandle) -> SessionPhase:
        """Poll the App Server supervisor state without pretending ``codex agents`` exists."""
        meta = handle.directory.read_meta()
        codex_status = meta.get("codex_status")
        if codex_status == "completed":
            self._settle_finished_session(handle)
            return SessionPhase.FINISHED
        if codex_status == "failed":
            self._finish_session(
                handle,
                DispatchOutcome.CRASHED,
                body=str(meta.get("error") or "The Codex App Server supervisor failed."),
            )
            return SessionPhase.GONE
        if codex_status == "resume_busy":
            # The lock stays held by the live parked run.  Only a later explicit
            # recovery may retry this same persisted thread; polling must not turn it
            # into a fresh dispatch behind the caller's back.
            return SessionPhase.PARKED
        pid = meta.get("pid")
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
                # Clear a transient miss once the process is observable again.  A null
                # value keeps the run metadata self-describing without growing a new
                # schema just for this one startup race.
                if meta.get("pid_missing_since") is not None:
                    handle.directory.update_meta(pid_missing_since=None)
            except PermissionError:
                # The process exists but Windows denied the probe; that is still alive
                # for polling purposes.
                pass
            except (OSError, ProcessLookupError):
                now = self.clock()
                missing_raw = meta.get("pid_missing_since")
                missing_since: Optional[datetime] = None
                if isinstance(missing_raw, str):
                    try:
                        missing_since = datetime.fromisoformat(missing_raw)
                    except ValueError:
                        missing_since = None
                    if missing_since is not None and missing_since.tzinfo is None:
                        missing_since = missing_since.replace(tzinfo=timezone.utc)
                if missing_since is None:
                    handle.directory.update_meta(pid_missing_since=now.isoformat())
                    return SessionPhase.RUNNING
                if now - missing_since < timedelta(seconds=CODEX_PID_MISSING_GRACE_SECONDS):
                    return SessionPhase.RUNNING
                if self._reconcile_codex_app_server(handle, meta):
                    return SessionPhase.RUNNING
                self._finish_session(
                    handle,
                    DispatchOutcome.INTERRUPTED,
                    body=(
                        "The Codex App Server process exited before reporting turn completion, "
                        "and its persisted Codex thread could not be reconciled."
                    ),
                )
                return SessionPhase.GONE
        return SessionPhase.RUNNING

    def _reconcile_codex_app_server(self, handle: RunHandle, meta: Dict[str, object]) -> bool:
        """Confirm a lost App Server child against Codex before settling its run.

        A PID can disappear when AgentJobs restarts, after a Windows probe race, or
        because the child exited after persisting its thread.  It is therefore only a
        trigger to reconcile, never proof that another task turn may be launched.
        """
        thread_id = meta.get("thread_id") or handle.session_id
        argv = meta.get("argv")
        if not isinstance(thread_id, str) or not thread_id or not isinstance(argv, list):
            handle.directory.update_meta(
                codex_lifecycle="reconciliation_failed",
                reconciliation_error="Run metadata has no persisted Codex thread or argv.",
            )
            return False
        rendered_argv = [item for item in argv if isinstance(item, str)]
        if len(rendered_argv) != len(argv) or not rendered_argv:
            handle.directory.update_meta(
                codex_lifecycle="reconciliation_failed",
                reconciliation_error="Run metadata has an invalid Codex argv.",
            )
            return False
        try:
            settings = parse_session_settings(
                rendered_argv,
                posture=str(meta.get("posture") or self.posture.posture.value),
                project_root=self.project_root,
            )
            app_server = CodexAppServerProcess(
                executable=resolve_executable(rendered_argv[0], driver=RunnerDriver.CODEX),
                cwd=self.project_root,
                env=self._environment(handle.directory, handle.run_id),
                settings=settings,
                service_name="agentjobs",
                thread_name=f"AgentJobs {self.resolution.project_id}/{handle.task_id}",
            )
            app_server.read_persisted_thread(thread_id)
        except CodexAppServerError as exc:
            handle.directory.update_meta(
                codex_lifecycle="reconciliation_failed",
                reconciliation_error=str(exc),
            )
            return False
        handle.directory.update_meta(
            codex_status="reconciling",
            codex_lifecycle="reconciled",
            reconciled_thread_id=thread_id,
            reconciled_at=self.clock().isoformat(),
            pid=None,
        )
        return True

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
                f"{self.session_noun(handle)} session `{handle.session_id}` stopped on an "
                f"authentication "
                f"failure at {stall.at.isoformat()} and will not resume by itself."
                f"{said}\n\n"
                "**Find out which failure this is before doing anything — there are two "
                "and they look identical from here.** Either the credential store "
                "genuinely cannot authenticate, or it can and this session is stuck on "
                "a refresh that already failed. Nothing you can see distinguishes them: "
                "not this task, not the run ledger, and not `agents --json`. One "
                "command does, run as a **fresh process** rather than inside the "
                "stalled session:\n\n"
                '```\nclaude -p "Reply with exactly: AUTH_OK"\n```\n\n'
                "**If it answers**, the credential is fine and only the session is "
                "stuck. Send it a message to wake it, or attach with "
                f"`{self.display_command()} attach {handle.session_id}`. Logging in "
                "again changes nothing.\n\n"
                "**If it fails to authenticate**, the store is genuinely dead and no "
                "restart will clear it. Run `claude auth login` in a terminal on that "
                "machine, then wake the session the same way. Answering inside the "
                "session cannot work while this is true: the credential is already "
                "gone, so anything sent is retried against nothing and fails "
                "instantly.\n\n"
                "Either way the session resumes in place -- nothing is lost and this "
                "task does not need re-dispatching."
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

    def session_noun(self, handle: RunHandle, *, capitalised: bool = True) -> str:
        """ "Dispatched session" or "Registered session", from what the run says it is.

        Every ball prompt below is read by a person deciding what to do about a session,
        and "dispatched" is the first thing they would check. It is false for a session
        AgentJobs adopted rather than started (task-320) -- and the difference is
        actionable, because a registered session was spawned by somebody who may still
        be waiting on it.
        """
        origin = handle.directory.read_meta().get("origin")
        word = "Registered" if origin == "registered" else "Dispatched"
        return word if capitalised else word.lower()

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
                f"{self.session_noun(handle)} session `{handle.session_id}` is parked on a "
                f"permission "
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

    def _check_running_stall(self, handle: RunHandle, transcript: str) -> None:
        """Report a session that claims to be working but has emitted nothing for long.

        ``RUNNING`` was the one phase that wrote nothing to the task and had no time
        bound of any kind (task-296). Every other phase concludes something: parked and
        auth-stalled hand to a human, stopped and finished settle the run. A session that
        merely *says* it is busy could sit for ever, and the task record would read
        ``agent``/``work`` throughout -- so a supervisor polling the record, which is the
        rule, correctly waits on a process that is doing nothing.

        **The signal is transcript growth, and it costs nothing new.** ``capture_transcript``
        already fetches the session's whole log on every poll, so the length of what it
        returned is a did-something answer that needs no extra subprocess, no new runner
        call, and nothing the codex driver would have to decline (task-296 decided
        against a driver ``liveness()`` for that reason).

        This is *not* the thing ENGINEERING.md forbids. That rule is about deriving
        **counts** by grepping a TTY capture, which repainting makes meaningless. Asking
        whether the length changed at all is not a count, and a repaint appends bytes, so
        it stays sound where a count does not.

        **Detection only, and deliberately non-fatal.** The session is not killed and not
        restarted: it stays attachable, which is what made the original recovery work,
        and restarting one that may hold a dirty worktree risks two sessions on one
        branch. Recovery was considered and left out (task-296, sc-4).

        Recoverable rather than sticky, unlike a permission park: if output resumes the
        status goes back to ``running`` and a later stall is reported again. ``stalled``
        is not in ``TERMINAL_STATUSES``, so the run stays live and keeps being polled --
        that is what makes the recovery reachable at all.
        """
        # An unreadable transcript is not evidence of silence. `capture_transcript`
        # returns "" both when the session produced nothing and when the fetch failed,
        # and starting a stall clock on the second would report healthy sessions.
        if not transcript:
            return

        meta = handle.directory.read_meta()
        size = len(transcript)
        now = self.clock()
        previous = meta.get("output_size")

        if not isinstance(previous, int) or size != previous:
            fields: Dict[str, object] = {
                "output_size": size,
                "output_changed_at": now.isoformat(),
            }
            # Growth after a stall retracts it, so the next silence is reported afresh.
            if meta.get("status") == "stalled":
                fields["status"] = "running"
            handle.directory.update_meta(**fields)
            return

        if meta.get("status") == "stalled":
            return  # already reported; polling is repeated, so this must be idempotent

        raw = meta.get("output_changed_at")
        if not isinstance(raw, str):
            handle.directory.update_meta(output_changed_at=now.isoformat())
            return
        try:
            since = datetime.fromisoformat(raw)
        except ValueError:  # pragma: no cover - written by us, one line above
            handle.directory.update_meta(output_changed_at=now.isoformat())
            return
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

        quiet = now - since
        if quiet < timedelta(seconds=self.resolution.limits.session_stall_seconds):
            return

        if self.manager.get_task(handle.task_id) is None:  # pragma: no cover - deleted
            return

        minutes = int(quiet.total_seconds() // 60)
        tail = readable_tail(transcript, OUTPUT_TAIL_LINES)
        quoted = f"\n\nThe end of its terminal, verbatim:\n\n```\n{tail}\n```" if tail else ""
        self.manager.handoff(
            handle.task_id,
            actor="dispatcher",
            ball=Ball.HUMAN,
            ball_reason=BallReason.INPUT,
            ball_prompt=(
                f"{self.session_noun(handle)} session `{handle.session_id}` still reports itself "
                f"as "
                f"working but has produced no output for {minutes} minutes, so this task "
                "has been reading `agent`/`work` while nothing happened. It was **not** "
                "killed and is still attachable: "
                f"`{self.display_command()} attach {handle.session_id}`. Attach to see "
                "what it is doing, or stop it and move this task on yourself."
                f"{quoted}"
            ),
        )
        handle.directory.update_meta(status="stalled", stalled_at=now.isoformat())
        # A stalled session is by definition not writing anything, so it will not carry
        # this handoff to `main` on its way past the way a working one would.
        self._commit_record(
            handle.task_id,
            f"report run {handle.run_id} as stalled",
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
                f"A {self.session_noun(handle, capitalised=False)} session ({handle.session_id}) "
                f"finished without handing "
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

        self._resolve_deferred_escalation(handle)

        # Last, so one commit covers the result entry and any handoff that followed it.
        # The session has exited by now; nobody else is coming back for this file.
        self._commit_record(
            handle.task_id,
            f"record run {handle.run_id} as {outcome.value}",
            directory=handle.directory,
        )

    def _resolve_deferred_escalation(self, handle: RunHandle) -> None:
        """Keep the promise a scripted finish made about *this* run (task-390).

        A finish that escalates hands the ball to ``agent`` and then asks whether anyone
        is there to take it. When the answer was "this run is live", the escalation wrote
        its own id onto this run's directory and stopped short of a final answer -- because
        a live run is evidence about now and the question is about next. This is where the
        question gets asked again, with the run gone and the answer knowable.

        Placed after the terminal record and the lock release, so the re-ask sees a task
        with no live run and a ledger that agrees. Placed before ``_commit_record`` so one
        commit still covers everything this settle wrote.
        """
        from agentjobs.dispatch.finish import ESCALATION_PENDING, resolve_deferred_escalation

        finish_id = handle.directory.read_meta().get(ESCALATION_PENDING)
        if not isinstance(finish_id, str) or not finish_id:
            return
        resolve_deferred_escalation(
            manager=self.manager,
            project_id=self.resolution.project_id,
            task_id=handle.task_id,
            finish_id=finish_id,
            home=self.home,
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
                "driver": self.runner.driver.value,
                "posture": self.posture.posture.value,
                **self.posture.as_data(),
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

        # A batch run's worker is the process started here, so it inherits the
        # environment directly and needs no settings hop.
        credential = mint_run_credential(directory.path, run_id)

        stdout_file = (directory.path / STDOUT_FILENAME).open("w", encoding="utf-8")
        stderr_file = (directory.path / STDERR_FILENAME).open("w", encoding="utf-8")
        try:
            # argv is a list and there is no shell. The two branches are written out
            # rather than unpacked from a dict so the platform difference stays legible.
            if os.name == "nt":
                process = subprocess.Popen(
                    argv,
                    cwd=str(self.project_root),
                    env=self._environment(directory, run_id, credential),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                process = subprocess.Popen(
                    argv,
                    cwd=str(self.project_root),
                    env=self._environment(directory, run_id, credential),
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
