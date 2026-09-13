"""A question to the conversation is not a permission park (task-441).

On 2026-09-13 a dispatched supervisor asked the owner a multiple-choice question while
the owner was typing into the same session through Remote Control. Claude Code reports
that menu exactly as it reports a permission prompt -- ``waiting``/``blocked`` -- so the
dispatcher moved the ball to human/input, paged the owner, and quoted about 170 KB of the
same screen repainted a dozen times. It also left the run's meta reading ``parked`` after
the question was answered, so a later real park on that run would have been swallowed.

These tests drive the real ``poll_session`` against the session-mode fake CLI from
``test_dispatch_runner`` and a session JSONL written where Claude Code writes one. The
JSONL shapes are the ones session 7d09e034 recorded: an assistant ``tool_use`` named
``AskUserQuestion``, a ``tool_result`` naming its id, and human turns carrying
``origin: {kind: human}`` -- the dispatch prompt included.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List

import pytest

from agentjobs.dispatch.auth import CLAUDE_HOME_ENV
from agentjobs.dispatch.runner import (
    TERMINAL_EXCERPT_MAX_BYTES,
    RunHandle,
    SessionPhase,
    terminal_excerpt,
)
from agentjobs.dispatch.session_question import (
    PRESENCE_WINDOW_SECONDS,
    QUESTION_GRACE_SECONDS,
    pending_question_in,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, LogEntryType
from test_dispatch_runner import (  # noqa: F401 - fixtures are used by name
    build,
    fake_cli,
    manager,
    session_resolution,
    set_ledger,
    task,
    workspace,
)

SHORT = "b55b35ad"
FULL = "b55b35ad-0000-4000-8000-000000000441"
ESC = "\x1b"


# ----- a session JSONL, shaped like Claude Code's ------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def human(text: str, at: datetime) -> Dict[str, object]:
    return {
        "type": "user",
        "timestamp": _iso(at),
        "sessionId": FULL,
        "origin": {"kind": "human"},
        "message": {"role": "user", "content": text},
    }


def tool_call(name: str, tool_id: str, at: datetime, payload: object) -> Dict[str, object]:
    return {
        "type": "assistant",
        "timestamp": _iso(at),
        "sessionId": FULL,
        "message": {
            "model": "claude-opus-5",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": payload}],
        },
    }


def question(tool_id: str, at: datetime) -> Dict[str, object]:
    return tool_call(
        "AskUserQuestion",
        tool_id,
        at,
        {
            "questions": [
                {
                    "question": "Should a finish retry an unexplained red once?",
                    "header": "Flaky retry",
                    "multiSelect": False,
                    "options": [
                        {"label": "Yes, retry and record", "description": "One retry."},
                        {"label": "No, stop the finish", "description": "Keep the rule."},
                    ],
                }
            ]
        },
    )


def tool_result(tool_id: str, at: datetime) -> Dict[str, object]:
    return {
        "type": "user",
        "timestamp": _iso(at),
        "sessionId": FULL,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "answered"}],
        },
    }


def write_session_log(entries: List[Dict[str, object]]) -> Path:
    path = Path(os.environ[CLAUDE_HOME_ENV]) / "projects" / "C--sandbox" / f"{FULL}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    return path


def dispatch_prompt(handle: RunHandle) -> str:
    argv = handle.directory.read_meta()["argv"]
    assert isinstance(argv, list)
    return str(argv[-1])


def started(handle: RunHandle) -> datetime:
    return datetime.fromisoformat(str(handle.directory.read_meta()["started_at"]))


def parked_ledger(fake_cli: Path) -> None:  # noqa: F811
    set_ledger(fake_cli, [{"id": SHORT, "status": "waiting", "state": "blocked", "pid": 4242}])


def working_ledger(fake_cli: Path) -> None:  # noqa: F811
    set_ledger(fake_cli, [{"id": SHORT, "status": "busy", "state": "working", "pid": 4242}])


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now


# ----- a1: a question asked of a person who is there does not move the ball -----------


class TestAQuestionIsNotAPark:
    def test_a_question_in_a_live_conversation_moves_nothing_and_sends_nothing(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path  # noqa: F811
    ) -> None:
        clock = Clock()
        runner = build(workspace, manager, session_resolution(fake_cli), clock=clock)
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        base = started(handle)
        write_session_log(
            [
                human(dispatch_prompt(handle), base + timedelta(seconds=1)),
                human("ask that question simply and clearly", base + timedelta(seconds=60)),
                question("toolu_q1", base + timedelta(seconds=63)),
            ]
        )
        clock.now = base + timedelta(seconds=75)
        parked_ledger(fake_cli)
        before = manager.get_task(task.id)
        assert before is not None

        phase = runner.poll_session(handle)

        assert phase is SessionPhase.PARKED
        after = manager.get_task(task.id)
        assert after is not None
        assert (after.ball, after.ball_reason, after.ball_prompt) == (
            before.ball,
            before.ball_reason,
            before.ball_prompt,
        )
        assert [e for e in after.log if e.type is LogEntryType.HANDOFF] == [
            e for e in before.log if e.type is LogEntryType.HANDOFF
        ]
        meta = handle.directory.read_meta()
        assert meta.get("status") != "parked"
        assert meta.get("question_id") == "toolu_q1"

    def test_the_dispatch_prompt_is_not_a_person_in_the_conversation(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path  # noqa: F811
    ) -> None:
        """Claude Code records the dispatch prompt as a human turn, and nobody is reading."""
        clock = Clock()
        runner = build(workspace, manager, session_resolution(fake_cli), clock=clock)
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        base = started(handle)
        write_session_log(
            [
                human(dispatch_prompt(handle), base + timedelta(seconds=1)),
                question("toolu_q1", base + timedelta(seconds=30)),
            ]
        )
        clock.now = base + timedelta(seconds=40)
        parked_ledger(fake_cli)

        runner.poll_session(handle)

        after = manager.get_task(task.id)
        assert after is not None
        assert (after.ball, after.ball_reason) == (Ball.HUMAN, BallReason.INPUT)
        prompt = after.ball_prompt or ""
        assert "asked a question with nobody in the conversation" in prompt
        assert "Should a finish retry an unexplained red once?" in prompt
        assert "1. Yes, retry and record" in prompt
        assert "permission" not in prompt
        assert handle.directory.read_meta()["parked_on"] == "question"

    def test_an_unanswered_question_becomes_a_handoff_after_the_grace(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path  # noqa: F811
    ) -> None:
        """Held, not ignored: a mistaken "live" cannot become a silent hang."""
        clock = Clock()
        runner = build(workspace, manager, session_resolution(fake_cli), clock=clock)
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        base = started(handle)
        asked = base + timedelta(seconds=63)
        write_session_log(
            [human("are you there?", base + timedelta(seconds=60)), question("toolu_q1", asked)]
        )
        parked_ledger(fake_cli)

        clock.now = asked + timedelta(seconds=QUESTION_GRACE_SECONDS - 1)
        runner.poll_session(handle)
        held = manager.get_task(task.id)
        assert held is not None and held.ball is Ball.AGENT

        clock.now = asked + timedelta(seconds=QUESTION_GRACE_SECONDS + 1)
        runner.poll_session(handle)
        runner.poll_session(handle)

        after = manager.get_task(task.id)
        assert after is not None
        assert (after.ball, after.ball_reason) == (Ball.HUMAN, BallReason.INPUT)
        assert "has not been answered in 10 minutes" in (after.ball_prompt or "")
        handoffs = [
            e for e in after.log if e.type is LogEntryType.HANDOFF and e.actor == "dispatcher"
        ]
        assert len(handoffs) == 1, "a repeated poll must not hand off twice"

    def test_the_real_incident_timeline_reads_as_a_live_question(self, tmp_path: Path) -> None:
        """Session 7d09e034: the owner's message, 3 s later the question, answered 19 s on."""
        t0 = datetime(2026, 9, 13, 18, 4, 41, tzinfo=timezone.utc)
        entries = [
            human("the dispatch prompt", t0 - timedelta(hours=2)),
            human("ask that question simply and clearly", t0),
            question("toolu_016C", t0 + timedelta(seconds=3)),
        ]
        lines = [json.dumps(entry) for entry in entries]
        path = tmp_path / "log.jsonl"

        pending = pending_question_in(
            lines, session_id=SHORT, path=path, exclude_texts=["the dispatch prompt"]
        )
        assert pending is not None and pending.live()

        answered = lines + [json.dumps(tool_result("toolu_016C", t0 + timedelta(seconds=22)))]
        assert pending_question_in(answered, session_id=SHORT, path=path) is None

        stale = [
            json.dumps(human("hello", t0 - timedelta(seconds=PRESENCE_WINDOW_SECONDS + 5))),
            json.dumps(question("toolu_x", t0)),
        ]
        later = pending_question_in(stale, session_id=SHORT, path=path)
        assert later is not None and not later.live()


# ----- a2: a permission prompt is still a park, and still wakes the owner ---------------


class TestAPermissionPromptIsStillAPark:
    @pytest.mark.parametrize(
        "entries",
        [
            pytest.param(lambda base: [], id="no-session-log"),
            pytest.param(
                lambda base: [
                    human("go ahead", base + timedelta(seconds=5)),
                    tool_call("Bash", "toolu_b1", base + timedelta(seconds=6), {"command": "ls"}),
                ],
                id="pending-bash-call-in-a-live-conversation",
            ),
            pytest.param(
                lambda base: [
                    human("go ahead", base + timedelta(seconds=5)),
                    question("toolu_q1", base + timedelta(seconds=6)),
                    tool_result("toolu_q1", base + timedelta(seconds=8)),
                    tool_call("Bash", "toolu_b1", base + timedelta(seconds=9), {"command": "ls"}),
                ],
                id="answered-question-then-a-permission-prompt",
            ),
        ],
    )
    def test_a_permission_prompt_parks_and_hands_to_the_owner(
        self,
        workspace: Path,  # noqa: F811
        manager: TaskManager,  # noqa: F811
        task,  # noqa: F811
        fake_cli: Path,  # noqa: F811
        entries: Callable[[datetime], List[Dict[str, object]]],
    ) -> None:
        clock = Clock()
        runner = build(workspace, manager, session_resolution(fake_cli), clock=clock)
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        base = started(handle)
        written = entries(base)
        if written:
            write_session_log(written)
        clock.now = base + timedelta(seconds=20)
        parked_ledger(fake_cli)

        assert runner.poll_session(handle) is SessionPhase.PARKED

        after = manager.get_task(task.id)
        assert after is not None
        assert (after.ball, after.ball_reason) == (Ball.HUMAN, BallReason.INPUT)
        prompt = after.ball_prompt or ""
        assert "parked on a permission prompt" in prompt
        assert "poetry run alembic upgrade head" in prompt
        assert handle.directory.read_meta()["parked_on"] == "permission"


# ----- a3: no ball prompt carries a capture ------------------------------------------


def repainted_capture(repaints: int = 15, width: int = 200, rows: int = 48) -> str:
    """A capture shaped like ``claude logs`` output on 2026-09-13.

    Measured on run_5f6044f0: every frame starts at cursor home (``ESC[H``), rows are
    padded to the terminal width and ended with ``ESC[K`` or ``ESC[<n>X`` instead of a
    newline, a background-coloured side pane pads each row, and there is not one newline
    in the whole capture. The owner's conversation cannot be committed to a public
    repository, so this reproduces the shape with invented text; the size of a dozen
    repaints is what task-414's ball prompt carried.
    """
    body = [f"line {n:02d} of the conversation, about what happened" for n in range(rows - 8)]
    prompt = [
        "Bash command",
        "   poetry run alembic upgrade head",
        " Do you want to proceed?",
        " ❯ 1. Yes",
        "   2. No",
        " Esc to cancel · Tab to amend",
    ]
    frame_rows = body + prompt
    frame = ESC + "[m" + ESC + "[H"
    for number, row in enumerate(frame_rows):
        pad = " " * max(0, width - 90 - len(row))
        side = ESC + "[48;2;38;38;38m" + " " * 90 + ESC + "[m"
        end = ESC + "[K" if number % 2 else ESC + f"[{len(pad) or 1}X"
        frame += ESC + "[38;2;153;153;153m" + row + ESC + "[m" + pad + end + side
    return frame * repaints


class TestTerminalTextIsBounded:
    def test_the_capture_is_the_size_the_incident_quoted_and_has_no_newlines(self) -> None:
        capture = repainted_capture()
        assert 150_000 < len(capture.encode("utf-8")) < 200_000
        assert "\n" not in capture

    def test_an_excerpt_is_a_few_kb_with_each_line_once(self) -> None:
        excerpt = terminal_excerpt(repainted_capture())

        assert len(excerpt.encode("utf-8")) <= TERMINAL_EXCERPT_MAX_BYTES
        assert excerpt.count("Do you want to proceed?") == 1
        assert "poetry run alembic upgrade head" in excerpt
        assert ESC not in excerpt
        assert excerpt.endswith("Esc to cancel · Tab to amend")

    def test_one_enormous_row_is_cut_to_the_cap(self) -> None:
        excerpt = terminal_excerpt("x" * 100_000 + "\nthe end")
        assert len(excerpt.encode("utf-8")) <= TERMINAL_EXCERPT_MAX_BYTES
        assert excerpt.endswith("the end")

    def test_a_park_on_a_repainted_capture_quotes_a_few_kb(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path  # noqa: F811
    ) -> None:
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        (fake_cli.parent / "logs.extra").write_text(repainted_capture(), encoding="utf-8")
        parked_ledger(fake_cli)

        runner.poll_session(handle)

        after = manager.get_task(task.id)
        assert after is not None and after.ball is Ball.HUMAN
        prompt = after.ball_prompt or ""
        assert len(prompt.encode("utf-8")) < TERMINAL_EXCERPT_MAX_BYTES + 1024
        assert prompt.count("Do you want to proceed?") == 1

    def test_a_stall_report_on_a_repainted_capture_quotes_a_few_kb(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path  # noqa: F811
    ) -> None:
        clock = Clock()
        runner = build(workspace, manager, session_resolution(fake_cli, stall=60), clock=clock)
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        (fake_cli.parent / "logs.extra").write_text(repainted_capture(), encoding="utf-8")
        working_ledger(fake_cli)
        runner.poll_session(handle)
        clock.now = clock.now + timedelta(seconds=120)

        runner.poll_session(handle)

        after = manager.get_task(task.id)
        assert after is not None and after.ball is Ball.HUMAN
        assert len((after.ball_prompt or "").encode("utf-8")) < TERMINAL_EXCERPT_MAX_BYTES + 1024


# ----- a4: a session that carries on is no longer parked ------------------------------


class TestAResumedSessionIsNoLongerParked:
    def test_resuming_returns_the_meta_to_running_and_the_ball_to_where_it_was(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path  # noqa: F811
    ) -> None:
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        before = manager.get_task(task.id)
        assert before is not None
        parked_ledger(fake_cli)
        runner.poll_session(handle)
        assert handle.directory.read_meta()["status"] == "parked"

        working_ledger(fake_cli)
        runner.poll_session(handle)

        meta = handle.directory.read_meta()
        assert meta["status"] == "running"
        assert meta.get("parked_on") is None
        after = manager.get_task(task.id)
        assert after is not None
        assert (after.ball, after.ball_reason, after.ball_prompt) == (
            before.ball,
            before.ball_reason,
            before.ball_prompt,
        )

    def test_a_second_park_on_the_same_run_is_surfaced_again(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path  # noqa: F811
    ) -> None:
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        parked_ledger(fake_cli)
        runner.poll_session(handle)
        working_ledger(fake_cli)
        runner.poll_session(handle)

        parked_ledger(fake_cli)
        runner.poll_session(handle)

        after = manager.get_task(task.id)
        assert after is not None
        assert (after.ball, after.ball_reason) == (Ball.HUMAN, BallReason.INPUT)
        parks = [
            e
            for e in after.log
            if e.type is LogEntryType.HANDOFF and (e.data or {}).get("park", {}).get("kind")
        ]
        assert len(parks) == 2

    def test_a_later_word_on_the_task_is_not_overwritten_by_the_resume(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path  # noqa: F811
    ) -> None:
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        parked_ledger(fake_cli)
        runner.poll_session(handle)
        manager.handoff(
            task.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Please review the branch.",
        )

        working_ledger(fake_cli)
        runner.poll_session(handle)

        after = manager.get_task(task.id)
        assert after is not None
        assert (after.ball, after.ball_reason) == (Ball.HUMAN, BallReason.REVIEW)
        assert handle.directory.read_meta()["status"] == "running"
