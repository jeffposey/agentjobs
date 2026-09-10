"""A directory of task YAML: the road in, and the road out.

Task records are rows in a database. This module is what remains of the backend that
came before -- **not a second authority, and not something a ``TaskManager`` is built
over.** It reads and writes a *directory of files*, which is still a real thing in three
places and nowhere else:

1.  **The import.** ``agentjobs storage import`` reads a corpus somebody arrived with
    and writes it into a database. That is the only road in, and it needs a reader.
2.  **The export.** ``agentjobs storage export`` writes the store's current state out as
    an interchange artifact. A person reads it, another tool ingests it; nothing
    maintains it.
3.  **The file-format tooling that prepares a corpus for import** -- the Markdown
    migration and the queue-position baseline -- which take a directory of files and
    leave a better directory of files behind.

What it deliberately does **not** have is everything that made the old
``TaskStorage`` an authority: advisory locks, a mutate-under-lock verb, id allocation,
a per-request parse snapshot, deletion, a project revision signal. Those exist to let
several writers share one corpus safely, and no writer shares a corpus any more. A file
here is an artifact, and the constraint that a task is a row is enforced where the rows
are.

The YAML loader and the load-error types live here rather than beside the store because
they are about *reading a file*, and the database has no files to read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import ValidationError

from .attachments import AttachmentStore
from .instrumentation import record_task_parse
from .models_v2 import SchemaVersionError, Task
from .models_v2 import load_task as _validate_v2
from .projects import contained_path
from .receipts import ReceiptStore

logger = logging.getLogger(__name__)


#: The safe YAML loader task files are read with.
#:
#: libyaml is roughly thirteen times faster than PyYAML's pure-Python parser over this
#: project's corpus (0.859s -> 0.065s, 119 files, measured 2026-08-17). It matters less
#: now that a listing is a query rather than a walk, but an import still parses every
#: file in a corpus in one go, which is the largest parse AgentJobs ever does.
#:
#: **Only the loader changes. Dumping stays on the pure-Python SafeDumper**, because
#: the two dumpers do not agree: 79 of 119 real task files serialise differently under
#: CSafeDumper, mostly in how long strings are folded and escaped. An export whose bytes
#: churned on a loader upgrade would be a poor interchange artifact.
try:
    from yaml import CSafeLoader as _SafeLoader

    YAML_LOADER = "libyaml (yaml.CSafeLoader)"
except ImportError:  # pragma: no cover - exercised by forcing the fallback in tests
    from yaml import SafeLoader as _SafeLoader  # type: ignore[assignment]

    YAML_LOADER = "pure-python (yaml.SafeLoader) -- libyaml not available"
    logger.warning(
        "libyaml is not available, so task files are parsed by PyYAML's pure-Python "
        "loader. This is around thirteen times slower and is the usual cause of a "
        "sluggish import. Install a PyYAML wheel built with the C extension to fix it."
    )


def yaml_loader_name() -> str:
    """The YAML loader currently in use.

    Surfaced on ``/api/version`` and printed by ``scripts/bench.py`` so a before/after
    pair cannot be accidentally compared across loaders -- a thirteenfold difference
    would swamp whatever change was actually under test.
    """
    return YAML_LOADER


def load_yaml(content: str) -> Any:
    """Parse YAML with the fastest safe loader available.

    A drop-in for ``yaml.safe_load``: same safety guarantees, same result. The parity
    is not assumed -- ``tests/test_yaml_loader.py`` asserts that every file in the real
    corpus loads identically under both.
    """
    return yaml.load(content, Loader=_SafeLoader)


def _describe_validation_error(exc: ValidationError) -> str:
    """Render a pydantic error as 'field: message', naming the field that is wrong.

    Pydantic's default rendering is several lines per error with a docs URL. The point
    is that a reader learns *which field* broke without opening a log aggregator, so the
    first few errors are compressed onto one line.
    """
    parts = []
    for error in exc.errors()[:3]:
        location = ".".join(str(item) for item in error.get("loc", ())) or "(root)"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    remaining = len(exc.errors()) - 3
    if remaining > 0:
        parts.append(f"and {remaining} more problem(s)")
    return "; ".join(parts)


class TaskLoadError(Exception):
    """A task file exists but cannot be read as a task.

    Carries the path and a field-level description so the message answers "which file,
    which field, what is wrong" without further digging.

    Still raised where there are no files: the store reports a record it had to
    quarantine at import as one of these, because a caller asking "what could not be
    read" wants one answer and not two shapes of answer.
    """

    def __init__(self, path: Path, reason: str, *, errors: Optional[List[Any]] = None):
        """Initialize with the offending file and a human-readable reason."""
        self.path = Path(path)
        self.task_id = self.path.stem
        self.reason = reason
        self.errors = errors or []
        super().__init__(f"{self.path.name}: {reason}")

    def as_dict(self) -> Dict[str, Any]:
        """Serialisable form, for API responses and templates."""
        return {
            "task_id": self.task_id,
            "path": str(self.path),
            "filename": self.path.name,
            "reason": self.reason,
        }


@dataclass
class LoadResult:
    """Tasks that loaded, plus the files that did not."""

    tasks: List[Task] = dc_field(default_factory=list)
    errors: List[TaskLoadError] = dc_field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


def canonical_bytes(task: Task) -> bytes:
    """A task as the YAML file AgentJobs would write for it.

    A free function rather than a method, because both sides need it and only one of
    them has a directory: the export writes these bytes, and the validator compares a
    file on disk against them to report one that was hand-shaped.

    ``by_alias=True`` is load-bearing: the version stamp is ``schema_version`` in Python
    only because ``schema`` shadows a BaseModel attribute, and dumping without the alias
    writes the wrong key -- a file the loader then rejects as v1. ``display_status`` is
    computed for API responses and must never be stored (design doc section 3).
    """
    document = task.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
        exclude={"display_status"},
    )
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=False).encode("utf-8")


class TaskFileCorpus:
    """One directory of task YAML, read and written as files.

    Not a task store. It has no locks and no transaction, because the cases it serves
    -- import a corpus, export a corpus, reshape a corpus before importing it -- each
    have exactly one writer by construction. The old backend's advisory locks existed
    for a directory several live processes wrote to at once, and there is no such
    directory now.
    """

    def __init__(self, tasks_dir: Path, *, create: bool = True):
        """Point at a directory, creating it unless the caller only means to read."""
        self.tasks_dir = Path(tasks_dir)
        if create:
            self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.receipts = ReceiptStore.for_tasks_directory(self.tasks_dir)
        # Sidecar images live beside the task files, so nothing above this layer
        # composes a filesystem path of its own.
        self.attachments = AttachmentStore(self.tasks_dir)

    # ----- reading ---------------------------------------------------------

    def task_path(self, task_id: str) -> Path:
        """Where a task's file lives.

        The composed path is checked for containment rather than trusted: an id that
        resolves outside this directory raises instead of reading or writing the file.
        """
        filename = task_id if task_id.endswith(".yaml") else f"{task_id}.yaml"
        return contained_path(self.tasks_dir, filename)

    _task_path = task_path

    def has_task(self, task_id: str) -> bool:
        """True when this directory holds a file for that id."""
        return self.task_path(task_id).exists()

    def load_task(self, task_id: str) -> Optional[Task]:
        """Load a task from its YAML file.

        Returns ``None`` when the file does not exist -- that is a legitimate answer to
        "is there a task with this id".

        Raises :class:`TaskLoadError` when the file exists but cannot be read as a task.
        That is *not* a legitimate answer: returning ``None`` there would make a broken
        file indistinguishable from a missing one and drop it out of every listing, and
        a record that silently vanishes is the worst available failure mode -- an import
        that skipped it would report a clean run.
        """
        path = self.task_path(task_id)
        if not path.exists():
            return None

        # Counted in `finally` rather than on the happy path: a file whose YAML is
        # invalid still cost a read and a parse attempt, and the counter is a measure
        # of work done, not of work that succeeded.
        try:
            content = path.read_text(encoding="utf-8")
            data = load_yaml(content) or {}
        except yaml.YAMLError as exc:
            raise TaskLoadError(path, f"invalid YAML: {exc}") from exc
        except OSError as exc:
            raise TaskLoadError(path, f"could not read the file: {exc}") from exc
        finally:
            record_task_parse()

        if not data:
            raise TaskLoadError(path, "the file is empty")
        if not isinstance(data, dict):
            raise TaskLoadError(
                path, f"expected a mapping at the top level, found {type(data).__name__}"
            )

        try:
            return _validate_v2(data, source=path.name)
        except SchemaVersionError as exc:
            # A missing or wrong `schema` stamp is a per-file problem: wrap it so a
            # stray unmigrated file is reported by filename rather than taking down the
            # whole read.
            raise TaskLoadError(path, str(exc)) from exc
        except ValidationError as exc:
            raise TaskLoadError(path, _describe_validation_error(exc), errors=exc.errors()) from exc

    def load_all(self) -> LoadResult:
        """Read every task file, keeping the broken ones instead of dropping them.

        One unreadable file must not take down the reading of the other thirty-seven,
        so errors are collected rather than raised. They are *returned* rather than
        logged, so a caller has to decide what to do with them -- which is what makes a
        broken file visible in a report instead of only in a log nobody reads.
        """
        result = LoadResult()
        for path in sorted(self.tasks_dir.glob("*.yaml")):
            try:
                task = self.load_task(path.stem)
            except TaskLoadError as exc:
                logger.error("%s", exc)
                result.errors.append(exc)
                continue
            if task is not None:
                result.tasks.append(task)
        return result

    def list_tasks(self) -> List[Task]:
        """Every task that loads. Use :meth:`load_all` when the broken ones matter too."""
        return self.load_all().tasks

    # ----- writing ---------------------------------------------------------

    def canonical_bytes(self, task: Task) -> bytes:
        """The bytes :meth:`save_task` would write. See :func:`canonical_bytes`."""
        return canonical_bytes(task)

    def save_task(self, task: Task) -> Task:
        """Write one task file in canonical form, returning the task that was written.

        A write receipt is recorded, which is what lets ``agentjobs validate --staged``
        tell a file this wrote from one somebody edited by hand. That distinction is
        still worth having for as long as a repository carries task files at all.
        """
        task.updated = datetime.now(tz=timezone.utc)
        path = self.task_path(task.id)
        payload = canonical_bytes(task)
        path.write_bytes(payload)
        self.receipts.record(task_id=task.id, path=path, data=payload, operation="write")
        return task


__all__ = [
    "YAML_LOADER",
    "LoadResult",
    "TaskFileCorpus",
    "TaskLoadError",
    "canonical_bytes",
    "load_yaml",
    "yaml_loader_name",
]
