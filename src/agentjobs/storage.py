"""YAML-backed persistence layer for AgentJobs tasks."""

from __future__ import annotations

import errno
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import yaml
from pydantic import ValidationError

from .models_v2 import SchemaVersionError, Task
from .models_v2 import load_task as _validate_v2
from .projects import contained_path

logger = logging.getLogger(__name__)


def _describe_validation_error(exc: ValidationError) -> str:
    """Render a pydantic error as 'field: message', naming the field that is wrong.

    Pydantic's default rendering is several lines per error with a docs URL. The point
    of this task is that a reader learns *which field* broke without opening a log
    aggregator, so the first few errors are compressed onto one line.
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


class TaskLockTimeout(Exception):
    """Another writer held the task lock for longer than we were willing to wait."""


class TaskStorage:
    """YAML-based task storage."""

    def __init__(self, tasks_dir: Path):
        """Initialize storage with tasks directory."""
        self.tasks_dir = Path(tasks_dir)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def _task_path(self, task_id: str) -> Path:
        """Resolve the path for a given task identifier.

        Task ids reach this method from URL path parameters, and one server now serves
        task directories drawn from a machine-wide registry. So the composed path is
        checked for containment rather than trusted: an id that resolves outside this
        project's tasks directory raises instead of reading or writing the file.
        """
        if task_id.endswith(".yaml"):
            filename = task_id
        else:
            filename = f"{task_id}.yaml"
        return contained_path(self.tasks_dir, filename)

    def load_task(self, task_id: str) -> Optional[Task]:
        """Load a task from its YAML file.

        Returns None when the file does not exist -- that is a legitimate answer to
        "is there a task with this id".

        Raises TaskLoadError when the file exists but cannot be read as a task. That
        is *not* a legitimate answer: it used to return None here too, which made a
        broken file indistinguishable from a missing one and dropped it out of every
        listing with nothing but a log line as evidence. A task that silently vanishes
        is the worst available failure mode, because the data it described is invisible
        precisely when someone needs to notice it is wrong.
        """
        path = self._task_path(task_id)
        if not path.exists():
            return None

        try:
            content = path.read_text(encoding="utf-8")
            data = yaml.safe_load(content) or {}
        except yaml.YAMLError as exc:
            raise TaskLoadError(path, f"invalid YAML: {exc}") from exc
        except OSError as exc:
            raise TaskLoadError(path, f"could not read the file: {exc}") from exc

        if not data:
            raise TaskLoadError(path, "the file is empty")
        if not isinstance(data, dict):
            raise TaskLoadError(
                path, f"expected a mapping at the top level, found {type(data).__name__}"
            )

        try:
            return _validate_v2(data, source=path.name)
        except SchemaVersionError as exc:
            # A missing or wrong `schema` stamp is a per-file problem, not a server
            # fault: wrap it so a stray unmigrated file is reported by filename in the
            # broken-files listing instead of crashing the whole listing.
            raise TaskLoadError(path, str(exc)) from exc
        except ValidationError as exc:
            raise TaskLoadError(path, _describe_validation_error(exc), errors=exc.errors()) from exc

    LOCK_TIMEOUT_SECONDS = 10.0
    LOCK_POLL_SECONDS = 0.01

    @contextmanager
    def locked(self, task_id: str, *, timeout: Optional[float] = None) -> Iterator[None]:
        """Hold an exclusive advisory lock on one task for the duration of the block.

        Implemented with O_CREAT|O_EXCL rather than fcntl or msvcrt: exclusive create
        is atomic on every filesystem AgentJobs runs on, needs no third-party
        dependency, and behaves the same on Windows and Unix. The cost is that a
        process killed mid-write leaves a stale lock, which is why the wait times out
        with an error naming the file rather than blocking forever.

        The lock is per task, so two agents working different tasks never contend.
        """
        lock_path = self._task_path(task_id).with_suffix(".lock")
        deadline = time.monotonic() + (self.LOCK_TIMEOUT_SECONDS if timeout is None else timeout)
        handle = None
        while True:
            try:
                handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TaskLockTimeout(
                        f"could not lock {lock_path.name} within "
                        f"{self.LOCK_TIMEOUT_SECONDS}s; another writer is holding it, "
                        "or a previous run died and left the lock behind"
                    ) from None
                time.sleep(self.LOCK_POLL_SECONDS)
            except OSError as exc:  # pragma: no cover - unexpected filesystem failure
                if exc.errno != errno.EEXIST:
                    raise
        try:
            os.write(handle, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(handle)
            try:
                lock_path.unlink()
            except FileNotFoundError:  # pragma: no cover - already cleaned up
                pass

    def mutate_task(self, task_id: str, mutator: Callable[[Task], Optional[Task]]) -> Task:
        """Read, change and write one task while holding its lock.

        This is the fix for the double-claim race, and the reason it is a method rather
        than advice in a docstring. The race was never inside save_task; it was in the
        load -> decide -> save sequence that every caller wrote by hand. Two agents
        could both read a task as ready, both decide they had won it, and both write.
        The second write silently overwrote the first, and nothing in the record showed
        it happened.

        The lock therefore spans all three steps, and the task is re-read *inside* it,
        so a decision is never made on a copy that went stale while waiting.

        The mutator may return None to mean "leave it alone", which is what lets a
        caller check a precondition under the lock and decline.
        """
        with self.locked(task_id):
            current = self.load_task(task_id)
            if current is None:
                raise ValueError(f"Task '{task_id}' not found.")
            updated = mutator(current)
            if updated is None:
                return current
            # Mutators assign attributes, which pydantic does not re-validate, so the
            # consistency rules are re-run here on the finished state -- one check at
            # the end rather than validate_assignment tripping over every intermediate
            # step of a multi-field transition. ValidationError subclasses ValueError,
            # so callers refuse the write the same way they refuse a bad precondition.
            Task.model_validate(
                updated.model_dump(mode="python", by_alias=True, exclude={"display_status"})
            )
            return self._write_task(updated)

    def save_task(self, task: Task) -> Task:
        """Save task to YAML file, returning the persisted Task instance."""
        with self.locked(task.id):
            return self._write_task(task)

    def _write_task(self, task: Task) -> Task:
        """Serialise a task to disk. Callers must already hold its lock.

        ``by_alias=True`` is load-bearing: the version stamp is ``schema_version`` in
        Python only because ``schema`` shadows a BaseModel attribute, and dumping
        without the alias writes the wrong key -- a file the loader then rejects as
        v1. ``display_status`` is computed for API responses and must never be
        stored (design doc section 3).
        """
        task.updated = datetime.now(tz=timezone.utc)
        path = self._task_path(task.id)
        task_dict = task.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
            exclude={"display_status"},
        )
        yaml_text = yaml.safe_dump(task_dict, sort_keys=False, allow_unicode=False)
        path.write_text(yaml_text, encoding="utf-8")
        return task

    def load_all(self) -> "LoadResult":
        """Load every task, keeping the broken ones instead of dropping them.

        One unreadable file must not take down the listing of the other thirty-seven,
        so errors are collected rather than raised. They are *returned* rather than
        logged, so that callers have to decide what to do with them -- which is what
        makes a broken file visible in the UI instead of only in a log nobody reads.
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
        """Every task that loads. Use load_all() when the broken ones matter too."""
        return self.load_all().tasks

    def generate_task_id(self) -> str:
        """Generate the next task identifier in sequence."""
        highest = 0
        for path in self.tasks_dir.glob("task-*.yaml"):
            stem = path.stem
            try:
                number = int(stem.split("-", maxsplit=1)[1])
            except (IndexError, ValueError):
                continue
            highest = max(highest, number)
        return f"task-{highest + 1:03d}"

    def delete_task(self, task_id: str) -> bool:
        """Delete task (archive)."""
        path = self._task_path(task_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def search_tasks(self, query: str) -> List[Task]:
        """Full-text search across tasks."""
        normalized = query.lower()
        results: List[Task] = []
        for task in self.list_tasks():
            haystacks = [
                task.title,
                task.spec.summary,
                task.spec.intent,
                task.spec.description,
                task.ball_prompt,
                " ".join(task.tags),
            ]
            if any(normalized in (haystack or "").lower() for haystack in haystacks):
                results.append(task)
        return results
