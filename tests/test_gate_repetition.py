"""What stops a run paying for the same gate twice, and what makes the bill visible.

Task-339. task-336 added a tab indicator to the React app and took sixty-five minutes to
reach review. Twenty-five of those were the work. Forty-eight were the gate, launched
**eight** times: a full gate on a stage already known red, two more full gates chained
back to back over identical code, and a ``--since-gate`` that found no receipt and fell
back to all ten stages because two untracked sandbox files were lying about.

Three of those eight were avoidable by a sequence written down in ENGINEERING.md, and
that half of the fix is prose. This is the other half: the tooling that says, in the
transcript, that a gate is about to re-answer a question this run already has an answer
to -- and, afterwards, what the run really paid.

The properties guarded here:

* **it warns, it never refuses.** A gate that declines to run is a new way for an agent
  to be stuck, and there are honest reasons to re-run one.
* **"this exact tree" means the tree, not the commit.** Half of task-336's repeats were
  over uncommitted code, so a fingerprint that stopped at ``HEAD`` would have missed them
  and a fingerprint that stopped at tracked files would have missed the sandbox scripts.
* **a gate that was abandoned is a gate that was paid for.** ``gate_finished`` recorded
  six of task-336's eight, and the two it missed were the two longest blocks in the run.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, alias: str | None = None) -> ModuleType:
    """Load a repository script by path, without making ``scripts/`` a package."""
    spec = importlib.util.spec_from_file_location(alias or name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = load_script("check", "check_under_repetition_test")
gate_scope = load_script("gate_scope", "gate_scope_under_repetition_test")
run_report = load_script("run_report", "run_report_under_repetition_test")


NOW = datetime(2026, 9, 5, 20, 13, 28, tzinfo=timezone.utc)
"""The moment task-336 launched its sixth gate, six minutes after its fourth went green."""


def green(tree: str, *, ts: str = "2026-09-05T20:07:08+00:00", seconds: float = 403.5) -> dict:
    """A ``gate_finished`` record for a full green gate over ``tree``."""
    return {
        "ts": ts,
        "kind": "gate_finished",
        "scope": "full",
        "passed": True,
        "seconds": seconds,
        "stages_run": 10,
        "stages_total": 10,
        "tree": tree,
    }


# --- the tree fingerprint -----------------------------------------------------------


class TestTheFingerprint:
    """What identifies "the thing this gate is about to verify"."""

    @staticmethod
    def git(root: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    @pytest.fixture()
    def repository(self, tmp_path: Path) -> Path:
        root = tmp_path / "repo"
        root.mkdir()
        self.git(root, "init", "-q")
        self.git(root, "config", "user.email", "gate@example.test")
        self.git(root, "config", "user.name", "Gate")
        (root / "source.py").write_text("value = 1\n", encoding="utf-8")
        self.git(root, "add", "source.py")
        self.git(root, "commit", "-qm", "first")
        return root

    def test_an_unchanged_tree_fingerprints_the_same_twice(self, repository: Path) -> None:
        """Otherwise the notice could never fire, however little had changed."""
        assert gate_scope.tree_fingerprint(repository) == gate_scope.tree_fingerprint(repository)

    def test_an_uncommitted_edit_changes_it(self, repository: Path) -> None:
        """The case ``HEAD`` alone would miss, and half of task-336's repeats.

        Gates 3 and 4 of that run were over the same *uncommitted* code; gate 6 was over
        the committed form of it. A fingerprint that stopped at the commit would have
        called all three the same tree and warned on gate 6 wrongly.
        """
        before = gate_scope.tree_fingerprint(repository)
        (repository / "source.py").write_text("value = 2\n", encoding="utf-8")

        assert gate_scope.tree_fingerprint(repository) != before

    def test_committing_that_edit_changes_it_again(self, repository: Path) -> None:
        (repository / "source.py").write_text("value = 2\n", encoding="utf-8")
        dirty = gate_scope.tree_fingerprint(repository)
        self.git(repository, "commit", "-qam", "second")

        assert gate_scope.tree_fingerprint(repository) != dirty

    def test_an_untracked_file_changes_it(self, repository: Path) -> None:
        """The sandbox scripts task-336 left lying about were untracked, not modified."""
        before = gate_scope.tree_fingerprint(repository)
        (repository / "sandbox.py").write_text("print('scratch')\n", encoding="utf-8")

        assert gate_scope.tree_fingerprint(repository) != before

    def test_editing_an_untracked_file_changes_it(self, repository: Path) -> None:
        """Its contents, not merely its name: it is source the next commit will carry."""
        (repository / "sandbox.py").write_text("print('scratch')\n", encoding="utf-8")
        before = gate_scope.tree_fingerprint(repository)
        (repository / "sandbox.py").write_text("print('different')\n", encoding="utf-8")

        assert gate_scope.tree_fingerprint(repository) != before

    def test_somewhere_that_is_not_a_checkout_answers_nothing(self, tmp_path: Path) -> None:
        """None means "cannot tell", and every caller reads it that way rather than
        as "unchanged" -- a fingerprint that guessed would produce a false notice."""
        assert gate_scope.tree_fingerprint(tmp_path) is None


# --- the notice ---------------------------------------------------------------------


class TestAlreadyGreen:
    """The sentence task-336's fourth, sixth and eighth gate launches never printed."""

    def test_a_matching_green_full_gate_is_named_with_its_moment(self) -> None:
        notice = check.already_green("abc123", [green("abc123")], now=NOW)

        assert notice is not None
        assert "ALREADY GREEN" in notice
        assert "2026-09-05T20:07:08+00:00" in notice
        assert "403.5s" in notice

    def test_it_says_how_long_ago_rather_than_only_when(self) -> None:
        """The age is the number a reader acts on: six seconds ago and two hours ago are
        the difference between a wasted gate and a reasonable re-run, and a bare
        timestamp makes them do the arithmetic mid-banner."""
        notice = check.already_green("abc123", [green("abc123")], now=NOW)

        assert notice is not None and "6 minutes ago" in notice

    def test_a_timestamp_it_cannot_parse_still_produces_the_notice(self) -> None:
        """The notice is the point; the age is a convenience on top of it."""
        record = green("abc123")
        record["ts"] = "some time on Tuesday"

        notice = check.already_green("abc123", [record], now=NOW)

        assert notice is not None and "ALREADY GREEN" in notice

    def test_a_different_tree_says_nothing(self) -> None:
        assert check.already_green("abc123", [green("def456")]) is None

    def test_a_failed_gate_over_the_same_tree_says_nothing(self) -> None:
        """Re-running a red gate after a fix is the loop this must not nag about."""
        record = green("abc123")
        record["passed"] = False

        assert check.already_green("abc123", [record]) is None

    def test_a_partial_green_over_the_same_tree_says_nothing(self) -> None:
        """``--only e2e`` passing says nothing about the other nine stages, which is
        the same rule ``PARTIAL RUN`` states and why no receipt is issued for one."""
        record = green("abc123")
        record["scope"] = "partial"

        assert check.already_green("abc123", [record]) is None

    def test_an_unknowable_fingerprint_says_nothing(self) -> None:
        assert check.already_green(None, [green("abc123")]) is None

    def test_the_most_recent_matching_gate_is_the_one_quoted(self) -> None:
        """A run gates the same tree three times; the reader wants the last answer."""
        notice = check.already_green(
            "abc123",
            [
                green("abc123", ts="2026-09-05T20:00:00+00:00"),
                green("abc123", ts="2026-09-05T20:07:08+00:00"),
            ],
        )

        assert notice is not None and "20:07:08" in notice

    def test_it_points_at_the_sequence_rather_than_only_complaining(self) -> None:
        notice = check.already_green("abc123", [green("abc123")])

        assert notice is not None
        assert "--only" in notice and "ENGINEERING.md" in notice


class TestTheNoticeNeverRefuses:
    """Task-339's constraint, in the one place it could quietly stop being true."""

    @staticmethod
    def stub(monkeypatch: pytest.MonkeyPatch, records: list[dict]) -> list[list[str]]:
        commands: list[list[str]] = []

        def record(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(check.subprocess, "run", record)
        monkeypatch.setattr(check, "setup_problems", lambda root, origin: [])
        monkeypatch.setattr(check.shutil, "which", lambda name: "npm.cmd")
        monkeypatch.setattr(check, "own_phases", lambda: records)
        monkeypatch.setattr(check.gate_scope, "tree_fingerprint", lambda root: "abc123")
        monkeypatch.setattr(check.gate_scope, "write_receipt", lambda *a, **k: Path("receipt"))
        monkeypatch.setattr(check.gate_scope, "head_commit", lambda root: "b" * 40)
        monkeypatch.setattr(check.gate_scope, "tree_is_clean", lambda root: True)
        return commands

    def test_every_stage_still_runs_and_the_run_is_still_green(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        commands = self.stub(monkeypatch, [green("abc123")])

        assert check.main([]) == 0
        # One command per step, not per stage: `api` is two since task-268.
        assert len(commands) == sum(len(stage.steps) for stage in check.stages())
        assert "ALREADY GREEN" in capsys.readouterr().out

    def test_nothing_is_printed_when_the_tree_has_moved(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.stub(monkeypatch, [green("something-else")])

        assert check.main([]) == 0
        assert "ALREADY GREEN" not in capsys.readouterr().out

    def test_a_partial_run_is_not_nagged_at(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--only`` is the loop the sequence *wants* an agent in. Warning there would
        train the reader to ignore the banner in the one case it is about."""
        self.stub(monkeypatch, [green("abc123")])

        assert check.main(["--only", "black"]) == 0
        assert "ALREADY GREEN" not in capsys.readouterr().out

    def test_an_empty_ledger_says_nothing(self) -> None:
        """The ordinary case: a gate run by hand, outside any dispatched run."""
        assert check.already_green("abc123", []) is None


class TestOwnPhasesIsSafe:
    """``own_phases`` is symmetrical with ``record_phase``: it may never raise.

    Instrumentation that can break the thing it measures is worse than none, and this
    one runs before every gate on the machine.
    """

    def test_outside_a_dispatched_run_it_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTJOBS_RUN_DIR", raising=False)

        assert check.own_phases() == []

    def test_a_run_directory_that_is_gone_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTJOBS_RUN_DIR", str(ROOT / "nowhere-at-all"))

        assert check.own_phases() == []

    def test_a_torn_ledger_line_is_skipped_rather_than_raised(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """These files are appended to by several processes over a run's life."""
        (tmp_path / "phases.jsonl").write_text(
            json.dumps(green("abc123")) + "\n{ this line is torn\n", encoding="utf-8"
        )
        monkeypatch.setenv("AGENTJOBS_RUN_DIR", str(tmp_path))

        records = check.own_phases()

        assert [record["kind"] for record in records] == ["gate_finished"]


# --- the paths that stopped a receipt ------------------------------------------------


class TestNamingWhatBlockedAReceipt:
    """task-336's gate 5 cost ten minutes for want of two files nobody had named."""

    def test_a_refusal_for_want_of_a_receipt_names_the_dirty_paths(self) -> None:
        scope = gate_scope.Scope(
            None,
            None,
            [],
            {},
            "no gate receipt in this checkout",
            blocking=[("scripts/sandbox.py", "untracked"), ("ENGINEERING.md", "M in git status")],
        )

        rendered = gate_scope.render(scope, ["black", "pytest"])

        assert "FULL GATE" in rendered
        assert "scripts/sandbox.py" in rendered
        assert "untracked" in rendered
        assert "ENGINEERING.md" in rendered

    def test_a_refusal_with_a_clean_tree_says_nothing_extra(self) -> None:
        """There is nothing to name, and inventing a remedy would be noise."""
        scope = gate_scope.Scope(None, None, [], {}, "no gate receipt in this checkout")

        rendered = gate_scope.render(scope, ["black", "pytest"])

        assert "stop this tree being a commit" not in rendered

    def test_a_dirty_tree_declining_a_receipt_names_them_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other end of the same problem: the run that failed to *earn* one."""
        monkeypatch.setattr(check.gate_scope, "head_commit", lambda root: "b" * 40)
        monkeypatch.setattr(check.gate_scope, "tree_is_clean", lambda root: False)
        monkeypatch.setattr(
            check.gate_scope, "dirty_paths", lambda root: [("scripts/sandbox.py", "untracked")]
        )

        message = check.issue_receipt(None)

        assert "No gate receipt written" in message
        assert "scripts/sandbox.py" in message

    def test_dirty_paths_tells_untracked_from_modified(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        for args in (
            ("init", "-q"),
            ("config", "user.email", "gate@example.test"),
            ("config", "user.name", "Gate"),
        ):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
        (root / "tracked.py").write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.py"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "first"], cwd=root, check=True, capture_output=True)
        (root / "tracked.py").write_text("value = 2\n", encoding="utf-8")
        (root / "sandbox.py").write_text("scratch\n", encoding="utf-8")

        found = dict(gate_scope.dirty_paths(root))

        assert found["sandbox.py"] == "untracked"
        assert found["tracked.py"] != "untracked"


# --- abandoned gates -----------------------------------------------------------------


def phases(directory: Path, records: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "phases.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def started(ts: str, **fields: object) -> dict:
    return {"ts": ts, "kind": "gate_started", "scope": "full", "stages_total": 10, **fields}


def finished(ts: str, seconds: float, **fields: object) -> dict:
    return {
        "ts": ts,
        "kind": "gate_finished",
        "scope": "full",
        "passed": True,
        "seconds": seconds,
        "stages_run": 10,
        "stages_total": 10,
        **fields,
    }


class TestAbandonedGates:
    """A gate that was killed was still paid for, and used to be invisible."""

    def test_a_started_gate_with_no_finish_is_counted(self, tmp_path: Path) -> None:
        phases(
            tmp_path,
            [
                started("2026-09-05T19:50:12+00:00"),
                {"ts": "2026-09-05T19:57:00+00:00", "kind": "gate_stage_started", "stage": "e2e"},
                started("2026-09-05T20:00:24+00:00"),
                finished("2026-09-05T20:07:08+00:00", 403.5),
            ],
        )

        gates = run_report.read_gates(tmp_path)

        assert len(gates) == 2
        assert gates[0].abandoned and not gates[1].abandoned

    def test_its_duration_runs_to_the_next_thing_the_run_did(self, tmp_path: Path) -> None:
        """task-336's real numbers: 19:50:12 to 20:00:24 is 612 seconds, which is the
        Bash tool's 600-second cap plus start-up and the ten-minute hole in its
        timeline."""
        phases(
            tmp_path,
            [
                started("2026-09-05T19:50:12+00:00"),
                started("2026-09-05T20:00:24+00:00"),
                finished("2026-09-05T20:07:08+00:00", 403.5),
            ],
        )

        assert run_report.read_gates(tmp_path)[0].seconds == pytest.approx(612.0)

    def test_it_names_the_stage_it_died_in(self, tmp_path: Path) -> None:
        """The useful half of "it was killed": both of task-336's died in e2e, having
        paid for the nine stages in front of it."""
        phases(
            tmp_path,
            [
                started("2026-09-05T19:50:12+00:00"),
                {"ts": "2026-09-05T19:51:00+00:00", "kind": "gate_stage_started", "stage": "black"},
                {"ts": "2026-09-05T19:57:00+00:00", "kind": "gate_stage_started", "stage": "e2e"},
                started("2026-09-05T20:00:24+00:00"),
                finished("2026-09-05T20:07:08+00:00", 403.5),
            ],
        )

        assert run_report.read_gates(tmp_path)[0].failed_stage == "e2e"

    def test_an_abandoned_gate_is_never_counted_as_passed(self, tmp_path: Path) -> None:
        phases(
            tmp_path, [started("2026-09-05T19:50:12+00:00"), started("2026-09-05T20:00:24+00:00")]
        )

        assert run_report.read_gates(tmp_path)[0].passed is False

    def test_it_counts_towards_the_time_thrown_away(self, tmp_path: Path) -> None:
        phases(
            tmp_path, [started("2026-09-05T19:50:12+00:00"), started("2026-09-05T20:00:24+00:00")]
        )
        run = run_report.Run(
            run_id="run_x",
            task_id="task-336",
            outcome="completed",
            started_at=None,
            finished_at=None,
            gates=run_report.read_gates(tmp_path),
        )

        assert run.wasted_gate_seconds == pytest.approx(612.0)
        assert run.abandoned_gates == 1

    def test_a_gate_whose_start_has_no_timestamp_is_still_dropped(self, tmp_path: Path) -> None:
        """Here the honest answer really is that nothing is known, and
        ``RunRecord.elapsed_seconds``'s rule -- never print an unknown as a number --
        still binds."""
        phases(
            tmp_path,
            [{"kind": "gate_started", "scope": "full"}, started("2026-09-05T20:00:24+00:00")],
        )

        assert [gate.abandoned for gate in run_report.read_gates(tmp_path)] == []

    def test_a_completed_gate_still_reports_its_own_recorded_seconds(self, tmp_path: Path) -> None:
        """The measured number wins over any arithmetic over timestamps; the gate is the
        only party that knows when it really began work."""
        phases(
            tmp_path,
            [started("2026-09-05T20:00:24+00:00"), finished("2026-09-05T20:07:08+00:00", 403.5)],
        )

        assert run_report.read_gates(tmp_path)[0].seconds == pytest.approx(403.5)

    def test_the_listing_says_abandoned_rather_than_failed(self, tmp_path: Path) -> None:
        """Two different facts. A failed gate found something; an abandoned one was
        killed, and conflating them hides which of the two an agent should act on."""
        phases(
            tmp_path, [started("2026-09-05T19:50:12+00:00"), started("2026-09-05T20:00:24+00:00")]
        )
        run = run_report.Run(
            run_id="run_x",
            task_id="task-336",
            outcome="completed",
            started_at=None,
            finished_at=None,
            gates=run_report.read_gates(tmp_path),
        )

        rendered = run_report.listing([run])

        assert "1 abandoned" in rendered
        assert "failed" not in rendered
