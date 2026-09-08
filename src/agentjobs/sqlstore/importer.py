"""Load a directory of task YAML into the SQLite store, with its history.

Two jobs, and the second is the hard one.

Loading current state is mechanical. **Reconstructing history is not**, because the log
was never written to be replayed: a content update records *that* `priority` changed and
never what it changed from, and 79 of this repository's 367 tasks have no creation entry
at all. Measured on task-273: replaying the log alone leaves 337 tasks whose final state
disagrees with their own row.

So the import replays what it can, then **reconciles**: any task whose replay disagrees
with its imported row gets one `import` event carrying the true values, marked
`reconstructed` so the timestamp reads as an upper bound rather than an observation.
After that the invariant `SUM(open_delta) == COUNT(*) of open tasks` holds exactly, and
a chart built on the history cannot silently disagree with the board.

Nothing here is a cutover. Task-311 owns switching the authority; this is the code it
calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from ..models_v2 import Task
from ..quotation import QuotedRemark, scan_task
from .backfill import DEDUPE_WINDOW_SECONDS, Observation, first_seen, observe
from .connection import Database
from .store import EVENT_AXES, SqlTaskStore

#: Ball and reason the manager sets for a lifecycle it records without them.
#:
#: Entries of the shape ``{"lifecycle": "ready"}`` are common in the corpus: an older
#: manager recorded the axis it changed and not the ones that change with it. These are
#: the manager's own semantics, applied only where the entry is silent, and they lift
#: ball reconstruction from 124 disagreeing tasks to 35.
IMPLIED_BALL: Dict[str, Tuple[str, str]] = {
    "ready": ("agent", "available"),
    "active": ("agent", "work"),
}


class CorpusAlreadyImported(Exception):
    """This project already holds rows, and the import was not asked to replace them.

    The whole import is one transaction, so an interruption leaves nothing behind and a
    retry is a first run. A *completed* import re-run is the dangerous case: the tasks
    would upsert harmlessly and the reconstructed history would be written a second
    time, doubling every event and breaking the backlog invariant with no error.

    Refusing by default and offering ``replace=True`` is what makes a re-run safe to
    reach for. ``replace`` empties this project's rows inside the same transaction that
    re-fills them, so an import is idempotent by being atomic rather than by trying to
    merge two corpora.
    """


class QuotationPolicyError(Exception):
    """A record carries a verbatim quotation of a person, so nothing was imported.

    **Why this refuses rather than quarantines** (task-376). The importer's usual
    answer to a record it cannot accept is ``import_quarantine``: the live tables stay
    provably valid while the bad record stays inspectable and re-importable. That is
    the right shape for a record that will not *parse*, and the wrong one here, because
    the thing being excluded is the text itself and ``import_quarantine.raw_text``
    holds the whole file. Quarantining would put the quotation in the database in the
    same act that claimed to keep it out, which is precisely the durable, queryable
    copy the rule exists to prevent.

    So the scan runs over every readable document **before** the write transaction
    opens, and a hit raises this with nothing written. The cost is the one the task
    named: a false positive stops a cutover. Three things bound it. The same detector
    fails the gate over ``tasks/``, so a record reaching an import has already passed
    the check on its way into ``main``; ``agentjobs redact`` makes a real hit a
    one-command fix; and ``enforce_quotation_policy=False`` lets an operator who has
    read the hits and judged them proceed, with the report saying the policy was off.

    The message names regions and tone groups, never the quoted text -- an exception
    string ends up in logs and issue trackers, which is the same mistake one layer out.
    """

    def __init__(self, offenders: Dict[str, List[QuotedRemark]]) -> None:
        """Build the refusal from the regions that tripped, grouped by task id."""
        self.offenders = offenders
        lines = [
            f"{len(offenders)} task record(s) quote a person verbatim, so nothing was "
            "imported. A task record states what somebody meant, not the words they "
            "used (ALLAGENTS.md, 'Paraphrase a person, never quote them'). Fix each "
            "region with `agentjobs redact`, or re-run with "
            "enforce_quotation_policy=False if these are false positives:",
        ]
        for task_id in sorted(offenders):
            for remark in offenders[task_id]:
                lines.append(f"  {task_id}: {remark.locator()}")
        super().__init__("\n".join(lines))


@dataclass
class ImportReport:
    """What an import did, in enough detail to verify it without reading the store."""

    imported: int = 0
    quarantined: List[Tuple[str, str]] = field(default_factory=list)
    events: int = 0
    reconciled: List[str] = field(default_factory=list)
    blobs: int = 0
    backfilled: int = 0
    missing_blobs: List[str] = field(default_factory=list)
    open_delta: int = 0
    open_rows: int = 0
    #: Regions that tripped the quotation policy, by task id, as locators -- never the
    #: quoted text. Populated whether or not the policy was enforced, so an import run
    #: with it off still says what it let through.
    quoted_remarks: Dict[str, List[str]] = field(default_factory=dict)
    quotation_policy_enforced: bool = True

    @property
    def reconciles(self) -> bool:
        """Whether the backlog invariant holds after the import."""
        return self.open_delta == self.open_rows

    def render(self) -> str:
        """A short operator-readable summary."""
        lines = [
            f"imported {self.imported} tasks, {self.events} history events, "
            f"{self.blobs} attachment blobs",
            f"git observations used: {self.backfilled}",
            f"reconciliation events written for {len(self.reconciled)} tasks whose "
            "replayed history disagreed with their record",
            f"backlog invariant: sum(open_delta)={self.open_delta} "
            f"open_tasks={self.open_rows} "
            f"{'OK' if self.reconciles else 'MISMATCH'}",
        ]
        if self.quarantined:
            lines.append(f"quarantined {len(self.quarantined)} unreadable records:")
            lines.extend(f"  {name}: {error}" for name, error in self.quarantined)
        if self.quoted_remarks:
            held = sum(len(items) for items in self.quoted_remarks.values())
            lines.append(
                f"quotation policy NOT enforced: {held} region(s) across "
                f"{len(self.quoted_remarks)} record(s) quote a person verbatim and were "
                "imported anyway:"
            )
            for task_id in sorted(self.quoted_remarks):
                lines.extend(f"  {task_id}: {item}" for item in self.quoted_remarks[task_id])
        if self.missing_blobs:
            lines.append(
                f"{len(self.missing_blobs)} attachment(s) had no readable sidecar file "
                "and were left out; the entries referencing them were quarantined"
            )
        return "\n".join(lines)


def _iso(value: Any) -> Optional[str]:
    """Normalise whatever the YAML held into the stored timestamp form."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


class CorpusImporter:
    """Reads a tasks directory into a :class:`SqlTaskStore`."""

    def __init__(self, store: SqlTaskStore, tasks_dir: Path) -> None:
        """Bind an importer to a store and the directory it reads."""
        self.store = store
        self.tasks_dir = Path(tasks_dir)
        self.database: Database = store.database
        self._observations: Dict[str, List[Observation]] = {}
        self._first_seen: Dict[str, str] = {}

    def run(
        self,
        *,
        reporting_tz: str = "UTC",
        backfill_git: bool = False,
        enforce_quotation_policy: bool = True,
        replace: bool = False,
    ) -> ImportReport:
        """Import every task file, then reconstruct and reconcile history.

        The whole import is one transaction. That is not only for speed: the deferred
        parent foreign key means a child may legitimately arrive before its parent, and
        the constraint is checked at commit, so a partially-ordered corpus loads without
        the importer having to topologically sort a task graph first.

        The content check runs before that transaction opens and raises
        :class:`QuotationPolicyError` if any readable record quotes a person verbatim,
        so a refusal leaves the store untouched rather than half-written. Pass
        ``enforce_quotation_policy=False`` to import anyway; the report records that.
        """
        report = ImportReport()
        report.quotation_policy_enforced = enforce_quotation_policy
        if backfill_git:
            # GitUnavailable propagates deliberately: the caller asked for a
            # backfill, and quietly importing without one would leave a store whose
            # coverage metadata says `backfilled` while nothing was.
            found = observe(self.tasks_dir)
            for item in found:
                self._observations.setdefault(item.task_file, []).append(item)
            self._first_seen = first_seen(found)
            report.backfilled = len(found)
        with self.database.write() as connection:
            self.store.ensure_project(root=str(self.tasks_dir), reporting_tz=reporting_tz)
            existing = self._existing_rows(connection)
            if existing and not replace:
                raise CorpusAlreadyImported(
                    f"project {self.store.project_id!r} already holds {existing} task "
                    "row(s). Re-running would write its reconstructed history a second "
                    "time and double every event. Pass replace=True (the CLI's "
                    "--replace) to empty this project and import it again in one "
                    "transaction."
                )
            if replace:
                self._empty_project(connection)
            documents = self._read_documents(report)
            # Screened before a single task row is written, and inside the transaction
            # rather than ahead of it, so the refusal below rolls back even the
            # quarantine rows `_read_documents` may just have added. "Nothing was
            # imported" is then literally true of the file on disk, which is what makes
            # a re-run after the fix a clean re-run rather than a repair.
            offenders = self._quoted_remarks(documents)
            report.quoted_remarks = {
                task_id: [remark.locator() for remark in remarks]
                for task_id, remarks in offenders.items()
            }
            if offenders and enforce_quotation_policy:
                raise QuotationPolicyError(offenders)
            self._load_blobs(documents, report)
            tasks = self._insert_tasks(documents, report)
            report.events = self._reconstruct(connection, tasks, report)
            connection.execute(
                "UPDATE project SET imported_from = ?, imported_at = ?, "
                "history_baseline_kind = ?, history_baseline_at = ? "
                "WHERE project_id = ?",
                (
                    str(self.tasks_dir),
                    _iso(datetime.now(tz=timezone.utc)),
                    "backfilled" if backfill_git else "reconstructed",
                    min((_iso(task.created) or "" for task in tasks), default=None),
                    self.store.project_id,
                ),
            )
        report.open_delta, report.open_rows = self.store.open_delta_reconciles()
        return report

    # ------------------------------------------------------------------
    # Re-running
    # ------------------------------------------------------------------

    def _existing_rows(self, connection: Any) -> int:
        """How many task rows this project already holds."""
        return int(
            connection.execute(
                "SELECT COUNT(*) AS n FROM task WHERE project_id = ?",
                (self.store.project_id,),
            ).fetchone()["n"]
        )

    def _empty_project(self, connection: Any) -> None:
        """Delete everything this project owns, inside the caller's transaction.

        Ordered children-first rather than relying on cascade, because ``task_event``,
        ``operation``, ``webhook_outbox`` and ``import_quarantine`` are keyed on the
        project and not on a task, so nothing would cascade them. ``blob`` is content
        addressed and shared between projects, so it is left alone: an orphaned blob is
        re-referenced by the import that follows, and deleting one another project still
        points at would be the only irreversible thing this could do.

        The ``project`` row itself stays, so its ``reporting_tz`` and any operator
        configuration survive a re-import.
        """
        project_id = self.store.project_id
        for table in (
            "attachment",
            "log_entry",
            "task_tag",
            "task_dependency",
            "task_acceptance",
            "task_deliverable",
            "task_branch",
            "task_run",
            "task_event",
            "operation",
            "webhook_outbox",
            "import_quarantine",
            "task_fts",
            "task",
        ):
            connection.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def _read_documents(self, report: ImportReport) -> List[Tuple[Path, Dict[str, Any]]]:
        """Parse every task file, quarantining the ones that cannot be read.

        A record that fails to parse or validate is not skipped and not forced in: it
        goes to ``import_quarantine``, which has no constraints, so the live tables stay
        provably valid while the bad record stays inspectable and re-importable.
        """
        documents: List[Tuple[Path, Dict[str, Any]]] = []
        for path in sorted(self.tasks_dir.glob("*.yaml")):
            raw = path.read_text(encoding="utf-8", errors="replace")
            try:
                document = yaml.safe_load(raw)
                if not isinstance(document, dict):
                    raise ValueError("the file does not contain a task mapping")
                Task.model_validate(document)
            except (yaml.YAMLError, ValidationError, ValueError) as exc:
                self._quarantine(path, raw, str(exc)[:2000])
                report.quarantined.append((path.name, str(exc)[:160]))
                continue
            documents.append((path, document))
        return documents

    @staticmethod
    def _quoted_remarks(
        documents: List[Tuple[Path, Dict[str, Any]]]
    ) -> Dict[str, List[QuotedRemark]]:
        """Every readable record that quotes a person verbatim, by task id."""
        offenders: Dict[str, List[QuotedRemark]] = {}
        for _, document in documents:
            task = Task.model_validate(document)
            remarks = scan_task(task)
            if remarks:
                offenders[task.id] = remarks
        return offenders

    def _quarantine(self, path: Path, raw: str, error: str) -> None:
        """Record one unreadable file, with the error that stopped it."""
        guess = None
        for line in raw.splitlines()[:20]:
            if line.startswith("id:"):
                guess = line.split(":", 1)[1].strip()
                break
        self.database.writer.execute(
            "INSERT INTO import_quarantine(project_id, source_path, task_id_guess, "
            "raw_text, error, imported_at) VALUES (?,?,?,?,?,?)",
            (
                self.store.project_id,
                str(path),
                guess,
                raw,
                error,
                _iso(datetime.now(tz=timezone.utc)),
            ),
        )

    def _load_blobs(
        self, documents: List[Tuple[Path, Dict[str, Any]]], report: ImportReport
    ) -> None:
        """Copy attachment bytes out of the sidecar files and into the store.

        Done before any task is inserted, because a task referencing a blob that is not
        there is refused rather than repaired.
        """
        for path, document in documents:
            for entry in document.get("log") or []:
                for attachment in entry.get("attachments") or []:
                    digest = attachment.get("sha256")
                    if not digest or self.store.attachments.has(digest):
                        continue
                    sidecar = self.tasks_dir / attachment.get("path", "")
                    try:
                        data = sidecar.read_bytes()
                    except OSError:
                        report.missing_blobs.append(str(sidecar))
                        continue
                    self.store.attachments.put(
                        digest, attachment.get("media_type", "image/png"), data
                    )
                    report.blobs += 1

    def _insert_tasks(
        self, documents: List[Tuple[Path, Dict[str, Any]]], report: ImportReport
    ) -> List[Task]:
        """Persist every readable task, without letting the store derive live history.

        ``save_task`` normally emits a ``native`` event per write, which is exactly
        right for a verb and exactly wrong here: it would stamp all 367 tasks as created
        at import time. History for an imported corpus is reconstructed instead.
        """
        tasks: List[Task] = []
        for path, document in documents:
            task = Task.model_validate(document)
            try:
                # Inside a savepoint, so a record that fails *halfway* through being
                # written leaves nothing rather than a partial row. Without it the task
                # row and the log entries inserted before the failure survived -- the
                # whole import being one transaction, only the outermost block rolls
                # back -- and the record then read as valid with most of its log gone.
                with self.database.savepoint("import_task"):
                    self.store.save_task(task, _record_history=False)
            except Exception as exc:  # noqa: BLE001 - the file is the suspect, not us
                self._quarantine(path, yaml.safe_dump(document, sort_keys=False), str(exc)[:2000])
                report.quarantined.append((path.name, str(exc)[:160]))
                continue
            tasks.append(task)
            report.imported += 1
        return tasks

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def _reconstruct(self, connection: Any, tasks: List[Task], report: ImportReport) -> int:
        """Replay each task's log into events, then reconcile against its record.

        Two passes rather than one, because :meth:`_bulk_renumber_commits` is a
        question about the corpus and cannot be answered while walking it: whether a
        position change was a bulk renumber depends on how many *other* tasks the same
        commit renumbered.
        """
        prepared = [(task, self._prepare(task)) for task in tasks]
        renumbers = self._bulk_renumber_commits(prepared)
        written = 0
        for task, rows in prepared:
            state: Dict[str, Any] = {stem: None for stem, _ in EVENT_AXES}
            state["archived"] = 0
            for row in rows:
                before = dict(state)
                after = dict(state)
                after.update({k: v for k, v in row["after"].items()})
                if row["kind"] == "create":
                    before = {stem: None for stem, _ in EVENT_AXES}
                self._write_event(
                    connection,
                    task.id,
                    row,
                    before,
                    after,
                    source=row.get("source", "reconstructed"),
                    detail=row.get("detail"),
                    mechanical=self._is_bulk_renumber(row, renumbers),
                )
                state = after
                written += 1
            truth = self._truth(task)
            if {k: state.get(k) for k in truth} != truth:
                self._write_event(
                    connection,
                    task.id,
                    {
                        "kind": "import",
                        "ts": _iso(task.updated),
                        "actor": "import",
                        "log_entry_id": None,
                        "operation_id": None,
                        "after": truth,
                    },
                    state,
                    {**state, **truth},
                    source="reconstructed",
                    detail={"reason": "replayed history disagreed with the imported record"},
                )
                written += 1
                report.reconciled.append(task.id)
        return written


    def _prepare(self, task: Task) -> List[Dict[str, Any]]:
        """This task's events, replayed, backfilled and ordered -- but not yet written."""
        rows = self._replay(task)
        git_rows = self._git_rows(task, rows)
        if not any(row["kind"] == "create" for row in rows):
            # Without a creation event the backlog series cannot reconcile: 79
            # tasks in this repository have none. The instant is the earliest
            # evidence the task existed, never a later one -- Rule A, analytics
            # design 4.3, where taking commit time literally opened the series
            # at -3.
            candidates = [_iso(task.created)]
            if task.log:
                candidates.append(_iso(task.log[0].ts))
            seen = self._first_seen.get(f"{task.id}.yaml")
            candidates.append(_utc(seen) if seen else None)
            born = min(x for x in candidates if x)
            rows.insert(
                0,
                {
                    "kind": "create",
                    "ts": born,
                    "actor": "import",
                    "log_entry_id": None,
                    "operation_id": None,
                    "after": {
                        "lifecycle": self._first_open_lifecycle(task),
                        # A task is not created archived, and is created with no
                        # parent and no place in line. Seeding these keeps the
                        # reconciliation below reporting the axes the log genuinely
                        # cannot reconstruct rather than every axis it never mentions.
                        "archived": 0,
                        "parent": None,
                        "position": None,
                    },
                },
            )
        # Git's *initial* observations seed the creation rather than becoming
        # events of their own: the replay's gap was never missing steps, it was
        # having no starting value. The rest merge in by timestamp.
        create = next(row for row in rows if row["kind"] == "create")
        for row in git_rows:
            if row.pop("_initial", False):
                # Assigned, not defaulted. Git is the authority for these axes
                # (Rule B) and the synthesised creation above seeds conservative
                # placeholders for them -- `setdefault` would let the placeholder
                # win, which silently discarded every backfilled parent.
                create["after"][row["axis"]] = row["after"][row["axis"]]
            else:
                rows.append(row)
        rows.sort(key=lambda row: (row["ts"] or "", row["kind"] != "create"))
        return rows

    @staticmethod
    def _bulk_renumber_commits(
        prepared: List[Tuple[Task, List[Dict[str, Any]]]],
    ) -> Dict[str, int]:
        """Commits that renumbered more than one task's place in line, and by how many.

        Item H of analytics-design section 6: ``mechanical`` has to be *populated*, not
        merely defined, because the page uses it to keep a bulk renumber out of the
        activity series -- and a column that is always ``0`` is worse than an absent
        one, since the query over it looks correct.

        Section 4.4 supplies the definition rather than a heuristic: *a bulk renumber
        rewrites* ``queue_position`` *with no per-task log entry*. Both halves are
        already computed by the time this runs. :meth:`_git_rows` has dropped every git
        observation sitting within :data:`DEDUPE_WINDOW_SECONDS` of a native
        ``queue_move``, so a surviving row is by construction a position change nobody
        logged; grouping the survivors by commit says how many tasks one commit did that
        to. Moving a task inside a band renumbers its band-mates as a side effect: the
        moved task's own change carries a log entry and has already dropped out, and
        what is left is the collateral.

        **Measured on this repository's 376 records** (2026-09-08): 325 surviving
        backfilled position events across 122 commits, of which 112 commits produced
        exactly one event and the rest produced 3, 4, 5, 87 and 93. The threshold falls
        in the gap between 5 and 87, so it decides nothing delicate -- 213 of the 325
        events are marked, and the two largest commits are the bulk renumbers section
        8.7 names.

        **Only ``queue_position``.** A commit that changes ``priority`` on seven tasks
        is a grooming pass -- seven decisions, which is activity -- and marking those
        mechanical would drop real work off the chart to catch nothing. The design names
        the renumber and nothing else, and that narrowness is deliberate.
        """
        touched: Dict[str, set] = {}
        for task, rows in prepared:
            for row in rows:
                commit = _renumber_commit(row)
                if commit is not None:
                    touched.setdefault(commit, set()).add(task.id)
        return {commit: len(tasks) for commit, tasks in touched.items() if len(tasks) > 1}

    @staticmethod
    def _is_bulk_renumber(row: Dict[str, Any], renumbers: Dict[str, int]) -> bool:
        """True when this event is one task's share of a bulk renumber."""
        commit = _renumber_commit(row)
        return commit is not None and commit in renumbers

    def _git_rows(self, task: Task, replayed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Turn this task's git observations into events, applying Rule B.

        Only the axes the log cannot speak for. ``queue_position`` is the one axis both
        sources see -- a bulk renumber rewrites it with no per-task log entry -- so a
        git observation is dropped when a native move of the same axis sits within
        :data:`DEDUPE_WINDOW_SECONDS` of it.
        """
        observations = self._observations.get(f"{task.id}.yaml", ())
        if not observations:
            return []
        native_moves = [row["ts"] for row in replayed if row["kind"] == "queue_move" and row["ts"]]
        rows: List[Dict[str, Any]] = []
        for item in observations:
            axis = "position" if item.field == "queue_position" else item.field
            value: Any = item.after
            if axis == "archived":
                value = 1 if str(item.after).lower() == "true" else 0
            elif axis == "position":
                digits = item.after or ""
                value = int(digits) if digits.isdigit() else None
                if any(
                    abs(_seconds_between(item.ts, moment)) <= DEDUPE_WINDOW_SECONDS
                    for moment in native_moves
                ):
                    continue
            elif axis == "parent" and not item.after:
                value = None
            kind = {
                "priority": "reprioritize",
                "archived": "archive",
                "position": "queue_move",
                "parent": "update_content",
            }[axis]
            if axis == "archived" and value == 0:
                kind = "unarchive"
            rows.append(
                {
                    "kind": kind,
                    # Normalised to the same UTC form every other timestamp uses. Git
                    # emits a local offset and the manager emits `Z`; leaving both in the
                    # column would misorder events under a string sort *and* put a change
                    # in the wrong day bucket, which is the one thing every chart on the
                    # analytics page depends on.
                    "ts": _utc(item.ts),
                    "actor": "git-backfill",
                    "log_entry_id": None,
                    "operation_id": None,
                    "axis": axis,
                    "after": {axis: value},
                    "source": "backfilled",
                    "detail": {"git_commit": item.commit, "field": item.field},
                    "_initial": item.is_initial,
                }
            )
        return rows

    @staticmethod
    def _first_open_lifecycle(task: Task) -> str:
        """The earliest lifecycle the log evidences, for a synthesised creation.

        Falls back to the current one, and never to ``closed``: a task cannot be
        created closed, and saying so would make the backlog series open negative.
        """
        for entry in task.log:
            value = entry.data.get("lifecycle")
            if value and value != "closed":
                return str(value)
        current = str(task.lifecycle)
        return current if current != "closed" else "ready"

    def _replay(self, task: Task) -> List[Dict[str, Any]]:
        """Turn the log into ordered state changes, as far as it can be trusted."""
        rows: List[Dict[str, Any]] = []
        created = False
        closed_seen = False
        for entry in task.log:
            data = entry.data or {}
            kind = str(entry.type)
            after: Dict[str, Any] = {}
            if kind == "transition":
                lifecycle = data.get("lifecycle")
                body = (entry.body or "").lower()
                if lifecycle == "closed":
                    event = "close"
                    closed_seen = True
                elif not created and "created" in body:
                    event, created = "create", True
                elif closed_seen and lifecycle and lifecycle != "closed":
                    event, closed_seen = "reopen", False
                elif lifecycle == "active":
                    event = "claim"
                else:
                    event = "update_content"
                for key in ("lifecycle", "ball", "ball_reason", "outcome", "owner", "priority"):
                    if key in data:
                        after[key] = data[key]
                if event == "close":
                    # Rule 6: a closed task holds no place in line. The log never says
                    # so explicitly, so every closed task disagreed on `position`.
                    after.update(
                        {
                            "ball": None,
                            "ball_reason": None,
                            "owner": None,
                            "position": None,
                        }
                    )
                elif event == "claim":
                    # A claim entry records the lifecycle and the ball, never the
                    # owner -- but the actor writing it is by definition the claimer.
                    after.setdefault("owner", entry.actor)
                elif lifecycle in IMPLIED_BALL:
                    ball, reason = IMPLIED_BALL[lifecycle]
                    after.setdefault("ball", ball)
                    after.setdefault("ball_reason", reason)
                    if lifecycle == "ready":
                        after.setdefault("owner", None)
            elif kind == "handoff":
                event = "handoff"
                for key in ("ball", "ball_reason"):
                    if key in data:
                        after[key] = data[key]
            elif kind == "queue_move":
                event = "queue_move"
                after["position"] = data.get("to")
            else:
                continue
            rows.append(
                {
                    "kind": event,
                    "ts": _iso(entry.ts),
                    "actor": entry.actor,
                    "log_entry_id": entry.id,
                    "operation_id": (data.get("operation") or {}).get("id"),
                    "after": after,
                }
            )
        return rows

    @staticmethod
    def _truth(task: Task) -> Dict[str, Any]:
        """The task's real current state on the axes a replay can get wrong."""
        return {
            "lifecycle": str(task.lifecycle),
            "ball": None if task.ball is None else str(task.ball),
            "ball_reason": None if task.ball_reason is None else str(task.ball_reason),
            "outcome": None if task.outcome is None else str(task.outcome),
            "archived": 1 if task.archived else 0,
            "priority": str(task.priority),
            "owner": task.assignment.owner,
            "position": task.queue_position,
            "parent": task.parent,
        }

    def _write_event(
        self,
        connection: Any,
        task_id: str,
        row: Dict[str, Any],
        before: Dict[str, Any],
        after: Dict[str, Any],
        *,
        source: str,
        detail: Optional[Dict[str, Any]] = None,
        mechanical: bool = False,
    ) -> None:
        """Insert one reconstructed or backfilled event."""
        columns = [
            "project_id",
            "task_id",
            "ts",
            "actor",
            "kind",
            "log_entry_id",
            "operation_id",
            "source",
            "mechanical",
            "detail_json",
        ]
        values: List[Any] = [
            self.store.project_id,
            task_id,
            row["ts"],
            row["actor"],
            row["kind"],
            row.get("log_entry_id"),
            row.get("operation_id"),
            source,
            1 if mechanical else 0,
            json.dumps(detail or {}),
        ]
        for stem, _ in EVENT_AXES:
            columns.extend([f"{stem}_from", f"{stem}_to"])
            values.extend([before.get(stem), after.get(stem)])
        connection.execute(
            f"INSERT INTO task_event({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in values)})",
            values,
        )


__all__ = [
    "CorpusAlreadyImported",
    "CorpusImporter",
    "ImportReport",
    "IMPLIED_BALL",
    "QuotationPolicyError",
]


def _renumber_commit(row: Dict[str, Any]) -> Optional[str]:
    """The commit behind a backfilled position change, or ``None`` if this is not one.

    One predicate, used by both halves of the bulk-renumber check, so that what counts
    as a renumber is written down once.
    """
    if row.get("source") != "backfilled" or row.get("axis") != "position":
        return None
    commit = (row.get("detail") or {}).get("git_commit")
    return str(commit) if commit else None


def _utc(value: str) -> str:
    """An ISO-8601 instant in the single stored form: UTC, ``Z``-suffixed."""
    return _parse(value).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _seconds_between(left: str, right: str) -> float:
    """Signed distance between two ISO-8601 instants, in seconds.

    Tolerant of the mixed forms the corpus holds -- git emits an offset, the manager
    emits ``Z`` -- because a de-duplication window that silently fails to parse would
    let every git observation through and double-count them.
    """
    return (_parse(left) - _parse(right)).total_seconds()


def _parse(value: str) -> datetime:
    """Parse an ISO-8601 instant, treating a naive one as UTC."""
    text = value.replace("Z", "+00:00")
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
