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
    working_tree_clean,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import DispatchTrigger, Lifecycle, LogEntry, Task
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


def dispatch_task(
    *,
    manager: TaskManager,
    project: Project,
    project_config: Dict[str, object],
    request: DispatchRequest,
    home: Optional[Path] = None,
    api_base: str = "http://localhost:8765",
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
    """
    task = manager.get_task(request.task_id)
    if task is None:
        raise DispatchRefused(f"No task {request.task_id!r} in project {project.id!r}.")

    if task.lifecycle is Lifecycle.CLOSED:
        raise TaskClosedError(
            f"{task.id} is closed ({(task.outcome.value if task.outcome else 'no outcome')}). "
            "Reopen it before dispatching an agent at it."
        )

    # Gate 1-4 from task-068, including the sentinel. Re-checked at spawn time by the
    # runner: this proves dispatch was permitted when it was asked, not for the lifetime
    # of the answer.
    resolution = assert_dispatch_permitted(project.id, home, group=request.group)

    causing = resolve_causing_entry(task, request.caused_by)
    assert_human_clocked(project_config, causing)
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

    if resolution.settings.require_clean_tree and not working_tree_clean(project.root):
        raise DirtyTreeError(
            f"{project.root} has uncommitted changes. An autonomous agent committing on "
            "top of in-flight work entangles the two, and unpicking that is hardest "
            f"exactly when you least expect it. Current HEAD is {git_head(project.root)}."
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

    handle.lock = lock
    return handle


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
