"""Portable validation, managed-write receipts, and the staged commit gate.

The point of this layer is to be loud where the Codex hook cannot see. So the tests
are mostly "put a specific defect in a file and check the report names it, by filename
and by rule" -- a validator whose message does not identify the file is a validator
people learn to ignore.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

import pytest
import yaml
from typer.testing import CliRunner

from agentjobs.cli import app
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle, Task
from agentjobs.receipts import DISABLE_ENV, ReceiptStore, content_hash
from agentjobs.storage import TaskStorage
from agentjobs.validation import (
    OVERRIDE_ENV,
    check_staged_receipts,
    override_reason,
    validate_corpus,
)

CONFIG: dict[str, object] = {
    "project_name": "Fixture",
    "tasks_directory": "tasks",
    "categories": ["general", "infrastructure"],
    "actors": [
        {"name": "Ada", "kind": "human", "display_name": "Ada Lovelace"},
        {"name": "bot", "kind": "agent", "display_name": "Bot"},
    ],
    "default_user": "Ada",
}


@pytest.fixture()
def project(tmp_path: Path) -> Iterator[Tuple[Path, TaskManager]]:
    """A project directory with config and an empty tasks directory."""
    (tmp_path / ".agentjobs").mkdir(parents=True)
    (tmp_path / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    manager = TaskManager(TaskStorage(tmp_path / "tasks"))
    yield tmp_path, manager


def ready(manager: TaskManager, task_id: str = "task-001-work", **kwargs: Any) -> Task:
    """A valid ready task."""
    return manager.create_task(
        id=task_id,
        title="Work",
        description="Do the thing.",
        category="general",
        lifecycle=Lifecycle.READY,
        **kwargs,
    )


def write_raw(root: Path, name: str, payload: Dict[str, Any]) -> Path:
    """Write a task file by hand, exactly as a direct editor would."""
    path = root / "tasks" / name
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def valid_payload(task_id: str, **overrides: Any) -> Dict[str, Any]:
    """A minimal valid schema-v2 task document."""
    now = datetime(2026, 8, 10, tzinfo=timezone.utc).isoformat()
    payload = {
        "schema": 2,
        "id": task_id,
        "title": "Hand written",
        "created": now,
        "updated": now,
        "lifecycle": "ready",
        "ball": "agent",
        "ball_reason": "available",
        "priority": "medium",
        "category": "general",
        "spec": {"summary": "s", "description": "d"},
    }
    # Derived from the id rather than fixed, so a fixture project holding several of
    # these does not trip the duplicate-position check it is not testing.
    payload["queue_position"] = int(task_id.split("-")[1]) * 100
    payload.update(overrides)
    if payload.get("lifecycle") == "closed":
        payload.pop("queue_position", None)
    return payload


def report_for(project) -> Any:
    """Validate the fixture project."""
    root, _ = project
    return validate_corpus(root / "tasks", project_config=CONFIG, project_root=root)


def rules(report) -> set:
    """The rule names a report fired."""
    return {finding.rule for finding in report.findings}


# ---------------------------------------------------------------------------
# ac-1: the semantic checks
# ---------------------------------------------------------------------------
class TestCleanCorpus:
    def test_a_manager_written_corpus_validates(self, project):
        _, manager = project
        ready(manager)
        ready(manager, "task-002-more")

        report = report_for(project)

        assert report.ok, report.render()
        assert report.checked == 2

    def test_an_empty_corpus_validates(self, project):
        assert report_for(project).ok


class TestUnreadableFiles:
    def test_a_file_that_will_not_parse_is_reported_by_name(self, project):
        root, _ = project
        (root / "tasks").mkdir(exist_ok=True)
        (root / "tasks" / "task-900-broken.yaml").write_text("id: [unclosed\n", encoding="utf-8")

        report = report_for(project)

        assert "unreadable" in rules(report)
        assert any("task-900-broken.yaml" == f.filename for f in report.findings)

    def test_an_unmigrated_v1_file_is_reported(self, project):
        root, _ = project
        (root / "tasks").mkdir(exist_ok=True)
        (root / "tasks" / "task-901-old.yaml").write_text(
            "id: task-901-old\nstatus: in_progress\n", encoding="utf-8"
        )

        assert "unreadable" in rules(report_for(project))


class TestModelEnforcedInvariants:
    """The state and log rules are enforced by the Task model, not re-checked here.

    A file that breaks one cannot load, so it arrives as `unreadable` carrying the
    model's own message, which already names the offending field. These cases prove
    that path works -- the validator is loud about them -- without a second copy of
    the rules that could drift from the first.
    """

    @pytest.mark.parametrize(
        "name,payload,expected",
        [
            (
                "task-902-noball",
                {"lifecycle": "active", "ball": None, "ball_reason": None},
                "ball",
            ),
            (
                "task-903-closedball",
                {"lifecycle": "closed", "outcome": "completed"},
                "ball",
            ),
            (
                "task-904-noout",
                {"lifecycle": "closed", "ball": None, "ball_reason": None},
                "outcome",
            ),
            (
                "task-905-noowner",
                {"lifecycle": "active", "ball_reason": "work"},
                "ball_prompt",
            ),
            (
                "task-906-noask",
                {"ball": "human", "ball_reason": "review"},
                "ball_prompt",
            ),
        ],
    )
    def test_a_broken_state_record_is_reported_by_file_and_field(
        self, project, name, payload, expected
    ):
        root, _ = project
        (root / "tasks").mkdir(exist_ok=True)
        document = valid_payload(name, **payload)
        document = {key: value for key, value in document.items() if value is not None}
        write_raw(root, f"{name}.yaml", document)

        report = report_for(project)

        finding = next(item for item in report.findings if item.filename == f"{name}.yaml")
        assert finding.rule == "unreadable"
        assert expected in finding.message

    def test_the_open_task_with_no_ball_is_the_record_that_started_this(self, project):
        """The original failure: lifecycle active, no ball, invisible to every listing."""
        root, _ = project
        (root / "tasks").mkdir(exist_ok=True)
        document = valid_payload("task-902-original", lifecycle="active")
        document.pop("ball")
        document.pop("ball_reason")
        write_raw(root, "task-902-original.yaml", document)

        report = report_for(project)

        assert not report.ok
        assert report.checked == 1
        assert any("task-902-original.yaml" == item.filename for item in report.findings)

    @pytest.mark.parametrize(
        "name,log",
        [
            (
                "task-907-dupe",
                [
                    {"id": 1, "ts": "2026-08-10T00:00:00+00:00", "actor": "bot", "type": "note"},
                    {"id": 1, "ts": "2026-08-10T00:01:00+00:00", "actor": "bot", "type": "note"},
                ],
            ),
            (
                "task-908-order",
                [
                    {"id": 5, "ts": "2026-08-10T00:00:00+00:00", "actor": "bot", "type": "note"},
                    {"id": 2, "ts": "2026-08-10T00:01:00+00:00", "actor": "bot", "type": "note"},
                ],
            ),
            (
                "task-909-thread",
                [
                    {
                        "id": 1,
                        "ts": "2026-08-10T00:00:00+00:00",
                        "actor": "bot",
                        "type": "answer",
                        "re": 99,
                    }
                ],
            ),
        ],
    )
    def test_a_broken_log_is_reported(self, project, name, log):
        root, _ = project
        (root / "tasks").mkdir(exist_ok=True)
        write_raw(root, f"{name}.yaml", valid_payload(name, log=log))

        report = report_for(project)

        finding = next(item for item in report.findings if item.filename == f"{name}.yaml")
        assert finding.rule == "unreadable"
        assert "log" in finding.message


class TestTaxonomy:
    def test_an_unconfigured_category_is_reported(self, project):
        root, _ = project
        (root / "tasks").mkdir(exist_ok=True)
        write_raw(root, "task-910-cat.yaml", valid_payload("task-910-cat", category="invented"))

        assert "unknown-category" in rules(report_for(project))

    def test_an_unconfigured_actor_is_reported(self, project):
        root, _ = project
        (root / "tasks").mkdir(exist_ok=True)
        write_raw(
            root,
            "task-911-actor.yaml",
            valid_payload(
                "task-911-actor",
                log=[
                    {"id": 1, "ts": "2026-08-10T00:00:00+00:00", "actor": "ghost", "type": "note"}
                ],
            ),
        )

        assert "unknown-actor" in rules(report_for(project))

    def test_a_project_with_no_declared_taxonomy_is_not_in_violation(self, project):
        """A project that never set a policy cannot be breaking it."""
        root, _ = project
        (root / "tasks").mkdir(exist_ok=True)
        write_raw(root, "task-912-any.yaml", valid_payload("task-912-any", category="invented"))

        report = validate_corpus(root / "tasks", project_config={}, project_root=root)

        assert "unknown-category" not in rules(report)


class TestRelationships:
    def test_a_missing_parent_is_reported(self, project):
        root, _ = project
        (root / "tasks").mkdir(exist_ok=True)
        write_raw(
            root, "task-913-orphan.yaml", valid_payload("task-913-orphan", parent="task-nope")
        )

        assert "missing-parent" in rules(report_for(project))

    def test_a_missing_dependency_is_reported(self, project):
        root, _ = project
        (root / "tasks").mkdir(exist_ok=True)
        write_raw(
            root,
            "task-914-dangling.yaml",
            valid_payload(
                "task-914-dangling", dependencies=[{"task": "task-nope", "type": "needs"}]
            ),
        )

        assert "missing-dependency" in rules(report_for(project))

    def test_a_self_dependency_is_reported(self, project):
        root, _ = project
        (root / "tasks").mkdir(exist_ok=True)
        write_raw(
            root,
            "task-915-self.yaml",
            valid_payload(
                "task-915-self", dependencies=[{"task": "task-915-self", "type": "needs"}]
            ),
        )

        assert "self-dependency" in rules(report_for(project))

    def test_a_needs_cycle_is_reported(self, project):
        """The silent deadlock: both tasks look ready and neither can ever be claimed."""
        root, _ = project
        (root / "tasks").mkdir(exist_ok=True)
        write_raw(
            root,
            "task-916-a.yaml",
            valid_payload("task-916-a", dependencies=[{"task": "task-917-b", "type": "needs"}]),
        )
        write_raw(
            root,
            "task-917-b.yaml",
            valid_payload("task-917-b", dependencies=[{"task": "task-916-a", "type": "needs"}]),
        )

        report = report_for(project)

        assert "dependency-cycle" in rules(report)
        assert any("permanently unclaimable" in f.message for f in report.findings)


class TestPathPolicy:
    def test_an_absolute_context_path_is_reported(self, project):
        root, _ = project
        (root / "tasks").mkdir(exist_ok=True)
        write_raw(
            root,
            "task-918-abs.yaml",
            valid_payload(
                "task-918-abs",
                spec={
                    "summary": "s",
                    "description": "d",
                    "context": [{"path": "/etc/passwd", "why": "should be relative"}],
                },
            ),
        )

        report = report_for(project)

        # Either path rule is a pass. A POSIX-style absolute path is not `absolute`
        # on Windows -- it has no drive -- so there it resolves relative to the drive
        # root and trips the escape rule instead. Both say the same useful thing, and
        # pinning one would make the test pass on one platform and fail on the other.
        assert rules(report) & {"absolute-path", "path-escapes-project"}, report.render()

    def test_a_path_escaping_the_project_is_reported(self, project):
        root, _ = project
        (root / "tasks").mkdir(exist_ok=True)
        write_raw(
            root,
            "task-919-escape.yaml",
            valid_payload(
                "task-919-escape",
                deliverables=[{"path": "../../elsewhere/file.txt", "note": "no"}],
            ),
        )

        assert "path-escapes-project" in rules(report_for(project))


class TestCanonicalSerialization:
    def test_a_hand_shaped_but_valid_file_is_reported(self, project):
        """Valid, loadable, and not what AgentJobs would have written."""
        root, manager = project
        task = ready(manager)
        path = root / "tasks" / f"{task.id}.yaml"
        content = path.read_text(encoding="utf-8")
        path.write_text("# hand edited\n" + content, encoding="utf-8")

        report = report_for(project)

        assert "non-canonical-serialization" in rules(report)

    def test_a_manager_written_file_is_canonical(self, project):
        _, manager = project
        ready(manager)

        assert "non-canonical-serialization" not in rules(report_for(project))


# ---------------------------------------------------------------------------
# ac-2: receipts
# ---------------------------------------------------------------------------
class TestReceipts:
    def test_a_managed_write_records_a_receipt_matching_the_file(self, project):
        root, manager = project
        task = ready(manager)

        store = ReceiptStore.for_tasks_directory(root / "tasks")
        receipt = store.latest(task.id)

        assert receipt is not None
        assert receipt.content_hash == content_hash(
            (root / "tasks" / f"{task.id}.yaml").read_bytes()
        )
        assert receipt.filename == f"{task.id}.yaml"
        assert receipt.version

    def test_every_verb_refreshes_the_receipt(self, project):
        """Whatever writes, the receipt tracks the current file."""
        root, manager = project
        task = ready(manager)
        store = ReceiptStore.for_tasks_directory(root / "tasks")
        first = store.latest(task.id).content_hash

        manager.claim_task(task.id, agent="bot")

        second = store.latest(task.id).content_hash
        assert second != first
        assert second == content_hash((root / "tasks" / f"{task.id}.yaml").read_bytes())

    def test_receipts_live_in_the_project_and_are_gitignored_by_pattern(self, project):
        root, manager = project
        ready(manager)

        directory = ReceiptStore.for_tasks_directory(root / "tasks").directory

        assert directory.is_dir()
        assert directory.parent.name == ".agentjobs"

    def test_a_hand_edit_does_not_match_the_receipt(self, project):
        root, manager = project
        task = ready(manager)
        path = root / "tasks" / f"{task.id}.yaml"
        path.write_text(path.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")

        store = ReceiptStore.for_tasks_directory(root / "tasks")

        assert not store.matches(task.id, path.read_bytes())

    def test_receipts_can_be_switched_off(self, project, monkeypatch):
        monkeypatch.setenv(DISABLE_ENV, "1")
        root, manager = project

        ready(manager)

        assert ReceiptStore.for_tasks_directory(root / "tasks").latest("task-001-work") is None

    def test_line_endings_do_not_change_the_hash(self):
        """Git may hand back CRLF; a gate that failed on that would just get bypassed."""
        assert content_hash(b"a: 1\r\nb: 2\r\n") == content_hash(b"a: 1\nb: 2\n")

    def test_a_failed_receipt_write_does_not_fail_the_task_write(self, project, monkeypatch):
        """Evidence is corroborating. Losing it must not lose the task."""
        root, manager = project

        def explode(*args, **kwargs):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(Path, "mkdir", explode)
        task = manager.create_task(
            id="task-920-resilient",
            title="Still written",
            description="d",
            category="general",
            lifecycle=Lifecycle.READY,
        )

        assert task.id == "task-920-resilient"


# ---------------------------------------------------------------------------
# ac-3: the staged gate
# ---------------------------------------------------------------------------
@pytest.fixture()
def repo(tmp_path: Path) -> Iterator[Tuple[Path, TaskManager]]:
    """A real git repository with an AgentJobs project inside it."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / ".agentjobs").mkdir()
    (tmp_path / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    yield tmp_path, TaskManager(TaskStorage(tmp_path / "tasks"))


def stage(repo_root: Path, path: Path) -> None:
    """Stage one file."""
    subprocess.run(
        ["git", "add", str(path.relative_to(repo_root).as_posix())], cwd=repo_root, check=True
    )


class TestStagedGate:
    def test_a_manager_written_staged_task_is_accepted(self, repo):
        root, manager = repo
        task = ready(manager)
        stage(root, root / "tasks" / f"{task.id}.yaml")

        assert check_staged_receipts(root, root / "tasks") == []

    def test_a_valid_looking_direct_edit_is_rejected(self, repo):
        """The check a schema-only validator structurally cannot make."""
        root, manager = repo
        task = ready(manager)
        path = root / "tasks" / f"{task.id}.yaml"
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        payload["priority"] = "critical"
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        stage(root, path)

        findings = check_staged_receipts(root, root / "tasks")

        assert [finding.rule for finding in findings] == ["receipt-mismatch"]

    def test_a_task_with_no_receipt_at_all_is_rejected(self, repo):
        root, _ = repo
        (root / "tasks").mkdir(exist_ok=True)
        path = write_raw(root, "task-921-byhand.yaml", valid_payload("task-921-byhand"))
        stage(root, path)

        findings = check_staged_receipts(root, root / "tasks")

        assert [finding.rule for finding in findings] == ["no-write-receipt"]
        assert "MCP tools" in findings[0].message

    def test_files_outside_the_tasks_directory_are_ignored(self, repo):
        root, _ = repo
        other = root / "config.yaml"
        other.write_text("a: 1\n", encoding="utf-8")
        stage(root, other)

        assert check_staged_receipts(root, root / "tasks") == []

    def test_an_unstaged_edit_is_not_checked(self, repo):
        """The gate is about what is being committed, not the working tree."""
        root, manager = repo
        task = ready(manager)
        path = root / "tasks" / f"{task.id}.yaml"
        path.write_text(path.read_text(encoding="utf-8") + "# later\n", encoding="utf-8")

        assert check_staged_receipts(root, root / "tasks") == []


# ---------------------------------------------------------------------------
# ac-4: the override
# ---------------------------------------------------------------------------
class TestOverride:
    def test_it_is_absent_by_default(self, monkeypatch):
        monkeypatch.delenv(OVERRIDE_ENV, raising=False)

        assert override_reason() is None

    def test_a_bare_flag_is_not_enough(self, monkeypatch):
        """It takes a reason, never a switch, so a shell history stays readable."""
        monkeypatch.setenv(OVERRIDE_ENV, "   ")

        assert override_reason() is None

    def test_a_stated_reason_is_returned(self, monkeypatch):
        monkeypatch.setenv(OVERRIDE_ENV, "restoring task-042 after a bad merge")

        assert override_reason() == "restoring task-042 after a bad merge"

    def test_the_override_bypasses_receipts_but_not_validation(self, repo, monkeypatch):
        """An emergency repair still may not commit a corpus that will not load."""
        root, _ = repo
        (root / "tasks").mkdir(exist_ok=True)
        write_raw(root, "task-922-invalid.yaml", valid_payload("task-922-invalid", parent="nope"))
        monkeypatch.setenv(OVERRIDE_ENV, "emergency")
        monkeypatch.chdir(root)

        result = CliRunner().invoke(app, ["validate", "--staged"])

        assert result.exit_code == 1
        assert "missing-parent" in result.output


# ---------------------------------------------------------------------------
# ac-5: the CLI, and the honesty about provenance
# ---------------------------------------------------------------------------
class TestCommand:
    def test_a_clean_corpus_exits_zero(self, project, monkeypatch):
        root, manager = project
        ready(manager)
        monkeypatch.chdir(root)

        result = CliRunner().invoke(app, ["validate"])

        assert result.exit_code == 0
        assert "no problems found" in result.output

    def test_a_broken_corpus_exits_nonzero_and_names_the_file(self, project, monkeypatch):
        root, _ = project
        (root / "tasks").mkdir(exist_ok=True)
        write_raw(root, "task-923-bad.yaml", valid_payload("task-923-bad", parent="task-nope"))
        monkeypatch.chdir(root)

        result = CliRunner().invoke(app, ["validate"])

        assert result.exit_code == 1
        assert "task-923-bad.yaml" in result.output
        assert "missing-parent" in result.output

    def test_validation_works_with_no_receipts_at_all(self, project, monkeypatch):
        """CI and a clean clone have none; the portable check must not need them."""
        root, manager = project
        ready(manager)
        store = ReceiptStore.for_tasks_directory(root / "tasks")
        for receipt in store.directory.glob("*.json"):
            receipt.unlink()
        monkeypatch.chdir(root)

        assert CliRunner().invoke(app, ["validate"]).exit_code == 0

    def test_the_hook_installer_writes_a_pre_commit_hook(self, repo, monkeypatch):
        root, _ = repo
        monkeypatch.chdir(root)

        result = CliRunner().invoke(app, ["validate", "--install-hook"])

        assert result.exit_code == 0
        hook = root / ".git" / "hooks" / "pre-commit"
        assert hook.exists()
        assert "agentjobs validate --staged" in hook.read_text(encoding="utf-8")
        assert OVERRIDE_ENV in hook.read_text(encoding="utf-8")

    def test_the_installer_leaves_an_existing_unrelated_hook_alone(self, repo, monkeypatch):
        root, _ = repo
        hook = root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        monkeypatch.chdir(root)

        result = CliRunner().invoke(app, ["validate", "--install-hook"])

        assert "already exists" in result.output
        assert "echo mine" in hook.read_text(encoding="utf-8")


class TestRealCorpus:
    """The strongest available check: this repository's own hundred-odd real records.

    Scoped to the findings that mean something is *wrong*: a file that will not load,
    a relationship pointing at nothing, or a dependency cycle that would deadlock.

    Taxonomy and serialization findings are deliberately tolerated here. The corpus
    predates the config's category list and its actor vocabulary, so it carries
    categories like `correctness` and actors like `Codex` that config never declared,
    and older records are not byte-identical to what today's writer produces. Both are
    real drift and both are worth fixing, but fixing a hundred historical records is
    its own task, and failing this test on them would only teach people to skip it.
    """

    def test_no_task_file_is_unloadable_or_points_at_nothing(self):
        root = Path(__file__).resolve().parents[1]
        config = yaml.safe_load(
            (root / ".agentjobs" / "config.yaml").read_text(encoding="utf-8-sig")
        )

        report = validate_corpus(
            root / "tasks" / "agentjobs", project_config=config, project_root=root
        )

        structural = [
            finding
            for finding in report.findings
            if finding.rule
            in {
                "unreadable",
                "filename-id-mismatch",
                "missing-parent",
                "missing-dependency",
                "self-parent",
                "self-dependency",
                "dependency-cycle",
                "path-escapes-project",
            }
        ]
        assert structural == [], "\n".join(finding.render() for finding in structural)
        assert report.checked > 50

    def test_the_tolerated_drift_is_only_taxonomy_and_serialization(self):
        """If a new rule starts firing on the real corpus, this says so out loud."""
        root = Path(__file__).resolve().parents[1]
        config = yaml.safe_load(
            (root / ".agentjobs" / "config.yaml").read_text(encoding="utf-8-sig")
        )

        report = validate_corpus(
            root / "tasks" / "agentjobs", project_config=config, project_root=root
        )

        assert {finding.rule for finding in report.findings} <= {
            "unknown-category",
            "unknown-actor",
            "non-canonical-serialization",
        }
