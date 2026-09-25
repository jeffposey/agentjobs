"""The agent's loop from a shell: claim, log, handoff, release, close, inbox (task-053).

Each verb is exercised through the command a runner without MCP would type, against an
unregistered project -- the state in which the CLI answers for itself, so no server is
needed. Every refusal is asserted twice: the exit code, and a log that did not grow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml
from typer.testing import CliRunner

from agentjobs.cli import app

runner = CliRunner()


def _init_project(root: Path, *, actors: Optional[List[Dict[str, str]]] = None) -> None:
    """A configured, unregistered project -- see ``test_cli._init_project`` for why."""
    config: Dict[str, Any] = {
        "project_name": "Test Project",
        "tasks_directory": "tasks",
        "prompts_directory": "prompts",
        "port": 9000,
        # Present on purpose: the new verbs must not fall back to it.
        "default_user": "jeff",
    }
    if actors is not None:
        config["actors"] = actors
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _task(task_id: str) -> Dict[str, Any]:
    shown = runner.invoke(app, ["show", task_id], catch_exceptions=False)
    assert shown.exit_code == 0, shown.output
    loaded = json.loads(shown.stdout)
    assert isinstance(loaded, dict)
    return loaded


def _ready_task(title: str = "Some work") -> str:
    result = runner.invoke(app, ["create", "--title", title, "--ready"], input="\n")
    assert result.exit_code == 0, result.output
    return next(word for word in result.stdout.split() if word.startswith("task-"))


def _ok(args: List[str]) -> str:
    result = runner.invoke(app, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return str(result.stdout)


def _refused(task_id: str, args: List[str]) -> str:
    """Run a verb that must be refused; assert it exited non-zero and wrote nothing."""
    before = _task(task_id)
    result = runner.invoke(app, args, catch_exceptions=False)
    assert result.exit_code != 0, result.output
    assert "Traceback" not in result.output
    after = _task(task_id)
    assert len(after["log"]) == len(before["log"])
    assert (after["lifecycle"], after["ball"], after["ball_reason"]) == (
        before["lifecycle"],
        before["ball"],
        before["ball_reason"],
    )
    return str(result.output)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    _init_project(tmp_path)
    return tmp_path


def _claimed(actor: str = "codex") -> str:
    task_id = _ready_task()
    _ok(["claim", task_id, "--actor", actor])
    return task_id


# ----- claim -------------------------------------------------------------------------


def test_claim_takes_a_ready_task_as_the_named_actor(project: Path) -> None:
    task_id = _ready_task()

    out = _ok(["claim", task_id, "--actor", "codex"])

    assert "Claimed" in out
    task = _task(task_id)
    assert (task["lifecycle"], task["ball"], task["ball_reason"]) == ("active", "agent", "work")
    assert task["assignment"]["owner"] == "codex"
    assert task["log"][-1]["actor"] == "codex"


def test_claim_refuses_a_task_someone_else_holds(project: Path) -> None:
    task_id = _claimed("codex")

    _refused(task_id, ["claim", task_id, "--actor", "claude"])


# ----- handoff -----------------------------------------------------------------------


def test_handoff_moves_the_ball_with_its_prompt(project: Path) -> None:
    task_id = _claimed()

    _ok(
        [
            "handoff",
            task_id,
            "--actor",
            "codex",
            "--ball",
            "human",
            "--reason",
            "decision",
            "--prompt",
            "Pick A or B.",
        ]
    )

    task = _task(task_id)
    assert (task["ball"], task["ball_reason"], task["ball_prompt"]) == (
        "human",
        "decision",
        "Pick A or B.",
    )
    entry = task["log"][-1]
    assert (entry["type"], entry["actor"]) == ("handoff", "codex")


def test_handoff_without_a_prompt_is_refused(project: Path) -> None:
    task_id = _claimed()

    out = _refused(
        task_id,
        ["handoff", task_id, "--actor", "codex", "--ball", "human", "--reason", "decision"],
    )

    # The schema's sentence, not pydantic's dump of the whole record.
    assert "ball_prompt is required" in out
    assert "input_value" not in out


def test_handoff_to_a_reason_the_holder_cannot_have_is_refused(project: Path) -> None:
    task_id = _claimed()

    _refused(
        task_id,
        [
            "handoff",
            task_id,
            "--actor",
            "codex",
            "--ball",
            "human",
            "--reason",
            "work",
            "--prompt",
            "Do it.",
        ],
    )


def test_handoff_off_a_human_review_is_refused(project: Path) -> None:
    """Approval goes through the approve route, which records the receipt and starts
    the finish; a command-line handoff would skip both."""
    task_id = _claimed()
    review = ["--ball", "human", "--reason", "review", "--prompt", "Review the branch."]
    _ok(["handoff", task_id, "--actor", "codex", *review])

    out = _refused(
        task_id,
        [
            "handoff",
            task_id,
            "--actor",
            "codex",
            "--ball",
            "agent",
            "--reason",
            "work",
            "--prompt",
            "Approved, merge it.",
        ],
    )

    assert "review" in out
    assert _task(task_id)["ball_reason"] == "review"


# ----- log ---------------------------------------------------------------------------


def test_log_appends_an_entry_of_the_named_type(project: Path) -> None:
    task_id = _claimed()

    _ok(["log", task_id, "--actor", "codex", "--type", "question", "Which port?"])
    asked = _task(task_id)["log"][-1]
    _ok(["log", task_id, "--actor", "codex", "--type", "answer", "--re", str(asked["id"]), "8876"])

    log = _task(task_id)["log"]
    assert (asked["type"], asked["actor"], asked["body"]) == ("question", "codex", "Which port?")
    answer = log[-1]
    assert (answer["type"], answer["re"], answer["body"]) == ("answer", asked["id"], "8876")


def test_log_refuses_a_manager_written_type(project: Path) -> None:
    task_id = _claimed()

    _refused(task_id, ["log", task_id, "--actor", "codex", "--type", "transition", "Claimed."])


def test_log_refuses_an_empty_body(project: Path) -> None:
    task_id = _claimed()

    _refused(task_id, ["log", task_id, "--actor", "codex", "--type", "progress", "  "])


# ----- release -----------------------------------------------------------------------


def test_release_returns_a_claimed_task_to_the_pool(project: Path) -> None:
    task_id = _claimed()

    _ok(["release", task_id, "--actor", "codex", "--note", "Out of time."])

    task = _task(task_id)
    assert (task["lifecycle"], task["ball"], task["ball_reason"]) == ("ready", "agent", "available")
    assert task["log"][-1]["actor"] == "codex"


def test_release_refuses_a_task_nobody_claimed(project: Path) -> None:
    task_id = _ready_task()

    _refused(task_id, ["release", task_id, "--actor", "codex"])


# ----- close -------------------------------------------------------------------------


def test_close_ends_the_task_with_its_outcome(project: Path) -> None:
    task_id = _claimed()

    _ok(["close", task_id, "--actor", "codex", "--outcome", "completed"])

    task = _task(task_id)
    assert (task["lifecycle"], task["outcome"], task["ball"]) == ("closed", "completed", None)


def test_close_without_an_outcome_is_refused(project: Path) -> None:
    task_id = _claimed()

    _refused(task_id, ["close", task_id, "--actor", "codex"])


def test_close_refuses_an_already_closed_task(project: Path) -> None:
    task_id = _claimed()
    _ok(["close", task_id, "--actor", "codex", "--outcome", "completed"])

    _refused(task_id, ["close", task_id, "--actor", "codex", "--outcome", "cancelled"])


# ----- --actor is required, with no default_user fallback ------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["claim"],
        ["handoff", "--ball", "human", "--reason", "decision", "--prompt", "Pick."],
        ["log", "--type", "progress", "Did a thing."],
        ["release"],
        ["close", "--outcome", "completed"],
    ],
    ids=["claim", "handoff", "log", "release", "close"],
)
def test_a_mutating_verb_without_an_actor_writes_nothing(project: Path, args: List[str]) -> None:
    """default_user is configured, and still not used: these verbs are an agent's."""
    task_id = _claimed() if args[0] != "claim" else _ready_task()

    result = runner.invoke(app, [args[0], task_id, *args[1:]])
    _refused(task_id, [args[0], task_id, *args[1:]])

    # 2 is Click's usage error: the option is missing, so the verb never ran. The
    # message itself is boxed and coloured by rich, so its text is not asserted.
    assert result.exit_code == 2, result.output


def test_an_actor_the_project_does_not_know_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _init_project(
        tmp_path, actors=[{"id": "jeff", "kind": "human"}, {"id": "codex", "kind": "agent"}]
    )
    task_id = _ready_task()

    out = _refused(task_id, ["claim", task_id, "--actor", "gpt-5"])

    assert "not an actor in this project" in out


def test_a_missing_task_is_reported_not_raised(project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "handoff",
            "task-nope",
            "--actor",
            "codex",
            "--ball",
            "human",
            "--reason",
            "decision",
            "--prompt",
            "Pick.",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "task-nope" in result.stdout
    assert "Traceback" not in result.output


# ----- inbox -------------------------------------------------------------------------


def test_inbox_lists_every_open_task_waiting_on_a_human(project: Path) -> None:
    decide = _claimed()
    _ok(
        [
            "handoff",
            decide,
            "--actor",
            "codex",
            "--ball",
            "human",
            "--reason",
            "decision",
            "--prompt",
            "Pick A or B.",
        ]
    )
    review = _claimed()
    _ok(
        [
            "handoff",
            review,
            "--actor",
            "codex",
            "--ball",
            "human",
            "--reason",
            "review",
            "--prompt",
            "Review the branch.\nThe diff is small.",
        ]
    )
    working = _claimed()
    closed = _claimed()
    _ok(
        [
            "handoff",
            closed,
            "--actor",
            "codex",
            "--ball",
            "human",
            "--reason",
            "decision",
            "--prompt",
            "Stale ask.",
        ]
    )
    _ok(["close", closed, "--actor", "codex", "--outcome", "cancelled"])

    out = _ok(["inbox"])

    assert decide in out and review in out
    assert working not in out and closed not in out
    assert "[decision]" in out and "Pick A or B." in out
    assert "[review]" in out and "Review the branch." in out and "The diff is small." in out
    assert "Stale ask." not in out


def test_inbox_says_so_when_nothing_waits(project: Path) -> None:
    _claimed()

    assert "Nothing is waiting on a human." in _ok(["inbox"])
