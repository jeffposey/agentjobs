"""Tests for the schema v2 models.

The consistency rules get the most coverage, because they are the design's actual
claim: that limbo is unrepresentable. A rule that is documented but not enforced is
exactly the v1 problem v2 exists to fix.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timezone
from typing import Any, Dict

import pytest
import yaml
from pydantic import ValidationError

from agentjobs.models_v2 import (
    BALL_REASONS,
    AcceptanceStatus,
    Ball,
    BallReason,
    BranchStatus,
    DeliverableStatus,
    DependencyType,
    Lifecycle,
    LinkRel,
    LogEntryType,
    Outcome,
    Priority,
    SchemaVersionError,
    Task,
    check_schema_version,
    load_task,
)

EXAMPLE = pathlib.Path(__file__).resolve().parents[1] / "schema" / "examples" / "task-048.v2.yaml"
NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)


def task_data(**overrides: Any) -> Dict[str, Any]:
    """A minimal valid v2 task, overridable per test."""
    base: Dict[str, Any] = {
        "schema": 2,
        "id": "task-001-example",
        "title": "Example",
        "created": NOW,
        "updated": NOW,
        "lifecycle": "draft",
        "ball": "human",
        "ball_reason": "spec",
        "ball_prompt": "Finish specifying this.",
        # Rule 6: a draft is open, so it holds a place in line like anything else.
        "queue_position": 100,
        "category": "infrastructure",
        # summary and description are both required, matching schema/agentjobs-v2.yaml.
        "spec": {"summary": "A one-line summary.", "description": "What to do."},
    }
    base.update(overrides)
    # Rule 6 is the same shape as rule 1: a closed task holds neither a ball nor a
    # place in line. Dropped here rather than at every closing test, exactly as the
    # base omits `outcome` until a test asks for one.
    if base.get("lifecycle") == "closed" and "queue_position" not in overrides:
        base.pop("queue_position", None)
    return base


class TestAgreesWithTheLinkMLSchema:
    """The Pydantic model and schema/agentjobs-v2.yaml must not drift apart."""

    def test_loads_the_linkml_validated_example(self) -> None:
        # This file validates against schema/agentjobs-v2.yaml via linkml-validate.
        # If Pydantic cannot load it, one of the two definitions has moved.
        data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))

        task = load_task(data, source=str(EXAMPLE))

        assert task.id == "task-048-schema-design"
        assert len(task.log) == 13

    def test_round_trips_without_losing_fields(self) -> None:
        data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
        task = load_task(data)

        # display_status is computed for API responses; the stored form excludes it,
        # exactly as TaskStorage._write_task does, and the strict loader rejects it.
        dumped = task.model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude={"display_status"}
        )
        reloaded = load_task(dumped)

        assert reloaded == task

    def test_what_pydantic_writes_still_validates_against_linkml(self) -> None:
        """The cross-check in the other direction, which is the one that can rot.

        Loading the example proves Pydantic accepts what LinkML produces. This proves
        LinkML accepts what Pydantic *writes* -- so a field renamed or serialised
        differently in the model cannot silently start emitting files the declared
        schema rejects.
        """
        from linkml.validator import validate

        task = load_task(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
        dumped = task.model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude={"display_status"}
        )

        report = validate(dumped, "schema/agentjobs-v2.yaml", "Task")

        assert not report.results, [result.message for result in report.results]

    def test_the_linkml_cross_check_can_actually_fail(self) -> None:
        # Guard against the assertion above passing because validation is a no-op.
        from linkml.validator import validate

        task = load_task(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
        dumped = task.model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude={"display_status"}
        )
        dumped["lifecycle"] = "nonsense"

        assert validate(dumped, "schema/agentjobs-v2.yaml", "Task").results

    def test_required_fields_match_linkml_exactly(self) -> None:
        """Required-ness must agree between the two definitions, not just field names.

        This is the drift that actually happened: models_v2 had spec.intent and
        spec.description optional while schema/agentjobs-v2.yaml required both. The
        round-trip test above did not catch it, because the one example file fills in
        every field. It only surfaced when the migrator produced 31 real tasks without
        an intent. Comparing the schemas directly is what closes that gap.
        """
        import yaml as _yaml

        linkml = _yaml.safe_load(
            pathlib.Path("schema/agentjobs-v2.yaml").read_text(encoding="utf-8")
        )

        def linkml_required(class_name: str) -> set:
            attrs = linkml["classes"][class_name].get("attributes") or {}
            return {n for n, a in attrs.items() if (a or {}).get("required")}

        def pydantic_required(model: Any) -> set:
            return {(f.alias or n) for n, f in model.model_fields.items() if f.is_required()}

        from agentjobs.models_v2 import AcceptanceCriterion, ContextPointer, LogEntry, Spec

        for name, model in [
            ("Spec", Spec),
            ("ContextPointer", ContextPointer),
            ("AcceptanceCriterion", AcceptanceCriterion),
            ("LogEntry", LogEntry),
        ]:
            assert pydantic_required(model) == linkml_required(name), (
                f"{name}: pydantic requires {sorted(pydantic_required(model))}, "
                f"LinkML requires {sorted(linkml_required(name))}"
            )

    def test_dumps_the_schema_stamp_under_its_alias(self) -> None:
        # The field is schema_version in Python because `schema` shadows a BaseModel
        # attribute; the file must still say `schema: 2`.
        task = Task.model_validate(task_data())

        assert task.model_dump(by_alias=True)["schema"] == 2


class TestSchemaStamp:
    def test_missing_stamp_names_the_migrator(self) -> None:
        data = task_data()
        del data["schema"]

        with pytest.raises(SchemaVersionError, match="agentjobs migrate-schema"):
            load_task(data)

    def test_missing_stamp_says_which_file(self) -> None:
        data = task_data()
        del data["schema"]

        with pytest.raises(SchemaVersionError, match="tasks/foo.yaml"):
            load_task(data, source="tasks/foo.yaml")

    def test_a_future_stamp_is_refused_rather_than_guessed_at(self) -> None:
        with pytest.raises(SchemaVersionError, match="understands schema 2"):
            check_schema_version({"schema": 3})

    def test_v1_file_fails_on_the_stamp_not_on_a_pile_of_field_errors(self) -> None:
        # The point of checking the stamp first: a v1 file otherwise produces a wall of
        # unknown-field errors that never mentions the real problem.
        v1_shaped = {"id": "task-001", "title": "Old", "status": "ready", "phases": []}

        with pytest.raises(SchemaVersionError):
            load_task(v1_shaped)


class TestStrictMode:
    def test_unknown_field_is_rejected_by_name(self) -> None:
        with pytest.raises(ValidationError, match="pirority"):
            Task.model_validate(task_data(pirority="high"))

    def test_unknown_nested_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Task.model_validate(
                task_data(spec={"summary": "s", "description": "d", "sumary": "typo"})
            )

    def test_spec_requires_a_description(self) -> None:
        with pytest.raises(ValidationError, match="description"):
            Task.model_validate(task_data(spec={"summary": "only a summary"}))

    def test_deleted_v1_fields_do_not_exist(self) -> None:
        for gone in ("phases", "prompts", "issues", "human_summary", "comments", "status"):
            with pytest.raises(ValidationError):
                Task.model_validate(task_data(**{gone: []}))


class TestRuleOneBallAndClosure:
    def test_open_task_requires_a_ball(self) -> None:
        with pytest.raises(ValidationError, match="ball is required"):
            Task.model_validate(task_data(ball=None, ball_reason=None, ball_prompt=None))

    def test_closed_task_must_not_have_a_ball(self) -> None:
        with pytest.raises(ValidationError, match="closed task must not have a ball"):
            Task.model_validate(
                task_data(
                    lifecycle="closed", outcome="completed", ball="human", ball_reason="review"
                )
            )

    def test_closed_task_with_no_ball_is_valid(self) -> None:
        task = Task.model_validate(
            task_data(
                lifecycle="closed",
                outcome="completed",
                ball=None,
                ball_reason=None,
                ball_prompt=None,
            )
        )

        assert task.ball is None

    def test_omitting_ball_and_spelling_it_null_are_the_same(self) -> None:
        # Design doc section 3: omission is canonical, explicit null is accepted.
        explicit = task_data(lifecycle="closed", outcome="completed", ball=None, ball_reason=None)
        implicit = task_data(lifecycle="closed", outcome="completed")
        for key in ("ball", "ball_reason", "ball_prompt"):
            implicit.pop(key, None)
        explicit.pop("ball_prompt", None)

        assert Task.model_validate(explicit) == Task.model_validate(implicit)


class TestRuleTwoBallReasonScoping:
    @pytest.mark.parametrize(
        ("ball", "reason"),
        [(ball, reason) for ball, reasons in BALL_REASONS.items() for reason in reasons],
    )
    def test_every_reason_is_accepted_for_its_own_holder(
        self, ball: Ball, reason: BallReason
    ) -> None:
        task = Task.model_validate(
            task_data(
                lifecycle="active",
                ball=ball.value,
                ball_reason=reason.value,
                assignment={"owner": "claude"},
            )
        )

        assert task.ball_reason is reason

    @pytest.mark.parametrize(
        ("ball", "reason"),
        [("human", "work"), ("agent", "review"), ("external", "decision"), ("agent", "service")],
    )
    def test_a_reason_from_another_holder_is_rejected(self, ball: str, reason: str) -> None:
        with pytest.raises(ValidationError, match="does not belong to"):
            Task.model_validate(
                task_data(
                    lifecycle="active",
                    ball=ball,
                    ball_reason=reason,
                    assignment={"owner": "claude"},
                )
            )

    def test_the_error_lists_the_permitted_reasons(self) -> None:
        with pytest.raises(
            ValidationError, match="answer, available, hold, redirect, revise, work"
        ):
            Task.model_validate(
                task_data(
                    lifecycle="active",
                    ball="agent",
                    ball_reason="review",
                    assignment={"owner": "claude"},
                )
            )

    @pytest.mark.parametrize("reason", ["answer", "redirect", "hold"])
    def test_the_reasons_task_231_added_are_agent_side(self, reason: str) -> None:
        """Each is a real distinction a human made, not a flavour of `revise`.

        The point of the values is that a cold reader can tell a rejection from an
        answer from a re-brief from a stop, which is what task-081 entry 26 had to
        repair in prose. Parametrised over the three so a value dropped from
        `BALL_REASONS` fails here rather than silently narrowing the vocabulary.
        """
        task = Task.model_validate(
            task_data(
                lifecycle="active",
                ball="agent",
                ball_reason=reason,
                assignment={"owner": "claude"},
            )
        )

        assert task.ball_reason == reason

    @pytest.mark.parametrize("reason", ["answer", "redirect", "hold"])
    def test_the_new_reasons_are_not_human_side(self, reason: str) -> None:
        with pytest.raises(ValidationError, match="does not belong to"):
            Task.model_validate(task_data(lifecycle="active", ball="human", ball_reason=reason))

    def test_a_held_task_reads_as_stopped_not_as_progress(self) -> None:
        """`display_status` is what a human scanning the list acts on.

        Every other agent-side reason means somebody is working; `hold` means nobody is,
        deliberately. Reading "In progress (claude)" on a task a human stopped is the
        list lying about the one state it was told to make visible.
        """
        held = Task.model_validate(
            task_data(
                lifecycle="active",
                ball="agent",
                ball_reason="hold",
                ball_prompt="Wait for the dispatch fixes to land.",
                assignment={"owner": "claude"},
            )
        )

        assert held.display_status == "On hold (claude)"

    def test_ball_without_a_reason_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="ball_reason is required"):
            Task.model_validate(task_data(ball_reason=None))

    def test_reason_without_a_ball_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="who holds it"):
            Task.model_validate(
                task_data(
                    lifecycle="closed",
                    outcome="completed",
                    ball=None,
                    ball_reason="review",
                    ball_prompt=None,
                )
            )


class TestRuleThreeOutcome:
    def test_closed_requires_an_outcome(self) -> None:
        with pytest.raises(ValidationError, match="closed task needs an outcome"):
            Task.model_validate(
                task_data(lifecycle="closed", ball=None, ball_reason=None, ball_prompt=None)
            )

    def test_open_must_not_have_an_outcome(self) -> None:
        with pytest.raises(ValidationError, match="only closed tasks have an outcome"):
            Task.model_validate(task_data(outcome="completed"))

    @pytest.mark.parametrize("outcome", [o.value for o in Outcome])
    def test_every_outcome_closes_a_task(self, outcome: str) -> None:
        task = Task.model_validate(
            task_data(
                lifecycle="closed", outcome=outcome, ball=None, ball_reason=None, ball_prompt=None
            )
        )

        assert task.outcome is Outcome(outcome)


class TestRuleFourBallPrompt:
    def test_a_handoff_without_its_ask_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="ball_prompt is required"):
            Task.model_validate(task_data(ball_prompt=None))

    def test_whitespace_does_not_count_as_an_ask(self) -> None:
        with pytest.raises(ValidationError, match="ball_prompt is required"):
            Task.model_validate(task_data(ball_prompt="   \n  "))

    def test_agent_available_may_omit_it_because_the_spec_is_the_ask(self) -> None:
        task = Task.model_validate(
            task_data(lifecycle="ready", ball="agent", ball_reason="available", ball_prompt=None)
        )

        assert task.ball_prompt is None


class TestRuleFiveOwner:
    @pytest.mark.parametrize("lifecycle", ["draft", "ready"])
    def test_unclaimed_lifecycles_must_not_have_an_owner(self, lifecycle: str) -> None:
        with pytest.raises(ValidationError, match="must be empty"):
            Task.model_validate(
                task_data(
                    lifecycle=lifecycle,
                    ball="agent",
                    ball_reason="available",
                    ball_prompt=None,
                    assignment={"owner": "claude"},
                )
            )

    def test_active_requires_an_owner(self) -> None:
        with pytest.raises(ValidationError, match="assignment.owner is required"):
            Task.model_validate(
                task_data(
                    lifecycle="active",
                    ball="agent",
                    ball_reason="work",
                    ball_prompt="Do the thing.",
                )
            )

    def test_eligible_is_authoring_time_and_independent_of_owner(self) -> None:
        task = Task.model_validate(
            task_data(
                lifecycle="ready",
                ball="agent",
                ball_reason="available",
                ball_prompt=None,
                assignment={"eligible": ["claude", "codex"]},
            )
        )

        assert task.assignment.owner is None
        assert task.assignment.eligible == ["claude", "codex"]


class TestLogIntegrity:
    def _with_log(self, *entries: Dict[str, Any]) -> Dict[str, Any]:
        return task_data(log=list(entries))

    def _entry(self, entry_id: int, **kw: Any) -> Dict[str, Any]:
        base = {"id": entry_id, "ts": NOW, "actor": "claude", "type": "note", "body": "x"}
        base.update(kw)
        return base

    def test_duplicate_ids_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate log entry id"):
            Task.model_validate(self._with_log(self._entry(1), self._entry(1)))

    def test_out_of_order_ids_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="append-only"):
            Task.model_validate(self._with_log(self._entry(2), self._entry(1)))

    def test_threading_to_a_missing_entry_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="does not exist"):
            Task.model_validate(self._with_log(self._entry(1, re=99)))

    def test_threading_forward_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not earlier"):
            Task.model_validate(self._with_log(self._entry(1), self._entry(2, re=2)))

    def test_next_log_id_continues_the_sequence(self) -> None:
        task = Task.model_validate(self._with_log(self._entry(1), self._entry(7)))

        assert task.next_log_id() == 8

    def test_next_log_id_starts_at_one_for_an_empty_log(self) -> None:
        assert Task.model_validate(task_data()).next_log_id() == 1

    def test_open_questions_excludes_answered_ones(self) -> None:
        task = Task.model_validate(
            self._with_log(
                self._entry(1, type="question", body="Which one?"),
                self._entry(2, type="question", body="And this?"),
                self._entry(3, type="answer", re=1, body="That one."),
            )
        )

        assert [entry.id for entry in task.open_questions()] == [2]


class TestParent:
    def test_a_task_cannot_be_its_own_parent(self) -> None:
        with pytest.raises(ValidationError, match="cannot be its own parent"):
            Task.model_validate(task_data(parent="task-001-example"))

    def test_a_different_parent_is_fine(self) -> None:
        assert Task.model_validate(task_data(parent="task-000-umbrella")).parent == (
            "task-000-umbrella"
        )


class TestDisplayStatus:
    """Derived on read, never stored (design doc section 3)."""

    def test_it_is_not_a_stored_field(self) -> None:
        # Computed for API responses, never stored: it is not a model field, and a
        # file that contains it is rejected by name rather than round-tripped.
        assert "display_status" not in Task.model_fields
        with pytest.raises(ValidationError):
            Task.model_validate(task_data(display_status="Ready"))

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"ball": "human", "ball_reason": "review"}, "Needs review"),
            ({"ball": "human", "ball_reason": "decision"}, "Needs decision"),
            ({"ball": "human", "ball_reason": "approval"}, "Needs approval"),
            ({"ball": "human", "ball_reason": "spec"}, "Needs spec"),
            ({"ball": "human", "ball_reason": "input"}, "Needs input"),
        ],
    )
    def test_human_reasons(self, overrides: Dict[str, Any], expected: str) -> None:
        assert Task.model_validate(task_data(**overrides)).display_status == expected

    def test_ready(self) -> None:
        task = Task.model_validate(
            task_data(lifecycle="ready", ball="agent", ball_reason="available", ball_prompt=None)
        )

        assert task.display_status == "Ready"

    def test_in_progress_names_the_owner(self) -> None:
        task = Task.model_validate(
            task_data(
                lifecycle="active",
                ball="agent",
                ball_reason="work",
                ball_prompt="Do it.",
                assignment={"owner": "claude"},
            )
        )

        assert task.display_status == "In progress (claude)"

    def test_blocked_names_the_dependency(self) -> None:
        task = Task.model_validate(
            task_data(
                lifecycle="active",
                ball="external",
                ball_reason="dependency",
                ball_prompt="Waiting on task-044.",
                assignment={"owner": "claude"},
                dependencies=[{"task": "task-044-docs", "type": "needs"}],
            )
        )

        assert task.display_status == "Blocked on task-044-docs"

    def test_closed_shows_its_outcome(self) -> None:
        task = Task.model_validate(
            task_data(
                lifecycle="closed",
                outcome="superseded",
                ball=None,
                ball_reason=None,
                ball_prompt=None,
            )
        )

        assert task.display_status == "Superseded"

    def test_archived_is_orthogonal_to_outcome(self) -> None:
        task = Task.model_validate(
            task_data(
                lifecycle="closed",
                outcome="completed",
                archived=True,
                ball=None,
                ball_reason=None,
                ball_prompt=None,
            )
        )

        assert task.display_status == "Completed (archived)"


class TestValueObjects:
    def test_links_validate_their_url(self) -> None:
        with pytest.raises(ValidationError):
            Task.model_validate(task_data(links=[{"url": "not-a-url", "rel": "pr"}]))

    def test_a_real_url_is_accepted(self) -> None:
        task = Task.model_validate(
            task_data(links=[{"url": "https://github.com/x/y/pull/1", "rel": "pr"}])
        )

        assert task.links[0].rel.value == "pr"

    def test_acceptance_and_deliverable_vocabularies_stay_distinct(self) -> None:
        # A criterion is verified (met); a deliverable is produced (done). Collapsing
        # them recreates the v1 problem of one word straining across meanings.
        with pytest.raises(ValidationError):
            Task.model_validate(
                task_data(acceptance=[{"id": "ac-1", "text": "t", "status": "done"}])
            )
        with pytest.raises(ValidationError):
            Task.model_validate(task_data(deliverables=[{"path": "p", "status": "met"}]))

    def test_dependency_type_uses_needs_not_depends_on(self) -> None:
        with pytest.raises(ValidationError):
            Task.model_validate(task_data(dependencies=[{"task": "t", "type": "depends_on"}]))

        task = Task.model_validate(task_data(dependencies=[{"task": "t", "type": "needs"}]))
        assert task.dependencies[0].type.value == "needs"


class TestDefaults:
    def test_a_minimal_task_is_a_draft_awaiting_its_spec(self) -> None:
        task = Task.model_validate(task_data())

        assert task.lifecycle is Lifecycle.DRAFT
        assert task.priority is Priority.MEDIUM
        assert task.archived is False
        assert task.is_open is True

    def test_priority_rank_orders_critical_first(self) -> None:
        rank = {
            p: Task.model_validate(task_data(priority=p.value)).priority_rank() for p in Priority
        }

        assert (
            rank[Priority.CRITICAL]
            < rank[Priority.HIGH]
            < rank[Priority.MEDIUM]
            < (rank[Priority.LOW])
        )


class TestEnumsRenderAsTheirValue:
    """str() must give `ready`, not `Lifecycle.READY` (Python 3.11 mixin-enum change).

    This shipped broken: the task list wrote `data-ball="Ball.HUMAN"` into its filter
    attributes, so filtering by ball or lifecycle matched nothing, while the status badge
    beside it rendered correctly because it *compares* rather than renders. Comparisons
    are unaffected by the 3.11 change, which is what let the bug through every existing
    test -- they assert on comparisons and on JSON, and Pydantic serialises by value.
    """

    ENUMS = [
        Lifecycle,
        Ball,
        BallReason,
        Outcome,
        Priority,
        AcceptanceStatus,
        DeliverableStatus,
        BranchStatus,
        DependencyType,
        LinkRel,
        LogEntryType,
    ]

    @pytest.mark.parametrize("enum_cls", ENUMS, ids=lambda c: c.__name__)
    def test_str_is_the_value(self, enum_cls: Any) -> None:
        for member in enum_cls:
            assert str(member) == member.value

    @pytest.mark.parametrize("enum_cls", ENUMS, ids=lambda c: c.__name__)
    def test_format_is_the_value(self, enum_cls: Any) -> None:
        # f-strings and Jinja's `{{ }}` take this path.
        for member in enum_cls:
            assert f"{member}" == member.value

    def test_comparison_against_a_bare_string_still_works(self) -> None:
        # The property that kept the bug hidden must itself keep working: templates
        # and routes compare against plain strings all over.
        assert Ball.HUMAN == "human"
        assert Lifecycle.CLOSED == "closed"


class TestDocumentationMatchesTheModel:
    """sc-4: docs/task-schema.md must describe v2 and stay in step with it.

    A reference page drifts silently -- nothing breaks when a field is added and the
    docs are not touched. This is the cheapest guard that actually catches it: every
    value of every closed vocabulary has to appear somewhere on the page.
    """

    DOC = pathlib.Path(__file__).resolve().parents[1] / "docs" / "task-schema.md"

    def test_the_page_documents_v2(self) -> None:
        text = self.DOC.read_text(encoding="utf-8")

        assert "## Schema v2" in text
        assert "schema: 2" in text

    @pytest.mark.parametrize(
        "enum_cls",
        [Lifecycle, Ball, BallReason, Outcome, Priority],
    )
    def test_every_state_vocabulary_value_is_documented(self, enum_cls: Any) -> None:
        text = self.DOC.read_text(encoding="utf-8")

        missing = [member.value for member in enum_cls if f"`{member.value}`" not in text]

        assert not missing, f"{enum_cls.__name__} values absent from the docs: {missing}"

    def test_every_task_field_is_documented(self) -> None:
        text = self.DOC.read_text(encoding="utf-8")
        # by_alias so `schema` is checked rather than the Python-side schema_version.
        names = {field.alias or name for name, field in Task.model_fields.items()}

        # `links[]` is an accepted way to write a list field, so both forms count.
        missing = [
            name for name in sorted(names) if f"`{name}`" not in text and f"`{name}[]`" not in text
        ]

        assert not missing, f"v2 Task fields absent from the docs: {missing}"
