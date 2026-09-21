"""A second finish for one task, spawned by a run's exit (task-514).

**The incident.** task-506, 2026-09-20. A human approved at 21:30:11 and `fin_9323da60`
started three seconds later and took the merge runway. At 21:30:44 the run that had been
working the task exited; `resume_approved_finish` fired on that exit and spawned
`fin_76cf6a4e` for the same task on the same approval. It cleared preflight, found the
runway held, and **queued** -- because the runway is a queue, not a refusal, with an hour
on it. At 21:45 the first finish merged, closed the task, removed the worktree and
deleted the branch. At 22:05 the duplicate was still queued, still `outcome: running`,
and was killed by hand; it never wrote a terminal outcome, so nothing on disk said how it
ended. A person watching saw **Completed** on the task page, **Finishing** on the slot
board, and "Queued for the merge runway -- this is the queue working, not a stall" as the
task's visible tail, twenty minutes after the work was done.

`resume_approved_finish` stated the assumption in its own docstring: *"A duplicate is
harmless -- the second finish meets the first one's lock and declines."* Two things were
wrong with that. The per-task lock is not the first lock a second finish meets; and the
lock was not even there, because the poller settling the run had deleted the *finish's*
lock on its way past.

**So the refusals are at the finish's own front door**, never in who may spawn one: both
callers of `resume_approved_finish` are allowed to be speculative, and that is the design.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, Iterator, List, Optional

import pytest
import yaml

from agentjobs.api.routes.tasks import APPROVAL_CLEARANCE
from agentjobs.dispatch.approval import (
    accept_signals,
    approval_data,
    consuming_finish,
    dispose,
    reviewed_branches,
    standing_approval_for,
)
from agentjobs.dispatch.finish import (
    DECLINED,
    DUPLICATE_KEY,
    FINISHED,
    Declined,
    LockHolder,
    _premises,
    already_in_flight,
    finish_task,
    finishes_root,
)
from agentjobs.dispatch.ledger import (
    RunLockTimeout,
    acquire_runway_lock,
    locks_root,
    runway_lock_name,
)
from agentjobs.models_v2 import Ball, BallReason, Outcome
from test_dispatch_finish import (  # noqa: F401 - `world` is a fixture below
    DISPATCHABLE_CONFIG,
    git,
    head,
    merged_into,
    settings,
)
import test_dispatch_finish

APPROVER = "Jeff Posey"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """``test_dispatch_finish``'s real clone, branch, worktree and task, reused unchanged."""
    built: Dict[str, Any] = test_dispatch_finish.world.__pytest_wrapped__.obj(  # type: ignore[attr-defined]
        tmp_path, monkeypatch
    )
    return built


# ----- the shapes the incident was made of ------------------------------------


@pytest.fixture
def a_live_peer() -> Iterator[int]:
    """The pid of a real process that is genuinely running while the test runs.

    ``os.getpid()`` will not do, and the reason is the refusal's own rule: an attempt
    recording *this* pid is this process's own earlier attempt, which is what a crash
    recovery looks like from the inside. Only a peer counts, so the tests stand one up.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait(timeout=30)


def a_finish_is_working_this_task(
    world: Dict[str, Any], pid: int, finish_id: str = "fin_working"
) -> Path:
    """A finish directory exactly as ``FinishDirectory.create`` leaves one, still running.

    ``pid`` is a live peer process -- the one fact the in-flight check turns on, and the
    one that separates this from an attempt the machine killed.
    """
    directory = finishes_root(world["home"]) / finish_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "meta.yaml").write_text(
        yaml.safe_dump(
            {
                "finish_id": finish_id,
                "task_id": world["task_id"],
                "project_id": "demo",
                "outcome": "running",
                "started_at": "2026-09-20T02:30:14+00:00",
                "pid": pid,
                "authority": "approval",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return directory


def approve(world: Dict[str, Any]) -> int:
    """Approve exactly as the route does: clearance, receipt, and the synchronous accept.

    The project's own config is written first, because ``_standing_approval`` reads it
    from disk to decide whether the handoff's author is a human: without it the approval
    is on the record and stands for nobody.
    """
    root = world["root"]
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(DISPATCHABLE_CONFIG), encoding="utf-8"
    )
    (root / ".gitignore").write_text(".agentjobs/\n", encoding="utf-8")
    git(root, "add", "--", ".gitignore")
    git(root, "commit", "-m", "chore: ignore the machine-local config")
    manager = world["manager"]
    task = manager.get_task(world["task_id"])
    approved = manager.handoff(
        world["task_id"],
        actor=APPROVER,
        ball=Ball.AGENT,
        ball_reason=BallReason.WORK,
        ball_prompt=APPROVAL_CLEARANCE,
        body=f"Approved by {APPROVER} through the web UI.",
        data=approval_data(
            approver=APPROVER, note="", reviewed=reviewed_branches(world["root"], task)
        ),
    )
    accept_signals(world["home"], "demo", approved, [approved.log[-1]])
    return int(approved.log[-1].id)


def receipt_for(world: Dict[str, Any]) -> Any:
    task = world["manager"].get_task(world["task_id"])
    found = standing_approval_for(world["home"], task, DISPATCHABLE_CONFIG, project_id="demo")
    assert found is not None, "the fixture did not leave a standing approval"
    return found


def spend_the_approval(world: Dict[str, Any], by: str) -> None:
    """Mark the approval consumed by another finish, as a finish's own ending does."""
    task = world["manager"].get_task(world["task_id"])
    assert task is not None
    receipt = receipt_for(world)
    entry = next(item for item in task.log if item.id == receipt.entry_id)
    dispose(
        world["home"],
        "demo",
        task,
        entry,
        status="consumed",
        disposition={"by": f"finish:{by}", "approver": APPROVER},
    )


def offer_the_finish(world: Dict[str, Any]) -> None:
    """A machine whose dispatch config switches the scripted finish on for ``demo``."""
    (world["home"] / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "runners": {"claude": {"argv": ["claude", "-p", "{prompt}"]}},
                "projects": {
                    "demo": {
                        "enabled": True,
                        "runner": "claude",
                        "finish": {"enabled": True, "base_branch": "main"},
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def run(world: Dict[str, Any], **overrides: Any) -> Any:
    return finish_task(
        manager=world["manager"],
        project=world["project"],
        task_id=world["task_id"],
        approver=APPROVER,
        home=world["home"],
        api_base="http://127.0.0.1:1",
        settings=settings(**{k: v for k, v in overrides.items() if k != "speculative"}),
        speculative=bool(overrides.get("speculative")),
    )


def meta_of(result: Any) -> Dict[str, Any]:
    assert result.directory is not None
    loaded = yaml.safe_load((result.directory / "meta.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def steps_named(world: Dict[str, Any], name: str) -> List[Any]:
    task = world["manager"].get_task(world["task_id"])
    assert task is not None
    return [entry for entry in task.log if entry.data.get("finish_step") == name]


def runway_is_free(world: Dict[str, Any]) -> bool:
    return not (locks_root(world["home"]) / f"{runway_lock_name(world['root'])}.lock").exists()


# ----- ac-1 and ac-5: the race itself -----------------------------------------


class TestASecondFinishNeverReachesTheRunway:
    """ac-1, ac-5. The refusal is at the front door, before anything is taken.

    "Before the runway" is the whole of it. A finish that reaches the runway does not
    decline; it queues, for up to ``runway_timeout_seconds`` -- an hour by default -- and
    it costs the next finish in line its place. task-509's finish was behind this one.
    """

    def test_a_finish_declines_when_another_one_is_already_working_the_task(
        self, world: Dict[str, Any], a_live_peer: int
    ) -> None:
        a_finish_is_working_this_task(world, a_live_peer)
        before = head(world["root"], "main")

        result = run(world)

        assert result.outcome == DECLINED
        assert result.reason == "finish_in_flight"
        assert "fin_working" in result.detail
        assert head(world["root"], "main") == before
        assert not merged_into(world["root"], world["branch"])

    def test_it_never_reaches_the_runway_or_the_record(
        self, world: Dict[str, Any], a_live_peer: int
    ) -> None:
        """The two things the duplicate did that cost the hour, asserted as absences.

        It joined the queue, and it wrote "this is the queue working, not a stall" onto a
        task whose work was about to be finished by somebody else.
        """
        a_finish_is_working_this_task(world, a_live_peer)

        run(world)

        assert runway_is_free(world), "a duplicate took or queued for the merge runway"
        assert steps_named(world, "runway_queued") == []
        assert steps_named(world, "started") == []

    def test_it_writes_a_terminal_outcome_of_its_own(
        self, world: Dict[str, Any], a_live_peer: int
    ) -> None:
        """``fin_76cf6a4e`` died holding ``outcome: running``, which is why the board never cleared."""
        a_finish_is_working_this_task(world, a_live_peer)

        meta = meta_of(run(world))

        assert meta["outcome"] == DECLINED
        assert meta["reason"] == "finish_in_flight"
        assert meta["finished_at"]
        assert meta[DUPLICATE_KEY] == "fin_working"

    def test_an_attempt_whose_process_is_gone_does_not_block_a_new_one(
        self, world: Dict[str, Any], a_live_peer: int
    ) -> None:
        """The refusal is about a finish that is *working*, not about a directory existing.

        A machine that rebooted mid-gate leaves a directory reading ``outcome: running``
        for ever. Treating that as in flight would make its task unfinishable, which is
        the failure ``finish_resume`` exists to repair rather than to inherit.
        """
        directory = a_finish_is_working_this_task(world, a_live_peer)
        meta = yaml.safe_load((directory / "meta.yaml").read_text(encoding="utf-8"))
        meta["pid"] = 999_999_999
        (directory / "meta.yaml").write_text(yaml.safe_dump(meta, sort_keys=False), "utf-8")

        result = run(world)

        assert result.outcome == FINISHED, result.render()

    def test_a_finish_for_another_task_is_not_this_ones_business(
        self, world: Dict[str, Any], a_live_peer: int
    ) -> None:
        directory = finishes_root(world["home"]) / "fin_elsewhere"
        directory.mkdir(parents=True)
        (directory / "meta.yaml").write_text(
            yaml.safe_dump(
                {
                    "finish_id": "fin_elsewhere",
                    "task_id": "task-999",
                    "project_id": "demo",
                    "outcome": "running",
                    "pid": a_live_peer,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        assert already_in_flight(world["home"], world["task_id"], project_id="demo") is None

    def test_an_attempt_recording_this_very_process_is_not_a_peer(
        self, world: Dict[str, Any]
    ) -> None:
        """A crash inside one process is a recovery, and every finish is its own process.

        ``test_finish_durable`` simulates a crash by raising inside the finish and calling
        it again, which leaves an attempt recording this pid. Reading that as contention
        would refuse every one of those recoveries.
        """
        a_finish_is_working_this_task(world, os.getpid())

        assert already_in_flight(world["home"], world["task_id"], project_id="demo") is None

    def test_a_declined_duplicate_does_not_itself_block_the_next_attempt(
        self, world: Dict[str, Any], a_live_peer: int
    ) -> None:
        """Otherwise the first duplicate poisons the task for every later finish."""
        a_finish_is_working_this_task(world, a_live_peer)
        first = run(world)
        assert first.outcome == DECLINED

        assert already_in_flight(world["home"], world["task_id"], project_id="demo") is not None
        # ...and once the real one is gone, nothing is left holding the task.
        meta_path = finishes_root(world["home"]) / "fin_working" / "meta.yaml"
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        meta["pid"] = 999_999_999
        meta_path.write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")

        assert already_in_flight(world["home"], world["task_id"], project_id="demo") is None


# ----- ac-2: the approval is spent --------------------------------------------


class TestAnApprovalAuthorisesOneFinish:
    """ac-2. The evidence that outlives the finish that spent it.

    A lock answers "is one running *now*", so it cannot catch the duplicate that is
    served after the first finish has ended -- which is the shape `fin_76cf6a4e` was in
    for the last twenty minutes of its life. ``_consume_approval`` already wrote the
    fact down, naming the finish; nothing had ever read it back.
    """

    def test_a_spawned_finish_declines_on_an_approval_another_finish_consumed(
        self, world: Dict[str, Any]
    ) -> None:
        approve(world)
        spend_the_approval(world, "fin_first")

        result = run(world, speculative=True)

        assert result.outcome == DECLINED
        assert result.reason == "approval_consumed"
        assert "fin_first" in result.detail
        assert meta_of(result)[DUPLICATE_KEY] == "fin_first"
        assert not merged_into(world["root"], world["branch"])

    def test_the_disposition_is_read_back_exactly_as_a_finish_writes_it(
        self, world: Dict[str, Any]
    ) -> None:
        approve(world)
        assert consuming_finish(world["home"], "demo", receipt_for(world)) == ""

        spend_the_approval(world, "fin_first")

        assert consuming_finish(world["home"], "demo", receipt_for(world)) == "fin_first"

    def test_running_the_command_by_hand_is_not_refused(self, world: Dict[str, Any]) -> None:
        """The documented retry, and the reason this refusal is not unconditional.

        A finish that escalates consumes its approval on the way out, and ENGINEERING.md
        says the way to retry it is to run ``agentjobs finish`` once its cause is fixed.
        A person typing that is asserting an intent no poller has; only an attempt
        *spawned* on a guess is refused here.
        """
        approve(world)
        spend_the_approval(world, "fin_first")

        result = run(world)

        assert result.outcome == FINISHED, result.render()

    def test_an_unspent_approval_still_finishes(self, world: Dict[str, Any]) -> None:
        """ac-4's half of this refusal: too broad a rule fails here.

        This is task-312's case with the run already gone -- the approval stands, nothing
        has acted on it, and a speculatively spawned finish is exactly what it is owed.
        """
        approve(world)

        result = run(world, speculative=True)

        assert result.outcome == FINISHED, result.render()
        # `merged_into` is unaskable here: a finish deletes the branch it merged.
        assert result.merge_commit


# ----- ac-3: the queue re-asks whether it still has anything to land -----------


class _WaitedItOut(BaseException):
    """Raised by the stand-in runway when a finish queued for the whole wait.

    ``BaseException`` deliberately: ``_guarded_sequence`` turns every ``Exception`` into
    an ``escalated`` finish, so an ordinary assertion inside the stand-in would be
    reported as the thing under test failing in a different way.
    """


class _QueuedRunway:
    """A runway that is busy, announces itself, and then polls -- the duplicate's shape.

    Standing up a second real finish to hold the strip would be testing the lock, which
    ``TestTheRunway`` already does against the real one. What is under test here is what
    a *queued* finish does when the work it is queued for stops existing underneath it.
    """

    def __init__(self, between_polls: Any, polls: int = 5) -> None:
        self.between_polls = between_polls
        self.polls = polls

    def __call__(self, home: Path, root: Path, **kwargs: Any) -> Any:
        on_wait = kwargs.get("on_wait")
        on_poll = kwargs.get("on_poll")
        if on_wait is not None:
            on_wait(LockHolder(pid=1234, kind="runway", finish_id="fin_working"))
        for _ in range(self.polls):
            self.between_polls()
            if on_poll is not None:
                on_poll()
        raise _WaitedItOut("the queued finish waited out the runway instead of leaving it")


@pytest.fixture
def instant_premise_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ten seconds between premise checks is right in production and pointless here."""
    monkeypatch.setattr("agentjobs.dispatch.finish.PREMISE_POLL_SECONDS", 0.0)


@pytest.mark.usefixtures("instant_premise_checks")
class TestAQueuedFinishLeavesTheQueueWhenTheWorkIsGone:
    """ac-3. Waiting an hour to be told the worktree is gone is the worst outcome available."""

    def test_it_leaves_when_the_task_closes_underneath_it(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def close_it() -> None:
            task = world["manager"].get_task(world["task_id"])
            if task is not None and task.is_open:
                world["manager"].close_task(
                    world["task_id"], actor="claude", outcome=Outcome.COMPLETED
                )

        monkeypatch.setattr(
            "agentjobs.dispatch.finish.acquire_runway_lock", _QueuedRunway(close_it)
        )

        result = run(world)

        assert result.outcome == DECLINED
        assert result.reason == "overtaken"
        assert "was closed while this finish was queued" in result.detail
        assert not merged_into(world["root"], world["branch"])

    def test_it_says_so_where_the_queue_note_said_otherwise(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The queue note is reassuring and correct in general; it was the visible tail here."""

        def merge_it_elsewhere() -> None:
            git(world["root"], "merge", "--no-ff", "-m", "somebody else's merge", world["branch"])

        monkeypatch.setattr(
            "agentjobs.dispatch.finish.acquire_runway_lock", _QueuedRunway(merge_it_elsewhere)
        )

        result = run(world)

        assert result.reason == "overtaken"
        queued = steps_named(world, "runway_queued")
        left = steps_named(world, "runway_left")
        assert queued and left, "it queued loudly and left silently"
        assert task_log_order(world, queued[-1].id) < task_log_order(world, left[-1].id)
        assert "already in `main`" in (left[-1].body or "")

    def test_it_writes_a_terminal_outcome(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What ``fin_76cf6a4e`` never did, which is why the slot board never cleared."""

        def close_it() -> None:
            task = world["manager"].get_task(world["task_id"])
            if task is not None and task.is_open:
                world["manager"].close_task(
                    world["task_id"], actor="claude", outcome=Outcome.COMPLETED
                )

        monkeypatch.setattr(
            "agentjobs.dispatch.finish.acquire_runway_lock", _QueuedRunway(close_it)
        )

        result = run(world)

        meta = meta_of(result)
        assert meta["outcome"] == DECLINED and meta["reason"] == "overtaken"
        assert meta["finished_at"]
        assert runway_is_free(world)

    def test_a_queue_whose_premises_all_still_hold_waits_as_it_always_did(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal must not fire on a finish that is simply third in line."""
        monkeypatch.setattr(
            "agentjobs.dispatch.finish.acquire_runway_lock",
            _QueuedRunway(lambda: None, polls=3),
        )

        with pytest.raises(_WaitedItOut):
            run(world)


def task_log_order(world: Dict[str, Any], entry_id: int) -> int:
    task = world["manager"].get_task(world["task_id"])
    assert task is not None
    return [entry.id for entry in task.log].index(entry_id)


class TestWhatThePremisesRead:
    """Each premise from the authority that owns it, and every failure answers "keep waiting"."""

    def test_nothing_gone_reads_as_nothing_gone(self, world: Dict[str, Any]) -> None:
        assert _premises(world["manager"], world["task_id"], plan_for(world))() is None

    def test_a_deleted_branch_is_named(self, world: Dict[str, Any]) -> None:
        plan = plan_for(world)
        git(world["root"], "worktree", "remove", "--force", str(world["worktree"]))
        git(world["root"], "branch", "-D", world["branch"])

        assert "no longer exists" in (_premises(world["manager"], world["task_id"], plan)() or "")

    def test_a_manager_that_will_not_answer_keeps_the_finish_waiting(
        self, world: Dict[str, Any]
    ) -> None:
        """An unreachable store is not evidence that the work is done."""

        class _Refuses:
            def get_task(self, task_id: str) -> Any:
                raise RuntimeError("the store is busy")

        assert _premises(_Refuses(), world["task_id"], plan_for(world))() is None  # type: ignore[arg-type]


def plan_for(world: Dict[str, Any]) -> Any:
    from agentjobs.dispatch.finish import preflight

    task = world["manager"].get_task(world["task_id"])
    assert task is not None
    return preflight(task, world["root"], settings())


# ----- the hook itself --------------------------------------------------------


class TestTheRunwayHookIsReal:
    """``on_poll`` on the real ``acquire_runway_lock``, not on a stand-in for it."""

    def test_a_raise_from_on_poll_abandons_the_wait(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        root = tmp_path / "clone"
        root.mkdir()
        held = acquire_runway_lock(home, root, finish_id="fin_held")
        polls: List[float] = []

        def give_up() -> None:
            polls.append(time.monotonic())
            raise Declined("overtaken", "there is nothing left to land")

        try:
            began = time.monotonic()
            with pytest.raises(Declined):
                acquire_runway_lock(
                    home, root, finish_id="fin_queued", timeout=3600.0, poll=0.01, on_poll=give_up
                )
            assert time.monotonic() - began < 30.0
            assert len(polls) == 1
        finally:
            held.release()

    def test_a_silent_on_poll_changes_nothing_about_the_timeout(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        root = tmp_path / "clone"
        root.mkdir()
        held = acquire_runway_lock(home, root, finish_id="fin_held")
        try:
            with pytest.raises(RunLockTimeout):
                acquire_runway_lock(
                    home,
                    root,
                    finish_id="fin_queued",
                    timeout=0.3,
                    poll=0.01,
                    on_poll=lambda: None,
                )
        finally:
            held.release()


# ----- ac-4: the resume this was built for ------------------------------------


class TestTheResumeStillWorks:
    """ac-4. task-312's case is the whole reason ``resume_approved_finish`` exists.

    A run stood between an approval and its finish, and has now ended with the finish
    never started. The speculative spawn is the only thing that starts one. A refusal
    that caught this too would have traded an hour of one task's queue for every
    approval landing on a live session never merging at all.
    """

    def test_the_spawn_is_still_speculative_by_design(self, world: Dict[str, Any]) -> None:
        """It is told to be speculative rather than told to prove nothing is running."""
        spawned: Dict[str, Any] = {}

        def record(**kwargs: Any) -> Optional[str]:
            spawned.update(kwargs)
            return "spawned.log"

        import agentjobs.dispatch.finish as finish_module

        offer_the_finish(world)
        original = finish_module.spawn_finish
        finish_module.spawn_finish = record
        try:
            finish_module.resume_approved_finish(
                project_id="demo",
                task_id=world["task_id"],
                approver=APPROVER,
                home=world["home"],
            )
        finally:
            finish_module.spawn_finish = original

        assert spawned.get("speculative") is True
        assert spawned.get("task_id") == world["task_id"]

    def test_and_the_finish_it_spawns_merges(self, world: Dict[str, Any]) -> None:
        """Nothing is in flight and the approval is unspent, so the refusals stay out of it."""
        approve(world)

        result = run(world, speculative=True)

        assert result.outcome == FINISHED, result.render()
        task = world["manager"].get_task(world["task_id"])
        assert task is not None and not task.is_open
