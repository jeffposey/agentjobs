"""Getting a run's identity to the worker, when the environment is not the channel.

Task-249. ``--bg`` hands the session to a persistent daemon that spawns the worker from
the daemon's own environment, so everything ``DispatchRunner._environment`` sets is
dropped unless that launch was the one that started the daemon. On the machine where
this was found, 12 launches in 61 were. The other 49 sessions came up holding whichever
run's identity the running daemon had been started with -- once, fourteen hours stale
and against a different task, which cost that run the merge authority a human had
granted it.

The delivery moves the values into the ``--settings`` document, because argv is what the
daemon does deliver. **That flag was already carrying something**, and it is the most
consequential thing in the launch: ``posture_flags`` puts the run's permission envelope
there. So the largest group below is not about the identity at all -- it is about the
envelope surviving the merge untouched, in every shape argv can present it.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, cast

import pytest

from agentjobs.dispatch.runner import posture_flags, settings_json
from agentjobs.dispatch.config import Posture
from agentjobs.dispatch.session_env import (
    SESSION_SETTINGS_FILENAME,
    SETTINGS_FLAG,
    Delivery,
    daemon_was_started,
    deliver_identity,
    merged_document,
    redacted,
    session_environment,
    write_session_settings,
)

PROMPT = "You are the agent `claude` working task `task-249`."
SECRETS = {"AGENT_TOKEN": "sk-live-xyz"}


def argv_with_prompt() -> list[str]:
    return ["claude.CMD", "--bg", "--remote-control", "--model", "claude-opus-5", PROMPT]


def settings_of(argv: List[str]) -> Dict[str, Any]:
    """The document argv now carries, inline or by path -- whichever shape it took."""
    value = argv[argv.index(SETTINGS_FLAG) + 1]
    candidate = Path(value)
    if candidate.is_file():
        return cast(Dict[str, Any], json.loads(candidate.read_text(encoding="utf-8")))
    return cast(Dict[str, Any], json.loads(value))


class TestTheDocument:
    def test_it_carries_the_run_this_session_is_being_started_for(self, tmp_path: Path) -> None:
        environment = session_environment(run_id="run_abc123", run_dir=tmp_path)

        assert environment["AGENTJOBS_RUN_ID"] == "run_abc123"
        assert environment["AGENTJOBS_RUN_DIR"] == str(tmp_path)

    def test_a_runner_may_not_overwrite_the_identity_of_the_run_it_is_started_for(
        self, tmp_path: Path
    ) -> None:
        """The same precedence `_environment` applies, and for the same reason."""
        environment = session_environment(
            run_id="run_abc123",
            run_dir=tmp_path,
            runner_env={"AGENTJOBS_RUN_ID": "run_somebody_else", "TOKEN": "sk-live"},
        )

        assert environment["AGENTJOBS_RUN_ID"] == "run_abc123"
        assert environment["TOKEN"] == "sk-live"

    def test_merging_keeps_every_key_it_did_not_come_for(self) -> None:
        document = merged_document(
            {"AGENTJOBS_RUN_ID": "run_abc123"},
            {"permissions": {"allow": ["Bash(pytest:*)"]}, "enabledMcpjsonServers": ["agentjobs"]},
        )

        assert document["permissions"] == {"allow": ["Bash(pytest:*)"]}
        assert document["enabledMcpjsonServers"] == ["agentjobs"]
        assert cast(Dict[str, str], document["env"])["AGENTJOBS_RUN_ID"] == "run_abc123"

    def test_redaction_keeps_the_names_and_drops_the_values(self) -> None:
        """ "Which variables did this run get" is worth answering from the record. The
        values are the half that may be secret."""
        safe = redacted({"permissions": {"allow": ["Read"]}, "env": {"AGENT_TOKEN": "sk-live"}})

        assert safe["permissions"] == {"allow": ["Read"]}
        assert safe["env"] == {"AGENT_TOKEN": "<redacted>"}
        assert "sk-live" not in json.dumps(safe)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_a_written_document_is_not_world_readable(self, tmp_path: Path) -> None:
        path = write_session_settings(tmp_path, {"TOKEN": "sk-live"})

        assert not stat.S_IMODE(path.stat().st_mode) & 0o077


class TestThePermissionEnvelopeSurvives:
    """The flag being merged into is the one that says what the run may do.

    ``posture_flags`` puts ``permissions.allow`` and ``enabledMcpjsonServers`` in
    ``--settings``. ``--settings`` is not repeatable, so appending a second would
    silently win and drop the first -- which would mean a run launched with an envelope
    nobody chose. Every posture that emits one is checked here against the real
    ``posture_flags`` output rather than a hand-written stand-in.
    """

    @pytest.mark.parametrize("posture", [Posture.AUTO, Posture.SUPERVISED])
    def test_the_allow_list_is_unchanged_by_the_merge(
        self, posture: Posture, tmp_path: Path
    ) -> None:
        flags = posture_flags(posture, ["agentjobs"])
        expected = json.loads(flags[flags.index(SETTINGS_FLAG) + 1])

        delivered = deliver_identity(
            ["claude.CMD", "--bg", *flags, PROMPT],
            prompt=PROMPT,
            directory=tmp_path,
            run_id="run_abc123",
        )

        assert delivered.delivery is Delivery.DELIVERED
        document = settings_of(delivered.argv)
        assert document["permissions"] == expected["permissions"]
        assert document["enabledMcpjsonServers"] == expected["enabledMcpjsonServers"]

    def test_read_only_keeps_its_deliberately_absent_allow_list(self, tmp_path: Path) -> None:
        """`read_only` must not be given one. Merging must not conjure the key."""
        flags = posture_flags(Posture.READ_ONLY, ["agentjobs"])

        delivered = deliver_identity(
            ["claude.CMD", "--bg", *flags, PROMPT],
            prompt=PROMPT,
            directory=tmp_path,
            run_id="run_abc123",
        )

        assert "permissions" not in settings_of(delivered.argv)

    def test_exactly_one_settings_flag_reaches_the_launcher(self, tmp_path: Path) -> None:
        flags = posture_flags(Posture.AUTO, ["agentjobs"])

        delivered = deliver_identity(
            ["claude.CMD", "--bg", *flags, PROMPT],
            prompt=PROMPT,
            directory=tmp_path,
            run_id="run_abc123",
        )

        assert delivered.argv.count(SETTINGS_FLAG) == 1

    def test_a_document_that_cannot_be_read_is_left_strictly_alone(self, tmp_path: Path) -> None:
        """Replacing an envelope we could not parse would be a worse failure than the
        one being fixed. Say `conflict` and change nothing."""
        original = ["claude.CMD", "--bg", SETTINGS_FLAG, "{not json at all", PROMPT]

        delivered = deliver_identity(
            original, prompt=PROMPT, directory=tmp_path, run_id="run_abc123"
        )

        assert delivered.delivery is Delivery.CONFLICT
        assert delivered.argv == original


class TestSplicingIntoArgv:
    def test_the_flag_lands_before_the_prompt(self, tmp_path: Path) -> None:
        """Where `compose_argv` puts the posture flags: a CLI expects options before a
        positional argument, and the prompt is the positional."""
        delivered = deliver_identity(
            argv_with_prompt(), prompt=PROMPT, directory=tmp_path, run_id="run_abc123"
        )

        assert delivered.delivery is Delivery.DELIVERED
        assert delivered.argv.index(SETTINGS_FLAG) < delivered.argv.index(PROMPT)

    def test_an_argv_with_no_prompt_still_gets_the_flag(self, tmp_path: Path) -> None:
        """A wake's argv has had the prompt taken out of it by the time anything looks."""
        delivered = deliver_identity(
            ["claude.CMD", "--bg"], prompt="", directory=tmp_path, run_id="run_abc123"
        )

        assert delivered.delivery is Delivery.DELIVERED
        assert SETTINGS_FLAG in delivered.argv

    def test_the_prompt_itself_is_untouched(self, tmp_path: Path) -> None:
        """`wake_argv` finds the resume point by looking for the prompt inside an
        element. Splicing must not disturb that."""
        delivered = deliver_identity(
            argv_with_prompt(), prompt=PROMPT, directory=tmp_path, run_id="run_abc123"
        )

        assert delivered.argv.count(PROMPT) == 1

    def test_the_equals_form_is_recognised_rather_than_duplicated(self, tmp_path: Path) -> None:
        inline = json.dumps({"env": {"THEIRS": "kept"}})

        delivered = deliver_identity(
            ["claude.CMD", "--bg", f"{SETTINGS_FLAG}={inline}", PROMPT],
            prompt=PROMPT,
            directory=tmp_path,
            run_id="run_abc123",
        )

        assert delivered.delivery is Delivery.DELIVERED
        element = next(e for e in delivered.argv if e.startswith(f"{SETTINGS_FLAG}="))
        document = json.loads(element.split("=", 1)[1])
        assert document["env"]["THEIRS"] == "kept"
        assert document["env"]["AGENTJOBS_RUN_ID"] == "run_abc123"

    def test_an_operators_settings_file_is_read_and_merged(self, tmp_path: Path) -> None:
        theirs = tmp_path / "operator.json"
        theirs.write_text(
            json.dumps({"model": "claude-opus-5", "env": {"THEIRS": "kept"}}), encoding="utf-8"
        )
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        delivered = deliver_identity(
            ["claude.CMD", "--bg", SETTINGS_FLAG, str(theirs), PROMPT],
            prompt=PROMPT,
            directory=run_dir,
            run_id="run_abc123",
        )

        document = settings_of(delivered.argv)
        assert document["model"] == "claude-opus-5"
        assert document["env"] == {
            "THEIRS": "kept",
            "AGENTJOBS_RUN_ID": "run_abc123",
            "AGENTJOBS_RUN_DIR": str(run_dir),
        }


class TestWhereTheDocumentGoes:
    """Inline by default; a file only when a runner has secrets of its own."""

    def test_without_a_runner_env_it_stays_in_argv(self, tmp_path: Path) -> None:
        """`meta.yaml` records argv verbatim, and that is how a reader of a run record
        sees which permissions it was granted. The default case must not lose that."""
        delivered = deliver_identity(
            argv_with_prompt(), prompt=PROMPT, directory=tmp_path, run_id="run_abc123"
        )

        assert not (tmp_path / SESSION_SETTINGS_FILENAME).exists()
        assert delivered.document is None
        assert settings_of(delivered.argv)["env"]["AGENTJOBS_RUN_ID"] == "run_abc123"

    def test_a_runner_env_goes_to_a_file_and_never_into_argv(self, tmp_path: Path) -> None:
        """Those values are where operators are told to put secrets *because* argv is
        recorded verbatim, so they may not go back into it."""
        delivered = deliver_identity(
            argv_with_prompt(),
            prompt=PROMPT,
            directory=tmp_path,
            run_id="run_abc123",
            runner_env=SECRETS,
        )

        assert (tmp_path / SESSION_SETTINGS_FILENAME).is_file()
        assert "sk-live-xyz" not in json.dumps(delivered.argv)
        assert settings_of(delivered.argv)["env"]["AGENT_TOKEN"] == "sk-live-xyz"

    def test_the_record_still_gets_the_envelope_when_a_file_is_used(self, tmp_path: Path) -> None:
        """argv then names a path, so the permission envelope would stop being readable
        from the run record unless the redacted document is handed back for it."""
        flags = posture_flags(Posture.AUTO, ["agentjobs"])

        delivered = deliver_identity(
            ["claude.CMD", "--bg", *flags, PROMPT],
            prompt=PROMPT,
            directory=tmp_path,
            run_id="run_abc123",
            runner_env=SECRETS,
        )

        assert delivered.document is not None
        assert cast(Dict[str, Any], delivered.document["permissions"])["allow"]
        assert delivered.document["env"] == {
            "AGENT_TOKEN": "<redacted>",
            "AGENTJOBS_RUN_ID": "<redacted>",
            "AGENTJOBS_RUN_DIR": "<redacted>",
        }
        assert "sk-live-xyz" not in json.dumps(delivered.document)


class TestNothingHereMayStopARun:
    def test_an_unwritable_directory_does_not_raise(self, tmp_path: Path) -> None:
        """The same trade `record_phase` makes: a run that loses its measurement is a
        gap in a report, and a run that dies because it could not be measured is a lost
        hour of work."""
        blocker = tmp_path / "blocked"
        blocker.write_text("I am a file, not a directory", encoding="utf-8")

        delivered = deliver_identity(
            argv_with_prompt(),
            prompt=PROMPT,
            directory=blocker,
            run_id="run_abc123",
            runner_env=SECRETS,
        )

        assert delivered.delivery is Delivery.FAILED
        assert delivered.argv == argv_with_prompt()


class TestTheDaemonBanner:
    def test_a_launch_that_started_the_daemon_is_recognised(self) -> None:
        assert daemon_was_started("Starting background service…\nbackgrounded")

    def test_a_launch_that_joined_a_running_daemon_is_not(self) -> None:
        """The steady state, and the whole reason this module exists."""
        assert not daemon_was_started("backgrounded · 7a0cba89\n  claude agents")


def test_the_settings_json_helper_is_the_shape_this_module_merges_into() -> None:
    """A guard against the two drifting apart. If `settings_json` ever stops producing
    an object, the merge above would be operating on something else entirely."""
    assert isinstance(json.loads(settings_json(allow_list=True, mcp_servers=[])), dict)
