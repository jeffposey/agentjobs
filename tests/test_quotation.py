"""The paraphrase rule and the three tiers that enforce it (task-376).

**Every fixture here is invented.** The rule exists because this repository has a
public remote and its records had begun reproducing real people's words; a test suite
that quoted the very remarks the project redacted would put them back, in a file that
is read more often than the records are. So the sentences below are written for this
file, they are nobody's, and none of them is drawn from anything that was removed.

Four things are worth pinning, and they are the four ways this could quietly stop
working rather than fail:

*   ``TestTwoSignals`` -- a quotation shape alone is not a finding and a tone word
    alone is not a finding. This corpus holds thousands of quoted technical phrases,
    and a detector that fired on the shape would be turned off within a day.
*   ``TestWhatItCannotSee`` -- the documented blind spots, asserted rather than
    described, so a later change that closes one is noticed and a later change that
    opens a new one is not mistaken for this design.
*   ``TestRedactionVerb`` -- redaction preserves substance, records itself, reaches the
    append-only log, and refuses an address it does not understand.
*   ``TestImportRefuses`` -- the import boundary refuses rather than quarantines, and
    writes nothing when it does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from typer.testing import CliRunner

from agentjobs.cli import app
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle, LogEntryType, Task
from agentjobs.quotation import (
    DISMISSAL,
    EMPHASIS,
    INFORMALITY,
    TASK_PROSE_FIELDS,
    TONE_GROUPS,
    VULGARITY,
    field_text,
    scan_task,
    scan_text,
    speakers_in,
)
from agentjobs.receipts import content_hash
from agentjobs.record_check import QUOTED_REMARK, check_record
from agentjobs.storage import TaskStorage

runner = CliRunner()

#: A synthetic verbatim quotation of a person, attributed by a reporting clause. The
#: shape the rule is about: somebody's chat message reproduced word for word, tone and
#: all, to make a point that a paraphrase would have made.
ATTRIBUTED_REMARK = (
    'The reviewer said "yeah this whole panel is garbage, honestly", so the layout is '
    "being reworked."
)

#: The same substance, written the way the rule asks for.
PARAPHRASE = "The reviewer rejected the panel's layout outright, so it is being reworked."


@pytest.fixture()
def manager(tmp_path: Path) -> TaskManager:
    """A manager over an empty corpus."""
    return TaskManager(TaskStorage(tmp_path / "tasks"))


def task_with(manager: TaskManager, **content: object) -> Task:
    """A ready task carrying whatever prose a case needs."""
    fields: dict = {
        "id": "task-001",
        "title": "Rework the panel",
        "description": "Do the thing.",
        "summary": "Short enough.",
        "lifecycle": Lifecycle.READY,
    }
    fields.update(content)
    return manager.create_task(**fields)


class TestTwoSignals:
    """Neither signal is a finding on its own; the intersection is."""

    def test_an_attributed_remark_with_informal_tone_is_found(self):
        found = scan_text(ATTRIBUTED_REMARK, field="spec.description")

        assert [remark.field for remark in found] == ["spec.description"]
        assert DISMISSAL in found[0].groups
        assert found[0].attributed

    def test_a_quoted_technical_phrase_is_not_a_finding(self):
        text = 'The route is `/api/tasks`, and the client said "404 Not Found".'

        assert scan_text(text, field="spec.description") == []

    def test_tone_outside_a_quotation_is_not_a_finding(self):
        text = "The reviewer thought the layout was garbage, honestly, so it was reworked."

        assert scan_text(text, field="spec.description") == []

    def test_an_unattributed_quotation_with_ordinary_tone_words_is_not_a_finding(self):
        text = 'The design calls the current band ordering "nonsense", and it is fixed here.'

        assert scan_text(text, field="spec.description") == []

    def test_vulgarity_needs_no_attribution_cue(self):
        text = 'The heading still reads "this is a load of crap" in the fixture.'

        found = scan_text(text, field="spec.description")

        assert [remark.groups for remark in found] == [(VULGARITY,)]
        assert not found[0].attributed

    def test_a_name_from_the_record_attributes_a_quotation(self, manager):
        """No reporting verb at all -- the speaker list comes from the record's own log."""
        task = task_with(manager)
        manager.add_log_entry(task.id, actor="Ada Lovelace", type=LogEntryType.NOTE, body="Seen.")
        task = manager.add_log_entry(
            task.id,
            actor="bot",
            type=LogEntryType.PROGRESS,
            body='Ada wanted it "on the left, or the top, whatever".',
        )

        found = scan_task(task, fields=[f"log[{task.log[-1].id}].body"])

        assert [remark.groups for remark in found] == [(INFORMALITY,)]

    def test_a_block_quote_is_one_finding_not_three(self):
        text = (
            "They wrote:\n\n> honestly this is\n> not going to work,\n> seriously\n\nSo it changed."
        )

        found = scan_text(text, field="log[3].body")

        assert len(found) == 1
        assert found[0].groups == (INFORMALITY,)

    def test_stacked_punctuation_is_emphasis(self):
        text = 'They asked "so this answers everything at once?!" before approving.'

        found = scan_text(text, field="spec.description")

        assert found[0].groups == (EMPHASIS,)

    def test_every_group_it_can_report_is_declared(self):
        assert set(TONE_GROUPS) == {VULGARITY, DISMISSAL, INFORMALITY, EMPHASIS}


class TestWhatItCannotSee:
    """The documented blind spots, asserted so they are a decision and not a surprise."""

    def test_a_calm_verbatim_quotation_passes(self):
        """No listed word, so no finding -- the lexicons are a floor, not a definition."""
        text = 'They said "the ordering should follow the queue, not the timestamps".'

        assert scan_text(text, field="spec.description") == []

    def test_a_paraphrase_that_keeps_the_tone_passes(self):
        """Rule 2 binds unquoted prose too, and nothing mechanical checks that."""
        text = "They thought the whole panel was garbage and said so, honestly."

        assert scan_text(text, field="spec.description") == []

    def test_a_two_word_shout_passes(self):
        """Two capitals in a row is a heading or a status label far more often than a voice."""
        text = 'They replied "WAY BETTER" once the spacing landed.'

        assert scan_text(text, field="spec.description") == []

    def test_a_quoted_machine_message_after_a_reporting_verb_is_a_false_positive(self):
        """Named rather than fixed: the shape is indistinguishable from a person's words."""
        text = 'The runner said "FATAL: the job died, terrible exit" and stopped.'

        assert scan_text(text, field="log[2].body") != []


class TestFieldAddressing:
    def test_every_prose_field_is_readable_by_its_name(self, manager):
        task = task_with(
            manager,
            lifecycle=Lifecycle.DRAFT,
            spec={
                "summary": "S",
                "description": "D",
                "intent": "Why.",
                "constraints": "None.",
                "out_of_scope": "Nothing.",
            },
        )

        for field in TASK_PROSE_FIELDS:
            assert field_text(task, field) is not None, field

    def test_a_log_body_is_addressed_by_entry_id(self, manager):
        task = task_with(manager)
        task = manager.add_log_entry(
            task.id, actor="bot", type=LogEntryType.PROGRESS, body="Started."
        )

        assert field_text(task, f"log[{task.log[-1].id}].body") == "Started."

    def test_an_unknown_region_reads_as_absent(self, manager):
        task = task_with(manager)

        assert field_text(task, "spec.acceptance") is None
        assert field_text(task, "log[999].body") is None

    def test_speakers_come_from_the_record_itself(self, manager):
        task = task_with(manager)
        task = manager.add_log_entry(
            task.id, actor="Ada Lovelace", type=LogEntryType.NOTE, body="A note."
        )

        assert {"ada", "lovelace"} <= speakers_in(task)


class TestRecordCheckTier:
    """The author-time warning: scoped to what the write could have written."""

    def test_a_create_carrying_a_remark_in_the_spec_warns(self, manager):
        task = task_with(manager, description=ATTRIBUTED_REMARK)

        kinds = [warning.kind for warning in check_record(task, verb="create")]

        assert QUOTED_REMARK in kinds

    def test_a_claim_does_not_warn_about_a_spec_somebody_else_wrote(self, manager):
        task = task_with(manager, description=ATTRIBUTED_REMARK)
        task = manager.claim_task(task.id, agent="bot")

        kinds = [warning.kind for warning in check_record(task, verb="claim")]

        assert QUOTED_REMARK not in kinds

    def test_a_log_append_warns_about_the_entry_it_just_wrote(self, manager):
        task = task_with(manager)
        task = manager.add_log_entry(
            task.id, actor="bot", type=LogEntryType.PROGRESS, body=ATTRIBUTED_REMARK
        )

        kinds = [warning.kind for warning in check_record(task, verb="log_append")]

        assert QUOTED_REMARK in kinds

    def test_a_log_append_is_silent_about_an_older_entry(self, manager):
        task = task_with(manager)
        manager.add_log_entry(
            task.id, actor="bot", type=LogEntryType.PROGRESS, body=ATTRIBUTED_REMARK
        )
        task = manager.add_log_entry(
            task.id, actor="bot", type=LogEntryType.PROGRESS, body="Nothing quoted here."
        )

        kinds = [warning.kind for warning in check_record(task, verb="log_append")]

        assert QUOTED_REMARK not in kinds

    def test_the_message_says_what_to_do_instead(self, manager):
        task = task_with(manager, description=ATTRIBUTED_REMARK)

        message = next(
            warning.message
            for warning in check_record(task, verb="create")
            if warning.kind == QUOTED_REMARK
        )

        assert "what somebody meant" in message
        assert "spec.description" in message


class TestRedactionVerb:
    """The supported replacement for the hand-splice, and its three properties."""

    def test_a_spec_field_is_replaced_and_the_removal_recorded(self, manager):
        task = task_with(manager, description=ATTRIBUTED_REMARK)
        removed = len(task.spec.description)

        task = manager.redact(
            task.id,
            field="spec.description",
            replacement=PARAPHRASE,
            reason="verbatim quotation of a person",
            actor="claude",
        )

        assert task.spec.description == PARAPHRASE
        entry = task.log[-1]
        assert entry.actor == "claude"
        assert entry.data["redaction"] == {
            "field": "spec.description",
            "reason": "verbatim quotation of a person",
            "removed_chars": removed,
        }

    def test_the_record_of_the_redaction_holds_none_of_what_was_removed(self, manager):
        task = task_with(manager, description=ATTRIBUTED_REMARK)

        task = manager.redact(
            task.id,
            field="spec.description",
            replacement=PARAPHRASE,
            reason="verbatim quotation of a person",
            actor="claude",
        )

        assert "garbage" not in (task.log[-1].body or "")
        assert scan_task(task) == []

    def test_it_reaches_a_log_entry_which_nothing_else_can(self, manager):
        task = task_with(manager)
        task = manager.add_log_entry(
            task.id, actor="bot", type=LogEntryType.PROGRESS, body=ATTRIBUTED_REMARK
        )
        entry_id = task.log[-1].id

        task = manager.redact(
            task.id,
            field=f"log[{entry_id}].body",
            replacement=PARAPHRASE,
            reason="verbatim quotation of a person",
            actor="claude",
        )

        assert next(entry for entry in task.log if entry.id == entry_id).body == PARAPHRASE
        assert scan_task(task) == []

    def test_the_file_is_left_canonical(self, manager, tmp_path):
        """The hand-splice's other cost: a file AgentJobs would have written differently."""
        task = task_with(manager, description=ATTRIBUTED_REMARK)
        task = manager.redact(
            task.id,
            field="spec.description",
            replacement=PARAPHRASE,
            reason="verbatim quotation of a person",
            actor="claude",
        )

        storage = manager.storage
        on_disk = storage.task_path(task.id).read_bytes()
        expected = storage.canonical_bytes(storage.load_task_uncached(task.id))

        # Hashed rather than compared byte for byte, for `receipts.content_hash`'s
        # reason: git may hand a file back with CRLF and the writer emits LF, and this
        # test is about the shape of the document, not the platform's line endings.
        assert content_hash(on_disk) == content_hash(expected)

    def test_an_unaddressable_region_is_refused_rather_than_guessed_at(self, manager):
        task = task_with(manager)

        with pytest.raises(ValueError, match="not a redactable region"):
            manager.redact(
                task.id,
                field="spec.acceptance",
                replacement="x",
                reason="r",
                actor="claude",
            )

    def test_a_missing_log_entry_is_refused(self, manager):
        task = task_with(manager)

        with pytest.raises(ValueError, match="no region"):
            manager.redact(
                task.id,
                field="log[999].body",
                replacement="x",
                reason="r",
                actor="claude",
            )

    def test_a_redaction_must_state_a_reason(self, manager):
        task = task_with(manager)

        with pytest.raises(ValueError, match="reason"):
            manager.redact(
                task.id,
                field="title",
                replacement="x",
                reason="   ",
                actor="claude",
            )

    def test_an_operation_id_makes_a_retry_a_replay(self, manager):
        task = task_with(manager, description=ATTRIBUTED_REMARK)
        kwargs = dict(
            field="spec.description",
            replacement=PARAPHRASE,
            reason="verbatim quotation of a person",
            actor="claude",
            operation_id="11111111-2222-3333-4444-555555555555",
        )

        first = manager.redact(task.id, **kwargs)
        second = manager.redact(task.id, **kwargs)

        assert len(first.log) == len(second.log)


class TestTheCommands:
    """`agentjobs quotations` finds them; `agentjobs redact` is how they are fixed."""

    @pytest.fixture()
    def project(self, tmp_path: Path, monkeypatch) -> Path:
        """An initialised project directory, with the CLI's cwd pointed at it."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["init", "--backend", "files"],
            input="Test\ntasks\nprompts\n9000\njeff\n",
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        return tmp_path

    def _seed(self, project: Path, description: str) -> str:
        manager = TaskManager(TaskStorage(project / "tasks"))
        task = manager.create_task(
            id="task-001",
            title="Rework the panel",
            description=description,
            summary="Short enough.",
            lifecycle=Lifecycle.READY,
        )
        return task.id

    def test_a_clean_corpus_exits_zero_and_says_so(self, project):
        self._seed(project, "Nothing quoted here.")

        result = runner.invoke(app, ["quotations"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "no quoted remarks" in result.stdout

    def test_a_quoted_remark_is_listed_and_exits_one(self, project):
        self._seed(project, ATTRIBUTED_REMARK)

        result = runner.invoke(app, ["quotations"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "task-001" in result.stdout
        assert "spec.description" in result.stdout

    def test_redact_replaces_the_region_and_the_scan_goes_quiet(self, project):
        task_id = self._seed(project, ATTRIBUTED_REMARK)

        redacted = runner.invoke(
            app,
            [
                "redact",
                task_id,
                "--field",
                "spec.description",
                "--replacement",
                PARAPHRASE,
                "--reason",
                "verbatim quotation of a person",
            ],
            catch_exceptions=False,
        )

        assert redacted.exit_code == 0
        assert runner.invoke(app, ["quotations"], catch_exceptions=False).exit_code == 0

    def test_a_replacement_file_carries_multi_line_prose(self, project, tmp_path):
        task_id = self._seed(project, ATTRIBUTED_REMARK)
        source = tmp_path / "replacement.md"
        source.write_text(PARAPHRASE + "\n\nA second paragraph.\n", encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "redact",
                task_id,
                "--field",
                "spec.description",
                "--replacement-file",
                str(source),
                "--reason",
                "verbatim quotation of a person",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        task = TaskStorage(project / "tasks").load_task_uncached(task_id)
        assert "A second paragraph." in task.spec.description

    def test_both_replacement_forms_at_once_is_refused(self, project):
        task_id = self._seed(project, ATTRIBUTED_REMARK)

        result = runner.invoke(
            app,
            [
                "redact",
                task_id,
                "--field",
                "spec.description",
                "--replacement",
                "x",
                "--replacement-file",
                "y",
                "--reason",
                "r",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        assert "exactly one" in result.stdout

    def test_a_replacement_that_still_quotes_somebody_is_called_out(self, project):
        """The verb applies what it was given; it does not re-edit it, so it says so."""
        task_id = self._seed(project, ATTRIBUTED_REMARK)

        result = runner.invoke(
            app,
            [
                "redact",
                task_id,
                "--field",
                "spec.description",
                "--replacement",
                ATTRIBUTED_REMARK,
                "--reason",
                "verbatim quotation of a person",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "still in spec.description" in result.stdout
