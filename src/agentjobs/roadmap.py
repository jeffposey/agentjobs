"""Project the backlog into the tracked ``docs/backlog.md`` the public repository reads.

The SQLite cutover moved the backlog into a database beside the server, which is the
right home for it and cost one thing nobody separated out: the repository's shop
window. ``origin`` is public, the only server runs on one machine behind a private
tailnet, and a reader who clones this repository can no longer see what is planned.
This module is the answer -- a **one-way projection** of the store into one tracked
markdown file.

Three properties are load-bearing, and none of them is decoration.

**It is a projection, not a mirror.** ``docs/storage-sqlite.md`` section 11 rejects a
second authority, and it is right to: a mirror something may write back to, or read as
truth, re-creates the split the migration removed. What is rejected there is
*bidirectionality*, not *derivation*. Nothing in this repository reads ``docs/backlog.md``
back, no code path treats it as a source of truth, and ``storage restore`` cannot be
pointed at it -- it is a build artefact that happens to be tracked, exactly like
``openapi.json`` and ``frontend/src/api/generated``.

**It is a pure function of the store, with no clock in it.** A "generated at"
stamp would make the file differ from its own regeneration the moment it was written,
so the freshness check could never pass and would be switched off inside a week. The
only inputs are the task records.

**It publishes the narrow half of a record.** A task carries branch names, run ids,
dispatch argv, handoff prose and internal decisions. A roadmap needs id, title, band,
place in line, one-sentence summary, and what blocks what. Everything else stays in the
store, which is both an editorial decision and the reason this file is safe to publish.

:func:`leaks` is the mechanical floor under that last property. It cannot judge whether
a summary is written for a public reader -- that is the ``roadmap`` playbook's work --
but it can refuse to publish a home directory or an email address, and a check that
fires on the unarguable cases is worth more than a rule nobody applies.

**The other half of the arrangement is not generated at all**, and this module holds a
second, much weaker check for it. A complete listing in queue order tells a reader what
happens next and nothing whatever about what the work *is*: half this backlog is one
subject spread over four bands, and no projection of the records can say so, because no
record says it. ``ROADMAP.md`` is therefore written by a person -- phases, the
workstreams inside them, and what each is for -- and links here for the detail.
:func:`audit_narrative` is what keeps a hand-written page from rotting unnoticed, and
its asymmetry is argued at :class:`NarrativeAudit`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models_v2 import PRIORITY_RANK, DependencyType, Lifecycle, Priority, Task
from .projects import Project
from .queue import listing_key
from .storage_config import StorageSettings, load_storage_settings

HEADER = """\
<!-- Generated from the AgentJobs task store by scripts/export_roadmap.py. Do not edit. -->
"""

BANDS: Tuple[Priority, ...] = tuple(
    sorted(PRIORITY_RANK, key=lambda priority: PRIORITY_RANK[priority])
)
"""Critical, high, medium, low -- the queue's own band order, read from its own table."""

PUBLISHED_LIFECYCLES: Tuple[Lifecycle, ...] = (Lifecycle.READY, Lifecycle.ACTIVE)
"""What a roadmap lists.

A draft is an idea nobody has specified yet: ``get_next_task`` will not return one and
no agent can claim one, so listing 29 of them beside work that is genuinely queued
would misrepresent the plan rather than disclose it. They are counted instead of
listed, so the file never implies the backlog is smaller than it is.
"""

PRIVATE_MARKERS: Tuple[Tuple[str, str], ...] = (
    (r"[A-Za-z]:[\\/]Users[\\/]", "a Windows home directory"),
    (r"(?<![\w.])/home/[A-Za-z0-9._-]+", "a Linux home directory"),
    (r"(?<![\w.])/Users/[A-Za-z0-9._-]+", "a macOS home directory"),
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "an email address"),
    (r"[A-Za-z0-9-]+\.ts\.net", "a tailnet hostname"),
)
"""Patterns that are machine-local or personal wherever they appear.

Deliberately narrow. A published artefact must carry none of this, and a check that
fires on ambiguous cases -- a loopback address, a project path -- would be argued with
and then disabled, which is worse than one that fires only on what nobody defends.
The editorial half of the same question belongs to a person reading the diff.
"""


@dataclass(frozen=True)
class Leak:
    """One published field carrying something that must not be published."""

    task_id: str
    field: str
    what: str
    matched: str

    def render(self) -> str:
        """``task-392 spec.summary contains a Windows home directory: C:\\Users\\``."""
        return f"{self.task_id} {self.field} contains {self.what}: {self.matched}"


class RoadmapLeakError(Exception):
    """Raised rather than publishing a record that names a person or a machine.

    Carries every leak, because the fix is to edit task records and a reader told about
    one at a time fixes them one round-trip at a time -- the same argument
    :class:`~agentjobs.playbooks.model.PlaybookError` makes for findings.
    """

    def __init__(self, found: Sequence[Leak]) -> None:
        self.leaks = list(found)
        super().__init__("; ".join(leak.render() for leak in self.leaks))


def published_fields(task: Task) -> List[Tuple[str, str]]:
    """The ``(field, text)`` pairs this projection actually puts in the file.

    Named rather than inlined so :func:`leaks` and :func:`render` cannot drift about
    what is published: a field scanned but not rendered is wasted strictness, and a
    field rendered but not scanned is the hole the scan exists to close.
    """
    return [("title", task.title), ("spec.summary", task.spec.summary)]


def leaks(tasks: Iterable[Task]) -> List[Leak]:
    """Every published field carrying a personal or machine-local marker."""
    found: List[Leak] = []
    for task in tasks:
        for field, text in published_fields(task):
            for pattern, what in PRIVATE_MARKERS:
                match = re.search(pattern, text)
                if match:
                    found.append(
                        Leak(task_id=task.id, field=field, what=what, matched=match.group(0))
                    )
    return found


def published(tasks: Iterable[Task]) -> List[Task]:
    """The open, unarchived, specified tasks a roadmap lists, in the queue's order."""
    listed = [
        task
        for task in tasks
        if task.is_open and not task.archived and task.lifecycle in PUBLISHED_LIFECYCLES
    ]
    return sorted(listed, key=listing_key)


def draft_count(tasks: Iterable[Task]) -> int:
    """Open drafts, which are counted rather than listed (see PUBLISHED_LIFECYCLES)."""
    return sum(
        1
        for task in tasks
        if task.is_open and not task.archived and task.lifecycle is Lifecycle.DRAFT
    )


ROSTER_ENTRY = re.compile(r"^\s*[-*]\s+`(task-[0-9A-Za-z][0-9A-Za-z-]*)`")
"""A task placed in a workstream: a list item whose first thing is a backticked id.

The narrative page names tasks in two quite different ways and only one of them is a
claim. A roster line -- ``- `task-309` -- the panel never says it will merge`` -- asserts
that this task is planned work in this workstream. A mention inside a paragraph is
commentary, and commentary about a task that has since closed is usually still true:
"the failure task-407 already exists about" does not stop being accurate history the day
task-407 merges. Auditing prose would therefore punish the page for being well written.
"""


@dataclass(frozen=True)
class NarrativeAudit:
    """What a hand-written roadmap page claims, checked against the store.

    The asymmetry between the two lists is the whole design, and it is deliberate rather
    than an oversight about to be tightened. :attr:`stale` is a lie -- the page offers a
    reader planned work that is finished or was never filed, and they can act on it.
    :attr:`unplaced` is merely lateness: a task filed this morning that nobody has put in
    a workstream yet, whose full entry is in the generated listing regardless.

    So stale ids fail and unplaced ids are reported. The stricter rule -- every open task
    placed, or the gate goes red -- was considered and rejected: it would make filing a
    task break the build until somebody edited prose, which is the shape of failure
    task-407 already exists about, and a check that stops ordinary work gets switched off
    inside a week.
    """

    placed: Tuple[str, ...]
    stale: Tuple[str, ...]
    unplaced: Tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Whether the page may be published. Only :attr:`stale` can answer no."""
        return not self.stale


def placed_ids(narrative: str) -> Tuple[str, ...]:
    """Every task id the page rosters, in the order it rosters them."""
    found = [
        match.group(1) for line in narrative.splitlines() if (match := ROSTER_ENTRY.match(line))
    ]
    return tuple(dict.fromkeys(found))


def audit_narrative(narrative: str, tasks: Iterable[Task]) -> NarrativeAudit:
    """Compare a hand-written roadmap page against the records it claims to describe."""
    listed = published(tasks)
    open_ids = {task.id for task in listed}

    placed = placed_ids(narrative)
    stale = tuple(task_id for task_id in placed if task_id not in open_ids)
    unplaced = tuple(task.id for task in listed if task.id not in set(placed))
    return NarrativeAudit(placed=placed, stale=stale, unplaced=unplaced)


def _relations(task: Task, titles: Dict[str, str], open_ids: frozenset) -> List[str]:
    """The one line under a task saying what it waits on and what it belongs to.

    Only unmet ``needs`` and the parent. A satisfied dependency shaped the order once
    and tells a reader nothing now; ``blocks`` and ``related`` are the same fact from
    the other end or an aside, and every extra edge is another thing that churns the
    file without changing what anyone would do about it.
    """
    parts: List[str] = []
    for dependency in task.dependencies:
        if dependency.type is not DependencyType.NEEDS or dependency.task not in open_ids:
            continue
        parts.append(f"needs {dependency.task}")
    if task.parent:
        parts.append(f"part of {task.parent} ({titles.get(task.parent, 'closed')})")
    if task.lifecycle is Lifecycle.ACTIVE:
        parts.append("in progress")
    return parts


def _entry(task: Task, titles: Dict[str, str], open_ids: frozenset) -> str:
    """One task as a markdown list item: identity, then summary, then relations.

    Blank lines between the three make each task a contiguous diff block, so a changed
    summary shows as a changed summary rather than as a reflowed band.
    """
    lines = [f"- **{task.id}** — {task.title}", "", f"  {task.spec.summary.strip()}"]
    relations = _relations(task, titles, open_ids)
    if relations:
        lines.extend(["", f"  *{' · '.join(relations)}*"])
    return "\n".join(lines)


PREAMBLE = """\
# Backlog

Every open AgentJobs task, in the order it will be worked. Bands run critical, high,
medium then low, and within a band the order is the queue's own — the same order
`agentjobs next` hands work out in.

This is the complete listing, and it is the one that is always current.
[ROADMAP.md](../ROADMAP.md) is the same work read as a plan — phases, and the
workstreams inside them — and it is written by a person rather than generated, so it
runs behind this file between passes.

This file is generated from the task store, which lives in a database beside the server
rather than in this repository. It is written one way and read by people: nothing here
reads it back, and it is never a source of truth. Regenerate it with

```bash
poetry run python scripts/export_roadmap.py docs/backlog.md
```

A task shows its id, its title, its one-sentence summary, what it is still waiting on,
and the umbrella it belongs to. Everything else a record carries — the working spec, the
decision log, branches, verification evidence — stays in the store.
"""


def render(tasks: Sequence[Task]) -> str:
    """The whole file, as a pure function of the records handed in.

    Raises :class:`RoadmapLeakError` rather than publishing a personal or machine-local
    detail. That is a refusal to write a file, not a refusal to answer: the leak names
    the task and the field, and the repair is to the record.
    """
    found = leaks(published(tasks))
    if found:
        raise RoadmapLeakError(found)

    listed = published(tasks)
    titles = {task.id: task.title for task in tasks}
    open_ids = frozenset(task.id for task in tasks if task.is_open)

    sections: List[str] = [HEADER, PREAMBLE]
    for band in BANDS:
        in_band = [task for task in listed if task.priority is band]
        if not in_band:
            continue
        sections.append(f"## {band.value.capitalize()} ({len(in_band)})\n")
        sections.append("\n\n".join(_entry(task, titles, open_ids) for task in in_band) + "\n")

    if not listed:
        sections.append("No task is ready or in progress.\n")

    drafts = draft_count(tasks)
    if drafts:
        noun = "task is a draft" if drafts == 1 else "tasks are drafts"
        sections.append(
            f"---\n\n{drafts} further open {noun}, not listed here. A draft is an idea that "
            "has not been specified yet: no agent can claim one and the queue does not hand "
            "one out.\n"
        )

    return "\n".join(sections)


def store_tasks(
    project: Project,
    *,
    settings: Optional[StorageSettings] = None,
) -> List[Task]:
    """Every record for one project, read straight from its database.

    Export tooling, and read-only. ``server_process`` is the sanctioned declaration for
    exactly this -- the import, backup, restore and export paths all hold it -- and WAL
    means a reader never blocks the server nor sees a half-written transaction, so this
    is safe to run against a live instance rather than needing one stopped.

    The imports are local because this module is imported by a gate script that must be
    able to say "there is no database here" rather than fail on an import.
    """
    from .sqlstore.store import SqlTaskStore
    from .store_factory import open_database, server_process

    resolved = settings or load_storage_settings()
    with server_process():
        store = SqlTaskStore(open_database(resolved.database_for(project.id)), project.id)
        return list(store.list_tasks())


def database_for(project: Project, *, settings: Optional[StorageSettings] = None) -> Path:
    """The file this project's records are in, whether or not it exists yet."""
    resolved = settings or load_storage_settings()
    return resolved.database_for(project.id)


def roadmap_for(project: Project, *, settings: Optional[StorageSettings] = None) -> str:
    """The rendered roadmap for one project, read from its store."""
    return render(store_tasks(project, settings=settings))


def audit_narrative_for(
    project: Project,
    narrative: str,
    *,
    settings: Optional[StorageSettings] = None,
) -> NarrativeAudit:
    """Audit a hand-written roadmap page against one project's store."""
    return audit_narrative(narrative, store_tasks(project, settings=settings))
