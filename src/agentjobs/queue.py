"""Queue position: where an open task stands in line inside its priority band.

Implements sections 3, 4 and 15 (step 1) of ``docs/task-selection-design.md``.

``priority`` is the urgency band; ``queue_position`` is the authoritative order
*within* that band. Together they are a total order over open work. The number is
order and nothing else -- it is not a score, not an estimate, and it carries no
meaning across bands: ``high/900`` is ahead of ``medium/100`` because of the band,
not because 900 beats 100.

Every ordering decision here reads only **immutable** fields: ``created``, ``id``,
``priority``, ``lifecycle``. ``updated`` appears nowhere, deliberately -- a queue
that consults it is a queue that reorders itself when somebody logs progress, which
is the failure task-081 exists to end.

Scope note: this module is the foundation, not the whole design. The reorder verbs,
the project queue lock, rebalancing and ``get_next_task`` are task-205.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models_v2 import Priority, Task
from .storage import TaskStorage, load_yaml

__all__ = [
    "QUEUE_STEP",
    "QueueAssignment",
    "QueueMigrationReport",
    "QueueRecord",
    "baseline_key",
    "next_position",
    "plan_queue_migration",
    "read_queue_records",
    "migrate_queue_positions",
]

#: The gap left between neighbours. Sparse numbering is what makes a reorder a
#: one-file diff instead of a whole-band rewrite: an insertion between two tasks
#: takes the midpoint and nobody else's file changes. About six insertions fit
#: between one original pair before the band needs rebalancing (task-205).
QUEUE_STEP = 100


def _open_in_band(tasks: Iterable[Task], priority: Priority) -> List[Task]:
    """Every open task in one band. A closed task is not in line at all."""
    return [task for task in tasks if task.is_open and task.priority is priority]


def next_position(tasks: Iterable[Task], priority: Priority) -> int:
    """The position a task joining ``priority`` should take: the bottom of the band.

    ``max(band) + QUEUE_STEP``, or ``QUEUE_STEP`` when the band is empty. A task
    nobody has placed does not get to preempt an order somebody thought about
    (design section 5.1).
    """
    occupied = [
        task.queue_position
        for task in _open_in_band(tasks, priority)
        if task.queue_position is not None
    ]
    return (max(occupied) if occupied else 0) + QUEUE_STEP


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueRecord:
    """The four fields the baseline needs, read straight out of a task file.

    **Deliberately not a** :class:`~agentjobs.models_v2.Task`. This migration exists
    precisely because the corpus has no positions yet, and consistency rule 6 refuses
    to load an open task without one -- so a migration built on ``TaskStorage.load_all``
    could not read a single one of the files it was written to fix. Reading the raw
    mapping is the same shape :mod:`agentjobs.migrate_schema` uses to convert v1 files
    that v2 cannot load either.

    ``created`` is kept as the raw string. It is only ever compared with other strings
    out of the same corpus, all of which the model wrote in ISO-8601, so ordering by it
    is ordering by time -- and not parsing it means a file with an odd timestamp still
    sorts somewhere deterministic instead of aborting the run.
    """

    task_id: str
    created: str
    priority: str
    is_open: bool
    queue_position: Optional[int]


def baseline_key(record: QueueRecord) -> Tuple[str, str]:
    """Sort key for the deterministic baseline: ``created`` ascending, then id (D10).

    Both halves are immutable, which is the whole point. The id breaks the tie when
    two tasks were created in the same instant, so the plan never depends on the order
    the directory happened to be read in.
    """
    return (record.created, record.task_id)


@dataclass(frozen=True)
class QueueAssignment:
    """One position the migration gives one task."""

    task_id: str
    band: str
    position: int

    def render(self) -> str:
        return f"  {self.task_id}: {self.band} -> {self.position}"


@dataclass
class QueueMigrationReport:
    """What a queue migration did, dry run or otherwise."""

    assignments: List[QueueAssignment] = field(default_factory=list)
    already_positioned: List[str] = field(default_factory=list)
    closed: List[str] = field(default_factory=list)
    unreadable: List[str] = field(default_factory=list)
    written: bool = False

    @property
    def changed(self) -> bool:
        """Whether this migration has anything to do. False means a no-op run."""
        return bool(self.assignments)

    def positions(self) -> Dict[str, int]:
        """The plan as a mapping, for tests and for callers that want the answer."""
        return {item.task_id: item.position for item in self.assignments}

    def render(self) -> str:
        """A human-readable summary, printed by whoever runs the migration."""
        lines = [
            f"Open tasks positioned:   {len(self.assignments)}",
            f"Already positioned:      {len(self.already_positioned)}",
            f"Closed (no position):    {len(self.closed)}",
            f"Unreadable (skipped):    {len(self.unreadable)}",
            f"Written to disk:         {'yes' if self.written else 'NO (dry run)'}",
        ]
        if self.unreadable:
            lines += ["", "SKIPPED"]
            lines += [f"  {name}" for name in self.unreadable]
        if self.assignments:
            lines += ["", "ASSIGNED"]
            lines += [item.render() for item in self.assignments]
        return "\n".join(lines)


def read_queue_records(tasks_dir: Path) -> Tuple[List[QueueRecord], List[str]]:
    """Read every task file in a directory as a :class:`QueueRecord`.

    Returns the records, and the names of the files it could not make one from. A
    file that is not a mapping, or that has no id, no ``created`` or no ``lifecycle``,
    is skipped and named rather than guessed at: the migration writes to real files,
    and an invented ordering key is worse than a gap somebody has to look at.
    """
    records: List[QueueRecord] = []
    skipped: List[str] = []
    for path in sorted(Path(tasks_dir).glob("*.yaml")):
        try:
            raw = load_yaml(path.read_text(encoding="utf-8"))
        except Exception:
            skipped.append(path.name)
            continue
        if not isinstance(raw, dict):
            skipped.append(path.name)
            continue
        task_id, created = raw.get("id"), raw.get("created")
        lifecycle = raw.get("lifecycle")
        if not isinstance(task_id, str) or not isinstance(lifecycle, str) or created is None:
            skipped.append(path.name)
            continue
        position = raw.get("queue_position")
        records.append(
            QueueRecord(
                task_id=task_id,
                created=str(created),
                priority=str(raw.get("priority") or Priority.MEDIUM.value),
                is_open=lifecycle != "closed",
                queue_position=position if isinstance(position, int) else None,
            )
        )
    return records, skipped


def plan_queue_migration(records: Sequence[QueueRecord]) -> QueueMigrationReport:
    """Plan the section 15 step-1 baseline. Pure: reads records, writes nothing.

    Within each band, open tasks that have no position are ordered by ``created``
    then id and assigned ``100, 200, 300, ...`` below whatever is already there.

    That one rule gives both of the properties the task asks for:

    * **Idempotent.** A task that already has a position keeps it, so a second run
      over a positioned corpus plans nothing at all and ``changed`` is False.
    * **Deterministic.** The plan is a function of ``created``, ``id``, ``priority``
      and ``lifecycle`` only. Two runs over the same unpositioned corpus produce the
      same numbers, and rewriting every ``updated`` in the corpus changes nothing.

    On a corpus where no open task has a position -- the live case this was written
    for -- "below whatever is already there" starts from zero, so each band reads
    exactly ``100, 200, 300, ...``.
    """
    report = QueueMigrationReport()
    for record in records:
        if not record.is_open:
            report.closed.append(record.task_id)
        elif record.queue_position is not None:
            report.already_positioned.append(record.task_id)

    bands: Dict[str, List[QueueRecord]] = {}
    for record in records:
        if record.is_open:
            bands.setdefault(record.priority, []).append(record)

    # Planned band by band in name order. Bands are independent -- positions are never
    # compared across one -- so this changes only the order of the report, never a
    # number. It is sorted anyway so that two runs produce identical output, not merely
    # identical assignments.
    for band in sorted(bands):
        members = bands[band]
        unplaced = sorted(
            (record for record in members if record.queue_position is None),
            key=baseline_key,
        )
        if not unplaced:
            continue
        taken = [r.queue_position for r in members if r.queue_position is not None]
        cursor = max(taken) if taken else 0
        for record in unplaced:
            cursor += QUEUE_STEP
            report.assignments.append(
                QueueAssignment(task_id=record.task_id, band=band, position=cursor)
            )
    return report


def migrate_queue_positions(tasks_dir: Path, *, write: bool = False) -> QueueMigrationReport:
    """Apply the baseline to one project's corpus.

    Nothing is written unless ``write`` is true, so the plan can be read before it is
    trusted -- the same shape as :func:`agentjobs.migrate_schema.migrate_corpus`.

    Writes go through ``TaskStorage.save_task`` rather than editing the YAML in place,
    so every file comes out in canonical form with a write receipt behind it. A
    migration that hand-shaped the YAML would leave each file it touched looking
    hand-edited to ``agentjobs validate``, which is exactly what receipts exist to
    detect.

    That also bumps each migrated file's ``updated``, which is correct: this *is* a
    real write. The rule the design states is about the migration's **inputs** --
    ``updated`` is not one of them, and no assignment above depends on it.
    """
    directory = Path(tasks_dir)
    records, skipped = read_queue_records(directory)
    report = plan_queue_migration(records)
    report.unreadable = skipped
    if not write:
        return report

    storage = TaskStorage(directory)
    for assignment in report.assignments:
        path = directory / f"{assignment.task_id}.yaml"
        raw = load_yaml(path.read_text(encoding="utf-8"))
        raw["queue_position"] = assignment.position
        storage.save_task(Task.model_validate(raw))
    report.written = True
    return report
