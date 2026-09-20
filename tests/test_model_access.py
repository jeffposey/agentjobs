"""The model-access package: configuration, the credential, the cap, and the parse.

Every test here runs on a machine with no model configured, which is not a limitation
of the suite -- it is the state ``docs/model-access-design.md`` §6 says must work first,
and the state this repository's own machine was in when the feature was written. The
configured path is exercised by substituting the opener, so the header assembly, the
status mapping and the response extraction are all under test without a credential and
without a network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest
import urllib.error

from agentjobs.modelaccess import budget, client, config, draft


# ----- fakes ------------------------------------------------------------------


class _Reply:
    """A stand-in for the object ``urlopen`` returns: a context manager with ``read``."""

    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Reply":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _opener(payload: Any, captured: Optional[list] = None):
    def send(request, timeout=None):  # noqa: ANN001 - mirrors urlopen's signature
        if captured is not None:
            captured.append(request)
        return _Reply(payload)

    return send


def _raiser(exc: BaseException):
    def send(request, timeout=None):  # noqa: ANN001
        raise exc

    return send


def _text(body: str) -> dict:
    return {"content": [{"type": "text", "text": body}]}


DRAFT_JSON = {
    "summary": "The task list pages badly once a project has a few hundred tasks.",
    "intent": "Filing more work should not make the backlog harder to read.",
    "description": "## What to do\n\nPage the listing.",
    "constraints": "No schema change.",
    "out_of_scope": "Search.",
    "acceptance": ["A project with 500 tasks renders its first page in under a second."],
}


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """A private AgentJobs home, so no test reads or writes the operator's own."""
    return tmp_path


def _write_config(home: Path, **overrides: Any) -> Path:
    payload = {"version": 1, "api_key": "not-a-real-key", **overrides}
    path = home / config.CONFIG_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ----- configuration ----------------------------------------------------------


def test_absent_config_is_not_an_error(home: Path) -> None:
    assert config.load_model_config(home) is None


def test_absent_everything_reads_unconfigured(home: Path, monkeypatch) -> None:
    monkeypatch.delenv(config.ENV_CREDENTIAL, raising=False)
    state = config.availability(home)
    assert state.available is False
    assert state.reason == config.UNCONFIGURED
    assert state.detail and "model.yaml" in state.detail


def test_an_environment_credential_is_enough(home: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_CREDENTIAL, "not-a-real-key")
    state = config.availability(home)
    assert state.available is True
    assert state.model == config.DEFAULT_MODEL


def test_the_file_wins_over_the_environment(home: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_CREDENTIAL, "from-the-environment")
    _write_config(home, api_key="from-the-file")
    assert config.resolve_credential(home) == "from-the-file"


def test_a_file_without_a_key_falls_back_to_the_environment(home: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_CREDENTIAL, "from-the-environment")
    _write_config(home)
    (home / config.CONFIG_FILENAME).write_text(json.dumps({"version": 1}), encoding="utf-8")
    assert config.resolve_credential(home) == "from-the-environment"


def test_the_sentinel_stops_model_calls(home: Path, monkeypatch) -> None:
    """Design §5: the operator's single blunt stop halts model work as well as runs."""
    monkeypatch.setenv(config.ENV_CREDENTIAL, "not-a-real-key")
    _write_config(home)
    (home / "DISPATCH_DISABLED").write_text("", encoding="utf-8")
    state = config.availability(home)
    assert state.available is False
    assert state.reason == config.SENTINEL


def test_the_sentinel_is_reported_before_the_credential_is_looked_for(
    home: Path, monkeypatch
) -> None:
    """A stopped machine must not be told to go and configure a credential."""
    monkeypatch.delenv(config.ENV_CREDENTIAL, raising=False)
    (home / "DISPATCH_DISABLED").write_text("", encoding="utf-8")
    assert config.availability(home).reason == config.SENTINEL


def test_dispatch_being_off_does_not_stop_drafting(home: Path, monkeypatch) -> None:
    """Design §5: the other three dispatch gates are not shared.

    A machine with no ``dispatch.yaml`` at all -- no master switch, no runner, no
    project enabled -- can still draft, because those gates authorise spawning a process
    for repository work and a drafting call is not one.
    """
    monkeypatch.setenv(config.ENV_CREDENTIAL, "not-a-real-key")
    assert not (home / "dispatch.yaml").exists()
    assert config.availability(home).available is True


def test_an_unparseable_file_is_invalid_config_not_unconfigured(home: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_CREDENTIAL, "not-a-real-key")
    (home / config.CONFIG_FILENAME).write_text("version: 1\n  bad: [\n", encoding="utf-8")
    state = config.availability(home)
    assert state.reason == config.INVALID_CONFIG


def test_a_foreign_provider_is_refused_and_names_the_seam(home: Path) -> None:
    _write_config(home, provider="openai")
    with pytest.raises(config.ModelConfigError) as caught:
        config.load_model_config(home)
    assert "base_url" in str(caught.value)


def test_a_bad_cap_is_refused_without_quoting_the_file(home: Path) -> None:
    """The message names the key and the path, never a value read out of the file.

    The file also holds the credential, so a helper that formatted offending values into
    its error would be one edit away from formatting that one.
    """
    _write_config(home, calls_per_hour=0)
    with pytest.raises(config.ModelConfigError) as caught:
        config.load_model_config(home)
    message = str(caught.value)
    assert "calls_per_hour" in message
    assert "not-a-real-key" not in message


def test_availability_never_carries_the_credential(home: Path, monkeypatch) -> None:
    """There is no field on the answer a key could occupy; assert it against the values."""
    monkeypatch.setenv(config.ENV_CREDENTIAL, "sk-a-very-distinctive-value")
    _write_config(home, api_key="sk-a-second-distinctive-value")
    state = config.availability(home)
    rendered = repr(state)
    assert "sk-a-very-distinctive-value" not in rendered
    assert "sk-a-second-distinctive-value" not in rendered


def test_the_config_object_carries_no_credential(home: Path) -> None:
    _write_config(home, api_key="sk-distinctive")
    loaded = config.load_model_config(home)
    assert loaded is not None
    assert "sk-distinctive" not in repr(loaded)
    assert not hasattr(loaded, "api_key")


def test_messages_url_is_built_from_base_url(home: Path) -> None:
    _write_config(home, base_url="http://127.0.0.1:11434/")
    loaded = config.load_model_config(home)
    assert loaded is not None
    assert loaded.messages_url == "http://127.0.0.1:11434/v1/messages"


# ----- the hourly cap ---------------------------------------------------------


def test_the_cap_counts_a_rolling_hour(home: Path) -> None:
    now = datetime.now(timezone.utc)
    budget.record_call(home, now=now - timedelta(minutes=90))
    budget.record_call(home, now=now - timedelta(minutes=30))
    assert budget.calls_in_last_hour(home, now=now) == 1


def test_recording_prunes_what_has_fallen_out_of_the_window(home: Path) -> None:
    now = datetime.now(timezone.utc)
    for minutes in (200, 180, 160):
        budget.record_call(home, now=now - timedelta(minutes=minutes))
    budget.record_call(home, now=now)
    stored = json.loads(budget.ledger_path(home).read_text(encoding="utf-8"))
    assert len(stored) == 1


def test_a_corrupt_counter_does_not_stop_a_draft(home: Path) -> None:
    budget.ledger_path(home).write_text("{not json", encoding="utf-8")
    assert budget.calls_in_last_hour(home) == 0


def test_over_cap_is_reported_by_availability(home: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_CREDENTIAL, "not-a-real-key")
    _write_config(home, calls_per_hour=2)
    budget.record_call(home)
    budget.record_call(home)
    state = config.availability(home)
    assert state.available is False
    assert state.reason == config.OVER_CAP
    assert state.calls_used == 2


def test_a_drafting_call_consumes_no_dispatch_run_slot(home: Path, monkeypatch) -> None:
    """Design §5: the run budgets count runs, and a draft is not one.

    Asserted against the ledger dispatch actually counts from: drafting writes its own
    file and leaves ``runs/`` alone, so a person drafting on their phone cannot exhaust
    the budget that exists to bound a runaway agent.
    """
    monkeypatch.setenv(config.ENV_CREDENTIAL, "not-a-real-key")
    _write_config(home)
    draft.draft_spec("t", "d", "proj", home=home, opener=_opener(_text(json.dumps(DRAFT_JSON))))
    assert budget.calls_in_last_hour(home) == 1
    assert not (home / "runs").exists()


def test_the_call_is_counted_before_it_is_sent(home: Path, monkeypatch) -> None:
    """A provider that refuses every request still runs the counter down.

    Counting the answer rather than the question would let a failing loop run without
    limit, which is the one thing a cap exists to stop.
    """
    monkeypatch.setenv(config.ENV_CREDENTIAL, "not-a-real-key")
    _write_config(home)
    failure = urllib.error.HTTPError("https://example.invalid", 500, "boom", {}, None)
    with pytest.raises(client.ModelCallError):
        draft.draft_spec("t", "d", "proj", home=home, opener=_raiser(failure))
    assert budget.calls_in_last_hour(home) == 1


# ----- the request ------------------------------------------------------------


def test_the_credential_goes_in_the_header_and_nowhere_else(home: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_CREDENTIAL, "sk-distinctive")
    captured: list = []
    loaded = config.ModelConfig()
    client.call_model("hello", loaded, home=home, opener=_opener(_text("{}"), captured))
    request = captured[0]
    assert request.get_header("X-api-key") == "sk-distinctive"
    assert "sk-distinctive" not in request.data.decode("utf-8")


def test_the_request_names_the_configured_model_and_cap(home: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_CREDENTIAL, "not-a-real-key")
    captured: list = []
    loaded = config.ModelConfig(model="a-model-id", max_tokens=99)
    client.call_model("hello", loaded, home=home, opener=_opener(_text("{}"), captured))
    body = json.loads(captured[0].data.decode("utf-8"))
    assert body["model"] == "a-model-id"
    assert body["max_tokens"] == 99


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, config.REFUSED),
        (403, config.REFUSED),
        (429, config.RATE_LIMITED),
        (400, config.UPSTREAM),
        (500, config.UPSTREAM),
        (503, config.UPSTREAM),
    ],
)
def test_statuses_map_to_the_closed_set(
    home: Path, monkeypatch, status: int, expected: str
) -> None:
    monkeypatch.setenv(config.ENV_CREDENTIAL, "not-a-real-key")
    failure = urllib.error.HTTPError("https://example.invalid", status, "no", {}, None)
    with pytest.raises(client.ModelCallError) as caught:
        client.call_model("hi", config.ModelConfig(), home=home, opener=_raiser(failure))
    assert caught.value.reason == expected


def test_a_provider_error_body_never_crosses_the_boundary(home: Path, monkeypatch) -> None:
    """Design §4: an upstream error body can echo request metadata, so none is forwarded.

    The stand-in provider answers with the caller's own credential in its error text,
    which is the exact failure the rule exists for.
    """
    monkeypatch.setenv(config.ENV_CREDENTIAL, "sk-distinctive")
    body = b'{"error":{"message":"bad key sk-distinctive for model x"}}'
    failure = urllib.error.HTTPError("https://example.invalid", 401, body.decode(), {}, None)
    with pytest.raises(client.ModelCallError) as caught:
        client.call_model("hi", config.ModelConfig(), home=home, opener=_raiser(failure))
    assert "sk-distinctive" not in str(caught.value)
    assert str(caught.value) == config.REASON_DETAIL[config.REFUSED]


def test_a_timeout_is_told_apart_from_an_outage(home: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_CREDENTIAL, "not-a-real-key")
    with pytest.raises(client.ModelCallError) as caught:
        client.call_model("hi", config.ModelConfig(), home=home, opener=_raiser(TimeoutError()))
    assert caught.value.reason == config.TIMEOUT

    unreachable = urllib.error.URLError("no route to host")
    with pytest.raises(client.ModelCallError) as second:
        client.call_model("hi", config.ModelConfig(), home=home, opener=_raiser(unreachable))
    assert second.value.reason == config.UPSTREAM


def test_a_wrapped_socket_timeout_is_still_a_timeout(home: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_CREDENTIAL, "not-a-real-key")
    wrapped = urllib.error.URLError(TimeoutError("timed out"))
    with pytest.raises(client.ModelCallError) as caught:
        client.call_model("hi", config.ModelConfig(), home=home, opener=_raiser(wrapped))
    assert caught.value.reason == config.TIMEOUT


def test_an_unexpected_response_shape_is_malformed_not_a_traceback(
    home: Path, monkeypatch
) -> None:
    monkeypatch.setenv(config.ENV_CREDENTIAL, "not-a-real-key")
    with pytest.raises(client.ModelCallError) as caught:
        client.call_model("hi", config.ModelConfig(), home=home, opener=_opener({"oops": True}))
    assert caught.value.reason == config.MALFORMED


def test_calling_with_no_credential_is_unconfigured(home: Path, monkeypatch) -> None:
    monkeypatch.delenv(config.ENV_CREDENTIAL, raising=False)
    with pytest.raises(client.ModelCallError) as caught:
        client.call_model("hi", config.ModelConfig(), home=home, opener=_opener(_text("{}")))
    assert caught.value.reason == config.UNCONFIGURED


# ----- the prompt and the parse -----------------------------------------------


def test_the_prompt_carries_what_the_human_typed_and_the_project(home: Path) -> None:
    rendered = draft.render_prompt("Paging is slow", "it drags at 400 tasks", "agentjobs")
    assert "Paging is slow" in rendered
    assert "it drags at 400 tasks" in rendered
    assert "agentjobs" in rendered


def test_the_prompt_forbids_the_fields_the_human_owns(home: Path) -> None:
    rendered = draft.render_prompt("t", "d", "p")
    assert "Do not set lifecycle, ball, priority, parent, dependencies, tags or actor" in rendered


def test_a_very_long_paste_is_clipped_and_says_so(home: Path) -> None:
    rendered = draft.render_prompt("t", "x" * (draft.MAX_INPUT_CHARS + 500), "p")
    assert "truncated" in rendered
    assert len(rendered) < draft.MAX_INPUT_CHARS + 3000


def test_a_plain_json_reply_parses(home: Path) -> None:
    parsed = draft.parse_draft_reply(json.dumps(DRAFT_JSON))
    assert parsed.summary.startswith("The task list pages badly")
    assert parsed.acceptance == DRAFT_JSON["acceptance"]


def test_a_fenced_reply_parses(home: Path) -> None:
    parsed = draft.parse_draft_reply("```json\n" + json.dumps(DRAFT_JSON) + "\n```")
    assert parsed.intent.startswith("Filing more work")


def test_prose_around_the_object_is_not_guessed_at(home: Path) -> None:
    """The design's objection to option A is a plausible paragraph in the wrong shape.

    Recovering an object from prose wrapped around it would be exactly that guess, so a
    reply like this is a refusal rather than a partial draft.
    """
    with pytest.raises(client.ModelCallError) as caught:
        draft.parse_draft_reply("Sure! Here you go:\n" + json.dumps(DRAFT_JSON) + "\nHope that helps.")
    assert caught.value.reason == config.MALFORMED


def test_a_non_object_reply_is_malformed(home: Path) -> None:
    with pytest.raises(client.ModelCallError):
        draft.parse_draft_reply("[1, 2, 3]")


def test_the_model_cannot_set_state_it_does_not_own(home: Path) -> None:
    """ac-5, enforced structurally: the fields are not read, so they do not exist.

    A model that answers with a priority, a parent, a dependency graph, a lifecycle and
    an actor produces a draft none of that survives into -- not a draft that carries it
    into a form where a person might not notice.
    """
    reply = dict(DRAFT_JSON)
    reply.update(
        {
            "lifecycle": "ready",
            "ball": "agent",
            "priority": "critical",
            "parent": "task-001-invented",
            "dependencies": [{"task": "task-002-invented", "type": "needs"}],
            "actor": "somebody",
            "tags": ["invented"],
        }
    )
    parsed = draft.parse_draft_reply(json.dumps(reply))
    rendered = repr(parsed)
    for forbidden in ("critical", "task-001-invented", "task-002-invented", "somebody", "invented"):
        assert forbidden not in rendered
    assert set(vars(parsed)) == {
        "summary",
        "intent",
        "description",
        "constraints",
        "out_of_scope",
        "acceptance",
    }


def test_a_field_of_the_wrong_type_is_treated_as_absent(home: Path) -> None:
    """A list where a string was asked for must not reach a form as ``['a', 'b']``."""
    parsed = draft.parse_draft_reply(json.dumps({**DRAFT_JSON, "summary": ["a", "b"]}))
    assert parsed.summary == ""


def test_acceptance_keeps_only_entries_that_are_text(home: Path) -> None:
    parsed = draft.parse_draft_reply(
        json.dumps({**DRAFT_JSON, "acceptance": ["real", "", 7, None, "  also real  "]})
    )
    assert parsed.acceptance == ["real", "also real"]


def test_filled_names_the_fields_a_draft_has_content_for(home: Path) -> None:
    parsed = draft.parse_draft_reply(
        json.dumps({"summary": "s", "constraints": "", "acceptance": ["a"]})
    )
    assert parsed.filled() == ["summary", "acceptance"]


# ----- the whole call ---------------------------------------------------------


def test_draft_spec_end_to_end_against_a_substituted_opener(home: Path, monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_CREDENTIAL, "not-a-real-key")
    _write_config(home, model="a-model-id")
    parsed = draft.draft_spec(
        "Paging is slow",
        "it drags at 400 tasks",
        "agentjobs",
        home=home,
        opener=_opener(_text(json.dumps(DRAFT_JSON))),
    )
    assert parsed.summary.startswith("The task list pages badly")
    assert parsed.acceptance


def test_draft_spec_refuses_before_spending_when_unconfigured(home: Path, monkeypatch) -> None:
    monkeypatch.delenv(config.ENV_CREDENTIAL, raising=False)
    with pytest.raises(client.ModelCallError) as caught:
        draft.draft_spec("t", "d", "p", home=home, opener=_opener(_text("{}")))
    assert caught.value.reason == config.UNCONFIGURED
    assert budget.calls_in_last_hour(home) == 0


def test_draft_spec_rechecks_the_sentinel_at_call_time(home: Path, monkeypatch) -> None:
    """A status route is a hint for the interface, never the authority for the call.

    The sentinel can appear between a page loading and a button being pressed.
    """
    monkeypatch.setenv(config.ENV_CREDENTIAL, "not-a-real-key")
    _write_config(home)
    (home / "DISPATCH_DISABLED").write_text("", encoding="utf-8")
    with pytest.raises(client.ModelCallError) as caught:
        draft.draft_spec("t", "d", "p", home=home, opener=_opener(_text("{}")))
    assert caught.value.reason == config.SENTINEL
    assert budget.calls_in_last_hour(home) == 0


def test_every_detail_sentence_is_written_in_this_repository() -> None:
    """The closed set is closed: each reason has a sentence, and no reason lacks one."""
    assert set(config.REASON_DETAIL) == config.REASONS
    for reason, sentence in config.REASON_DETAIL.items():
        assert sentence.strip(), reason
