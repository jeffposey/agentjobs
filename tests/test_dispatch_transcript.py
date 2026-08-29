"""The structured transcript reader: what it makes of a session's own JSONL.

These assert on the **rendered** values -- the summary sentence, the failure flag, the
diff counts -- rather than on the presence of a field, because the defect this replaces
was a panel that had all the right markup and displayed
``NewMCPserverfoundinthisproject:agentjobs``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agentjobs.dispatch.transcript import (
    KIND_NARRATION,
    KIND_PROMPT,
    KIND_TOOLS,
    SOURCE_JSONL,
    SOURCE_NONE,
    find_session_transcript,
    project_slug,
    read_structured_transcript,
    summarize,
)


# ----- building a transcript to read ------------------------------------------


def assistant(*blocks: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "assistant",
        "isSidechain": False,
        "message": {"role": "assistant", "content": list(blocks)},
    }


def user_text(text: str) -> Dict[str, Any]:
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": text}}


def tool_use(call_id: str, name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": payload}


def tool_result(
    call_id: str,
    content: Any = "ok",
    *,
    is_error: bool = False,
    result: Any = None,
) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "type": "user",
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": content,
                    "is_error": is_error,
                }
            ],
        },
    }
    if result is not None:
        event["toolUseResult"] = result
    return event


def write_transcript(path: Path, events: List[Dict[str, Any]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def transcript(tmp_path: Path) -> Path:
    return tmp_path / "session.jsonl"


# ----- the shape a reader gets ------------------------------------------------


class TestEntries:
    def test_narration_survives_as_sentences(self, transcript: Path) -> None:
        """The whole point: words and line breaks arrive intact.

        The repaint this replaces rendered "New MCP server found in this project" as one
        run-together string, because a TUI draws a space by moving the cursor.
        """
        write_transcript(
            transcript,
            [
                assistant(
                    {"type": "text", "text": "New MCP server found in this project:\nagentjobs"}
                )
            ],
        )
        result = read_structured_transcript(transcript)
        assert result.source == SOURCE_JSONL
        assert result.entries[0].kind == KIND_NARRATION
        assert result.entries[0].text == "New MCP server found in this project:\nagentjobs"

    def test_narration_tools_and_prompt_are_separate_entries(self, transcript: Path) -> None:
        write_transcript(
            transcript,
            [
                user_text("work task-023"),
                assistant(
                    {"type": "text", "text": "Reading the record."},
                    tool_use("t1", "Bash", {"command": "cat task.yaml", "description": "Read it"}),
                ),
                tool_result("t1"),
            ],
        )
        kinds = [entry.kind for entry in read_structured_transcript(transcript).entries]
        assert kinds == [KIND_PROMPT, KIND_NARRATION, KIND_TOOLS]

    def test_consecutive_calls_collapse_into_one_summarized_entry(self, transcript: Path) -> None:
        write_transcript(
            transcript,
            [
                assistant(
                    tool_use("t1", "Edit", {"file_path": "C:/x/DependencyState.tsx"}),
                    tool_use("t2", "Bash", {"command": "npm test"}),
                    tool_use("t3", "Bash", {"command": "npm run build"}),
                    tool_use("t4", "Bash", {"command": "git add -p"}),
                    tool_use("t5", "Bash", {"command": "git commit"}),
                ),
            ],
        )
        entries = read_structured_transcript(transcript).entries
        assert len(entries) == 1
        assert entries[0].summary == "Edited DependencyState.tsx, ran 4 commands"
        assert len(entries[0].calls) == 5

    def test_narration_closes_a_group(self, transcript: Path) -> None:
        """Two runs of calls separated by prose are two entries, not one."""
        write_transcript(
            transcript,
            [
                assistant(tool_use("t1", "Bash", {"command": "ls"})),
                assistant({"type": "text", "text": "Now the spec."}),
                assistant(tool_use("t2", "Bash", {"command": "pytest"})),
            ],
        )
        entries = read_structured_transcript(transcript).entries
        assert [entry.kind for entry in entries] == [KIND_TOOLS, KIND_NARRATION, KIND_TOOLS]

    def test_a_calls_detail_is_available_for_the_disclosure(self, transcript: Path) -> None:
        write_transcript(
            transcript,
            [
                assistant(
                    tool_use(
                        "t1",
                        "Bash",
                        {"command": "poetry run pytest -q", "description": "Run the suite"},
                    )
                ),
                tool_result("t1", "2 passed"),
            ],
        )
        call = read_structured_transcript(transcript).entries[0].calls[0]
        assert call.title == "Run the suite"
        assert call.detail == "poetry run pytest -q"
        assert call.output == "2 passed"

    def test_colour_codes_in_a_commands_output_come_off(self, transcript: Path) -> None:
        """A program's stdout carries real colour codes; the panel renders none of them.

        Observed on this feature's own dispatched run: a green pytest progress line
        reached the reader as ``\\x1b[32m.\\x1b[0m`` repeated forty times.
        """
        write_transcript(
            transcript,
            [
                assistant(tool_use("t1", "Bash", {"command": "pytest -q"})),
                tool_result("t1", "\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\n\x1b[32m2 passed\x1b[0m"),
            ],
        )
        call = read_structured_transcript(transcript).entries[0].calls[0]
        assert call.output == "..\n2 passed"


class TestFailure:
    def test_a_failed_call_is_flagged_on_the_call_and_counted_on_the_group(
        self, transcript: Path
    ) -> None:
        """A run that is quietly erroring must not look like one that is working."""
        write_transcript(
            transcript,
            [
                assistant(
                    tool_use("t1", "Bash", {"command": "pytest"}),
                    tool_use("t2", "Bash", {"command": "npm run build"}),
                ),
                tool_result("t1", "2 passed"),
                tool_result("t2", "error TS2345: argument of type 'x'", is_error=True),
            ],
        )
        entry = read_structured_transcript(transcript).entries[0]
        assert entry.failed == 1
        assert [call.failed for call in entry.calls] == [False, True]
        assert "TS2345" in entry.calls[1].output


class TestDiffCounts:
    def test_an_edits_patch_becomes_added_and_removed_lines(self, transcript: Path) -> None:
        patch = [
            {
                "lines": [
                    "   context",
                    "-  old one",
                    "+  new one",
                    "+  and another",
                ]
            }
        ]
        write_transcript(
            transcript,
            [
                assistant(tool_use("t1", "Edit", {"file_path": "/repo/App.tsx"})),
                tool_result("t1", "edited", result={"structuredPatch": patch}),
            ],
        )
        entry = read_structured_transcript(transcript).entries[0]
        assert (entry.added, entry.removed) == (2, 1)

    def test_a_created_file_counts_its_whole_content_as_additions(self, transcript: Path) -> None:
        write_transcript(
            transcript,
            [
                assistant(tool_use("t1", "Write", {"file_path": "/repo/new.ts"})),
                tool_result(
                    "t1",
                    "created",
                    result={"type": "create", "content": "one\ntwo\nthree"},
                ),
            ],
        )
        entry = read_structured_transcript(transcript).entries[0]
        assert (entry.added, entry.removed) == (3, 0)

    def test_an_unrecognised_result_reports_no_counts_rather_than_guessing(
        self, transcript: Path
    ) -> None:
        write_transcript(
            transcript,
            [
                assistant(tool_use("t1", "Edit", {"file_path": "/repo/App.tsx"})),
                tool_result("t1", "edited", result={"someFutureShape": 12}),
            ],
        )
        entry = read_structured_transcript(transcript).entries[0]
        assert (entry.added, entry.removed) == (0, 0)


class TestTolerance:
    """The format belongs to Claude Code. Unknown shapes are skipped, never raised."""

    def test_unknown_event_types_and_content_blocks_are_skipped(self, transcript: Path) -> None:
        write_transcript(
            transcript,
            [
                {"type": "some-future-event", "payload": {"anything": True}},
                assistant(
                    {"type": "thinking", "thinking": "", "signature": "…"},
                    {"type": "some_future_block", "value": 1},
                    {"type": "text", "text": "Still here."},
                ),
            ],
        )
        entries = read_structured_transcript(transcript).entries
        assert [entry.text for entry in entries] == ["Still here."]

    def test_an_unparseable_line_does_not_lose_the_readable_ones(self, transcript: Path) -> None:
        transcript.write_text(
            json.dumps(assistant({"type": "text", "text": "First."}))
            + "\n{ not json at all\n"
            + json.dumps(assistant({"type": "text", "text": "Second."}))
            + "\n",
            encoding="utf-8",
        )
        entries = read_structured_transcript(transcript).entries
        assert [entry.text for entry in entries] == ["First.", "Second."]

    def test_a_subagents_own_turns_are_left_out(self, transcript: Path) -> None:
        """A sidechain is a different conversation; interleaving it reads as nonsense."""
        sidechain = assistant({"type": "text", "text": "I am a subagent."})
        sidechain["isSidechain"] = True
        write_transcript(
            transcript,
            [assistant({"type": "text", "text": "Main chain."}), sidechain],
        )
        entries = read_structured_transcript(transcript).entries
        assert [entry.text for entry in entries] == ["Main chain."]

    def test_a_result_whose_call_fell_outside_the_window_is_ignored(self, transcript: Path) -> None:
        write_transcript(transcript, [tool_result("orphan", "output nobody asked for")])
        result = read_structured_transcript(transcript)
        assert result.source == SOURCE_NONE
        assert result.entries == []


class TestDegradation:
    """A session with no readable transcript degrades to a sentence, not an empty box."""

    def test_a_missing_path_says_so(self) -> None:
        result = read_structured_transcript(None)
        assert result.source == SOURCE_NONE
        assert "No structured transcript" in result.note

    def test_a_file_that_is_not_there_says_so(self, tmp_path: Path) -> None:
        result = read_structured_transcript(tmp_path / "gone.jsonl")
        assert result.source == SOURCE_NONE
        assert "could not be read" in result.note

    def test_a_transcript_with_no_turns_yet_says_so(self, transcript: Path) -> None:
        write_transcript(transcript, [{"type": "mode", "mode": "normal"}])
        result = read_structured_transcript(transcript)
        assert result.source == SOURCE_NONE
        assert "no assistant turns yet" in result.note

    def test_a_wholly_unparseable_file_counts_what_it_could_not_read(
        self, transcript: Path
    ) -> None:
        transcript.write_text("not json\nstill not json\n", encoding="utf-8")
        result = read_structured_transcript(transcript)
        assert result.source == SOURCE_NONE
        assert "2 of its lines could not be parsed" in result.note


class TestBounding:
    def test_only_the_last_entries_are_returned_and_the_rest_are_declared(
        self, transcript: Path
    ) -> None:
        write_transcript(
            transcript,
            [assistant({"type": "text", "text": f"step {index}"}) for index in range(10)],
        )
        result = read_structured_transcript(transcript, limit=3)
        assert [entry.text for entry in result.entries] == ["step 7", "step 8", "step 9"]
        assert result.total_entries == 10
        assert result.truncated is True

    def test_a_short_transcript_is_not_declared_truncated(self, transcript: Path) -> None:
        write_transcript(transcript, [assistant({"type": "text", "text": "only step"})])
        result = read_structured_transcript(transcript, limit=40)
        assert result.truncated is False

    def test_reading_only_the_tail_of_a_long_file_still_declares_truncation(
        self, transcript: Path
    ) -> None:
        """The window bounds a poll's cost; what it left off is stated, not hidden."""
        write_transcript(
            transcript,
            [assistant({"type": "text", "text": f"step {index}"}) for index in range(200)],
        )
        result = read_structured_transcript(transcript, limit=40, window=2_000)
        assert result.truncated is True
        assert result.total_entries < 200
        assert result.entries[-1].text == "step 199"


# ----- the summary sentence ---------------------------------------------------


class TestSummarize:
    def test_no_calls_summarize_to_nothing(self) -> None:
        assert summarize([]) == ""

    @pytest.mark.parametrize(
        ("calls", "expected"),
        [
            ([("Bash", "")], "Ran 1 command"),
            ([("Bash", ""), ("Bash", ""), ("Bash", "")], "Ran 3 commands"),
            ([("Write", "App.tsx")], "Edited App.tsx"),
            ([("Edit", "a.ts"), ("Edit", "b.ts")], "Edited a.ts and b.ts"),
            (
                [("Edit", "a.ts"), ("Edit", "b.ts"), ("Edit", "c.ts")],
                "Edited a.ts and 2 more files",
            ),
            ([("Edit", "a.ts"), ("Edit", "a.ts")], "Edited a.ts"),
            ([("Read", "x.py"), ("Read", "y.py")], "Read x.py and y.py"),
            ([("Grep", "needle")], "Ran 1 search"),
            ([("Grep", "a"), ("Glob", "b")], "Ran 2 searches"),
            ([("task_handoff", "")], "Called task_handoff"),
            ([("task_handoff", ""), ("task_close", "")], "Called 2 tools"),
            (
                [("Edit", "App.tsx"), ("Bash", ""), ("Bash", "")],
                "Edited App.tsx, ran 2 commands",
            ),
        ],
    )
    def test_a_run_of_calls_reads_as_a_sentence(self, calls, expected) -> None:
        from agentjobs.dispatch.transcript import TranscriptCall

        assert summarize([TranscriptCall(name=n, title=t) for n, t in calls]) == expected


# ----- finding the file -------------------------------------------------------


class TestLocating:
    def test_a_working_directory_becomes_claude_codes_store_name(self) -> None:
        assert project_slug(Path("C:/projects/agentjobs")) == "C--projects-agentjobs"

    def test_a_session_id_matches_its_transcript_by_prefix(self, tmp_path: Path) -> None:
        """The ledger records eight hex characters; the file is named for the full UUID."""
        store = tmp_path / ".claude" / "projects" / "C--projects-agentjobs"
        store.mkdir(parents=True)
        wanted = store / "553f321b-aefe-49a0-9a50-64886c6f19e1.jsonl"
        wanted.write_text("{}\n", encoding="utf-8")
        (store / "aaaaaaaa-0000-0000-0000-000000000000.jsonl").write_text("{}\n", encoding="utf-8")
        found = find_session_transcript("553f321b", Path("C:/projects/agentjobs"), home=tmp_path)
        assert found == wanted

    def test_a_session_that_relocated_into_a_worktree_is_still_found(self, tmp_path: Path) -> None:
        store = tmp_path / ".claude" / "projects" / "C--projects-agentjobs--worktrees-023"
        store.mkdir(parents=True)
        wanted = store / "553f321b-aefe-49a0-9a50-64886c6f19e1.jsonl"
        wanted.write_text("{}\n", encoding="utf-8")
        found = find_session_transcript("553f321b", Path("C:/projects/agentjobs"), home=tmp_path)
        assert found == wanted

    def test_an_unknown_session_finds_nothing_rather_than_the_wrong_file(
        self, tmp_path: Path
    ) -> None:
        store = tmp_path / ".claude" / "projects" / "C--projects-agentjobs"
        store.mkdir(parents=True)
        (store / "aaaaaaaa-0000-0000-0000-000000000000.jsonl").write_text("{}\n", encoding="utf-8")
        assert (
            find_session_transcript("bbbbbbbb", Path("C:/projects/agentjobs"), home=tmp_path)
            is None
        )

    def test_no_session_id_finds_nothing(self, tmp_path: Path) -> None:
        assert find_session_transcript("", Path("C:/x"), home=tmp_path) is None

    def test_a_home_with_no_store_finds_nothing(self, tmp_path: Path) -> None:
        assert find_session_transcript("553f321b", Path("C:/x"), home=tmp_path) is None
