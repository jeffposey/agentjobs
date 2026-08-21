"""Tests for recognising a session that died on an expired login.

The failure this covers is not hypothetical and not reconstructed: every transcript line
below is shaped like the three genuine ``authentication_failed`` records found on this
machine on 2026-08-20 and 2026-08-21 (task-224, log entry 6).

The test that matters most is the one that looks least like a test --
``test_a_transcript_that_merely_mentions_the_error_is_not_a_stall``. Any session that
reads the task record, this module or this file has the literal string
``authentication_failed`` in its own transcript, so a detector built on ``grep`` would
report every session investigating the bug as suffering from it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from agentjobs.dispatch.auth import (
    CLAUDE_HOME_ENV,
    claude_home,
    read_auth_stall,
    session_log_path,
)

SESSION = "61e30711"
FULL_SESSION = "61e30711-01b1-4d8f-9bf5-cc82eaf49b3a"
WHEN = datetime(2026, 8, 21, 15, 38, 21, 136000, tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def auth_failure_line(
    *, at: datetime = WHEN, session: str = FULL_SESSION, text: str = "Login expired"
) -> dict:
    """The real article, trimmed of the fields nothing reads.

    ``model: "<synthetic>"`` is the give-away that the message was generated locally
    rather than returned by the API, and it is what tells a dead turn from a live one.
    """
    return {
        "type": "assistant",
        "timestamp": _stamp(at),
        "message": {
            "model": "<synthetic>",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
        "error": "authentication_failed",
        "isApiErrorMessage": True,
        "sessionId": session,
        "sessionKind": "bg",
        "entrypoint": "cli",
    }


def real_reply_line(*, at: datetime, session: str = FULL_SESSION) -> dict:
    """A turn the model actually produced, which is the proof that auth came back."""
    return {
        "type": "assistant",
        "timestamp": _stamp(at),
        "message": {
            "model": "claude-opus-5",
            "role": "assistant",
            "content": [{"type": "text", "text": "Carrying on."}],
        },
        "sessionId": session,
    }


def user_line(*, at: datetime, text: str = "should be logged in now") -> dict:
    """A human nudging a dead session, which is not a recovery. Verified: the nudge sent
    at 15:43:38 failed nine milliseconds later, against the discarded credential."""
    return {
        "type": "user",
        "timestamp": _stamp(at),
        "message": {"role": "user", "content": text},
        "sessionId": FULL_SESSION,
    }


def write_transcript(
    home: Path, lines: List[dict], *, session: str = FULL_SESSION, project: str = "C--projects-x"
) -> Path:
    """Lay out a Claude home the way Claude Code does: one JSONL per session."""
    directory = home / "projects" / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.jsonl"
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


def stall_for(home: Path, session: str = SESSION, since: Optional[datetime] = None):
    return read_auth_stall(session, home=home, since=since)


# ----- finding the transcript -------------------------------------------------


class TestLocatingASessionTranscript:
    def test_the_short_ledger_id_finds_the_full_uuid_transcript(self, tmp_path: Path) -> None:
        """The ledger prints eight characters; the file is named for the whole UUID."""
        written = write_transcript(tmp_path, [auth_failure_line()])

        assert session_log_path(SESSION, home=tmp_path) == written

    def test_a_bg_job_that_names_its_own_transcript_is_believed_first(self, tmp_path: Path) -> None:
        """`jobs/<id>/state.json` carries an absolute path, which beats guessing."""
        elsewhere = write_transcript(tmp_path, [auth_failure_line()], project="C--somewhere-else")
        job = tmp_path / "jobs" / SESSION
        job.mkdir(parents=True)
        (job / "state.json").write_text(
            json.dumps({"sessionId": FULL_SESSION, "linkScanPath": str(elsewhere)}),
            encoding="utf-8",
        )

        assert session_log_path(SESSION, home=tmp_path) == elsewhere

    def test_a_runner_that_is_not_claude_code_simply_has_no_transcript(
        self, tmp_path: Path
    ) -> None:
        """The graceful no-op every non-Claude runner gets, without anyone naming them."""
        assert session_log_path("deadbeef", home=tmp_path) is None
        assert stall_for(tmp_path, "deadbeef") is None

    def test_a_file_that_is_not_named_for_a_uuid_is_not_a_session_transcript(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / "projects" / "C--projects-x"
        directory.mkdir(parents=True)
        (directory / f"{SESSION}-notes.jsonl").write_text("{}\n", encoding="utf-8")

        assert session_log_path(SESSION, home=tmp_path) is None

    def test_the_claude_home_comes_from_claude_codes_own_env_var(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv(CLAUDE_HOME_ENV, str(tmp_path / "elsewhere"))

        assert claude_home() == tmp_path / "elsewhere"
        assert claude_home(tmp_path) == tmp_path, "an explicit path still wins"


# ----- deciding whether a session is stalled ----------------------------------


class TestReadingAStall:
    def test_a_dead_session_is_reported_with_what_it_said_and_when(self, tmp_path: Path) -> None:
        path = write_transcript(tmp_path, [auth_failure_line(text="Login expired")])

        stall = stall_for(tmp_path)

        assert stall is not None
        assert stall.session_id == SESSION
        assert stall.at == WHEN
        assert stall.message == "Login expired"
        assert stall.log_path == path

    def test_a_transcript_that_merely_mentions_the_error_is_not_a_stall(
        self, tmp_path: Path
    ) -> None:
        """The false positive a substring search would produce, and the reason for JSON.

        This is what a session *reading about* the bug looks like: the string is in a
        tool result, not in a top-level `error` field.
        """
        write_transcript(
            tmp_path,
            [
                {
                    "type": "user",
                    "timestamp": _stamp(WHEN),
                    "sessionId": FULL_SESSION,
                    "message": {
                        "role": "user",
                        "content": 'the JSONL carries "error": "authentication_failed"',
                    },
                },
                real_reply_line(at=WHEN + timedelta(seconds=1)),
            ],
        )

        assert stall_for(tmp_path) is None

    def test_an_error_line_that_is_not_an_api_error_is_not_a_stall(self, tmp_path: Path) -> None:
        line = auth_failure_line()
        line["isApiErrorMessage"] = False
        write_transcript(tmp_path, [line])

        assert stall_for(tmp_path) is None

    def test_a_failure_recorded_against_another_session_is_ignored(self, tmp_path: Path) -> None:
        """Belt and braces on a prefix-matched filename: the line names its own session."""
        write_transcript(
            tmp_path,
            [auth_failure_line(session="99999999-0000-0000-0000-000000000000")],
        )

        assert stall_for(tmp_path) is None

    def test_a_session_that_recovered_is_not_stalled(self, tmp_path: Path) -> None:
        """The 2026-08-20 occurrence: it healed itself, and history is not a stall."""
        write_transcript(
            tmp_path,
            [
                auth_failure_line(),
                real_reply_line(at=WHEN + timedelta(minutes=2)),
            ],
        )

        assert stall_for(tmp_path) is None

    def test_a_human_nudge_after_the_failure_does_not_count_as_recovery(
        self, tmp_path: Path
    ) -> None:
        """Because it did not fix anything -- it failed 9ms after arriving."""
        write_transcript(
            tmp_path,
            [
                auth_failure_line(),
                user_line(at=WHEN + timedelta(minutes=5)),
            ],
        )

        stall = stall_for(tmp_path)

        assert stall is not None and stall.at == WHEN

    def test_a_second_stall_after_a_recovery_is_the_one_reported(self, tmp_path: Path) -> None:
        again = WHEN + timedelta(hours=8)
        write_transcript(
            tmp_path,
            [
                auth_failure_line(),
                real_reply_line(at=WHEN + timedelta(minutes=2)),
                auth_failure_line(at=again),
            ],
        )

        stall = stall_for(tmp_path)

        assert stall is not None and stall.at == again

    def test_a_failure_from_before_this_run_started_says_nothing_about_it(
        self, tmp_path: Path
    ) -> None:
        """A session id can be resumed, and a transcript outlives the run that wrote it."""
        write_transcript(tmp_path, [auth_failure_line()])

        assert stall_for(tmp_path, since=WHEN + timedelta(seconds=1)) is None
        assert stall_for(tmp_path, since=WHEN) is not None

    def test_an_empty_transcript_is_not_a_stall(self, tmp_path: Path) -> None:
        write_transcript(tmp_path, [])

        assert stall_for(tmp_path) is None

    def test_a_line_that_is_not_json_is_stepped_over_rather_than_fatal(
        self, tmp_path: Path
    ) -> None:
        path = write_transcript(tmp_path, [auth_failure_line()])
        path.write_text(
            "not json at all\n" + path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        assert stall_for(tmp_path) is not None


class TestReadingOnlyTheTail:
    def test_only_the_tail_is_read_so_an_old_failure_is_not_resurrected(
        self, tmp_path: Path
    ) -> None:
        """A failure buried under a megabyte of newer work is a session that carried on."""
        filler = [real_reply_line(at=WHEN + timedelta(seconds=i)) for i in range(1, 400)]
        write_transcript(tmp_path, [auth_failure_line(), *filler])

        assert read_auth_stall(SESSION, home=tmp_path, tail_bytes=4096) is None

    def test_the_partial_first_line_of_a_window_does_not_break_the_read(
        self, tmp_path: Path
    ) -> None:
        """A byte offset lands mid-record; half a JSON object must not stop the scan."""
        filler = [real_reply_line(at=WHEN - timedelta(seconds=i)) for i in range(400, 0, -1)]
        write_transcript(tmp_path, [*filler, auth_failure_line()])

        assert read_auth_stall(SESSION, home=tmp_path, tail_bytes=4096) is not None
