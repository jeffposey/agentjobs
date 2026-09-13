"""A finish that survives its own failures: one gate retry, recovery from merge evidence (task-322).

**Real repositories, real merges, and real process deaths where it matters.** A crash is
simulated by raising ``KeyboardInterrupt`` at the exact line under test: it is a
``BaseException``, so nothing in the finisher catches it, the process-level state it
leaves behind -- a merge in git, an intent with no result, a directory with no ending --
is exactly what a killed process leaves, and the *next* call is a fresh
``finish_task`` reading only what is on disk. No in-memory object from the first attempt
is handed to the second.

The gate is a stub that records every invocation, so "the gate was not run again" is an
assertion about a count, not about a code path.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from agentjobs.actors import FINISHER
from agentjobs.dispatch import finish as finish_module
from agentjobs.dispatch.finish import (
    ESCALATED,
    FINISHED,
    FLAKY_TEST,
    INPUTS_CHANGED,
    GateAttempt,
    explain_red,
    receipt_vector,
    retry_selection,
)
from agentjobs.dispatch.finish_receipts import APPLIED, FinishReceipts
from agentjobs.models_v2 import Ball, BallReason, Lifecycle
import test_dispatch_finish
from test_dispatch_finish import (
    _interpreter,
    add_served_change,
    git,
    head,
    merged_into,
    publish_gate_scope,
    run,
)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """``test_dispatch_finish``'s real clone, branch, worktree and task, reused unchanged."""
    built: Dict[str, Any] = test_dispatch_finish.world.__pytest_wrapped__.obj(  # type: ignore[attr-defined]
        tmp_path, monkeypatch
    )
    return built


@pytest.fixture
def serving() -> Iterator[Any]:
    """``test_dispatch_finish``'s controllable version endpoint, reused unchanged."""
    yield from test_dispatch_finish.serving.__pytest_wrapped__.obj()  # type: ignore[attr-defined]


TESTS_DIR = Path(__file__).resolve().parent

GATE_STUB = """\
import json
import os
import pathlib
import subprocess
import sys

CALLS = pathlib.Path({calls!r})
CLONE = pathlib.Path({clone!r})
MODE = {mode!r}
STAGES = ["ruff", "pytest", "vitest"]

args = sys.argv[1:]
with CALLS.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"args": args}}) + chr(10))
run_dir = os.environ.get("AGENTJOBS_RUN_DIR")
if run_dir:
    if not args:
        selected = STAGES
    elif args[0] == "--only":
        selected = args[1].split(",")
    else:
        selected = STAGES[STAGES.index(args[1]):]
    with (pathlib.Path(run_dir) / "phases.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({{"kind": "gate_started", "stages": selected}}) + chr(10))


def red(test):
    print("FAILED " + test + " - AssertionError: it broke")
    print("Failed at stage 'pytest'.", file=sys.stderr)
    sys.exit(1)


first = len(CALLS.read_text(encoding="utf-8").splitlines()) == 1
if MODE == "flaky":
    if first:
        red("tests/test_flaky.py::test_sometimes")
    sys.exit(0)
if MODE == "always_red":
    red("tests/test_broken.py::test_always")
if MODE == "main_fix":
    if pathlib.Path("docs/fix.md").exists():
        sys.exit(0)
    target = CLONE / "docs" / "fix.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("the correction main needed", encoding="utf-8")
    subprocess.run(["git", "-C", str(CLONE), "add", "--", "docs/fix.md"], check=True)
    subprocess.run(
        ["git", "-C", str(CLONE), "commit", "-m", "fix: the correction"],
        check=True,
        capture_output=True,
    )
    red("tests/test_validate.py::TestRealCorpus::test_no_task_file_is_unloadable_or_points_at_nothing")
if MODE in ("corpus_fix", "corpus_unrelated"):
    sys.path.insert(0, {tests!r})
    from support import task_store
    from agentjobs.manager import TaskManager
    from agentjobs.models_v2 import Lifecycle

    manager = TaskManager(task_store(pathlib.Path({tasks!r}), project_id="demo"))
    if any(task.title == "the correction" for task in manager.list_tasks()):
        sys.exit(0)
    manager.create_task(
        title="the correction",
        category="infrastructure",
        summary="Written through the API while the gate was running.",
        description="The record the first attempt's corpus check was missing.",
        lifecycle=Lifecycle.READY,
    )
    if MODE == "corpus_fix":
        red("tests/test_task_corpus.py::test_agentjobs_context_paths_exist")
    red("tests/test_other.py::test_unrelated")
sys.exit(0)
"""


def install_gate(world: Dict[str, Any], mode: str) -> Path:
    """Commit a recording stub gate to the base. Returns the file its calls go to."""
    root: Path = world["root"]
    calls = root.parent / f"gate-calls-{mode}.jsonl"
    (root / "scripts" / "check.py").write_text(
        GATE_STUB.format(
            calls=str(calls),
            clone=root.as_posix(),
            mode=mode,
            tests=TESTS_DIR.as_posix(),
            tasks=(root / "tasks").as_posix(),
        ),
        encoding="utf-8",
    )
    git(root, "add", "--", "scripts/check.py")
    git(root, "commit", "-m", f"chore: a {mode} gate")
    return calls


def gate_calls(calls: Path) -> List[List[str]]:
    if not calls.is_file():
        return []
    return [json.loads(line)["args"] for line in calls.read_text(encoding="utf-8").splitlines()]


def entries_with(task: Any, key: str) -> List[Any]:
    return [entry for entry in task.log if key in entry.data]


def merge_entry(task: Any) -> Any:
    return next(entry for entry in reversed(task.log) if entry.data.get("finish_step") == "merge")


def merges_on_main(root: Path) -> List[str]:
    listing = git(root, "rev-list", "--merges", "main").stdout
    return [line for line in listing.splitlines() if line.strip()]


# ----- the one retry (gate-1, gate-5) -----------------------------------------------


class TestAFlakeIsRetriedOnceAndRecorded:
    def test_a_red_stage_that_passes_on_retry_merges_and_says_it_was_a_flake(
        self, world: Dict[str, Any]
    ) -> None:
        calls = install_gate(world, "flaky")

        result = run(world)

        assert result.outcome == FINISHED, result.render()
        assert gate_calls(calls) == [[], ["--from", "pytest"]]
        task = world["manager"].get_task(world["task_id"])
        flakes = entries_with(task, "flaky_test")
        assert len(flakes) == 1
        flake = flakes[0]
        assert flake.actor == FINISHER
        assert flake.data["flaky_test"]["tests"] == ["tests/test_flaky.py::test_sometimes"]
        assert flake.data["flaky_test"]["stage"] == "pytest"
        assert flake.data["flaky_test"]["finish_id"] == result.finish_id
        assert len(flake.data["flaky_test"]["commit"]) == 40
        assert "Flaky test" in flake.body

    def test_the_merge_record_and_commit_say_the_green_came_on_a_retry(
        self, world: Dict[str, Any]
    ) -> None:
        install_gate(world, "flaky")

        result = run(world)

        task = world["manager"].get_task(world["task_id"])
        merged = merge_entry(task)
        assert merged.data["gate"]["classification"] == FLAKY_TEST
        assert "green on its one retry" in merged.body
        message = git(world["root"], "log", "-1", "--format=%B", result.merge_commit).stdout
        assert "green on its one retry" in message
        assert "flaky_test" in message

    def test_a_second_red_stops_and_nothing_is_retried_again(self, world: Dict[str, Any]) -> None:
        calls = install_gate(world, "always_red")

        result = run(world)

        assert result.outcome == ESCALATED
        assert result.reason == "gate_failed"
        assert "twice" in result.detail
        assert len(gate_calls(calls)) == 2
        assert not merged_into(world["root"], world["branch"])
        task = world["manager"].get_task(world["task_id"])
        assert entries_with(task, "flaky_test") == []

    def test_a_red_that_names_no_stage_is_not_retried(self, world: Dict[str, Any]) -> None:
        root: Path = world["root"]
        calls = root.parent / "nameless.jsonl"
        (root / "scripts" / "check.py").write_text(
            "import pathlib, sys\n"
            f"pathlib.Path({str(calls)!r}).open('a').write('call' + chr(10))\n"
            "print('something died')\nsys.exit(1)\n",
            encoding="utf-8",
        )
        git(root, "add", "--", "scripts/check.py")
        git(root, "commit", "-m", "chore: a gate that names nothing")

        result = run(world)

        assert result.outcome == ESCALATED and result.reason == "gate_failed"
        assert calls.read_text().count("call") == 1
        assert "not retried" in result.detail


# ----- proof that inputs changed (gate-2, gate-3, gate-4) ---------------------------


class TestTheSeptemberFifthIncidents:
    """Both 2026-09-05 reds, as they would happen today.

    task-337: main was fixed after the finish rebased. task-340: the backlog the corpus
    checks read changed while the gate ran. Records are rows now, so the second is
    reproduced through the task database the gate reads, not through a committed file.
    """

    def test_a_fix_landing_on_main_is_rebased_onto_and_proved(self, world: Dict[str, Any]) -> None:
        publish_gate_scope(world)
        calls = install_gate(world, "main_fix")

        result = run(world)

        assert result.outcome == FINISHED, result.render()
        # The retry rebased onto the fix (the stub is green only when it can see it) and
        # re-ran what the move reaches plus the red stage onward; ruff kept its green.
        assert gate_calls(calls) == [[], ["--only", "pytest,vitest"]]
        task = world["manager"].get_task(world["task_id"])
        gate = merge_entry(task).data["gate"]
        assert gate["classification"] == INPUTS_CHANGED
        assert gate["moved_paths"] == ["docs/fix.md"]
        first, second = gate["attempts"]
        assert first["passed"] is False and second["passed"] is True
        assert first["base"] != second["base"]
        assert first["head"] != second["head"]
        assert gate["receipt"]["pytest"].startswith("attempt 2")
        assert gate["receipt"]["vitest"].startswith("attempt 2")
        assert "cannot reach it" in gate["receipt"]["ruff"]
        assert entries_with(task, "flaky_test") == []

    def test_a_backlog_correction_during_the_gate_is_proved_from_the_database(
        self, world: Dict[str, Any]
    ) -> None:
        publish_gate_scope(world)
        calls = install_gate(world, "corpus_fix")

        result = run(world)

        assert result.outcome == FINISHED, result.render()
        assert gate_calls(calls) == [[], ["--from", "pytest"]]
        task = world["manager"].get_task(world["task_id"])
        gate = merge_entry(task).data["gate"]
        assert gate["classification"] == INPUTS_CHANGED
        first, second = gate["attempts"]
        assert first["corpus"] and second["corpus"]
        assert first["corpus"] != second["corpus"]
        assert "task corpus changed" in gate["explanation"]

    def test_a_backlog_change_does_not_explain_a_test_that_does_not_read_it(
        self, world: Dict[str, Any]
    ) -> None:
        publish_gate_scope(world)
        install_gate(world, "corpus_unrelated")

        result = run(world)

        assert result.outcome == FINISHED, result.render()
        task = world["manager"].get_task(world["task_id"])
        assert merge_entry(task).data["gate"]["classification"] == FLAKY_TEST
        assert len(entries_with(task, "flaky_test")) == 1


def attempt(**fields: Any) -> GateAttempt:
    base: Dict[str, Any] = {
        "number": 1,
        "selection": [],
        "head": "a" * 40,
        "base": "b" * 40,
        "corpus": "feed:1",
        "stage": "pytest",
        "stages": ["ruff", "pytest", "e2e"],
    }
    base.update(fields)
    return GateAttempt(**base)


class TestWhatTheRetryRuns:
    def test_an_unmoved_base_keeps_the_greens_before_the_red_stage(self) -> None:
        selection, _ = retry_selection(attempt(), moved=False, moved_stages=[])
        assert selection == ["--from", "pytest"]

    def test_an_unclassified_move_runs_the_whole_gate(self) -> None:
        selection, why = retry_selection(attempt(), moved=True, moved_stages=None)
        assert selection == []
        assert "nothing classifies" in why

    def test_a_gate_that_did_not_announce_its_stages_runs_whole(self) -> None:
        selection, _ = retry_selection(attempt(stages=[]), moved=True, moved_stages=["pytest"])
        assert selection == []

    def test_a_classified_move_reruns_what_it_reaches_before_the_red_stage(self) -> None:
        selection, _ = retry_selection(attempt(), moved=True, moved_stages=["ruff"])
        assert selection == ["--only", "ruff,pytest,e2e"]

    def test_no_partial_retry_is_described_as_covering_less_than_every_stage(self) -> None:
        first = attempt()
        second = attempt(number=2, selection=["--from", "pytest"], stages=["pytest", "e2e"])
        vector = receipt_vector(first, second)
        assert set(vector) == {"ruff", "pytest", "e2e"}
        assert vector["ruff"].startswith("attempt 1")
        assert vector["e2e"].startswith("attempt 2")


class TestWhatCountsAsProof:
    def test_a_move_that_does_not_reach_the_red_stage_proves_nothing(self) -> None:
        classification, _ = explain_red(
            None,
            attempt(),
            moved_paths=["docs/x.md"],
            moved_stages=["ruff"],
            corpus_after="feed:1",
            base_after="c" * 40,
        )
        assert classification == FLAKY_TEST

    def test_an_unclassified_move_is_named_but_not_counted(self) -> None:
        classification, why = explain_red(
            None,
            attempt(),
            moved_paths=["src/x.py"],
            moved_stages=None,
            corpus_after="feed:1",
            base_after="c" * 40,
        )
        assert classification == FLAKY_TEST
        assert "may be the real cause" in why

    def test_a_corpus_change_without_a_declaration_proves_nothing(self) -> None:
        classification, _ = explain_red(
            None,
            attempt(tests=["FAILED tests/test_task_corpus.py::test_x - boom"]),
            moved_paths=[],
            moved_stages=[],
            corpus_after="feed:2",
            base_after="b" * 40,
        )
        assert classification == FLAKY_TEST


# ----- recovery from merge evidence (durable-1, durable-2) --------------------------


class TestACrashAfterTheMerge:
    def test_a_merge_whose_result_was_never_written_is_recovered_not_repeated(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The crash window §9a names: git committed the merge, nothing else did."""
        calls = install_gate(world, "flaky")
        real_settle = FinishReceipts.settle

        def die_after_the_merge(self: Any, finish_id: str, activity: str, *args: Any, **kw: Any):
            if activity == "merge" and args and args[1] == APPLIED:
                raise KeyboardInterrupt("the process died between git merge and its receipt")
            return real_settle(self, finish_id, activity, *args, **kw)

        monkeypatch.setattr(FinishReceipts, "settle", die_after_the_merge)
        with pytest.raises(KeyboardInterrupt):
            run(world)
        monkeypatch.setattr(FinishReceipts, "settle", real_settle)

        assert len(merges_on_main(world["root"])) == 1
        task = world["manager"].get_task(world["task_id"])
        assert all(entry.data.get("finish_step") != "merge" for entry in task.log)
        gated = len(gate_calls(calls))

        second = run(world)

        assert second.outcome == FINISHED, second.render()
        assert len(merges_on_main(world["root"])) == 1
        assert second.merge_commit == merges_on_main(world["root"])[0]
        assert len(gate_calls(calls)) == gated, "the gate ran again on a merged branch"
        task = world["manager"].get_task(world["task_id"])
        assert task.lifecycle is Lifecycle.CLOSED
        recorded = merge_entry(task)
        assert recorded.data["merge_commit"] == second.merge_commit
        assert recorded.data.get("recovered_from")
        assert "did not merge again" in recorded.body

    def test_a_delivery_that_stopped_resumes_without_a_second_gate(
        self, world: Dict[str, Any], serving: Any
    ) -> None:
        add_served_change(world)
        calls = install_gate(world, "flaky")
        first = run(world, restart=[])
        assert first.outcome == ESCALATED and first.reason == "no_restart_command"
        gated = len(gate_calls(calls))

        # The server is already running the merge -- somebody restarted it by hand -- so
        # a resumed delivery must not restart it again. The command would fail if it ran.
        server = serving({"source_root": str(world["root"]), "source_commit": head(world["root"])})
        second = run(
            world,
            restart=[_interpreter(), "-c", "raise SystemExit(3)"],
            verify_base=server.base,
        )

        assert second.outcome == FINISHED, second.render()
        assert len(gate_calls(calls)) == gated
        assert second.merge_commit == first.merge_commit
        by_name = {step.step: step for step in second.steps}
        assert by_name["merge"].skipped
        assert by_name["restart"].skipped and "already serves" in by_name["restart"].detail
        verify = FinishReceipts(world["home"], "demo", world["task_id"]).result(
            "verify", second.merge_commit
        )
        assert verify is not None
        assert verify["result"]["deployed_commit"] == head(world["root"])
        assert verify["result"]["source_root"] == str(world["root"])

    def test_a_branch_that_moved_after_its_merge_is_not_delivered_as_finished(
        self, world: Dict[str, Any]
    ) -> None:
        add_served_change(world)
        calls = install_gate(world, "flaky")
        first = run(world, restart=[])
        assert first.merge_commit
        (world["worktree"] / "later.txt").write_text("more work", encoding="utf-8")
        git(world["worktree"], "add", "--", "later.txt")
        git(world["worktree"], "commit", "-m", "more work after the merge")
        gated = len(gate_calls(calls))

        second = run(world, restart=[_interpreter(), "-c", "pass"])

        assert second.outcome == ESCALATED
        assert second.reason == "branch_moved_after_merge"
        assert second.merge_commit == first.merge_commit
        assert len(gate_calls(calls)) == gated
        task = world["manager"].get_task(world["task_id"])
        assert task.is_open
        assert "The merge is done" in task.ball_prompt


@pytest.mark.parametrize(
    "step", ["rebuild_frontend", "restart_server", "verify_live", "mark_branch_merged"]
)
def test_a_crash_at_any_delivery_step_resumes_there_without_merging_or_gating_again(
    step: str, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = install_gate(world, "flaky")

    def die(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt(f"the process died in {step}")

    monkeypatch.setattr(finish_module, step, die)
    with pytest.raises(KeyboardInterrupt):
        run(world)
    monkeypatch.undo()
    merged = merges_on_main(world["root"])
    assert len(merged) == 1
    gated = len(gate_calls(calls))

    second = run(world)

    assert second.outcome == FINISHED, second.render()
    assert merges_on_main(world["root"]) == merged
    assert second.merge_commit == merged[0]
    assert len(gate_calls(calls)) == gated
    task = world["manager"].get_task(world["task_id"])
    assert task.lifecycle is Lifecycle.CLOSED


class TestCleanupAfterTheClose:
    def test_a_closed_task_whose_cleanup_died_is_cleaned_up_on_the_next_run(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def die(plan: Any) -> Any:
            raise KeyboardInterrupt("the process died before the worktree was removed")

        monkeypatch.setattr(finish_module, "remove_worktree", die)
        with pytest.raises(KeyboardInterrupt):
            run(world)
        monkeypatch.undo()
        task = world["manager"].get_task(world["task_id"])
        assert task.lifecycle is Lifecycle.CLOSED
        assert world["worktree"].exists()

        second = run(world)

        assert second.outcome == FINISHED and second.reason == "cleanup_resumed"
        assert not world["worktree"].exists()
        branches = git(world["root"], "branch", "--list", world["branch"]).stdout
        assert branches.strip() == ""

    def test_a_dirty_worktree_is_never_discarded_to_finish_cleanup(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def die(plan: Any) -> Any:
            raise KeyboardInterrupt("died before cleanup")

        monkeypatch.setattr(finish_module, "remove_worktree", die)
        with pytest.raises(KeyboardInterrupt):
            run(world)
        monkeypatch.undo()
        precious = world["worktree"] / "uncommitted.txt"
        precious.write_text("somebody's work", encoding="utf-8")

        second = run(world)

        assert second.outcome == FINISHED
        assert precious.read_text(encoding="utf-8") == "somebody's work"
        worktree = next(step for step in second.steps if step.step == "worktree")
        assert "left in place" in worktree.detail


# ----- authority at the merge, and Stop after it (durable-4) ------------------------


def a_stop_now() -> List[Dict[str, Any]]:
    return [
        {
            "run_id": "run_x",
            "requester": "Jeff Posey",
            "source": "gui",
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
    ]


class TestAuthorityIsReadAtTheMerge:
    def test_a_stop_during_the_gate_merges_nothing_and_starts_nothing(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "agentjobs.dispatch.approval.stop_requests", lambda *args, **kwargs: a_stop_now()
        )
        before = world["manager"].get_task(world["task_id"])

        result = run(world)

        assert result.outcome == ESCALATED and result.reason == "stopped"
        assert result.escalation_dispatch == "withdrawn"
        assert result.dispatched_run_id is None
        assert not merged_into(world["root"], world["branch"])
        task = world["manager"].get_task(world["task_id"])
        assert (task.ball, task.ball_reason) == (before.ball, before.ball_reason)

    def test_an_approval_superseded_during_the_gate_merges_nothing(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Receipt:
            entry_id = 7
            approver = "Jeff Posey"

        answers = iter([Receipt()])
        monkeypatch.setattr(finish_module, "_standing_approval", lambda *args: next(answers, None))

        result = run(world)

        assert result.outcome == ESCALATED and result.reason == "approval_withdrawn"
        assert not merged_into(world["root"], world["branch"])

    def test_a_stop_after_the_merge_ends_delivery_and_says_the_merge_is_done(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = world["root"]
        branch = world["branch"]

        def stop_once_merged(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
            contained = subprocess.run(
                ["git", "-C", str(root), "merge-base", "--is-ancestor", branch, "main"],
                capture_output=True,
            )
            return a_stop_now() if contained.returncode == 0 else []

        monkeypatch.setattr("agentjobs.dispatch.approval.stop_requests", stop_once_merged)
        calls = install_gate(world, "flaky")

        result = run(world)

        assert result.outcome == ESCALATED and result.reason == "stopped_after_merge"
        assert result.merge_commit is not None
        assert result.dispatched_run_id is None
        task = world["manager"].get_task(world["task_id"])
        assert task.is_open
        assert (task.ball, task.ball_reason) == (Ball.HUMAN, BallReason.DECISION)
        assert "The merge is done" in task.ball_prompt

        monkeypatch.setattr("agentjobs.dispatch.approval.stop_requests", lambda *a, **k: [])
        gated = len(gate_calls(calls))
        resumed = run(world)
        assert resumed.outcome == FINISHED, resumed.render()
        assert resumed.merge_commit == result.merge_commit
        assert len(gate_calls(calls)) == gated
