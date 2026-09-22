"""Bounded agent loops: authorisation, the digest, the driver's guardrails, the budgets.

**The chains here are real.** Every iteration in ``TestTheChain`` is a genuine
``dispatch_task`` that starts a genuine process, which edits a file in the project and
commits it, and a genuine ``evaluate_task`` that runs a genuine check against the tree
that process left behind. Nothing is patched into the loop's decision path. That is a
deliberate expense: the whole feature is a claim about what happens between one process
ending and the next beginning, and a suite that stubbed the dispatcher would be asserting
that its own stub returns what it was told to.

What *is* injected is the poll interval, because the alternative is fifteen seconds of
real waiting per iteration for no information at all, and the clock, because a chain's
wall-clock bound is four hours by default and no suite may wait one out.

The fake runner commits its edit. A real agent would; and ``require_clean_tree`` is left
on so that a chain whose runner left the tree dirty would be refused at iteration two --
which is the behaviour, not an obstacle to work around.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agentjobs.api.authorization import ROUTE_CAPABILITIES
from agentjobs.api.main import app
from agentjobs.capabilities import GRANTS
from agentjobs.dispatch.budget import chains_since, check_budget
from agentjobs.dispatch.chains import (
    AlreadyPassingError,
    BoundExceedsCeilingError,
    ChainAlreadyLiveError,
    NoChecksToChainError,
    UnknownChainError,
    authorize_chain,
    chain_history,
    check_digest,
    iteration_results,
    live_chain,
    revoke_chain,
)
from agentjobs.dispatch.config import AutoDispatchLimits, sentinel_path
from agentjobs.dispatch.handback import deliver_handback, record_handback
from agentjobs.dispatch.ledger import list_runs
from agentjobs.dispatch.loop import ChainStop, run_chain, vector_of
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    MANAGER_WRITTEN_LOG_TYPES,
    AcceptanceCriterion,
    AcceptanceStatus,
    Ball,
    BallReason,
    DispatchMode,
    DispatchPosture,
    DispatchTrigger,
    Lifecycle,
    LogEntryType,
    QuestionDraft,
    QuestionOption,
    Task,
)
from agentjobs.principals import RUN_CREDENTIAL_HEADER, PrincipalKind
from agentjobs.projects import Project, ProjectRegistry
from agentjobs.record_check import BLOCKED_HANDOFF, check_record
from support import task_store

PROJECT_CONFIG: Dict[str, object] = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}

HUMAN = "Jeff Posey"

#: A check that passes once ``counter.txt`` in the project root reaches ``n``. The loop's
#: oracle: cheap, objective, and decided entirely by what the previous iteration did to
#: the tree -- which is the shape section 10 says a loop pays off on.
COUNTER_CHECK = textwrap.dedent(
    """
    import pathlib, sys
    want = int(sys.argv[1])
    text = pathlib.Path("counter.txt").read_text(encoding="utf-8").strip() or "0"
    print(f"counter is {text}, wanted {want}")
    sys.exit(0 if int(text) >= want else 1)
    """
)

#: A runner that does what an agent does to a repository: changes something and commits.
#: ``mode`` decides what it changes, so one script drives a converging chain, a stuck
#: one, a regressing one and a thrashing one.
COUNTING_RUNNER = textwrap.dedent(
    """
    import pathlib, subprocess, sys
    mode = sys.argv[1]
    root = pathlib.Path(sys.argv[2])
    counter = root / "counter.txt"
    value = int((counter.read_text(encoding="utf-8").strip() or "0"))
    if mode == "advance":
        value += 1
    elif mode == "regress":
        # Undo whatever progress there was: the agent broke a passing check.
        value = 0
    elif mode == "stuck":
        pass
    counter.write_text(str(value) + "\\n", encoding="utf-8")
    # A busy no-op, so a thrashing chain has a real diff every turn and the vector
    # comparison is doing the work rather than the absence of a change.
    (root / "scratch.txt").write_text(f"turn {value}\\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
    subprocess.run(["git", "commit", "-m", f"turn {value}"], cwd=root, capture_output=True)
    print(f"runner {mode} left counter at {value}")
    """
)


# ----- fixtures ---------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    machine = tmp_path / "home"
    machine.mkdir()
    return machine


@pytest.fixture
def project(tmp_path: Path, home: Path) -> Project:
    """A registered project with a clean git tree, a counter, and an actor vocabulary."""
    root = tmp_path / "proj"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(PROJECT_CONFIG), encoding="utf-8"
    )
    (root / "tasks").mkdir()
    (root / "counter.txt").write_text("0\n", encoding="utf-8")
    (root / ".gitignore").write_text(".agentjobs/\ntasks/\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True)
    return ProjectRegistry(home=home).add(root, project_id="sandbox")


@pytest.fixture
def manager(project: Project) -> TaskManager:
    return TaskManager(task_store(project.root / "tasks", project_id="sandbox"))


@pytest.fixture
def check_script(tmp_path: Path) -> Path:
    script = tmp_path / "counter_check.py"
    script.write_text(COUNTER_CHECK, encoding="utf-8")
    return script


@pytest.fixture
def runner_script(tmp_path: Path) -> Path:
    script = tmp_path / "counting_runner.py"
    script.write_text(COUNTING_RUNNER, encoding="utf-8")
    return script


def write_dispatch_config(
    home: Path,
    runner_script: Path,
    project: Project,
    *,
    mode: str = "advance",
    limits: Optional[Dict[str, object]] = None,
) -> None:
    """A machine that permits 'sandbox' and whose runner edits the repository."""
    config: Dict[str, object] = {
        "version": 1,
        "enabled": True,
        "runners": {
            "fake": {
                "argv": [
                    sys.executable,
                    str(runner_script),
                    mode,
                    str(project.root),
                ],
                "actor": "claude",
            }
        },
        "projects": {
            "sandbox": {
                "enabled": True,
                "runner": "fake",
                "require_clean_tree": True,
                # On, because the handback path is only reachable at all when it is, and
                # section 4's whole subject is what that path does with a refusal.
                "auto_dispatch": True,
            }
        },
    }
    if limits is not None:
        config["limits"] = limits
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def configured(home: Path, project: Project, runner_script: Path) -> None:
    """Every test in this file runs on a machine that permits 'sandbox'.

    Autouse because *authorising* a chain runs the checks, and running a check passes the
    same four gates a dispatch passes -- so even the pure-authorisation cases need a
    configured machine. A test that wants different limits or a different runner mode
    calls :func:`write_dispatch_config` again and overwrites this.
    """
    write_dispatch_config(home, runner_script, project)


def a_task(
    manager: TaskManager,
    check_script: Path,
    *,
    wants: Sequence[int] = (3,),
    extra_prose: bool = True,
    checks: bool = True,
) -> Task:
    """A ready task with failing checks, which is what a chain is authorised against.

    Left ``ready`` rather than claimed, deliberately. A claimed task whose ball nobody has
    moved and which this machine has never dispatched is refused by ``task_being_worked``
    -- correctly, because something outside AgentJobs is holding it -- so a chain
    authorised against one would stop at iteration one. The first iteration claims it, and
    every iteration after that arrives at a task the driver itself handed back.

    ``wants`` is one counter threshold per checked criterion. **A converging chain needs
    more than one**, and that is a property of the design rather than of this helper: with
    a single binary check, a chain that takes three turns to flip it produces three
    identical result vectors and is stopped as thrash on the second. The design says so in
    as many words -- *an agent that edits files busily while every check keeps returning
    the same answer is thrashing* -- so a loop worth running is one whose criteria flip
    one at a time, and the fixtures say that out loud rather than working around it.
    """
    criteria: List[AcceptanceCriterion] = []
    if checks:
        for index, want in enumerate(wants, start=1):
            criteria.append(
                AcceptanceCriterion(
                    id=f"sc-{index}",
                    text=f"The counter reaches {want}",
                    check=[sys.executable, str(check_script), str(want)],
                )
            )
    if extra_prose:
        criteria.append(AcceptanceCriterion(id="sc-prose", text="It reads well", verify="Look."))
    task = manager.create_task(
        title="Converge the counter",
        category="general",
        summary="A loop with a cheap oracle.",
        description="Raise the counter until the check passes.",
        lifecycle=Lifecycle.READY,
        actor=HUMAN,
        acceptance=[item.model_dump(mode="json", exclude_none=True) for item in criteria],
    )
    stored = manager.get_task(task.id)
    assert stored is not None
    return stored


def authorize(
    manager: TaskManager,
    project: Project,
    task: Task,
    home: Path,
    *,
    iterations: int = 5,
    wall_clock: int = 4 * 60 * 60,
):
    return authorize_chain(
        manager=manager,
        project=project,
        task=task,
        actor=HUMAN,
        max_iterations=iterations,
        wall_clock_seconds=wall_clock,
        home=home,
    )


def drive(manager: TaskManager, project: Project, home: Path, task_id: str, **kwargs):
    """Run a chain to its stop, at a poll interval a suite can afford."""
    return run_chain(
        manager=manager,
        project=project,
        project_config=PROJECT_CONFIG,
        task_id=task_id,
        home=home,
        poll_seconds=0.05,
        **kwargs,
    )


def vectors_on(manager: TaskManager, task_id: str, chain_id: str):
    task = manager.get_task(task_id)
    assert task is not None
    return iteration_results(task, chain_id)


@pytest.fixture
def served(tmp_path, home: Path, project: Project, runner_script: Path, monkeypatch):
    """A served project on a machine configured to permit it."""
    from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache

    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    with TestClient(app) as client:
        yield client, project.root, home

    reset_dependency_cache()


def a_run_credential(home: Path, task_id: str) -> str:
    """A live run's minted credential, for asking what a *run* is allowed to do.

    Minted inside the test rather than in the fixture, and the ordering is the point: the
    server's startup reconciler finds a seeded run whose pid is absent, marks it
    interrupted, and revokes its credential -- so a token minted before the lifespan opens
    is refused as ``unverified_run_credential`` and a test asserting on capabilities would
    pass while proving nothing about them.
    """
    from agentjobs.dispatch.credentials import mint_run_credential
    from agentjobs.dispatch.runner import RunDirectory

    directory = RunDirectory.create(
        home,
        "run_c4a1f001",
        {
            "run_id": "run_c4a1f001",
            "task_id": task_id,
            "project_id": "sandbox",
            "mode": "session",
            "agent": "claude",
            "status": "running",
        },
    )
    token = mint_run_credential(directory.path, "run_c4a1f001")
    assert token, "the credential must actually mint, or nothing below is tested"
    return token


# ---------------------------------------------------------------------------
# sc-4: the digest
# ---------------------------------------------------------------------------


class TestTheDigest:
    def test_it_covers_the_check_and_not_the_prose(
        self, manager: TaskManager, check_script: Path
    ) -> None:
        task = a_task(manager, check_script)
        before = check_digest(task)

        reworded = manager.update_task(
            task.id,
            actor=HUMAN,
            acceptance=[
                {
                    "id": "sc-1",
                    "text": "The counter reaches three, clarified",
                    "check": [sys.executable, str(check_script), "3"],
                },
                {"id": "sc-prose", "text": "It reads well", "verify": "Look."},
            ],
        )

        assert check_digest(reworded) == before

    def test_changing_a_check_changes_the_digest(
        self, manager: TaskManager, check_script: Path
    ) -> None:
        task = a_task(manager, check_script)
        before = check_digest(task)

        moved = manager.update_task(
            task.id,
            actor="claude",
            acceptance=[
                {
                    "id": "sc-1",
                    "text": "The counter reaches three",
                    # The move this exists to catch: a check that passes trivially.
                    "check": [sys.executable, "-c", "pass"],
                },
                {"id": "sc-prose", "text": "It reads well", "verify": "Look."},
            ],
        )

        assert check_digest(moved) != before

    def test_adding_a_prose_criterion_leaves_it_alone(
        self, manager: TaskManager, check_script: Path
    ) -> None:
        task = a_task(manager, check_script)
        before = check_digest(task)

        widened = manager.update_task(
            task.id,
            actor="claude",
            acceptance=[
                {
                    "id": "sc-1",
                    "text": "The counter reaches three",
                    "check": [sys.executable, str(check_script), "3"],
                },
                {"id": "sc-prose", "text": "It reads well", "verify": "Look."},
                {"id": "sc-3", "text": "Somebody likes it", "verify": "Ask."},
            ],
        )

        assert check_digest(widened) == before


# ---------------------------------------------------------------------------
# sc-1, sc-2: authorising a chain
# ---------------------------------------------------------------------------


class TestAuthorization:
    def test_it_writes_one_entry_carrying_the_digest_and_the_bounds(
        self, manager: TaskManager, project: Project, home: Path, check_script: Path
    ) -> None:
        task = a_task(manager, check_script)

        outcome = authorize(manager, project, task, home, iterations=4, wall_clock=3600)

        entries = [item for item in outcome.task.log if item.type is LogEntryType.CHAIN_AUTHORIZED]
        assert len(entries) == 1
        assert entries[0].actor == HUMAN
        assert entries[0].data["check_digest"] == check_digest(task)
        assert entries[0].data["max_iterations"] == 4
        assert entries[0].data["wall_clock_seconds"] == 3600
        assert entries[0].data["criteria"] == ["sc-1"]

    def test_the_baseline_pass_is_recorded_as_iteration_zero(
        self, manager: TaskManager, project: Project, home: Path, check_script: Path
    ) -> None:
        task = a_task(manager, check_script)

        outcome = authorize(manager, project, task, home)

        vectors = iteration_results(outcome.task, outcome.chain.chain_id)
        assert vectors == [(0, [("sc-1", "failed")])]

    def test_a_task_with_no_check_is_refused(
        self, manager: TaskManager, project: Project, home: Path, check_script: Path
    ) -> None:
        task = a_task(manager, check_script, checks=False)

        with pytest.raises(NoChecksToChainError) as refusal:
            authorize(manager, project, task, home)

        assert refusal.value.reason == "no_checks"

    def test_a_task_whose_checks_already_pass_is_refused(
        self, manager: TaskManager, project: Project, home: Path, check_script: Path
    ) -> None:
        task = a_task(manager, check_script, wants=(0,))

        with pytest.raises(AlreadyPassingError) as refusal:
            authorize(manager, project, task, home)

        assert refusal.value.reason == "already_passing"
        assert not [
            item
            for item in (manager.get_task(task.id) or task).log
            if item.type is LogEntryType.CHAIN_AUTHORIZED
        ]

    @pytest.mark.parametrize(
        ("iterations", "wall_clock"),
        [(21, 4 * 60 * 60), (0, 4 * 60 * 60), (5, 13 * 60 * 60), (5, 0)],
    )
    def test_a_bound_past_its_ceiling_is_refused(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        check_script: Path,
        iterations: int,
        wall_clock: int,
    ) -> None:
        task = a_task(manager, check_script)

        with pytest.raises(BoundExceedsCeilingError) as refusal:
            authorize(manager, project, task, home, iterations=iterations, wall_clock=wall_clock)

        assert refusal.value.reason == "bound_exceeds_ceiling"

    def test_a_second_live_chain_is_refused(
        self, manager: TaskManager, project: Project, home: Path, check_script: Path
    ) -> None:
        task = a_task(manager, check_script)
        first = authorize(manager, project, task, home)

        with pytest.raises(ChainAlreadyLiveError) as refusal:
            authorize(manager, project, first.task, home)

        assert refusal.value.reason == "chain_already_live"

    def test_a_chain_is_authorisable_again_once_the_first_is_revoked(
        self, manager: TaskManager, project: Project, home: Path, check_script: Path
    ) -> None:
        task = a_task(manager, check_script)
        first = authorize(manager, project, task, home)
        after = revoke_chain(manager=manager, task=first.task, actor=HUMAN)

        second = authorize(manager, project, after, home)

        assert second.chain.chain_id != first.chain.chain_id
        assert len(chain_history(second.task)) == 2


# ---------------------------------------------------------------------------
# sc-3: neither entry can be forged, and no run may write one
# ---------------------------------------------------------------------------


class TestForgery:
    @pytest.mark.parametrize(
        "entry_type", [LogEntryType.CHAIN_AUTHORIZED, LogEntryType.CHAIN_REVOKED]
    )
    def test_the_entry_types_are_manager_written(self, entry_type: LogEntryType) -> None:
        assert entry_type in MANAGER_WRITTEN_LOG_TYPES

    @pytest.mark.parametrize(
        "entry_type", [LogEntryType.CHAIN_AUTHORIZED, LogEntryType.CHAIN_REVOKED]
    )
    def test_add_log_entry_refuses_them(
        self, manager: TaskManager, check_script: Path, entry_type: LogEntryType
    ) -> None:
        task = a_task(manager, check_script)

        with pytest.raises(ValueError, match="manager"):
            manager.add_log_entry(
                task.id,
                actor="claude",
                type=entry_type,
                body="I authorise myself.",
                data={"chain_id": "chain_forged", "max_iterations": 20},
            )

    @pytest.mark.parametrize("endpoint", ["authorize_task_chain", "revoke_task_chain"])
    def test_the_routes_need_a_capability_no_run_holds(self, endpoint: str) -> None:
        rule = ROUTE_CAPABILITIES[endpoint]

        assert rule.capability not in GRANTS[PrincipalKind.RUN]
        assert rule.capability in GRANTS[PrincipalKind.OWNER]


# ---------------------------------------------------------------------------
# sc-9: revocation
# ---------------------------------------------------------------------------


class TestRevocation:
    def test_it_takes_no_arguments_and_stops_the_live_chain(
        self, manager: TaskManager, project: Project, home: Path, check_script: Path
    ) -> None:
        task = a_task(manager, check_script)
        authorized = authorize(manager, project, task, home)

        after = revoke_chain(manager=manager, task=authorized.task, actor=HUMAN)

        assert live_chain(after) is None
        assert chain_history(after)[0].revoked is True

    def test_revoking_twice_writes_once(
        self, manager: TaskManager, project: Project, home: Path, check_script: Path
    ) -> None:
        task = a_task(manager, check_script)
        authorized = authorize(manager, project, task, home)
        once = revoke_chain(manager=manager, task=authorized.task, actor=HUMAN)

        twice = revoke_chain(manager=manager, task=once, actor=HUMAN)

        revocations = [
            item
            for item in (manager.get_task(twice.id) or twice).log
            if item.type is LogEntryType.CHAIN_REVOKED
        ]
        assert len(revocations) == 1

    def test_revoking_nothing_says_so(self, manager: TaskManager, check_script: Path) -> None:
        task = a_task(manager, check_script)

        with pytest.raises(UnknownChainError):
            revoke_chain(manager=manager, task=task, actor=HUMAN)

    def test_a_revoked_chain_starts_no_iteration(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        check_script: Path,
        runner_script: Path,
    ) -> None:
        write_dispatch_config(home, runner_script, project)
        task = a_task(manager, check_script)
        authorized = authorize(manager, project, task, home)
        revoke_chain(manager=manager, task=authorized.task, actor=HUMAN)

        result = drive(manager, project, home, task.id)

        assert result.stop is ChainStop.REVOKED
        assert result.iterations_run == 0
        assert list_runs(home) == []

    def test_the_machine_sentinel_stops_a_chain(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        check_script: Path,
        runner_script: Path,
    ) -> None:
        """Asserted rather than assumed, which is what the spec asks for.

        The sentinel is one of the four gates ``assert_dispatch_permitted`` walks, and the
        driver walks them again before every iteration. So a file somebody drops in their
        home directory stops every chain on the machine at once, and this is the evidence.
        """
        write_dispatch_config(home, runner_script, project)
        task = a_task(manager, check_script)
        authorize(manager, project, task, home)
        sentinel_path(home).write_text("stop everything\n", encoding="utf-8")

        result = drive(manager, project, home, task.id)

        assert result.stop is ChainStop.REVOKED
        assert result.iterations_run == 0
        assert list_runs(home) == []
        stopped = manager.get_task(task.id)
        assert stopped is not None and stopped.ball is Ball.HUMAN


# ---------------------------------------------------------------------------
# sc-5, sc-6, sc-7: the driver
# ---------------------------------------------------------------------------


class TestTheChain:
    def test_it_converges_and_hands_off_for_review(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        check_script: Path,
        runner_script: Path,
    ) -> None:
        write_dispatch_config(home, runner_script, project, mode="advance")
        task = a_task(manager, check_script, wants=(1, 2, 3))
        authorized = authorize(manager, project, task, home, iterations=5)

        result = drive(manager, project, home, task.id)

        assert result.stop is ChainStop.CONVERGED
        assert result.iterations_run == 3
        settled = manager.get_task(task.id)
        assert settled is not None
        assert settled.ball is Ball.HUMAN
        assert settled.ball_reason is BallReason.REVIEW
        # The loop settles the checkable half and says which half is which.
        assert "sc-1" in (settled.ball_prompt or "")
        assert "sc-prose" in (settled.ball_prompt or "")
        # And it never closes anything.
        assert settled.lifecycle is Lifecycle.ACTIVE
        assert settled.outcome is None
        # The unchecked criterion is untouched.
        statuses = {item.id: item.status for item in settled.acceptance}
        assert statuses["sc-1"] is AcceptanceStatus.MET
        assert statuses["sc-3"] is AcceptanceStatus.MET
        assert statuses["sc-prose"] is AcceptanceStatus.PENDING
        # One check_result per pass, plus the baseline.
        assert [item[0] for item in vectors_on(manager, task.id, authorized.chain.chain_id)] == [
            0,
            1,
            2,
            3,
        ]

    def test_iteration_n_plus_one_never_begins_while_n_is_live(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        check_script: Path,
        runner_script: Path,
    ) -> None:
        """Asserted against the ledger, which is the only thing that knows.

        Every run this chain started has a terminal status by the time the chain ends,
        and no two runs overlap: each run's start is at or after the previous run's
        finish. A driver that started n+1 early would leave two runs whose intervals
        cross, whatever the task record said.
        """
        write_dispatch_config(home, runner_script, project, mode="advance")
        task = a_task(manager, check_script, wants=(1, 2, 3))
        authorize(manager, project, task, home, iterations=5)

        drive(manager, project, home, task.id)

        runs = sorted(list_runs(home), key=lambda item: item.started_at or datetime.min)
        assert len(runs) == 3
        assert all(item.status in {"finished", "cancelled", "failed"} for item in runs)
        for earlier, later in zip(runs, runs[1:]):
            assert earlier.finished_at is not None
            assert later.started_at is not None
            assert earlier.finished_at <= later.started_at

    def test_a_chain_that_never_converges_stops_at_its_cap(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        check_script: Path,
        runner_script: Path,
    ) -> None:
        write_dispatch_config(home, runner_script, project, mode="advance")
        task = a_task(manager, check_script, wants=(1, 2, 99))
        authorize(manager, project, task, home, iterations=2)

        result = drive(manager, project, home, task.id)

        assert result.stop is ChainStop.ITERATION_CAP
        assert result.iterations_run == 2
        stopped = manager.get_task(task.id)
        assert stopped is not None
        assert stopped.ball is Ball.HUMAN
        assert stopped.ball_reason is BallReason.DECISION
        assert "iteration_cap" in (stopped.ball_prompt or "")

    def test_a_regression_stops_the_chain_immediately(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        check_script: Path,
        runner_script: Path,
    ) -> None:
        """Two criteria: one the runner breaks, one it cannot.

        The counter starts at 2. ``sc-a`` wants 1 and passes at the baseline; ``sc-b``
        wants 99 and never will. The regressing runner zeroes the counter, so ``sc-a``
        goes from met to failed on the first iteration -- which is the case the design
        says stops with no retry.
        """
        write_dispatch_config(home, runner_script, project, mode="regress")
        (project.root / "counter.txt").write_text("2\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=project.root, capture_output=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=project.root, capture_output=True)

        task = manager.create_task(
            title="Two criteria",
            category="general",
            summary="One passes, one cannot.",
            description="A regression case.",
            lifecycle=Lifecycle.READY,
            actor=HUMAN,
            acceptance=[
                {
                    "id": "sc-a",
                    "text": "counter >= 1",
                    "check": [sys.executable, str(check_script), "1"],
                },
                {
                    "id": "sc-b",
                    "text": "counter >= 99",
                    "check": [sys.executable, str(check_script), "99"],
                },
            ],
        )
        stored = manager.get_task(task.id)
        assert stored is not None
        authorized = authorize(manager, project, stored, home, iterations=5)
        assert iteration_results(authorized.task, authorized.chain.chain_id) == [
            (0, [("sc-a", "met"), ("sc-b", "failed")])
        ]

        result = drive(manager, project, home, task.id)

        assert result.stop is ChainStop.REGRESSION
        assert result.iterations_run == 1
        assert "sc-a" in result.detail
        stopped = manager.get_task(task.id)
        assert stopped is not None and stopped.ball is Ball.HUMAN

    def test_three_identical_vectors_are_thrash_and_two_are_not(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        check_script: Path,
        runner_script: Path,
    ) -> None:
        """One chain, driven to its stop, is the evidence for both halves.

        The stuck runner leaves the vector unchanged every turn, and the baseline is the
        first of the identical vectors. The chain therefore has three identical vectors
        after iteration **two** and stops there -- which says two did not stop it, because
        iteration two happened.
        """
        write_dispatch_config(home, runner_script, project, mode="stuck")
        task = a_task(manager, check_script, wants=(99,))
        authorized = authorize(manager, project, task, home, iterations=5)

        result = drive(manager, project, home, task.id)

        assert result.stop is ChainStop.THRASH
        assert result.iterations_run == 2
        vectors = vectors_on(manager, task.id, authorized.chain.chain_id)
        assert [item[1] for item in vectors] == [[("sc-1", "failed")]] * 3
        stopped = manager.get_task(task.id)
        assert stopped is not None and stopped.ball is Ball.HUMAN
        assert "thrash" in (stopped.ball_prompt or "")

    def test_editing_a_check_mid_chain_stops_it_under_its_own_name(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        check_script: Path,
        runner_script: Path,
    ) -> None:
        """The move L4 exists to make structurally ineffective, done to a live chain."""
        write_dispatch_config(home, runner_script, project, mode="stuck")
        task = a_task(manager, check_script, wants=(99,))
        authorized = authorize(manager, project, task, home, iterations=5)

        # An agent swaps the failing check for one that passes trivially.
        manager.update_task(
            task.id,
            actor="claude",
            acceptance=[
                {"id": "sc-1", "text": "counter >= 99", "check": [sys.executable, "-c", "pass"]},
                {"id": "sc-prose", "text": "It reads well", "verify": "Look."},
            ],
        )

        result = drive(manager, project, home, task.id)

        assert result.stop is ChainStop.CHECKS_CHANGED
        assert result.iterations_run == 0
        assert list_runs(home) == []
        stopped = manager.get_task(task.id)
        assert stopped is not None and stopped.ball is Ball.HUMAN
        assert authorized.chain.data.check_digest[:12] in (stopped.ball_prompt or "")

    def test_the_wall_clock_stops_it_under_its_own_name(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        check_script: Path,
        runner_script: Path,
    ) -> None:
        write_dispatch_config(home, runner_script, project, mode="advance")
        task = a_task(manager, check_script, wants=(99,))
        authorize(manager, project, task, home, iterations=5, wall_clock=3600)

        # Two hours later, with nothing having run.
        later = datetime.now(timezone.utc) + timedelta(hours=2)
        result = drive(manager, project, home, task.id, now=lambda: later)

        assert result.stop is ChainStop.WALL_CLOCK
        assert result.iterations_run == 0
        stopped = manager.get_task(task.id)
        assert stopped is not None and stopped.ball is Ball.HUMAN
        assert "wall_clock" in (stopped.ball_prompt or "")

    def test_a_stopped_chain_is_recorded_as_spent(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        check_script: Path,
        runner_script: Path,
    ) -> None:
        """A second driver must not find the authorisation and start iterating again."""
        write_dispatch_config(home, runner_script, project, mode="advance")
        task = a_task(manager, check_script, wants=(1, 2, 3))
        authorize(manager, project, task, home, iterations=5)

        drive(manager, project, home, task.id)
        after = manager.get_task(task.id)
        assert after is not None

        assert live_chain(after) is None
        second = drive(manager, project, home, task.id)
        assert second.iterations_run == 0


# ---------------------------------------------------------------------------
# sc-8: the budgets, and L7
# ---------------------------------------------------------------------------


class TestBudgets:
    def test_the_per_day_cap_counts_chains_not_iterations(
        self,
        manager: TaskManager,
        project: Project,
        home: Path,
        check_script: Path,
        runner_script: Path,
    ) -> None:
        """The whole of L7's first clause: five iterations against a daily cap of three.

        Read literally, section 2a would have refused iteration four by a limit designed
        for a different mechanism. This is the case that would have died.
        """
        write_dispatch_config(
            home,
            runner_script,
            project,
            mode="advance",
            limits={"auto": {"per_task_per_day": 3, "per_task_lifetime": 10}},
        )
        task = a_task(manager, check_script, wants=(1, 2, 3, 4, 5))
        authorize(manager, project, task, home, iterations=5)

        result = drive(manager, project, home, task.id)

        assert result.stop is ChainStop.CONVERGED
        assert result.iterations_run == 5

    def test_the_cooldown_does_not_apply_within_a_chain(
        self, manager: TaskManager, check_script: Path
    ) -> None:
        """Every iteration of a chain starts seconds after the last, by construction.

        Asserted at the cap itself rather than only through a chain, because the chain
        test above would pass for the wrong reason if the cooldown happened to be zero.
        """
        task = a_task(manager, check_script)
        moment = datetime.now(timezone.utc)
        limits = AutoDispatchLimits(per_task_per_day=3, per_task_lifetime=10, cooldown_seconds=60)
        manager.record_dispatch(
            task.id,
            actor=HUMAN,
            run_id="run_a",
            agent="claude",
            runner="fake",
            mode=DispatchMode.SESSION,
            posture=DispatchPosture.AUTO,
            trigger=DispatchTrigger.CHAIN,
            caused_by=1,
            argv=["fake"],
            cwd=".",
            git_head="0000000",
        )
        fresh = manager.get_task(task.id)
        assert fresh is not None

        assert check_budget(fresh, limits, now=moment, trigger=DispatchTrigger.CHAIN) is None
        outside = check_budget(fresh, limits, now=moment, trigger=DispatchTrigger.MANUAL)
        assert outside is not None and outside.limit == "cooldown"

    def test_the_lifetime_cap_still_counts_dispatches_inside_a_chain(
        self, manager: TaskManager, project: Project, home: Path, check_script: Path
    ) -> None:
        """The backstop under the driver. Redefining it would make it not one."""
        task = a_task(manager, check_script)
        limits = AutoDispatchLimits(per_task_per_day=3, per_task_lifetime=2, cooldown_seconds=0)
        for index in range(2):
            manager.record_dispatch(
                task.id,
                actor=HUMAN,
                run_id=f"run_{index}",
                agent="claude",
                runner="fake",
                mode=DispatchMode.SESSION,
                posture=DispatchPosture.AUTO,
                trigger=DispatchTrigger.CHAIN,
                caused_by=1,
                argv=["fake"],
                cwd=".",
                git_head="0000000",
            )
        fresh = manager.get_task(task.id)
        assert fresh is not None

        refusal = check_budget(fresh, limits, trigger=DispatchTrigger.CHAIN)

        assert refusal is not None and refusal.limit == "per_task_lifetime"

    def test_a_fourth_chain_in_a_day_is_refused(
        self, manager: TaskManager, project: Project, home: Path, check_script: Path
    ) -> None:
        """What the per-day cap still says when it counts chains: stop restarting this."""
        task = a_task(manager, check_script)
        limits = AutoDispatchLimits(per_task_per_day=3, per_task_lifetime=99, cooldown_seconds=0)
        current = task
        for _ in range(4):
            outcome = authorize(manager, project, current, home)
            current = revoke_chain(manager=manager, task=outcome.task, actor=HUMAN)

        fresh = manager.get_task(task.id)
        assert fresh is not None
        assert chains_since(fresh, datetime.now(timezone.utc) - timedelta(days=1)) == 4

        refusal = check_budget(fresh, limits, trigger=DispatchTrigger.CHAIN)

        assert refusal is not None and refusal.limit == "per_task_per_day"
        assert "authorisations rather than iterations" in refusal.message


# ---------------------------------------------------------------------------
# the vector, which both guardrails compare
# ---------------------------------------------------------------------------


class TestTheVector:
    def test_it_is_ids_and_statuses_and_nothing_else(self) -> None:
        from agentjobs.models_v2 import CheckOutcome

        first = [
            CheckOutcome(
                id="sc-1", status=AcceptanceStatus.FAILED, exit_code=1, duration_seconds=1.0
            ),
            CheckOutcome(id="sc-2", status=AcceptanceStatus.MET, exit_code=0, duration_seconds=2.0),
        ]
        second = [
            CheckOutcome(
                id="sc-1",
                status=AcceptanceStatus.FAILED,
                exit_code=1,
                duration_seconds=9.0,
                output_tail="a different line failed this time",
            ),
            CheckOutcome(id="sc-2", status=AcceptanceStatus.MET, exit_code=0, duration_seconds=0.5),
        ]

        assert vector_of(first) == vector_of(second)


# ---------------------------------------------------------------------------
# sc-3: a run principal is refused at both verbs
# ---------------------------------------------------------------------------


class TestARunMayNotAuthorize:
    """Over HTTP, with a real run credential, because that is where it would happen.

    The table test above asserts the classification. This asserts the consequence,
    because a rule and its enforcement are two things and this codebase has the
    application-wide dependency precisely so they cannot drift.
    """

    def test_a_run_is_refused_at_both_verbs(self, served, check_script: Path) -> None:
        client, root, home = served
        manager = TaskManager(task_store(root / "tasks", project_id="sandbox"))
        task = a_task(manager, check_script)

        credential = a_run_credential(home, task.id)
        del client  # the run gets its own, for the reason below
        # Constructed, not entered: a second lifespan would run the startup reconciler
        # again, which finds this run's recorded pid absent and marks it interrupted --
        # and a credential dies with its run.
        run = TestClient(
            app, client=("127.0.0.1", 51000), headers={RUN_CREDENTIAL_HEADER: credential}
        )
        for path, body in (
            (f"/api/projects/sandbox/tasks/{task.id}/chain", {"user": HUMAN}),
            (f"/api/projects/sandbox/tasks/{task.id}/chain/revoke", {"user": HUMAN}),
        ):
            response = run.post(path, json=body)

            assert response.status_code == 403, (path, response.text)
            assert response.json()["code"] == "capability_denied"

    def test_the_owner_is_not(self, served, check_script: Path) -> None:
        client, root, _home = served
        manager = TaskManager(task_store(root / "tasks", project_id="sandbox"))
        task = a_task(manager, check_script)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task.id}/chain",
            json={"user": HUMAN, "max_iterations": 3},
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["max_iterations"] == 3
        assert body["live"] is True
        assert body["criteria"] == ["sc-1"]
        assert [item["iteration"] for item in body["iterations"]] == [0]

    def test_an_agent_id_may_not_authorise_a_chain(self, served, check_script: Path) -> None:
        """The same refusal a dispatch gets, reached through the same function."""
        client, root, _home = served
        manager = TaskManager(task_store(root / "tasks", project_id="sandbox"))
        task = a_task(manager, check_script)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task.id}/chain", json={"user": "claude"}
        )

        assert response.status_code in {403, 409}, response.text
        assert response.json()["code"] == "authorizer_not_human"

    def test_revoke_is_one_click_with_an_empty_body(self, served, check_script: Path) -> None:
        client, root, _home = served
        manager = TaskManager(task_store(root / "tasks", project_id="sandbox"))
        task = a_task(manager, check_script)
        client.post(f"/api/projects/sandbox/tasks/{task.id}/chain", json={"user": HUMAN})

        response = client.post(
            f"/api/projects/sandbox/tasks/{task.id}/chain/revoke", json={"user": HUMAN}
        )

        assert response.status_code == 200, response.text
        chains = response.json()["chains"]
        assert len(chains) == 1
        assert chains[0]["revoked"] is True
        assert chains[0]["live"] is False

    def test_the_panel_reads_the_history_back(self, served, check_script: Path) -> None:
        client, root, _home = served
        manager = TaskManager(task_store(root / "tasks", project_id="sandbox"))
        task = a_task(manager, check_script)
        client.post(f"/api/projects/sandbox/tasks/{task.id}/chain", json={"user": HUMAN})

        response = client.get(f"/api/projects/sandbox/tasks/{task.id}/chains")

        assert response.status_code == 200, response.text
        chains = response.json()["chains"]
        assert len(chains) == 1
        assert chains[0]["digest_matches"] is True
        assert chains[0]["iterations"][0]["results"][0]["status"] == "failed"


# ---------------------------------------------------------------------------
# sc-11, sc-12, sc-13: an answer never starts a run it cannot do
# ---------------------------------------------------------------------------


class TestAnswersThatStartNothing:
    """Task-150 section 4, replayed against the shape of this task's own entries 4-7."""

    def test_a_handback_refused_for_unmet_needs_moves_the_ball(
        self, manager: TaskManager, project: Project, home: Path
    ) -> None:
        """The exact failure: an answer to a question on a blocked task.

        An agent asks; the owner answers; the ball goes to ``agent``/``answer``; the
        dispatcher cannot claim because a dependency is open. Before task-150 the record
        was left saying an agent had it. Now the ball names the dependency.
        """
        blocker = manager.create_task(
            title="Phase one",
            category="general",
            summary="Has to land first.",
            description="The dependency.",
            lifecycle=Lifecycle.READY,
            actor=HUMAN,
        )
        blocked = manager.create_task(
            title="Phase two",
            category="general",
            summary="Waits on phase one.",
            description="The blocked task.",
            lifecycle=Lifecycle.READY,
            actor=HUMAN,
            dependencies=[{"task": blocker.id, "type": "needs"}],
        )
        manager.handoff(
            blocked.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.DECISION,
            ball_prompt="Build it?",
        )
        manager.handoff(
            blocked.id,
            actor=HUMAN,
            ball=Ball.AGENT,
            ball_reason=BallReason.ANSWER,
            ball_prompt="Decide after phase one.",
        )
        answered = manager.get_task(blocked.id)
        assert answered is not None and answered.ball is Ball.AGENT

        outcome = deliver_handback(
            manager=manager,
            project=project,
            project_config=PROJECT_CONFIG,
            task=answered,
            home=home,
        )
        record_handback(manager, answered, outcome)

        assert outcome.reason == "unmet_dependencies"
        assert any(blocker.id in item for item in outcome.open_tasks)
        after = manager.get_task(blocked.id)
        assert after is not None
        assert after.ball is Ball.EXTERNAL
        assert after.ball_reason is BallReason.DEPENDENCY
        assert blocker.id in (after.ball_prompt or "")
        # One write, not two: the refusal is the handoff's body.
        assert after.log[-1].type is LogEntryType.HANDOFF
        assert blocker.id in (after.log[-1].body or "")

    def test_an_option_may_say_it_does_not_dispatch(self) -> None:
        option = QuestionOption(
            label="Decide after phase one",
            description="Leave it parked.",
            dispatches=False,
            ball=Ball.EXTERNAL,
            ball_reason=BallReason.DEPENDENCY,
        )

        assert option.dispatches is False
        assert option.ball is Ball.EXTERNAL

    def test_such_an_option_must_name_a_holder(self) -> None:
        with pytest.raises(ValidationError):
            QuestionOption(label="Wait", dispatches=False)

    def test_a_dispatching_option_may_not_name_one(self) -> None:
        with pytest.raises(ValidationError):
            QuestionOption(label="Go", ball=Ball.HUMAN, ball_reason=BallReason.DECISION)

    def test_answering_with_it_starts_nothing(self, served) -> None:
        """The whole of sc-12, over the endpoint a person's browser actually posts to."""
        client, root, home = served
        manager = TaskManager(task_store(root / "tasks", project_id="sandbox"))
        task = manager.create_task(
            title="Build the loop?",
            category="general",
            summary="A decision.",
            description="Whether to build it.",
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        manager.handoff(
            task.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.DECISION,
            ball_prompt="Build bounded agent loops?",
            questions=[
                QuestionDraft.model_validate(
                    {
                        "body": "Build bounded agent loops?",
                        "options": [
                            {
                                "label": "Decide after phase one",
                                "description": "Leave it parked.",
                                "recommended": True,
                                "dispatches": False,
                                "ball": "external",
                                "ball_reason": "dependency",
                            },
                            {"label": "Yes, build it"},
                        ],
                    }
                )
            ],
        )
        asked = manager.get_task(task.id)
        assert asked is not None
        question = [item for item in asked.log if item.type is LogEntryType.QUESTION][-1]

        response = client.post(
            f"/api/projects/sandbox/tasks/{task.id}/answer",
            json={
                "user": HUMAN,
                "answers": [{"re": question.id, "selected": ["Decide after phase one"]}],
            },
        )

        assert response.status_code == 200, response.text
        after = manager.get_task(task.id)
        assert after is not None
        assert after.ball is Ball.EXTERNAL
        assert after.ball_reason is BallReason.DEPENDENCY
        # The choice is on the record, which is the other half of "records the choice
        # without starting a run".
        answers = [item for item in after.log if item.type is LogEntryType.ANSWER]
        assert answers[-1].data["selected"] == ["Decide after phase one"]
        assert list_runs(home) == []

    def test_a_dispatching_option_still_hands_work_back(self, served) -> None:
        """The default is unchanged, which is what keeps every existing question working."""
        client, root, _home = served
        manager = TaskManager(task_store(root / "tasks", project_id="sandbox"))
        task = manager.create_task(
            title="Build the loop?",
            category="general",
            summary="A decision.",
            description="Whether to build it.",
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        manager.handoff(
            task.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.DECISION,
            ball_prompt="Build bounded agent loops?",
            questions=[
                QuestionDraft.model_validate(
                    {
                        "body": "Build bounded agent loops?",
                        "options": [
                            {
                                "label": "Decide after phase one",
                                "dispatches": False,
                                "ball": "external",
                                "ball_reason": "dependency",
                            },
                            {"label": "Yes, build it"},
                        ],
                    }
                )
            ],
        )
        asked = manager.get_task(task.id)
        assert asked is not None
        question = [item for item in asked.log if item.type is LogEntryType.QUESTION][-1]

        client.post(
            f"/api/projects/sandbox/tasks/{task.id}/answer",
            json={
                "user": HUMAN,
                "answers": [{"re": question.id, "selected": ["Yes, build it"]}],
            },
        )

        after = manager.get_task(task.id)
        assert after is not None
        assert after.ball is Ball.AGENT
        # `work` rather than `answer`, because auto-dispatch is on in this fixture and
        # the answer really did start a run, which claimed the task. That is the point:
        # the default option hands work back exactly as it always has.
        assert [item.type for item in after.log if item.type is LogEntryType.DISPATCH]

    def test_free_text_keeps_the_ordinary_path(self, served) -> None:
        """Anything typed may be an instruction, and nothing here can tell."""
        client, root, _home = served
        manager = TaskManager(task_store(root / "tasks", project_id="sandbox"))
        task = manager.create_task(
            title="Build the loop?",
            category="general",
            summary="A decision.",
            description="Whether to build it.",
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        manager.handoff(
            task.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.DECISION,
            ball_prompt="Build bounded agent loops?",
            questions=[
                QuestionDraft.model_validate(
                    {
                        "body": "Build bounded agent loops?",
                        "options": [
                            {
                                "label": "Decide after phase one",
                                "dispatches": False,
                                "ball": "external",
                                "ball_reason": "dependency",
                            }
                        ],
                    }
                )
            ],
        )
        asked = manager.get_task(task.id)
        assert asked is not None
        question = [item for item in asked.log if item.type is LogEntryType.QUESTION][-1]

        client.post(
            f"/api/projects/sandbox/tasks/{task.id}/answer",
            json={
                "user": HUMAN,
                "feedback": "Wait -- but first go and measure how long a chain takes.",
                "answers": [{"re": question.id, "selected": ["Decide after phase one"]}],
            },
        )

        after = manager.get_task(task.id)
        assert after is not None
        assert after.ball is Ball.AGENT

    def test_handing_a_blocked_task_to_a_human_warns(self, manager: TaskManager) -> None:
        """sc-13, at the moment of the write, which is the only moment it is cheap."""
        blocker = manager.create_task(
            title="Phase one",
            category="general",
            summary="Has to land first.",
            description="The dependency.",
            lifecycle=Lifecycle.READY,
            actor=HUMAN,
        )
        blocked = manager.create_task(
            title="Phase two",
            category="general",
            summary="Waits on phase one.",
            description="The blocked task.",
            lifecycle=Lifecycle.READY,
            actor=HUMAN,
            dependencies=[{"task": blocker.id, "type": "needs"}],
        )
        asked = manager.handoff(
            blocked.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.DECISION,
            ball_prompt="Build it?",
        )
        unmet = manager.dependency_facts()[blocked.id].unmet_needs

        warnings = check_record(asked, verb="handoff", unmet_needs=unmet)

        assert [item.kind for item in warnings] == [BLOCKED_HANDOFF]
        assert blocker.id in warnings[0].message

    def test_it_is_silent_on_an_unblocked_handoff(self, manager: TaskManager) -> None:
        """Silence on an ordinary write is this module's requirement, not its aspiration."""
        task = manager.create_task(
            title="Ordinary",
            category="general",
            summary="Nothing blocks it.",
            description="A task.",
            lifecycle=Lifecycle.READY,
            actor=HUMAN,
        )
        asked = manager.handoff(
            task.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Review the branch.",
        )

        assert check_record(asked, verb="handoff", unmet_needs=()) == []

    def test_it_is_silent_when_the_ball_goes_to_the_dependency(self, manager: TaskManager) -> None:
        """``external``/``dependency`` is the right answer, so it draws no comment."""
        blocker = manager.create_task(
            title="Phase one",
            category="general",
            summary="Has to land first.",
            description="The dependency.",
            lifecycle=Lifecycle.READY,
            actor=HUMAN,
        )
        blocked = manager.create_task(
            title="Phase two",
            category="general",
            summary="Waits on phase one.",
            description="The blocked task.",
            lifecycle=Lifecycle.READY,
            actor=HUMAN,
            dependencies=[{"task": blocker.id, "type": "needs"}],
        )
        parked = manager.handoff(
            blocked.id,
            actor="claude",
            ball=Ball.EXTERNAL,
            ball_reason=BallReason.DEPENDENCY,
            ball_prompt=f"Waiting on {blocker.id}.",
        )
        unmet = manager.dependency_facts()[blocked.id].unmet_needs

        assert check_record(parked, verb="handoff", unmet_needs=unmet) == []
