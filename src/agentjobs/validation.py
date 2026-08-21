"""Portable validation of a task corpus.

The backstop for everything the Codex hook cannot see: another client, a text editor,
a script, a merge that went wrong. It needs nothing but the files, so it works in CI,
in a clean clone, and on a machine that has never run AgentJobs.

**What it can and cannot prove.** It proves the corpus is safe to load and that every
record satisfies the invariants the model and the workflow depend on. It cannot prove
which program wrote a file, because a careful hand-edit produces a file that validates
perfectly. That is the gap ``--staged`` closes locally using write receipts, and the
gap CI structurally cannot close -- stated here rather than hidden behind the word
"enforcement".
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

import yaml

from .actors import load_actors
from .manager import TaskManager
from .models_v2 import Task
from .receipts import ReceiptStore, content_hash
from .storage import TaskStorage


@dataclass(frozen=True)
class Finding:
    """One problem, named precisely enough to fix without further digging."""

    filename: str
    rule: str
    message: str
    task_id: Optional[str] = None

    def render(self) -> str:
        """One line, for a terminal or a CI log."""
        return f"{self.filename}: [{self.rule}] {self.message}"


@dataclass
class ValidationReport:
    """Everything one validation run found."""

    findings: List[Finding] = field(default_factory=list)
    checked: int = 0
    receipts_available: bool = False

    @property
    def ok(self) -> bool:
        """Whether the corpus passed."""
        return not self.findings

    def render(self) -> str:
        """The report a human reads."""
        if self.ok:
            return f"{self.checked} task file(s) validated; no problems found."
        lines = [finding.render() for finding in sorted(self.findings, key=lambda f: f.filename)]
        lines.append(f"\n{len(self.findings)} problem(s) across {self.checked} task file(s).")
        return "\n".join(lines)


def validate_corpus(
    tasks_dir: Path,
    *,
    project_config: Optional[Dict[str, object]] = None,
    project_root: Optional[Path] = None,
) -> ValidationReport:
    """Validate every task file in one directory."""
    storage = TaskStorage(tasks_dir)
    manager = TaskManager(storage)
    report = ValidationReport()
    config = project_config or {}

    loaded = storage.load_all()
    report.checked = len(loaded.tasks) + len(loaded.errors)

    # Every state and log invariant -- an open task with no ball, a closed one with
    # no outcome, an active one with no owner, a handoff with no ask, duplicate or
    # out-of-order log ids, a thread pointing at nothing -- is enforced by the Task
    # model at load time, so a file that breaks one arrives here as a load error
    # naming the offending field. Re-checking them after load was tried and removed:
    # every such check was unreachable, and an unreachable check is decoration that
    # makes the file look more thorough than it is.
    for error in loaded.errors:
        report.findings.append(
            Finding(
                filename=Path(error.path).name,
                rule="unreadable",
                message=error.reason,
                task_id=error.task_id,
            )
        )

    known = {task.id for task in loaded.tasks}
    for task in loaded.tasks:
        filename = f"{task.id}.yaml"
        report.findings.extend(_check_filename(task, tasks_dir))
        report.findings.extend(_check_taxonomy(task, filename, config))
        report.findings.extend(_check_relationships(task, filename, known))
        report.findings.extend(_check_paths(task, filename, project_root))
        report.findings.extend(_check_canonical_form(task, filename, storage))

    report.findings.extend(_check_cycles(manager, loaded.tasks))
    report.findings.extend(_check_queue(tasks_dir))
    return report


# The four things that can be wrong with a queue, and the one rule each of them
# breaks. Kept next to the check so the messages and the design stay in step.
_QUEUE_RULES = "design doc section 3.2"


def _check_queue(tasks_dir: Path) -> List[Finding]:
    """Queue positions: present on open work, absent on closed, positive, unique.

    **Reads the raw YAML rather than loaded tasks, and that is the point.** Three of
    these four conditions are rejected by ``Task`` at load time (rule 6 and ``ge=1``),
    so a check written against ``LoadResult.tasks`` could only ever catch duplicates;
    the other three would be decoration. Worse, the corpus you most need this check
    for is the one that will not load -- and a Pydantic message about field
    ``queue_position`` on one file tells you nothing about which *other* file it
    collides with.

    So this walks the files themselves. A broken queue stays inspectable: every
    offending id is named, with its band, whether or not the record is loadable.
    Findings, never exceptions -- you must be able to see a broken queue in order to
    fix it (design doc section 8).

    Only the four queue rules are read out of the raw mapping. Anything else wrong
    with the file is already reported as ``unreadable`` by the loader, and a file too
    broken to yield an id or a lifecycle is left to that report rather than guessed at.
    """
    findings: List[Finding] = []
    # band -> position -> the ids claiming it
    bands: Dict[str, Dict[int, List[str]]] = {}

    for path in sorted(tasks_dir.glob("*.yaml")):
        filename = path.name
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue  # reported as `unreadable` by the loader; not this check's job.
        if not isinstance(raw, dict):
            continue

        task_id = raw.get("id")
        lifecycle = raw.get("lifecycle")
        if not isinstance(task_id, str) or not isinstance(lifecycle, str):
            continue
        closed = lifecycle == "closed"
        position = raw.get("queue_position")

        if position is None:
            if not closed:
                findings.append(
                    Finding(
                        filename,
                        "queue-missing",
                        f"lifecycle '{lifecycle}' is open, so queue_position is "
                        f"required ({_QUEUE_RULES})",
                        task_id,
                    )
                )
            continue

        if closed:
            findings.append(
                Finding(
                    filename,
                    "queue-on-closed",
                    f"closed task carries queue_position {position!r}; a closed task "
                    "is not in line any more",
                    task_id,
                )
            )
            continue

        # `bool` is an `int` in Python, and `queue_position: true` parses as one.
        if not isinstance(position, int) or isinstance(position, bool) or position < 1:
            findings.append(
                Finding(
                    filename,
                    "queue-not-positive",
                    f"queue_position {position!r} is not a positive integer",
                    task_id,
                )
            )
            continue

        band = str(raw.get("priority") or "medium")
        bands.setdefault(band, {}).setdefault(position, []).append(task_id)

    for band, positions in sorted(bands.items()):
        for position, ids in sorted(positions.items()):
            if len(ids) < 2:
                continue
            shared = ", ".join(sorted(ids))
            for task_id in sorted(ids):
                findings.append(
                    Finding(
                        f"{task_id}.yaml",
                        "queue-duplicate",
                        f"queue_position {position} in band '{band}' is shared by "
                        f"{shared}; positions are unique among open tasks of one band",
                        task_id,
                    )
                )
    return findings


def _check_filename(task: Task, tasks_dir: Path) -> List[Finding]:
    """The stored id and the filename must agree.

    They are two names for the same thing, and every lookup goes through the
    filename. A file whose id disagrees is reachable under one name and reports the
    other, which makes every subsequent error confusing.
    """
    expected = tasks_dir / f"{task.id}.yaml"
    if expected.exists():
        return []
    matches = [path for path in tasks_dir.glob("*.yaml") if path.stem != task.id]
    for path in matches:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and data.get("id") == task.id:
            return [
                Finding(
                    filename=path.name,
                    rule="filename-id-mismatch",
                    message=f"file is named {path.stem!r} but stores id {task.id!r}",
                    task_id=task.id,
                )
            ]
    return []


def _check_taxonomy(task: Task, filename: str, config: Dict[str, object]) -> List[Finding]:
    """Categories and actor references, where the project has declared them.

    Only checked when the project says what it allows. A project that has not
    configured categories or actors is not in violation of a policy it never set.
    """
    findings: List[Finding] = []
    categories = config.get("categories")
    if isinstance(categories, list) and categories and task.category not in categories:
        findings.append(
            Finding(
                filename,
                "unknown-category",
                f"category {task.category!r} is not one of: {', '.join(sorted(map(str, categories)))}",
                task.id,
            )
        )
    actors = load_actors(config) if config else {}
    if actors:
        referenced = {entry.actor for entry in task.log}
        if task.assignment.owner:
            referenced.add(task.assignment.owner)
        referenced.update(task.assignment.eligible)
        for actor in sorted(referenced - set(actors)):
            findings.append(
                Finding(
                    filename,
                    "unknown-actor",
                    f"{actor!r} is referenced but is not a configured actor",
                    task.id,
                )
            )
    return findings


def _check_relationships(task: Task, filename: str, known: Set[str]) -> List[Finding]:
    """Parents and dependencies must exist and must not point at themselves."""
    findings: List[Finding] = []
    if task.parent is not None:
        if task.parent == task.id:
            findings.append(
                Finding(filename, "self-parent", "a task cannot be its own parent", task.id)
            )
        elif task.parent not in known:
            findings.append(
                Finding(
                    filename, "missing-parent", f"parent {task.parent!r} does not exist", task.id
                )
            )
    for dependency in task.dependencies:
        if dependency.task == task.id:
            findings.append(
                Finding(
                    filename,
                    "self-dependency",
                    f"a task cannot {dependency.type.value} itself",
                    task.id,
                )
            )
        elif dependency.task not in known:
            findings.append(
                Finding(
                    filename,
                    "missing-dependency",
                    f"{dependency.type.value} dependency {dependency.task!r} does not exist",
                    task.id,
                )
            )
    return findings


def _check_cycles(manager: TaskManager, tasks: Sequence[Task]) -> List[Finding]:
    """Report `needs` cycles, using the canonical implementation.

    Calls ``TaskManager.dependency_facts`` rather than walking the graph here. A
    second implementation would be free to disagree with the one the product uses to
    decide what is claimable, and the disagreement would surface as a validator that
    passes a corpus nobody can work.
    """
    findings: List[Finding] = []
    facts = manager.dependency_facts(list(tasks))
    for task in tasks:
        cycles = facts[task.id].needs_cycles
        for cycle in cycles:
            findings.append(
                Finding(
                    filename=f"{task.id}.yaml",
                    rule="dependency-cycle",
                    message=(
                        f"needs cycle {' -> '.join(cycle)}: every task in it is permanently "
                        "unclaimable, and no listing will say so"
                    ),
                    task_id=task.id,
                )
            )
    return findings


def _check_paths(task: Task, filename: str, project_root: Optional[Path]) -> List[Finding]:
    """Context and deliverable paths must stay inside the project.

    Only checked when a root is known. A path that escapes the repository is either a
    mistake or an attempt to point tooling somewhere it should not go; either way the
    record should not carry it.
    """
    if project_root is None:
        return []
    findings: List[Finding] = []
    root = project_root.resolve()
    candidates = [pointer.path for pointer in task.spec.context]
    candidates.extend(deliverable.path for deliverable in task.deliverables)
    for candidate in candidates:
        path = Path(candidate)
        if path.is_absolute():
            findings.append(
                Finding(
                    filename,
                    "absolute-path",
                    f"path {candidate!r} should be repository-relative",
                    task.id,
                )
            )
            continue
        try:
            resolved = (root / path).resolve()
        except (OSError, RuntimeError):  # pragma: no cover - unresolvable
            continue
        if root not in resolved.parents and resolved != root:
            findings.append(
                Finding(
                    filename,
                    "path-escapes-project",
                    f"path {candidate!r} resolves outside the project",
                    task.id,
                )
            )
    return findings


def _check_canonical_form(task: Task, filename: str, storage: TaskStorage) -> List[Finding]:
    """Report a file that AgentJobs would have written differently.

    Not a correctness failure on its own -- the file loads and validates. It is the
    signal that something other than AgentJobs shaped it, which is exactly what this
    layer exists to make visible.
    """
    path = storage.tasks_dir / filename
    try:
        actual = path.read_bytes()
    except OSError:
        return []
    expected = storage.canonical_bytes(task)
    if content_hash(actual) == content_hash(expected):
        return []
    return [
        Finding(
            filename,
            "non-canonical-serialization",
            "the file is valid but is not byte-identical to what AgentJobs would write; "
            "it was probably hand-edited. Re-save it through a managed operation.",
            task.id,
        )
    ]


# ---------------------------------------------------------------------------
# The staged-change gate
# ---------------------------------------------------------------------------
def staged_task_files(repo_root: Path, tasks_dir: Path) -> List[Path]:
    """Task files staged for commit, as absolute paths."""
    try:
        completed = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    resolved_tasks = tasks_dir.resolve()
    staged: List[Path] = []
    for line in completed.stdout.splitlines():
        name = line.strip()
        if not name.endswith((".yaml", ".yml")):
            continue
        path = (repo_root / name).resolve()
        if path.parent == resolved_tasks:
            staged.append(path)
    return staged


def staged_content(repo_root: Path, path: Path) -> Optional[bytes]:
    """The staged bytes of a file, which may differ from what is on disk."""
    relative = path.resolve().relative_to(repo_root.resolve()).as_posix()
    try:
        completed = subprocess.run(
            ["git", "show", f":{relative}"],
            cwd=str(repo_root),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def check_staged_receipts(
    repo_root: Path, tasks_dir: Path, receipts: Optional[ReceiptStore] = None
) -> List[Finding]:
    """Require each staged task file to match a receipt from a managed write.

    This is the check a schema-only validator cannot make: a hand edit that happens
    to be valid is indistinguishable from a managed one by content alone, and only
    differs in whether AgentJobs recorded writing it.
    """
    store = receipts or ReceiptStore.for_tasks_directory(tasks_dir)
    findings: List[Finding] = []
    for path in staged_task_files(repo_root, tasks_dir):
        data = staged_content(repo_root, path)
        if data is None:
            continue
        task_id = path.stem
        receipt = store.latest(task_id)
        if receipt is None:
            findings.append(
                Finding(
                    path.name,
                    "no-write-receipt",
                    "no managed write of this task was recorded on this machine. Make the "
                    "change through the AgentJobs MCP tools, API or CLI, or re-stage after "
                    "doing so.",
                    task_id,
                )
            )
        elif receipt.content_hash != content_hash(data):
            findings.append(
                Finding(
                    path.name,
                    "receipt-mismatch",
                    "the staged content differs from the last managed write of this task, "
                    "so it was edited outside AgentJobs. Re-apply the change through a "
                    "managed operation.",
                    task_id,
                )
            )
    return findings


#: The wording the override must carry, so a reader of a shell history can tell an
#: emergency repair from routine work.
OVERRIDE_ENV = "AGENTJOBS_ALLOW_DIRECT_WRITE_REASON"


def override_reason(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The maintainer's stated reason for bypassing the staged gate, if any.

    Deliberately awkward: it takes a reason, it is never a bare flag, and the reason
    is printed with every bypassed path. An agent following its guidance never sets
    it, because the guidance says to diagnose a failing managed operation rather than
    route around it.
    """
    import os

    source = os.environ if env is None else env
    reason = (source.get(OVERRIDE_ENV) or "").strip()
    return reason or None
