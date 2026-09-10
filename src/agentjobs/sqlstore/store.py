"""``SqlTaskStore`` -- the authoritative SQLite backend for task records.

Shaped to be a drop-in for :class:`agentjobs.storage.TaskStorage` so that task-311's
cutover is a swap rather than a rewrite of every caller. The file-shaped members of that
surface are the exception, and they are deliberately not emulated: ``task_path`` raises,
and the three advisory file locks become real transactions. See
:mod:`agentjobs.storage_protocol` for the boundary this satisfies.

**History is derived here, not by callers.** Every write diffs the incoming task against
the row already stored and emits the matching ``task_event``. That is the one design
decision in this module worth arguing for: a caller that must remember to record history
eventually forgets, and the resulting gap is invisible until someone asks a question
about the past and gets a confident wrong answer.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import yaml

from ..attachments import MEDIA_TYPES
from ..models_v2 import Task
from .blobs import SqlAttachmentStore
from .connection import Database, SqlStoreError
from .reporting_tz import check_reporting_tz

#: Axes carried on every ``task_event`` row, as (event column stem, task attribute).
EVENT_AXES: Tuple[Tuple[str, str], ...] = (
    ("lifecycle", "lifecycle"),
    ("ball", "ball"),
    ("ball_reason", "ball_reason"),
    ("outcome", "outcome"),
    ("archived", "archived"),
    ("priority", "priority"),
    ("owner", "owner"),
    ("position", "queue_position"),
    ("parent", "parent"),
)


def _now() -> str:
    """Current instant as the ISO-8601 UTC string every timestamp column holds."""
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _iso(value: Optional[datetime]) -> Optional[str]:
    """Normalise a model datetime to the stored string form."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _enum(value: Any) -> Optional[str]:
    """The stored form of an enum-or-None model field."""
    return None if value is None else str(value)


class TaskNotFound(ValueError):
    """No task with that id exists in this project."""


class SqlTaskStore:
    """Task storage backed by one SQLite database, scoped to one project."""

    supports_task_files = False
    """A record is rows. There is no path to name and nothing to commit."""

    def __init__(self, database: Database, project_id: str) -> None:
        """Bind a store to ``project_id`` inside ``database``."""
        self.database = database
        self.project_id = project_id
        self.attachments = SqlAttachmentStore(database, project_id)

    # -----------------------------------------------------------------------
    # Project bookkeeping
    # -----------------------------------------------------------------------

    def ensure_project(
        self,
        *,
        root: Optional[str] = None,
        reporting_tz: str = "UTC",
    ) -> None:
        """Create this project's row if it is not there yet.

        ``reporting_tz`` is an IANA zone name such as ``America/Chicago``, never a
        fixed offset: an offset is right for half the year (analytics design 3.5,
        section 6 item C). Checked here rather than trusted, because the wrong value
        does not fail -- it silently misfiles late-evening work by a day for six
        months of the year, and there is nothing on the chart to see. Migration 002
        carries the same rule as a trigger, for a writer that does not come through
        this door.
        """
        reporting_tz = check_reporting_tz(reporting_tz)
        with self.database.write() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO project(project_id, root, created_at, reporting_tz) "
                "VALUES (?, ?, ?, ?)",
                (self.project_id, root, _now(), reporting_tz),
            )

    # -----------------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        """The connection a read should use.

        Inside a write transaction the reader connection cannot see uncommitted rows,
        so a verb that reads back what it just wrote must use the writer. Outside one,
        the thread's reader keeps the write path free.
        """
        return self.database.writer if self.database.in_transaction else self.database.reader()

    def load_task(self, task_id: str) -> Optional[Task]:
        """Assemble one task, or None when there is no such task."""
        task_id = self._normalised_id(task_id)
        connection = self._connection()
        row = connection.execute(
            "SELECT * FROM task WHERE project_id = ? AND task_id = ?",
            (self.project_id, task_id),
        ).fetchone()
        if row is None:
            return None
        return self._assemble(connection, [row])[0]

    # The cache these mirror does not exist here: a query is the read, and there is no
    # snapshot to go stale. They stay because the manager and the API call them by name.
    load_task_uncached = load_task

    def list_tasks(self) -> List[Task]:
        """Every task in the project, in the listing order the queue defines."""
        connection = self._connection()
        rows = connection.execute(
            "SELECT * FROM task WHERE project_id = ? "
            "ORDER BY (lifecycle = 'closed'), priority_rank, queue_position, task_id",
            (self.project_id,),
        ).fetchall()
        return self._assemble(connection, rows)

    list_tasks_uncached = list_tasks

    def search_tasks(self, query: str) -> List[Task]:
        """Full-text search, with an exact id match first.

        The id comes first because it is the handle people quote to each other: a
        reviewer asking about "058" means task-058, and a search that reads every prose
        field but not the identifier answers "no such task" to the one query it must
        always get right.
        """
        text = query.strip()
        if not text:
            return []
        connection = self._connection()
        found: List[str] = []
        exact = connection.execute(
            "SELECT task_id FROM task WHERE project_id = ? AND lower(task_id) LIKE ?",
            (self.project_id, f"%{text.lower()}%"),
        ).fetchall()
        found.extend(row["task_id"] for row in exact)
        try:
            hits = connection.execute(
                "SELECT task_id FROM task_fts WHERE project_id = ? AND task_fts MATCH ? "
                "ORDER BY rank",
                (self.project_id, self._fts_query(text)),
            ).fetchall()
        except sqlite3.OperationalError:
            # An FTS5 syntax error is a user typing punctuation, not a fault. The id
            # matches above still stand, so degrade to them rather than raising.
            hits = []
        for row in hits:
            if row["task_id"] not in found:
                found.append(row["task_id"])
        if not found:
            return []
        placeholders = ",".join("?" for _ in found)
        rows = connection.execute(
            f"SELECT * FROM task WHERE project_id = ? AND task_id IN ({placeholders})",
            (self.project_id, *found),
        ).fetchall()
        order = {task_id: index for index, task_id in enumerate(found)}
        tasks = self._assemble(connection, rows)
        tasks.sort(key=lambda task: order[task.id])
        return tasks

    @staticmethod
    def _fts_query(text: str) -> str:
        """Turn user text into an FTS5 expression that cannot be a syntax error.

        Every token is quoted, so punctuation a person typed is searched for rather
        than parsed as an operator, and a trailing ``*`` keeps prefix matching -- typing
        "attach" should find "attachments" while the word is still being typed.
        """
        tokens = [
            part
            for part in "".join(
                character if character.isalnum() else " " for character in text
            ).split()
            if part
        ]
        if not tokens:
            raise sqlite3.OperationalError("no searchable tokens")
        return " AND ".join(f'"{token}"*' for token in tokens)

    def project_revision(self) -> Tuple[str, int]:
        """A cheap signal that changes whenever this project's tasks change.

        The file backend hashes every task file on every poll -- 47 ms over 9 MB. Here
        the same question is the high-water mark of a monotonic counter plus the row
        count, which is an index probe.
        """
        connection = self._connection()
        row = connection.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(revision), 0) AS r, "
            "COALESCE(MAX(updated_at), '') AS u FROM task WHERE project_id = ?",
            (self.project_id,),
        ).fetchone()
        return f"{row['r']}-{row['u']}", int(row["n"])

    # -----------------------------------------------------------------------
    # Assembly
    # -----------------------------------------------------------------------

    def _assemble(self, connection: sqlite3.Connection, rows: Sequence[sqlite3.Row]) -> List[Task]:
        """Build ``Task`` aggregates from task rows plus one keyed read per child table.

        Batched deliberately: a listing of 367 tasks is eight queries, not 367 x 8.
        """
        if not rows:
            return []
        ids = [row["task_id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        args = (self.project_id, *ids)

        def grouped(sql: str) -> Dict[str, List[sqlite3.Row]]:
            out: Dict[str, List[sqlite3.Row]] = {task_id: [] for task_id in ids}
            for item in connection.execute(sql, args):
                out[item["task_id"]].append(item)
            return out

        tags = grouped(
            f"SELECT * FROM task_tag WHERE project_id = ? AND task_id IN ({placeholders}) "
            "ORDER BY ord"
        )
        deps = grouped(
            f"SELECT * FROM task_dependency WHERE project_id = ? AND task_id IN "
            f"({placeholders}) ORDER BY ord"
        )
        acceptance = grouped(
            f"SELECT * FROM task_acceptance WHERE project_id = ? AND task_id IN "
            f"({placeholders}) ORDER BY ord"
        )
        deliverables = grouped(
            f"SELECT * FROM task_deliverable WHERE project_id = ? AND task_id IN "
            f"({placeholders}) ORDER BY ord"
        )
        branches = grouped(
            f"SELECT * FROM task_branch WHERE project_id = ? AND task_id IN "
            f"({placeholders}) ORDER BY ord"
        )
        entries = grouped(
            f"SELECT * FROM log_entry WHERE project_id = ? AND task_id IN "
            f"({placeholders}) ORDER BY entry_id"
        )
        attachments = grouped(
            f"SELECT a.*, b.media_type, b.size_bytes FROM attachment a "
            f"JOIN blob b ON b.sha256 = a.sha256 "
            f"WHERE a.project_id = ? AND a.task_id IN ({placeholders}) ORDER BY a.ord"
        )
        runs: Dict[str, Dict[str, sqlite3.Row]] = {task_id: {} for task_id in ids}
        for run in connection.execute(
            f"SELECT * FROM task_run WHERE project_id = ? AND task_id IN ({placeholders})", args
        ):
            runs[run["task_id"]][run["run_id"]] = run

        tasks: List[Task] = []
        for row in rows:
            task_id = row["task_id"]
            by_entry: Dict[int, List[sqlite3.Row]] = {}
            for item in attachments[task_id]:
                by_entry.setdefault(item["entry_id"], []).append(item)
            tasks.append(
                Task.model_validate(
                    self._document(
                        row,
                        tags=tags[task_id],
                        dependencies=deps[task_id],
                        acceptance=acceptance[task_id],
                        deliverables=deliverables[task_id],
                        branches=branches[task_id],
                        entries=entries[task_id],
                        attachments=by_entry,
                        runs=runs[task_id],
                    )
                )
            )
        return tasks

    def _document(
        self,
        row: sqlite3.Row,
        *,
        tags: Sequence[sqlite3.Row],
        dependencies: Sequence[sqlite3.Row],
        acceptance: Sequence[sqlite3.Row],
        deliverables: Sequence[sqlite3.Row],
        branches: Sequence[sqlite3.Row],
        entries: Sequence[sqlite3.Row],
        attachments: Dict[int, List[sqlite3.Row]],
        runs: Dict[str, sqlite3.Row],
    ) -> Dict[str, Any]:
        """The task document, in exactly the shape ``Task`` validates.

        This is the single place the stored columns become the public API shape, which
        is what "one authoritative representation per field, assembled into the existing
        shape" means in practice.
        """
        document: Dict[str, Any] = {
            "schema": 2,
            "id": row["task_id"],
            "title": row["title"],
            "created": row["created_at"],
            "updated": row["updated_at"],
            "lifecycle": row["lifecycle"],
            "archived": bool(row["archived"]),
            "priority": row["priority"],
            "category": row["category"],
            "tags": [item["tag"] for item in tags],
            "assignment": {"eligible": json.loads(row["eligible_json"])},
            "spec": {
                "summary": row["spec_summary"],
                "description": row["spec_description"],
                "context": json.loads(row["spec_context_json"]),
            },
            "acceptance": [],
            "deliverables": [],
            "dependencies": [],
            "links": json.loads(row["links_json"]),
            "branches": [],
            "log": [],
        }
        for key, column in (
            ("ball", "ball"),
            ("ball_reason", "ball_reason"),
            ("ball_prompt", "ball_prompt"),
            ("outcome", "outcome"),
            ("queue_position", "queue_position"),
            ("effort", "effort"),
            ("parent", "parent_id"),
            ("posture", "posture"),
        ):
            if row[column] is not None:
                document[key] = row[column]
        if row["owner"] is not None:
            document["assignment"]["owner"] = row["owner"]
        for key, column in (
            ("intent", "spec_intent"),
            ("constraints", "spec_constraints"),
            ("out_of_scope", "spec_out_of_scope"),
        ):
            if row[column] is not None:
                document["spec"][key] = row[column]

        for item in acceptance:
            criterion: Dict[str, Any] = {
                "id": item["ac_id"],
                "text": item["text"],
                "status": item["status"],
            }
            if item["verify"] is not None:
                criterion["verify"] = item["verify"]
            document["acceptance"].append(criterion)
        for item in deliverables:
            entry: Dict[str, Any] = {"path": item["path"], "status": item["status"]}
            if item["note"] is not None:
                entry["note"] = item["note"]
            document["deliverables"].append(entry)
        for item in dependencies:
            dependency: Dict[str, Any] = {"task": item["other_id"], "type": item["type"]}
            if item["note"] is not None:
                dependency["note"] = item["note"]
            document["dependencies"].append(dependency)
        for item in branches:
            branch: Dict[str, Any] = {"name": item["name"], "status": item["status"]}
            if item["merged_at"] is not None:
                branch["merged_at"] = item["merged_at"]
            document["branches"].append(branch)

        for item in entries:
            data = json.loads(item["data_json"])
            log_entry: Dict[str, Any] = {
                "id": item["entry_id"],
                "ts": item["ts"],
                "actor": item["actor"],
                "type": item["type"],
                "data": data,
            }
            if item["body"] is not None:
                log_entry["body"] = item["body"]
            if item["re"] is not None:
                log_entry["re"] = item["re"]
            # What ran is a property of the run, and `task_run` is authoritative for
            # it -- measured across this repository's corpus, a run id carries exactly
            # one `dispatch` entry (167 of 167).
            #
            # How it *ended* is not: two run ids in the same corpus carry two
            # `dispatch_result` entries each -- interrupted at 22:27, completed at
            # 22:31, both true, both about one run. A row cannot hold two outcomes, so
            # the terminal payload stays on the entry that recorded it and
            # `task_run.ended_at`/`outcome` are a latest-observed projection over those
            # entries, kept only to make "which runs are in the air" an index probe.
            if item["type"] == "dispatch":
                run = runs.get(str(data.get("run_id", "")))
                if run is not None:
                    data.update(self._run_payload(run))
            found = attachments.get(item["entry_id"])
            if found:
                log_entry["attachments"] = [
                    {
                        "path": (
                            f"attachments/{row['task_id']}/{item['sha256']}"
                            f"{MEDIA_TYPES.get(item['media_type'], '')}"
                        ),
                        "media_type": item["media_type"],
                        "sha256": item["sha256"],
                        "size_bytes": item["size_bytes"],
                        "label": item["label"],
                    }
                    for item in found
                ]
            document["log"].append(log_entry)
        return document

    @staticmethod
    def _run_payload(run: sqlite3.Row) -> Dict[str, Any]:
        """Render a ``dispatch`` entry's ``data`` from the authoritative run row."""
        payload: Dict[str, Any] = {
            "run_id": run["run_id"],
            "agent": run["agent"],
            "runner": run["runner"],
            "mode": run["mode"],
            "posture": run["posture"],
            "trigger": run["trigger"],
            "caused_by": run["caused_by"],
            "argv": json.loads(run["argv_json"]),
            "cwd": run["cwd"],
            "git_head": run["git_head"],
        }
        for key in (
            "posture_source",
            "posture_ceiling",
            "posture_requested",
            "session_id",
            "playbook",
            "playbook_hash",
        ):
            if run[key] is not None:
                payload[key] = run[key]
        if run["selection_json"] is not None:
            payload["selection"] = json.loads(run["selection_json"])
        return payload

    # -----------------------------------------------------------------------
    # Writes
    # -----------------------------------------------------------------------

    def save_task(self, task: Task, *, _record_history: bool = True) -> Task:
        """Persist a task, deriving and recording its history in the same transaction.

        ``_record_history`` is private and exists for exactly one caller: the importer,
        which is loading records whose history already happened and must not be stamped
        as having happened at import time. Every other write records history, and that
        is not optional -- it is what makes the store's own past trustworthy.
        """
        with self.database.write():
            return self._persist(task, record_history=_record_history)

    def mutate_task(self, task_id: str, mutator: Callable[[Task], Optional[Task]]) -> Task:
        """Read, change and write one task inside a single transaction.

        The file backend takes an advisory lock around read-decide-write because the
        three steps are separate operations on a file. Here they are one transaction,
        which is the same guarantee without a lock file that can be left behind by a
        process that died -- audit finding F2 stops existing rather than being fixed.
        """
        task_id = self._normalised_id(task_id)
        with self.database.write():
            current = self.load_task(task_id)
            if current is None:
                raise TaskNotFound(f"Task '{task_id}' not found.")
            updated = mutator(current)
            if updated is None:
                return current
            # Mutators assign attributes, which pydantic does not re-validate, so the
            # consistency rules are re-run here on the finished state.
            Task.model_validate(
                updated.model_dump(mode="python", by_alias=True, exclude={"display_status"})
            )
            return self._persist(updated)

    def _persist(self, task: Task, *, record_history: bool = True) -> Task:
        """Write the whole aggregate, emit history, and refresh the search index.

        ``record_history`` is what distinguishes a verb from an import, and it governs
        the ``updated`` stamp as well as the event. A verb *is* the update, so the
        write restamps. An import is not: the record's own ``updated`` is data being
        preserved, and overwriting it silently re-dates the whole corpus to the
        migration -- which is the exact class of loss the cutover's verification step
        exists to catch, so the two are kept consistent here rather than papered over
        there.
        """
        connection = self.database.writer
        previous = self.load_task(task.id)
        if record_history:
            task.updated = datetime.now(tz=timezone.utc)

        closed = str(task.lifecycle) == "closed"
        closed_at: Optional[str] = None
        if closed:
            existing = connection.execute(
                "SELECT closed_at FROM task WHERE project_id = ? AND task_id = ?",
                (self.project_id, task.id),
            ).fetchone()
            was_closed = previous is not None and str(previous.lifecycle) == "closed"
            closed_at = existing["closed_at"] if (existing and was_closed) else _iso(task.updated)

        first_claimed = connection.execute(
            "SELECT first_claimed_at FROM task WHERE project_id = ? AND task_id = ?",
            (self.project_id, task.id),
        ).fetchone()
        first_claimed_at = first_claimed["first_claimed_at"] if first_claimed else None
        if first_claimed_at is None and str(task.lifecycle) == "active":
            first_claimed_at = _iso(task.updated)

        revision = 1 if previous is None else self._revision_of(task.id) + 1
        last_activity = _iso(task.log[-1].ts) if task.log else _iso(task.updated)

        connection.execute(
            """INSERT INTO task (
                 project_id, task_id, seq, title, created_at, updated_at, revision,
                 lifecycle, ball, ball_reason, ball_prompt, outcome, archived,
                 priority, queue_position, category, effort, owner, parent_id, posture,
                 spec_summary, spec_intent, spec_description, spec_constraints,
                 spec_out_of_scope, spec_context_json, links_json, eligible_json,
                 closed_at, first_claimed_at, last_activity_at, log_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(project_id, task_id) DO UPDATE SET
                 seq=excluded.seq, title=excluded.title, created_at=excluded.created_at,
                 updated_at=excluded.updated_at, revision=excluded.revision,
                 lifecycle=excluded.lifecycle, ball=excluded.ball,
                 ball_reason=excluded.ball_reason, ball_prompt=excluded.ball_prompt,
                 outcome=excluded.outcome, archived=excluded.archived,
                 priority=excluded.priority, queue_position=excluded.queue_position,
                 category=excluded.category, effort=excluded.effort, owner=excluded.owner,
                 parent_id=excluded.parent_id, posture=excluded.posture,
                 spec_summary=excluded.spec_summary, spec_intent=excluded.spec_intent,
                 spec_description=excluded.spec_description,
                 spec_constraints=excluded.spec_constraints,
                 spec_out_of_scope=excluded.spec_out_of_scope,
                 spec_context_json=excluded.spec_context_json,
                 links_json=excluded.links_json, eligible_json=excluded.eligible_json,
                 closed_at=excluded.closed_at, first_claimed_at=excluded.first_claimed_at,
                 last_activity_at=excluded.last_activity_at, log_count=excluded.log_count
            """,
            (
                self.project_id,
                task.id,
                self._sequence(task.id),
                task.title,
                _iso(task.created),
                _iso(task.updated),
                revision,
                str(task.lifecycle),
                _enum(task.ball),
                _enum(task.ball_reason),
                task.ball_prompt,
                _enum(task.outcome),
                1 if task.archived else 0,
                str(task.priority),
                task.queue_position,
                task.category,
                task.effort,
                task.assignment.owner,
                task.parent,
                _enum(task.posture),
                task.spec.summary,
                task.spec.intent,
                task.spec.description,
                task.spec.constraints,
                task.spec.out_of_scope,
                json.dumps([item.model_dump(mode="json") for item in task.spec.context]),
                json.dumps([item.model_dump(mode="json") for item in task.links]),
                json.dumps(list(task.assignment.eligible)),
                closed_at,
                first_claimed_at,
                last_activity,
                len(task.log),
            ),
        )
        self._replace_children(connection, task)
        if record_history:
            self._emit_event(connection, previous, task)
        return task

    def _revision_of(self, task_id: str) -> int:
        """The revision currently stored, or 0 when the task is new."""
        row = self.database.writer.execute(
            "SELECT revision FROM task WHERE project_id = ? AND task_id = ?",
            (self.project_id, task_id),
        ).fetchone()
        return int(row["revision"]) if row else 0

    @staticmethod
    def _sequence(task_id: str) -> Optional[int]:
        """The number an id carries, for generation and ordering. None if it has none.

        **The number is the segment after the first hyphen, not the last one.** Reading
        the tail is right for ``task-047`` and wrong for every id carrying a slug:
        ``task-047-lint-debt`` ends in ``debt``, so it parsed as "no number" and was
        left out of the maximum -- which is exactly the omission
        :meth:`generate_task_id` says this column exists to fix.

        It bit on 2026-09-08 (task-378). Four projects whose ids all carry slugs were
        cut over, and the next ``agentjobs create`` in each of them minted ``task-001``
        beside a ``task-001-<slug>`` that had been there for weeks, because every
        imported row held ``seq = NULL``. Nothing collided -- the two ids are genuinely
        different strings -- but the numbering had restarted, and it would have kept
        restarting once per create.

        Migration ``003`` recomputes the column for rows already stored, because a
        parser fixed here only ever fixes rows written after it.
        """
        parts = task_id.split("-")
        if len(parts) < 2 or not parts[1].isdigit():
            return None
        return int(parts[1])

    def _replace_children(self, connection: sqlite3.Connection, task: Task) -> None:
        """Rewrite the owned child rows.

        Delete-then-insert rather than a diff: these lists are at most a few dozen rows,
        the whole thing is inside one transaction, and a diff would be more code with
        more ways to leave a stale row behind.
        """
        keys = (self.project_id, task.id)
        for table in (
            "task_tag",
            "task_dependency",
            "task_acceptance",
            "task_deliverable",
            "task_branch",
        ):
            connection.execute(f"DELETE FROM {table} WHERE project_id = ? AND task_id = ?", keys)
        connection.executemany(
            "INSERT INTO task_tag(project_id, task_id, tag, ord) VALUES (?,?,?,?)",
            [(*keys, tag, index) for index, tag in enumerate(dict.fromkeys(task.tags))],
        )
        connection.executemany(
            "INSERT INTO task_dependency(project_id, task_id, other_id, type, note, ord) "
            "VALUES (?,?,?,?,?,?)",
            [
                (*keys, item.task, str(item.type), item.note, index)
                for index, item in enumerate(task.dependencies)
            ],
        )
        connection.executemany(
            "INSERT INTO task_acceptance(project_id, task_id, ac_id, ord, text, verify, "
            "status) VALUES (?,?,?,?,?,?,?)",
            [
                (*keys, item.id, index, item.text, item.verify, str(item.status))
                for index, item in enumerate(task.acceptance)
            ],
        )
        connection.executemany(
            "INSERT INTO task_deliverable(project_id, task_id, path, ord, note, status) "
            "VALUES (?,?,?,?,?,?)",
            [
                (*keys, item.path, index, item.note, str(item.status))
                for index, item in enumerate(task.deliverables)
            ],
        )
        connection.executemany(
            "INSERT INTO task_branch(project_id, task_id, name, status, merged_at, ord) "
            "VALUES (?,?,?,?,?,?)",
            [
                (*keys, item.name, str(item.status), _iso(item.merged_at), index)
                for index, item in enumerate(task.branches)
            ],
        )
        self._replace_log(connection, task)
        self._reindex(connection, task)

    def _replace_log(self, connection: sqlite3.Connection, task: Task) -> None:
        """Insert log entries this task has and the database does not.

        Append-only in both directions: entries are never updated or deleted here, so a
        caller that hands back a task with an entry removed cannot erase it. The
        exception is an entry whose body changed, which the manager never does.
        """
        existing = {
            row["entry_id"]
            for row in connection.execute(
                "SELECT entry_id FROM log_entry WHERE project_id = ? AND task_id = ?",
                (self.project_id, task.id),
            )
        }
        for entry in task.log:
            if entry.id in existing:
                continue
            data = dict(entry.data)
            if entry.type in ("dispatch", "dispatch_result"):
                self._record_run(connection, task.id, entry.type, data)
            connection.execute(
                "INSERT INTO log_entry(project_id, task_id, entry_id, ts, actor, type, "
                "body, re, data_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    self.project_id,
                    task.id,
                    entry.id,
                    _iso(entry.ts),
                    entry.actor,
                    str(entry.type),
                    entry.body,
                    entry.re,
                    json.dumps(data, default=str, sort_keys=True),
                ),
            )
            operation = data.get("operation")
            if isinstance(operation, dict) and operation.get("id"):
                connection.execute(
                    "INSERT OR IGNORE INTO operation(project_id, operation_id, kind, "
                    "actor, fingerprint, task_id, log_entry_id, applied_at, "
                    "result_revision) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        self.project_id,
                        operation["id"],
                        operation.get("kind", "unknown"),
                        entry.actor,
                        operation.get("fingerprint", ""),
                        task.id,
                        entry.id,
                        _iso(entry.ts),
                        self._revision_of(task.id),
                    ),
                )
            for index, attachment in enumerate(entry.attachments or []):
                if not self.attachments.has(attachment.sha256):
                    # A dangling reference is refused rather than repaired. The bytes
                    # are the thing the entry shows, and inventing a placeholder row
                    # would turn a missing image into a corrupt one that reads as fine.
                    raise SqlStoreError(
                        f"{task.id} log entry {entry.id} references attachment "
                        f"{attachment.sha256[:12]}..., whose bytes are not in this "
                        "store. Store the blob before saving the task that points at "
                        "it -- the importer does this from the sidecar file."
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO attachment(project_id, task_id, entry_id, ord, "
                    "sha256, label) VALUES (?,?,?,?,?,?)",
                    (
                        self.project_id,
                        task.id,
                        entry.id,
                        index,
                        attachment.sha256,
                        attachment.label,
                    ),
                )

    def _record_run(
        self, connection: sqlite3.Connection, task_id: str, entry_type: str, data: Dict[str, Any]
    ) -> None:
        """Make ``task_run`` the authoritative home of a dispatch entry's payload."""
        run_id = data.get("run_id")
        if not run_id:
            return
        if entry_type == "dispatch_result":
            connection.execute(
                "UPDATE task_run SET ended_at = ?, outcome = ?, exit_code = ?, "
                "duration_seconds = ?, log_path = ? WHERE project_id = ? AND run_id = ?",
                (
                    _now(),
                    data.get("outcome"),
                    data.get("exit_code"),
                    data.get("duration_seconds"),
                    data.get("log_path"),
                    self.project_id,
                    run_id,
                ),
            )
            return
        connection.execute(
            """INSERT OR IGNORE INTO task_run(
                 project_id, run_id, task_id, agent, runner, mode, posture,
                 posture_source, posture_ceiling, posture_requested, trigger, caused_by,
                 playbook, playbook_hash, session_id, git_head, cwd, argv_json,
                 selection_json, started_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self.project_id,
                run_id,
                task_id,
                data.get("agent", ""),
                data.get("runner", ""),
                data.get("mode", "session"),
                data.get("posture", "auto"),
                data.get("posture_source"),
                data.get("posture_ceiling"),
                data.get("posture_requested"),
                data.get("trigger", "manual"),
                int(data.get("caused_by", 1) or 1),
                data.get("playbook"),
                data.get("playbook_hash"),
                data.get("session_id"),
                data.get("git_head", ""),
                data.get("cwd", ""),
                json.dumps(data.get("argv") or []),
                json.dumps(data["selection"]) if data.get("selection") else None,
                _now(),
            ),
        )

    def _reindex(self, connection: sqlite3.Connection, task: Task) -> None:
        """Keep the FTS row in step with the task it describes."""
        connection.execute(
            "DELETE FROM task_fts WHERE project_id = ? AND task_id = ?",
            (self.project_id, task.id),
        )
        connection.execute(
            "INSERT INTO task_fts(task_id, project_id, title, summary, description, "
            "ball_prompt, tags) VALUES (?,?,?,?,?,?,?)",
            (
                task.id,
                self.project_id,
                task.title,
                task.spec.summary,
                task.spec.description,
                task.ball_prompt or "",
                " ".join(task.tags),
            ),
        )

    # -----------------------------------------------------------------------
    # History
    # -----------------------------------------------------------------------

    def _emit_event(
        self, connection: sqlite3.Connection, previous: Optional[Task], task: Task
    ) -> None:
        """Record what moved, with before and after, or nothing when nothing moved."""
        before = self._axes(previous)
        after = self._axes(task)
        if previous is not None and before == after:
            return

        newest = task.log[-1] if task.log else None
        kind = self._kind(before, after, previous, newest)
        actor = newest.actor if newest is not None else "system"
        timestamp = _iso(newest.ts) if newest is not None else _iso(task.updated)
        operation_id = None
        if newest is not None and isinstance(newest.data.get("operation"), dict):
            operation_id = newest.data["operation"].get("id")

        columns = [
            "project_id",
            "task_id",
            "ts",
            "actor",
            "kind",
            "log_entry_id",
            "operation_id",
            "source",
        ]
        values: List[Any] = [
            self.project_id,
            task.id,
            timestamp,
            actor,
            kind,
            newest.id if newest is not None else None,
            operation_id,
            "native",
        ]
        for stem, _ in EVENT_AXES:
            columns.extend([f"{stem}_from", f"{stem}_to"])
            values.extend([before.get(stem), after.get(stem)])
        placeholders = ",".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO task_event({','.join(columns)}) VALUES ({placeholders})", values
        )

    @staticmethod
    def _axes(task: Optional[Task]) -> Dict[str, Any]:
        """The nine history axes of a task, or all-None for one that did not exist."""
        if task is None:
            return {stem: None for stem, _ in EVENT_AXES}
        return {
            "lifecycle": str(task.lifecycle),
            "ball": _enum(task.ball),
            "ball_reason": _enum(task.ball_reason),
            "outcome": _enum(task.outcome),
            "archived": 1 if task.archived else 0,
            "priority": str(task.priority),
            "owner": task.assignment.owner,
            "position": task.queue_position,
            "parent": task.parent,
        }

    @staticmethod
    def _kind(
        before: Dict[str, Any],
        after: Dict[str, Any],
        previous: Optional[Task],
        newest: Optional[Any],
    ) -> str:
        """Name the event, from the axes that moved and the entry that accompanied it.

        Derived rather than passed in, so a caller cannot mislabel history, and so the
        manager needs no knowledge that history exists at all.
        """
        if previous is None:
            return "create"
        if after["lifecycle"] == "closed" and before["lifecycle"] != "closed":
            return "close"
        if before["lifecycle"] == "closed" and after["lifecycle"] != "closed":
            return "reopen"
        if after["lifecycle"] == "active" and before["lifecycle"] != "active":
            return "claim"
        if after["archived"] != before["archived"]:
            return "archive" if after["archived"] else "unarchive"
        entry_type = str(newest.type) if newest is not None else ""
        if entry_type == "handoff":
            return "handoff"
        if before["owner"] is not None and after["owner"] is None and after["lifecycle"] == "ready":
            return "release"
        if after["priority"] != before["priority"]:
            return "reprioritize"
        if entry_type == "queue_move" or after["position"] != before["position"]:
            return "queue_move"
        if after["ball"] != before["ball"] or after["ball_reason"] != before["ball_reason"]:
            return "handoff"
        return "update_content"

    # -----------------------------------------------------------------------
    # Creation, deletion, identity
    # -----------------------------------------------------------------------

    def generate_task_id(self) -> str:
        """The next free task id in this project.

        Reads ``seq``, which is the parsed numeric part of every id, so a hand-written
        id like ``task-047-lint-debt`` participates in the maximum instead of being
        skipped -- the file backend's glob silently ignored 158 of 240 such files.
        """
        row = (
            self._connection()
            .execute(
                "SELECT COALESCE(MAX(seq), 0) AS top FROM task WHERE project_id = ?",
                (self.project_id,),
            )
            .fetchone()
        )
        return f"task-{int(row['top']) + 1:03d}"

    def delete_task(self, task_id: str) -> bool:
        """Remove a task and everything owned by it. False when there was none."""
        task_id = self._normalised_id(task_id)
        with self.database.write() as connection:
            cursor = connection.execute(
                "DELETE FROM task WHERE project_id = ? AND task_id = ?",
                (self.project_id, task_id),
            )
            connection.execute(
                "DELETE FROM task_fts WHERE project_id = ? AND task_id = ?",
                (self.project_id, task_id),
            )
            return cursor.rowcount > 0

    @staticmethod
    def _normalised_id(task_id: str) -> str:
        """Ids reach storage with and without the historical ``.yaml`` suffix."""
        return task_id[: -len(".yaml")] if task_id.endswith(".yaml") else task_id

    # -----------------------------------------------------------------------
    # The file-shaped surface, and what replaces it
    # -----------------------------------------------------------------------

    @contextmanager
    def locked(self, task_id: str, *, timeout: Optional[float] = None) -> Iterator[None]:
        """A transaction, where the file backend took an advisory lock on one task.

        Kept by name so callers written against the file backend keep working through
        task-311's cutover. It is strictly stronger: a lock file can be orphaned by a
        process that dies, and a transaction cannot.
        """
        with self.database.write():
            yield

    @contextmanager
    def creation_lock(self, *, timeout: Optional[float] = None) -> Iterator[None]:
        """A transaction, where the file backend serialised id allocation with a lock.

        The project-wide locks collapse into the same transaction the per-task one
        does. They keep their own signatures rather than aliasing :meth:`locked`,
        because the file backend's take no task id and a caller passing none to an
        alias of a one-argument method fails at the call rather than at the lock.
        """
        with self.database.write():
            yield

    @contextmanager
    def queue_lock(self, *, timeout: Optional[float] = None) -> Iterator[None]:
        """A transaction, where the file backend serialised queue moves with a lock."""
        with self.database.write():
            yield

    def task_path(self, task_id: str) -> Path:
        """Refused: a task is rows, and there is no file to name.

        Raising beats returning a plausible path. Every caller of this in the file
        backend hands the result to git, and a path that does not exist would turn a
        clear failure into a silent one.
        """
        raise SqlStoreError(
            f"task_path({task_id!r}) has no answer under SQLite storage: task records "
            "are rows, not files. Callers that committed task files to git are retired "
            "by task-311; use `agentjobs export` for an interchange file."
        )

    _task_path = task_path

    @property
    def tasks_dir(self) -> Path:
        """Refused, for the same reason as :meth:`task_path`.

        Only queue repair asked storage for a directory, and it did so in order to read
        raw files that would not load. Under SQL a record that cannot satisfy the
        constraints is not a row at all, so there is nothing in a directory to repair.
        """
        raise SqlStoreError(
            "tasks_dir has no answer under SQLite storage: task records are rows, not "
            "files in a directory."
        )

    def has_task(self, task_id: str) -> bool:
        """True when this project holds a task with that id.

        The store-neutral replacement for ``storage._task_path(id).exists()``, which is
        how the manager used to turn a missing task into its own error type before
        reaching a mutator.
        """
        task_id = self._normalised_id(task_id)
        row = (
            self._connection()
            .execute(
                "SELECT 1 FROM task WHERE project_id = ? AND task_id = ? LIMIT 1",
                (self.project_id, task_id),
            )
            .fetchone()
        )
        return row is not None

    def load_all(self) -> Any:
        """Every task, plus the quarantined records, in the file backend's shape.

        ``LoadResult`` is what the manager, the validator and the broken-tasks endpoint
        consume, so the SQL store answers in it rather than making three callers learn a
        second shape. The mapping is exact: a row that loads is a task, and a record
        that could not become a row is an error carrying the reason it was refused.
        """
        from ..taskfiles import LoadResult, TaskLoadError

        errors = [
            TaskLoadError(
                Path(str(record.get("source_path") or record.get("task_id_guess") or "?")),
                str(record.get("error") or "quarantined at import"),
            )
            for record in self.quarantined()
        ]
        return LoadResult(tasks=self.list_tasks(), errors=errors)

    def refresh(self) -> None:
        """No-op: there is no snapshot cache to invalidate."""

    def transaction(self) -> Any:
        """Group several writes so they commit or fail together.

        The boundary's replacement for the three advisory locks. Re-entrant, so a verb
        that calls another verb joins the outer transaction instead of opening a second
        one -- see :meth:`Database.write`.
        """
        return self.database.write()

    def canonical_bytes(self, task: Task) -> bytes:
        """The task as YAML, for export and for diagnostics.

        Retained because a YAML rendering is still the interchange format and the
        thing a person reads when a record looks wrong. It is no longer what storage
        compares against, because bytes on disk are no longer the authority.
        """
        document = task.model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude={"display_status"}
        )
        return yaml.safe_dump(document, sort_keys=False, allow_unicode=False).encode("utf-8")

    # -----------------------------------------------------------------------
    # Diagnostics
    # -----------------------------------------------------------------------

    def quarantined(self) -> List[Dict[str, Any]]:
        """Legacy records that could not be loaded, with the error that stopped them.

        This replaces the file backend's raw-read escape hatch. The live tables keep
        every constraint; anything that cannot satisfy them is here instead, where it
        is still inspectable and re-importable but cannot be mistaken for a task.
        """
        return [
            dict(row)
            for row in self._connection().execute(
                "SELECT id, source_path, task_id_guess, error, imported_at FROM "
                "import_quarantine WHERE project_id = ? AND resolved_at IS NULL "
                "ORDER BY id",
                (self.project_id,),
            )
        ]

    def open_delta_reconciles(self) -> Tuple[int, int]:
        """``(sum of open_delta, count of open tasks)`` -- the history invariant.

        Named on task-273 and depended on by the analytics design (section 6, item D).
        Equality means every event that ever opened or closed a task is present and
        counted once; inequality means the history is lying and no chart built on it
        can be trusted.
        """
        connection = self._connection()
        summed = connection.execute(
            "SELECT COALESCE(SUM(open_delta), 0) AS total FROM task_event WHERE project_id = ?",
            (self.project_id,),
        ).fetchone()["total"]
        counted = connection.execute(
            "SELECT COUNT(*) AS n FROM task WHERE project_id = ? AND lifecycle <> 'closed'",
            (self.project_id,),
        ).fetchone()["n"]
        return int(summed), int(counted)


__all__ = ["SqlTaskStore", "TaskNotFound", "EVENT_AXES"]
