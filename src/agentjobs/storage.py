"""YAML-backed persistence layer for AgentJobs tasks."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Union

import yaml
from pydantic import ValidationError

from .attachments import AttachmentStore
from .instrumentation import record_task_parse
from .models_v2 import SchemaVersionError, Task
from .receipts import ReceiptStore
from .models_v2 import load_task as _validate_v2
from .projects import contained_path

logger = logging.getLogger(__name__)


#: The safe YAML loader task files are read with.
#:
#: libyaml is roughly thirteen times faster than PyYAML's pure-Python parser over this
#: project's corpus (0.859s -> 0.065s, 119 files, measured 2026-08-17), and reading is
#: the hot path: a listing parses every file, while a write serialises one.
#:
#: **Only the loader changes. Dumping stays on the pure-Python SafeDumper**, because
#: the two dumpers do not agree: 79 of 119 real task files serialise differently under
#: CSafeDumper, mostly in how long strings are folded and escaped. That is not a
#: cosmetic difference here. `canonical_bytes` exists so the validator can compare a
#: stored file against the form AgentJobs would have produced, and the receipt store
#: hashes those bytes -- so swapping the dumper would make most of the existing corpus
#: look hand-shaped until rewritten, and would put a formatting churn diff through
#: every task file on its next write. The read win is available without paying that.
try:
    from yaml import CSafeLoader as _SafeLoader

    YAML_LOADER = "libyaml (yaml.CSafeLoader)"
except ImportError:  # pragma: no cover - exercised by forcing the fallback in tests
    from yaml import SafeLoader as _SafeLoader  # type: ignore[assignment]

    YAML_LOADER = "pure-python (yaml.SafeLoader) -- libyaml not available"
    logger.warning(
        "libyaml is not available, so task files are parsed by PyYAML's pure-Python "
        "loader. This is around thirteen times slower and is the usual cause of a "
        "sluggish AgentJobs. Install a PyYAML wheel built with the C extension to fix "
        "it."
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


#: One parse of the corpus, shared for the duration of a scope.
#:
#: The map is keyed by tasks directory, so a request that touches two projects gets one
#: parse of each rather than one of the first. It lives in a ContextVar holding a
#: *mutable dict* rather than an immutable value, because FastAPI runs synchronous
#: routes in a worker thread with a copied context: a value assigned inside the route
#: updates the copy and is invisible to the caller, while a dict reached through the
#: copy is the same dict. The same subtlety bit the parse counter in task-131.
@dataclass
class _Snapshot:
    """What one scope has already parsed for one corpus.

    ``tasks`` memoises individual reads and ``result`` memoises the whole-corpus read.
    Both exist because requests arrive in both orders: the task-detail route reads its
    own task first and the corpus afterwards, and without the per-task memo that one
    file would be parsed twice -- once alone, once again as part of the corpus walk.
    """

    tasks: Dict[str, Union[Task, TaskLoadError, None]] = dc_field(default_factory=dict)
    result: Optional["LoadResult"] = None


_corpus_snapshot: ContextVar[Optional[Dict[str, _Snapshot]]] = ContextVar(
    "agentjobs_corpus_snapshot", default=None
)


@contextmanager
def corpus_snapshot() -> Iterator[None]:
    """Parse each task file at most once for the duration of the block.

    A single request used to walk the whole corpus about four times, because
    ``list_tasks``, ``dependency_facts``, ``_dependency_states`` and ``_open_children``
    each went back to storage independently. Memoising at ``load_all`` fixes all four
    at once without changing a single call site: it is the funnel they all pass
    through.

    **Scoped, deliberately, not process-wide.** AgentJobs is written for several
    writers -- the CLI, agents, git checkouts, a person editing YAML -- and a snapshot
    that outlived its request would serve a task record that had already changed on
    disk. Entering a scope per request keeps the window to the length of one request,
    within which the answer must be self-consistent anyway. Caching *across* requests
    is task-134, and it has to earn that with an invalidation story this does not need.

    Writes inside the scope drop the snapshot, so a read after a write in the same
    request sees the write rather than the state from before it.
    """
    token = _corpus_snapshot.set({})
    try:
        yield
    finally:
        _corpus_snapshot.reset(token)


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
        self.receipts = ReceiptStore.for_tasks_directory(self.tasks_dir)
        # Sidecar images live beside the task files and are reached through storage,
        # so nothing above this layer composes a filesystem path of its own.
        self.attachments = AttachmentStore(self.tasks_dir)

    def project_revision(self) -> tuple[str, int]:
        """Return a cheap signal that changes when this project's task files change.

        The API is not the only writer: the CLI, agents, direct edits, and git all
        replace YAML files without going through a shared process counter.  Hashing
        sorted file bytes covers every one of those paths, including a rapid same-size
        rewrite on filesystems whose timestamps collide.  It deliberately does not
        parse or validate YAML, so broken task files still participate in the signal.
        """
        digest = hashlib.blake2s(digest_size=12)
        count = 0
        for path in sorted(self.tasks_dir.glob("*.yaml"), key=lambda item: item.name):
            try:
                content = path.read_bytes()
            except FileNotFoundError:
                # An atomic writer can replace a file between glob and stat.  The next
                # poll will see the completed replacement; a transient 500 would turn
                # an ordinary write into a false unreachable signal.
                continue
            count += 1
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
            digest.update(b"\0")
        return digest.hexdigest(), count

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
        snapshot = self._snapshot()
        if snapshot is None:
            return self._load_task_uncached(task_id)

        key = self._normalised_id(task_id)
        if key in snapshot.tasks:
            found = snapshot.tasks[key]
            if isinstance(found, TaskLoadError):
                raise found
            return found

        try:
            task = self._load_task_uncached(task_id)
        except TaskLoadError as exc:
            snapshot.tasks[key] = exc
            raise
        if task is not None:
            snapshot.tasks[key] = task
        # A miss is not memoised as "absent": guessing "no such task" from a cache is
        # the kind of shortcut that turns into a bug report, and the miss costs one stat.
        return task

    def _load_task_uncached(self, task_id: str) -> Optional[Task]:
        """Read one task straight from disk, ignoring any snapshot.

        The write path must use this. ``mutate_task`` re-reads the task *inside* its
        lock precisely so a decision is never made on a copy that went stale while
        waiting, and serving that read from a snapshot taken earlier in the same
        request would quietly undo the concurrency guarantee that lock exists for.
        """
        path = self._task_path(task_id)
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

        **Windows reports contention two different ways.** The obvious one is
        ``FileExistsError`` (EEXIST). The other only appears under real concurrency: a
        file whose last handle has closed but whose delete has not yet completed sits in
        a *delete-pending* state, and opening it returns ERROR_ACCESS_DENIED, which
        Python raises as ``PermissionError`` (EACCES). That is a lock still being
        released, so it must be retried like any other contention -- treating it as a
        hard error made a losing claimant crash with "Permission denied" instead of
        being told the task was already taken. Roughly one attempt in forty, so it hides
        well from a serial test.

        The cost is that a genuine permissions problem also spins until the timeout
        rather than failing immediately. The timeout message names that possibility,
        which is the better trade: spurious failures under normal contention are worse
        than a slow, well-described failure in a misconfigured directory.
        """
        lock_path = self._task_path(task_id).with_suffix(".lock")
        deadline = time.monotonic() + (self.LOCK_TIMEOUT_SECONDS if timeout is None else timeout)
        handle = None
        while True:
            try:
                handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except (FileExistsError, PermissionError):
                pass
            except OSError as exc:  # pragma: no cover - unexpected filesystem failure
                if exc.errno != errno.EEXIST:
                    raise
            if time.monotonic() >= deadline:
                raise TaskLockTimeout(
                    f"could not lock {lock_path.name} within "
                    f"{self.LOCK_TIMEOUT_SECONDS}s; another writer is holding it, a "
                    "previous run died and left the lock behind, or the task directory "
                    "is not writable"
                ) from None
            time.sleep(self.LOCK_POLL_SECONDS)
        try:
            os.write(handle, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(handle)
            try:
                lock_path.unlink()
            except FileNotFoundError:  # pragma: no cover - already cleaned up
                pass

    #: Lock name covering the whole project, used for creation. It is not a task id and
    #: cannot collide with one: generated and hand-written ids never start with a dot.
    CREATION_LOCK = ".creation"

    @contextmanager
    def creation_lock(self, *, timeout: Optional[float] = None) -> Iterator[None]:
        """Serialise task creation across the whole project.

        Per-task locking cannot help here: before a task exists there is nothing to
        lock, so two concurrent creates both generate the same next id, both find
        nothing on disk, and one silently overwrites the other. The id is the thing
        being decided, which is why this lock covers the project rather than an id.

        Creation is rare next to mutation, so a single writer for it costs nothing
        measurable, and it is held for as little as possible.
        """
        with self.locked(self.CREATION_LOCK, timeout=timeout):
            yield

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
            current = self._load_task_uncached(task_id)
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
        # Recorded here, at the single point every managed write passes through, so
        # the manager, the API, the CLI, MCP, the GUI and the schema migrator all
        # produce receipts without any of them knowing receipts exist.
        self.receipts.record(
            task_id=task.id, path=path, data=yaml_text.encode("utf-8"), operation="write"
        )
        # A read later in the same scope must see this write, not the corpus as it was
        # before it. Dropping the snapshot here rather than trying to patch the written
        # task into it keeps the invalidation trivially correct: the next read reparses.
        self._invalidate_snapshot()
        return task

    def canonical_bytes(self, task: Task) -> bytes:
        """Serialise a task exactly as ``_write_task`` would.

        Exposed so the validator can compare a stored file against the form AgentJobs
        would have produced, and report the difference when a file was hand-shaped.
        """
        task_dict = task.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
            exclude={"display_status"},
        )
        return yaml.safe_dump(task_dict, sort_keys=False, allow_unicode=False).encode("utf-8")

    @staticmethod
    def _normalised_id(task_id: str) -> str:
        """Task ids reach storage with and without the .yaml suffix; index by stem."""
        return task_id[: -len(".yaml")] if task_id.endswith(".yaml") else task_id

    def _snapshot_key(self) -> str:
        """This storage's identity within a corpus snapshot."""
        return str(self.tasks_dir)

    def _snapshot(self) -> Optional[_Snapshot]:
        """This corpus's scratchpad for the current scope, or None outside a scope."""
        cache = _corpus_snapshot.get()
        if cache is None:
            return None
        return cache.setdefault(self._snapshot_key(), _Snapshot())

    def _invalidate_snapshot(self) -> None:
        """Drop any snapshot of this corpus, because it has just been written to."""
        cache = _corpus_snapshot.get()
        if cache is not None:
            cache.pop(self._snapshot_key(), None)

    def load_all(self) -> "LoadResult":
        """Load every task, keeping the broken ones instead of dropping them.

        Inside a ``corpus_snapshot()`` scope the first call does the work and the rest
        reuse it, which is what takes a request from four passes over the corpus to
        one. Outside a scope it behaves exactly as it always did.
        """
        snapshot = self._snapshot()
        if snapshot is None:
            return self._load_all_uncached()
        if snapshot.result is not None:
            return snapshot.result
        snapshot.result = self._load_all_uncached()
        return snapshot.result

    def _load_all_uncached(self) -> "LoadResult":
        """Read and parse every task file.

        One unreadable file must not take down the listing of the other thirty-seven,
        so errors are collected rather than raised. They are *returned* rather than
        logged, so that callers have to decide what to do with them -- which is what
        makes a broken file visible in the UI instead of only in a log nobody reads.
        """
        result = LoadResult()
        for path in sorted(self.tasks_dir.glob("*.yaml")):
            # Through load_task, so a file this scope has already read is reused rather
            # than parsed a second time.
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
        self._invalidate_snapshot()
        return True

    def search_tasks(self, query: str) -> List[Task]:
        """Full-text search across tasks.

        ``task.id`` is searched first because it is the handle people actually quote
        to each other. A reviewer asking about "058" means task-058, and a search
        that reads every prose field but not the identifier answers "no such task"
        to the one query it should always get right.
        """
        normalized = query.lower()
        results: List[Task] = []
        for task in self.list_tasks():
            haystacks = [
                task.id,
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
