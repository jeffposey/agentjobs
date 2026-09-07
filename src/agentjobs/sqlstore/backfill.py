"""Recover, from git, the field history the task log never wrote down.

The log records *that* a content update changed ``priority`` and never what it changed
from. Measured on task-273: replaying the log leaves essentially every task unable to
say what its priority was last month. Git can say, because the task files have been
committed since 2025-10-26 and a diff carries both sides of every line.

Two rules govern this, both measured on task-213 and both load-bearing. They are stated
here next to the code that implements them because getting either wrong produces a chart
that is wrong without being obviously broken.

**Rule A -- clamp a backfilled creation to the earliest evidence.** Git records when a
change was *committed*, which postdates it: three tasks' logs record a close twenty hours
before git first sees the file. Taking commit time literally opens the backlog series at
**-3**, which is impossible and undiagnosable. A backfilled creation is therefore
timestamped ``min(commit time, the record's own created, its first log entry)``.

**Rule B -- backfill only the axes the log does not record.** Lifecycle and outcome are
already in the log with their true timestamps, and git records the same transitions again
at commit time. Backfilling them double-counts every close: measured at ``SUM(open_delta)
= 69`` against 125 open tasks, a 45% error with no hint in any query plan. So git is
authoritative for ``priority``, ``parent`` and ``archived``; ``queue_position`` takes
both, de-duplicated against a native move of the same axis within five minutes; and
``lifecycle`` and ``outcome`` are never taken from git at all.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

#: Axes git may speak for. Deliberately not lifecycle or outcome -- see Rule B.
BACKFILL_FIELDS: Sequence[str] = ("priority", "archived", "queue_position", "parent")

#: How close a git observation must be to a native event on the same axis to be
#: considered the same change seen twice. A record is committed seconds to minutes
#: after it is written; five minutes covers that without swallowing a genuine
#: second move.
DEDUPE_WINDOW_SECONDS = 300

_COMMIT = re.compile(r"^@@C([0-9a-f]+)\|(.+)$")
_FIELD = re.compile(r"^([+-])(" + "|".join(BACKFILL_FIELDS) + r"): (.*)$")


@dataclass(frozen=True)
class Observation:
    """One field change git can see, and the commit that is the evidence for it."""

    task_file: str
    ts: str
    field: str
    before: Optional[str]
    after: Optional[str]
    commit: str

    #: True only when this is the file's very first commit. Set by :func:`observe`.
    at_creation: bool = False

    @property
    def is_initial(self) -> bool:
        """True when this is the value the record was created with, not a change.

        These are the valuable ones: the replay's failure was never missing
        intermediate steps -- priority moved 37 times in the project's life -- it was
        having no *starting* value to reason from.

        A missing ``-`` line is **not** sufficient evidence on its own. ``archived:``
        and ``parent:`` did not exist as fields until the schema-v2 migration added
        them to every existing record, so each arrives as an addition with nothing on
        the left, on a file that is months old. Treating those as creation values
        silently backdates them and loses 39 real parent changes.
        """
        return self.before is None and self.at_creation


class GitUnavailable(RuntimeError):
    """The task directory is not in a git repository, or git could not be run."""


def repository_root(tasks_dir: Path) -> Path:
    """The git work tree containing ``tasks_dir``."""
    try:
        result = subprocess.run(
            ["git", "-C", str(tasks_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - git missing from PATH
        raise GitUnavailable(f"git could not be run: {exc}") from exc
    if result.returncode != 0:
        raise GitUnavailable(
            f"{tasks_dir} is not inside a git repository, so there is no history to "
            "backfill from. Import without --backfill-git."
        )
    return Path(result.stdout.strip())


def observe(tasks_dir: Path) -> List[Observation]:
    """Every backfillable field change in the history of ``tasks_dir``.

    One pass over the whole history: ``git log -p -U0``, measured at 0.4 s and 11.2 MB
    of diff across 1,418 commits. Cheap enough to be a one-time job rather than a
    project, which is why it is worth doing at all.
    """
    root = repository_root(tasks_dir)
    relative = tasks_dir.resolve().relative_to(root.resolve()).as_posix()
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "--reverse",
            "--format=@@C%H|%cI",
            "-p",
            "-U0",
            "-M",
            "--no-color",
            "--",
            relative,
        ],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    if result.returncode != 0:  # pragma: no cover - a broken repository
        raise GitUnavailable(f"git log failed: {result.stderr[:400]}")

    observations: List[Observation] = []
    commit = ""
    timestamp = ""
    current: Optional[str] = None
    pending: Dict[str, List[Optional[str]]] = {}

    def flush() -> None:
        if current is None:
            return
        for field, (before, after) in pending.items():
            if before != after:
                observations.append(Observation(current, timestamp, field, before, after, commit))
        pending.clear()

    for line in result.stdout.splitlines():
        header = _COMMIT.match(line)
        if header is not None:
            flush()
            current = None
            commit, timestamp = header.group(1), header.group(2)
            continue
        if line.startswith("+++ b/"):
            flush()
            path = line[6:]
            current = path.rsplit("/", 1)[-1] if path.endswith(".yaml") else None
            continue
        if line.startswith(("--- ", "diff ", "@@", "index ", "new file", "deleted file")):
            continue
        if current is None:
            continue
        match = _FIELD.match(line)
        if match is not None:
            sign, field, value = match.group(1), match.group(2), match.group(3).strip()
            slot = pending.setdefault(field, [None, None])
            slot[0 if sign == "-" else 1] = value
    flush()
    birth = first_seen(observations)
    return [
        Observation(
            item.task_file,
            item.ts,
            item.field,
            item.before,
            item.after,
            item.commit,
            at_creation=item.ts == birth.get(item.task_file),
        )
        for item in observations
    ]


def first_seen(observations: Sequence[Observation]) -> Dict[str, str]:
    """The earliest commit instant per task file.

    One half of Rule A: the other terms come from the record itself, and the import
    takes the minimum of all three.
    """
    earliest: Dict[str, str] = {}
    for item in observations:
        if item.task_file not in earliest or item.ts < earliest[item.task_file]:
            earliest[item.task_file] = item.ts
    return earliest


__all__ = [
    "BACKFILL_FIELDS",
    "DEDUPE_WINDOW_SECONDS",
    "GitUnavailable",
    "Observation",
    "first_seen",
    "observe",
    "repository_root",
]
