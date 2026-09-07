"""A session AgentJobs did not start, telling AgentJobs that it exists.

Every stall protection dispatch has is keyed on a run record. ``poll_live_sessions``
iterates ``live_runs(home)``; a session with no run directory is never iterated, never
polled, and therefore gets none of ``_park_session``, ``_park_auth_stall``,
``_settle_finished_session`` or ``_check_running_stall``. The protections are correct and
simply do not apply to a whole class of session -- the one somebody spawns by hand.

That is not hypothetical. Task-217 sat dead for four hours on 2026-08-23 while its
record read ``agent``/``work`` throughout, and the forensics in task-296 established the
cause: its two siblings in the same epic have run directories and it has none, because
it was never dispatched. ``_park_session`` had been written, correct and deployed for
five days. It never ran, because dispatch did not know the session was there.

**This module closes that by adoption rather than by detection.** A session says "I am
working task X and my id is Y"; AgentJobs verifies the claim against the runner's own
session ledger and writes the same run record a dispatch would have written. From the
next poll the session is followed exactly like a dispatched one, through the existing
code, with no second implementation of any judgement.

Four properties are deliberate, and each one is a refusal somewhere below:

- **A dispatched run registers to nothing.** ``AGENTJOBS_RUN_ID`` naming a live run for
  this task returns "already known" and touches nothing, so ``agentjobs run register``
  is safe to make an unconditional instruction in ALLAGENTS.md. A rule with an exception
  the agent has to evaluate is a rule half of them get wrong.
- **Interactive sessions cannot be registered at all.** A session a person is sitting in
  is the normal, common state that must never be reported as a fault, and adopting one
  would eventually have the poller ``stop`` a session somebody was typing into. Refusing
  by construction is stronger than any heuristic that tries to tell the two apart later.
- **The claimed session is verified before anything is written.** It must be live in the
  runner's own ledger, under this project's root. A run record naming a session nothing
  can find is worse than no run record: it looks covered and is not.
- **Registration is not a dispatch, and does not write a ``dispatch`` entry.** Nobody
  authorised a run and AgentJobs started no process, so the human-clocked chain (design
  section 2, D4) must not be forged by a command that any agent can invoke. What lands
  on the record is a ``note`` naming the session, the run and what the adoption buys.

Two functions hold everything specific to one agent CLI -- ``self_session_id`` and
``session_kind``. A second driver adds a branch there and nowhere else; that is the same
seam ``classify_session`` and ``capture_session_id`` already sit on in ``runner``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from agentjobs.actors import load_actors
from agentjobs.dispatch.config import RunnerDriver, assert_dispatch_permitted
from agentjobs.dispatch.guards import (
    DispatchRefused,
    actor_kind,
    live_runs,
    resolve_machine_home,
)
from agentjobs.dispatch.ledger import RunLockTimeout, acquire_run_lock
from agentjobs.dispatch.runner import (
    DispatchRunError,
    DispatchRunner,
    RunDirectory,
    SessionPhase,
    classify_session,
    git_head,
    new_run_id,
)
from agentjobs.models_v2 import DispatchMode, Lifecycle, LogEntryType
from agentjobs.projects import Project
from agentjobs.store_factory import TaskManagerLike

RUN_ID_ENV = "AGENTJOBS_RUN_ID"
"""What a dispatched session is told its run is called, in ``session_env``'s settings."""


# ----- the two driver-shaped questions ----------------------------------------


def self_session_id(driver: RunnerDriver, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The id this agent's own session has, if its CLI publishes one to the session.

    Returned in the **short** form the rest of dispatch stores -- what
    ``capture_session_id`` reads out of a launcher's output, and what ``attach``,
    ``logs`` and ``stop`` take. The ledger is matched on either form, but a record that
    stored the long one would put a uuid into every ball prompt telling a human what to
    type.

    Claude Code publishes ``CLAUDE_CODE_SESSION_ID`` (the full uuid, whose first group is
    the short id) and ``CLAUDE_JOB_DIR`` (whose last component is the short id) into a
    background session's environment; measured on 2.1.247, 2026-08-27. Codex publishes
    nothing equivalent, so a Codex session has to be given ``--session`` -- which is why
    this returns ``None`` rather than raising: not knowing is an ordinary answer, and the
    caller turns it into a refusal that says what to pass.
    """
    values = env if env is not None else os.environ
    if driver is not RunnerDriver.CLAUDE:
        return None
    raw = (values.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    short = raw.split("-", 1)[0]
    if _is_short_id(short):
        return short
    job_dir = (values.get("CLAUDE_JOB_DIR") or "").strip()
    if job_dir:
        candidate = _last_path_component(job_dir)
        if _is_short_id(candidate):
            return candidate
    return None


def session_kind(row: Mapping[str, object]) -> str:
    """Whether a ledger row is a background session or one a person is sitting in.

    ``kind`` is what Claude Code 2.1.247 answers with. The fallback is the shape that
    distinguished them before it existed and still holds: a background session has the
    short ``id`` the CLI assigns for ``attach``/``logs``/``stop``, and an interactive one
    has only a ``pid`` and its ``sessionId``.
    """
    kind = row.get("kind")
    if isinstance(kind, str) and kind:
        return kind
    return "background" if row.get("id") else "interactive"


def _is_short_id(value: str) -> bool:
    """Whether this looks like the eight-hex id a session ledger keys on."""
    return len(value) == 8 and all(character in "0123456789abcdef" for character in value)


def _last_path_component(value: str) -> str:
    """The last component of a path written with either separator.

    ``Path`` is not used: this value comes out of the environment of a process that may
    have been launched from a shell with the other platform's conventions, and
    ``PurePosixPath`` on Windows would keep the whole backslash string as one component.
    """
    return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


# ----- refusals ---------------------------------------------------------------


class RegistrationRefused(DispatchRefused):
    """A session could not be adopted, and nothing was written."""

    reason = "registration_refused"


class SessionUnnamedError(RegistrationRefused):
    """Neither the caller nor the environment could say which session this is."""

    reason = "session_unnamed"


class SessionUnknownError(RegistrationRefused):
    """The claimed session is not live in the runner's ledger under this project."""

    reason = "session_unknown"


class SessionInteractiveError(RegistrationRefused):
    """The claimed session is one a person is sitting in."""

    reason = "session_interactive"


class RegistrationTaskClosedError(RegistrationRefused):
    """The task is closed, so there is nothing for a session to be working."""

    reason = "task_closed"


class RegistrationRunExistsError(RegistrationRefused):
    """Another live run already holds this task."""

    reason = "live_run_exists"


class DispatchedElsewhereError(RegistrationRefused):
    """This process is a dispatched run for a *different* task."""

    reason = "dispatched_elsewhere"


class UnknownActorError(RegistrationRefused):
    """The identity offered is not in the project's actor vocabulary."""

    reason = "unknown_actor"


# ----- the result -------------------------------------------------------------


@dataclass(frozen=True)
class Registration:
    """What registering did, in the terms the caller has to report."""

    task_id: str
    run_id: str
    session_id: str
    already_known: bool
    detail: str
    directory: Optional[Path] = None
    entry_id: Optional[int] = None
    started_at: Optional[datetime] = None


# ----- the command itself ------------------------------------------------------


def register_session(
    *,
    manager: TaskManagerLike,
    project: Project,
    project_config: Mapping[str, object],
    task_id: str,
    session_id: Optional[str] = None,
    actor: Optional[str] = None,
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Registration:
    """Adopt this session into the run ledger, or refuse saying exactly why.

    Order is the same one ``dispatch_task`` uses and for the same reason: everything
    that can refuse without writing comes first, so a refused registration leaves no
    half-adopted run behind. Nothing here starts, stops or resumes a process.
    """
    values = env if env is not None else os.environ

    task = manager.get_task(task_id)
    if task is None:
        raise RegistrationRefused(f"No task {task_id!r} in project {project.id!r}.")
    if task.lifecycle is Lifecycle.CLOSED:
        raise RegistrationTaskClosedError(
            f"{task.id} is closed, so there is nothing here for a session to be working. "
            "Reopen it first if that is wrong."
        )

    # Gate 1-4, and not merely politeness: the poller calls `assert_dispatch_permitted`
    # per run and skips one it refuses, so a run record written for a project that may
    # not dispatch would be adopted by nothing. Refusing here says that out loud instead
    # of leaving a row that looks covered.
    resolution = assert_dispatch_permitted(project.id, home)
    machine_home = resolve_machine_home(home, resolution)

    already = _already_dispatched(machine_home, task.id, values)
    if already is not None:
        return already

    actor_id = (actor or resolution.runner.actor_id).strip()
    # A project that configures no actors accepts any id -- the same allowance
    # `assert_runner_actor_known` and `validate_actor` make for a freshly initialised
    # project, and refusing here would make registration impossible on one.
    vocabulary = load_actors(dict(project_config))
    if vocabulary and actor_kind(dict(project_config), actor_id) is None:
        known = ", ".join(sorted(vocabulary))
        raise UnknownActorError(
            f"{actor_id!r} is not one of {project.id!r}'s configured actors ({known}), "
            "so the note this would write is one the task API refuses anyway. Pass "
            "--actor with an id from the project's `actors:`."
        )

    runner = DispatchRunner(
        manager=manager,
        resolution=resolution,
        project_root=project.root,
        home=machine_home,
    )

    claimed = (session_id or "").strip() or self_session_id(resolution.runner.driver, values)
    if not claimed:
        raise SessionUnnamedError(
            "This session could not name itself, and no --session was given. "
            f"The {resolution.runner.driver.value} driver reads the session id out of "
            "the environment its CLI publishes; a runner that publishes none has to be "
            "told. Find the id with "
            f"`{runner.display_command()} agents` and pass `--session <id>`."
        )

    row = _live_row(runner, claimed)
    kind = session_kind(row)

    running = live_runs(machine_home)
    for run in running:
        if run.task_id == task.id:
            raise RegistrationRunExistsError(
                f"{task.id} already has run {run.run_id} in state {run.status!r}, so it "
                "is already being followed. One live run per task, always. If that run "
                "is over and its record does not say so, cancel it and register again."
            )

    if kind != "background":
        # An attended session gets an *interactive* run (task-354): a record the poller
        # never parks, stops or settles, which is what made refusing these necessary
        # until then. Ordinarily the claim writes this record itself; registering is
        # for a session that claimed some other way, or before that existed.
        from agentjobs.dispatch.interactive import ORIGIN_REGISTERED, start_interactive_run
        from agentjobs.session_identity import SessionIdentity

        identity = SessionIdentity(
            session_id=str(row.get("sessionId") or claimed),
            cwd=str(row.get("cwd") or project.root),
            driver=resolution.runner.driver.value,
        )
        record = start_interactive_run(
            home=machine_home,
            project=project,
            task=task,
            identity=identity,
            actor=actor_id,
            origin=ORIGIN_REGISTERED,
        )
        if record is None:
            raise RegistrationRunExistsError(
                f"{task.id} could not be given an interactive run: it is not active, or "
                "something else holds it. Claim it first."
            )
        return Registration(
            task_id=task.id,
            run_id=record.run_id,
            session_id=identity.session_id,
            already_known=False,
            detail=(
                f"Session {claimed} is {kind}, so it is recorded as interactive run "
                f"{record.run_id}: visible on the dashboard, followed by nothing, and "
                "never stopped by the poller."
            ),
            directory=record.path,
            started_at=record.started_at,
        )

    # The machine's concurrency ceiling is deliberately NOT applied. It exists to stop a
    # click starting an agent the machine cannot afford; this session is already running,
    # and refusing it would not stop it -- it would only keep it invisible, which is the
    # entire defect. The registered run does occupy a slot from here on, because the
    # machine really is running that agent, and a later dispatch being refused on it is
    # the truth rather than a side effect.

    run_id = new_run_id()
    started = _row_started_at(row)
    try:
        lock = acquire_run_lock(machine_home, task.id, run_id=run_id, timeout=1.0)
    except RunLockTimeout as exc:
        raise RegistrationRunExistsError(str(exc)) from exc

    try:
        updated = manager.add_log_entry(
            task.id,
            actor=actor_id,
            type=LogEntryType.NOTE,
            body=_note_body(run_id=run_id, session_id=claimed, runner=runner),
            data={
                "registration": {
                    "run_id": run_id,
                    "session_id": claimed,
                    "runner": resolution.runner.name,
                    "driver": resolution.runner.driver.value,
                    "mode": DispatchMode.SESSION.value,
                    "cwd": str(project.root),
                    "git_head": git_head(project.root),
                    "session_started_at": started.isoformat() if started else None,
                }
            },
        )
        entry_id = updated.log[-1].id
        directory = RunDirectory.create(
            machine_home,
            run_id,
            {
                "run_id": run_id,
                "task_id": task.id,
                "project_id": project.id,
                "mode": DispatchMode.SESSION.value,
                "driver": resolution.runner.driver.value,
                # No posture. AgentJobs did not choose this session's permission
                # envelope and has no way to read it, and writing the project default
                # here would be a claim about what the session may do that nothing
                # supports. `origin` is what a reader should key on instead.
                "origin": "registered",
                "status": "running",
                "started_at": (started or datetime.now(timezone.utc)).isoformat(),
                "session_id": claimed,
                "dispatch_entry_id": entry_id,
                "argv": [],
            },
        )
    except BaseException:
        # Nothing is following this run, so nothing will release the lock later.
        lock.release()
        raise

    return Registration(
        task_id=task.id,
        run_id=run_id,
        session_id=claimed,
        already_known=False,
        detail=(
            f"Session {claimed} adopted as run {run_id}. It is now polled like a " "dispatched run."
        ),
        directory=directory.path,
        entry_id=entry_id,
        started_at=started,
    )


def _already_dispatched(
    home: Path, task_id: str, values: Mapping[str, str]
) -> Optional[Registration]:
    """The no-op result when this process already *is* a run, or ``None``.

    Three answers, not two. A live run for this task is the ordinary case and returns a
    result saying nothing was needed. A live run for a *different* task is a mistake
    worth naming rather than quietly adopting, because it means a dispatched agent is
    registering against a task it was not sent to work. Anything else -- no variable, an
    unknown run, a run already concluded -- falls through and registers, because in none
    of those is the session currently covered.
    """
    run_id = (values.get(RUN_ID_ENV) or "").strip()
    if not run_id:
        return None
    for run in live_runs(home):
        if run.run_id != run_id:
            continue
        if run.task_id == task_id:
            return Registration(
                task_id=task_id,
                run_id=run_id,
                session_id="",
                already_known=True,
                detail=(
                    f"Already known: AgentJobs dispatched this session as run {run_id}, "
                    "so it has a run record and is polled. Nothing to do."
                ),
            )
        raise DispatchedElsewhereError(
            f"This process is run {run_id}, which AgentJobs dispatched to work "
            f"{run.task_id!r}, not {task_id!r}. Registering it here would point one "
            "session's run record at two tasks. Work the task you were dispatched to, "
            "or hand that one back first."
        )
    return None


def _live_row(runner: DispatchRunner, session_id: str) -> Dict[str, object]:
    """The claimed session's live ledger row, or a refusal that says which way it failed.

    "Wrong id" and "already dead" are different mistakes with different fixes, so the
    failure path spends one extra ledger read to tell them apart. The listing is scoped
    to the project root, which is also the check that the session is *this* project's --
    and is why a session launched from inside a worktree cannot register: the poller
    looks it up the same way and would report it gone at the next tick.
    """
    try:
        rows = runner.ledger()
    except DispatchRunError as exc:
        raise SessionUnknownError(
            f"Could not read {runner.display_command()}'s session ledger, so the claim "
            f"that session {session_id} exists could not be checked, and a run record "
            f"naming a session nothing can find is worse than none: {exc}"
        ) from exc
    for row in rows:
        if row.get("id") != session_id and row.get("sessionId") != session_id:
            continue
        # The active listing is *supposed* to omit a session that is over, and on Claude
        # Code 2.1.247 it does. Asked again anyway, through the same classifier the
        # poller uses, because "the row is here" and "the session is alive" are two
        # claims and only the second one is the one being relied on.
        phase = classify_session(
            str(row.get("status")) if row.get("status") is not None else None,
            str(row.get("state")) if row.get("state") is not None else None,
        )
        if phase is SessionPhase.STOPPED:
            raise SessionUnknownError(_over_message(runner, session_id, "stopped"))
        return row

    try:
        finished = [
            row
            for row in runner.ledger(include_finished=True)
            if row.get("id") == session_id or row.get("sessionId") == session_id
        ]
    except DispatchRunError:  # pragma: no cover - the first read already worked
        finished = []
    if finished:
        state = finished[0].get("state") or finished[0].get("status") or "over"
        raise SessionUnknownError(_over_message(runner, session_id, str(state)))
    live = ", ".join(str(row.get("id") or row.get("sessionId")) for row in rows[:6]) or "none"
    raise SessionUnknownError(
        f"No live session {session_id} under {runner.project_root}. Live there now: "
        f"{live}. A session launched from somewhere else -- a worktree, most likely -- "
        "is not listed here, and the poller would look it up exactly this way and "
        "report it gone. Relaunch it from the project root, or dispatch it."
    )


def _over_message(runner: DispatchRunner, session_id: str, state: str) -> str:
    """Refusal for a session that exists but is over -- a different fix from a wrong id."""
    return (
        f"Session {session_id} is in {runner.display_command()}'s ledger but is "
        f"{state!r}, not live. There is nothing left to follow, so nothing was written."
    )


def _row_started_at(row: Mapping[str, object]) -> Optional[datetime]:
    """When the session itself started, from the ledger's own epoch-milliseconds field.

    Preferred over the clock at registration because two things measure from it: the
    ``session_stale_seconds`` window in ``_settle_finished_session``, and the duration
    written to the task when the run concludes. A session adopted an hour into its work
    should report an hour, not a minute.
    """
    raw = row.get("startedAt")
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    try:
        return datetime.fromtimestamp(float(raw) / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):  # pragma: no cover - absurd clock value
        return None


def _note_body(*, run_id: str, session_id: str, runner: DispatchRunner) -> str:
    """What a reader of the task record sees, written for one with no other context."""
    return (
        f"Session `{session_id}` registered itself as working this task, and was adopted "
        f"as run `{run_id}`.\n\n"
        "**AgentJobs did not start this session** -- somebody spawned it by hand -- so "
        "nothing had a record of it and none of the dispatch protections applied to it. "
        "They do now: from the next poll it is watched for a permission prompt it is "
        "parked on, for an expired login, for finishing without handing off, and for "
        "claiming to work while emitting nothing. Whichever of those happens is written "
        "here, on this record.\n\n"
        f"It was **not** started by AgentJobs and will not be restarted by it. Attach "
        f"with `{runner.display_command()} attach {session_id}`."
    )


def registration_lines(result: Registration) -> List[str]:
    """The result as a human reads it, shared by every caller that prints one."""
    lines = [result.detail]
    if result.already_known:
        return lines
    if result.directory is not None:
        lines.append(f"   Run directory: {result.directory}")
    lines.append(
        "   Not a dispatch: nobody authorised a run and none was started, so the task "
        "record carries a note rather than a dispatch entry."
    )
    return lines
