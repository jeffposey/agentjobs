"""Structured questions and the answers they get back (task-017).

The feature exists because a handoff at `human`/`decision` could only leave prose in
`ball_prompt` and hope for prose back, and the person reading it is on a phone. So the
assertions here are about what the record ends up holding -- typed options on the
question, `answer` entries threaded by `re`, the ball moving once -- and not about any
markup, which is the frontend's test to write.

The worked example at the bottom is task-077's real handoff: four questions, one
answered with a number nobody offered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.main import app


@pytest.fixture(autouse=True)
def setup_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A project with one human and one agent, in a temporary directory."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    agentjobs_dir = tmp_path / ".agentjobs"
    agentjobs_dir.mkdir()
    (agentjobs_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": "Test",
                "tasks_directory": "tasks",
                "actors": [
                    {"name": "jeff", "kind": "human", "display_name": "Jeff"},
                    {"name": "test-agent", "kind": "agent"},
                ],
                "default_user": "jeff",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTJOBS_TASKS_DIR", str(tasks_dir))
    reset_dependency_cache()
    yield
    reset_dependency_cache()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def claimed(client: TestClient) -> str:
    """An active task an agent holds, ready to hand off."""
    response = client.post(
        "/api/tasks",
        json={
            "title": "Sample Task",
            "description": "Test task",
            "category": "test",
            "lifecycle": "ready",
        },
    )
    assert response.status_code == 201
    task_id = str(response.json()["id"])
    assert client.post(f"/api/tasks/{task_id}/claim", json={"agent": "test-agent"}).status_code == 200
    return task_id


def ask(
    client: TestClient,
    task_id: str,
    questions: List[Dict[str, Any]],
    *,
    prompt: str = "Four decisions before I can carry on.",
) -> Dict[str, Any]:
    """Hand off to the human with structured questions, the way an agent does."""
    response = client.post(
        f"/api/tasks/{task_id}/handoff",
        json={
            "actor": "test-agent",
            "ball": "human",
            "ball_reason": "decision",
            "ball_prompt": prompt,
            "questions": questions,
        },
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def entries(task: Dict[str, Any], entry_type: str) -> List[Dict[str, Any]]:
    return [entry for entry in task["log"] if entry["type"] == entry_type]


POSTURE = {
    "body": "Which permission posture should a dispatched run get by default?",
    "options": [
        {
            "label": "supervised",
            "description": "Allow-listed commands; anything else parks for a human.",
            "recommended": True,
        },
        {"label": "auto", "description": "Classifier-gated, and stops at the merge gate."},
        {"label": "read_only", "description": "No shell at all."},
    ],
}


# --------------------------------------------------------------------------------------
# Asking
# --------------------------------------------------------------------------------------


def test_a_handoff_can_carry_questions_and_they_land_on_the_record(
    client: TestClient, claimed: str
) -> None:
    """sc-2: an agent poses questions with options and a recommendation."""
    task = ask(client, claimed, [POSTURE])

    asked = entries(task, "question")
    assert len(asked) == 1
    assert asked[0]["body"] == POSTURE["body"]
    assert [option["label"] for option in asked[0]["data"]["options"]] == [
        "supervised",
        "auto",
        "read_only",
    ]
    assert asked[0]["data"]["options"][0]["recommended"] is True
    # Not recommended by omission, not by absence of the key: a reader of the YAML
    # should not have to know the default.
    assert asked[0]["data"]["options"][1]["recommended"] is False
    assert asked[0]["data"]["multi_select"] is False


def test_the_prompt_stays_prose_beside_the_questions(client: TestClient, claimed: str) -> None:
    """A constraint of the task: this adds structure beside ball_prompt, not instead."""
    task = ask(client, claimed, [POSTURE], prompt="One decision before I can carry on.")
    assert task["ball_prompt"] == "One decision before I can carry on."
    assert task["ball"] == "human"
    assert task["ball_reason"] == "decision"


def test_questions_thread_to_the_handoff_that_raised_them(
    client: TestClient, claimed: str
) -> None:
    """So a reader can tell which ask a question belongs to without guessing by time."""
    task = ask(client, claimed, [POSTURE, {"body": "And the ceiling?"}])
    handoff = entries(task, "handoff")[-1]
    assert [entry["re"] for entry in entries(task, "question")] == [handoff["id"], handoff["id"]]


def test_all_the_questions_arrive_together_or_none_do(client: TestClient, claimed: str) -> None:
    """The reason questions ride on the handoff rather than on N log calls.

    A human woken by the handoff must not be able to open a form holding two of four.
    """
    task = ask(client, claimed, [POSTURE, {"body": "Second"}, {"body": "Third"}])
    ids = [entry["id"] for entry in entries(task, "question")]
    handoff_id = entries(task, "handoff")[-1]["id"]
    # Contiguous, immediately after the handoff: one mutation wrote all four entries.
    assert ids == [handoff_id + 1, handoff_id + 2, handoff_id + 3]


def test_a_question_with_no_options_is_a_plain_question(client: TestClient, claimed: str) -> None:
    """Every field defaults, so the prose-only question this repository already has works."""
    task = ask(client, claimed, [{"body": "What is the threshold?", "placeholder": "minutes"}])
    asked = entries(task, "question")[-1]
    assert asked["data"]["options"] == []
    assert asked["data"]["placeholder"] == "minutes"


def test_a_malformed_option_is_refused_at_the_write(client: TestClient, claimed: str) -> None:
    """The whole argument for typing this rather than leaving it in free-form `data`.

    An option list that cannot be rendered must fail while the agent that wrote it is
    still around to be told, not in front of the person trying to answer it.
    """
    response = client.post(
        f"/api/tasks/{claimed}/handoff",
        json={
            "actor": "test-agent",
            "ball": "human",
            "ball_reason": "decision",
            "ball_prompt": "Pick one.",
            "questions": [{"body": "Which?", "options": [{"labl": "typo in the key"}]}],
        },
    )
    # 400 rather than 422: this app renders a validation failure as a bad request.
    assert response.status_code == 400


def test_a_question_posted_on_its_own_still_gets_options(client: TestClient, claimed: str) -> None:
    """`task_log_append`'s path: a question raised mid-work, without moving the ball."""
    response = client.post(
        f"/api/tasks/{claimed}/log",
        json={
            "actor": "test-agent",
            "type": "question",
            "body": POSTURE["body"],
            "data": {"options": POSTURE["options"], "multi_select": True},
        },
    )
    assert response.status_code == 200, response.text
    asked = entries(response.json(), "question")[-1]
    assert asked["data"]["multi_select"] is True
    assert len(asked["data"]["options"]) == 3


# --------------------------------------------------------------------------------------
# Answering
# --------------------------------------------------------------------------------------


def answer(client: TestClient, task_id: str, **payload: Any) -> Any:
    return client.post(f"/api/tasks/{task_id}/answer", json={"user": "jeff", **payload})


def test_answering_writes_an_answer_entry_threaded_to_its_question(
    client: TestClient, claimed: str
) -> None:
    """sc-3, the half the record has to prove."""
    asked = entries(ask(client, claimed, [POSTURE]), "question")[-1]

    response = answer(client, claimed, answers=[{"re": asked["id"], "selected": ["supervised"]}])
    assert response.status_code == 200, response.text
    task = response.json()["task"]

    given = entries(task, "answer")
    assert len(given) == 1
    assert given[0]["re"] == asked["id"]
    assert given[0]["data"]["selected"] == ["supervised"]
    assert given[0]["actor"] == "jeff"
    assert given[0]["body"] == "Chose: supervised"


def test_an_answered_question_stops_being_open(client: TestClient, claimed: str) -> None:
    asked = entries(ask(client, claimed, [POSTURE, {"body": "Second"}]), "question")
    answer(client, claimed, answers=[{"re": asked[0]["id"], "selected": ["auto"]}])

    task = client.get(f"/api/tasks/{claimed}").json()
    answered = {entry["re"] for entry in entries(task, "answer")}
    still_open = [entry["id"] for entry in entries(task, "question") if entry["id"] not in answered]
    assert still_open == [asked[1]["id"]]


def test_answering_moves_the_ball_once_for_the_whole_form(
    client: TestClient, claimed: str
) -> None:
    """sc-6, as decided: one Submit, one handoff. Not one per question."""
    asked = entries(ask(client, claimed, [POSTURE, {"body": "Second"}]), "question")
    before = len(entries(client.get(f"/api/tasks/{claimed}").json(), "handoff"))

    task = answer(
        client,
        claimed,
        answers=[
            {"re": asked[0]["id"], "selected": ["auto"]},
            {"re": asked[1]["id"], "other": "later"},
        ],
    ).json()["task"]

    assert len(entries(task, "handoff")) == before + 1
    assert task["ball"] == "agent"
    assert task["ball_reason"] == "answer"
    assert len(entries(task, "answer")) == 2


def test_tapping_options_and_typing_nothing_still_produces_a_real_ask(
    client: TestClient, claimed: str
) -> None:
    """The schema requires a ball_prompt, and the point of the feature is not typing one.

    Composed rather than left blank, so the agent resuming learns what it asked and what
    it was told without walking the log.
    """
    asked = entries(ask(client, claimed, [POSTURE]), "question")[-1]
    task = answer(client, claimed, answers=[{"re": asked["id"], "selected": ["auto"]}]).json()["task"]

    assert POSTURE["body"] in task["ball_prompt"]
    assert "Chose: auto" in task["ball_prompt"]


def test_free_text_survives_beside_the_options(client: TestClient, claimed: str) -> None:
    """The constraint the task is most insistent about, at the per-question level."""
    asked = entries(ask(client, claimed, [POSTURE]), "question")[-1]
    task = answer(
        client,
        claimed,
        answers=[
            {"re": asked["id"], "selected": ["supervised"], "other": "but only on weekdays"}
        ],
    ).json()["task"]

    given = entries(task, "answer")[-1]
    assert given["data"]["selected"] == ["supervised"]
    assert given["data"]["other"] == "but only on weekdays"
    assert "Chose: supervised" in given["body"]
    assert "but only on weekdays" in given["body"]


def test_free_text_alone_answers_a_question_with_options(
    client: TestClient, claimed: str
) -> None:
    """Rejecting every option offered is a first-class answer, not an error."""
    asked = entries(ask(client, claimed, [POSTURE]), "question")[-1]
    task = answer(
        client, claimed, answers=[{"re": asked["id"], "other": "None of these. Use `autonomous`."}]
    ).json()["task"]

    given = entries(task, "answer")[-1]
    assert given["data"]["selected"] == []
    assert given["body"] == "None of these. Use `autonomous`."


def test_prose_with_no_questions_behaves_exactly_as_before(
    client: TestClient, claimed: str
) -> None:
    """task-231's route keeps working unchanged for a task that has no questions on it."""
    client.post(
        f"/api/tasks/{claimed}/handoff",
        json={
            "actor": "test-agent",
            "ball": "human",
            "ball_reason": "decision",
            "ball_prompt": "What now?",
        },
    )
    prose = "Option 2, and skip the third question entirely."
    task = answer(client, claimed, feedback=prose).json()["task"]

    assert task["ball_prompt"] == prose
    assert entries(task, "answer") == []


def test_prose_is_carried_alongside_the_answers(client: TestClient, claimed: str) -> None:
    asked = entries(ask(client, claimed, [POSTURE]), "question")[-1]
    task = answer(
        client,
        claimed,
        answers=[{"re": asked["id"], "selected": ["auto"]}],
        feedback="Do not start this until task-016 merges.",
    ).json()["task"]

    assert "Chose: auto" in task["ball_prompt"]
    assert "Do not start this until task-016 merges." in task["ball_prompt"]


def test_multi_select_accepts_more_than_one_option(client: TestClient, claimed: str) -> None:
    question = dict(POSTURE, multi_select=True)
    asked = entries(ask(client, claimed, [question]), "question")[-1]
    task = answer(
        client, claimed, answers=[{"re": asked["id"], "selected": ["supervised", "auto"]}]
    ).json()["task"]

    assert entries(task, "answer")[-1]["data"]["selected"] == ["supervised", "auto"]
    assert entries(task, "answer")[-1]["body"] == "Chose: supervised, auto"


# --------------------------------------------------------------------------------------
# What the record refuses
# --------------------------------------------------------------------------------------


def test_an_option_the_question_never_offered_is_refused(
    client: TestClient, claimed: str
) -> None:
    """A stale form or a typo. Either way the human's tap did not mean what this says."""
    asked = entries(ask(client, claimed, [POSTURE]), "question")[-1]
    response = answer(client, claimed, answers=[{"re": asked["id"], "selected": ["autonomous"]}])

    assert response.status_code == 409
    assert "autonomous" in response.json()["detail"]


def test_answering_the_same_question_twice_is_refused(client: TestClient, claimed: str) -> None:
    """Otherwise `open_questions()` is quietly wrong for the rest of the task's life."""
    asked = entries(ask(client, claimed, [POSTURE]), "question")[-1]
    assert answer(client, claimed, answers=[{"re": asked["id"], "selected": ["auto"]}]).status_code == 200

    again = answer(client, claimed, answers=[{"re": asked["id"], "selected": ["supervised"]}])
    assert again.status_code == 409
    assert "already been answered" in again.json()["detail"]


def test_answering_something_that_is_not_a_question_is_refused(
    client: TestClient, claimed: str
) -> None:
    task = ask(client, claimed, [POSTURE])
    handoff_id = entries(task, "handoff")[-1]["id"]
    response = answer(client, claimed, answers=[{"re": handoff_id, "other": "yes"}])

    assert response.status_code == 409
    assert "not a question" in response.json()["detail"]


def test_a_second_option_on_a_single_choice_question_is_refused(
    client: TestClient, claimed: str
) -> None:
    asked = entries(ask(client, claimed, [POSTURE]), "question")[-1]
    response = answer(
        client, claimed, answers=[{"re": asked["id"], "selected": ["auto", "read_only"]}]
    )
    assert response.status_code == 409


def test_an_empty_answer_is_refused(client: TestClient, claimed: str) -> None:
    """Closing a thread while recording nothing is worse than leaving it open."""
    asked = entries(ask(client, claimed, [POSTURE]), "question")[-1]
    response = answer(client, claimed, answers=[{"re": asked["id"], "other": "   "}])
    assert response.status_code == 409


def test_a_submission_with_neither_answers_nor_prose_is_refused(
    client: TestClient, claimed: str
) -> None:
    ask(client, claimed, [POSTURE])
    assert answer(client, claimed).status_code == 400


def test_one_bad_answer_writes_none_of_them(client: TestClient, claimed: str) -> None:
    """Atomic in the failing direction too, which is the direction that matters."""
    asked = entries(ask(client, claimed, [POSTURE, {"body": "Second"}]), "question")
    response = answer(
        client,
        claimed,
        answers=[
            {"re": asked[0]["id"], "selected": ["auto"]},
            {"re": asked[1]["id"], "selected": ["an option that was never offered"]},
        ],
    )
    assert response.status_code == 409

    task = client.get(f"/api/tasks/{claimed}").json()
    assert entries(task, "answer") == []
    assert task["ball"] == "human"


# --------------------------------------------------------------------------------------
# sc-4: the handoff that produced the request, reproduced end to end
# --------------------------------------------------------------------------------------

#: task-077's real handoff, 2026-08-18: four questions Jeff answered in seconds when they
#: were put to him as choices, and would have had to write four paragraphs to answer here.
#: Q3 is the one that matters most -- it wanted a number, and he took none of the options.
TASK_077_QUESTIONS: List[Dict[str, Any]] = [
    {
        "body": "Does dispatch drive `claude agents` in session mode, or supervise its own `-p` processes?",
        "options": [
            {
                "label": "Session mode primary, batch retained",
                "description": "`--bg --remote-control`, with `-p` kept as a declared runner mode.",
                "recommended": True,
            },
            {
                "label": "Session-only, delete batch",
                "description": "One code path; loses the spend ceiling and structured output.",
            },
            {
                "label": "Batch-only",
                "description": "Keeps the spend ceiling; no mid-flight redirect.",
            },
        ],
    },
    {
        "body": "Should runs still be killed when their supervisor restarts?",
        "options": [
            {
                "label": "No -- re-attach to whatever is in the ledger",
                "description": "`claude stop` is an independent kill switch, so the orphan premise is gone.",
                "recommended": True,
            },
            {
                "label": "Keep the rule",
                "description": "Consistent with the original design; costs a real run per redeploy.",
            },
        ],
    },
    {
        "body": "How long may a session sit idle with an unmoved ball before it counts as stalled?",
        "placeholder": "a number of minutes",
        "options": [
            {"label": "15 minutes", "description": "More false positives if an agent pauses."},
            {"label": "4 hours", "description": "Only catches overnight stalls."},
            {"label": "Keep the 1800s hard kill", "description": "Kills the session outright."},
        ],
    },
    {
        "body": "What happens to task-070 and task-072?",
        "multi_select": True,
        "options": [
            {"label": "Rescope task-070 to batch mode", "recommended": True},
            {"label": "Rescope task-072 to our own ledger", "recommended": True},
            {"label": "Close task-072 as superseded"},
            {"label": "Close both and write one new wrapper task"},
        ],
    },
]


def test_the_task_077_handoff_is_answerable_end_to_end(client: TestClient, claimed: str) -> None:
    """sc-4. Four questions, one numeric, one where every option was rejected.

    The answers are the ones Jeff actually gave on 2026-08-18, entry 8 of task-077.
    """
    task = ask(client, claimed, TASK_077_QUESTIONS, prompt="sc-1 verified; four decisions needed.")
    asked = entries(task, "question")
    assert len(asked) == 4

    response = answer(
        client,
        claimed,
        answers=[
            {"re": asked[0]["id"], "selected": ["Session mode primary, batch retained"]},
            {"re": asked[1]["id"], "selected": ["No -- re-attach to whatever is in the ledger"]},
            # None of the three offered. This is the case that made free text a constraint
            # rather than a nicety, and it is the numeric one.
            {"re": asked[2]["id"], "other": "60 minutes"},
            {
                "re": asked[3]["id"],
                "selected": [
                    "Rescope task-070 to batch mode",
                    "Rescope task-072 to our own ledger",
                ],
            },
        ],
        feedback="Q2 from entry 5 is still evidenced but not end-to-end tested.",
    )
    assert response.status_code == 200, response.text
    task = response.json()["task"]

    given = entries(task, "answer")
    assert [entry["re"] for entry in given] == [entry["id"] for entry in asked]
    assert given[2]["data"]["selected"] == []
    assert given[2]["data"]["other"] == "60 minutes"
    assert given[3]["data"]["selected"] == [
        "Rescope task-070 to batch mode",
        "Rescope task-072 to our own ledger",
    ]

    # One handoff for the lot, and the prompt an agent resumes on carries every answer.
    assert task["ball"] == "agent"
    assert task["ball_reason"] == "answer"
    assert "60 minutes" in task["ball_prompt"]
    assert "Session mode primary" in task["ball_prompt"]
    assert "still evidenced but not end-to-end tested" in task["ball_prompt"]

    # Nothing is left open: four asked, four answered.
    answered = {entry["re"] for entry in given}
    assert all(entry["id"] in answered for entry in asked)
