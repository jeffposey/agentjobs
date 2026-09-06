"""The record-quality check, and the two things about it that are easy to get wrong.

Task-306, from finding F-14 of the 2026-08-21 context audit. Two of these classes are
about the checks themselves; the other two are about the design decision recorded on
task-306, which is what would silently rot:

* ``TestVerbScoping`` -- a condition may only be raised by a verb that could have caused
  it. Without that the checks become a per-call lint on records the caller never wrote,
  which is exactly the alternative the decision rejected.
* ``TestDefaultPromptsStayInStep`` -- the three default prompts are copied into
  ``record_check`` so it depends on nothing but the model. A copy that falls out of step
  with its source turns the check into a no-op that still passes every test about its
  own behaviour, so the copy is pinned against the source.
* ``TestValidateIsUnaffected`` -- the corpus sweep must keep reporting nothing about
  either condition, however many records are in that state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentjobs import record_check
from agentjobs.manager import WORK_PROMPT, TaskManager
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType, Task
from agentjobs.record_check import (
    DEFAULT_BALL_PROMPT,
    DEFAULT_BALL_PROMPTS,
    LONG_SUMMARY,
    SUMMARY_WORD_CEILING,
    UNNAMED_REVIEW_LINK,
    WARNING_KINDS,
    check_record,
    summary_words,
    warning_dicts,
)
from agentjobs.storage import TaskStorage
from agentjobs.validation import validate_corpus

#: A summary one word past the ceiling. Built rather than written out so the fixture
#: cannot drift from the constant it is meant to be one side of.
TOO_LONG = " ".join(["word"] * (SUMMARY_WORD_CEILING + 1))
JUST_SHORT = " ".join(["word"] * SUMMARY_WORD_CEILING)


@pytest.fixture()
def manager(tmp_path: Path) -> TaskManager:
    """A manager over an empty corpus."""
    return TaskManager(TaskStorage(tmp_path / "tasks"))


def worked_task(manager: TaskManager, *, summary: str = "Short enough.") -> Task:
    """A task claimed by an agent, with one entry logged since the claim.

    This is the state finding F-14 named: a session has been working and has never said
    what the task is waiting on, so ``ball_prompt`` is still the claim's own default.
    """
    task = manager.create_task(
        id="task-001",
        title="Work",
        description="Do the thing.",
        summary=summary,
        lifecycle=Lifecycle.READY,
    )
    manager.claim_task(task.id, agent="bot")
    return manager.add_log_entry(task.id, actor="bot", type=LogEntryType.PROGRESS, body="Started.")


def review_task(manager: TaskManager, *, prompt: str) -> Task:
    """A branch handed to a human for review, with ``prompt`` as the ask."""
    task = manager.create_task(
        id="task-002",
        title="Review",
        description="Look at the thing.",
        summary="Short enough.",
        lifecycle=Lifecycle.READY,
    )
    manager.claim_task(task.id, agent="bot")
    return manager.handoff(
        task.id,
        actor="bot",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt=prompt,
    )


class TestSummaryLength:
    def test_a_summary_at_the_ceiling_is_silent(self, manager):
        task = manager.create_task(
            id="task-001", title="T", description="D", summary=JUST_SHORT, lifecycle=Lifecycle.READY
        )

        assert check_record(task, verb="create") == []

    def test_a_summary_past_the_ceiling_names_its_length_and_the_fix(self, manager):
        task = manager.create_task(
            id="task-001", title="T", description="D", summary=TOO_LONG, lifecycle=Lifecycle.READY
        )

        warnings = check_record(task, verb="create")

        assert [warning.kind for warning in warnings] == [LONG_SUMMARY]
        message = warnings[0].message
        assert f"{SUMMARY_WORD_CEILING + 1} words" in message
        assert "spec.description" in message

    def test_words_are_counted_the_way_the_audit_counted_them(self):
        assert summary_words("one two  three\nfour") == 4
        assert summary_words("") == 0


class TestDefaultBallPrompt:
    def test_the_claim_itself_is_silent(self, manager):
        """The claim wrote the default a moment ago; that is its job, not a drift."""
        task = manager.create_task(
            id="task-001", title="T", description="D", lifecycle=Lifecycle.READY
        )
        claimed = manager.claim_task(task.id, agent="bot")

        assert claimed.ball_prompt == WORK_PROMPT
        assert check_record(claimed, verb="log_append") == []

    def test_a_default_surviving_one_entry_later_is_reported(self, manager):
        task = worked_task(manager)

        warnings = check_record(task, verb="log_append")

        assert [warning.kind for warning in warnings] == [DEFAULT_BALL_PROMPT]
        assert "1 log entry has been added" in warnings[0].message

    def test_the_count_agrees_with_itself_when_there_is_more_than_one(self, manager):
        task = worked_task(manager)
        task = manager.add_log_entry(
            task.id, actor="bot", type=LogEntryType.NOTE, body="And again."
        )

        assert "2 log entries have been added" in check_record(task, verb="log_append")[0].message

    def test_a_prompt_somebody_wrote_is_silent(self, manager):
        task = worked_task(manager)
        stated = manager.handoff(
            task.id,
            actor="bot",
            ball=Ball.AGENT,
            ball_reason=BallReason.WORK,
            ball_prompt="Rebase onto main, then re-run the e2e stage.",
        )
        stated = manager.add_log_entry(
            stated.id, actor="bot", type=LogEntryType.PROGRESS, body="More."
        )

        assert check_record(stated, verb="log_append") == []

    def test_a_task_not_at_agent_work_is_silent(self, manager):
        """A ready task's empty prompt is schema-permitted and says nothing is wrong."""
        task = manager.create_task(
            id="task-001", title="T", description="D", lifecycle=Lifecycle.READY
        )

        assert task.ball_reason is BallReason.AVAILABLE
        assert check_record(task, verb="log_append") == []

    def test_the_count_is_entries_since_the_claim_not_the_whole_log(self, manager):
        """A task with a long history before its claim must not report that history."""
        task = worked_task(manager)
        message = check_record(task, verb="log_append")[0].message

        assert "1 log entry" in message
        assert len(task.log) > 1


class TestVerbScoping:
    """A condition is raised only by a verb that could have caused it.

    This is the recorded decision on task-306, and the property that keeps an ordinary
    write silent. Claiming a task somebody else specified must not lecture the claimer
    about a summary they neither wrote nor should rewrite.
    """

    def test_claiming_a_long_summary_says_nothing_about_it(self, manager):
        task = manager.create_task(
            id="task-001", title="T", description="D", summary=TOO_LONG, lifecycle=Lifecycle.READY
        )
        claimed = manager.claim_task(task.id, agent="bot")

        assert check_record(claimed, verb="claim") == []
        assert check_record(claimed, verb="log_append") == []
        assert check_record(claimed, verb="close") == []

    def test_only_the_spec_writing_verbs_raise_a_long_summary(self, manager):
        task = manager.create_task(
            id="task-001", title="T", description="D", summary=TOO_LONG, lifecycle=Lifecycle.READY
        )

        raised = {
            verb
            for verb in ("create", "update_content", "claim", "log_append", "handoff", "close")
            if check_record(task, verb=verb)
        }

        assert raised == record_check.SPEC_WRITING_VERBS

    def test_appending_to_a_worked_task_says_nothing_about_its_summary(self, manager):
        task = worked_task(manager, summary=TOO_LONG)

        kinds = [warning.kind for warning in check_record(task, verb="log_append")]

        assert kinds == [DEFAULT_BALL_PROMPT]

    def test_no_verb_evaluates_every_condition_for_the_corpus_view(self, manager):
        # Two records rather than one, because the conditions are no longer jointly
        # satisfiable: a default ask survives only at agent/work, and a review link is
        # only ever handed to a human. The property being pinned is that every kind is
        # reachable with no verb -- a condition nothing can raise is a dead check.
        worked = worked_task(manager, summary=TOO_LONG)
        handed = review_task(manager, prompt="Open http://127.0.0.1:8910/app/ and look.")

        kinds = {warning.kind for warning in check_record(worked)}
        kinds |= {warning.kind for warning in check_record(handed)}

        assert kinds == set(WARNING_KINDS)


class TestReviewLinks:
    """The link convention the review panel reads, checked where it is written (task-363).

    The panel lifts an address into its card only when the address is alone on its
    line, and titles the row from the ``Name: `` in front of it. An address left in the
    middle of a sentence cannot be named and cannot be moved without leaving a hole, so
    the only place to resolve it is the handoff being written -- which is where this
    fires, at the moment the agent could still fix it in one edit.
    """

    def test_named_link_lines_say_nothing(self, manager):
        task = review_task(
            manager,
            prompt=(
                "Three checks; the third needs a task page rather than the list.\n"
                "\n"
                "Desktop shell: http://127.0.0.1:8910/app/\n"
                "Tablet: http://127.0.0.1:8910/app/?w=1024\n"
                "- Task page for check 3: http://127.0.0.1:8910/app/p/s/tasks/task-143\n"
            ),
        )

        assert check_record(task, verb="handoff") == []

    def test_an_address_in_a_sentence_is_raised(self, manager):
        task = review_task(manager, prompt="Open http://127.0.0.1:8910/app/ and confirm the log.")

        warnings = check_record(task, verb="handoff")

        assert [warning.kind for warning in warnings] == [UNNAMED_REVIEW_LINK]
        assert "http://127.0.0.1:8910/app/" in warnings[0].message

    def test_a_bare_address_on_its_own_line_is_raised_for_having_no_name(self, manager):
        # The panel can hoist this one; it cannot say what it is, which was the other
        # half of the review that produced the convention.
        task = review_task(manager, prompt="Look at it:\n\nhttp://127.0.0.1:8910/app/\n")

        assert [w.kind for w in check_record(task, verb="handoff")] == [UNNAMED_REVIEW_LINK]

    def test_a_handoff_with_no_address_says_nothing(self, manager):
        task = review_task(manager, prompt="Read the diff and approve.")

        assert check_record(task, verb="handoff") == []

    def test_an_address_handed_to_an_agent_is_prose_like_any_other(self, manager):
        # No card on that screen, so there is nothing to name it for.
        task = review_task(manager, prompt="Read the diff.")
        handed = manager.handoff(
            task.id,
            actor="Jeff Posey",
            ball=Ball.AGENT,
            ball_reason=BallReason.REVISE,
            ball_prompt="See http://127.0.0.1:8910/app/ for what I mean.",
        )

        assert check_record(handed, verb="handoff") == []

    def test_only_a_prompt_writing_verb_raises_it(self, manager):
        task = review_task(manager, prompt="Open http://127.0.0.1:8910/app/ and confirm.")

        raised = {
            verb
            for verb in ("create", "update_content", "claim", "log_append", "handoff", "close")
            if any(w.kind == UNNAMED_REVIEW_LINK for w in check_record(task, verb=verb))
        }

        assert raised == record_check.PROMPT_WRITING_VERBS

    @pytest.mark.parametrize(
        "line, named",
        [
            ("Desktop shell: http://127.0.0.1:8910/app/", True),
            ("  Tablet:  http://127.0.0.1:8910/app/?w=1024  ", True),
            ("- Build: https://example.test/ci/9", True),
            ("* PR: https://example.test/pull/9", True),
            ("http://127.0.0.1:8910/app/", False),
            ("Open http://127.0.0.1:8910/app/ and look", False),
            ("Compare https://example.test/a with https://example.test/b", False),
            ("Open https://example.test/x", False),
        ],
    )
    def test_the_line_shapes_the_panel_and_this_check_must_agree_on(self, line, named):
        """Pinned here because the same rule is implemented in ReviewLinks.tsx.

        The two run in different runtimes and neither can call the other, so this list
        is what stops one being changed without the other. Each row is a shape a real
        handoff has taken.
        """
        assert (record_check.unnamed_review_links(line) == []) is named


class TestDefaultPromptsStayInStep:
    """The copied prompt strings, pinned against the modules that write them."""

    def test_the_claim_default_is_one_of_them(self):
        assert WORK_PROMPT in DEFAULT_BALL_PROMPTS

    def test_the_draft_and_import_defaults_are_too(self, manager):
        drafted = manager.create_task(id="task-001", title="T", description="D")
        assert drafted.ball_prompt in DEFAULT_BALL_PROMPTS

        # The migration writes its own variant and has no verb to call, so the pin is
        # against its source: the string it hands a converted task must be one this
        # module knows about, or an imported corpus is invisible to the check.
        source = Path(record_check.__file__).parent / "migration" / "converter.py"
        text = source.read_text(encoding="utf-8")
        assert any(f'"{prompt}"' in text for prompt in DEFAULT_BALL_PROMPTS)

    def test_the_supervision_prompt_is_not_one_of_them(self):
        """It names the open children, so it is never boilerplate nobody wrote."""
        from agentjobs.manager import supervision_prompt

        assert supervision_prompt(["task-002"]) not in DEFAULT_BALL_PROMPTS


class TestValidateIsUnaffected:
    """Neither condition may reach the corpus sweep, whatever state the corpus is in."""

    def test_a_corpus_full_of_both_conditions_still_validates_clean(self, manager, tmp_path):
        for index in range(3):
            task = manager.create_task(
                id=f"task-00{index + 1}",
                title="T",
                description="D",
                summary=TOO_LONG,
                lifecycle=Lifecycle.READY,
            )
            manager.claim_task(task.id, agent="bot")
            manager.add_log_entry(task.id, actor="bot", type=LogEntryType.PROGRESS, body="On it.")

        report = validate_corpus(tmp_path / "tasks")

        assert report.checked == 3
        assert report.ok, report.render()
        assert not any(kind in report.render() for kind in WARNING_KINDS)


class TestStructuredForm:
    def test_warnings_render_as_kind_and_message(self, manager):
        task = manager.create_task(
            id="task-001", title="T", description="D", summary=TOO_LONG, lifecycle=Lifecycle.READY
        )

        rendered = warning_dicts(check_record(task, verb="create"))

        assert rendered == [{"kind": LONG_SUMMARY, "message": rendered[0]["message"]}]
        assert set(rendered[0]) == {"kind", "message"}
