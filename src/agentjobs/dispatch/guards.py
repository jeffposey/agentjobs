"""The preconditions that stand between an HTTP request and a running agent.

Task-068 answered *may this machine dispatch for this project at all*. This module
answers the harder question: *may this particular dispatch happen, now, caused by this*.
The two are separate because the first is configuration and the second is evidence.

The load-bearing one is `assert_human_clocked`. Everything else here is a limit;
that one is a structural property:

    A dispatch may only be caused by a log entry whose actor is a human.

Which makes the circular failure mode -- agent finishes, agent starts, repeat -- *not
representable* rather than merely capped. Counters and cooldowns defend a loop that is
still allowed to exist; this rule removes the loop. It is deliberately one function with
its own tests rather than a condition spread across call sites, because a rule enforced
in three places is a rule enforced in two places as soon as someone adds a fourth.

The permanent cost is real and worth restating: "agent finishes, the next agent picks up
automatically" will never work. Every turn of the wheel costs one human click.

**What task-188 changed, and what it deliberately did not.** The rule above is intact
and is still asserted on every path. What changed is *where the entry it reads comes
from*: a caller that names the human doing the clicking gets that human's authorising
entry **written to the task record first**, and the rule is then evaluated against the
stored entry exactly as before.

Read that twice, because the skim-reading of it is wrong. This is **not** "trust an
actor supplied by the request". The request supplies an identity claim -- the same claim
`POST /log` and `POST /approve` have always accepted on this unauthenticated localhost
API -- and the server validates it against the project's configured actors, refuses
anything that is not `kind: human`, and *persists* the entry. The evidence
`assert_human_clocked` reads is still a row in the append-only log on disk, resolved
from storage at spawn time. Design section 2's forgeability requirement is untouched:
nothing here lets a request supply its own justification, only cause one to be recorded
under a name the project already recognises. The difference between those two is the
whole of this design.

Why it was worth doing: the human-clocked check was standing in for two questions at
once -- *who authorised this run* and *does the agent have enough to work from* -- and
answering the first by proxying through an artifact of the second. Measured against this
project's own backlog on 2026-08-20, it refused 72 of 74 open tasks, because the
`transition` entry the manager writes when an agent files a task is attributed to that
agent, so every agent-filed task failed from birth. Sufficiency is now asked directly,
of the spec (`record_can_brief`), and it is the only thing that stops to ask a human for
text.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from agentjobs.actors import Actor, load_actors
from agentjobs.dispatch.config import (
    DispatchError,
    DispatchResolution,
    assert_dispatch_permitted,
)
from agentjobs.dispatch.config import DispatchRunner as ConfigRunner
from agentjobs.dispatch.ledger import RunLockTimeout, acquire_run_lock
from agentjobs.dispatch.runner import (
    META_FILENAME,
    DispatchRunner,
    RunHandle,
    git_head,
    runs_root,
    uncommitted_paths,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import DispatchTrigger, Lifecycle, LogEntry, LogEntryType, Task
from agentjobs.projects import Project

TERMINAL_RUN_STATUSES = frozenset({"finished", "cancelled", "failed"})
"""Run statuses that mean nothing is executing any more.

Anything else -- including ``starting`` and ``parked`` -- counts as live. A run whose
meta says ``starting`` either is about to execute or died before it could say otherwise,
and both of those should block a second dispatch of the same task.
"""


# ----- refusals ---------------------------------------------------------------


class DispatchRefused(DispatchError):
    """Base for a dispatch that could have happened but must not."""

    reason = "dispatch_refused"


class NoCausingEntryError(DispatchRefused):
    """There is no log entry to attribute the dispatch to."""

    reason = "no_causing_entry"


class CausingActorNotHumanError(DispatchRefused):
    """The entry that would cause this dispatch was written by an agent."""

    reason = "not_human_clocked"


class AuthorizerNotHumanError(DispatchRefused):
    """The identity a caller offered as the authoriser of this run is not a human.

    Distinct from `not_human_clocked`, which is about an entry that already exists. This
    one refuses *before* anything is written, so a bad identity never leaves a row in an
    append-only log. An unconfigured id is refused rather than assumed human, for the
    same reason `assert_human_clocked` refuses one: "we do not know who this is" must
    not be able to start a process on someone's machine.
    """

    reason = "authorizer_not_human"


class ConflictingAuthorizationError(DispatchRefused):
    """The caller both named an existing causing entry and asked to write a new one.

    Refused rather than resolved by precedence. The two mean different things -- *this
    entry authorised the run* versus *I am authorising it now* -- and silently picking
    one would record an authorisation the caller did not intend.
    """

    reason = "conflicting_authorization"


class RecordCannotBriefError(DispatchRefused):
    """The task record has no specification, so there is nothing to brief an agent with.

    The one case that stops to ask a human for text. See `record_can_brief` for what is
    measured and why it is not `ball_prompt`.
    """

    reason = "insufficient_record"


class UnknownRunnerActorError(DispatchRefused):
    """The runner would act as an identity this project does not configure.

    Checked before the claim rather than discovered afterwards. The claim itself never
    validated the id, so a dispatch used to succeed and leave the task owned by an actor
    that no managed write could act as -- the agent could not log progress, hand off or
    close under the identity that owned its own work.
    """

    reason = "unknown_runner_actor"


class TaskClosedError(DispatchRefused):
    """The task is closed. Dispatching would start work on something finished."""

    reason = "task_closed"


class LiveRunExistsError(DispatchRefused):
    """This task already has a run that has not reached a terminal state."""

    reason = "live_run_exists"


class ConcurrencyLimitError(DispatchRefused):
    """The machine is already running as many agents as it is configured to."""

    reason = "concurrency_limit"


class DirtyTreeError(DispatchRefused):
    """The project has uncommitted changes and this project requires a clean tree."""

    reason = "dirty_tree"


class ClaimLostError(DispatchRefused):
    """Someone else claimed the task first, so nothing was started."""

    reason = "claim_lost"


class OwnerMismatchError(DispatchRefused):
    """The task is owned by an actor other than the runner's agent."""

    reason = "owner_mismatch"


# ----- the human-clocked rule -------------------------------------------------


def resolve_causing_entry(task: Task, caused_by: Optional[int] = None) -> LogEntry:
    """The log entry a dispatch is attributed to.

    Defaults to the newest entry, which is the one a person just wrote by clicking
    something. An explicit ``caused_by`` is accepted so a caller can name the entry it
    means, and is validated rather than trusted.
    """
    if not task.log:
        raise NoCausingEntryError(
            f"{task.id} has no log entries, so there is nothing a dispatch could be "
            "caused by. Every dispatch traces to a human act (design section 2)."
        )
    if caused_by is None:
        return task.log[-1]
    for entry in task.log:
        if entry.id == caused_by:
            return entry
    raise NoCausingEntryError(
        f"{task.id} has no log entry {caused_by}. Newest is {task.log[-1].id}."
    )


def record_can_brief(task: Task) -> bool:
    """Whether this record, on its own, could brief an agent that has never seen it.

    **Keyed on `spec.description`, and on nothing else.** That is the working
    specification -- the field the resumption contract makes load-bearing -- and an
    empty one is the only state that unambiguously means *there is nothing here to work
    from*.

    Two other candidates were considered and rejected, both deliberately:

    - **`ball_prompt`.** Empty on every `ready` task in this project's backlog (69 of
      69) *and correctly so*: a `ready`/`agent`/`available` task has not been handed to
      anyone, it is in the pool, so there is no current ask to state. A check keyed on
      it fires on 100% of the tasks you dispatch from, which teaches the human to click
      through the box without reading it -- the exact failure this function exists to
      avoid.
    - **An empty `acceptance[]`.** A genuine call, and the answer is no. Two of this
      project's 74 open tasks have no acceptance criteria and both have a full
      description; they are exploratory tasks whose record plainly *can* brief an agent.
      Firing on them would reintroduce ceremony on tasks that do not need it, to buy a
      criterion the agent is usually expected to propose anyway. Missing acceptance
      criteria are a grooming problem, not an authorisation one.

    Measured 2026-08-20 against the live corpus: 0 of 74 open tasks fail this, which is
    what "special occasion" is supposed to mean.
    """
    return bool(task.spec.description.strip())


def assert_authorizer_is_human(config: Dict[str, object], actor_id: str) -> Actor:
    """Refuse unless the id a caller is clicking as is a configured human.

    Checked before any write. The entry this authorises must name a real person: one
    attributed to nobody, or to whatever `default_user` happens to be regardless of who
    clicked, is worse than the ceremony it replaces, because it looks like evidence and
    is not.

    Equality with the project's `default_user` is deliberately *not* required, unlike
    the review endpoints. Those need to know which person is at the keyboard; this needs
    to know the entry names a real one. A project that eventually configures several
    humans (task-066) should be able to dispatch as any of them, and the log entry
    records which.
    """
    actor = actor_kind(config, actor_id)
    if actor is None:
        known = ", ".join(sorted(load_actors(config))) or "none"
        raise AuthorizerNotHumanError(
            f"{actor_id!r} is not an actor this project configures, so a dispatch "
            f"authorised by them could not be attributed to anyone. Configured actors: "
            f"{known}. Add them to 'actors:' in .agentjobs/config.yaml with "
            "'kind: human'."
        )
    if not actor.is_human:
        raise AuthorizerNotHumanError(
            f"{actor_id!r} is an agent, and an agent may not authorise a dispatch "
            "(design section 2). This is not a configuration option, and it is what "
            "keeps an agent-starts-agent loop impossible rather than merely capped."
        )
    return actor


def compose_authorization_body(actor: Actor, surface: Optional[str] = None) -> str:
    """The sentence written when a human dispatches without typing anything.

    Composed here rather than sent by the client, on the same principle that makes
    `promote` compose its own sentence when a human promotes without a note: the record
    should read as a record, and a body the caller supplies is a body the caller can get
    wrong. Naming the surface matters because "who clicked" and "what they clicked" are
    both part of what a later reader is trying to reconstruct.

    It describes an **authorisation, not an outcome**, and the distinction is the whole
    reason for the wording. The entry is written inside the run lock and before the
    claim -- deliberately, because that ordering is what makes it evidence rather than
    decoration -- so the spawn can still be refused after it lands, and the log is
    append-only, so nothing can take it back. A body reading "Dispatched by ..." beside
    no `dispatch` entry would be the one sentence this feature can write into a record
    that is not true. "... authorised a dispatch" is true either way: the human did
    authorise it, and whether a run followed is what the entries after it say.
    """
    where = f" from {surface}" if surface else ""
    return (
        f"{actor.display_name} authorised a dispatch of this task{where}. No extra "
        "instruction was given: the task record is the brief."
    )


def assert_runner_actor_known(config: Dict[str, object], runner: ConfigRunner) -> None:
    """Refuse unless the identity this runner writes as is a configured actor.

    A project that has configured *no* actors accepts any id -- the same allowance
    ``validate_actor`` makes for a freshly initialised project, and refusing here would
    make dispatch impossible on one. Where a vocabulary exists, an id outside it is
    refused *now* rather than at the first write the dispatched agent attempts.
    """
    actors = load_actors(config)
    if not actors:
        return
    if runner.actor_id in actors:
        return
    known = ", ".join(sorted(actors)) or "none"
    hint = (
        f"Set 'actor:' on runner {runner.name!r} in ~/.agentjobs/dispatch.yaml to one of "
        f"them, or add {runner.actor_id!r} to 'actors:' in .agentjobs/config.yaml."
    )
    raise UnknownRunnerActorError(
        f"Runner {runner.name!r} would act as {runner.actor_id!r}, which this project "
        f"does not configure as an actor. Configured actors: {known}. A dispatched agent "
        f"must be able to log progress and hand off under the identity that owns its "
        f"task, and it cannot do that as an unknown actor. {hint}"
    )


def actor_kind(config: Dict[str, object], actor_id: str) -> Optional[Actor]:
    """Look one actor up in the project's configured vocabulary."""
    return load_actors(config).get(actor_id)


def assert_human_clocked(config: Dict[str, object], entry: LogEntry) -> Actor:
    """Refuse unless the causing entry was written by a configured human.

    **The rule, and not a configuration option.** An agent handoff never causes a
    dispatch, in any mode.

    An unconfigured actor is refused rather than assumed human. That is stricter than
    `validate_actor`, which accepts any id on a project that has configured none -- a
    reasonable default for writing a note and the wrong one here, because "we do not
    know who this is" must not be able to start a process on someone's machine.
    """
    actor = actor_kind(config, entry.actor)
    if actor is None:
        raise CausingActorNotHumanError(
            f"Log entry {entry.id} was written by {entry.actor!r}, which this project "
            "does not configure as an actor. A dispatch must be caused by a known "
            "human, and an unknown actor cannot be shown to be one. Add them to "
            "'actors:' in .agentjobs/config.yaml with 'kind: human'."
        )
    if not actor.is_human:
        raise CausingActorNotHumanError(
            f"Log entry {entry.id} ({entry.type.value}) was written by {entry.actor!r}, "
            "an agent. A dispatch may only be caused by a human act (design section 2, "
            "D4) -- which is what makes an agent-starts-agent loop impossible rather "
            "than merely capped. Act on the task yourself, then dispatch."
        )
    return actor


# ----- what is already running ------------------------------------------------


@dataclass(frozen=True)
class LiveRun:
    """A run directory that has not reached a terminal state."""

    run_id: str
    task_id: str
    project_id: str
    status: str
    path: Path


def live_runs(home: Path) -> List[LiveRun]:
    """Every non-terminal run on this machine, read from the run directories.

    A directory scan with no shared index, for the same reason tasks are one file each:
    nothing to contend on, and a run that was never finished still leaves its row.

    This is the minimal read the guards need. The full ledger -- listing, cancelling,
    reconciling on startup -- is task-072; when that lands this should call it rather
    than keep a second reader.
    """
    root = runs_root(home)
    if not root.is_dir():
        return []
    found: List[LiveRun] = []
    for directory in sorted(root.iterdir()):
        meta_path = directory / META_FILENAME
        if not meta_path.is_file():
            continue
        try:
            meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            # An unreadable run is counted as live: it cannot be shown to have ended,
            # and treating it as finished would let a second run start beside it.
            meta = {}
        status = str(meta.get("status") or "unknown")
        if status in TERMINAL_RUN_STATUSES:
            continue
        found.append(
            LiveRun(
                run_id=str(meta.get("run_id") or directory.name),
                task_id=str(meta.get("task_id") or ""),
                project_id=str(meta.get("project_id") or ""),
                status=status,
                path=directory,
            )
        )
    return found


# ----- the whole precondition chain -------------------------------------------


@dataclass(frozen=True)
class DispatchRequest:
    """One request to start an agent, with everything the guards need to judge it."""

    task_id: str
    caused_by: Optional[int] = None
    trigger: DispatchTrigger = DispatchTrigger.MANUAL
    group: Optional[str] = None
    """Runner group this dispatch asks for -- the narrowest rung of the ladder.

    Naming a group is a request about *cost and capability*, never about permission: it
    is handed to ``assert_dispatch_permitted``, which opens all four gates before it
    looks at groups at all. There is no group name that makes a refused dispatch
    proceed, and one that names a group this machine does not define is refused rather
    than quietly falling back to the project's runner.
    """

    authorized_by: Optional[str] = None
    """Actor id of the human doing the clicking, when there is one.

    Set, the dispatcher **writes** that human's authorising entry onto the task and then
    resolves the causing entry from the stored record, so ``assert_human_clocked`` reads
    a real row on disk exactly as it always has. Unset -- the CLI, MCP and
    auto-dispatch -- the causing entry is whatever the log already holds, unchanged.

    This is an identity claim, not evidence, and the difference is load-bearing. The
    server validates it against the project's configured actors and refuses anything
    that is not ``kind: human``; what it never does is take a *justification* from the
    request. See this module's docstring.
    """

    authorization_note: Optional[str] = None
    """What the human typed, when the record could not brief an agent without it.

    Becomes the body of the authorising entry, so one action serves both purposes: the
    text that was missing is now on the record, and the entry that authorises the run is
    the one that carries it. Optional in every other case, and never a gate -- task-162
    owns the affordance for adding instructions to a dispatch you are already making.
    """

    surface: Optional[str] = None
    """Where the click happened, for the composed sentence. Prose for a reader, never
    read back by any check."""


def dispatch_task(
    *,
    manager: TaskManager,
    project: Project,
    project_config: Dict[str, object],
    request: DispatchRequest,
    home: Optional[Path] = None,
    api_base: Optional[str] = None,
) -> RunHandle:
    """Check every precondition, claim the task, and start a run.

    Order matters and is chosen so that the cheapest and most consequential refusals
    come first: configuration, then authority, then contention, then state. Nothing is
    written until every check has passed, and the claim happens *before* the spawn so
    that losing a race costs a rejected HTTP request rather than a model call somebody
    pays for.

    Raises a `DispatchRefused` subclass naming the gate that refused. Never queues: a
    concurrency limit that queues turns a click into a promise to spend money later, at
    a moment nobody is watching. "Busy, try again" is worse UX and better behaviour.

    ``api_base`` is passed through untouched, including ``None``: a caller that knows the
    address the server answered on says so, and everyone else leaves it to
    ``dispatch/address.py``. Repeating a default here is what let the HTTP and CLI paths
    disagree about what a dispatched agent was told (task-154).
    """
    task = manager.get_task(request.task_id)
    if task is None:
        raise DispatchRefused(f"No task {request.task_id!r} in project {project.id!r}.")

    authorizer_id = (request.authorized_by or "").strip() or None
    note = (request.authorization_note or "").strip() or None
    if authorizer_id is not None and request.caused_by is not None:
        raise ConflictingAuthorizationError(
            f"This dispatch named log entry {request.caused_by} as its cause *and* asked "
            f"to write a new authorising entry as {authorizer_id!r}. Send one or the "
            "other: citing an entry and creating one are different acts."
        )

    if task.lifecycle is Lifecycle.CLOSED:
        raise TaskClosedError(
            f"{task.id} is closed ({(task.outcome.value if task.outcome else 'no outcome')}). "
            "Reopen it before dispatching an agent at it."
        )

    # Gate 1-4 from task-068, including the sentinel. Re-checked at spawn time by the
    # runner: this proves dispatch was permitted when it was asked, not for the lifetime
    # of the answer.
    resolution = assert_dispatch_permitted(project.id, home, group=request.group)

    # Two ways in, and they differ only in where the authorising entry comes from.
    #
    #   * A caller that names the human clicking (the browser) gets that human's entry
    #     *written* below, inside the run lock, once every refusal that can be judged
    #     without a write has passed. Nothing is checked against the request after that:
    #     the entry is re-read from storage and put through `assert_human_clocked` like
    #     any other.
    #   * Everyone else (CLI, MCP, auto-dispatch) resolves the entry the log already
    #     holds, exactly as before.
    #
    # Only what can be judged *before* writing happens here; the write is deferred so a
    # dispatch refused for a live run or a busy machine leaves no authorisation behind
    # for a run that never started.
    causing: Optional[LogEntry] = None
    authorizer: Optional[Actor] = None
    if authorizer_id is None:
        causing = resolve_causing_entry(task, request.caused_by)
        assert_human_clocked(project_config, causing)
    else:
        authorizer = assert_authorizer_is_human(project_config, authorizer_id)
        if note is None and not record_can_brief(task):
            raise RecordCannotBriefError(
                f"{task.id} has no spec.description, so there is nothing for an agent to "
                "work from and nothing this dispatch could be attributed to beyond the "
                "click itself. Say what the agent should do, and that becomes both the "
                "brief and the authorising entry."
            )
    assert_runner_actor_known(project_config, resolution.runner)

    running = live_runs(_home(home, resolution))
    for run in running:
        if run.task_id == task.id:
            raise LiveRunExistsError(
                f"{task.id} already has run {run.run_id} in state {run.status!r}. "
                "One live run per task, always -- a second would have two agents "
                "editing the same repository with the same task record."
            )
    if len(running) >= resolution.limits.max_concurrent_runs:
        raise ConcurrencyLimitError(
            f"{len(running)} run(s) already active and this machine allows "
            f"{resolution.limits.max_concurrent_runs}. Refused rather than queued: a "
            "queue turns this click into a promise to spend money later, when nobody "
            "is watching. Cancel a run or try again."
        )

    # AgentJobs' own tasks directory is excluded from this. A project that keeps its task
    # records in the repository being dispatched -- this one does -- has that directory
    # dirtied by dispatch itself, both before the spawn (the claim) and after the run (the
    # terminal dispatch_result entry). Counting those refused every dispatch on the
    # strength of AgentJobs' own writes; see task-182 and the design doc.
    if resolution.settings.require_clean_tree:
        dirty = uncommitted_paths(project.root, ignore=[manager.storage.tasks_dir])
        if dirty is None or dirty:
            named = ", ".join(sorted(dirty)[:5]) if dirty else "git could not be read"
            raise DirtyTreeError(
                f"{project.root} has uncommitted changes ({named}). An autonomous agent "
                "committing on top of in-flight work entangles the two, and unpicking "
                "that is hardest exactly when you least expect it. Current HEAD is "
                f"{git_head(project.root)}."
            )

    # Taken before the claim and held for the run's lifetime. The storage lock the
    # claim uses protects a write lasting microseconds; this one protects a process
    # lasting half an hour, which is why it cannot be the same `with` block.
    machine_home = _home(home, resolution)
    try:
        lock = acquire_run_lock(machine_home, task.id, timeout=1.0)
    except RunLockTimeout as exc:
        # The same fact the live-run scan reports, established by the primitive that is
        # actually atomic. The scan reads a directory and can lose a race with itself;
        # this cannot, which is why it gets the final say.
        raise LiveRunExistsError(str(exc)) from exc

    try:
        if authorizer is not None:
            task, causing = _write_authorizing_entry(
                manager,
                task,
                authorizer=authorizer,
                note=note,
                surface=request.surface,
            )
        if causing is None:  # pragma: no cover - one branch or the other always sets it
            raise DispatchRefused(f"{task.id} produced no causing entry to dispatch on.")

        # The invariant, asserted once on the final entry whichever path produced it: a
        # dispatch is caused by a stored log entry whose actor this project configures
        # as a human. The authorising-entry path does not bypass this check, it
        # satisfies it -- which is why an agent still cannot cause a dispatch even
        # though the dispatcher now writes entries of its own.
        assert_human_clocked(project_config, causing)

        task = _claim_or_verify(manager, task, resolution.runner.actor_id)

        runner = DispatchRunner(
            manager=manager,
            resolution=resolution,
            project_root=project.root,
            home=machine_home,
            api_base=api_base,
        )
        handle = runner.start(
            task,
            actor=causing.actor,
            caused_by=causing.id,
            trigger=request.trigger,
        )
    except BaseException:
        # Nothing started, so nothing will release it later.
        lock.release()
        raise

    # Only now does a run id exist to write into the lock. Until this line the lock says
    # `run=` empty, and a lock that names no run can only be judged by the pid that took
    # it -- which is the weaker of the two rules in `stale_lock_reason` and the one that
    # cannot answer "has this run ended?". Naming it is what makes the lock reclaimable
    # against the ledger, and therefore what stops a leaked one being permanent
    # (task-190).
    lock.adopt(handle.run_id)
    handle.lock = lock
    return handle


def _write_authorizing_entry(
    manager: TaskManager,
    task: Task,
    *,
    authorizer: Actor,
    note: Optional[str],
    surface: Optional[str],
) -> tuple[Task, LogEntry]:
    """Record the human's authorisation, then read it back out of storage.

    The re-read is the point, and it is not defensive padding. `add_log_entry` hands
    back the object it just persisted, and resolving from *that* would leave the
    causing entry one in-memory hop away from the request that asked for it. Going
    through `manager.get_task` means the entry `assert_human_clocked` judges came off
    disk, so design section 2's "resolved from the stored task, never taken from the
    request body" is true in the literal sense the sentence means -- and a change that
    quietly started trusting the request would have to delete this function to work.

    A `note` entry, not a new type. It is exactly what a human writing the authorising
    note by hand produced before this existed (task-185's control, which stays), so the
    record reads the same whichever way the human got there, and nothing downstream has
    to learn a new type to understand an old task.
    """
    body = note or compose_authorization_body(authorizer, surface)
    manager.add_log_entry(
        task.id,
        actor=authorizer.id,
        type=LogEntryType.NOTE,
        body=body,
        # Descriptive only. Nothing reads this back to decide anything -- if it did, a
        # caller could set it on a hand-written note and the flag would become the
        # forgeable evidence this whole design avoids.
        data={"authorizes_dispatch": True, "surface": surface}
        if surface
        else {"authorizes_dispatch": True},
    )
    stored = manager.get_task(task.id)
    if stored is None:  # pragma: no cover - the task was read moments ago
        raise DispatchRefused(f"{task.id} disappeared while authorising a dispatch.")
    return stored, stored.log[-1]


def _claim_or_verify(manager: TaskManager, task: Task, agent: str) -> Task:
    """Claim a ready task, or check that an active one is already ours.

    Claiming first is the whole point: `claim_task` runs under task-055's per-task lock,
    so of two simultaneous dispatches exactly one proceeds and the other is refused
    having started nothing. Letting the *child* claim itself after spawn would mean two
    processes start and one discovers, after paying for a model call, that it lost.
    """
    if task.lifecycle is Lifecycle.READY:
        try:
            return manager.claim_task(task.id, agent=agent)
        except ValueError as exc:
            raise ClaimLostError(
                f"Could not claim {task.id}, so nothing was started: {exc}"
            ) from exc

    owner = task.assignment.owner
    if owner is not None and owner != agent:
        raise OwnerMismatchError(
            f"{task.id} is owned by {owner!r} and this project dispatches as {agent!r}. "
            "Release it first, or dispatch the runner that owns it."
        )
    return task


def _home(home: Optional[Path], resolution: DispatchResolution) -> Path:
    """The AgentJobs home the resolution was read from, so runs land beside it."""
    if home is not None:
        return Path(home)
    config_path = resolution.config.path
    if config_path is not None:
        return config_path.parent
    from agentjobs.projects import default_home

    return default_home()
