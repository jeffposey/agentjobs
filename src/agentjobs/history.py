"""Finish and gate history: the rows, derived once from the same records the files hold.

Task-472, analytics design section 20. A scripted finish writes ``meta.yaml`` and
``phases.jsonl`` under the home directory's ``finishes/``; the gate appends its phases to
whichever run it is inside; neither is queryable. The store now carries ``finish``,
``finish_step``, ``gate_run`` and ``gate_stage`` as an index over those files, and this
module is where a file's content becomes a row.

**One derivation, two moments.** :func:`finish_record` turns a ``meta.yaml`` mapping into
a ``finish`` row and :class:`GateAssembler` turns the gate's phase events into a
``gate_run`` row and its stages. The finisher and the gate feed them as they run, and
the one-time import feeds them from the files afterwards. So the importer is not a second
reading of the format that can drift from the first: a finish recorded live and the same
finish imported from disk produce the same rows, and one set of tests covers both.

**Writing never breaks the thing being measured.** :class:`FinishHistory` and
:class:`GateHistory` swallow every failure the way ``dispatch.phases.record_phase`` does.
A finish that could not index itself is a gap in a chart; a finish that stopped because
it could not is a merge nobody asked to lose. The gate goes one step further and turns
its client's patience off: a quarter of a minute waiting for a service that is not
running would be the gate slowed by its own instrumentation, which is the one thing the
task's constraints forbid.

**Neither writer opens the database.** Both hold a manager from
``store_factory.task_manager_for`` -- inside the server that is the store, anywhere else
it is the service (task-273) -- and write through ``record_finish`` and
``record_gate_run``. A gate in a worktree therefore lands in the served project's store
without ever composing a path to it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from .dispatch.phases import RUN_ID_ENV, current_run, phases_path, read_phases
from .projects import Project, ProjectRegistry
from .sqlstore.history import IMPORTED, NATIVE, HistoryWrite

FINISHES_DIRNAME = "finishes"
"""Mirrors ``dispatch.finish.FINISHES_DIRNAME``; a test holds the two together. Named
here as well because this module must not import the finisher -- the finisher imports
this."""

FINISH_PREFIX = "fin_"

GATE_HISTORY_ENV = "AGENTJOBS_GATE_HISTORY"
"""Set to ``off`` and the gate indexes nothing. The gate sets it on every child it
starts, because ``pytest`` runs this repository's own tests of ``check.main`` and each
simulated gate would otherwise write a real row; ``conftest`` sets it too."""

STALE_RUNNING = timedelta(hours=24)
"""A finish still ``running`` this long after it started is a finish whose process died.
The import records it as ``interrupted``; a younger one is the native writer's."""

SCOPES: Dict[str, str] = {
    "full": "full",
    "partial": "partial",
    "necessity": "since_gate",
    "since_gate": "since_gate",
    "concurrent": "concurrent",
}
"""The gate's scope vocabulary as the store spells it. One rename: the gate calls a
reduced ``--since-gate`` run ``necessity``."""

GATE_KINDS = frozenset(
    {"gate_started", "gate_stage_started", "gate_stage_finished", "gate_finished"}
)

_PREFLIGHT = re.compile(r"^(?P<branch>\S+) at [0-9a-f]+ in (?P<checkout>.+)$")
_TASK_BRANCH = re.compile(r"^[a-z]+/(task-\d+)")


def finishes_root(home: Path) -> Path:
    """Where finish records live under a home directory."""
    return Path(home) / FINISHES_DIRNAME


# ----- timestamps ---------------------------------------------------------------------


def stamp(value: Any) -> Optional[str]:
    """A timestamp in the store's form: ISO-8601, UTC, ``Z`` suffix. ``None`` stays."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def moment_of(value: Any) -> Optional[datetime]:
    """The datetime a stored or recorded timestamp names, or ``None``."""
    text = stamp(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def now_stamp() -> str:
    return stamp(datetime.now(timezone.utc)) or ""


# ----- the finish row ------------------------------------------------------------------

STILL_RUNNING = "still_running"
NO_TASK = "no_task_id"
NO_START = "no_started_at"


def finish_record(
    meta: Mapping[str, Any], *, source: str = NATIVE, now: Optional[datetime] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The ``finish`` row a ``meta.yaml`` mapping describes, or why there is none.

    Returns ``(record, None)`` or ``(None, reason)``. The rules are section 20.4's:
    ``merged`` was added to the file later than the rest, so where it is absent it is
    derived as *finished with a merge commit*; a ``running`` record with no ``finished_at``
    is ``interrupted`` once it is older than :data:`STALE_RUNNING` and is left to its
    writer while younger. The native writer never meets the second rule -- its record is
    ``running`` precisely while it is running -- so it applies to imports only.
    """
    task_id = str(meta.get("task_id") or "").strip()
    if not task_id:
        return None, NO_TASK
    started_at = stamp(meta.get("started_at"))
    if started_at is None:
        return None, NO_START
    outcome = str(meta.get("outcome") or "running")
    finished_at = stamp(meta.get("finished_at"))
    if outcome == "running" and finished_at is None and source == IMPORTED:
        started = moment_of(started_at)
        current = now or datetime.now(timezone.utc)
        if started is None or current - started < STALE_RUNNING:
            return None, STILL_RUNNING
        outcome = "interrupted"
    merge_commit = meta.get("merge_commit") or None
    merged = meta.get("merged")
    if merged is None:
        merged = outcome == "finished" and merge_commit is not None
    seconds = meta.get("seconds")
    record: Dict[str, Any] = {
        "task_id": task_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "seconds": float(seconds) if seconds is not None else None,
        "outcome": outcome,
        "reason": meta.get("reason") or None,
        "stopped_at": meta.get("stopped_at") or None,
        "merged": bool(merged),
        "merge_commit": merge_commit,
        "run_id": meta.get("run_id") or None,
        "dispatched_run_id": meta.get("dispatched_run_id") or None,
        "authority": meta.get("authority") or None,
        "source": source,
    }
    return record, None


def step_row(seq: int, ts: Any, fields: Mapping[str, Any]) -> Dict[str, Any]:
    """One ``finish_step`` row from what ``StepLog.append`` records."""
    return {
        "seq": seq,
        "step": str(fields.get("step") or "unexpected"),
        "ok": bool(fields.get("ok")),
        "skipped": bool(fields.get("skipped")),
        "seconds": float(fields.get("seconds") or 0.0),
        "detail": fields.get("detail"),
        "ts": stamp(ts) or now_stamp(),
    }


def steps_from_phases(records: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Every ``finish_step`` line as a row, ``seq`` in file order."""
    steps: List[Dict[str, Any]] = []
    for record in records:
        if record.get("kind") != "finish_step":
            continue
        steps.append(step_row(len(steps) + 1, record.get("ts"), record))
    return steps


def preflight_checkout(
    records: Sequence[Mapping[str, Any]],
) -> Tuple[Optional[str], Optional[str]]:
    """``(branch, checkout)`` from the preflight step's detail, where it has the shape
    ``<branch> at <sha> in <path>``. The gate's own record does not carry them."""
    for record in records:
        if record.get("kind") == "finish_step" and record.get("step") == "preflight":
            match = _PREFLIGHT.match(str(record.get("detail") or ""))
            if match:
                return match.group("branch"), match.group("checkout")
            return None, None
    return None, None


# ----- the gate row -----------------------------------------------------------------------


def _mb(value: Any) -> Optional[int]:
    """A free-memory reading from a phase event, or ``None`` when it was not measured."""
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _low(value: Any) -> Optional[bool]:
    """``low_memory`` from a phase event: a stage's flag, or the gate's list of stages."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return bool(value)
    return bool(value)


class GateAssembler:
    """The ``gate_run`` row and its stages, built from the gate's phase events in order.

    Fed by :class:`GateHistory` as the gate runs and by :func:`gates_from_phases` from a
    finish's file afterwards, with the same four event kinds either way. ``feed``
    returns whether the event changed anything, which is what tells the live writer
    whether there is something new to send.
    """

    def __init__(
        self,
        gate_id: str,
        *,
        origin: str,
        finish_id: Optional[str] = None,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        checkout: Optional[str] = None,
        branch: Optional[str] = None,
        source: str = NATIVE,
    ) -> None:
        self.gate_id = gate_id
        self.record: Dict[str, Any] = {
            "origin": origin,
            "finish_id": finish_id,
            "run_id": run_id,
            "task_id": task_id,
            "scope": "full",
            "tree": None,
            "checkout": checkout,
            "branch": branch,
            "started_at": None,
            "finished_at": None,
            "seconds": None,
            "passed": None,
            "failed_stage": None,
            "stages_run": None,
            "stages_total": None,
            "source": source,
            "free_mb_start": None,
            "free_mb_low": None,
            "low_memory": None,
        }
        self.stages: List[Dict[str, Any]] = []
        self._stage_started: Dict[str, str] = {}
        self._stage_free: Dict[str, Optional[int]] = {}

    @property
    def started(self) -> bool:
        return self.record["started_at"] is not None

    @property
    def open(self) -> bool:
        return self.started and self.record["finished_at"] is None

    def feed(self, ts: Any, kind: str, fields: Mapping[str, Any]) -> bool:
        when = stamp(ts) or now_stamp()
        if kind == "gate_started":
            self.record["started_at"] = when
            self.record["scope"] = SCOPES.get(str(fields.get("scope") or "full"), "full")
            self.record["tree"] = fields.get("tree") or None
            total = fields.get("stages_total")
            self.record["stages_total"] = int(total) if total is not None else None
            self.record["free_mb_start"] = _mb(fields.get("free_mb"))
            return True
        if not self.started:
            return False
        stage = str(fields.get("stage") or "")
        if kind == "gate_stage_started":
            if stage:
                self._stage_started[stage] = when
                self._stage_free[stage] = _mb(fields.get("free_mb"))
            return False
        if kind == "gate_stage_finished":
            if not stage:
                return False
            seconds = fields.get("seconds")
            self.stages.append(
                {
                    "seq": len(self.stages) + 1,
                    "stage": stage,
                    "seconds": float(seconds) if seconds is not None else None,
                    "passed": True,
                    "started_at": self._stage_started.pop(stage, when),
                    "finished_at": when,
                    "free_mb_start": self._stage_free.pop(stage, None),
                    "free_mb_end": _mb(fields.get("free_mb")),
                    "free_mb_low": _mb(fields.get("free_mb_low")),
                    "low_memory": _low(fields.get("low_memory")),
                }
            )
            return True
        if kind == "gate_finished":
            self.record["finished_at"] = when
            seconds = fields.get("seconds")
            self.record["seconds"] = float(seconds) if seconds is not None else None
            passed = fields.get("passed")
            self.record["passed"] = bool(passed) if passed is not None else None
            run = fields.get("stages_run")
            self.record["stages_run"] = int(run) if run is not None else len(self.stages)
            total = fields.get("stages_total")
            if total is not None:
                self.record["stages_total"] = int(total)
            failed = fields.get("failed_stage") or None
            self.record["failed_stage"] = failed
            self.record["free_mb_low"] = _mb(fields.get("free_mb_low"))
            self.record["low_memory"] = _low(fields.get("low_memory"))
            if failed:
                # The stage that failed has no `gate_stage_finished` of its own. Its
                # seconds stay null (section 20.4); its start is known when the gate
                # recorded one.
                self.stages.append(
                    {
                        "seq": len(self.stages) + 1,
                        "stage": str(failed),
                        "seconds": None,
                        "passed": False,
                        "started_at": self._stage_started.pop(str(failed), when),
                        "finished_at": when,
                        "free_mb_start": self._stage_free.pop(str(failed), None),
                    }
                )
            return True
        return False


def gates_from_phases(
    records: Sequence[Mapping[str, Any]],
    *,
    finish_id: str,
    task_id: Optional[str],
    source: str = IMPORTED,
    checkout: Optional[str] = None,
    branch: Optional[str] = None,
) -> List[GateAssembler]:
    """Every gate a finish's phase file records, ``<finish_id>:g<n>`` in file order.

    A ``gate_started`` with no ``gate_finished`` stays open, ``finished_at`` null; that
    is a gate killed mid-run, and 9 of the 214 on this machine are one (section 20.4).
    Lines of other kinds are the finisher's own and are passed over.
    """
    gates: List[GateAssembler] = []
    current: Optional[GateAssembler] = None
    for record in records:
        kind = str(record.get("kind") or "")
        if kind == "gate_started":
            current = GateAssembler(
                f"{finish_id}:g{len(gates) + 1}",
                origin="finish",
                finish_id=finish_id,
                task_id=task_id,
                checkout=checkout,
                branch=branch,
                source=source,
            )
            gates.append(current)
        elif kind not in GATE_KINDS or current is None:
            continue
        current.feed(record.get("ts"), kind, record)
    return gates


# ----- the live writers ---------------------------------------------------------------------


class FinishHistory:
    """The finisher's index of itself, kept current as ``meta.yaml`` and the steps change.

    Attached to a ``FinishDirectory``: every ``write_meta`` calls :meth:`meta_written` with
    the merged mapping and every ``finish_step`` phase calls :meth:`step_recorded`. Each
    sends the whole record, so what the store holds is always the latest file. Failures
    are counted and swallowed; ``failures`` is for the finish's own report and tests.
    """

    def __init__(self, manager: Any, finish_id: str) -> None:
        self.manager = manager
        self.finish_id = finish_id
        self.record: Optional[Dict[str, Any]] = None
        self.steps: List[Dict[str, Any]] = []
        self.writes = 0
        self.failures = 0
        self.last: Optional[HistoryWrite] = None

    def meta_written(self, meta: Mapping[str, Any]) -> None:
        record, _ = finish_record(meta, source=NATIVE)
        if record is None:
            return
        self.record = record
        self._put()

    def step_recorded(self, ts: Any, **fields: Any) -> None:
        self.steps.append(step_row(len(self.steps) + 1, ts, fields))
        self._put()

    def _put(self) -> None:
        if self.record is None:
            return
        try:
            self.last = self.manager.record_finish(self.finish_id, self.record, self.steps)
            self.writes += 1
        except Exception:  # noqa: BLE001 - a record must never fail a finish
            self.failures += 1


class GateHistory:
    """The gate's index of itself: one row when it starts, updated per stage and at the end.

    Built by :meth:`open` from the checkout the gate runs in, which decides the project
    (the registered project this checkout is a worktree of) and the origin (the finish or
    run whose environment this process inherited, else ``manual``). ``None`` from ``open``
    means the gate indexes nothing this run, and says why in ``GateHistory.declined``.

    The cost is measured, not assumed: ``seconds`` is the wall time spent in writes, and
    ``summary`` is what the gate prints under its timing table (acceptance ac-5).
    """

    declined: Optional[str] = None
    """Why the last :meth:`open` returned ``None``, for the gate to print."""

    def __init__(
        self,
        manager: Any,
        project_id: str,
        *,
        origin: str,
        finish_id: Optional[str],
        run_id: Optional[str],
        task_id: Optional[str],
        checkout: str,
        branch: Optional[str],
        gate_id: Optional[str] = None,
    ) -> None:
        self.manager = manager
        self.project_id = project_id
        self.origin = origin
        self.finish_id = finish_id
        self.run_id = run_id
        self.task_id = task_id
        self.checkout = checkout
        self.branch = branch
        self._gate_id = gate_id
        self.assembler: Optional[GateAssembler] = None
        self.writes = 0
        self.failures = 0
        self.seconds = 0.0
        self.enabled = True

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        environ: Optional[Mapping[str, str]] = None,
        registry: Optional[ProjectRegistry] = None,
        timeout: float = 5.0,
    ) -> Optional["GateHistory"]:
        """A writer for the gate in ``root``, or ``None`` with :attr:`declined` set.

        Declines when the switch is off, when the checkout belongs to no registered
        project, or when the manager cannot be built. It does not probe the service:
        the first write finds out, once, without waiting.
        """
        env = environ if environ is not None else os.environ
        cls.declined = None
        if env.get(GATE_HISTORY_ENV, "").strip().lower() in {"off", "0", "false", "no"}:
            cls.declined = f"{GATE_HISTORY_ENV} is off"
            return None
        project = project_for_checkout(root, registry=registry)
        if project is None:
            cls.declined = "this checkout is not a registered project's clone or worktree"
            return None
        try:
            from .store_factory import task_manager_for

            manager = task_manager_for(project, client_timeout=timeout, patient=False)
        except Exception as exc:  # noqa: BLE001 - never fail the gate over this
            cls.declined = f"no manager for {project.id}: {exc}"
            return None
        origin, finish_id, run_id, task_id = gate_origin(env)
        branch = checkout_branch(root)
        if task_id is None and branch:
            task_id = task_from_branch(branch)
        return cls(
            manager,
            project.id,
            origin=origin,
            finish_id=finish_id,
            run_id=run_id,
            task_id=task_id,
            checkout=str(Path(root).resolve()),
            branch=branch,
        )

    @property
    def gate_id(self) -> str:
        """``<finish_id>:g<n>``, ``<run_id>:g<n>`` or ``man_<uuid>``, fixed at the first
        event: ``n`` counts the ``gate_started`` lines the run's phase file holds by then,
        this gate's included, which is how the import numbers the same gate."""
        if self._gate_id is None:
            owner = self.finish_id or self.run_id
            if owner is None:
                self._gate_id = f"man_{uuid.uuid4().hex}"
            else:
                self._gate_id = f"{owner}:g{max(1, gates_started_so_far())}"
        return self._gate_id

    def observe(self, kind: str, **fields: Any) -> None:
        """Take the same event ``record_phase`` just wrote, and send the row if it changed."""
        if not self.enabled or kind not in GATE_KINDS:
            return
        if self.assembler is None:
            if kind != "gate_started":
                return
            self.assembler = GateAssembler(
                self.gate_id,
                origin=self.origin,
                finish_id=self.finish_id,
                run_id=self.run_id,
                task_id=self.task_id,
                checkout=self.checkout,
                branch=self.branch,
                source=NATIVE,
            )
        if self.assembler.feed(now_stamp(), kind, fields):
            self._put()

    def _put(self) -> None:
        if self.assembler is None:
            return
        began = time.perf_counter()
        try:
            self.manager.record_gate_run(
                self.assembler.gate_id, self.assembler.record, self.assembler.stages
            )
            self.writes += 1
        except Exception:  # noqa: BLE001 - never fail the gate over this
            self.failures += 1
            if self.failures >= 2:
                # Two refusals is a service that is not there; stop asking.
                self.enabled = False
        finally:
            self.seconds += time.perf_counter() - began

    def close(self) -> None:
        client = getattr(self.manager, "client", None)
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    def summary(self) -> str:
        """One line for the gate's timing table: what the index cost this run."""
        where = f"{self.gate_id} in {self.project_id}"
        if self.writes == 0 and self.failures:
            return f"  history   {self.seconds:6.1f}s  not recorded: the service did not answer ({where})"
        note = f", {self.failures} failed" if self.failures else ""
        return f"  history   {self.seconds:6.1f}s  {self.writes} writes{note} ({where})"


# ----- who and where the gate is ----------------------------------------------------------


def gate_origin(
    environ: Mapping[str, str],
) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """``(origin, finish_id, run_id, task_id)`` from the environment a gate inherited.

    The finisher runs the gate with ``AGENTJOBS_RUN_ID`` set to its finish id and the run
    directory set to its own; a dispatched run's session carries its run id. Either is
    trusted only while ``phases.current_run`` says the directory is live, which is the
    stale-identity guard of task-249. ``task_id`` comes from that directory's
    ``meta.yaml``. Anything else is ``manual``.
    """
    run_id = (environ.get(RUN_ID_ENV) or "").strip()
    directory = current_run() if run_id else None
    if not run_id or directory is None:
        return "manual", None, None, None
    task_id: Optional[str] = None
    meta_path = directory / "meta.yaml"
    try:
        loaded = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and loaded.get("task_id"):
            task_id = str(loaded["task_id"])
    except (OSError, yaml.YAMLError):
        task_id = None
    if run_id.startswith(FINISH_PREFIX):
        return "finish", run_id, None, task_id
    return "run", None, run_id, task_id


def gates_started_so_far() -> int:
    """How many ``gate_started`` lines the current run's phase file holds."""
    directory = current_run()
    if directory is None:
        return 0
    return sum(1 for record in read_phases(directory) if record.get("kind") == "gate_started")


def task_from_branch(branch: Optional[str]) -> Optional[str]:
    """``task-NNN`` from a ``<type>/task-NNN-...`` branch name, else ``None``."""
    if not branch:
        return None
    match = _TASK_BRANCH.match(branch.strip())
    return match.group(1) if match else None


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def checkout_branch(root: Path) -> Optional[str]:
    """The branch checked out in ``root``, or ``None`` when git cannot say."""
    answer = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return None if answer in (None, "HEAD") else answer


def clone_root(root: Path) -> Optional[Path]:
    """The main clone a checkout belongs to: itself, or the clone its worktree hangs off.

    ``git rev-parse --git-common-dir`` answers the shared ``.git`` directory, which for a
    worktree is the clone's. A worktree beside the clone in ``worktrees/`` is not inside
    any registered root, so containment cannot place it; this can.
    """
    common = _git(root, "rev-parse", "--git-common-dir")
    if common is None:
        return None
    path = Path(common)
    if not path.is_absolute():
        path = Path(root) / path
    try:
        return path.resolve().parent
    except OSError:
        return None


def project_for_checkout(
    root: Path, *, registry: Optional[ProjectRegistry] = None
) -> Optional[Project]:
    """The registered project whose clone ``root`` is or is a worktree of, else ``None``.

    Never a project composed from the checkout's own configuration: ``config.yaml`` names
    no project id, and a checkout nobody registered is not served by anything.
    """
    reg = registry if registry is not None else ProjectRegistry()
    try:
        projects = reg.list_projects()
    except Exception:  # noqa: BLE001 - an unreadable registry is "no project"
        return None
    if not projects:
        return None
    clone = clone_root(Path(root))
    if clone is not None:
        for project in projects:
            try:
                if Path(project.root).resolve() == clone:
                    return project
            except OSError:
                continue
    try:
        return reg.resolve_default(Path(root))
    except Exception:  # noqa: BLE001 - not inside any registered project
        return None


# ----- the one-time import ------------------------------------------------------------------

NO_META = "no meta.yaml"
OTHER_PROJECT = "another project's finish"


@dataclass
class FinishImportReport:
    """What :func:`import_finishes` did, in enough detail to say what it skipped and why."""

    project_id: str
    scanned: int = 0
    finishes: int = 0
    steps: int = 0
    gates: int = 0
    stages: int = 0
    torn_lines: int = 0
    skipped: List[Tuple[str, str]] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"Scanned {self.scanned} finish directories for {self.project_id}.",
            f"  finishes  {self.finishes}",
            f"  steps     {self.steps}",
            f"  gates     {self.gates}",
            f"  stages    {self.stages}",
        ]
        if self.torn_lines:
            lines.append(f"  torn phase lines passed over: {self.torn_lines}")
        if self.skipped:
            by_reason: Dict[str, List[str]] = {}
            for finish_id, reason in self.skipped:
                by_reason.setdefault(reason, []).append(finish_id)
            lines.append(f"Skipped {len(self.skipped)}:")
            for reason, ids in sorted(by_reason.items()):
                shown = ", ".join(ids[:6]) + (f", +{len(ids) - 6} more" if len(ids) > 6 else "")
                lines.append(f"  {reason} ({len(ids)}): {shown}")
        return "\n".join(lines)


def read_phases_counting(directory: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Like ``phases.read_phases``, and also how many lines would not parse."""
    path = phases_path(directory)
    if not path.is_file():
        return [], 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0
    records: List[Dict[str, Any]] = []
    torn = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            loaded = json.loads(line)
        except ValueError:
            torn += 1
            continue
        if isinstance(loaded, dict):
            records.append(loaded)
        else:
            torn += 1
    return records, torn


def import_finishes(
    home: Path,
    manager: Any,
    project_id: str,
    *,
    now: Optional[datetime] = None,
) -> FinishImportReport:
    """Index every finish directory under ``home`` for ``project_id``. Re-runnable.

    Idempotent on finish id: a finish already in the store is reported as ``exists`` and
    nothing of it is touched, gates included. A finish whose task the project does not
    have is refused by the store and reported as ``unknown_task``. Section 20.4 has the
    rest of the rules; :func:`finish_record` and :func:`gates_from_phases` apply them.
    """
    report = FinishImportReport(project_id=project_id)
    root = finishes_root(home)
    if not root.is_dir():
        return report
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        finish_id = directory.name
        if not finish_id.startswith(FINISH_PREFIX):
            continue
        report.scanned += 1
        meta_path = directory / "meta.yaml"
        if not meta_path.is_file():
            report.skipped.append((finish_id, NO_META))
            continue
        try:
            loaded = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            report.skipped.append((finish_id, f"unreadable meta.yaml: {exc}"))
            continue
        meta: Dict[str, Any] = loaded if isinstance(loaded, dict) else {}
        if meta.get("project_id") and str(meta["project_id"]) != project_id:
            report.skipped.append((finish_id, OTHER_PROJECT))
            continue
        record, why = finish_record(meta, source=IMPORTED, now=now)
        if record is None:
            report.skipped.append((finish_id, why or "unreadable"))
            continue
        records, torn = read_phases_counting(directory)
        report.torn_lines += torn
        steps = steps_from_phases(records)
        outcome = manager.record_finish(finish_id, record, steps)
        if not outcome.written:
            report.skipped.append((finish_id, outcome.reason or "refused"))
            continue
        report.finishes += 1
        report.steps += len(steps)
        branch, checkout = preflight_checkout(records)
        for gate in gates_from_phases(
            records,
            finish_id=finish_id,
            task_id=record["task_id"],
            source=IMPORTED,
            checkout=checkout,
            branch=branch,
        ):
            written = manager.record_gate_run(gate.gate_id, gate.record, gate.stages)
            if written.written:
                report.gates += 1
                report.stages += len(gate.stages)
    return report


__all__ = [
    "FINISHES_DIRNAME",
    "FINISH_PREFIX",
    "GATE_HISTORY_ENV",
    "GATE_KINDS",
    "SCOPES",
    "STALE_RUNNING",
    "FinishHistory",
    "FinishImportReport",
    "GateAssembler",
    "GateHistory",
    "checkout_branch",
    "clone_root",
    "finish_record",
    "finishes_root",
    "gate_origin",
    "gates_from_phases",
    "import_finishes",
    "moment_of",
    "preflight_checkout",
    "project_for_checkout",
    "read_phases_counting",
    "stamp",
    "step_row",
    "steps_from_phases",
    "task_from_branch",
]
