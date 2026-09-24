"""Retrying a stopped finish on the approval already given (task-575).

task-563 was approved three times for one change. Its finish stopped at the gate, then
at a rebase; the agent repaired each, and each repair touched nothing the owner had
reviewed -- two e2e specs, then a conflict resolution keeping both sides. The retry was
refused to the agent's shell as a merge without review, correctly, so each repair cost
another click. These tests pin the verb that removes the click and the guards that keep
the approval meaning something.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from agentjobs.actors import FINISHER
from agentjobs.dispatch.finish import Escalate, escalate_on_record, finish_task
from agentjobs.dispatch.finish_retry import (
    DECLINED,
    HANDED_BACK,
    MAX_RETRIES_PER_APPROVAL,
    RETRY_KEY,
    RETRYING,
    RetryOutcome,
    classify_path,
    keeps_both_sides,
    request_finish_retry,
)
from agentjobs.models_v2 import Ball, BallReason, Outcome
from test_dispatch_finish import git, head, settings
from test_finish_duplicate import APPROVER, approve, offer_the_finish
import test_dispatch_finish


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """``test_dispatch_finish``'s real clone, branch, worktree and task, reused unchanged."""
    built: Dict[str, Any] = test_dispatch_finish.world.__pytest_wrapped__.obj(  # type: ignore[attr-defined]
        tmp_path, monkeypatch
    )
    return built


# ----- the rule, on bytes ---------------------------------------------------------


class TestTheRule:
    def test_a_path_the_rebase_alone_explains_qualifies(self) -> None:
        verdict = classify_path(
            "src/app.py", base=b"a\n", approved=b"a\n", main=b"a\nmain\n", new=b"a\nmain\n"
        )
        assert (verdict.kind, verdict.qualifies) == ("rebase", True)

    def test_a_test_file_qualifies_whatever_it_says(self) -> None:
        for path in ("tests/test_x.py", "frontend/e2e/list.spec.ts", "frontend/src/A.test.tsx"):
            verdict = classify_path(path, base=None, approved=b"old\n", main=None, new=b"new\n")
            assert (verdict.kind, verdict.qualifies) == ("test", True), path

    def test_generated_output_qualifies(self) -> None:
        verdict = classify_path(
            "frontend/src/api/generated/types.gen.ts",
            base=b"x\n",
            approved=b"y\n",
            main=b"z\n",
            new=b"w\n",
        )
        assert (verdict.kind, verdict.qualifies) == ("generated", True)

    def test_changing_reviewed_source_does_not(self) -> None:
        verdict = classify_path(
            "frontend/src/TaskList.tsx",
            base=b"a\n",
            approved=b"a\nreviewed\n",
            main=b"a\n",
            new=b"a\nsomething else\n",
        )
        assert (verdict.kind, verdict.qualifies) == ("reviewed_source", False)

    def test_touching_source_the_branch_never_had_does_not(self) -> None:
        verdict = classify_path(
            "src/elsewhere.py", base=b"a\n", approved=b"a\n", main=b"a\n", new=b"a\nnew\n"
        )
        assert (verdict.kind, verdict.qualifies) == ("new_source", False)

    def test_a_resolution_keeping_both_sides_qualifies(self) -> None:
        verdict = classify_path(
            "src/both.py",
            base=b"top\nend\n",
            approved=b"top\nbranch\nend\n",
            main=b"top\nmain\nend\n",
            new=b"top\nmain\nbranch\nend\n",
        )
        assert (verdict.kind, verdict.qualifies) == ("conflict_resolution", True)

    def test_a_resolution_that_drops_a_side_does_not(self) -> None:
        kept, why = keeps_both_sides(
            b"top\nend\n", b"top\nbranch\nend\n", b"top\nmain\nend\n", b"top\nmain\nend\n"
        )
        assert not kept and "drops" in why

    def test_a_resolution_that_writes_its_own_line_does_not(self) -> None:
        verdict = classify_path(
            "src/both.py",
            base=b"top\nend\n",
            approved=b"top\nbranch\nend\n",
            main=b"top\nmain\nend\n",
            new=b"top\nmain\nbranch\ninvented\nend\n",
        )
        assert (verdict.kind, verdict.qualifies) == ("conflict_resolution", False)
        assert "neither side" in verdict.why


# ----- the request, against a real clone ------------------------------------------


class Spawns:
    """Stands in for ``spawn_finish`` and records every finish the retry would start."""

    def __init__(self, then: Optional[Any] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.then = then

    def __call__(self, **kwargs: Any) -> Optional[str]:
        self.calls.append(kwargs)
        if self.then is not None:
            self.then()
        return "finish.log"


def stop_the_finish(world: Dict[str, Any], step: str = "gate") -> None:
    """Write the escalation a stopped finish writes, through the finisher's own function."""
    escalate_on_record(
        world["manager"],
        world["task_id"],
        Escalate(step, "red" if step == "gate" else "conflict", f"The {step} stopped."),
        [],
        None,
        root=world["root"],
        retry_on_approval=True,
    )


def commit_in_worktree(world: Dict[str, Any], path: str, text: str) -> str:
    target = world["worktree"] / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git(world["worktree"], "add", "--", path)
    git(world["worktree"], "commit", "-m", f"fix: {path}")
    return head(world["worktree"])


def ask(world: Dict[str, Any], spawn: Any, **kwargs: Any) -> RetryOutcome:
    return request_finish_retry(
        manager=world["manager"],
        project=world["project"],
        task_id=world["task_id"],
        actor="claude",
        summary=kwargs.pop("summary", "Fixed the e2e spec."),
        home=world["home"],
        spawn=spawn,
        **kwargs,
    )


@pytest.fixture
def approved(world: Dict[str, Any]) -> Dict[str, Any]:
    offer_the_finish(world)
    world["approved_head"] = head(world["worktree"])
    world["approval_entry"] = approve(world)
    return world


class TestTheRequest:
    def test_a_test_only_repair_retries_on_the_standing_approval(
        self, approved: Dict[str, Any]
    ) -> None:
        stop_the_finish(approved)
        new_head = commit_in_worktree(approved, "tests/test_thing.py", "def test_x():\n    pass\n")
        spawn = Spawns()

        outcome = ask(approved, spawn)

        assert outcome.outcome == RETRYING, outcome.detail
        assert spawn.calls == [
            {
                "project": approved["project"],
                "task_id": approved["task_id"],
                "approver": APPROVER,
                "home": approved["home"],
            }
        ]
        task = approved["manager"].get_task(approved["task_id"])
        entry = task.log[-1]
        assert entry.actor == FINISHER
        retry = entry.data[RETRY_KEY]
        assert retry["approval_entry"] == approved["approval_entry"]
        assert retry["approved_head"] == approved["approved_head"]
        assert retry["new_head"] == new_head
        assert retry["attempt"] == 1 and retry["limit"] == MAX_RETRIES_PER_APPROVAL
        assert [(p["path"], p["kind"]) for p in retry["paths"]] == [("tests/test_thing.py", "test")]
        assert f"entry {approved['approval_entry']}" in entry.body

    def test_the_retried_finish_merges_without_a_second_approval(
        self, approved: Dict[str, Any]
    ) -> None:
        """ac-1 end to end: the spawned process is the real finish, run in-process."""
        stop_the_finish(approved)
        commit_in_worktree(approved, "tests/test_thing.py", "def test_x():\n    pass\n")
        results: List[Any] = []

        def the_real_finish() -> None:
            results.append(
                finish_task(
                    manager=approved["manager"],
                    project=approved["project"],
                    task_id=approved["task_id"],
                    approver=APPROVER,
                    home=approved["home"],
                    api_base="http://127.0.0.1:1",
                    settings=settings(),
                )
            )

        outcome = ask(approved, Spawns(then=the_real_finish))

        assert outcome.outcome == RETRYING
        assert results and results[0].merge_commit, results[0].render() if results else None
        assert git(approved["root"], "show", "main:tests/test_thing.py").stdout.startswith("def")
        task = approved["manager"].get_task(approved["task_id"])
        assert task.outcome is Outcome.COMPLETED
        humans = [e for e in task.log if e.actor == APPROVER and e.type.value == "handoff"]
        assert len(humans) == 1, "the retry must not have needed a second approval"

    def test_a_repair_to_reviewed_source_goes_back_to_review(
        self, approved: Dict[str, Any]
    ) -> None:
        stop_the_finish(approved)
        commit_in_worktree(approved, "docs/feature.md", "a different deliverable\n")
        spawn = Spawns()

        outcome = ask(approved, spawn)

        assert outcome.outcome == HANDED_BACK
        assert outcome.reason == "repair_changes_reviewed_work"
        assert spawn.calls == []
        task = approved["manager"].get_task(approved["task_id"])
        assert (task.ball, task.ball_reason) == (Ball.HUMAN, BallReason.REVIEW)
        assert "docs/feature.md" in task.ball_prompt
        assert "reviewed_source" in task.ball_prompt
        assert "Fixed the e2e spec." in task.ball_prompt

    def test_a_rebase_that_kept_both_sides_retries(self, world: Dict[str, Any]) -> None:
        """task-563's second stop: main moved under the branch and the agent merged both."""
        approved = world
        offer_the_finish(approved)
        commit_in_worktree(approved, "shared.txt", "base\nbranch line\n")
        approve(approved)
        (approved["root"] / "shared.txt").write_text("base\nmain line\n", encoding="utf-8")
        git(approved["root"], "add", "--", "shared.txt")
        git(approved["root"], "commit", "-m", "feat: main moves")
        stop_the_finish(approved, "rebase")

        rebase = git_may_fail(approved["worktree"], "rebase", "main")
        assert rebase.returncode != 0, "the fixture was meant to conflict"
        (approved["worktree"] / "shared.txt").write_text(
            "base\nmain line\nbranch line\n", encoding="utf-8"
        )
        git(approved["worktree"], "add", "--", "shared.txt")
        git(approved["worktree"], "-c", "core.editor=true", "rebase", "--continue")
        spawn = Spawns()

        outcome = ask(approved, spawn)

        assert outcome.outcome == RETRYING, outcome.detail
        kinds = {p["path"]: p["kind"] for p in outcome.data["paths"]}
        assert kinds["shared.txt"] == "conflict_resolution"
        assert len(spawn.calls) == 1

    def test_retries_per_approval_are_bounded_on_the_record(self, approved: Dict[str, Any]) -> None:
        spawn = Spawns()
        for attempt in range(MAX_RETRIES_PER_APPROVAL):
            stop_the_finish(approved)
            assert ask(approved, spawn).outcome == RETRYING, attempt
        stop_the_finish(approved)

        outcome = ask(approved, spawn)

        assert outcome.outcome == HANDED_BACK
        assert outcome.reason == "retry_limit"
        assert len(spawn.calls) == MAX_RETRIES_PER_APPROVAL
        task = approved["manager"].get_task(approved["task_id"])
        counted = [e for e in task.log if RETRY_KEY in (e.data or {})]
        assert [e.data[RETRY_KEY]["attempt"] for e in counted] == [1, 2]
        assert (task.ball, task.ball_reason) == (Ball.HUMAN, BallReason.REVIEW)

    def test_a_retry_whose_finish_has_not_stopped_again_is_not_repeated(
        self, approved: Dict[str, Any]
    ) -> None:
        stop_the_finish(approved)
        spawn = Spawns()
        assert ask(approved, spawn).outcome == RETRYING

        outcome = ask(approved, spawn)

        assert (outcome.outcome, outcome.reason) == (DECLINED, "no_escalation")
        assert len(spawn.calls) == 1

    def test_nothing_to_retry_without_a_stopped_finish(self, approved: Dict[str, Any]) -> None:
        outcome = ask(approved, Spawns())
        assert (outcome.outcome, outcome.reason) == (DECLINED, "no_escalation")

    def test_nothing_to_retry_without_an_approval(self, world: Dict[str, Any]) -> None:
        offer_the_finish(world)
        outcome = ask(world, Spawns())
        assert (outcome.outcome, outcome.reason) == (DECLINED, "no_standing_approval")

    def test_the_same_operation_id_replays(self, approved: Dict[str, Any]) -> None:
        stop_the_finish(approved)
        spawn = Spawns()
        assert ask(approved, spawn, operation_id="op-1").outcome == RETRYING
        stop_the_finish(approved)

        again = ask(approved, spawn, operation_id="op-1")

        assert again.outcome == "replayed"
        assert len(spawn.calls) == 1

    def test_the_escalation_prompt_names_the_verb_and_forbids_the_merge(
        self, approved: Dict[str, Any]
    ) -> None:
        stop_the_finish(approved)
        prompt = approved["manager"].get_task(approved["task_id"]).ball_prompt
        assert "task_finish_retry" in prompt
        assert "Never run `agentjobs finish`" in prompt


def git_may_fail(root: Path, *args: str) -> Any:
    import subprocess

    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


# ----- the route ------------------------------------------------------------------


class TestTheRoute:
    def test_the_route_runs_the_judgement_in_the_server(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ac-3: the retry is asked of the server, which decides and spawns; not the caller."""
        from test_finish_approval_hook import task_awaiting_review

        import yaml

        from agentjobs.api.dependencies import reset_dependency_cache
        from agentjobs.api.main import app

        (tmp_path / "tasks").mkdir()
        (tmp_path / ".agentjobs").mkdir()
        (tmp_path / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "project_name": "Test",
                    "tasks_directory": "tasks",
                    "actors": [
                        {"name": "jeff", "kind": "human"},
                        {"name": "claude", "kind": "agent"},
                    ],
                    "default_user": "jeff",
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("AGENTJOBS_TASKS_DIR", str(tmp_path / "tasks"))
        reset_dependency_cache()
        seen: List[Dict[str, Any]] = []

        def judged(**kwargs: Any) -> RetryOutcome:
            seen.append(kwargs)
            return RetryOutcome(outcome=RETRYING, reason="retrying", detail="started")

        monkeypatch.setattr("agentjobs.dispatch.finish_retry.request_finish_retry", judged)
        try:
            client = TestClient(app)
            task_id = task_awaiting_review(client)
            response = client.post(
                f"/api/tasks/{task_id}/finish-retry",
                json={"actor": "claude", "summary": "fixed", "operation_id": "op-9"},
            )
        finally:
            reset_dependency_cache()

        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == RETRYING
        assert [(s["task_id"], s["actor"], s["summary"], s["operation_id"]) for s in seen] == [
            (task_id, "claude", "fixed", "op-9")
        ]
