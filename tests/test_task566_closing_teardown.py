"""What a run leaves running is stopped when its task closes (task-566).

The case these are written from is task-557, 2026-09-24: two review sandboxes started as
background jobs from the task's worktree kept serving for fifteen minutes after the finish
had merged, closed and removed the worktree; its empty root survived; and the dispatched
session could not exit until the owner messaged it.

Two halves, as in the task. **A**: the finish stops every process running out of the
worktree before removing it, and the directory is then really gone. **B**: a session still
open, idle and unaddressed after the close is stopped -- and one a person has written to
since its review is not, which is task-482's case.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

from agentjobs.dispatch import closed_sessions, finish, worktree_teardown
from agentjobs.dispatch.closed_sessions import Deps, end_lingering, read_transcript
from agentjobs.dispatch.ledger import RunRecord
from agentjobs.dispatch.proctree import Proc
from agentjobs.dispatch.worktree_teardown import Facts, residents, stop_residents

WORKTREE = Path("C:/projects/worktrees/agentjobs-557")
T0 = 134_000_000_000_000_000


def row(
    pid: int,
    ppid: int,
    *,
    cwd: str = "C:/elsewhere",
    cmd: str = "",
    name: str = "python.exe",
    age: int = 0,
):
    proc = Proc(pid, ppid, T0 + pid * 1_000_000 + age, name, cmd)
    return proc, Facts(proc.created, cwd, cmd)


def recorder(into: List[Any], *, key: str = "pid") -> Any:
    """A stand-in for an act that succeeds, remembering what it was done to."""

    def act(target: Any) -> bool:
        into.append(getattr(target, key))
        return True

    return act


# ----- A: which processes belong to a worktree ---------------------------------------


class TestResidents:
    def test_cwd_and_command_line_both_count_in_every_spelling(self) -> None:
        rows = [
            row(10, 1, cwd="C:\\projects\\worktrees\\agentjobs-557\\"),
            row(11, 1, cwd="c:/projects/worktrees/agentjobs-557/frontend"),
            row(12, 1, cmd='bash -c "cd C:/projects/worktrees/agentjobs-557 && python s.py 8930"'),
            row(13, 1, cmd="bash -c 'cd /c/projects/worktrees/agentjobs-557; ls'"),
            row(14, 1, cmd="python C:\\projects\\worktrees\\agentjobs-557\\scripts\\x.py"),
        ]
        found, kept = residents(rows, WORKTREE, self_pid=999)
        assert sorted(item.proc.pid for item in found) == [10, 11, 12, 13, 14]
        assert {item.why for item in found} == {"working directory", "command line"}
        assert kept == []

    def test_a_longer_name_sharing_the_prefix_is_not_the_worktree(self) -> None:
        rows = [
            row(10, 1, cwd="C:/projects/worktrees/agentjobs-5570"),
            row(11, 1, cmd="python C:/projects/worktrees/agentjobs-5571/x.py"),
            row(12, 1, cwd="C:/projects/worktrees/agentjobs-55"),
        ]
        assert residents(rows, WORKTREE, self_pid=999) == ([], [])

    def test_descendants_go_with_their_parent_wherever_they_run(self) -> None:
        # epic_walk_sandbox.py's sleepers: their cwd need not be the worktree.
        rows = [
            row(10, 1, cwd=str(WORKTREE)),
            row(20, 10, cwd="C:/Windows"),
            row(30, 20, cwd="C:/Windows"),
            row(40, 1, cwd="C:/Windows"),
        ]
        found, _ = residents(rows, WORKTREE, self_pid=999)
        assert sorted(item.proc.pid for item in found) == [10, 20, 30]

    def test_this_process_and_its_ancestors_are_never_stopped(self) -> None:
        # A posture finish is run by the session's own shell, which may have cd'd there.
        rows = [
            row(5, 1, name="claude.exe", cmd="claude --session-id 66b55e81-aaaa"),
            row(
                6, 5, name="bash.exe", cmd=f"bash -c 'cd {WORKTREE.as_posix()} && agentjobs finish'"
            ),
            row(7, 6, cwd=str(WORKTREE), cmd="python -m agentjobs finish"),
            row(8, 5, name="bash.exe", cwd=str(WORKTREE), cmd="python sandbox.py 8930"),
        ]
        found, kept = residents(rows, WORKTREE, self_pid=7)
        assert [item.proc.pid for item in found] == [8]
        assert sorted(proc.pid for proc, _ in kept) == [6, 7]

    def test_a_claude_session_is_left_to_claude_stop(self) -> None:
        rows = [
            row(5, 1, name="claude.exe", cwd=str(WORKTREE), cmd="claude --bg"),
            row(
                6, 1, name="node.exe", cwd=str(WORKTREE), cmd="node cli.js --session-id 1234abcd-x"
            ),
        ]
        found, kept = residents(rows, WORKTREE, self_pid=999)
        assert found == []
        assert sorted(proc.pid for proc, _ in kept) == [5, 6]

    def test_an_unreadable_process_is_not_judged(self) -> None:
        proc = Proc(10, 1, T0, "python.exe", "")
        assert residents([(proc, None)], WORKTREE, self_pid=999) == ([], [])


class TestStopResidents:
    def rows(self) -> List[Tuple[Proc, Optional[Facts]]]:
        return [
            row(10, 1, cwd=str(WORKTREE), cmd="python sandbox.py 8930"),
            row(20, 10, cmd="python sleeper.py"),
        ]

    def test_leaves_first_and_the_step_names_them(self) -> None:
        table = self.rows()
        facts = {proc.pid: fact for proc, fact in table}
        ended: List[int] = []
        result = stop_residents(
            WORKTREE,
            rows=lambda: table,
            facts=facts.get,
            end=recorder(ended),
            self_pid=999,
            sleep=lambda _: None,
        )
        assert ended == [20, 10]
        assert "stopped 2 process(es)" in result.sentence()
        assert "python.exe 10 (working directory)" in result.sentence()

    def test_a_reused_pid_is_not_stopped(self) -> None:
        table = self.rows()
        ended: List[int] = []

        def facts(pid: int) -> Optional[Facts]:
            # Pid 10 has been handed to a new process since the table was read.
            if pid == 10:
                return Facts(T0 + 99_000_000_000, str(WORKTREE), "python sandbox.py 8930")
            return dict((p.pid, f) for p, f in table)[pid]

        result = stop_residents(
            WORKTREE,
            rows=lambda: table,
            facts=facts,
            end=recorder(ended),
            self_pid=999,
            sleep=lambda _: None,
        )
        assert ended == [20]
        assert "pid 10: the pid now belongs to a different process" in result.sentence()

    def test_a_changed_command_line_is_not_stopped(self) -> None:
        table = self.rows()
        ended: List[int] = []
        current = {proc.pid: fact for proc, fact in table}
        seen = current[10]
        assert seen is not None
        current[10] = Facts(seen.created, str(WORKTREE), "python something-else.py")
        result = stop_residents(
            WORKTREE,
            rows=lambda: table,
            facts=current.get,
            end=recorder(ended),
            self_pid=999,
            sleep=lambda _: None,
        )
        assert 10 not in ended
        assert "its command line changed" in result.sentence()

    def test_a_launcher_that_exits_with_its_child_is_not_a_failure(self) -> None:
        # A virtualenv's python.exe exits when the interpreter it started is stopped. It is
        # still alive when re-read before its own stop, and gone by the time the terminate
        # reaches it -- that is "exited by itself", not "could not be ended".
        table = self.rows()
        known = {proc.pid: fact for proc, fact in table}
        reads: Dict[int, int] = {}

        def facts(pid: int) -> Optional[Facts]:
            reads[pid] = reads.get(pid, 0) + 1
            return known[pid] if reads[pid] == 1 else None

        result = stop_residents(
            WORKTREE,
            rows=lambda: table,
            facts=facts,
            end=lambda proc: proc.pid == 20,
            self_pid=999,
            sleep=lambda _: None,
        )
        assert [item.proc.pid for item in result.ended] == [20]
        assert [item.proc.pid for item in result.gone] == [10]
        assert result.declined == []
        assert "1 exited by themselves (pid 10)" in result.sentence()

    def test_nothing_there_says_so(self) -> None:
        result = stop_residents(WORKTREE, rows=lambda: [], self_pid=999)
        assert result.sentence() == "nothing to stop"

    def test_an_unreadable_machine_is_reported_not_raised(self) -> None:
        def broken() -> List[Tuple[Proc, Optional[Facts]]]:
            raise OSError("snapshot failed")

        result = stop_residents(WORKTREE, rows=broken, self_pid=999)
        assert "could not look for processes in the worktree: snapshot failed" == result.sentence()


# ----- A, for real: a live process in a real worktree ---------------------------------


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.skipif(os.name != "nt", reason="the PEB reader is the Windows reference path")
def test_the_finish_stops_a_sandbox_and_leaves_no_root_behind(tmp_path: Path, monkeypatch) -> None:
    """The incident, reproduced: a process pinned in the worktree, and its child elsewhere.

    Real processes, the real process table and the real removal. Nothing outside this
    test's own temporary worktree can match it, so the scan is safe under a parallel gate.
    """
    monkeypatch.setattr(worktree_teardown, "RESIDENT_READER", worktree_teardown.machine_rows)
    root = tmp_path / "clone"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(
        root,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "base",
    )
    worktree = tmp_path / "worktrees" / "clone-557"
    git(root, "worktree", "add", "-q", str(worktree), "-b", "feat/task-557-x")

    # A sandbox whose cwd is the worktree, which starts a sleeper whose cwd is not.
    child = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],cwd={str(tmp_path)!r});"
        "time.sleep(120)"
    )
    sandbox = subprocess.Popen([sys.executable, "-c", child], cwd=worktree)
    try:
        deadline = time.time() + 20
        tree: List[Proc] = []
        while time.time() < deadline:
            table = [proc for proc, _ in worktree_teardown.machine_rows()]
            me = next(proc for proc in table if proc.pid == sandbox.pid)
            from agentjobs.dispatch.proctree import descendants

            tree = descendants(table, [me])
            if tree:
                break
            time.sleep(0.2)
        assert tree, "the sandbox never started its sleeper"

        plan = SimpleNamespace(
            has_worktree=True, worktree=worktree, root=root, base="main", branch="feat/task-557-x"
        )
        stopped = finish.stop_worktree_processes(plan)  # type: ignore[arg-type]
        removed = finish.remove_worktree(plan)  # type: ignore[arg-type]

        assert stopped.step == "teardown"
        # A virtualenv's python.exe is a launcher that starts the real interpreter as its
        # child, so each Python here is two processes: at least the sandbox and its sleeper.
        assert stopped.detail.startswith("stopped "), stopped.detail
        # The launcher may exit on its own once its interpreter is stopped, so the sandbox's
        # own pid is either stopped or reported as having exited -- never left running.
        assert str(sandbox.pid) in stopped.detail
        assert "(started by one of them)" in stopped.detail
        assert "did not stop" not in stopped.detail
        assert sandbox.wait(timeout=10) is not None
        after = {proc.pid for proc in worktree_teardown.proctree.fast_process_table()}
        assert all(proc.pid not in after or proc.pid == 0 for proc in tree)
        assert removed.detail == f"removed {worktree}"
        assert not worktree.exists()
    finally:
        if sandbox.poll() is None:
            sandbox.kill()


def test_a_worktree_left_in_place_is_not_torn_down(tmp_path: Path) -> None:
    plan = SimpleNamespace(has_worktree=False, worktree=tmp_path / "gone")
    step = finish.stop_worktree_processes(plan)  # type: ignore[arg-type]
    assert step.skipped and step.detail == "no worktree to remove"


# ----- B: reading a session's transcript ----------------------------------------------


def at(minute: int, second: int = 0) -> str:
    return f"2026-09-24T19:{minute:02d}:{second:02d}.000Z"


def moment(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 24, 19, minute, second, tzinfo=timezone.utc)


def prompt(when: str, text: str, origin: str = "human") -> Dict[str, Any]:
    return {
        "type": "user",
        "timestamp": when,
        "origin": {"kind": origin},
        "message": {"role": "user", "content": text},
    }


def assistant(when: str) -> Dict[str, Any]:
    return {"type": "assistant", "timestamp": when, "message": {"role": "assistant", "content": []}}


def tool_result(when: str) -> Dict[str, Any]:
    return {
        "type": "user",
        "timestamp": when,
        "message": {"content": [{"type": "tool_result", "content": "ok"}]},
    }


def turn_end(when: str) -> Dict[str, Any]:
    return {"type": "system", "subtype": "turn_duration", "timestamp": when}


def transcript(tmp_path: Path, events: List[Dict[str, Any]]) -> Path:
    path = tmp_path / "66b55e81-1fcb-4967-99cc-678f7b262089.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


INCIDENT = [
    prompt(at(0), "You are the agent `claude` working task `task-557`..."),
    assistant(at(1)),
    tool_result(at(2)),
    prompt(at(20), "<task-notification>...</task-notification>", origin="task-notification"),
    assistant(at(25, 30)),
    turn_end(at(25, 47)),
]


class TestReadTranscript:
    def test_the_incident_after_handoff_is_idle_and_unaddressed(self, tmp_path: Path) -> None:
        reading = read_transcript(
            transcript(tmp_path, INCIDENT), run_started=moment(0), review_handoff=moment(25, 36)
        )
        assert reading.mid_turn is False
        assert reading.human_at is None

    def test_the_owner_writing_after_the_handoff_protects_it(self, tmp_path: Path) -> None:
        events = INCIDENT + [
            prompt(at(46, 21), "why has this session not ended?"),
            assistant(at(47)),
            turn_end(at(47, 33)),
        ]
        reading = read_transcript(
            transcript(tmp_path, events), run_started=moment(0), review_handoff=moment(25, 36)
        )
        assert reading.mid_turn is False
        assert reading.human_at == "2026-09-24T19:46:21+00:00"

    def test_without_a_review_handoff_the_dispatch_prompt_does_not_count(
        self, tmp_path: Path
    ) -> None:
        reading = read_transcript(
            transcript(tmp_path, INCIDENT), run_started=moment(0), review_handoff=None
        )
        assert reading.human_at is None

    def test_a_turn_still_open_is_mid_turn(self, tmp_path: Path) -> None:
        events = INCIDENT + [prompt(at(30), "<task-notification/>", origin="task-notification")]
        reading = read_transcript(
            transcript(tmp_path, events), run_started=moment(0), review_handoff=None
        )
        assert reading.mid_turn is True
        assert reading.human_at is None

    def test_a_queued_message_after_the_turn_is_mid_turn(self, tmp_path: Path) -> None:
        events = INCIDENT + [
            {"type": "queue-operation", "operation": "enqueue", "timestamp": at(30)}
        ]
        reading = read_transcript(
            transcript(tmp_path, events), run_started=moment(0), review_handoff=None
        )
        assert reading.mid_turn is True

    def test_an_unknown_origin_counts_as_a_person(self, tmp_path: Path) -> None:
        events = INCIDENT + [prompt(at(30), "hello", origin="something-new"), turn_end(at(31))]
        reading = read_transcript(
            transcript(tmp_path, events), run_started=moment(0), review_handoff=moment(25, 36)
        )
        assert reading.human_at is not None

    def test_an_empty_transcript_proves_nothing(self, tmp_path: Path) -> None:
        reading = read_transcript(transcript(tmp_path, []), run_started=None, review_handoff=None)
        assert reading.mid_turn is True


# ----- B: the backstop ---------------------------------------------------------------


class Fake:
    """A run's world, with every act recorded."""

    def __init__(
        self, tmp_path: Path, events: List[Dict[str, Any]], *, closed: bool = True
    ) -> None:
        self.path = transcript(tmp_path, events)
        old = time.time() - 120
        os.utime(self.path, (old, old))
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir()
        (self.run_dir / "meta.yaml").write_text("status: running\n", encoding="utf-8")
        self.stops: List[str] = []
        self.results: List[str] = []
        self.notes: List[Tuple[str, Dict[str, Any]]] = []
        self.task = SimpleNamespace(
            is_open=not closed,
            log=[
                SimpleNamespace(
                    type="handoff",
                    ts=moment(25, 36),
                    data={"ball": "human", "ball_reason": "review"},
                )
            ],
        )
        self.now = moment(32, 30)

    def record(self, **changes: Any) -> RunRecord:
        fields: Dict[str, Any] = dict(
            run_id="run_3acd7e91",
            path=self.run_dir,
            task_id="task-557",
            project_id="agentjobs",
            mode="session",
            status="running",
            session_id="66b55e81",
            started_at=moment(0),
            slot_released_at=moment(31, 54),
            slot_released_reason="task_closed",
        )
        fields.update(changes)
        return RunRecord(**fields)

    def deps(self) -> Deps:
        import yaml

        return Deps(
            task=lambda record: self.task,
            transcript=lambda record: self.path,
            stop=lambda record: (
                recorder(self.stops, key="run_id")(record),
                "stopped session 66b55e81",
            ),
            conclude=lambda record, body: self.results.append(body),
            note=lambda record, body, data: self.notes.append((body, data)),
            meta=lambda record: yaml.safe_load((self.run_dir / "meta.yaml").read_text()) or {},
            now=lambda: self.now,
        )


class TestEndLingering:
    def test_an_idle_unaddressed_session_is_stopped_and_concluded(self, tmp_path: Path) -> None:
        fake = Fake(tmp_path, INCIDENT)
        said = end_lingering(fake.record(), fake.deps())
        assert said == "stopped its lingering session: stopped session 66b55e81"
        assert fake.stops == ["run_3acd7e91"]
        assert len(fake.results) == 1 and "task-566" in fake.results[0]
        assert "claude --resume 66b55e81" in fake.results[0]
        assert fake.notes == []

    def test_a_session_a_person_wrote_to_is_left_and_the_record_says_why(
        self, tmp_path: Path
    ) -> None:
        events = INCIDENT + [
            prompt(at(29), "one more thing"),
            assistant(at(29, 30)),
            turn_end(at(30)),
        ]
        fake = Fake(tmp_path, events)
        record = fake.record()
        said = end_lingering(record, fake.deps())
        assert said is not None and said.startswith("left running: a person wrote to it")
        assert fake.stops == [] and fake.results == []
        assert len(fake.notes) == 1
        body, data = fake.notes[0]
        assert "task-482" in body and "after its review handoff" in body
        assert data == {"run_id": "run_3acd7e91", "stop_skipped": "human_message"}
        # Once, not every tick.
        assert end_lingering(record, fake.deps()) is None
        assert len(fake.notes) == 1

    def test_not_before_the_grace_period(self, tmp_path: Path) -> None:
        fake = Fake(tmp_path, INCIDENT)
        fake.now = moment(31, 54) + timedelta(seconds=closed_sessions.GRACE_SECONDS - 1)
        assert end_lingering(fake.record(), fake.deps()) is None
        assert fake.stops == []

    def test_never_mid_turn(self, tmp_path: Path) -> None:
        fake = Fake(tmp_path, INCIDENT + [assistant(at(32))])
        assert end_lingering(fake.record(), fake.deps()) is None
        assert fake.stops == []

    def test_not_while_the_transcript_is_still_being_written(self, tmp_path: Path) -> None:
        fake = Fake(tmp_path, INCIDENT)
        os.utime(fake.path, None)  # written just now
        assert end_lingering(fake.record(), fake.deps()) is None
        assert fake.stops == []

    def test_the_transcript_is_read_again_before_the_stop(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fake = Fake(tmp_path, INCIDENT)
        real = closed_sessions.read_transcript
        calls: List[int] = []

        def moving(path: Path, **kwargs: Any) -> Any:
            reading = real(path, **kwargs)
            calls.append(1)
            if len(calls) == 2:  # something was written between the two reads
                return closed_sessions.TranscriptReading(False, None, reading.mtime + 1)
            return reading

        monkeypatch.setattr(closed_sessions, "read_transcript", moving)
        assert end_lingering(fake.record(), fake.deps()) is None
        assert len(calls) == 2 and fake.stops == []

    def test_a_task_that_is_open_again_is_left_alone(self, tmp_path: Path) -> None:
        fake = Fake(tmp_path, INCIDENT, closed=False)
        assert end_lingering(fake.record(), fake.deps()) is None

    def test_only_a_slot_released_because_the_task_closed(self, tmp_path: Path) -> None:
        fake = Fake(tmp_path, INCIDENT)
        assert end_lingering(fake.record(slot_released_at=None), fake.deps()) is None
        assert end_lingering(fake.record(mode="interactive"), fake.deps()) is None
        assert fake.stops == []

    def test_a_failed_stop_concludes_nothing(self, tmp_path: Path) -> None:
        fake = Fake(tmp_path, INCIDENT)
        deps = fake.deps()
        deps.stop = lambda record: (False, "`stop 66b55e81` exited 1")
        said = end_lingering(fake.record(), deps)
        assert said == "could not stop its lingering session: `stop 66b55e81` exited 1"
        assert fake.results == []


def test_the_suite_never_finds_a_real_transcript() -> None:
    assert closed_sessions.TRANSCRIPT_FINDER("66b55e81", Path(".")) is None
