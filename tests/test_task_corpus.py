"""The corpus checks: every record loads, nothing dangles, nobody is quoted.

This is the safety net for the schema migration (task-052). Any change to
src/agentjobs/models_v2.py that breaks loading of an existing record fails here with the
exact record and validation error, and any file that lost its `schema: 2` stamp is named
rather than silently treated as v1.

**Every check here now reads the store** (task-311, then task-380). There used to be a
second kind, parametrised per file and about the *file format*; it collected nothing once
the frozen records left the checkout, and a test that collects nothing is not a check.
`tests/corpus_source.py` says where each of those guarantees went. Everything here is
about *the backlog*, and reads whichever store holds it.

**They skipped, silently, from task-311 until task-411**: an autouse fixture hid the
machine's store from every test. They now read it through a home captured before that
fixture runs, and **a store that cannot be read fails them** rather than skipping.
`tests/test_corpus_source.py` asserts both halves, so the silence cannot come back
unnoticed. `corpus_source` says how, and how a machine with no store opts out.

**They read a live store, so they couple branches.** A record filed with a bad pointer or
a verbatim quotation turns every gate on this machine red, with nothing in any diff to
explain it; the failure names the task, and the fix is to the record, not to the branch.
Seen from the gate's side that is task-409.
"""

from __future__ import annotations

import subprocess
from pathlib import PurePosixPath, PureWindowsPath

from agentjobs.models_v2 import DeliverableStatus, Lifecycle, SCHEMA_VERSION, Task, load_task
from agentjobs.quotation import scan_task

import corpus_source

REPO_ROOT = corpus_source.REPO_ROOT


def is_absolute(path: str) -> bool:
    """True for a path anchored outside the repository, in either OS's spelling."""
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def ignored_by_git(paths: set[str]) -> set[str]:
    """Return the subset of `paths` git ignores, i.e. generated rather than checked in.

    Deliberately byte-oriented: `text=True` would translate each `\\n` to `\\r\\n` on
    Windows, git would receive every path with a trailing carriage return, and it
    would echo back names that match nothing here. Falls back to treating nothing as
    generated when git cannot answer -- outside a checkout, the stricter behaviour is
    the safe one.
    """
    candidates = sorted(path for path in paths if not is_absolute(path))
    if not candidates:
        return set()

    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        input="\n".join(candidates).encode("utf-8"),
        cwd=REPO_ROOT,
        capture_output=True,
    )
    # 0: at least one path is ignored. 1: none are. Anything else: git could not answer.
    if result.returncode not in (0, 1):
        return set()

    lines = result.stdout.decode("utf-8", errors="replace").splitlines()
    return {line.strip().replace("\\", "/") for line in lines if line.strip()}


RETIRED_RECORDS = PurePosixPath("tasks") / corpus_source.PROJECT_ID


def retired_record_id(path: str) -> str | None:
    """The task id a pointer at one of this project's retired record files names, if any."""
    candidate = PurePosixPath(path.replace("\\", "/"))
    if candidate.parent != RETIRED_RECORDS or candidate.suffix != ".yaml":
        return None
    return candidate.stem


def checkable_pointers(backlog: list[Task]) -> list[tuple[str, str]]:
    """The `(task id, path)` pairs whose target the filesystem is then asked about.

    Everything exempt is dropped here rather than at the assertion, so the rules are
    one thing to read and a synthetic backlog can exercise them without a checkout.
    """
    known = {task.id for task in backlog}
    open_tasks = [task for task in backlog if task.lifecycle is not Lifecycle.CLOSED]
    promised = {
        deliverable.path.rstrip("/")
        for task in open_tasks
        for deliverable in task.deliverables
        if deliverable.status is not DeliverableStatus.DONE
    }

    pointers: list[tuple[str, str]] = []
    for task in open_tasks:
        for pointer in task.spec.context:
            path = pointer.path.rstrip("/")
            if "://" in path or any(character in path for character in "*{}<>"):
                continue
            if path in promised:
                continue
            # An absolute path is the validator's `absolute-path` rule to report, and
            # checking it here would ask whether one machine happens to have that
            # directory -- `REPO_ROOT / "C:/elsewhere"` resolves to `C:/elsewhere`.
            if is_absolute(path):
                continue
            record = retired_record_id(path)
            if record is not None:
                if record not in known:
                    pointers.append((task.id, path))
                continue
            pointers.append((task.id, path))
    return pointers


def test_corpus_is_not_empty() -> None:
    """Guard against the corpus silently emptying.

    Counted through the resolved source rather than by globbing a directory: a directory
    that has moved and a corpus that has been migrated look identical from a glob, and
    only one of them is a fault.
    """
    tasks = corpus_source.backlog()
    assert len(tasks) >= 20, (
        f"expected the task corpus to contain at least 20 records, found {len(tasks)} -- "
        "did a directory move, or a cutover go wrong, without this test being updated?"
    )


def test_every_record_carries_the_stamp_and_round_trips() -> None:
    """The stamp and the lossless round trip, asserted over rows rather than files.

    This used to be parametrised over every task file and it took its stamp straight
    from the YAML. task-380 retired those files, at which point it collected no cases
    at all -- so it is asked of the records instead, which is where the corpus is.

    The two halves land differently now, and only one of them is fully covered here.
    The *stamp* is a `CHECK` constraint the database enforces on every write
    (docs/storage-sqlite.md section 3), so this is a second opinion on it rather than
    the enforcement. The *round trip* is the real assertion: a record that does not
    survive being dumped and reloaded breaks the export an operator recovers a corpus
    with, and nothing in the database can notice that.
    """
    for task in corpus_source.backlog():
        assert task.schema_version == SCHEMA_VERSION, (
            f"{task.id} is not at schema {SCHEMA_VERSION} -- a record below it is "
            "treated as v1 and refused by the loader"
        )

        dumped = task.model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude={"display_status"}
        )
        reparsed = load_task(dumped, source=task.id)
        assert (
            reparsed.model_dump(
                mode="json", by_alias=True, exclude_none=True, exclude={"display_status"}
            )
            == dumped
        ), f"{task.id} does not survive a serialize/deserialize round trip"


def test_agentjobs_task_ids_and_relationships_are_not_dangling() -> None:
    """The durable roadmap must not point at records that do not exist."""
    tasks = corpus_source.backlog()
    ids = [task.id for task in tasks]
    assert len(ids) == len(set(ids)), "AgentJobs task ids must be unique"

    known = set(ids)
    for task in tasks:
        if task.parent is not None:
            assert task.parent in known, f"{task.id} has missing parent {task.parent}"
        for dependency in task.dependencies:
            assert dependency.task in known, (
                f"{task.id} has missing {dependency.type.value} dependency " f"{dependency.task}"
            )


def test_agentjobs_context_paths_exist() -> None:
    """Read-this-first pointers are useful only while their target still exists.

    **A file an open task exists in order to create is exempt, whichever task points
    at it.** A pending deliverable is a promise the backlog has already made, so a
    pointer at one is a forward reference rather than rot. task-002 is the single-task
    case -- it names an untracked plugin manifest as both the thing to read and the
    thing to produce -- and requiring that to exist made this test pass only in a clone
    where someone had created the file by hand, and fail in every worktree and every
    fresh clone. That is a test asserting the state of one developer's disk rather than
    the state of the repository.

    The exemption spans tasks because a deliverable arrives on one branch and is cited
    from several records at once (task-345). Eleven pointers across eight records named
    the capture control task-346 was building; every branch that was not task-346's
    therefore gated red on work nobody had done wrong, and the repository had no green
    branch until an unrelated one merged. Scoping the exemption to the pointer's own
    record made the gate a statement about which branch you happened to be standing on.
    It stays honest at the other end: the exempting task has to still be open, so a
    promise that is abandoned or closed undelivered puts its pointers straight back
    under the check.

    Generated output is exempt for the same reason, arriving by a different door:
    `src/agentjobs/frontend_dist/` exists only after `npm run build`, so a pointer at
    it passed in a clone where someone had built the frontend and failed in every
    fresh worktree. Worse, the gate builds it -- so the first run failed and the
    second passed.

    Every bad pointer is reported at once. Stopping at the first turns corpus rot into
    one fix per full gate run, and the gate takes four and a half minutes.

    **A closed record's pointers are history, not a promise** (task-411). A pointer exists
    to orient somebody about to do the work, and once the work is done there is nobody
    left to orient. Holding closed records to it would mean every file deletion edits the
    closed tasks that once cited the file -- rewriting what their worker was actually told
    -- and it would couple a branch's gate to records nobody is working.

    **A pointer at a retired record file names a record, and is checked as one.** Before
    task-380 a task was a file, so "read task-190 first" was spelled
    `tasks/agentjobs/task-190.yaml`. The file is gone and the record is not: it is a row
    `agentjobs show` reads. Such a pointer passes when the record it names is in the
    backlog, and fails when it is not -- which is the dangling this check exists to catch.
    """
    pointers = checkable_pointers(corpus_source.backlog())

    generated = ignored_by_git({path for _, path in pointers})
    missing = [
        f"{task_id} -> {path}"
        for task_id, path in pointers
        if path not in generated and not (REPO_ROOT / path).exists()
    ]

    assert not missing, "context pointers name paths that do not exist: " + ", ".join(missing)


def synthetic_task(task_id: str, *, context: list[str], delivers: list[tuple[str, str]]) -> Task:
    """A minimal open record, for exercising `checkable_pointers` without a backlog."""
    return load_task(
        {
            "schema": SCHEMA_VERSION,
            "id": task_id,
            "title": task_id,
            "created": "2026-01-01T00:00:00Z",
            "updated": "2026-01-01T00:00:00Z",
            "category": "chore",
            "lifecycle": "ready",
            "ball": "agent",
            "ball_reason": "available",
            "queue_position": 1,
            "spec": {
                "summary": task_id,
                "description": task_id,
                "context": [{"path": path, "why": "why"} for path in context],
            },
            "deliverables": [
                {"path": path, "note": "note", "status": status} for path, status in delivers
            ],
        },
        source=task_id,
    )


def test_a_pointer_at_another_open_tasks_pending_deliverable_is_exempt() -> None:
    """The cross-task half of the exemption, which the live corpus cannot pin down.

    `test_agentjobs_context_paths_exist` reads whatever the backlog happens to hold, so
    it stops exercising this the moment the branch carrying the file merges -- and the
    failure it guards against only appears when somebody is mid-flight. This states the
    rule directly: a cited task promises the file, so the pointer stands; nobody
    promises it, so the pointer is reported.
    """
    builder = synthetic_task("task-900", context=[], delivers=[("frontend/src/New.tsx", "pending")])
    citer = synthetic_task("task-901", context=["frontend/src/New.tsx"], delivers=[])

    assert checkable_pointers([builder, citer]) == []
    assert checkable_pointers([citer]) == [("task-901", "frontend/src/New.tsx")]


def test_a_delivered_promise_stops_exempting_the_pointers_that_cited_it() -> None:
    """`done` means the file is on disk, so the check takes over from the promise."""
    builder = synthetic_task("task-900", context=[], delivers=[("frontend/src/New.tsx", "done")])
    citer = synthetic_task("task-901", context=["frontend/src/New.tsx"], delivers=[])

    assert checkable_pointers([builder, citer]) == [("task-901", "frontend/src/New.tsx")]


def test_open_ui_tasks_do_not_target_legacy_templates() -> None:
    """New product UI work belongs to React; Jinja is compatibility/history only."""
    for task in corpus_source.backlog():
        if task.lifecycle is Lifecycle.CLOSED:
            continue

        paths = [pointer.path for pointer in task.spec.context]
        paths.extend(deliverable.path for deliverable in task.deliverables)
        assert not any(
            path.startswith("src/agentjobs/api/templates") for path in paths
        ), f"{task.id} still directs open UI work to the legacy template tree"

        current_summary = task.spec.summary.lower()
        assert (
            "web ui is server-rendered" not in current_summary
        ), f"{task.id} presents server rendering as the current UI"


def test_no_task_record_quotes_a_person_verbatim() -> None:
    """The paraphrase rule, enforced over the backlog before a record reaches `main`.

    The author-time half of task-376. `agentjobs.record_check` warns whoever wrote the
    quotation while they are still in context, and `agentjobs.sqlstore.importer` refuses
    an import carrying one -- but the importer fires long after the author has gone, and
    a warning refuses nothing. This is the tier in between: a record written with a
    verbatim quote of a person fails the gate rather than reaching a public remote.

    It reads the store, so nothing in a diff selects it. That is task-409's subject;
    until it is settled, the unqualified gate is what this is guaranteed by.

    The failure names regions, not remarks. Reproducing the quotation in a test failure
    would put it in CI output, which is the same mistake one layer out; run
    `agentjobs quotations` to see what actually tripped, and `agentjobs redact` to fix
    it.
    """
    offenders = [
        f"{task.id}: {remark.locator()}"
        for task in corpus_source.backlog()
        for remark in scan_task(task)
    ]

    assert not offenders, (
        "task records quote a person verbatim instead of paraphrasing them "
        "(ALLAGENTS.md, 'Paraphrase a person, never quote them'). Run "
        "`agentjobs quotations` to read them and `agentjobs redact` to replace each "
        "with a paraphrase:\n  " + "\n  ".join(offenders)
    )
