"""Failure classes over real runs: what failed, what the machine did, what it cost a person.

Design section 9a gives every failure a class and a retry disposition. The execution journal
already records each of them as it happens; this module reads them back for a time window
and adds them up, so recovery can be turned into prevention -- a class that keeps costing
human actions is the next thing to fix (task-419, a7; the recurring review is task-440).

**Read-only, and machine-local.** Three sources, none of them a task store:

- the execution journal (``execution.db``): attempt conclusions and their failure classes,
  launch and delivery results the world could not settle, escalations to a person, policy
  waits, Stops, and the auth, usage-limit and spend-limit incidents with their waiters;
- scripted-finish directories: the gate verdict a finish recorded, which names a
  ``flaky_test`` or a proven input change and the tests behind it;
- phase records in run and finish directories: ``gate_stage_browser_gone``, the one gate
  retry that is infrastructure rather than a test outcome (task-404).

**One occurrence per failure, and a human action is counted once.** An escalation hands the
ball to a person; that action belongs to the class the escalation names. A ``worker_gone``
attempt followed by ``attempts_exhausted`` is one retried or stopped ``worker_gone`` plus one
``attempts_exhausted`` costing the action, never two actions. A Stop is a person's own
decision and costs no *required* action.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from agentjobs.dispatch import clock as dispatch_clock
from agentjobs.execution import reducer
from agentjobs.execution.store import ExecutionStore

RETRIED = "retried"
WAITED = "waited"
STOPPED = "stopped"
DISPOSITIONS = (RETRIED, WAITED, STOPPED)

FLAKY_TEST = "flaky_test"
GATE_INPUTS_CHANGED = "gate_inputs_changed"
BROWSER_DEATH = "browser_death"
AUTH_UNAVAILABLE = reducer.AUTH_UNAVAILABLE
USAGE_EXHAUSTED = "usage_exhausted"
SPEND_LIMIT = "spend_limit"
CANCELLED_BY_USER = reducer.CANCELLED_BY_USER

TEST_CLASSES = frozenset({FLAKY_TEST, BROWSER_DEATH})
"""Classes whose occurrences name test ids, and so can repeat by test."""

INCIDENT_CLASS = {
    "auth": AUTH_UNAVAILABLE,
    "usage_limit": USAGE_EXHAUSTED,
    "spend_limit": SPEND_LIMIT,
}

_OUTCOME_CLASS = {
    "interrupted": reducer.WORKER_GONE,
    "crashed": reducer.WORKER_GONE,
    "failed": reducer.WORKER_FAILED,
    "timeout": reducer.TIMED_OUT,
    "finished_without_handoff": reducer.SPEC_GAP,
}
"""``dispatch.journal.FAILURE_CLASS_BY_OUTCOME``, for an attempt the controller did not drive:
its conclusion records the outcome but no class. Duplicated rather than imported because the
journal adapter imports the runner, and a report has no business loading either."""


@dataclass(frozen=True)
class Occurrence:
    klass: str
    disposition: str
    human_actions: int
    at: datetime
    project_id: str
    task_id: str
    run_id: str
    detail: str = ""
    tests: Tuple[str, ...] = ()

    def as_data(self) -> Dict[str, Any]:
        return {
            "class": self.klass,
            "disposition": self.disposition,
            "human_actions": self.human_actions,
            "at": self.at.isoformat(),
            "project_id": self.project_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "detail": self.detail,
            "tests": list(self.tests),
        }


@dataclass
class ClassRollup:
    klass: str
    occurrences: List[Occurrence] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.occurrences)

    @property
    def dispositions(self) -> Dict[str, int]:
        counted = {name: 0 for name in DISPOSITIONS}
        for item in self.occurrences:
            counted[item.disposition] = counted.get(item.disposition, 0) + 1
        return counted

    @property
    def human_actions(self) -> int:
        return sum(item.human_actions for item in self.occurrences)

    @property
    def tests(self) -> Dict[str, int]:
        counted: Dict[str, int] = {}
        for item in self.occurrences:
            for test in item.tests:
                counted[test] = counted.get(test, 0) + 1
        return counted

    @property
    def repeated(self) -> List[str]:
        """Test ids that failed this way more than once in the window."""
        return sorted(test for test, seen in self.tests.items() if seen > 1)

    def as_data(self) -> Dict[str, Any]:
        return {
            "class": self.klass,
            "count": self.count,
            "dispositions": self.dispositions,
            "human_actions": self.human_actions,
            "runs": sorted({item.run_id for item in self.occurrences if item.run_id}),
            "tasks": sorted(
                {f"{item.project_id}/{item.task_id}" for item in self.occurrences if item.task_id}
            ),
            "tests": self.tests,
            "repeated_tests": self.repeated,
            "occurrences": [item.as_data() for item in self.occurrences],
        }


@dataclass(frozen=True)
class Rollup:
    since: Optional[datetime]
    until: Optional[datetime]
    classes: Tuple[ClassRollup, ...]
    unreadable: Tuple[str, ...] = ()

    def by_class(self) -> Dict[str, ClassRollup]:
        return {item.klass: item for item in self.classes}

    @property
    def human_actions(self) -> int:
        return sum(item.human_actions for item in self.classes)

    def as_data(self) -> Dict[str, Any]:
        return {
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
            "human_actions": self.human_actions,
            "classes": [item.as_data() for item in self.classes],
            "unreadable": list(self.unreadable),
        }

    def render(self) -> str:
        window = (
            f"{self.since.isoformat() if self.since else 'the beginning'} to "
            f"{self.until.isoformat() if self.until else 'now'}"
        )
        lines = [f"Failure classes, {window}"]
        if not self.classes:
            lines.append("  No failures recorded in this window.")
        for item in self.classes:
            counts = item.dispositions
            lines.append(
                f"  {item.klass}: {item.count} (retried {counts[RETRIED]}, waited "
                f"{counts[WAITED]}, stopped {counts[STOPPED]}); human actions "
                f"{item.human_actions}"
            )
            for occurrence in item.occurrences:
                tests = f" tests {', '.join(occurrence.tests)}" if occurrence.tests else ""
                lines.append(
                    f"    {occurrence.at.isoformat()} {occurrence.project_id}/"
                    f"{occurrence.task_id} {occurrence.run_id or '-'} "
                    f"{occurrence.disposition}{tests}"
                )
            for test in item.repeated:
                lines.append(f"    REPEATED: {test} ({item.tests[test]} times in this window)")
        lines.append(f"Human actions in total: {self.human_actions}")
        for problem in self.unreadable:
            lines.append(f"Unreadable, and not counted: {problem}")
        return "\n".join(lines)


# ----- reading ---------------------------------------------------------------------


def _moment(raw: object) -> Optional[datetime]:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _loads(raw: object) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _journal_occurrences(store: ExecutionStore) -> List[Occurrence]:
    found: List[Occurrence] = []
    executions = {
        row["execution_id"]: row
        for row in store.read("SELECT execution_id, project_id, task_id FROM execution")
    }
    events: Dict[str, List[Any]] = {}
    for row in store.read(
        "SELECT execution_id, sequence, kind, payload_json, observed_at FROM execution_event "
        "ORDER BY execution_id, sequence"
    ):
        events.setdefault(row["execution_id"], []).append(row)
    activities: Dict[str, List[Any]] = {}
    for row in store.read(
        "SELECT activity_id, execution_id, run_id, kind, state, result_json, error_class, "
        "updated_at, shadow FROM activity WHERE shadow = 0 ORDER BY updated_at"
    ):
        activities.setdefault(row["execution_id"], []).append(row)
    cancels = {
        row["run_id"]: _loads(row["cancel_json"])
        for row in store.read(
            "SELECT run_id, cancel_json FROM run_attempt WHERE cancel_requested = 1"
        )
    }
    # An attempt admitted with no execution -- a legacy or pre-envelope admission -- writes no
    # event when it concludes. Its own row is the record, and nothing retries it.
    for row in store.read(
        "SELECT run_id, project_id, task_id, outcome, concluded_at, cancel_requested "
        "FROM run_attempt WHERE execution_id IS NULL AND state = 'terminal'"
    ):
        at = _moment(row["concluded_at"]) or dispatch_clock.utcnow()
        if int(row["cancel_requested"]) and str(cancels.get(row["run_id"], {}).get("source")) != (
            "internal_transfer"
        ):
            klass, outcome = CANCELLED_BY_USER, "cancelled"
        else:
            outcome = str(row["outcome"] or "")
            klass = _OUTCOME_CLASS.get(outcome, "")
        if klass:
            found.append(
                Occurrence(
                    klass,
                    STOPPED,
                    0,
                    at=at,
                    project_id=str(row["project_id"]),
                    task_id=str(row["task_id"]),
                    run_id=str(row["run_id"]),
                    detail=f"{outcome}; admitted without an execution, so never retried",
                )
            )

    for execution_id, execution in executions.items():
        history = events.get(execution_id, [])
        acts = activities.get(execution_id, [])
        escalations = [a for a in acts if a["kind"] == "escalate" and a["state"] == "applied"]
        claimed: Set[str] = set()

        def escalation_after(sequence_time: Optional[datetime]) -> Optional[Any]:
            for activity in escalations:
                when = _moment(activity["updated_at"])
                if sequence_time is None or when is None or when >= sequence_time:
                    return activity
            return None

        def cost(activity: Any) -> int:
            return 1 if _loads(activity["result_json"]).get("handoff") else 0

        for index, event in enumerate(history):
            payload = _loads(event["payload_json"])
            at = _moment(event["observed_at"]) or dispatch_clock.utcnow()
            common = {
                "at": at,
                "project_id": execution["project_id"],
                "task_id": execution["task_id"],
            }
            if event["kind"] == "concluded":
                run_id = str(payload.get("run_id") or "")
                outcome = str(payload.get("outcome") or "")
                if outcome == "cancelled" or run_id in cancels:
                    cancel = cancels.get(run_id, {})
                    if str(cancel.get("source") or "") == "internal_transfer":
                        continue
                    found.append(
                        Occurrence(
                            CANCELLED_BY_USER,
                            STOPPED,
                            0,
                            run_id=run_id,
                            detail=f"stopped by {cancel.get('requester') or 'unrecorded'}",
                            **common,
                        )
                    )
                    continue
                klass = str(payload.get("failure_class") or _OUTCOME_CLASS.get(outcome) or "")
                if not klass:
                    continue
                later = [e["kind"] for e in history[index + 1 :]]
                escalated = escalation_after(at)
                if "admitted" in later:
                    disposition, actions = RETRIED, 0
                elif escalated is not None:
                    disposition = STOPPED
                    same = str(escalated["error_class"] or "") == klass
                    actions = cost(escalated) if same else 0
                    if same:
                        claimed.add(escalated["activity_id"])
                elif payload.get("retry_owed"):
                    disposition, actions = WAITED, 0
                else:
                    disposition, actions = STOPPED, 0
                found.append(
                    Occurrence(klass, disposition, actions, run_id=run_id, detail=outcome, **common)
                )
            elif event["kind"] == "policy_observed" and not payload.get("permitted"):
                klass = str(payload.get("class") or "")
                if klass in reducer.WAIT_CLASSES:
                    found.append(
                        Occurrence(
                            klass,
                            WAITED,
                            0,
                            run_id="",
                            detail=str(payload.get("detail") or ""),
                            **common,
                        )
                    )
        for activity in acts:
            if activity["state"] != "unknown" or activity["kind"] == "escalate":
                continue
            at = _moment(activity["updated_at"]) or dispatch_clock.utcnow()
            escalated = next(
                (a for a in escalations if a["error_class"] == reducer.EFFECT_UNKNOWN), None
            )
            if escalated is not None:
                claimed.add(escalated["activity_id"])
            found.append(
                Occurrence(
                    reducer.EFFECT_UNKNOWN,
                    STOPPED if escalated is not None else WAITED,
                    cost(escalated) if escalated is not None else 0,
                    at=at,
                    project_id=execution["project_id"],
                    task_id=execution["task_id"],
                    run_id=str(activity["run_id"] or ""),
                    detail=str(activity["kind"]),
                )
            )
        for activity in escalations:
            if activity["activity_id"] in claimed:
                continue
            found.append(
                Occurrence(
                    str(activity["error_class"] or "escalated"),
                    STOPPED,
                    cost(activity),
                    at=_moment(activity["updated_at"]) or dispatch_clock.utcnow(),
                    project_id=execution["project_id"],
                    task_id=execution["task_id"],
                    run_id=str(activity["run_id"] or ""),
                    detail="escalated",
                )
            )
    return found


def _incident_occurrences(store: ExecutionStore) -> List[Occurrence]:
    try:
        incidents = {
            row["incident_id"]: row
            for row in store.read("SELECT incident_id, kind, notified_at FROM auth_incident")
        }
        waiters = store.read(
            "SELECT incident_id, run_id, project_id, task_id, status, nudges, detail_json, "
            "joined_at FROM auth_waiter ORDER BY joined_at"
        )
    except Exception:  # noqa: BLE001 - a journal from before task-417 has no such tables
        return []
    found: List[Occurrence] = []
    for waiter in waiters:
        incident = incidents.get(waiter["incident_id"])
        if incident is None:
            continue
        klass = INCIDENT_CLASS.get(str(incident["kind"]), str(incident["kind"]))
        status = str(waiter["status"])
        detail = _loads(waiter["detail_json"])
        if status in ("escalated", "uncertain") or "escalated_entry" in detail:
            disposition = STOPPED
        elif status == "removed":
            disposition = STOPPED
        else:
            disposition = WAITED
        # A handoff written for this waiter is the one thing it asked of a person: the login
        # an auth notification names, the limit a spend park names, or an escalation. A
        # usage-limit park is handed to an external service, and asks nobody anything.
        if "escalated_entry" in detail:
            actions = 1
        elif klass in (AUTH_UNAVAILABLE, SPEND_LIMIT) and isinstance(
            detail.get("handoff_entry"), int
        ):
            actions = 1
        else:
            actions = 0
        found.append(
            Occurrence(
                klass,
                disposition,
                actions,
                at=_moment(waiter["joined_at"]) or dispatch_clock.utcnow(),
                project_id=str(waiter["project_id"]),
                task_id=str(waiter["task_id"]),
                run_id=str(waiter["run_id"]),
                detail=f"{status}, {int(waiter['nudges'])} resumes",
            )
        )
    return found


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml

        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable meta is reported by omission
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _read_phases(directory: Path) -> Iterable[Dict[str, Any]]:
    path = directory / "phases.jsonl"
    if not path.is_file():
        return []
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _test_id(line: object) -> str:
    """``tests/x.py::T::t`` out of the gate's ``FAILED tests/x.py::T::t - message`` line.

    A finish records whole summary lines; a repeat is a repeat of the test, whatever the
    assertion said the second time.
    """
    text = str(line).strip()
    for prefix in ("FAILED ", "ERROR "):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.split(" - ", 1)[0].strip()


def _directory_occurrences(home: Path) -> List[Occurrence]:
    found: List[Occurrence] = []
    for root, is_finish in ((home / "runs", False), (home / "finishes", True)):
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir()):
            if not directory.is_dir() or directory.name in ("receipts", "spawn"):
                continue
            phases = list(_read_phases(directory))
            if not phases:
                continue
            meta = _read_yaml(directory / "meta.yaml")
            project_id = str(meta.get("project_id") or "")
            task_id = str(meta.get("task_id") or "")
            run_id = str(meta.get("run_id") or meta.get("finish_id") or directory.name)
            for record in phases:
                at = _moment(record.get("ts")) or dispatch_clock.utcnow()
                kind = record.get("kind")
                if kind == "gate_stage_browser_gone":
                    passed = bool(record.get("retried")) and bool(record.get("passed"))
                    found.append(
                        Occurrence(
                            BROWSER_DEATH,
                            RETRIED if passed else STOPPED,
                            0,
                            at=at,
                            project_id=project_id,
                            task_id=task_id,
                            run_id=run_id,
                            detail=str(record.get("stage") or ""),
                            tests=tuple(str(t) for t in record.get("tests") or ()),
                        )
                    )
                elif is_finish and kind == "finish_gate_receipt":
                    classification = str(record.get("classification") or "")
                    if classification not in ("flaky_test", "inputs_changed"):
                        continue
                    attempts = record.get("attempts") or []
                    first = attempts[0] if attempts and isinstance(attempts[0], dict) else {}
                    found.append(
                        Occurrence(
                            FLAKY_TEST if classification == "flaky_test" else GATE_INPUTS_CHANGED,
                            RETRIED,
                            0,
                            at=at,
                            project_id=project_id,
                            task_id=task_id,
                            run_id=run_id,
                            detail=str(first.get("failed_stage") or ""),
                            tests=tuple(_test_id(t) for t in first.get("failing_tests") or ()),
                        )
                    )
    return found


def rollup(
    home: Path,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    store: Optional[ExecutionStore] = None,
) -> Rollup:
    """Every failure recorded on this machine between ``since`` and ``until``, by class."""
    unreadable: List[str] = []
    occurrences: List[Occurrence] = []
    try:
        if store is None:
            from agentjobs.execution.factory import execution_store_for

            store = execution_store_for(home)
        occurrences.extend(_journal_occurrences(store))
        occurrences.extend(_incident_occurrences(store))
    except Exception as exc:  # noqa: BLE001 - say what could not be read, report the rest
        unreadable.append(f"execution journal: {exc}")
    occurrences.extend(_directory_occurrences(home))
    selected = [
        item
        for item in occurrences
        if (since is None or item.at >= since) and (until is None or item.at <= until)
    ]
    grouped: Dict[str, ClassRollup] = {}
    for item in sorted(selected, key=lambda occurrence: occurrence.at):
        grouped.setdefault(item.klass, ClassRollup(item.klass)).occurrences.append(item)
    ordered = sorted(
        grouped.values(), key=lambda entry: (-entry.human_actions, -entry.count, entry.klass)
    )
    return Rollup(since, until, tuple(ordered), tuple(unreadable))


def counts(result: Rollup) -> Mapping[str, int]:
    """``{class: count}``, the shape a test compares against what it injected."""
    return {item.klass: item.count for item in result.classes}


__all__: Sequence[str] = [
    "BROWSER_DEATH",
    "ClassRollup",
    "FLAKY_TEST",
    "Occurrence",
    "Rollup",
    "counts",
    "rollup",
]
