"""A whole dispatch cycle on a project served from the database rather than from files.

Dispatch is the product, and it is also the one family that keeps a direct-to-storage
path -- ``store_factory.dispatch_manager_for`` is a documented exception to "only the
server opens the database". So at the moment this project cut over it was simultaneously
the least tested and the most exceptional part of the migration, which is the gap
task-378 exists to close.

Every case here runs against a project that has been through ``cut_over``. The four
things that change for such a project, and that nothing else asserts end to end:

**The dispatch entry has nowhere on disk to be.** It is a row. A test that read it back
through the same manager would pass against an implementation that wrote a file and
never looked at the database, so the entries are read back through a *second* store over
the same database.

**The clean-tree check stops excusing the tasks directory.** ``task_file_exclusions``
returning ``[]`` is already asserted in ``test_task_files_retired.py``; what is asserted
here is the consequence -- a real change under ``tasks/`` now refuses a dispatch, which
is the coverage task-182 had to give up and task-311 bought back.

**The dispatcher has nothing to commit.** ``commit_task_record`` answers that as a fact
about the store rather than by failing to find a file, and the checkout is clean at the
end of a cycle that used to dirty it twice.

**The epic walk has to read the store.** This is the one worth designing carefully. The
YAML files survive a cutover as a frozen copy, so a walk that polled files instead of
rows would watch a child that never changes and would look, from outside, exactly like a
child that is still working. Every walk case below therefore moves its child in the
database *while leaving the file on disk saying the opposite*, and asserts on what the
walk concluded.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest
import yaml

from agentjobs.cutover import cut_over
from agentjobs.dispatch.config import assert_dispatch_permitted
from agentjobs.dispatch.epic import ChildVerdict, WalkSettings, WalkStop, walk_epic
from agentjobs.dispatch.guards import (
    DirtyTreeError,
    DispatchRequest,
    DispatchTrigger,
    dispatch_task,
)
from agentjobs.dispatch.ledger import find_run
from agentjobs.dispatch.record_commit import commit_task_record, task_file_exclusions
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    DispatchMode,
    DispatchOutcome,
    DispatchPosture,
    Lifecycle,
    LogEntryType,
    Outcome,
)
from agentjobs.projects import Project, ProjectRegistry
from agentjobs.sqlstore import SqlTaskStore
from agentjobs.storage import TaskStorage
from agentjobs.storage_config import load_storage_settings
from agentjobs.store_factory import (
    close_databases,
    dispatch_manager_for,
    open_database,
    server_process,
)

PROJECT_CONFIG: Dict[str, object] = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}

TERMINAL_OUTCOMES = frozenset(DispatchOutcome)
"""Every outcome a supervisor may reach. The assertion is that it reached one."""

STUB = "print('started', flush=True)\n"
"""A runner that starts and exits.

Nothing here is about what the agent does; every assertion is about what AgentJobs wrote
before and after it, which is the half the storage backend can change.
"""


# ----- the world --------------------------------------------------------------


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def build_project(root: Path) -> None:
    """A git-clean project whose ``tasks/`` directory is **tracked**.

    Tracked on purpose. A project that gitignores its task files cannot show the
    difference the exclusion made, and the project this migration is for tracked them --
    that is the whole reason the dispatcher had to commit them and the clean-tree check
    had to excuse them.
    """
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(PROJECT_CONFIG), encoding="utf-8"
    )
    (root / "tasks").mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    (root / ".gitignore").write_text(".agentjobs/\n", encoding="utf-8")
    git(root, "init")
    git(root, "config", "user.email", "t@t.t")
    git(root, "config", "user.name", "t")
    git(root, "add", "--", "README.md", ".gitignore")
    git(root, "commit", "-m", "init")


def write_dispatch_config(home: Path, tmp_path: Path, *, require_clean_tree: bool = False) -> None:
    stub = tmp_path / "stub_runner.py"
    stub.write_text(STUB, encoding="utf-8")
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "api_base": "http://127.0.0.1:1",
                "runners": {
                    "fake": {"argv": [sys.executable, str(stub), "{prompt}"], "actor": "claude"}
                },
                "projects": {
                    "sandbox": {
                        "enabled": True,
                        "runner": "fake",
                        "require_clean_tree": require_clean_tree,
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def seed(root: Path, *, title: str = "Dispatchable", parent: Optional[str] = None) -> str:
    """A ready task whose newest entry is a human's, written as a file, pre-cutover."""
    manager = TaskManager(TaskStorage(root / "tasks"))
    task = manager.create_task(
        title=title,
        category="general",
        summary="A task to dispatch.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
        parent=parent,
    )
    manager.add_log_entry(task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go.")
    return task.id


class World:
    """A registered, cut-over, git-clean project and the handles a test needs."""

    def __init__(self, root: Path, home: Path, project: Project) -> None:
        self.root = root
        self.home = home
        self.project = project

    @property
    def manager(self) -> TaskManager:
        """What the dispatch subsystem itself would get. The exception, exercised."""
        return dispatch_manager_for(self.project)

    def another_writer(self) -> TaskManager:
        """A manager over a *separate* store on the same database.

        Every "did the walk really read the store" assertion depends on the write and
        the read not being the same object: a manager that answered from what it last
        wrote would satisfy them without a database being involved at all.
        """
        settings = load_storage_settings()
        with server_process():
            store = SqlTaskStore(
                open_database(settings.database_for(self.project.id)), self.project.id
            )
            store.ensure_project(root=str(self.project.root))
            return TaskManager(store)

    def file_on_disk(self, task_id: str) -> Dict[str, Any]:
        """The frozen YAML the cutover left behind, parsed.

        Not a store. This is what a reader that still resolved a task by composing a
        directory would see, and several assertions below turn on it disagreeing with
        the database.
        """
        path = self.root / "tasks" / f"{task_id}.yaml"
        assert path.exists(), f"no file for {task_id} under {self.root / 'tasks'}"
        document: Dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        return document


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    monkeypatch.delenv("AGENTJOBS_DATABASE", raising=False)
    monkeypatch.delenv("AGENTJOBS_TASKS_DIR", raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    close_databases()

    root = tmp_path / "sandbox"
    build_project(root)
    project = Project(id="sandbox", name="Sandbox", root=root)
    ProjectRegistry(home=home).add(root, project_id="sandbox")
    yield World(root, home, project)
    settle_every_run()
    close_databases()


def settle_every_run() -> None:
    """Wait for each dispatch supervisor thread to finish before the database closes.

    A real dispatch starts a real process and a thread that waits on it and then writes
    the terminal entry. The thread outlives the test body, so without this the teardown
    closes the database out from under a write that is still in progress -- which
    surfaces as an unhandled thread exception attributed to whichever test happened to
    be running, and is a fact about the harness rather than about the application.
    """
    for thread in threading.enumerate():
        if thread.name.startswith("dispatch-") and thread.is_alive():
            thread.join(timeout=30)


def migrate(world: World) -> None:
    """Cut the project over, then commit the files it left frozen on disk.

    Committing them matters: everything below asserts on a *clean* checkout, and a
    cutover does not delete the files it imported -- deliberately, because they are what
    a rollback would otherwise have to reconstruct.
    """
    result = cut_over(world.project, backfill_git=False, skip_backup=True)
    assert result.ok, result.verified.render()
    git(world.root, "add", "--", "tasks")
    if dirty(world.root):
        git(world.root, "commit", "-m", "chore: the records as they stood at the cutover")
    assert load_storage_settings().on_sqlite("sandbox")
    assert dirty(world.root) == []


def edit_the_frozen_copy(world: World, task_id: str) -> Path:
    """Make a real, tracked change under ``tasks/``, as a person editing a record would.

    Deliberately a task file rather than some other file in that directory: the
    exclusion was written to cover exactly these, so a test that dirtied a README beside
    them would not be exercising the exception at all.
    """
    frozen = world.root / "tasks" / f"{task_id}.yaml"
    assert frozen.exists(), f"the cutover left no frozen copy of {task_id}"
    frozen.write_text(
        frozen.read_text(encoding="utf-8") + "\n# somebody edited this\n", encoding="utf-8"
    )
    return frozen


def dirty(root: Path) -> List[str]:
    out = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def start(world: World, task_id: str, **kwargs: Any) -> Any:
    return dispatch_task(
        manager=world.manager,
        project=world.project,
        project_config=PROJECT_CONFIG,
        request=DispatchRequest(task_id=task_id, **kwargs),
        home=world.home,
        api_base="http://127.0.0.1:1",
    )


# ----- part one, step 1: the dispatch entry is a row --------------------------


class TestTheDispatchEntryLandsInTheStore:
    """ac-1. What ``dispatch_task`` writes when there is no file to write it to."""

    def test_a_dispatch_passes_its_guards_and_starts_a_run(
        self, world: World, tmp_path: Path
    ) -> None:
        migrate(world)
        write_dispatch_config(world.home, tmp_path)
        task_id = seed_after_cutover(world)

        handle = start(world, task_id)

        assert handle.run_id.startswith("run_")
        assert handle.task_id == task_id

    def test_the_entry_is_readable_through_a_store_that_did_not_write_it(
        self, world: World, tmp_path: Path
    ) -> None:
        """The entry is in the database, not in the manager that recorded it."""
        migrate(world)
        write_dispatch_config(world.home, tmp_path)
        task_id = seed_after_cutover(world)

        handle = start(world, task_id)

        task = world.another_writer().get_task(task_id)
        assert task is not None
        entries = [entry for entry in task.log if entry.type is LogEntryType.DISPATCH]
        assert len(entries) == 1
        assert entries[0].data["run_id"] == handle.run_id

    def test_the_claim_that_precedes_the_spawn_is_in_the_store_too(
        self, world: World, tmp_path: Path
    ) -> None:
        """The claim is written before the process starts, and it is a row as well."""
        migrate(world)
        write_dispatch_config(world.home, tmp_path)
        task_id = seed_after_cutover(world)

        start(world, task_id)

        task = world.another_writer().get_task(task_id)
        assert task is not None
        assert task.lifecycle is Lifecycle.ACTIVE
        assert task.assignment.owner == "claude"

    def test_the_run_row_is_derived_from_the_entry(self, world: World, tmp_path: Path) -> None:
        """The ledger row and the log entry are two views of one dispatch.

        The row is machine-local and the entry is a database row, so after the cutover
        they no longer share any storage at all. Asserting they still agree is the
        whole of "with the run row derived from it": ``run_id``, the task, the argv the
        process was handed, and the head it started from.
        """
        migrate(world)
        write_dispatch_config(world.home, tmp_path)
        task_id = seed_after_cutover(world)

        handle = start(world, task_id)

        task = world.another_writer().get_task(task_id)
        assert task is not None
        entry = [e for e in task.log if e.type is LogEntryType.DISPATCH][-1]
        row = find_run(world.home, handle.run_id)
        assert row.task_id == task_id
        assert row.project_id == "sandbox"
        assert row.run_id == entry.data["run_id"]
        assert row.argv == entry.data["argv"]
        assert entry.data["cwd"] == str(world.root)

    def test_nothing_was_written_into_the_checkout(self, world: World, tmp_path: Path) -> None:
        """The property the whole migration is for, on the dispatch path specifically."""
        migrate(world)
        write_dispatch_config(world.home, tmp_path)
        task_id = seed_after_cutover(world)

        start(world, task_id)

        assert dirty(world.root) == []


def seed_after_cutover(world: World) -> str:
    """A ready, human-clocked task created *in the database*.

    Created through the dispatch manager rather than through a file, because a task
    seeded as YAML after the cutover would not exist as far as the store is concerned --
    which is the migration's central fact and worth honouring in the fixtures too.
    """
    manager = world.manager
    task = manager.create_task(
        title="Dispatchable",
        category="general",
        summary="A task to dispatch.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
    )
    manager.add_log_entry(task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go.")
    return task.id


# ----- part one, step 2: the clean-tree check sees the whole tree -------------


class TestTheCleanTreeCheckCoversTheTasksDirectory:
    """The coverage task-182 gave up, asserted as a refusal rather than as a list.

    ``test_task_files_retired.py`` asserts ``task_file_exclusions`` returns ``[]``. That
    is the mechanism; this is the behaviour, and the two can disagree -- a caller that
    computed the exclusions and then ignored them would pass that test and fail these.
    """

    def test_a_change_under_tasks_now_refuses_a_dispatch(
        self, world: World, tmp_path: Path
    ) -> None:
        write_dispatch_config(world.home, tmp_path, require_clean_tree=True)
        task_id = seed(world.root)
        migrate(world)
        edit_the_frozen_copy(world, task_id)

        with pytest.raises(DirtyTreeError) as refusal:
            start(world, task_id)

        assert "tasks/" in str(refusal.value).replace("\\", "/")

    def test_the_same_change_was_invisible_before_the_cutover(
        self, world: World, tmp_path: Path
    ) -> None:
        """The contrast, so the case above is a change in behaviour and not a tautology.

        On files the identical edit is excused, because dispatch dirtied that directory
        itself and refusing on its own writes refused every dispatch.
        """
        write_dispatch_config(world.home, tmp_path, require_clean_tree=True)
        task_id = seed(world.root)
        git(world.root, "add", "--", "tasks")
        git(world.root, "commit", "-m", "chore: the record")
        edit_the_frozen_copy(world, task_id)

        handle = start(world, task_id)

        assert handle.run_id
        assert task_file_exclusions(world.manager) == [world.project.tasks_dir()]

    def test_the_gate_ignores_a_change_it_should_not_see(
        self, world: World, tmp_path: Path
    ) -> None:
        """A migrated project with a clean tree still dispatches.

        Without this, the two cases above are equally satisfied by a gate that refuses
        everything.
        """
        write_dispatch_config(world.home, tmp_path, require_clean_tree=True)
        task_id = seed(world.root)
        migrate(world)

        assert start(world, task_id).run_id

    def test_a_change_anywhere_else_still_refuses(self, world: World, tmp_path: Path) -> None:
        """Unchanged behaviour, kept honest: the gate did not simply stop looking."""
        migrate(world)
        write_dispatch_config(world.home, tmp_path, require_clean_tree=True)
        task_id = seed_after_cutover(world)
        (world.root / "README.md").write_text("edited\n", encoding="utf-8")

        with pytest.raises(DirtyTreeError):
            start(world, task_id)


# ----- part one, step 3: the terminal entry, and the commit that is not needed -


class TestTheRunEndsWithoutTouchingTheCheckout:
    """The dispatcher's two writes, and the tidy-up that no longer has anything to do."""

    def test_the_terminal_entry_lands_in_the_store(self, world: World, tmp_path: Path) -> None:
        """Written by the run's own supervisor thread, after the process has exited.

        Not by the test. This is the entry the migration's timing argument is about --
        it is written after the session's last commit, by definition -- so having the
        test write it would skip the part that was ever in doubt.
        """
        migrate(world)
        write_dispatch_config(world.home, tmp_path)
        task_id = seed_after_cutover(world)
        handle = start(world, task_id)

        settle_every_run()

        task = world.another_writer().get_task(task_id)
        assert task is not None
        results = [e for e in task.log if e.type is LogEntryType.DISPATCH_RESULT]
        assert len(results) == 1
        assert results[0].data["run_id"] == handle.run_id
        # The outcome is whatever the supervisor concluded -- this runner exits
        # without handing off, which is a real terminal outcome and not a failure.
        assert DispatchOutcome(results[0].data["outcome"]) in TERMINAL_OUTCOMES
        assert find_run(world.home, handle.run_id).outcome == results[0].data["outcome"]

    def test_committing_the_record_reports_there_is_nothing_to_commit(
        self, world: World, tmp_path: Path
    ) -> None:
        """Answered as a fact about the store, and not as a git failure to find a file."""
        migrate(world)
        write_dispatch_config(world.home, tmp_path)
        task_id = seed_after_cutover(world)
        start(world, task_id)

        outcome = commit_task_record(world.manager, task_id, subject="chore: the record")

        assert outcome.committed is False
        assert outcome.path is None
        assert "not a file in this checkout" in outcome.detail

    def test_the_checkout_is_clean_at_the_end_of_the_cycle(
        self, world: World, tmp_path: Path
    ) -> None:
        """Both of the dispatcher's writes, and nothing in ``git status`` afterwards.

        This is the property task-203 was opened for. Before the cutover the answer was
        "the dispatcher commits its own record"; now it is "there was never a file", and
        the observable is the same either way, which is why it is asserted rather than
        argued.
        """
        migrate(world)
        write_dispatch_config(world.home, tmp_path)
        task_id = seed_after_cutover(world)
        start(world, task_id)
        settle_every_run()
        commit_task_record(world.manager, task_id, subject="chore: the record")

        assert dirty(world.root) == []


# ----- part one, step 5: the walk watches the record, in the store ------------


class WalkDispatcher:
    """A stand-in for ``dispatch_task`` that claims the child, as the real one does.

    Claiming is the only thing the walk depends on the dispatcher for -- without it the
    same child is offered again on the next pass. Everything else happens in another
    process.
    """

    def __init__(self, manager: TaskManager) -> None:
        self.manager = manager
        self.started: List[str] = []

    def __call__(
        self, *, manager: Any, project: Any, project_config: Any, request: Any, **_: Any
    ) -> Any:
        self.started.append(request.task_id)
        assert request.trigger is DispatchTrigger.CHILD
        assert request.on_behalf_of_parent is True
        task = self.manager.get_task(request.task_id)
        assert task is not None
        if task.lifecycle is Lifecycle.READY:
            self.manager.claim_task(task.id, agent="claude")

        class Handle:
            run_id = f"run_{request.task_id}"

        return Handle()


def epic(world: World, parent_id: Optional[str] = None) -> str:
    """An active parent whose dispatch a human authorised, all of it in the store.

    ``parent_id`` names a task that already exists -- the case where the epic was
    written as a file before the cutover and imported, which is what every real epic on
    this machine is.
    """
    manager = world.manager
    if parent_id is None:
        parent_id = manager.create_task(
            title="Parent",
            category="general",
            summary="An epic.",
            description="It has children.",
            lifecycle=Lifecycle.READY,
            actor="Jeff Posey",
        ).id
    written = manager.add_log_entry(
        parent_id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Work this epic."
    )
    manager.claim_task(parent_id, agent="claude")
    caused_by = [e for e in written.log if e.actor == "Jeff Posey" and e.type is LogEntryType.NOTE][
        -1
    ].id
    manager.record_dispatch(
        parent_id,
        actor="Jeff Posey",
        run_id="run_parent",
        agent="claude",
        runner="fake",
        mode=DispatchMode.BATCH,
        posture=DispatchPosture.AUTONOMOUS,
        argv=["fake"],
        cwd=str(world.root),
        git_head="0000000",
        caused_by=caused_by,
        trigger=DispatchTrigger.MANUAL,
    )
    return parent_id


def child_of(world: World, parent_id: str, title: str) -> str:
    manager = world.manager
    child = manager.create_task(
        title=title,
        category="general",
        summary=f"{title}.",
        description="A child of the epic.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
        parent=parent_id,
    )
    return child.id


class TestTheWalkWatchesTheStore:
    """ac-3. Asserted on what the walk observed, never on what the store holds.

    A cutover leaves the YAML files on disk, frozen at the moment of the import. So the
    failure this class is built to catch is not "the walk crashes" -- it is a walk that
    reads a file, sees a child that is forever ``ready``, and waits out its ceiling
    looking exactly like a walk watching a child that is still working.

    Every case therefore closes its child through a *second* manager over the database
    and leaves the file saying something else, then asserts the walk agreed with the
    database.
    """

    def drive(
        self,
        world: World,
        parent_id: str,
        moves: Dict[str, str],
        *,
        dispatcher: WalkDispatcher,
    ) -> Any:
        """Walk the epic, moving each child once, through another writer entirely."""
        writer = world.another_writer()
        clock = {"now": 0.0}
        done: set[str] = set()

        def tick(_seconds: float) -> None:
            clock["now"] += 1.0
            for child_id, move in moves.items():
                if child_id in done:
                    continue
                task = writer.get_task(child_id)
                if task is None or task.lifecycle is not Lifecycle.ACTIVE:
                    continue
                done.add(child_id)
                if move == "complete":
                    writer.close_task(child_id, actor="claude", outcome=Outcome.COMPLETED)
                elif move == "park":
                    writer.handoff(
                        child_id,
                        actor="claude",
                        ball=Ball.HUMAN,
                        ball_reason=BallReason.REVIEW,
                        ball_prompt="Look at this.",
                    )

        return walk_epic(
            manager=world.manager,
            project=world.project,
            project_config=PROJECT_CONFIG,
            parent_id=parent_id,
            home=world.home,
            api_base="http://127.0.0.1:1",
            settings=WalkSettings(poll_seconds=0.0, child_timeout_seconds=1000.0),
            dispatch=dispatcher,
            read_run_status=lambda run_id: "exited",
            sleep=tick,
            now=lambda: clock["now"],
        )

    def test_it_starts_a_child_and_watches_it_to_a_terminal_state(
        self, world: World, tmp_path: Path
    ) -> None:
        migrate(world)
        write_dispatch_config(world.home, tmp_path)
        parent_id = epic(world)
        first = child_of(world, parent_id, "First")
        second = child_of(world, parent_id, "Second")
        dispatcher = WalkDispatcher(world.manager)

        result = self.drive(
            world, parent_id, {first: "complete", second: "complete"}, dispatcher=dispatcher
        )

        assert result.stop is WalkStop.ALL_CHILDREN_DONE
        assert dispatcher.started == [first, second]
        assert [attempt.verdict for attempt in result.attempts] == [
            ChildVerdict.COMPLETED,
            ChildVerdict.COMPLETED,
        ]

    def test_the_state_it_watched_was_never_written_to_a_file(
        self, world: World, tmp_path: Path
    ) -> None:
        """The one that separates reading the store from reading the directory.

        The children were created after the cutover, so they have no file at all, and
        the parent's file is the frozen copy the import left. A walk that resolved a
        task by composing a directory could not have reached either -- so a green
        assertion here is evidence about *where* the walk read, not merely that it read.
        """
        write_dispatch_config(world.home, tmp_path)
        # The epic itself is a file first, imported by the cutover, which is what every
        # epic on the machine this task is for actually is.
        imported = seed(world.root, title="Parent")
        migrate(world)
        parent_id = epic(world, imported)
        first = child_of(world, parent_id, "First")
        dispatcher = WalkDispatcher(world.manager)

        result = self.drive(world, parent_id, {first: "complete"}, dispatcher=dispatcher)

        assert result.stop is WalkStop.ALL_CHILDREN_DONE
        assert [attempt.verdict for attempt in result.attempts] == [ChildVerdict.COMPLETED]
        # The child was created after the cutover, so it has no file to read at all.
        assert not (world.root / "tasks" / f"{first}.yaml").exists()
        # And the parent's frozen file still says what it said at the cutover -- it does
        # not know it was claimed, dispatched, or walked. That is what a file-reading
        # walk would have been answering from.
        assert world.file_on_disk(parent_id)["lifecycle"] == "ready"
        assert world.file_on_disk(parent_id).get("assignment", {}).get("owner") is None

    def test_a_parked_child_still_stops_the_walk(self, world: World, tmp_path: Path) -> None:
        """The stop rule is a property of the record, whichever storage holds it."""
        migrate(world)
        write_dispatch_config(world.home, tmp_path)
        parent_id = epic(world)
        first = child_of(world, parent_id, "First")
        second = child_of(world, parent_id, "Second")
        dispatcher = WalkDispatcher(world.manager)

        result = self.drive(
            world,
            parent_id,
            {first: "park", second: "complete"},
            dispatcher=dispatcher,
        )

        assert result.stop is WalkStop.CHILD_NEEDS_A_HUMAN
        assert dispatcher.started == [first]
        assert second not in dispatcher.started

    def test_it_never_closes_the_parent(self, world: World, tmp_path: Path) -> None:
        """Unchanged by the migration, and worth pinning where a storage change could
        plausibly have moved it."""
        migrate(world)
        write_dispatch_config(world.home, tmp_path)
        parent_id = epic(world)
        first = child_of(world, parent_id, "First")
        dispatcher = WalkDispatcher(world.manager)

        self.drive(world, parent_id, {first: "complete"}, dispatcher=dispatcher)

        parent = world.another_writer().get_task(parent_id)
        assert parent is not None
        assert parent.lifecycle is Lifecycle.ACTIVE


# ----- the guards themselves still resolve on a migrated project --------------


class TestTheDispatchGuardsResolveThroughTheStore:
    """A refusal that came from the wrong storage would be a false one."""

    def test_a_closed_task_is_refused_on_what_the_database_says(
        self, world: World, tmp_path: Path
    ) -> None:
        """The file says ``ready``; the database says closed. The database decides."""
        write_dispatch_config(world.home, tmp_path)
        task_id = seed(world.root)
        migrate(world)
        world.another_writer().close_task(task_id, actor="claude", outcome=Outcome.COMPLETED)

        assert world.file_on_disk(task_id)["lifecycle"] == "ready"
        with pytest.raises(Exception) as refusal:
            start(world, task_id)
        assert task_id in str(refusal.value)
        assert "closed" in str(refusal.value)

    def test_the_permission_check_is_unaffected_by_the_backend(
        self, world: World, tmp_path: Path
    ) -> None:
        migrate(world)
        write_dispatch_config(world.home, tmp_path)
        resolution = assert_dispatch_permitted("sandbox", world.home)
        assert resolution.settings.runner == "fake"
