"""Tasks an agent is supposed to be working that nothing is working (task-499).

**The incident.** task-421, 2026-09-19. An interactive session supervising it made its
last commit at 16:05 CDT and hit its usage limit minutes later with a gate running. It
never handed off. The task then read ``agent``/``revise`` for twenty-two hours -- a state
that says an agent is on this, while no agent was -- and nothing on any surface said
otherwise. It was found by the owner asking why his notifications had gone quiet.

**Why nothing caught it.** Every stall detector in ``dispatch/`` is keyed on a run
record. ``auth.read_limit_stall`` reads a run's transcript; ``auth_recovery`` polls run
handles; ``idle_sessions`` walks processes; the poller iterates runs. A holder that
registered nothing is not a stalled run to any of them, it is nothing at all. task-320
closed this class by *requiring* ``agentjobs run register``, which is a convention, and
this session did not follow it. Tightening the rule cannot fix this, because the rule is
what failed.

**So the signal is read off the task record**, which is the one thing that always
exists. An open task, claimed (``lifecycle: active``), with the ball on an agent, whose
newest log entry is older than a threshold, and with no live run doing anything about
it. That query needs no process inspection, no transcript and no cooperation from
whatever was working it -- so it catches the interactive session that died, the
dispatched run that was orphaned, the agent that forgot to register and the agent that
simply wandered off, without distinguishing between them. It should not need to.

**Derived, never written.** Nothing here writes a ``ball``, a ``ball_prompt`` or a
handoff. A derived signal clears itself the moment a log entry lands; a written ball has
to be retracted, and a parent left asking for something already resolved is the defect
task-467 exists to fix. It is not repeated here.

**Reported through the attention episode.** A stalled task is work waiting on the owner:
only he can re-dispatch it, take it over, or let it sit. :mod:`agentjobs.attention`
folds these into the same waiting set the badge, the desktop notifier and the phone all
read, so there is one episode and one acknowledgment rather than a second notification
policy competing with it.

Two states are deliberately **not** stalls:

* ``lifecycle: ready`` with the ball on an agent. That is the backlog. Every unclaimed
  task in the queue reads ``agent``/``available``, and the oldest of them have been
  quiet for weeks; measured over this repository's corpus on 2026-09-20, counting them
  turned 3,101 agent-held quiet stretches into 4,617 and the 90th percentile from 16
  minutes into 67.
* ``agent``/``hold``. A hold is a deliberate park with a release condition on the
  record, so nobody is meant to be on it -- and it is the one agent-side reason no
  dispatch path will act on. The same corpus puts the median hold at eighteen hours,
  which is what reporting them would look like.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    cast,
)

from .models_v2 import Ball, BallReason, LabelledTask, Lifecycle, Task

StallReason = Literal["no_agent", "undelivered_handback"]
"""The two ways a task can be in this set. A closed alphabet, because the API model
and the client both branch on it and neither may be handed a third value."""

NO_AGENT: StallReason = "no_agent"
"""Nothing is running against this task at all, and it has been quiet past the threshold."""

UNDELIVERED_HANDBACK: StallReason = "undelivered_handback"
"""A live run holds feedback it was told about and never given, and neither has moved.

The second case, recorded on task-499 from a live incident on 2026-09-20 rather than
reasoned about: task-176-dispatch-on-create handed off for review, the owner requested
changes, and the dispatcher correctly declined to start a second run because one was
still live -- recording that the feedback would be delivered when the run settled. The
run never settled. Every run-side signal stayed healthy: a process existed, its status
was ``running``, its health was ``working``. Asking the run whether it is alive gets yes
and learns nothing; the thing that was wrong is visible only on the task, where the ball
moved to ``agent`` and no agent entry followed it.
"""


class LiveRun(Protocol):
    """What this module needs to know about a run: its id, and whether it owes a handback.

    A structural type rather than :class:`~agentjobs.dispatch.ledger.RunRecord`, so the
    predicate can be tested against two fields instead of a run directory, and so this
    module does not pull the dispatch stack into everything that reads a task.
    """

    @property
    def run_id(self) -> str:
        ...

    @property
    def handback_pending(self) -> Optional[int]:
        ...


@dataclass(frozen=True)
class Stall:
    """One task nothing is working, and the evidence for saying so.

    Carries the numbers rather than a sentence because two surfaces render it and a
    third reads it in a test: matching on the prose of a label is what ENGINEERING.md's
    rendered-value rule exists to prevent.
    """

    task_id: str
    reason: StallReason
    quiet_since: datetime
    """The newest log entry's timestamp -- what the threshold is measured from."""
    quiet_seconds: float
    threshold_seconds: float
    run_id: str = ""
    """The run the undelivered handback is addressed to. Empty for :data:`NO_AGENT`."""

    def describe(self) -> str:
        """One line for a log, a CLI row or a test failure."""
        quiet = _duration_phrase(self.quiet_seconds)
        if self.reason == UNDELIVERED_HANDBACK:
            return f"{self.task_id}: feedback undelivered to {self.run_id} for {quiet}"
        return f"{self.task_id}: no agent on it, quiet for {quiet}"


def _duration_phrase(seconds: float) -> str:
    """``3h 12m`` -- minutes below an hour, never seconds; this is an hours-scale signal."""
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def last_activity(task: Task) -> datetime:
    """When something last happened *to* this task, as the record tells it.

    The newest log entry, falling back to ``created`` for a task with no log at all.
    Deliberately not ``updated``: that moves for an edit to a field, and an edit is not
    somebody working. The log is what a handoff, a claim, a decision and a progress note
    all write, which makes it the honest measure of "an agent is doing something here".
    """
    newest = max((entry.ts for entry in task.log), default=None)
    return _aware(newest or task.created)


def _aware(moment: datetime) -> datetime:
    """A stored timestamp as an aware one, so a subtraction cannot raise."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def quiet_since(task: LabelledTask, newest: Optional[Mapping[str, datetime]]) -> datetime:
    """:func:`last_activity`, for a record or for a row plus a lookup.

    The detector reads six things off a task and five of them are on a listing row; the
    sixth is this, and it is the only reason the dashboard would have to load every
    record in the project to find out whether anything is stalled (task-498). So the
    caller may pass the newest log timestamp per task id instead, read in one bounded
    query over the handful of tasks :func:`is_candidate` admitted.

    **The answer is the same either way, and that is the point.** ``max(ts)`` over a
    task's rows is ``max(ts)`` over its log; a task with no rows is absent from the
    lookup and falls back to ``created``, which is exactly what :func:`last_activity`
    does for an empty log. The denormalised ``last_activity_at`` column would have been
    cheaper still and is *not* this: it holds the last entry appended rather than the
    newest one, and ``tests/test_stalled_tasks.py`` pins the difference on purpose.
    """
    if newest is None:
        return last_activity(cast(Task, task))
    found = newest.get(task.id)
    return _aware(found) if found is not None else _aware(task.created)


def is_candidate(task: LabelledTask) -> bool:
    """Whether this task is one an agent is supposed to be working right now.

    Claimed and held by an agent, and not deliberately parked. See the module docstring
    for why ``ready`` and ``hold`` are excluded -- both would otherwise make the ordinary
    backlog look like an outage.
    """
    return (
        task.is_open
        and task.lifecycle is Lifecycle.ACTIVE
        and task.ball is Ball.AGENT
        and task.ball_reason is not BallReason.HOLD
    )


def stall_for(
    task: LabelledTask,
    run: Optional[LiveRun],
    *,
    minutes: int,
    handback_minutes: int,
    now: Optional[datetime] = None,
    since: Optional[datetime] = None,
) -> Optional[Stall]:
    """Whether *task* is stalled, given the live run against it (or the absence of one).

    Three outcomes, and the middle one is the point:

    * **no live run** -- nothing is working it, so the threshold is the long one.
    * **a live run holding an undelivered handback** -- something is running and is not
      going to act on what the task is waiting for. The shorter threshold.
    * **a live run with nothing pending** -- not a stall. Something is working it, and
      whether *that* has hung is a question the run-keyed detectors already own.
    """
    if not is_candidate(task):
        return None
    if run is not None and run.handback_pending is None:
        return None
    reason = NO_AGENT if run is None else UNDELIVERED_HANDBACK
    limit = minutes if run is None else handback_minutes
    since = since if since is not None else last_activity(cast(Task, task))
    moment = now or datetime.now(timezone.utc)
    quiet = (moment - since).total_seconds()
    if quiet < timedelta(minutes=limit).total_seconds():
        return None
    return Stall(
        task_id=task.id,
        reason=reason,
        quiet_since=since,
        quiet_seconds=quiet,
        threshold_seconds=timedelta(minutes=limit).total_seconds(),
        run_id=run.run_id if run is not None else "",
    )


def stalls(
    tasks: Sequence[LabelledTask],
    runs: Mapping[str, LiveRun],
    *,
    minutes: int,
    handback_minutes: int,
    now: Optional[datetime] = None,
    newest: Optional[Mapping[str, datetime]] = None,
) -> List[Stall]:
    """Every stalled task in *tasks*, quietest first.

    Pure and total: *runs* is the live runs by task id, so a caller with no ledger at
    all -- a test, or a machine that has never dispatched -- passes ``{}`` and gets the
    answer for "nothing is running", which is the honest one.
    """
    found = [
        stall_for(
            task,
            runs.get(task.id),
            minutes=minutes,
            handback_minutes=handback_minutes,
            now=now,
            since=quiet_since(task, newest),
        )
        for task in tasks
    ]
    return sorted(
        (stall for stall in found if stall is not None),
        key=lambda stall: stall.quiet_seconds,
        reverse=True,
    )


# ----- reading the machine ------------------------------------------------------------


def live_runs_by_task(project_id: str, *, home: Optional[Path] = None) -> Dict[str, LiveRun]:
    """This machine's live runs for *project_id*, keyed by the task each is against.

    The ledger is a scan of every run directory the machine has ever had -- 324 of them
    here on 2026-09-20 -- so it is read only once a candidate exists to judge against
    it. See :func:`stalled_in`, which is what enforces that.

    Never raises. A ledger that cannot be read has to cost the signal rather than the
    page that carries it, and the failure is safe in the direction that matters: with no
    runs in hand a quiet task reads as ``no_agent``, which is a report to the owner
    rather than a silence.
    """
    # Imported here rather than at module scope: `dispatch.ledger` pulls in the whole
    # dispatch stack, the execution store and `store_factory`, and this module is
    # imported by `attention`, which is imported by every read of the badge.
    try:
        from .dispatch.ledger import live_runs
        from .projects import default_home
    except Exception:  # noqa: BLE001 - a derived signal may not cost the import
        return {}
    try:
        records = live_runs(home or default_home())
    except Exception:  # noqa: BLE001 - see the docstring
        return {}
    found: Dict[str, LiveRun] = {}
    for record in records:
        if record.project_id != project_id or not record.task_id:
            continue
        # A run carrying an undelivered handback wins the slot, because that is the case
        # worth reporting: if any live run against this task owes it feedback it has not
        # been given, the task is waiting on something nothing will deliver.
        existing = found.get(record.task_id)
        if existing is None or (
            existing.handback_pending is None and record.handback_pending is not None
        ):
            found[record.task_id] = record
    return found


def stalled_in(
    tasks: Sequence[LabelledTask],
    *,
    project_id: str,
    home: Optional[Path] = None,
    now: Optional[datetime] = None,
    settings: Optional["StalledSettings"] = None,
    newest_log_ts: Optional[Callable[[Sequence[str]], Mapping[str, datetime]]] = None,
) -> List[Stall]:
    """The stalled tasks in a corpus already in hand, reading the ledger only if needed.

    The caller passes the corpus because every caller has one: the attention reconcile
    lists the project to find what a person is holding, and the dashboard lists it to
    build every panel. Loading it again here would be a second read of the same rows for
    a signal that is usually empty.

    **Short-circuits twice.** No candidate task means no ledger read, which is the
    ordinary case on a machine where nothing has been claimed and left; and a candidate
    that has not been quiet long enough to be stalled under *either* threshold is
    filtered before the ledger is opened, because whichever branch it takes it cannot be
    reported yet.

    ``newest_log_ts`` lets the corpus be listing rows rather than records. The five other
    things this reads are on a row; the sixth is the newest log timestamp, and a caller
    that passes this resolves it for the handful of ids :func:`is_candidate` admitted
    instead of handing over every log in the project to have one number taken from each
    (task-498). Both short-circuits survive it, and it is asked only about candidates --
    so a corpus with nothing claimed runs no extra query either.
    """
    active = settings or load_settings(home)
    if not active.enabled:
        return []
    moment = now or datetime.now(timezone.utc)
    floor = timedelta(minutes=min(active.minutes, active.handback_minutes)).total_seconds()
    claimed = [task for task in tasks if is_candidate(task)]
    if not claimed:
        return []
    newest = newest_log_ts([task.id for task in claimed]) if newest_log_ts is not None else None
    candidates = [
        task for task in claimed if (moment - quiet_since(task, newest)).total_seconds() >= floor
    ]
    if not candidates:
        return []
    return stalls(
        candidates,
        live_runs_by_task(project_id, home=home),
        minutes=active.minutes,
        handback_minutes=active.handback_minutes,
        now=moment,
        newest=newest,
    )


@dataclass(frozen=True)
class StalledSettings:
    """The thresholds, as :mod:`agentjobs.dispatch.config` parsed them.

    Re-exported here under a name this module owns so that a caller wanting the defaults
    -- a test, a CLI, a machine with no ``dispatch.yaml`` -- does not have to import the
    dispatch configuration to get them.
    """

    enabled: bool = True
    minutes: int = 60
    handback_minutes: int = 30


def load_settings(home: Optional[Path] = None) -> StalledSettings:
    """This machine's thresholds, defaulted, never raising at a reader.

    Same shape and the same reasoning as ``machine_ceiling``: a machine with no dispatch
    configuration has no runs either, so the honest answer is the defaults rather than a
    500 on the surface that renders the badge.
    """
    try:
        from .dispatch.config import DispatchError, load_dispatch_config
    except Exception:  # noqa: BLE001 - as in `live_runs_by_task`
        return StalledSettings()
    try:
        config = load_dispatch_config(home)
    except DispatchError:
        return StalledSettings()
    if config is None:
        return StalledSettings()
    block = config.stalled_tasks
    return StalledSettings(
        enabled=block.enabled,
        minutes=block.minutes,
        handback_minutes=block.handback_minutes,
    )
