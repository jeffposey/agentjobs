"""The idle-session sweep (task-447): who may be stopped, and the proof demanded first.

Every protected class in the spec has its own test, named for the class, and each builds
the same machine with one fact changed -- so a green test says that fact alone is what kept
the session alive. The command lines are the shapes read off this machine on 2026-09-13.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

from agentjobs.dispatch.config import (
    DispatchConfigError,
    load_dispatch_config,
    set_idle_session_settings,
)
from agentjobs.dispatch.idle_sessions import (
    KIND_BACKGROUND,
    KIND_COMMAND,
    KIND_DAEMON,
    KIND_DESKTOP,
    KIND_INTERACTIVE,
    KIND_PTY_HOST,
    KIND_RC_CHILD,
    KIND_RC_HOST,
    VERDICT_CANDIDATE,
    VERDICT_IDLE,
    VERDICT_IN_USE,
    VERDICT_PROTECTED,
    VERDICT_REPORT_ONLY,
    Evidence,
    IdleSessionBook,
    ProcessRow,
    SessionView,
    SweepDeps,
    classify_process,
    judge,
    note_mode,
    split_command_line,
    stop_candidate,
    sweep,
    take_inventory,
)
from agentjobs.execution.store import ExecutionStore

NOW = 1_789_400_000.0
HOUR = 3600
EXE = "C:/Users/me/AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
DESKTOP = "C:/Program Files/WindowsApps/Claude_1.52386.3.0_x64__pzs8sxrjxfjjc/app/claude.exe"
BUNDLED = "C:/Users/me/AppData/Roaming/Claude/claude-code/2.1.266/claude.exe"
IDLE_UUID = "764ca346-1111-4222-8333-444455556666"
RC_UUID = "641272aa-1111-4222-8333-444455556666"
CWD = "C:/projects/agentjobs"


def row(pid: int, ppid: int, cmdline: str, exe: str = EXE) -> ProcessRow:
    return ProcessRow(pid=pid, ppid=ppid, name=exe.rsplit("/", 1)[-1], exe=exe, cmdline=cmdline)


def machine() -> List[ProcessRow]:
    """The 2026-09-13 inventory in miniature: one of each kind, and one idle bg session."""
    return [
        row(1, 0, "services.exe", exe="C:/Windows/System32/services.exe"),
        row(10, 1, f'"{EXE}" daemon run --origin transient'),
        row(
            11,
            10,
            f"{EXE} --bg-pty-host //./pipe/cc-daemon-pty 200 50 -- {EXE} --session-id {IDLE_UUID}",
        ),
        row(12, 11, f"{EXE} --session-id {IDLE_UUID} --remote-control --model claude-opus-5"),
        row(20, 1, f'"{EXE}" rc --spawn same-dir --remote-control-session-name-prefix agentjobs'),
        row(21, 20, f"{EXE} --print --sdk-url https://example.invalid/v1 --session-id {RC_UUID}"),
        row(30, 1, f'"{DESKTOP}"', exe=DESKTOP),
        row(
            31, 30, f"{BUNDLED} --output-format stream-json --input-format stream-json", exe=BUNDLED
        ),
        row(40, 1, f'"{EXE}" --model claude-opus-5'),
        row(90, 1, "python.exe -m agentjobs serve", exe="C:/Python/python.exe"),
    ]


def ledger_for(status: str = "idle", **extra: Any) -> List[Dict[str, Any]]:
    entry = {
        "pid": 12,
        "id": IDLE_UUID[:8],
        "sessionId": IDLE_UUID,
        "cwd": CWD,
        "kind": "background",
        "name": "durable execution design",
        "status": status,
        "state": "done",
    }
    entry.update(extra)
    return [entry]


def transcripts(quiet_seconds: Dict[str, float]):
    def find(session_uuid: str, cwd: str) -> Optional[Tuple[str, float]]:
        if session_uuid not in quiet_seconds:
            return None
        return (
            f"C:/Users/me/.claude/projects/x/{session_uuid}.jsonl",
            NOW - quiet_seconds[session_uuid],
        )

    return find


def evidence(
    *,
    processes: Optional[List[ProcessRow]] = None,
    ledger: Optional[List[Dict[str, Any]]] = None,
    ledger_error: Optional[str] = None,
    runs: Optional[Dict[str, Tuple[str, str]]] = None,
    quiet: Optional[Dict[str, float]] = None,
    self_pid: int = 90,
) -> Evidence:
    return Evidence(
        processes=machine() if processes is None else processes,
        ledger=ledger_for() if ledger is None else ledger,
        ledger_error=ledger_error,
        run_sessions=runs or {},
        transcripts=transcripts(
            {IDLE_UUID: 5 * HOUR, RC_UUID: 9 * HOUR} if quiet is None else quiet
        ),
        identities=lambda pid: f"win:{pid}:1000",
        now=NOW,
        self_pid=self_pid,
    )


def verdicts(ev: Evidence, idle_minutes: int = 240) -> Dict[int, SessionView]:
    return {view.pid: view for view in judge(ev, idle_minutes=idle_minutes)}


# ----- classification -----------------------------------------------------------------


class TestClassification:
    def test_every_kind_on_the_2026_09_13_machine_is_named(self) -> None:
        processes = machine()
        parents = {p.pid: p for p in processes}
        kinds = {p.pid: classify_process(p, parents) for p in processes if p.pid in parents}

        assert kinds[10] == KIND_DAEMON
        assert kinds[11] == KIND_PTY_HOST
        assert kinds[12] == KIND_BACKGROUND
        assert kinds[20] == KIND_RC_HOST
        assert kinds[21] == KIND_RC_CHILD
        assert kinds[30] == KIND_DESKTOP
        assert kinds[31] == KIND_DESKTOP
        assert kinds[40] == KIND_INTERACTIVE

    def test_non_claude_processes_are_not_listed(self) -> None:
        assert {1, 90}.isdisjoint(verdicts(evidence()))

    def test_short_lived_commands_are_not_sessions(self) -> None:
        processes = machine() + [
            row(50, 1, f"{EXE} agents --json"),
            row(51, 1, f"{EXE} --bg hello"),
        ]
        found = verdicts(evidence(processes=processes))
        assert found[50].kind == KIND_COMMAND and found[50].verdict == VERDICT_PROTECTED
        assert found[51].kind == KIND_COMMAND

    def test_quoted_paths_split_as_one_token(self) -> None:
        assert split_command_line('"C:/Program Files/a b.exe" rc --spawn') == [
            "C:/Program Files/a b.exe",
            "rc",
            "--spawn",
        ]


# ----- the one candidate, and each thing that protects it ------------------------------


class TestTheCandidate:
    def test_an_idle_resumable_bg_session_past_the_threshold_is_a_candidate(self) -> None:
        view = verdicts(evidence())[12]

        assert view.verdict == VERDICT_CANDIDATE
        assert view.idle_seconds == 5 * HOUR
        assert view.resume_commands == [
            f"claude attach {IDLE_UUID[:8]}",
            f"claude --bg --resume {IDLE_UUID}",
        ]

    def test_it_is_the_only_candidate_on_the_machine(self) -> None:
        candidates = [
            v.pid for v in judge(evidence(), idle_minutes=240) if v.verdict == VERDICT_CANDIDATE
        ]
        assert candidates == [12]

    def test_under_the_threshold_it_is_idle_not_a_candidate(self) -> None:
        view = verdicts(evidence(quiet={IDLE_UUID: 3 * HOUR}))[12]
        assert view.verdict == VERDICT_IDLE

    def test_idle_time_is_the_transcript_not_process_age(self) -> None:
        """A session started days ago that wrote a minute ago is not idle."""
        view = verdicts(evidence(quiet={IDLE_UUID: 60}))[12]
        assert view.verdict == VERDICT_IDLE and view.idle_seconds == 60


class TestNeverStopped:
    """One test per protected class in the spec (a3)."""

    def test_remote_control_host(self) -> None:
        view = verdicts(evidence())[20]
        assert view.verdict == VERDICT_PROTECTED
        assert view.name == "Remote Control: agentjobs"

    def test_remote_control_child_is_reported_never_a_candidate(self) -> None:
        view = verdicts(evidence())[21]
        assert view.verdict == VERDICT_REPORT_ONLY
        assert view.idle_seconds == 9 * HOUR

    def test_daemon(self) -> None:
        assert verdicts(evidence())[10].verdict == VERDICT_PROTECTED

    def test_desktop_app_and_its_bundled_sessions(self) -> None:
        found = verdicts(evidence())
        assert found[30].verdict == VERDICT_PROTECTED
        assert found[31].verdict == VERDICT_PROTECTED

    def test_interactive_session(self) -> None:
        assert verdicts(evidence())[40].verdict == VERDICT_PROTECTED

    def test_busy_session(self) -> None:
        view = verdicts(evidence(ledger=ledger_for("busy", state="working")))[12]
        assert view.verdict == VERDICT_IN_USE

    def test_blocked_session(self) -> None:
        view = verdicts(evidence(ledger=ledger_for("waiting", state="blocked")))[12]
        assert view.verdict == VERDICT_IN_USE

    def test_attached_session(self) -> None:
        processes = machine() + [row(60, 1, f"{EXE} attach {IDLE_UUID[:8]}")]
        assert verdicts(evidence(processes=processes))[12].verdict == VERDICT_IN_USE

    def test_live_agentjobs_run(self) -> None:
        view = verdicts(evidence(runs={IDLE_UUID[:8]: ("run_abcd1234", "running")}))[12]
        assert view.verdict == VERDICT_PROTECTED and view.run_id == "run_abcd1234"

    def test_any_agentjobs_run_record_even_a_finished_one(self) -> None:
        """``finished_without_handoff`` keeps its session attachable on purpose (task-442)."""
        view = verdicts(evidence(runs={IDLE_UUID[:8]: ("run_x", "finished_without_handoff")}))[12]
        assert view.verdict == VERDICT_PROTECTED

    def test_agentjobs_dispatch_name_with_no_record(self) -> None:
        view = verdicts(evidence(ledger=ledger_for(name="agentjobs/task-447@f92ef995")))[12]
        assert view.verdict == VERDICT_PROTECTED

    def test_the_session_doing_the_sweep(self) -> None:
        processes = machine() + [
            row(91, 12, "python.exe -m agentjobs sessions idle", exe="C:/py/python.exe")
        ]
        view = verdicts(evidence(processes=processes, self_pid=91))[12]
        assert view.verdict == VERDICT_PROTECTED and "running the sweep" in view.reason


class TestUnprovenIsProtected:
    def test_no_ledger_row(self) -> None:
        assert verdicts(evidence(ledger=[]))[12].verdict == VERDICT_PROTECTED

    def test_unreadable_ledger(self) -> None:
        assert verdicts(evidence(ledger_error="timed out"))[12].verdict == VERDICT_PROTECTED

    def test_no_transcript_means_not_resumable(self) -> None:
        assert verdicts(evidence(quiet={}))[12].verdict == VERDICT_PROTECTED

    def test_unrecognised_status(self) -> None:
        assert verdicts(evidence(ledger=ledger_for("hibernating")))[12].verdict == VERDICT_PROTECTED


# ----- stopping -----------------------------------------------------------------------


class FakeMachine:
    """A mutable machine behind SweepDeps, recording every stop it is asked for."""

    def __init__(self) -> None:
        self.processes = machine()
        self.ledger = ledger_for()
        self.quiet = {IDLE_UUID: 5.0 * HOUR, RC_UUID: 9.0 * HOUR}
        self.identity_of: Dict[int, str] = {}
        self.stopped: List[str] = []
        self.reads = 0

    def deps(self) -> SweepDeps:
        return SweepDeps(
            processes=self._processes,
            ledger=lambda: self.ledger,
            transcripts=lambda sid, cwd: transcripts(self.quiet)(sid, cwd),
            identity=lambda pid: self.identity_of.get(pid, f"win:{pid}:1000"),
            alive=lambda pid: any(p.pid == pid for p in self.processes),
            stop=self._stop,
            runs=lambda: {},
            now=lambda: NOW,
            sleep=lambda seconds: None,
            self_pid=90,
        )

    def _processes(self) -> List[ProcessRow]:
        self.reads += 1
        return list(self.processes)

    def _stop(self, short_id: str) -> Tuple[bool, str]:
        self.stopped.append(short_id)
        self.processes = [p for p in self.processes if p.pid not in (11, 12)]
        return True, "stopped"


@pytest.fixture
def book(tmp_path: Path):
    store = ExecutionStore(tmp_path / "execution.db")
    yield IdleSessionBook(store)
    store.close()


class TestSweep:
    def test_report_mode_reads_nothing_and_stops_nothing(self, tmp_path: Path, book) -> None:
        fake = FakeMachine()
        result = sweep(
            tmp_path, enforce=False, idle_minutes=240, max_stops=3, book=book, deps=fake.deps()
        )

        assert fake.stopped == [] and fake.reads == 0
        assert result.stops == [] and book.events() == []

    def test_enforcement_stops_only_the_candidate_and_records_how_to_resume(
        self, tmp_path: Path, book
    ) -> None:
        fake = FakeMachine()
        result = sweep(
            tmp_path, enforce=True, idle_minutes=240, max_stops=3, book=book, deps=fake.deps()
        )

        assert fake.stopped == [IDLE_UUID[:8]]
        assert [r.outcome for r in result.stops] == ["stopped"]
        stops = [e for e in book.events() if e.kind == "stop"]
        assert len(stops) == 1
        detail = stops[0].detail
        assert stops[0].session_id == IDLE_UUID
        assert detail["name"] == "durable execution design"
        assert detail["cwd"] == CWD
        assert detail["idle_seconds"] == 5 * HOUR
        assert detail["last_activity"]
        assert "quiet for 5h 00m" in detail["reason"]
        assert detail["resume_commands"] == [
            f"claude attach {IDLE_UUID[:8]}",
            f"claude --bg --resume {IDLE_UUID}",
        ]

    def test_max_stops_bounds_one_sweep(self, tmp_path: Path, book) -> None:
        fake = FakeMachine()
        sweep(tmp_path, enforce=True, idle_minutes=240, max_stops=0, book=book, deps=fake.deps())
        assert fake.stopped == []


class TestReCheckedImmediatelyBeforeEachStop:
    def _candidate(self, fake: FakeMachine, tmp_path: Path) -> SessionView:
        inventory = take_inventory(tmp_path, idle_minutes=240, deps=fake.deps())
        (view,) = inventory.candidates
        return view

    def test_a_reused_pid_is_not_stopped(self, tmp_path: Path, book) -> None:
        fake = FakeMachine()
        view = self._candidate(fake, tmp_path)
        fake.identity_of[12] = "win:12:2000"  # same number, a different process

        report = stop_candidate(view, book=book, idle_minutes=240, deps=fake.deps(), trigger="test")

        assert report.outcome == "declined" and fake.stopped == []
        assert book.events()[0].outcome == "declined"

    def test_a_changed_command_line_is_not_stopped(self, tmp_path: Path, book) -> None:
        fake = FakeMachine()
        view = self._candidate(fake, tmp_path)
        fake.processes = [
            replace(p, cmdline=p.cmdline + " --other") if p.pid == 12 else p for p in fake.processes
        ]

        report = stop_candidate(view, book=book, idle_minutes=240, deps=fake.deps(), trigger="test")

        assert report.outcome == "declined" and fake.stopped == []

    def test_a_session_that_became_busy_is_not_stopped(self, tmp_path: Path, book) -> None:
        fake = FakeMachine()
        view = self._candidate(fake, tmp_path)
        fake.ledger = ledger_for("busy")

        report = stop_candidate(view, book=book, idle_minutes=240, deps=fake.deps(), trigger="test")

        assert report.outcome == "declined" and fake.stopped == []

    def test_a_transcript_written_since_the_inventory_is_not_stopped(
        self, tmp_path: Path, book
    ) -> None:
        fake = FakeMachine()
        view = self._candidate(fake, tmp_path)
        fake.quiet[IDLE_UUID] = 5.0

        report = stop_candidate(view, book=book, idle_minutes=240, deps=fake.deps(), trigger="test")

        assert report.outcome == "declined" and fake.stopped == []


# ----- the switch-over record and the settings ----------------------------------------


class TestModeRecord:
    def test_nothing_is_recorded_while_enforcement_has_never_been_on(self, book) -> None:
        assert note_mode(book, enforce=False, idle_minutes=240) is None
        assert book.events() == []

    def test_switching_on_and_off_records_the_auth_incident_count(self, book) -> None:
        on = note_mode(book, enforce=True, idle_minutes=240)
        again = note_mode(book, enforce=True, idle_minutes=240)
        off = note_mode(book, enforce=False, idle_minutes=240)

        assert on is not None and on.outcome == "enforce" and on.detail["auth_incidents"] == 0
        assert again is None
        assert off is not None and off.outcome == "report"


class TestSettings:
    def write(self, home: Path, body: Dict[str, Any]) -> None:
        home.mkdir(parents=True, exist_ok=True)
        (home / "dispatch.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")

    def test_enforcement_is_off_by_default(self, tmp_path: Path) -> None:
        self.write(tmp_path, {"version": 1, "runners": {}})
        config = load_dispatch_config(tmp_path)
        assert config is not None
        assert config.idle_sessions.enforce is False
        assert config.idle_sessions.idle_minutes == 240

    def test_the_switch_writes_and_keeps_unknown_keys(self, tmp_path: Path) -> None:
        self.write(tmp_path, {"version": 1, "runners": {}, "idle_sessions": {"future": 1}})

        settings = set_idle_session_settings(enforce=True, idle_minutes=120, home=tmp_path)

        assert settings.enforce is True and settings.idle_minutes == 120
        raw = yaml.safe_load((tmp_path / "dispatch.yaml").read_text(encoding="utf-8"))
        assert raw["idle_sessions"]["future"] == 1

    def test_a_non_boolean_enforce_is_refused(self, tmp_path: Path) -> None:
        self.write(tmp_path, {"version": 1, "runners": {}, "idle_sessions": {"enforce": "yes"}})
        with pytest.raises(DispatchConfigError):
            load_dispatch_config(tmp_path)
