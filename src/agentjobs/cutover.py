"""Moving a project's tasks into the database, and moving them back out again.

The store, the importer and the backup machinery were built by task-273. This module is
the operator-facing sequence they were built for, and the reason it is one module rather
than a CLI command is that every step has to be able to run in a test.

## The sequence, and why it is that order

1.  **Preview.** The import, run against a throwaway database, reporting exactly what a
    real one would. Nothing is written anywhere that survives the call. This is where a
    malformed record, a quoted remark or a history that will not reconcile is found, and
    finding it here costs nothing.
2.  **Back up.** A ``VACUUM INTO`` snapshot of the existing database plus its manifest,
    taken under one hold of the write lock. Skipped only when there is no database yet.
3.  **Import.** One transaction. An interruption leaves nothing behind; a re-run over a
    project that already holds rows is refused unless it is told to replace them.
4.  **Verify.** Every readable file is compared against the row it became, field for
    field, and the backlog invariant is checked. This is the step that earns the word
    "verified" in the acceptance criteria: a row count agreeing with a file count proves
    almost nothing.
5.  **Record.** Only now is ``storage.yaml`` written. It is what every client reads to
    decide where to look, so flipping it before the store holds the corpus points the
    whole machine at an empty database.

Rollback runs the same list backwards, and the direction matters: the export writes the
store's **current** state, not the snapshot taken before the cutover, so changes made
after the cutover survive going back. That is the property the spec asks for by name.

## What this module will not do

It does not stop other writers. Quiescing is the operator's act -- stop the server,
stop the agents -- and the reason it is not automated is that a tool which believes it
has stopped every writer and has not is worse than one that says the operator must.
:func:`cut_over` refuses to run while it can see a live server on the project, which is
the part that *can* be checked rather than assumed.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .__version__ import __version__
from .models_v2 import Task
from .projects import Project
from .sqlstore import SqlTaskStore
from .sqlstore.backup import VerifyReport, snapshot, verify
from .sqlstore.connection import Database
from .sqlstore.importer import CorpusImporter, ImportReport
from .sqlstore.migrations import upgrade
from .storage import TaskStorage
from .storage_config import (
    StorageSettings,
    load_storage_settings,
    record_cutover,
    record_rollback,
)
from .store_factory import open_database, server_process

ATTACHMENTS_DIRNAME = "attachments"


class CutoverError(RuntimeError):
    """A step of the cutover could not be completed, and nothing was switched."""


# ----- verification ---------------------------------------------------------


@dataclass
class VerificationReport:
    """Whether what is in the store is what was on disk.

    Deliberately field-level. A count of rows agreeing with a count of files is
    consistent with every record having been silently truncated, and the whole point of
    this step is to be able to say the corpus arrived rather than that the right number
    of things did.
    """

    files: int = 0
    rows: int = 0
    missing: List[str] = field(default_factory=list)
    extra: List[str] = field(default_factory=list)
    differing: List[Tuple[str, str]] = field(default_factory=list)
    unreadable: List[Tuple[str, str]] = field(default_factory=list)
    attachments_expected: int = 0
    attachments_present: int = 0
    open_delta: int = 0
    open_rows: int = 0

    @property
    def ok(self) -> bool:
        """True when the store holds every record the directory did, unchanged."""
        return (
            not self.missing
            and not self.extra
            and not self.differing
            and self.open_delta == self.open_rows
            and self.attachments_expected == self.attachments_present
        )

    def render(self) -> str:
        """An operator-readable summary, naming everything that did not match."""
        lines = [
            f"{self.rows} rows against {self.files} readable file(s)",
            f"backlog invariant: sum(open_delta)={self.open_delta} "
            f"open_tasks={self.open_rows} "
            f"{'OK' if self.open_delta == self.open_rows else 'MISMATCH'}",
            f"attachments: {self.attachments_present}/{self.attachments_expected} blobs present",
        ]
        if self.unreadable:
            lines.append(f"{len(self.unreadable)} file(s) could not be read and were quarantined:")
            lines.extend(f"  {name}: {reason}" for name, reason in self.unreadable)
        if self.missing:
            lines.append(f"MISSING from the store: {', '.join(sorted(self.missing))}")
        if self.extra:
            lines.append(f"in the store but not on disk: {', '.join(sorted(self.extra))}")
        if self.differing:
            lines.append(f"{len(self.differing)} record(s) differ from their file:")
            lines.extend(f"  {task_id}: {detail}" for task_id, detail in self.differing)
        lines.append("VERIFIED" if self.ok else "NOT VERIFIED")
        return "\n".join(lines)


def _document(task: Task) -> Dict[str, Any]:
    """The comparable form of a task: what it holds, with nothing derived.

    ``mode="python"`` rather than ``"json"``, so a timestamp is compared as an instant
    and not as the string somebody happened to write it in. The corpus contains records
    whose ``merged_at`` carries a local offset while the store normalises everything to
    UTC on the way in (storage-sqlite.md §4) -- the same moment in two spellings, and a
    string comparison calls that data loss.
    """
    return task.model_dump(
        mode="python", by_alias=True, exclude_none=True, exclude={"display_status"}
    )


def _first_difference(left: Dict[str, Any], right: Dict[str, Any]) -> str:
    """Name one field that differs, so the report says what to look at.

    One rather than all: a record that differs usually differs in one place, and a
    report that dumps two whole documents per mismatch is one nobody reads.
    """
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            if key == "log":
                return (
                    f"log: {len(left.get('log') or [])} entries on disk, "
                    f"{len(right.get('log') or [])} in the store"
                )
            return f"{key}: {left.get(key)!r} on disk, {right.get(key)!r} in the store"
    return "documents differ but no field does"  # pragma: no cover - defensive


def verify_import(store: SqlTaskStore, tasks_dir: Path) -> VerificationReport:
    """Compare every task file against the row it became.

    Reads the directory directly rather than through ``TaskStorage`` so that a file the
    model refuses is *reported* rather than dropped: a record that could not be imported
    and a record that was never there look identical from a listing.
    """
    report = VerificationReport()
    on_disk: Dict[str, Dict[str, Any]] = {}

    for path in sorted(Path(tasks_dir).glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            task = Task.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - the file is the suspect
            report.unreadable.append((path.name, str(exc)[:160]))
            continue
        report.files += 1
        on_disk[task.id] = _document(task)
        for entry in task.log:
            report.attachments_expected += len(entry.attachments or [])

    in_store = {task.id: _document(task) for task in store.list_tasks()}
    report.rows = len(in_store)
    report.missing = [task_id for task_id in on_disk if task_id not in in_store]
    report.extra = [task_id for task_id in in_store if task_id not in on_disk]
    for task_id, document in on_disk.items():
        stored = in_store.get(task_id)
        if stored is not None and stored != document:
            report.differing.append((task_id, _first_difference(document, stored)))

    for task in store.list_tasks():
        for entry in task.log:
            for attachment in entry.attachments or []:
                if store.attachments.has(attachment.sha256):
                    report.attachments_present += 1

    report.open_delta, report.open_rows = store.open_delta_reconciles()
    return report


# ----- the steps ------------------------------------------------------------


@dataclass
class CutoverResult:
    """Everything one cutover produced, for a caller that has to print it."""

    project_id: str
    imported: ImportReport
    verified: VerificationReport
    backup: Optional[Path] = None
    database: Optional[Path] = None
    recorded: bool = False

    @property
    def ok(self) -> bool:
        """True when the project is now served from the database."""
        return self.recorded and self.verified.ok


def preview(
    project: Project,
    *,
    tasks_dir: Optional[Path] = None,
    backfill_git: bool = False,
    enforce_quotation_policy: bool = True,
) -> Tuple[ImportReport, VerificationReport]:
    """Run the whole import against a throwaway database and report what happened.

    The dry run is the real import, which is the only kind worth having: a separate
    "check" implementation would drift from the code it is meant to predict, and the
    interesting failures -- a constraint the corpus violates, a history that will not
    reconcile -- are exactly the ones only the real path finds.
    """
    source = Path(tasks_dir) if tasks_dir is not None else project.tasks_dir()
    scratch = Path(tempfile.mkdtemp(prefix="agentjobs-preview-"))
    database = Database(scratch / "preview.db")
    try:
        upgrade(database, agentjobs_version=__version__, snapshot_before=False)
        store = SqlTaskStore(database, project.id)
        store.ensure_project(root=str(project.root))
        imported = CorpusImporter(store, source).run(
            backfill_git=backfill_git,
            enforce_quotation_policy=enforce_quotation_policy,
        )
        return imported, verify_import(store, source)
    finally:
        database.close()
        shutil.rmtree(scratch, ignore_errors=True)


def back_up(
    settings: Optional[StorageSettings] = None, *, destination: Optional[Path] = None
) -> Optional[Path]:
    """Snapshot the database and write the manifest describing it.

    ``None`` when there is no database yet, which is the ordinary state of the first
    cutover on a machine. Both files are written under one hold of the write lock, so
    the manifest cannot describe a database one row ahead of the file beside it.
    """
    resolved = settings or load_storage_settings()
    if not resolved.database.exists():
        return None
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = (
        Path(destination)
        if destination is not None
        else resolved.database.with_name(
            f"{resolved.database.stem}.backup.{stamp}{resolved.database.suffix}"
        )
    )
    with server_process():
        database = open_database(resolved.database)
        written = snapshot(database, target)
    return written


def import_project(
    project: Project,
    *,
    settings: Optional[StorageSettings] = None,
    tasks_dir: Optional[Path] = None,
    replace: bool = False,
    backfill_git: bool = False,
    enforce_quotation_policy: bool = True,
    reporting_tz: str = "UTC",
) -> Tuple[ImportReport, VerificationReport]:
    """Import one project's directory into the machine's database, then verify it.

    Does **not** switch the project over: importing and switching are separate acts so
    that a verified store can sit beside a still-authoritative directory while somebody
    reads the report.
    """
    resolved = settings or load_storage_settings()
    source = Path(tasks_dir) if tasks_dir is not None else project.tasks_dir()
    with server_process():
        database = open_database(resolved.database)
        store = SqlTaskStore(database, project.id)
        store.ensure_project(root=str(project.root), reporting_tz=reporting_tz)
        imported = CorpusImporter(store, source).run(
            reporting_tz=reporting_tz,
            backfill_git=backfill_git,
            enforce_quotation_policy=enforce_quotation_policy,
            replace=replace,
        )
        return imported, verify_import(store, source)


def cut_over(
    project: Project,
    *,
    settings: Optional[StorageSettings] = None,
    tasks_dir: Optional[Path] = None,
    replace: bool = False,
    backfill_git: bool = True,
    enforce_quotation_policy: bool = True,
    reporting_tz: str = "UTC",
    skip_backup: bool = False,
) -> CutoverResult:
    """Back up, import, verify, and only then make the database authoritative.

    Refuses to record the switch on a verification that did not pass. The store is left
    holding the import either way, which is deliberate: the failure is then inspectable
    with ordinary queries, and the project is still served from its files while somebody
    looks at it.

    ``backfill_git`` defaults to **on** here and off everywhere else. The backfill reads
    the git history of the task files, so it has to happen before those files are
    retired -- and the cutover is the last moment anybody is looking. Retire first and
    the evidence is gone permanently (storage-sqlite.md §4).
    """
    resolved = settings or load_storage_settings()
    backup = None if skip_backup else back_up(resolved)

    imported, verified = import_project(
        project,
        settings=resolved,
        tasks_dir=tasks_dir,
        replace=replace,
        backfill_git=backfill_git,
        enforce_quotation_policy=enforce_quotation_policy,
        reporting_tz=reporting_tz,
    )

    result = CutoverResult(
        project_id=project.id,
        imported=imported,
        verified=verified,
        backup=backup,
        database=resolved.database,
    )
    if not verified.ok:
        return result

    source = Path(tasks_dir) if tasks_dir is not None else project.tasks_dir()
    record_cutover(project.id, source, home=resolved.home)
    result.recorded = True
    return result


# ----- getting back out -----------------------------------------------------


@dataclass
class ExportReport:
    """What an export wrote, and where."""

    destination: Path
    tasks: int = 0
    attachments: int = 0
    written: List[str] = field(default_factory=list)

    def render(self) -> str:
        """An operator-readable summary."""
        return (
            f"wrote {self.tasks} task file(s) and {self.attachments} attachment(s) "
            f"to {self.destination}"
        )


def export_project(
    project: Project,
    destination: Path,
    *,
    settings: Optional[StorageSettings] = None,
) -> ExportReport:
    """Write the store's current state out as task YAML, with its attachment bytes.

    This is the interchange artifact, and the spec is explicit that it is a thing a
    person asks for rather than a mirror something maintains: nothing calls this on a
    write, and nothing commits what it produces.

    It is also the mechanism rollback is built on, which is why the filenames and the
    attachment layout are byte-compatible with what the file backend produced. A
    directory this wrote is a directory ``TaskStorage`` can serve.
    """
    resolved = settings or load_storage_settings()
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=True)
    report = ExportReport(destination=target)

    with server_process():
        database = open_database(resolved.database)
        store = SqlTaskStore(database, project.id)
        for task in store.list_tasks():
            path = target / f"{task.id}.yaml"
            path.write_bytes(store.canonical_bytes(task))
            report.tasks += 1
            report.written.append(path.name)
            for entry in task.log:
                for attachment in entry.attachments or []:
                    blob = target / attachment.path
                    blob.parent.mkdir(parents=True, exist_ok=True)
                    blob.write_bytes(store.attachments.read(attachment))
                    report.attachments += 1
    return report


def roll_back(
    project: Project,
    *,
    settings: Optional[StorageSettings] = None,
    tasks_dir: Optional[Path] = None,
) -> ExportReport:
    """Put the project back on its files, keeping everything written since the cutover.

    The export runs first and against the store's **current** state, so a handoff made
    after the cutover is on disk before anything is switched. Exporting the pre-cutover
    snapshot instead would be a rollback that silently discarded a day's work, which is
    the failure this ordering exists to prevent.

    The database is not deleted. A rollback is a decision that can itself be wrong, and
    the store is the only copy of the reconstructed and backfilled history.
    """
    resolved = settings or load_storage_settings()
    entry = resolved.for_project(project.id)
    destination = (
        Path(tasks_dir)
        if tasks_dir is not None
        else Path(entry.source)
        if entry.source
        else project.tasks_dir()
    )
    report = export_project(project, destination, settings=resolved)
    record_rollback(project.id, home=resolved.home)
    return report


# ----- what an operator asks first ------------------------------------------


@dataclass
class ProjectStatus:
    """One line of ``agentjobs storage status``."""

    project_id: str
    backend: str
    cutover_at: Optional[str]
    source: Optional[str]
    rows: Optional[int]
    files: Optional[int]


def status(
    projects: List[Project], *, settings: Optional[StorageSettings] = None
) -> List[ProjectStatus]:
    """Where each project's tasks actually are, counted rather than assumed.

    Both counts are reported for every project, whichever backend is authoritative,
    because the interesting states are the asymmetric ones: rows and no files means the
    retirement step has run, files and no rows means a cutover was never done, and both
    means a migrated project whose old directory is still on disk.
    """
    resolved = settings or load_storage_settings()
    lines: List[ProjectStatus] = []
    database: Optional[Database] = None
    if resolved.database.exists():
        with server_process():
            database = open_database(resolved.database)

    for project in projects:
        entry = resolved.for_project(project.id)
        rows: Optional[int] = None
        if database is not None:
            with server_process():
                rows = len(SqlTaskStore(database, project.id).list_tasks())
        try:
            files: Optional[int] = len(list(project.tasks_dir().glob("*.yaml")))
        except Exception:  # noqa: BLE001 - an unreadable directory is a fair answer here
            files = None
        lines.append(
            ProjectStatus(
                project_id=project.id,
                backend=entry.backend,
                cutover_at=entry.cutover_at,
                source=entry.source,
                rows=rows,
                files=files,
            )
        )
    return lines


def verify_backup(path: Path, *, settings: Optional[StorageSettings] = None) -> VerifyReport:
    """Open a snapshot read-only and check it is restorable. Never touches the live store."""
    del settings
    return verify(Path(path))


def file_store_for(project: Project, *, tasks_dir: Optional[Path] = None) -> TaskStorage:
    """The file backend for a project, whatever the machine's configuration says.

    Used by the rollback path and by the tests, both of which legitimately need to read
    the directory of a project that is currently served from the database.
    """
    return TaskStorage(Path(tasks_dir) if tasks_dir is not None else project.tasks_dir())


__all__ = [
    "CutoverError",
    "CutoverResult",
    "ExportReport",
    "ProjectStatus",
    "VerificationReport",
    "back_up",
    "cut_over",
    "export_project",
    "file_store_for",
    "import_project",
    "preview",
    "roll_back",
    "status",
    "verify_backup",
    "verify_import",
]
