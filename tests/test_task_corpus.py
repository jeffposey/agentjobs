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

**Which means everything here currently skips, and this file is not enforcing anything.**
An autouse fixture hides the machine's store from every test; the measurement, why it was
not fixed in passing, and what it costs to fix are in
`corpus_source.WHY_THESE_SKIP` and task-411. Do not cite a check in this module as
enforcement until that closes.
"""

from __future__ import annotations

import subprocess
from pathlib import PurePosixPath, PureWindowsPath

import pytest

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


def agentjobs_tasks() -> list[Task]:
    """The product backlog, from whichever backend holds it. Skips if unreadable."""
    tasks = corpus_source.product_tasks()
    if tasks is None:
        pytest.skip(
            "this repository's own backlog could not be read from either backend; "
            "`agentjobs storage status` says where it is"
        )
    return tasks


def corpus_tasks() -> list[Task]:
    """Every record: the product backlog plus the fixture data beside it."""
    tasks = corpus_source.all_tasks()
    if tasks is None:
        pytest.skip("this repository's own backlog could not be read from either backend")
    return tasks


def test_corpus_is_not_empty() -> None:
    """Guard against the corpus silently emptying, whichever backend holds it.

    Counted through the resolved source rather than by globbing a directory: a directory
    that has moved and a corpus that has been migrated look identical from a glob, and
    only one of them is a fault.
    """
    tasks = corpus_tasks()
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
    for task in corpus_tasks():
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
    tasks = agentjobs_tasks()
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

    A task's own pending deliverables are exempt. A task may legitimately point at a
    file it exists in order to create -- task-002 does exactly that, naming an
    untracked plugin manifest as both the thing to read and the thing to produce.
    Requiring it to exist made this test pass only in a clone where someone had
    already created the file by hand, and fail in every worktree and every fresh
    clone. That is a test asserting the state of one developer's disk rather than the
    state of the repository.

    Generated output is exempt for the same reason, arriving by a different door:
    `src/agentjobs/frontend_dist/` exists only after `npm run build`, so a pointer at
    it passed in a clone where someone had built the frontend and failed in every
    fresh worktree. Worse, the gate builds it -- so the first run failed and the
    second passed.

    Every bad pointer is reported at once. Stopping at the first turns corpus rot into
    one fix per full gate run, and the gate takes four and a half minutes.
    """
    pointers: list[tuple[str, str]] = []
    for task in agentjobs_tasks():
        pending_deliverables = {
            deliverable.path.rstrip("/")
            for deliverable in task.deliverables
            if deliverable.status is not DeliverableStatus.DONE
        }
        for pointer in task.spec.context:
            path = pointer.path.rstrip("/")
            if "://" in path or any(character in path for character in "*{}<>"):
                continue
            if path in pending_deliverables:
                continue
            # An absolute path is the validator's `absolute-path` rule to report, and
            # checking it here would ask whether one machine happens to have that
            # directory -- `REPO_ROOT / "C:/elsewhere"` resolves to `C:/elsewhere`.
            if is_absolute(path):
                continue
            pointers.append((task.id, path))

    generated = ignored_by_git({path for _, path in pointers})
    missing = [
        f"{task_id} -> {path}"
        for task_id, path in pointers
        if path not in generated and not (REPO_ROOT / path).exists()
    ]

    assert not missing, "context pointers name paths that do not exist: " + ", ".join(missing)


def test_open_ui_tasks_do_not_target_legacy_templates() -> None:
    """New product UI work belongs to React; Jinja is compatibility/history only."""
    for task in agentjobs_tasks():
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
        f"{task.id}: {remark.locator()}" for task in corpus_tasks() for remark in scan_task(task)
    ]

    assert not offenders, (
        "task records quote a person verbatim instead of paraphrasing them "
        "(ALLAGENTS.md, 'Paraphrase a person, never quote them'). Run "
        "`agentjobs quotations` to read them and `agentjobs redact` to replace each "
        "with a paraphrase:\n  " + "\n  ".join(offenders)
    )
