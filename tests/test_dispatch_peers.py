"""task-451: reading the live-session roster, and sending one message into it.

Every test here is about the same asymmetry. A hit makes a wake cheap; a miss makes it
ordinary. So the interesting cases are all the ways this says *no*, and each of them has
to be a returned miss rather than an exception -- because the caller's fallback is the
fork, which is a correct answer to all of them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from agentjobs.dispatch.peers import (
    DELIVERED,
    NOT_DELIVERED,
    SENDER_NAME,
    LiveSession,
    find_live_session,
    roster,
    send_peer_message,
)


def register(
    directory: Path,
    *,
    pid: int,
    session_id: str,
    name: str = "agentjobs/task-451/3db3bfde",
    status: str = "idle",
) -> Path:
    """One session registration, shaped like the ones Claude Code writes."""
    path = directory / f"{pid}.json"
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "sessionId": session_id,
                "jobId": session_id[:8],
                "cwd": "C:\\projects\\agentjobs",
                "kind": "bg",
                "entrypoint": "cli",
                "version": "2.1.276",
                "status": status,
                "name": name,
                "peerProtocol": 1,
                "peerFeatures": ["notify_idle", "artifact_yield"],
                "messagingSocketPath": f"\\\\.\\pipe\\LOCAL\\cc-msg-{pid:032x}",
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def sessions(tmp_path: Path) -> Path:
    directory = tmp_path / "sessions"
    directory.mkdir()
    return directory


class TestRoster:
    def test_a_registration_is_read_into_a_live_session(self, sessions: Path) -> None:
        register(sessions, pid=4242, session_id="fb911c1e-7d70-4749-ad46-5bebac28b8b7")
        (row,) = roster(sessions)
        assert row.pid == 4242
        assert row.session_id == "fb911c1e-7d70-4749-ad46-5bebac28b8b7"
        assert row.job_id == "fb911c1e"
        assert row.status == "idle"
        assert row.version == "2.1.276"

    def test_a_missing_directory_is_no_sessions_rather_than_an_error(self, tmp_path: Path) -> None:
        assert roster(tmp_path / "never-created") == []

    @pytest.mark.parametrize(
        "content", ["not json at all", "[]", '"a string"'], ids=["unparseable", "a-list", "a-str"]
    )
    def test_anything_that_is_not_a_mapping_is_dropped_rather_than_raised(
        self, sessions: Path, content: str
    ) -> None:
        """The file layout is undocumented and an unrelated CLI release may change it.

        Not understanding a row costs a fork, which is what happens today anyway. Raising
        would cost the dispatch.
        """
        (sessions / "999.json").write_text(content, encoding="utf-8")
        assert roster(sessions) == []

    @pytest.mark.parametrize(
        "content",
        ['{"sessionId": "abc"}', '{"pid": "not an int", "sessionId": "abc"}', '{"name": "a"}'],
        ids=["no-pid", "pid-not-an-int", "name-only"],
    )
    def test_a_missing_field_becomes_its_empty_value_rather_than_dropping_the_row(
        self, sessions: Path, content: str
    ) -> None:
        """Two callers want different fields off one file. See ``_row``.

        Requiring a session id here would make a name-only registration invisible to
        ``idle_sessions.live_session_names``, which is how a name gets treated as free and
        the session Claude Code starts ends up called something the record does not know.
        """
        (sessions / "999.json").write_text(content, encoding="utf-8")
        (row,) = roster(sessions)
        assert row.pid == 0 or isinstance(row.pid, int)

    def test_a_row_with_no_session_id_is_never_matched_by_a_lookup(self, sessions: Path) -> None:
        """The other half of that leniency: a wake still needs an id to match on."""
        (sessions / "999.json").write_text('{"pid": 3, "name": "a"}', encoding="utf-8")
        assert roster(sessions) != []
        assert find_live_session("aaaa1111", sessions_dir=sessions) is None

    def test_one_bad_row_does_not_hide_the_good_ones(self, sessions: Path) -> None:
        (sessions / "1.json").write_text("{{{", encoding="utf-8")
        register(sessions, pid=2, session_id="aaaaaaaa-0000-0000-0000-000000000000")
        assert [row.pid for row in roster(sessions)] == [2]


class TestFindLiveSession:
    FULL = "a046a3af-7c6c-419a-879c-9729b8e08326"

    def test_a_full_uuid_matches(self, sessions: Path) -> None:
        register(sessions, pid=1, session_id=self.FULL)
        found = find_live_session(self.FULL, sessions_dir=sessions)
        assert found is not None and found.pid == 1

    def test_the_short_id_a_run_records_matches_too(self, sessions: Path) -> None:
        """A run stores the 8-hex id the launcher printed; the roster stores the uuid."""
        register(sessions, pid=1, session_id=self.FULL)
        found = find_live_session("a046a3af", sessions_dir=sessions)
        assert found is not None and found.session_id == self.FULL

    def test_an_ambiguous_prefix_matches_nothing(self, sessions: Path) -> None:
        """Two conversations behind one prefix makes "which one" a guess, and the fork
        answers it correctly without guessing."""
        register(sessions, pid=1, session_id="a046a3af-1111-0000-0000-000000000000")
        register(sessions, pid=2, session_id="a046a3af-2222-0000-0000-000000000000")
        assert find_live_session("a046a3af", sessions_dir=sessions) is None

    def test_an_exact_match_beats_a_prefix(self, sessions: Path) -> None:
        register(sessions, pid=1, session_id="a046a3af")
        register(sessions, pid=2, session_id="a046a3af-2222-0000-0000-000000000000")
        found = find_live_session("a046a3af", sessions_dir=sessions)
        assert found is not None and found.pid == 1

    def test_a_stopped_session_is_simply_absent(self, sessions: Path) -> None:
        """The fallback trigger, in one line: `claude stop` removes both files within
        seconds, so this is a file lookup and never a connect timeout."""
        path = register(sessions, pid=1, session_id=self.FULL)
        path.unlink()
        assert find_live_session(self.FULL, sessions_dir=sessions) is None

    def test_an_empty_id_matches_nothing(self, sessions: Path) -> None:
        register(sessions, pid=1, session_id=self.FULL)
        assert find_live_session("", sessions_dir=sessions) is None
        assert find_live_session("   ", sessions_dir=sessions) is None


def live(name: str = "agentjobs/task-451/3db3bfde", status: str = "idle") -> LiveSession:
    return LiveSession(
        pid=4242,
        session_id="fb911c1e-7d70-4749-ad46-5bebac28b8b7",
        job_id="fb911c1e",
        name=name,
        status=status,
        cwd="C:\\projects\\agentjobs",
        version="2.1.276",
    )


class Recorder:
    """A stand-in for ``subprocess.run`` that answers however the test needs."""

    def __init__(self, stdout: str = DELIVERED, returncode: int = 0, raises: Any = None) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.raises = raises
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append({"argv": argv, **kwargs})
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


def send(target: LiveSession, message: str = "wake up", **kwargs: Any) -> Any:
    run = kwargs.pop("run", Recorder())
    return (
        send_peer_message(
            target, message, prefix=["claude"], cwd=Path("."), env={}, run=run, **kwargs
        ),
        run,
    )


class TestAddressing:
    def test_a_name_with_an_at_sign_is_refused_without_spawning_anything(self) -> None:
        """The finding the separator change rests on.

        ``SendMessage`` validates ``to`` before any lookup and rejects an ``@`` outright.
        Proven on Claude Code 2.1.276, 2026-09-18: ``agentjobs/task-999@aa451sbx``,
        ``agentjobs-task-999@aa451sbx``, ``agentjobs@task-999`` and ``task-999@aa451sbx``
        all draw *"to must be a bare teammate name"*, while ``agentjobs/task-999`` and
        ``agentjobs-task-999-aa451sbx`` reach the lookup and answer *"No agent named"*.
        So the character is the whole of it, and there is nothing to be gained by paying
        for a turn to be told so.
        """
        delivery, run = send(live(name="agentjobs/task-451@3db3bfde"))
        assert not delivery.delivered
        assert "@" in delivery.detail
        assert run.calls == []

    def test_an_unnamed_session_is_refused(self) -> None:
        delivery, run = send(live(name=""))
        assert not delivery.delivered
        assert run.calls == []

    def test_a_slash_separated_name_is_addressable(self) -> None:
        assert live().addressable


class TestSending:
    def test_the_sentinel_is_what_a_delivery_rests_on(self) -> None:
        delivery, _ = send(live())
        assert delivery.delivered
        assert delivery.session_id == "fb911c1e-7d70-4749-ad46-5bebac28b8b7"

    def test_the_target_name_and_the_message_both_reach_the_sender(self) -> None:
        _, run = send(live(), "the ball prompt, verbatim")
        instruction = run.calls[0]["input"]
        assert "agentjobs/task-451/3db3bfde" in instruction
        assert "the ball prompt, verbatim" in instruction

    def test_the_instruction_goes_on_stdin_and_never_in_argv(self) -> None:
        """The same trap `wake_argv` documents, and it fails the same silent way.

        Measured on Claude Code 2.1.276 through the Windows `claude.CMD` shim: this
        instruction as a positional argument is dropped and the turn comes up with no
        task at all -- exit 0, nothing on stderr, `"I'm ready. What would you like me to
        work on?"`. On stdin it is acted on every time.
        """
        _, run = send(live(), "the ball prompt, verbatim")
        argv = run.calls[0]["argv"]
        assert "the ball prompt, verbatim" not in " ".join(argv)
        assert argv[-1] == SENDER_NAME, "nothing positional after the flags"

    def test_the_sender_names_itself_so_the_receiver_records_who_woke_it(self) -> None:
        """``--name`` carries through to ``from-name`` on the receiver's own record.

        Without it the sender is named after its working directory -- task-449's first
        delivery arrived as ``tmp-76``, which says nothing about who sent it or why.
        """
        _, run = send(live())
        argv = run.calls[0]["argv"]
        assert argv[:2] == ["claude", "-p"]
        assert "--name" in argv and argv[argv.index("--name") + 1] == SENDER_NAME

    def test_the_sender_is_given_none_of_this_runs_authority(self) -> None:
        """A courier that calls one tool, not a second agent holding the run's permissions."""
        _, run = send(live())
        argv = run.calls[0]["argv"]
        assert "--settings" not in argv
        assert argv[argv.index("--permission-mode") + 1] == "auto"

    def test_a_held_message_is_a_miss_rather_than_a_delivery(self) -> None:
        """The one that matters most: a session that held the message was not woken.

        A ``bypassPermissions`` receiver without ``crossSessionInbound: accept`` holds a
        prompting-class sender's message indefinitely when no terminal is attached, and
        believing that was a wake would leave a supervisor waiting on nothing.
        """
        delivery, _ = send(live(), run=Recorder(stdout=f"{NOT_DELIVERED} it was held"))
        assert not delivery.delivered
        assert "held" in delivery.detail

    def test_prose_that_names_neither_outcome_is_a_miss(self) -> None:
        delivery, _ = send(live(), run=Recorder(stdout="I think that went fine!"))
        assert not delivery.delivered

    def test_the_last_sentinel_wins_so_a_quoted_payload_cannot_fake_one(self) -> None:
        """The payload is a wake prompt carrying a human's ball prompt -- arbitrary text.

        An echoed one appears before the sender's own verdict, so reading from the end is
        what keeps the claim the sender's rather than the payload's.
        """
        echoed = f"the message said {DELIVERED} somewhere\n{NOT_DELIVERED} refused"
        delivery, _ = send(live(), run=Recorder(stdout=echoed))
        assert not delivery.delivered

    def test_a_non_zero_exit_is_a_miss(self) -> None:
        delivery, _ = send(live(), run=Recorder(stdout="boom", returncode=1))
        assert not delivery.delivered
        assert "exited 1" in delivery.detail

    def test_a_timeout_is_a_miss(self) -> None:
        delivery, _ = send(
            live(), run=Recorder(raises=subprocess.TimeoutExpired(cmd="claude", timeout=180.0))
        )
        assert not delivery.delivered
        assert "did not return" in delivery.detail

    def test_a_launcher_that_will_not_start_is_a_miss(self) -> None:
        delivery, _ = send(live(), run=Recorder(raises=OSError("no such file")))
        assert not delivery.delivered
        assert "could not start" in delivery.detail

    def test_the_timeout_is_passed_to_the_subprocess(self) -> None:
        _, run = send(live(), timeout=12.5)
        assert run.calls[0]["timeout"] == 12.5


class TestLiveSessionShape:
    @pytest.mark.parametrize("status,expected", [("busy", True), ("idle", False), ("", False)])
    def test_busy_reads_the_status_the_roster_recorded(self, status: str, expected: bool) -> None:
        assert live(status=status).busy is expected

    def test_a_delivery_detail_names_the_session_a_reader_can_check(self) -> None:
        delivery, _ = send(live())
        assert "fb911c1e" in delivery.detail
        assert "4242" in delivery.detail


def test_nothing_here_reads_the_auth_token(sessions: Path) -> None:
    """The key file beside each registration is never opened.

    Task-449 proved the documented auth line authenticates a plain process to another
    session's inbox, and that the envelope after it is published nowhere. This module
    deliberately stops at the registration and lets ``SendMessage`` do the delivery, so a
    key file that is unreadable -- or absent -- changes nothing.
    """
    register(sessions, pid=7, session_id="aaaaaaaa-0000-0000-0000-000000000000")
    (sessions / "7.deadbeef.key").write_text("{ this is not json", encoding="utf-8")
    found: Optional[LiveSession] = find_live_session("aaaaaaaa", sessions_dir=sessions)
    assert found is not None and found.pid == 7
