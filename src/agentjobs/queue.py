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

This module holds the queue's arithmetic and its rules; it writes nothing and knows
nothing about locks. The verbs that apply what it plans -- move, reprioritize,
rebalance, compaction, repair and selection -- live in :mod:`agentjobs.manager`, which
takes the project queue lock around every one of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Collection, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models_v2 import PRIORITY_RANK, Priority, Task
from .storage import TaskStorage, load_yaml

__all__ = [
    "QUEUE_STEP",
    "REPAIR_COMMAND",
    "BandEntry",
    "Placement",
    "QueueAssignment",
    "QueueCorruptionError",
    "QueueMigrationReport",
    "QueuePlace",
    "QueueProblem",
    "QueueRecord",
    "RenumberPass",
    "band_entries",
    "bands_at_or_above",
    "baseline_key",
    "find_queue_problems",
    "next_position",
    "order_key",
    "place_of",
    "placement_from",
    "plan_compaction",
    "plan_insertion",
    "plan_queue_migration",
    "plan_rebalance",
    "plan_renumber",
    "read_queue_record",
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


#: What the error message tells a reader to type. Named here rather than spelled out
#: at each raise, so the CLI verb and the messages quoting it cannot drift apart.
REPAIR_COMMAND = "agentjobs queue repair"


def bands_at_or_above(rank: int) -> Set[str]:
    """The names of every band at least as urgent as ``rank``.

    The scope of the selection-time integrity check (design section 8): the winning
    band and the bands above it, which is exactly the set an answer and its explanation
    read. Wider would make every selection hostage to corruption it never consulted;
    narrower would let ``explain_next`` assert an order over tasks it had not checked.
    """
    return {priority.value for priority, band in PRIORITY_RANK.items() if band <= rank}


def order_key(task: Task) -> Tuple[int, int]:
    """``(band, place)`` -- the total order over open work, and the only one.

    Consistency rule 6 guarantees an open task has a position, so a task reaching this
    function without one did not come off disk. Raising beats defaulting: a fallback of
    ``0`` would silently put it first, which is the class of bug task-081 exists to end.
    """
    position = task.queue_position
    if position is None:  # pragma: no cover - rule 6 refuses to load such a task
        raise QueueCorruptionError([QueueProblem("missing", task.priority.value, (task.id,), None)])
    return (task.priority_rank(), position)


# ---------------------------------------------------------------------------
# Placement: where a task is being asked to go
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Placement:
    """Where in its band a task is being put. One of four kinds, one decision."""

    kind: str
    target: Optional[str] = None

    BOTTOM: ClassVar[str] = "bottom"
    TOP: ClassVar[str] = "top"
    BEFORE: ClassVar[str] = "before"
    AFTER: ClassVar[str] = "after"

    def describe(self) -> str:
        """A phrase for a log entry body, e.g. ``the top of the band``."""
        if self.kind in (Placement.BEFORE, Placement.AFTER):
            return f"{self.kind} {self.target}"
        return f"the {self.kind} of the band"

    def as_data(self) -> Dict[str, object]:
        """The ``placement`` mapping stored in a ``queue_move`` entry's data."""
        data: Dict[str, object] = {"kind": self.kind}
        if self.target is not None:
            data["target"] = self.target
        return data


def placement_from(
    *,
    before: Optional[str] = None,
    after: Optional[str] = None,
    top: bool = False,
    bottom: bool = False,
) -> Placement:
    """Turn the four caller-facing flags into one :class:`Placement`.

    Exactly one, because "before task-063 and also at the top" is not a placement -- it
    is two answers to one question, and picking one of them on the caller's behalf is
    how a reorder ends up somewhere nobody asked for.
    """
    chosen = [
        name
        for name, given in (
            ("before", before is not None),
            ("after", after is not None),
            ("top", top),
            ("bottom", bottom),
        )
        if given
    ]
    if len(chosen) != 1:
        given = ", ".join(chosen) if chosen else "none"
        raise ValueError(
            "exactly one placement is required -- before=, after=, top= or bottom= "
            f"(given: {given})"
        )
    if before is not None:
        return Placement(Placement.BEFORE, before)
    if after is not None:
        return Placement(Placement.AFTER, after)
    return Placement(Placement.TOP if top else Placement.BOTTOM)


# ---------------------------------------------------------------------------
# A band, and the arithmetic of inserting into one
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BandEntry:
    """One open task's place in one band: its id and its number."""

    task_id: str
    position: int


def band_entries(
    tasks: Iterable[Task],
    priority: Priority,
    *,
    excluding: Iterable[str] = (),
) -> List[BandEntry]:
    """The open members of one band, in order, optionally without some of them.

    ``excluding`` is how a move ignores the tasks it is itself moving. A mover's own
    current number must not count as an occupied neighbour, or shifting it one place
    would compute the gap it is already sitting in.
    """
    skip = set(excluding)
    entries = [
        BandEntry(task.id, task.queue_position)
        for task in tasks
        if task.is_open
        and task.priority is priority
        and task.id not in skip
        and task.queue_position is not None
    ]
    entries.sort(key=lambda entry: (entry.position, entry.task_id))
    return entries


def _index_of(entries: Sequence[BandEntry], task_id: str) -> int:
    for index, entry in enumerate(entries):
        if entry.task_id == task_id:
            return index
    raise ValueError(
        f"'{task_id}' is not an open task in this band, so nothing can be placed " "relative to it"
    )


def _fill(low: int, high: int, count: int) -> Optional[List[int]]:
    """``count`` evenly spaced integers strictly between ``low`` and ``high``.

    None when the gap cannot hold them -- the caller's cue to rebalance the band and
    ask once more (design section 4). ``low`` is 0 for "above everything", which makes
    the top of a band the same arithmetic as anywhere else in it.
    """
    step = (high - low) // (count + 1)
    if step < 1:
        return None
    return [low + step * offset for offset in range(1, count + 1)]


def plan_insertion(
    entries: Sequence[BandEntry],
    placement: Placement,
    *,
    count: int = 1,
) -> Optional[List[int]]:
    """The numbers ``count`` tasks must take to land at ``placement`` in this band.

    ``entries`` must already exclude the tasks being moved. Returns None when the chosen
    gap is exhausted; the caller rebalances and asks again, which is the one retry
    design section 4 allows.

    ``count`` above one is a group move: the tasks land contiguously, evenly spaced in
    the gap, keeping the order they were handed in.
    """
    if count < 1:
        raise ValueError("a placement must move at least one task")

    if placement.kind == Placement.BOTTOM:
        base = entries[-1].position if entries else 0
        return [base + QUEUE_STEP * offset for offset in range(1, count + 1)]

    if placement.kind == Placement.TOP:
        if not entries:
            return [QUEUE_STEP * offset for offset in range(1, count + 1)]
        return _fill(0, entries[0].position, count)

    if placement.kind == Placement.BEFORE:
        index = _index_of(entries, str(placement.target))
        low = entries[index - 1].position if index > 0 else 0
        return _fill(low, entries[index].position, count)

    if placement.kind == Placement.AFTER:
        index = _index_of(entries, str(placement.target))
        low = entries[index].position
        if index + 1 == len(entries):
            return [low + QUEUE_STEP * offset for offset in range(1, count + 1)]
        return _fill(low, entries[index + 1].position, count)

    raise ValueError(f"unknown placement kind '{placement.kind}'")


# ---------------------------------------------------------------------------
# Renumbering, and the direction rule that makes it safe (design section 6)
# ---------------------------------------------------------------------------

#: One renumbering pass: the writes to make, in the order they must be made.
RenumberPass = List[Tuple[str, int]]


def plan_renumber(entries: Sequence[BandEntry], targets: Sequence[int]) -> List[RenumberPass]:
    """Order the writes so that every partial application is still a valid queue.

    A multi-file write in this corpus is not atomic: a crash, a kill, or simply another
    process reading the directory can observe the band halfway through. The rule the
    design states (section 6) is that the band read from disk at *any* instant is a
    valid queue in the same order it had before. Direction is what buys that without a
    transaction:

    * **Upward** -- every target above every current number -- is applied **tail
      first**. At each step the suffix that has moved sits above everything else in its
      original relative order, and the prefix that has not still sits below all of it.
    * **Downward** is applied **head first**, by the mirror argument.
    * **Anything else** is two passes: up into a free range above both sets, then down
      onto the targets. Each pass is then strictly directional by construction.

    ``entries`` must be in queue order and ``targets`` ascending and the same length. A
    renumber that changes nothing returns no passes at all.
    """
    if len(entries) != len(targets):
        raise ValueError(
            f"the band has {len(entries)} open tasks but {len(targets)} targets were "
            "planned; a renumber moves every member or none"
        )
    if not entries:
        return []
    if list(targets) != sorted(targets) or len(set(targets)) != len(targets):
        raise ValueError("renumber targets must be strictly ascending and distinct")

    current = [entry.position for entry in entries]
    if current == list(targets):
        return []

    ids = [entry.task_id for entry in entries]
    final: RenumberPass = list(zip(ids, targets))

    if min(targets) > max(current):
        return [list(reversed(final))]
    if max(targets) < min(current):
        return [final]

    base = max(max(current), max(targets))
    staging = [base + QUEUE_STEP * offset for offset in range(1, len(ids) + 1)]
    return [list(reversed(list(zip(ids, staging)))), final]


def plan_rebalance(entries: Sequence[BandEntry]) -> List[RenumberPass]:
    """Restore usable spacing without changing anybody's place.

    Targets start above the current maximum, so a rebalance is always the upward form
    and therefore always a single tail-first pass. Numbers creep upward over a band's
    life as a result; compaction is the cosmetic answer to that, and nothing depends on
    the magnitude.
    """
    if not entries:
        return []
    base = max(entry.position for entry in entries)
    targets = [base + QUEUE_STEP * offset for offset in range(1, len(entries) + 1)]
    return plan_renumber(entries, targets)


def plan_compaction(entries: Sequence[BandEntry]) -> List[RenumberPass]:
    """Renumber a band back to ``100, 200, 300, ...``. Explicit only, and cosmetic."""
    targets = [QUEUE_STEP * offset for offset in range(1, len(entries) + 1)]
    return plan_renumber(entries, targets)


# ---------------------------------------------------------------------------
# Corruption is loud (design section 8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueuePlace:
    """One open task's claim on a place in line, from a Task or from a raw file.

    Deliberately three fields and no more. Integrity is a statement about ids, bands
    and numbers, and a structure that also carried timestamps would invite a tie-break
    -- which is the one thing selection may never do.
    """

    task_id: str
    band: str
    position: Optional[int]


def place_of(task: Task) -> QueuePlace:
    """The claim an in-memory task makes on its band."""
    return QueuePlace(task.id, task.priority.value, task.queue_position)


@dataclass(frozen=True)
class QueueProblem:
    """One broken queue rule, named well enough to fix by hand if you have to."""

    kind: str
    band: str
    task_ids: Tuple[str, ...]
    position: Optional[int]

    def render(self) -> str:
        names = ", ".join(self.task_ids)
        if self.kind == "duplicate":
            return f"band '{self.band}' position {self.position} is claimed by {names}"
        if self.kind == "missing":
            return f"{names} is open in band '{self.band}' with no queue_position"
        return f"{names} has queue_position {self.position!r} in band '{self.band}'"


class QueueCorruptionError(RuntimeError):
    """Selection was asked for an answer it cannot honestly give.

    Never swallowed and never fallen back from. A queue that quietly answers while
    corrupt trains everybody to ignore corruption, and the failure it produces --
    silently working the wrong task -- leaves no trace at all. Refusing is the point,
    and it is the same argument as the source-root check that refuses to start a server
    importing another checkout's code.
    """

    def __init__(self, problems: Sequence[QueueProblem]) -> None:
        self.problems: Tuple[QueueProblem, ...] = tuple(problems)
        detail = "; ".join(problem.render() for problem in self.problems)
        super().__init__(
            "the queue is broken and selection will not guess an order: "
            f"{detail}. Repair it with: {REPAIR_COMMAND}"
        )


def find_queue_problems(
    places: Iterable[QueuePlace],
    *,
    bands: Optional[Collection[str]] = None,
) -> List[QueueProblem]:
    """Every queue rule these places break. Findings, never an exception.

    ``bands`` limits the scope to the bands named; None checks every one of them.
    Selection passes the winning band and the bands above it -- the set its answer and
    its explanation actually read -- because a duplicate in ``low`` does not falsify the
    claim that a particular ``high`` task is next, and making every selection hostage to
    corruption in a band it never reads would punish the wrong caller.
    """
    problems: List[QueueProblem] = []
    claims: Dict[str, Dict[int, List[str]]] = {}

    for place in places:
        if bands is not None and place.band not in bands:
            continue
        if place.position is None:
            problems.append(QueueProblem("missing", place.band, (place.task_id,), None))
            continue
        if place.position < 1:
            problems.append(
                QueueProblem("not-positive", place.band, (place.task_id,), place.position)
            )
            continue
        claims.setdefault(place.band, {}).setdefault(place.position, []).append(place.task_id)

    for band in sorted(claims):
        for position, ids in sorted(claims[band].items()):
            if len(ids) > 1:
                problems.append(QueueProblem("duplicate", band, tuple(sorted(ids)), position))

    problems.sort(key=lambda problem: (problem.band, problem.kind, problem.task_ids))
    return problems


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
        record = read_queue_record(path)
        if record is None:
            skipped.append(path.name)
        else:
            records.append(record)
    return records, skipped


def read_queue_record(path: Path) -> Optional[QueueRecord]:
    """One task file as a :class:`QueueRecord`, or None when it cannot yield one.

    Raw, because that is the only way to read the files that matter most here: a task
    whose ``queue_position`` is missing, duplicated or negative is often one the loader
    refuses outright, and a check that could only see loadable files could not see the
    corpus it exists to describe.

    An out-of-range integer is carried through verbatim so the caller can report it as
    the wrong thing it is; only a value that is not an integer at all reads as absent.
    ``bool`` is an ``int`` in Python, so ``queue_position: true`` would otherwise be
    carried as position 1 and pass every check.
    """
    try:
        raw = load_yaml(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    task_id, created = raw.get("id"), raw.get("created")
    lifecycle = raw.get("lifecycle")
    if not isinstance(task_id, str) or not isinstance(lifecycle, str) or created is None:
        return None
    position = raw.get("queue_position")
    numbered = isinstance(position, int) and not isinstance(position, bool)
    return QueueRecord(
        task_id=task_id,
        created=str(created),
        priority=str(raw.get("priority") or Priority.MEDIUM.value),
        is_open=lifecycle != "closed",
        queue_position=position if numbered else None,
    )


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
    storage = TaskStorage(directory)
    # Migration assigns positions, so it is a queue-lock holder like every other path
    # that does (design section 7). The plan is taken *inside* the lock rather than
    # before it: a create landing between the read and the writes would take a number
    # this plan is about to hand to somebody else.
    with storage.queue_lock():
        records, skipped = read_queue_records(directory)
        report = plan_queue_migration(records)
        report.unreadable = skipped
        if not write:
            return report

        for assignment in report.assignments:
            path = directory / f"{assignment.task_id}.yaml"
            raw = load_yaml(path.read_text(encoding="utf-8"))
            raw["queue_position"] = assignment.position
            storage.save_task(Task.model_validate(raw))
        report.written = True
        return report
