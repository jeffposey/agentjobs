"""Tests for the v1 to v2 migrator.

Two of these matter more than the rest: that the field-accounting gate refuses an
unrecognised field, and that verify_no_loss actually detects loss. Both are safety
machinery, and safety machinery that cannot fail is decoration.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from agentjobs.migrate_schema import (
    AlreadyV2Error,
    MigrationError,
    UnmappedFieldError,
    convert_task,
    migrate_corpus,
    normalise_actors,
    verify_no_loss,
)
from agentjobs.models_v2 import load_task

CORPUS = Path(__file__).resolve().parents[1] / "tasks"


def v1_task(**overrides: Any) -> Dict[str, Any]:
    """A minimal but realistic v1 task."""
    base: Dict[str, Any] = {
        "id": "task-900-example",
        "title": "Example task",
        "created": "2026-01-01T00:00:00Z",
        "updated": "2026-01-02T00:00:00Z",
        "status": "draft",
        "priority": "medium",
        "category": "infrastructure",
        "estimated_effort": "2 hours",
        "description": "Do the thing described here.",
        "prompts": {"starter": "Do the thing described here.", "followups": []},
        "tags": ["example"],
    }
    base.update(overrides)
    return base


class TestFieldAccounting:
    """An unrecognised field must stop the run, not vanish."""

    def test_unknown_field_refuses_conversion(self) -> None:
        with pytest.raises(UnmappedFieldError, match="mystery_field"):
            convert_task(v1_task(mystery_field="something important"))

    def test_the_error_says_what_to_do_about_it(self) -> None:
        with pytest.raises(UnmappedFieldError, match="MAPPED_FIELDS or INTENTIONALLY_DROPPED"):
            convert_task(v1_task(mystery_field=1))

    def test_intentionally_dropped_fields_pass(self) -> None:
        # issues[] is empty corpus-wide and its model is deleted in v2.
        conversion = convert_task(v1_task(issues=[]))

        assert "issues" not in conversion.data

    def test_already_v2_is_refused(self) -> None:
        with pytest.raises(AlreadyV2Error, match="will not run twice"):
            convert_task({"schema": 2, "id": "t"})

    def test_a_task_with_no_id_is_refused(self) -> None:
        data = v1_task()
        del data["id"]
        with pytest.raises(MigrationError, match="no id"):
            convert_task(data)

    def test_an_unknown_status_is_refused_rather_than_defaulted(self) -> None:
        with pytest.raises(MigrationError, match="unknown v1 status"):
            convert_task(v1_task(status="mystery"))


class TestLossVerificationCanFail:
    """verify_no_loss is the main safety net; prove it detects each kind of loss."""

    def _converted(self) -> tuple:
        v1 = v1_task(
            status="completed",
            # A description long enough that it is NOT wholly duplicated into the
            # derived summary. With a short one, dropping spec.description leaves the
            # text still findable in summary -- and verify_no_loss is right to say
            # nothing was lost. The fixture has to be able to lose something.
            description=(
                "First sentence that becomes the summary. "
                "A second paragraph containing the distinctive phrase quokka-parade "
                "that appears nowhere else in the record."
            ),
            human_summary="A human-written summary.",
            success_criteria=[{"id": "sc-1", "description": "It works", "status": "completed"}],
            deliverables=[{"path": "src/thing.py", "status": "completed"}],
            branches=[{"name": "feat/thing", "status": "merged"}],
            status_updates=[
                {
                    "timestamp": "2026-01-01T10:00:00Z",
                    "author": "claude",
                    "status": "in_progress",
                    "summary": "Started work",
                    "details": "A distinctive detail sentence.",
                }
            ],
        )
        return v1, convert_task(v1).data

    def test_a_clean_conversion_reports_no_loss(self) -> None:
        v1, v2 = self._converted()

        assert verify_no_loss(v1, v2) == []

    @pytest.mark.parametrize(
        ("label", "break_it"),
        [
            ("description dropped", lambda d: d["spec"].pop("description", None)),
            ("acceptance truncated", lambda d: d.__setitem__("acceptance", [])),
            ("deliverables dropped", lambda d: d.pop("deliverables", None)),
            ("branches dropped", lambda d: d.pop("branches", None)),
            ("log truncated", lambda d: d.__setitem__("log", [])),
            ("criterion text emptied", lambda d: d["acceptance"][0].__setitem__("text", "")),
        ],
    )
    def test_each_kind_of_loss_is_detected(self, label: str, break_it: Any) -> None:
        v1, v2 = self._converted()
        broken = copy.deepcopy(v2)
        break_it(broken)

        assert verify_no_loss(v1, broken), f"{label} was not detected"


class TestStateMapping:
    @pytest.mark.parametrize(
        ("v1_status", "expected"),
        [
            ("draft", {"lifecycle": "draft", "ball": "human", "ball_reason": "spec"}),
            ("ready", {"lifecycle": "ready", "ball": "agent", "ball_reason": "available"}),
            ("completed", {"lifecycle": "closed", "outcome": "completed"}),
        ],
    )
    def test_simple_statuses(self, v1_status: str, expected: Dict[str, Any]) -> None:
        data = convert_task(v1_task(status=v1_status)).data

        for key, value in expected.items():
            assert data[key] == value

    def test_in_progress_becomes_active_with_an_owner(self) -> None:
        data = convert_task(v1_task(status="in_progress", assigned_to="Codex")).data

        assert data["lifecycle"] == "active"
        assert data["ball"] == "agent"
        assert data["assignment"]["owner"] == "codex"

    def test_under_review_lands_on_human_review(self) -> None:
        data = convert_task(v1_task(status="under_review", assigned_to="claude")).data

        assert (data["ball"], data["ball_reason"]) == ("human", "review")

    def test_waiting_for_human_reads_decision_from_the_last_update(self) -> None:
        data = convert_task(
            v1_task(
                status="waiting_for_human",
                assigned_to="claude",
                status_updates=[
                    {
                        "timestamp": "2026-01-01T10:00:00Z",
                        "author": "claude",
                        "status": "waiting_for_human",
                        "summary": "Need a decision on the storage backend",
                    }
                ],
            )
        ).data

        assert data["ball_reason"] == "decision"
        assert "storage backend" in data["ball_prompt"]

    def test_blocked_and_claimed_becomes_active_and_external(self) -> None:
        data = convert_task(
            v1_task(
                status="blocked",
                assigned_to="claude",
                branches=[{"name": "feat/x", "status": "active"}],
                status_updates=[
                    {
                        "timestamp": "2026-01-01T10:00:00Z",
                        "author": "claude",
                        "status": "blocked",
                        "summary": "Waiting on the upstream fix",
                    }
                ],
            )
        ).data

        assert (data["lifecycle"], data["ball"], data["ball_reason"]) == (
            "active",
            "external",
            "dependency",
        )

    def test_blocked_and_unclaimed_becomes_ready_and_says_so(self) -> None:
        conversion = convert_task(v1_task(status="blocked"))

        assert conversion.data["lifecycle"] == "ready"
        assert any("blocked" in note.detail for note in conversion.notes)

    def test_archived_reads_its_outcome_from_the_record(self) -> None:
        superseded = convert_task(
            v1_task(
                status="archived",
                status_updates=[
                    {
                        "timestamp": "2026-01-01T10:00:00Z",
                        "author": "codex",
                        "status": "archived",
                        "summary": "Archived in favour of phase-specific tasks",
                    }
                ],
            )
        ).data
        cancelled = convert_task(v1_task(status="archived")).data

        assert superseded["outcome"] == "superseded"
        assert superseded["archived"] is True
        assert cancelled["outcome"] == "cancelled"


class TestActorNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Codex", ["codex"]),
            ("claude", ["claude"]),
            ("Claude + Codex", ["claude", "codex"]),
            ("TBD", []),
            ("", []),
            (None, []),
        ],
    )
    def test_real_corpus_values(self, raw: Any, expected: list) -> None:
        assert normalise_actors(raw) == expected

    def test_a_non_actor_assignee_is_flagged_not_silently_dropped(self) -> None:
        conversion = convert_task(v1_task(assigned_to="TBD"))

        assert any(note.field == "assigned_to" for note in conversion.notes)

    def test_unclaimed_tasks_get_eligible_not_owner(self) -> None:
        data = convert_task(v1_task(status="ready", assigned_to="Codex")).data

        assert data["assignment"] == {"eligible": ["codex"]}


class TestContentPreservation:
    def test_a_distinct_starter_is_preserved(self) -> None:
        # The design expected starters to be droppable duplicates; 37 of 38 are not.
        data = convert_task(
            v1_task(
                description="The description.",
                prompts={"starter": "A genuinely different briefing.", "followups": []},
            )
        ).data

        assert "A genuinely different briefing." in data["spec"]["description"]

    def test_a_duplicate_starter_is_not_repeated(self) -> None:
        data = convert_task(
            v1_task(description="Same text.", prompts={"starter": "Same text.", "followups": []})
        ).data

        assert data["spec"]["description"].count("Same text.") == 1

    def test_phases_become_a_description_appendix(self) -> None:
        data = convert_task(
            v1_task(
                phases=[
                    {
                        "id": "phase-1",
                        "title": "Do first",
                        "status": "completed",
                        "notes": "A phase note worth keeping.",
                    }
                ]
            )
        ).data

        assert "Do first" in data["spec"]["description"]
        assert "A phase note worth keeping." in data["spec"]["description"]

    def test_followups_become_instruction_entries(self) -> None:
        data = convert_task(
            v1_task(
                prompts={
                    "starter": "Start.",
                    "followups": [
                        {
                            "timestamp": "2026-01-01T12:00:00Z",
                            "author": "jeff",
                            "content": "Also check the README.",
                        }
                    ],
                }
            )
        ).data

        instructions = [e for e in data["log"] if e["type"] == "instruction"]
        assert len(instructions) == 1
        assert instructions[0]["actor"] == "jeff"
        assert "README" in instructions[0]["body"]

    def test_a_question_comment_becomes_a_question_entry(self) -> None:
        data = convert_task(
            v1_task(
                comments=[
                    {
                        "id": "c1",
                        "task_id": "t",
                        "author": "claude",
                        "kind": "question",
                        "content": "Which approach?",
                        "created": "2026-01-01T11:00:00Z",
                    }
                ]
            )
        ).data

        assert [e["type"] for e in data["log"] if e["type"] == "question"] == ["question"]

    def test_every_task_gains_an_audit_entry_naming_its_v1_status(self) -> None:
        data = convert_task(v1_task(status="completed")).data
        audit = data["log"][-1]

        assert audit["actor"] == "system"
        assert audit["data"]["v1_status"] == "completed"
        assert "migrate-schema" in audit["body"]

    def test_the_log_is_ordered_by_time(self) -> None:
        data = convert_task(
            v1_task(
                status_updates=[
                    {
                        "timestamp": "2026-01-03T00:00:00Z",
                        "author": "a",
                        "status": "draft",
                        "summary": "later",
                    },
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "author": "a",
                        "status": "draft",
                        "summary": "earlier",
                    },
                ]
            )
        ).data

        bodies = [e["body"] for e in data["log"][:2]]
        assert bodies[0].startswith("earlier")
        assert [e["id"] for e in data["log"]] == list(range(1, len(data["log"]) + 1))


class TestCorpusMigration:
    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        source = tmp_path / "tasks"
        source.mkdir()
        path = source / "task-900-example.yaml"
        path.write_text(yaml.safe_dump(v1_task()), encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        report = migrate_corpus([path], write=False)

        assert report.written is False
        assert path.read_text(encoding="utf-8") == before

    def test_a_single_failure_aborts_the_whole_write(self, tmp_path: Path) -> None:
        # A corpus half in v1 and half in v2 is worse than one entirely in v1.
        source = tmp_path / "tasks"
        source.mkdir()
        good = source / "good.yaml"
        good.write_text(yaml.safe_dump(v1_task()), encoding="utf-8")
        bad = source / "bad.yaml"
        bad.write_text(yaml.safe_dump(v1_task(id="task-901", mystery="x")), encoding="utf-8")

        report = migrate_corpus([good, bad], write=True)

        assert report.failures
        assert report.written is False
        assert "schema: 2" not in good.read_text(encoding="utf-8")

    def test_writing_to_an_output_dir_leaves_the_source_alone(self, tmp_path: Path) -> None:
        source = tmp_path / "tasks"
        source.mkdir()
        path = source / "task-900-example.yaml"
        path.write_text(yaml.safe_dump(v1_task()), encoding="utf-8")
        out = tmp_path / "converted"

        report = migrate_corpus([path], output_dir=out, write=True)

        assert report.written is True
        assert "schema: 2" not in path.read_text(encoding="utf-8")
        assert "schema: 2" in (out / "task-900-example.yaml").read_text(encoding="utf-8")


class TestTheRealCorpus:
    """The migration that actually matters, run as a dry run on every real file."""

    def _corpus_files(self) -> list:
        return sorted((CORPUS / "agentjobs").glob("*.yaml")) + sorted(
            (CORPUS / "test-data").glob("*.yaml")
        )

    def test_every_real_task_converts_loads_and_loses_nothing(self) -> None:
        failures = []
        for path in self._corpus_files():
            v1 = yaml.safe_load(path.read_text(encoding="utf-8"))
            if v1.get("schema"):
                continue  # already migrated; this test is about the v1 path
            conversion = convert_task(v1, source=str(path))
            losses = verify_no_loss(v1, conversion.data)
            if losses:
                failures.append(f"{path.name}: {losses[0]}")
            round_tripped = yaml.safe_load(
                yaml.safe_dump(
                    yaml.safe_load(yaml.safe_dump(conversion.data, default_flow_style=False))
                )
            )
            try:
                load_task(round_tripped, source=str(path))
            except Exception as exc:
                failures.append(f"{path.name}: does not load as v2 -- {exc}")

        assert not failures, failures
