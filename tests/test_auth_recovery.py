"""Auth and quota recovery without a person: probe, then wake, then check (task-417).

Every fixture here is disposable. The credential is fake: the probe and the session CLI are
a Python script in ``tmp_path``, and the Claude home is a directory these tests write. Nothing
touches the machine's real authentication store, and nothing reads a credential value.

Scenarios map to task-417's acceptance criteria:

- a1 -- ``TestSelfHealingNeedsNobody``
- a2 -- ``TestADeadStore``
- a3 -- ``TestProbeClassification``
- a4 -- ``TestSharedIncidents``
- a5 -- ``TestALostAcknowledgement``
- a6 -- ``TestStopAndLaterWords``
- a8 -- ``TestUsageLimits``

The live-driver contract check is ``TestTheRealProbeContract``. It is opt-in, because it
spends a real model call.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from agentjobs.dispatch import auth_recovery
from agentjobs.dispatch.auth import CLAUDE_HOME_ENV, read_limit_stall
from agentjobs.dispatch.auth_probe import (
    PROBE_TOKEN,
    ProbeClass,
    ProbeRequest,
    ProbeResult,
    classify_probe,
    run_probe,
)
from agentjobs.dispatch.peers import SESSIONS_DIR_ENV
from agentjobs.dispatch.auth_recovery import (
    MARKER,
    ClaudeSessionNudger,
    IncidentBook,
    NudgeReceipt,
    Profile,
    Stall,
)
from agentjobs.dispatch.journal import journal, request_cancel
from agentjobs.dispatch.ledger import find_run
from agentjobs.dispatch.poller import poll_live_sessions
from agentjobs.dispatch.runner import DispatchRunner, RunDirectory, SessionPhase, runs_root
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType
from agentjobs.projects import ProjectRegistry
from support import task_store
from test_dispatch_auth import auth_failure_line, real_reply_line, write_transcript
from test_dispatch_runner import write_script

SHORT = "b55b35ad"
FULL = "b55b35ad-0000-4000-8000-000000000001"

REAL_SUCCESS = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "api_error_status": None,
    "result": PROBE_TOKEN,
    "num_turns": 1,
    "terminal_reason": "completed",
}
"""Trimmed from the live probe run on Claude Code 2.1.270, 2026-09-13 (task-417 entry 13)."""

SPEND_LIMIT_TEXT = (
    "You've hit your monthly spend limit - raise it at "
    "claude.ai/settings/usage?from=cc_cli_limit_message"
)
"""Verbatim from task-224 entry 47: six of these were once counted as healthy answers."""

LOGIN_EXPIRED = "Login expired · Please run /login"


def _success() -> Dict[str, Any]:
    return {"exit": 0, "stdout": json.dumps(REAL_SUCCESS)}


def _auth_failure() -> Dict[str, Any]:
    return {
        "exit": 1,
        "stdout": json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 401,
                "result": LOGIN_EXPIRED,
            }
        ),
    }


# ----- a3: what counts as ready ------------------------------------------------


class TestProbeClassification:
    def test_the_live_success_shape_is_ready(self) -> None:
        result = classify_probe(exit_code=0, stdout=json.dumps(REAL_SUCCESS))
        assert result.klass is ProbeClass.READY and result.ready

    def test_task_224_entry_47s_spend_limit_is_never_ready(self) -> None:
        """The regression: text output, exit 1, and not one word about a login."""
        result = classify_probe(exit_code=1, stdout=SPEND_LIMIT_TEXT)
        assert result.klass is ProbeClass.SPEND_LIMIT
        assert not result.ready

    def test_a_spend_limit_carrying_a_five_hour_reset_is_still_a_spend_limit(self) -> None:
        """Seen in this machine's transcripts: waiting for that reset would wait forever."""
        stdout = json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": SPEND_LIMIT_TEXT,
                "quotaLimits": {"rateLimitType": "five_hour", "resetsAt": 1787392800},
            }
        )
        assert classify_probe(exit_code=1, stdout=stdout).klass is ProbeClass.SPEND_LIMIT

    def test_a_session_limit_is_exhausted_with_its_structured_reset(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "You've hit your session limit · resets 3:30am (America/Chicago)",
                "quotaLimits": {"rateLimitType": "five_hour", "resetsAt": 1789201800},
            }
        )
        result = classify_probe(exit_code=1, stdout=stdout)
        assert result.klass is ProbeClass.USAGE_EXHAUSTED
        assert result.resets_at == datetime.fromtimestamp(1789201800, tz=timezone.utc)

    def test_the_other_failures_are_distinguished(self) -> None:
        assert (
            classify_probe(exit_code=1, stdout=_auth_failure()["stdout"]).klass
            is ProbeClass.AUTH_REJECTED
        )
        assert (
            classify_probe(exit_code=1, stdout="API Error: 429 rate_limit_error").klass
            is ProbeClass.RATE_LIMITED
        )
        assert classify_probe(exit_code=None, stdout="", timed_out=True).klass is ProbeClass.TIMEOUT
        assert classify_probe(exit_code=0, stdout="backgrounded").klass is ProbeClass.MALFORMED

    def test_the_live_not_logged_in_shape_is_an_auth_rejection(self) -> None:
        """Captured from Claude Code 2.1.270 on 2026-09-13 against an empty Claude home.
        ``subtype`` still says ``success``, so only ``is_error`` tells it from an answer."""
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": None,
                "terminal_reason": "api_error",
                "total_cost_usd": 0,
                "result": "Not logged in · Please run /login",
            }
        )
        result = classify_probe(exit_code=1, stdout=stdout)
        assert result.klass is ProbeClass.AUTH_REJECTED

    def test_an_answer_that_is_not_the_token_is_malformed_not_ready(self) -> None:
        near = dict(REAL_SUCCESS, result=f"Sure! {PROBE_TOKEN}")
        assert classify_probe(exit_code=0, stdout=json.dumps(near)).klass is ProbeClass.MALFORMED

    def test_exit_zero_is_not_enough(self) -> None:
        errored = dict(REAL_SUCCESS, is_error=True)
        assert not classify_probe(exit_code=0, stdout=json.dumps(errored)).ready
        assert not classify_probe(exit_code=1, stdout=json.dumps(REAL_SUCCESS)).ready

    def test_the_probe_argv_disables_tools_and_mcp_and_keeps_oauth(self, tmp_path: Path) -> None:
        argv = ProbeRequest(executable=["claude"], model="claude-opus-5", cwd=tmp_path).argv()
        assert argv[:2] == ["claude", "-p"]
        assert argv[argv.index("--model") + 1] == "claude-opus-5"
        assert argv[argv.index("--tools") + 1] == ""
        assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
        assert "--bare" not in argv, "--bare restricts auth to an API key"

    def test_a_probe_that_cannot_start_is_a_launch_failure(self, tmp_path: Path) -> None:
        request = ProbeRequest(executable=[str(tmp_path / "no-such-cli")], model=None, cwd=tmp_path)
        assert run_probe(request).klass is ProbeClass.LAUNCH_FAILED

    def test_a_stale_daemon_latch_plays_no_part(self, tmp_path: Path) -> None:
        """task-417 entry 6: ``auth_required`` outlived a successful login. Nothing reads it."""
        (tmp_path / "daemon-auth-status.json").write_text(
            json.dumps({"status": "auth_required"}), encoding="utf-8"
        )
        assert classify_probe(exit_code=0, stdout=json.dumps(REAL_SUCCESS)).ready


class TestReadingAQuotaRefusal:
    def _line(self, *, at: datetime, text: str, resets: Optional[int]) -> dict:
        line = auth_failure_line(at=at, session=FULL, text=text)
        line["error"] = "rate_limit"
        if resets is not None:
            line["quotaLimits"] = {
                "rateLimitType": "five_hour",
                "status": "rejected",
                "resetsAt": resets,
            }
        return line

    def test_a_session_limit_reports_its_reset(self, tmp_path: Path) -> None:
        at = datetime(2026, 9, 12, 3, 57, 41, tzinfo=timezone.utc)
        text = "You've hit your session limit · resets 3:30am (America/Chicago)"
        write_transcript(tmp_path, [self._line(at=at, text=text, resets=1789201800)], session=FULL)
        stall = read_limit_stall(SHORT, home=tmp_path)
        assert stall is not None
        assert stall.kind == "usage_limit"
        assert stall.resets_at == datetime.fromtimestamp(1789201800, tz=timezone.utc)

    def test_a_spend_limit_is_a_spend_limit_whatever_reset_it_carries(self, tmp_path: Path) -> None:
        at = datetime(2026, 8, 22, 7, 47, 3, tzinfo=timezone.utc)
        text = "You've hit your monthly spend limit · raise it at claude.ai/settings/usage"
        write_transcript(tmp_path, [self._line(at=at, text=text, resets=1787392800)], session=FULL)
        stall = read_limit_stall(SHORT, home=tmp_path)
        assert stall is not None and stall.kind == "spend_limit"

    def test_a_subagents_refusal_is_not_the_sessions(self, tmp_path: Path) -> None:
        at = datetime(2026, 9, 12, 3, 57, 41, tzinfo=timezone.utc)
        line = self._line(at=at, text="You've hit your session limit", resets=1789201800)
        line["isSidechain"] = True
        write_transcript(tmp_path, [line], session=FULL)
        assert read_limit_stall(SHORT, home=tmp_path) is None

    def test_a_real_reply_after_the_refusal_clears_it(self, tmp_path: Path) -> None:
        at = datetime(2026, 9, 12, 3, 57, 41, tzinfo=timezone.utc)
        lines = [
            self._line(at=at, text="You've hit your session limit", resets=1789201800),
            real_reply_line(at=at + timedelta(hours=1), session=FULL),
        ]
        write_transcript(tmp_path, lines, session=FULL)
        assert read_limit_stall(SHORT, home=tmp_path) is None


# ----- the nudge adapter ----------------------------------------------------------


class _FakeClaude:
    """A scripted ``subprocess.run`` for the nudger: a listing, stop and resume."""

    def __init__(
        self,
        *,
        pid_clears: bool = True,
        resume: str = "woke",
        send: str = "AGENTJOBS-WAKE-DELIVERED",
    ) -> None:
        self.rows: List[Dict[str, Any]] = [
            {"id": SHORT, "sessionId": FULL, "pid": 4242, "status": "idle"}
        ]
        self.calls: List[List[str]] = []
        self.pid_clears = pid_clears
        self.resume = resume
        self.send = send
        self.stdin: Optional[str] = None
        self.sent: Optional[str] = None

    def __call__(self, argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        arguments = argv[1:]
        self.calls.append(arguments)
        if arguments[:1] == ["agents"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.rows), "")
        if arguments[:1] == ["-p"]:
            # The peer sender (task-451). Only reached when the roster has a live row.
            self.sent = kwargs.get("input")
            return subprocess.CompletedProcess(argv, 0, self.send, "")
        if arguments[:1] == ["stop"]:
            if self.pid_clears:
                for row in self.rows:
                    if row["id"] == arguments[1]:
                        row["pid"] = None
            return subprocess.CompletedProcess(argv, 0, "stopped", "")
        if arguments[:2] == ["--bg", "--resume"]:
            self.stdin = kwargs.get("input")
            if self.resume == "copy":
                out = (
                    f"note: session {SHORT} is already running, so this started a copy as c0ffee12"
                )
                return subprocess.CompletedProcess(argv, 0, out, "")
            if self.resume == "mute":
                return subprocess.CompletedProcess(argv, 0, "", "")
            out = f"woke session {SHORT} with its saved options (--remote-control, --model)"
            return subprocess.CompletedProcess(argv, 0, out, "")
        raise AssertionError(f"unexpected call {arguments}")


def _nudger(fake: _FakeClaude, tmp_path: Path) -> ClaudeSessionNudger:
    ticks = iter(range(1000))
    return ClaudeSessionNudger(
        ["claude"],
        cwd=tmp_path,
        env={},
        quiesce_seconds=3,
        run=fake,
        sleep=lambda _: None,
        monotonic=lambda: float(next(ticks)),
    )


class TestTheNudgeAdapter:
    def test_it_stops_waits_for_no_pid_then_resumes_by_full_uuid_with_no_flags(
        self, tmp_path: Path
    ) -> None:
        fake = _FakeClaude()
        receipt = _nudger(fake, tmp_path).nudge(SHORT, "carry on")
        assert receipt.state == "applied" and receipt.session_id == SHORT
        resumes = [call for call in fake.calls if call[:1] == ["--bg"]]
        assert resumes == [["--bg", "--resume", FULL]], "saved options only: no other flag"
        assert fake.calls.index(["stop", SHORT]) < fake.calls.index(resumes[0])
        assert fake.stdin == "carry on"

    def test_a_session_whose_pid_never_clears_is_not_resumed(self, tmp_path: Path) -> None:
        """Entry 8: resuming 1.2s after stop, before the process exited, started a copy."""
        fake = _FakeClaude(pid_clears=False)
        receipt = _nudger(fake, tmp_path).nudge(SHORT, "carry on")
        assert receipt.state == "not_applied"
        assert not [call for call in fake.calls if call[:1] == ["--bg"]]

    def test_a_copy_is_not_a_wake_and_is_stopped(self, tmp_path: Path) -> None:
        fake = _FakeClaude(resume="copy")
        receipt = _nudger(fake, tmp_path).nudge(SHORT, "carry on")
        assert receipt.state == "not_applied"
        assert ["stop", "c0ffee12"] in fake.calls

    def test_a_busy_session_is_never_stopped_to_be_resumed(self, tmp_path: Path) -> None:
        fake = _FakeClaude()
        fake.rows[0]["status"] = "busy"
        receipt = _nudger(fake, tmp_path).nudge(SHORT, "carry on")
        assert receipt.state == "deferred"
        assert [call for call in fake.calls if call[:1] != ["agents"]] == []

    def test_an_unrecognised_answer_is_unknown_not_applied(self, tmp_path: Path) -> None:
        fake = _FakeClaude(resume="mute")
        assert _nudger(fake, tmp_path).nudge(SHORT, "carry on").state == "unknown"


class TestTheNudgeWakesInPlaceFirst:
    """task-451: a parked session is alive, and a live session can simply be messaged.

    The stop-and-resume below it is correct and stays. What it costs is the thing this
    avoids: it destroys a working process and its session id to deliver one sentence, and
    then the recovery has to find the run it was recovering under a new identity.
    """

    def _live(self, name: str = "agentjobs/task-417/f92ef995") -> None:
        directory = Path(os.environ[SESSIONS_DIR_ENV])
        (directory / "4242.json").write_text(
            json.dumps(
                {
                    "pid": 4242,
                    "sessionId": FULL,
                    "jobId": SHORT,
                    "status": "idle",
                    "name": name,
                    "version": "2.1.276",
                }
            ),
            encoding="utf-8",
        )

    def test_a_live_session_is_messaged_and_never_stopped(self, tmp_path: Path) -> None:
        self._live()
        fake = _FakeClaude()

        receipt = _nudger(fake, tmp_path).nudge(SHORT, "your login works again; carry on")

        assert receipt.state == "applied"
        assert receipt.session_id == SHORT, "the same session, so the incident still matches"
        assert "your login works again" in (fake.sent or "")
        assert not [call for call in fake.calls if call[:1] in (["stop"], ["--bg"])]

    def test_a_session_absent_from_the_roster_stops_and_resumes_as_before(
        self, tmp_path: Path
    ) -> None:
        """The process really has gone, so there is nothing to message. Nothing changed."""
        fake = _FakeClaude()

        receipt = _nudger(fake, tmp_path).nudge(SHORT, "carry on")

        assert receipt.state == "applied"
        assert fake.sent is None
        assert ["--bg", "--resume", FULL] in fake.calls

    def test_a_refused_delivery_falls_through_rather_than_failing_the_recovery(
        self, tmp_path: Path
    ) -> None:
        """A miss has spent nothing, so it must not be mistaken for an attempt."""
        self._live()
        fake = _FakeClaude(send="AGENTJOBS-WAKE-FAILED it was held")

        receipt = _nudger(fake, tmp_path).nudge(SHORT, "carry on")

        assert receipt.state == "applied"
        assert fake.sent is not None
        assert ["--bg", "--resume", FULL] in fake.calls

    def test_a_name_the_peer_channel_refuses_falls_through(self, tmp_path: Path) -> None:
        self._live(name="agentjobs/task-417@f92ef995")
        fake = _FakeClaude()

        assert _nudger(fake, tmp_path).nudge(SHORT, "carry on").state == "applied"
        assert fake.sent is None
        assert ["--bg", "--resume", FULL] in fake.calls

    def test_a_busy_session_is_still_deferred_before_any_of_this(self, tmp_path: Path) -> None:
        """The busy check stays in front: something else already woke it."""
        self._live()
        fake = _FakeClaude()
        fake.rows[0]["status"] = "busy"

        assert _nudger(fake, tmp_path).nudge(SHORT, "carry on").state == "deferred"
        assert fake.sent is None


# ----- a machine: a registered project, a fake CLI, a Claude home ---------------------


AUTH_FAKE_CLI = r"""
import json, sys, pathlib

sys.stdout.reconfigure(encoding="utf-8")
here = pathlib.Path(__file__).parent
ledger = here / "ledger.json"
argv = sys.argv[1:]

def rows():
    return json.loads(ledger.read_text()) if ledger.is_file() else []

def log(name, value):
    with (here / name).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value) + "\n")

if argv[:1] == ["agents"]:
    listed = rows() if "--all" in argv else [r for r in rows() if r.get("status") != "stopped"]
    print(json.dumps(listed))
    raise SystemExit(0)

if argv[:1] == ["logs"]:
    print("session output")
    raise SystemExit(0)

if argv[:1] == ["stop"]:
    log("calls.log", argv)
    updated = []
    for row in rows():
        if row["id"] == argv[1]:
            row.update({"pid": None, "status": "stopped", "state": "stopped"})
        updated.append(row)
    ledger.write_text(json.dumps(updated))
    print("stopped")
    raise SystemExit(0)

if argv[:1] == ["-p"]:
    log("probes.log", argv)
    script = here / "probe.json"
    plan = json.loads(script.read_text()) if script.is_file() else [{"exit": 1, "stdout": "no plan"}]
    step = plan[0]
    if len(plan) > 1:
        script.write_text(json.dumps(plan[1:]))
    sys.stdin.read()
    print(step["stdout"])
    raise SystemExit(step["exit"])

if argv[:2] == ["--bg", "--resume"]:
    message = sys.stdin.read()
    log("calls.log", argv)
    log("nudges.log", {"argv": argv, "stdin": message})
    updated = []
    for row in rows():
        if row.get("sessionId") == argv[2]:
            row.update({"pid": 5151, "status": "busy", "state": "working"})
        updated.append(row)
    ledger.write_text(json.dumps(updated))
    reply = here / "reply_on_wake.json"
    if reply.is_file():
        plan = json.loads(reply.read_text())
        with open(plan["transcript"], "a", encoding="utf-8") as handle:
            for line in plan["lines"]:
                handle.write(json.dumps(line) + "\n")
    print("woke session " + argv[2][:8] + " with its saved options (--model)")
    raise SystemExit(0)

ledger.write_text(json.dumps([{
    "id": "b55b35ad", "sessionId": "b55b35ad-0000-4000-8000-000000000001", "pid": 4242,
    "kind": "background", "status": "busy", "state": "working",
}]))
print("backgrounded · b55b35ad · aj-task")
"""


class Machine:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.root = tmp_path / "project"
        self.claude = tmp_path / "claude"
        (self.root / ".agentjobs").mkdir(parents=True)
        (self.root / "tasks").mkdir()
        self.home.mkdir()
        self.claude.mkdir()
        (self.root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "project_name": "Sandbox",
                    "tasks_directory": "tasks",
                    "actors": [
                        {"name": "Jeff Posey", "kind": "human"},
                        {"name": "claude", "kind": "agent"},
                    ],
                    "default_user": "Jeff Posey",
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "cli").mkdir()
        self.cli = write_script(tmp_path / "cli" / "fakecli.py", AUTH_FAKE_CLI)
        self.write_dispatch_yaml()
        monkeypatch.setenv("AGENTJOBS_HOME", str(self.home))
        monkeypatch.setenv(CLAUDE_HOME_ENV, str(self.claude))
        ProjectRegistry(home=self.home).add(self.root, project_id="sandbox")
        self.manager = TaskManager(task_store(self.root / "tasks", project_id="sandbox"))
        self.base = datetime.now(timezone.utc)
        # The poller's own recovery pass runs on the wall clock; these tests drive the
        # recovery tick themselves on a fake clock, so the poll only detects and parks.
        monkeypatch.setattr("agentjobs.dispatch.poller._recover_parked", lambda *a: [])

    def write_dispatch_yaml(self, *, enabled: bool = True) -> None:
        (self.home / "dispatch.yaml").write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "enabled": enabled,
                    "runners": {
                        "fake": {
                            "mode": "session",
                            "argv": [
                                sys.executable,
                                str(self.cli),
                                "--bg",
                                "--model",
                                "claude-opus-5",
                                "{prompt}",
                            ],
                        }
                    },
                    "projects": {
                        "sandbox": {
                            "enabled": True,
                            "runner": "fake",
                            "posture": "autonomous",
                            "require_clean_tree": False,
                        }
                    },
                    "limits": {"session_stale_seconds": 3600, "max_concurrent_runs": 5},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    # -- runs --------------------------------------------------------------------

    def start(self, *, session_short: str = SHORT, session_full: str = FULL) -> tuple[str, str]:
        from agentjobs.dispatch.config import assert_dispatch_permitted

        created = self.manager.create_task(
            title="Dispatchable",
            category="infrastructure",
            summary="A task to dispatch.",
            description="Do the thing.",
            lifecycle=Lifecycle.READY,
        )
        self.manager.claim_task(created.id, agent="claude")
        runner = DispatchRunner(
            manager=self.manager,
            resolution=assert_dispatch_permitted("sandbox", self.home),
            project_root=self.root,
            home=self.home,
        )
        handle = runner.start(self.task(created.id), actor="Jeff Posey", caused_by=1)
        if session_short != SHORT:
            meta_rows = self.rows()
            meta_rows.append(
                {
                    "id": session_short,
                    "sessionId": session_full,
                    "pid": 4343,
                    "status": "busy",
                    "state": "working",
                }
            )
            self.set_rows(meta_rows)
            RunDirectory(path=runs_root(self.home) / handle.run_id).update_meta(
                session_id=session_short
            )
        return handle.run_id, created.id

    def task(self, task_id: str) -> Any:
        task = self.manager.get_task(task_id)
        assert task is not None
        return task

    def meta(self, run_id: str) -> Dict[str, Any]:
        return RunDirectory(path=runs_root(self.home) / run_id).read_meta()

    def rows(self) -> List[Dict[str, Any]]:
        path = self.cli.parent / "ledger.json"
        return json.loads(path.read_text()) if path.is_file() else []

    def set_rows(self, rows: List[Dict[str, Any]]) -> None:
        (self.cli.parent / "ledger.json").write_text(json.dumps(rows), encoding="utf-8")

    def go_idle(self, short: str = SHORT) -> None:
        self.set_rows(
            [
                dict(row, status="idle", state="done") if row["id"] == short else row
                for row in self.rows()
            ]
        )

    def transcript(self, lines: List[dict], *, full: str = FULL) -> Path:
        return write_transcript(self.claude, lines, session=full)

    def die_on_login(self, *, offset: float = 1.0, full: str = FULL) -> datetime:
        at = datetime.now(timezone.utc) + timedelta(seconds=offset)
        self.transcript([auth_failure_line(at=at, session=full, text=LOGIN_EXPIRED)], full=full)
        return at

    def reply_on_wake(self, *, full: str = FULL, offset_seconds: float = 30.0) -> None:
        path = self.claude / "projects" / "C--projects-x" / f"{full}.jsonl"
        at = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
        (self.cli.parent / "reply_on_wake.json").write_text(
            json.dumps(
                {
                    "transcript": str(path),
                    "lines": [
                        {
                            "type": "user",
                            "timestamp": at.isoformat().replace("+00:00", "Z"),
                            "message": {"role": "user", "content": "resumed"},
                            "sessionId": full,
                        },
                        real_reply_line(at=at + timedelta(seconds=2), session=full),
                    ],
                }
            ),
            encoding="utf-8",
        )

    def plan_probes(self, steps: List[Dict[str, Any]]) -> None:
        (self.cli.parent / "probe.json").write_text(json.dumps(steps), encoding="utf-8")

    def lines(self, name: str) -> List[Any]:
        path = self.cli.parent / name
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    # -- driving -----------------------------------------------------------------

    def poll(self) -> Dict[str, Any]:
        return {result.run_id: result for result in poll_live_sessions(self.home)}

    def tick(self, seconds: float, **kwargs: Any) -> List[str]:
        moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        return auth_recovery.tick(
            self.home,
            managers={"sandbox": self.manager},
            clock=lambda: moment,
            **kwargs,
        )

    def book(self) -> IncidentBook:
        return IncidentBook(journal(self.home))

    def human_handoffs(self, task_id: str) -> List[Any]:
        task = self.manager.get_task(task_id)
        assert task is not None
        return [
            entry
            for entry in task.log
            if entry.type is LogEntryType.HANDOFF
            and entry.data.get("ball") in (Ball.HUMAN.value, Ball.EXTERNAL.value)
            and entry.actor == "dispatcher"
        ]


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Machine:
    return Machine(tmp_path, monkeypatch)


# ----- a1 ----------------------------------------------------------------------------


class TestSelfHealingNeedsNobody:
    """task-224's store that recovered in about two minutes, and task-410's buffered
    feedback without a Stop: both end with the same session working and no person asked."""

    def test_a_store_that_recovers_is_probed_and_the_session_resumed_in_place(
        self, machine: Machine
    ) -> None:
        run_id, task_id = machine.start()
        machine.die_on_login()
        machine.go_idle()
        machine.plan_probes([_auth_failure(), _auth_failure(), _success()])
        machine.reply_on_wake(offset_seconds=124)

        assert machine.poll()[run_id].phase is SessionPhase.AUTH_STALLED
        task = machine.task(task_id)
        assert task is not None and task.ball is Ball.AGENT, "no person paged for a 2-minute blip"

        machine.tick(0)  # immediate probe: still refused
        machine.tick(61)  # refused
        assert machine.lines("nudges.log") == [], "no work turn against a dead store"
        machine.tick(122)  # answers -> resume
        nudges = machine.lines("nudges.log")
        assert len(nudges) == 1
        assert nudges[0]["argv"] == ["--bg", "--resume", FULL], "no flags: saved options"
        assert f"task `{task_id}`" in nudges[0]["stdin"]
        assert "same session" in nudges[0]["stdin"]

        # The resume is not the recovery; the reply is.
        machine.tick(130)
        waiter = machine.book().waiters(machine.meta(run_id)["auth_incident"])[0]
        assert waiter.status == auth_recovery.RECOVERED

        assert machine.human_handoffs(task_id) == [], "zero human actions"
        assert machine.meta(run_id)["status"] == "running"
        assert find_run(machine.home, run_id).status == "running", "run not concluded"
        assert len(machine.lines("probes.log")) == 3

        # A later poll of the recovered session is an ordinary poll again.
        assert machine.poll()[run_id].phase is SessionPhase.RUNNING

    def test_the_stop_before_resume_is_not_mistaken_for_the_session_going_away(
        self, machine: Machine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """task-417 entry 8: a poll between `stop` and `--resume` concluded the run
        `interrupted` while its session carried on untracked."""
        run_id, task_id = machine.start()
        machine.die_on_login()
        machine.go_idle()
        machine.poll()
        observed: List[SessionPhase] = []

        class PollingMidNudge:
            def nudge(self, session_id: str, message: str) -> NudgeReceipt:
                machine.set_rows([])  # stopped: gone from the active listing
                observed.append(machine.poll()[run_id].phase)
                return NudgeReceipt("applied", f"woke session {session_id}", session_id)

        machine.tick(
            0,
            probe=lambda request: ProbeResult(ProbeClass.READY, "ok", 0),
            nudger_for=lambda runner: PollingMidNudge(),
        )
        assert observed == [SessionPhase.AUTH_STALLED]
        assert find_run(machine.home, run_id).status not in {"finished", "cancelled", "failed"}

    def test_feedback_sent_while_parked_rides_in_the_resume_and_is_not_sent_twice(
        self, machine: Machine
    ) -> None:
        """task-410 without the Stop: the buffered message is delivered with the wake."""
        run_id, task_id = machine.start()
        machine.die_on_login()
        machine.go_idle()
        machine.poll()
        machine.manager.handoff(
            task_id,
            actor="Jeff Posey",
            ball=Ball.AGENT,
            ball_reason=BallReason.REVISE,
            ball_prompt="Also cover the Codex driver in the docs.",
        )
        machine.plan_probes([_success()])
        machine.reply_on_wake()
        machine.tick(0)

        nudges = machine.lines("nudges.log")
        assert len(nudges) == 1
        assert "Also cover the Codex driver in the docs." in nudges[0]["stdin"]
        feedback = max(
            entry.id for entry in machine.task(task_id).log if entry.type is LogEntryType.HANDOFF
        )
        assert machine.meta(run_id)["delivered_through_entry"] == feedback

        from agentjobs.dispatch.handback import pending_handback

        task = machine.task(task_id)
        config = ProjectRegistry(home=machine.home).get("sandbox").load_config()
        assert pending_handback(task, config, after_entry=feedback) is None

    def test_the_policy_clause_the_execution_was_granted_is_delivered(
        self, machine: Machine
    ) -> None:
        run_id, task_id = machine.start()
        clause = "Posture `autonomous` releases the merge gate: this run merges its own work."
        store = journal(machine.home)
        store.admit(
            project_id="sandbox",
            task_id=task_id,
            run_id=run_id,
            capacity=5,
            mode="session",
            envelope={"policy_clause": clause, "posture": "autonomous"},
            workflow_version=2,
            operation_id="admission-for-the-test",
        )
        machine.die_on_login()
        machine.go_idle()
        machine.poll()
        machine.plan_probes([_success()])
        machine.tick(0)
        assert clause in machine.lines("nudges.log")[0]["stdin"]


# ----- a2 ----------------------------------------------------------------------------


class TestADeadStore:
    def test_one_notification_naming_the_login_then_recovery_needs_no_answer_or_dispatch(
        self, machine: Machine
    ) -> None:
        run_id, task_id = machine.start()
        machine.die_on_login()
        machine.go_idle()
        machine.poll()
        machine.plan_probes([_auth_failure()])

        for seconds in (0, 60, 120, 180, 240):
            machine.tick(seconds)
        assert machine.human_handoffs(task_id) == [], "not before the five-minute deadline"

        for seconds in (301, 361, 421):
            machine.tick(seconds)
        handoffs = machine.human_handoffs(task_id)
        assert len(handoffs) == 1, "one actionable incident, not a page per probe"
        task = machine.task(task_id)
        assert task.ball is Ball.HUMAN and task.ball_reason is BallReason.INPUT
        assert "claude auth login" in (task.ball_prompt or "")
        assert "auth_unavailable" in (task.ball_prompt or "")
        assert handoffs[0].data[MARKER]["action"] == "notify"
        dispatches_before = [e for e in task.log if e.type is LogEntryType.DISPATCH]

        # The person logs in. Nothing else: the next probe answers.
        machine.plan_probes([_success()])
        machine.reply_on_wake(offset_seconds=483)
        machine.tick(481)
        assert len(machine.lines("nudges.log")) == 1
        machine.tick(490)

        task = machine.task(task_id)
        assert task.ball is Ball.AGENT and task.ball_reason is BallReason.WORK, "cleared"
        assert [e for e in task.log if e.type is LogEntryType.DISPATCH] == dispatches_before
        assert len(machine.human_handoffs(task_id)) == 1
        assert machine.book().incident(machine.meta(run_id)["auth_incident"]) is not None

    def test_probes_are_capped_per_hour_and_never_overlap(self, machine: Machine) -> None:
        machine.start()
        book = machine.book()
        profile = Profile("claude", ("claude",), "claude-opus-5", str(machine.claude))
        now = datetime.now(timezone.utc)
        joined = book.join(
            kind="auth",
            profile=profile,
            run_id="run_x",
            project_id="sandbox",
            task_id="task-x",
            session_id="deadbeef",
            stall=Stall("auth", now, LOGIN_EXPIRED),
            now=now,
        )
        first = book.claim_probe(joined.incident, now=now, timeout=30)
        assert first is not None
        assert book.claim_probe(joined.incident, now=now, timeout=30) is None, "overlap"
        book.finish_probe(
            first,
            joined.incident.incident_id,
            ProbeResult(ProbeClass.AUTH_REJECTED, "no"),
            now=now,
            next_probe_at=now,
        )
        for minute in range(1, auth_recovery.PROBE_HOURLY_CAP):
            moment = now + timedelta(seconds=minute * 50)
            incident = book.incident(joined.incident.incident_id)
            assert incident is not None
            probe_id = book.claim_probe(incident, now=moment, timeout=30)
            assert probe_id is not None, minute
            book.finish_probe(
                probe_id,
                incident.incident_id,
                ProbeResult(ProbeClass.AUTH_REJECTED, "no"),
                now=moment,
                next_probe_at=moment,
            )
        capped = book.incident(joined.incident.incident_id)
        assert capped is not None
        late = now + timedelta(seconds=auth_recovery.PROBE_HOURLY_CAP * 50)
        assert book.claim_probe(capped, now=late, timeout=30) is None
        pushed = book.incident(joined.incident.incident_id)
        assert pushed is not None and pushed.next_probe_at >= now + timedelta(hours=1)


# ----- a4 ----------------------------------------------------------------------------


OTHER_SHORT = "c0ffee12"
OTHER_FULL = "c0ffee12-0000-4000-8000-000000000002"


class TestSharedIncidents:
    def test_compatible_runs_share_one_probe_and_one_notification(self, machine: Machine) -> None:
        first_run, first_task = machine.start()
        second_run, second_task = machine.start(session_short=OTHER_SHORT, session_full=OTHER_FULL)
        machine.die_on_login()
        machine.die_on_login(full=OTHER_FULL)
        machine.go_idle()
        machine.go_idle(OTHER_SHORT)
        machine.poll()
        assert machine.meta(first_run)["auth_incident"] == machine.meta(second_run)["auth_incident"]

        calls: List[ProbeRequest] = []

        def refused(request: ProbeRequest) -> ProbeResult:
            calls.append(request)
            return ProbeResult(ProbeClass.AUTH_REJECTED, LOGIN_EXPIRED, 1)

        for seconds in (0, 60, 120, 180, 240, 301):
            machine.tick(seconds, probe=refused)
        assert len(calls) == 6, "one probe per due tick for both runs, not one each"

        incident = machine.book().incident(machine.meta(first_run)["auth_incident"])
        assert incident is not None and incident.notified_at is not None
        first = machine.human_handoffs(first_task)
        second = machine.human_handoffs(second_task)
        assert len(first) == len(second) == 1
        assert first[0].data[MARKER]["incident"] == second[0].data[MARKER]["incident"]
        assert f"sandbox/{second_task}" in (first[0].body or "")

    def test_an_incompatible_profile_does_not_share_readiness(self, machine: Machine) -> None:
        first_run, _ = machine.start()
        second_run, _ = machine.start(session_short=OTHER_SHORT, session_full=OTHER_FULL)
        other_argv = [sys.executable, str(machine.cli), "--bg", "--model", "claude-haiku-4-5"]
        RunDirectory(path=runs_root(machine.home) / second_run).update_meta(argv=other_argv)
        machine.die_on_login()
        machine.die_on_login(full=OTHER_FULL)
        machine.go_idle()
        machine.go_idle(OTHER_SHORT)
        machine.poll()
        assert machine.meta(first_run)["auth_incident"] != machine.meta(second_run)["auth_incident"]

        nudged: List[str] = []

        class Recording:
            def nudge(self, session_id: str, message: str) -> NudgeReceipt:
                nudged.append(session_id)
                return NudgeReceipt("applied", "woke", session_id)

        def only_opus(request: ProbeRequest) -> ProbeResult:
            if request.model == "claude-opus-5":
                return ProbeResult(ProbeClass.READY, "ok", 0)
            return ProbeResult(ProbeClass.AUTH_REJECTED, LOGIN_EXPIRED, 1)

        machine.tick(0, probe=only_opus, nudger_for=lambda runner: Recording())
        assert nudged == [SHORT], "the haiku profile's refusal was not overridden"


# ----- a5 ----------------------------------------------------------------------------


class _Crash(BaseException):
    """The process dying between the nudge's effect and its recorded result."""


class TestALostAcknowledgement:
    def _crash_mid_nudge(self, machine: Machine, *, deliver: bool) -> tuple[str, str]:
        run_id, task_id = machine.start()
        machine.die_on_login()
        machine.go_idle()
        machine.poll()

        class Dies:
            def nudge(self, session_id: str, message: str) -> NudgeReceipt:
                if deliver:
                    at = datetime.now(timezone.utc) + timedelta(seconds=5)
                    path = machine.claude / "projects" / "C--projects-x" / f"{FULL}.jsonl"
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "type": "user",
                                    "timestamp": at.isoformat(),
                                    "message": {"role": "user", "content": message},
                                    "sessionId": FULL,
                                }
                            )
                            + "\n"
                        )
                raise _Crash()

        with pytest.raises(_Crash):
            machine.tick(
                0,
                probe=lambda request: ProbeResult(ProbeClass.READY, "ok", 0),
                nudger_for=lambda runner: Dies(),
            )
        return run_id, task_id

    def test_a_delivered_message_is_recognised_and_not_sent_again(self, machine: Machine) -> None:
        run_id, task_id = self._crash_mid_nudge(machine, deliver=True)
        resent: List[str] = []

        class MustNotSend:
            def nudge(self, session_id: str, message: str) -> NudgeReceipt:
                resent.append(message)
                return NudgeReceipt("applied", "woke", session_id)

        ready = lambda request: ProbeResult(ProbeClass.READY, "ok", 0)  # noqa: E731
        machine.tick(10, probe=ready, nudger_for=lambda runner: MustNotSend())
        machine.tick(
            auth_recovery.NUDGE_LEASE_SECONDS + 5, probe=ready, nudger_for=lambda r: MustNotSend()
        )
        waiter = machine.book().waiters(machine.meta(run_id)["auth_incident"])[0]
        assert waiter.status == auth_recovery.NUDGED
        assert resent == []

        # Delivered is not recovered: that still needs the session's own reply.
        machine.tick(auth_recovery.NUDGE_LEASE_SECONDS + 20, probe=ready)
        assert machine.book().waiters(waiter.incident_id)[0].status == auth_recovery.NUDGED
        path = machine.claude / "projects" / "C--projects-x" / f"{FULL}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            reply = real_reply_line(
                at=datetime.now(timezone.utc) + timedelta(seconds=30), session=FULL
            )
            handle.write(json.dumps(reply) + "\n")
        machine.tick(auth_recovery.NUDGE_LEASE_SECONDS + 40, probe=ready)
        assert machine.book().waiters(waiter.incident_id)[0].status == auth_recovery.RECOVERED
        assert machine.human_handoffs(task_id) == []

    def test_an_unprovable_delivery_is_escalated_once_and_the_run_is_held(
        self, machine: Machine
    ) -> None:
        run_id, task_id = self._crash_mid_nudge(machine, deliver=False)
        resent: List[str] = []

        class MustNotSend:
            def nudge(self, session_id: str, message: str) -> NudgeReceipt:
                resent.append(message)
                return NudgeReceipt("applied", "woke", session_id)

        ready = lambda request: ProbeResult(ProbeClass.READY, "ok", 0)  # noqa: E731
        for seconds in (
            auth_recovery.NUDGE_LEASE_SECONDS + 5,
            auth_recovery.NUDGE_LEASE_SECONDS + 70,
        ):
            machine.tick(seconds, probe=ready, nudger_for=lambda runner: MustNotSend())
        waiter = machine.book().waiters(machine.meta(run_id)["auth_incident"])[0]
        assert waiter.status == auth_recovery.UNCERTAIN
        assert resent == [], "never resent blind"
        handoffs = machine.human_handoffs(task_id)
        assert len(handoffs) == 1 and handoffs[0].data[MARKER]["action"] == "escalate"

        # The session was stopped mid-nudge; a poll must not conclude the run on that.
        machine.set_rows([])
        assert machine.poll()[run_id].phase is SessionPhase.AUTH_STALLED
        assert find_run(machine.home, run_id).status not in {"finished", "cancelled", "failed"}


# ----- a6 ----------------------------------------------------------------------------


class TestStopAndLaterWords:
    def test_stop_removes_the_waiter_and_no_later_nudge_happens(self, machine: Machine) -> None:
        run_id, task_id = machine.start()
        machine.die_on_login()
        machine.go_idle()
        machine.poll()
        request_cancel(
            machine.home,
            find_run(machine.home, run_id),
            requester="Jeff Posey",
            source="gui",
            reason="not now",
        )
        machine.plan_probes([_success()])
        for seconds in (0, 61, 400):
            machine.tick(seconds)
        assert machine.lines("nudges.log") == []
        assert machine.lines("probes.log") == [], "no remaining waiter means no probe"
        waiter = machine.book().waiters(machine.meta(run_id)["auth_incident"])[0]
        assert waiter.status == auth_recovery.REMOVED
        incident = machine.book().incident(waiter.incident_id)
        assert incident is not None and incident.state == "closed"

    def test_a_newer_review_handoff_is_not_talked_over_or_overwritten(
        self, machine: Machine
    ) -> None:
        run_id, task_id = machine.start()
        machine.die_on_login()
        machine.go_idle()
        machine.poll()
        machine.plan_probes([_auth_failure()])
        for seconds in (0, 301):
            machine.tick(seconds)
        assert len(machine.human_handoffs(task_id)) == 1
        machine.manager.handoff(
            task_id,
            actor="Jeff Posey",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="I am reviewing what is there so far; leave it.",
        )
        machine.plan_probes([_success()])
        machine.tick(361)
        assert machine.lines("nudges.log") == []
        task = machine.task(task_id)
        assert task.ball is Ball.HUMAN and task.ball_reason is BallReason.REVIEW
        assert task.ball_prompt == "I am reviewing what is there so far; leave it."


# ----- a8 ----------------------------------------------------------------------------


class TestUsageLimits:
    def _limit_line(self, *, at: datetime, resets: datetime) -> dict:
        line = auth_failure_line(
            at=at,
            session=FULL,
            text="You've hit your session limit · resets 3:30am (America/Chicago)",
        )
        line["error"] = "rate_limit"
        line["quotaLimits"] = {
            "rateLimitType": "five_hour",
            "status": "rejected",
            "resetsAt": int(resets.timestamp()),
        }
        return line

    def test_parks_external_resumes_once_after_the_reset_and_never_loops(
        self, machine: Machine
    ) -> None:
        run_id, task_id = machine.start()
        now = datetime.now(timezone.utc)
        resets = (now + timedelta(hours=2)).replace(microsecond=0)
        machine.transcript([self._limit_line(at=now + timedelta(seconds=1), resets=resets)])
        machine.go_idle()
        assert machine.poll()[run_id].phase is SessionPhase.AUTH_STALLED

        task = machine.task(task_id)
        assert task.ball is Ball.EXTERNAL and task.ball_reason is BallReason.SERVICE
        assert resets.isoformat() in (task.ball_prompt or "")

        calls: List[ProbeRequest] = []

        def ready(request: ProbeRequest) -> ProbeResult:
            calls.append(request)
            return ProbeResult(ProbeClass.READY, "ok", 0)

        nudged: List[str] = []

        class Recording:
            def nudge(self, session_id: str, message: str) -> NudgeReceipt:
                nudged.append(message)
                return NudgeReceipt("applied", "woke", session_id)

        machine.tick(3600, probe=ready, nudger_for=lambda runner: Recording())
        assert calls == [] and nudged == [], "nothing is sent against a limit still in force"
        machine.tick(2 * 3600 + 61, probe=ready, nudger_for=lambda runner: Recording())
        assert len(calls) == 1 and len(nudged) == 1
        assert "a usage limit" in nudged[0]

        # Refused again for the same window: a person, not another resume.
        machine.transcript(
            [
                self._limit_line(at=now + timedelta(seconds=1), resets=resets),
                self._limit_line(at=now + timedelta(hours=2, minutes=2), resets=resets),
            ]
        )
        machine.poll()
        machine.tick(2 * 3600 + 200, probe=ready, nudger_for=lambda runner: Recording())
        assert len(nudged) == 1
        task = machine.task(task_id)
        assert task.ball is Ball.HUMAN
        assert "will not loop against the limit" in (task.ball_prompt or "")

    def test_a_probe_still_refused_waits_for_its_own_reported_reset(self, machine: Machine) -> None:
        run_id, _ = machine.start()
        now = datetime.now(timezone.utc)
        resets = now + timedelta(minutes=10)
        machine.transcript([self._limit_line(at=now + timedelta(seconds=1), resets=resets)])
        machine.go_idle()
        machine.poll()
        later = now + timedelta(hours=3)

        def still(request: ProbeRequest) -> ProbeResult:
            return ProbeResult(ProbeClass.USAGE_EXHAUSTED, "session limit", 1, resets_at=later)

        machine.tick(11 * 60 + 5, probe=still, nudger_for=lambda runner: None)
        incident = machine.book().incident(machine.meta(run_id)["auth_incident"])
        assert incident is not None
        assert incident.next_probe_at >= later


# ----- the epic walk holds ------------------------------------------------------------


class TestTheWalkHolds:
    def test_a_recovery_park_holds_and_an_escalation_grounds(self, machine: Machine) -> None:
        from agentjobs.dispatch.epic import _recovering

        _, task_id = machine.start()
        for action, expected in (("notify", True), ("park", True), ("escalate", False)):
            task = machine.manager.handoff(
                task_id,
                actor="dispatcher",
                ball=Ball.HUMAN,
                ball_reason=BallReason.INPUT,
                ball_prompt=f"{action}",
                data={MARKER: {"incident": "inc_x", "run_id": "run_x", "action": action}},
            )
            assert _recovering(task, "parked") is expected, action
        assert _recovering(task, "finished") is False


# ----- the real driver --------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("AGENTJOBS_REAL_AUTH_PROBE") != "1" or shutil.which("claude") is None,
    reason="spends a real model call; set AGENTJOBS_REAL_AUTH_PROBE=1 to verify the contract",
)
class TestTheRealProbeContract:
    """Both halves against the installed CLI, never by damaging a real store: an empty,
    disposable Claude home for the refusal, and the machine's own home, read-only, for
    the answer. The suite's conftest points ``CLAUDE_CONFIG_DIR`` at a temp directory,
    which is why the second test has to remove it."""

    def test_an_empty_claude_home_is_refused_not_ready(self, tmp_path: Path) -> None:
        executable = shutil.which("claude")
        assert executable is not None
        empty = tmp_path / "empty-claude-home"
        empty.mkdir()
        env = dict(os.environ, **{CLAUDE_HOME_ENV: str(empty)})
        result = run_probe(ProbeRequest(executable=[executable], model=None, cwd=tmp_path, env=env))
        assert result.klass is ProbeClass.AUTH_REJECTED, result.detail

    def test_the_installed_cli_answers_the_probe_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        executable = shutil.which("claude")
        assert executable is not None
        monkeypatch.delenv(CLAUDE_HOME_ENV, raising=False)
        result = run_probe(ProbeRequest(executable=[executable], model=None, cwd=tmp_path))
        assert result.klass is ProbeClass.READY, result.detail
