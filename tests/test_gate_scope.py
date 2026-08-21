"""The gate's necessity rule: what it is allowed to skip, and what it must never skip.

Task-221, absorbed into task-233. The worked example is a branch that was rebased onto
``main``, bringing in **one task YAML**, after which the full six-minute gate was run
again to re-establish something that could not have changed.

The rule is an exception to an emphatic rule in ENGINEERING.md -- that the unqualified
``scripts/check.py`` is what the commit rule means -- and exceptions of that kind erode
into "except when I judged it unnecessary". So the tests here are mostly about the
properties that stop it eroding, not about the saving:

* the decision is derived from a receipt the gate itself wrote, never asserted;
* an unclassified path selects **every** stage, so an incomplete table costs time
  rather than coverage;
* a reduced run names its evidence and cannot be read as a full green.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]

EVERY = [
    "black",
    "ruff",
    "mypy",
    "api",
    "icons",
    "oxlint",
    "pytest",
    "vitest",
    "build",
    "e2e",
]

RECORD = "tasks/some-project/task-042.yaml"
"""A task record, spelled without naming a real one so no write guard mistakes it."""


def load_script(name: str) -> ModuleType:
    """Load a repository script by path, without making ``scripts/`` a package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate_scope = load_script("gate_scope")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A real git repository with one commit, because every answer here comes from git.

    Stubbing git would test the stub. The whole risk in this feature is that the diff is
    computed against the wrong thing, and only git can be wrong about that.
    """
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "gate@example.com")
    git("config", "user.name", "Gate")
    (root / "src").mkdir()
    (root / "src" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tasks" / "some-project").mkdir(parents=True)
    (root / RECORD).write_text("id: task-042\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "first")
    return root


def commit_all(root: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=root, check=True, capture_output=True
    )


def add_a_record(root: Path) -> str:
    """The change the worked example is about: one more task record, committed."""
    path = "tasks/some-project/task-043.yaml"
    (root / path).write_text("id: task-043\n", encoding="utf-8")
    commit_all(root, "a record correction brought in by a rebase")
    return path


# --- the classification table -------------------------------------------------------


class TestClassification:
    """What each family of paths can reach, and what happens to a path nobody claimed."""

    def test_a_task_record_reaches_pytest_and_nothing_else(self) -> None:
        """The worked example. One task YAML, and only the suite that reads the corpus."""
        stages, _ = gate_scope.stages_for([RECORD], EVERY)

        assert stages == ["pytest"]

    def test_a_task_record_still_runs_the_stage_that_reads_the_live_corpus(self) -> None:
        """'It was only a task file' is not a safe skip, and this is why.

        ``tests/test_validate.py::TestRealCorpus`` loads this repository's own records.
        It is the one stage whose inputs are not bounded by the diff, so a rule that
        skipped it would be unsound however convenient.
        """
        stages, _ = gate_scope.stages_for(["tasks/other-project/task-9.yaml"], EVERY)

        assert "pytest" in stages

    def test_prose_reaches_pytest_because_the_documentation_contract_reads_it(self) -> None:
        stages, _ = gate_scope.stages_for(["docs/agent-workflow.md", "ENGINEERING.md"], EVERY)

        assert stages == ["pytest"]

    def test_source_is_unclassified_and_therefore_runs_everything(self) -> None:
        """The default-deny property. An incomplete table costs a minute, not a check."""
        stages, reasons = gate_scope.stages_for(["src/agentjobs/manager.py"], EVERY)

        assert stages == EVERY
        assert "unclassified" in reasons["src/agentjobs/manager.py"]

    def test_a_file_type_nobody_thought_about_runs_everything(self) -> None:
        stages, _ = gate_scope.stages_for(["some/new/thing.rs"], EVERY)

        assert stages == EVERY

    def test_one_unclassified_path_beside_a_classified_one_still_runs_everything(self) -> None:
        """A reduced set is only sound if *every* change is accounted for."""
        stages, _ = gate_scope.stages_for([RECORD, "src/agentjobs/manager.py"], EVERY)

        assert stages == EVERY

    def test_every_class_names_stages_the_gate_actually_has(self) -> None:
        """A typo in the table would silently select nothing for that class."""
        for entry in gate_scope.CLASSES:
            assert set(entry.stages) <= set(EVERY), entry


# --- receipts -----------------------------------------------------------------------


class TestReceipts:
    """The evidence a reduced run rests on. Without it the rule is a judgement call."""

    def test_a_receipt_round_trips(self, repository: Path) -> None:
        head = gate_scope.head_commit(repository)

        gate_scope.write_receipt(repository, head, basis=None)

        assert gate_scope.read_receipt(repository) == {"commit": head, "basis": None}

    def test_the_receipt_lives_outside_the_tree_it_attests_to(self, repository: Path) -> None:
        """A receipt inside the work tree would be a change the next run had to classify."""
        gate_scope.write_receipt(repository, gate_scope.head_commit(repository), basis=None)

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
        )
        assert status.stdout.strip() == ""

    def test_a_derived_receipt_records_what_it_derived_from(self, repository: Path) -> None:
        """A chain of reduced runs has to be auditable rather than anonymous."""
        first = gate_scope.head_commit(repository)
        add_a_record(repository)
        second = gate_scope.head_commit(repository)

        gate_scope.write_receipt(repository, second, basis=first)

        assert gate_scope.read_receipt(repository) == {"commit": second, "basis": first}

    def test_a_corrupt_receipt_reads_as_no_receipt(self, repository: Path) -> None:
        path = gate_scope.receipt_path(repository)
        assert path is not None
        path.write_text("{not json", encoding="utf-8")

        assert gate_scope.read_receipt(repository) is None

    def test_a_receipt_without_a_commit_reads_as_no_receipt(self, repository: Path) -> None:
        path = gate_scope.receipt_path(repository)
        assert path is not None
        path.write_text(json.dumps({"basis": None}), encoding="utf-8")

        assert gate_scope.read_receipt(repository) is None

    def test_a_dirty_tree_is_reported_as_dirty(self, repository: Path) -> None:
        assert gate_scope.tree_is_clean(repository)

        (repository / "src" / "thing.py").write_text("x = 2\n", encoding="utf-8")

        assert not gate_scope.tree_is_clean(repository)

    def test_an_untracked_file_makes_the_tree_dirty(self, repository: Path) -> None:
        """A receipt names a commit, and untracked source is not in any commit."""
        (repository / "new.py").write_text("y = 1\n", encoding="utf-8")

        assert not gate_scope.tree_is_clean(repository)


# --- what changed since ------------------------------------------------------------


class TestChangedSince:
    def test_an_uncommitted_edit_counts(self, repository: Path) -> None:
        head = gate_scope.head_commit(repository)
        (repository / "src" / "thing.py").write_text("x = 2\n", encoding="utf-8")

        assert gate_scope.changed_since(repository, head) == ["src/thing.py"]

    def test_an_untracked_file_counts(self, repository: Path) -> None:
        """It is source the next commit will carry, so the gate is about to verify it."""
        head = gate_scope.head_commit(repository)
        (repository / "extra.py").write_text("z = 1\n", encoding="utf-8")

        assert gate_scope.changed_since(repository, head) == ["extra.py"]

    def test_a_commit_made_since_counts(self, repository: Path) -> None:
        head = gate_scope.head_commit(repository)
        path = add_a_record(repository)

        assert gate_scope.changed_since(repository, head) == [path]

    def test_an_unknown_commit_yields_no_answer_rather_than_an_empty_one(
        self, repository: Path
    ) -> None:
        """An empty list would mean 'nothing changed', which is the dangerous reading."""
        assert gate_scope.changed_since(repository, "0" * 40) is None


# --- the decision, end to end -------------------------------------------------------


class TestResolve:
    def test_without_a_receipt_nothing_is_narrowed(self, repository: Path) -> None:
        scope = gate_scope.resolve(repository, EVERY)

        assert not scope.reduced
        assert scope.refusal is not None
        assert "receipt" in scope.refusal

    def test_the_worked_example_runs_pytest_alone(self, repository: Path) -> None:
        """task-221's example: a rebase whose only import is one task record."""
        gate_scope.write_receipt(repository, gate_scope.head_commit(repository), basis=None)
        add_a_record(repository)

        scope = gate_scope.resolve(repository, EVERY)

        assert scope.stages == ["pytest"]

    def test_an_unchanged_tree_selects_no_stages_at_all(self, repository: Path) -> None:
        gate_scope.write_receipt(repository, gate_scope.head_commit(repository), basis=None)

        scope = gate_scope.resolve(repository, EVERY)

        assert scope.reduced and scope.stages == [] and scope.paths == []

    def test_a_receipt_for_a_commit_git_has_never_heard_of_narrows_nothing(
        self, repository: Path
    ) -> None:
        gate_scope.write_receipt(repository, "0" * 40, basis=None)

        scope = gate_scope.resolve(repository, EVERY)

        assert not scope.reduced


# --- the output ---------------------------------------------------------------------


class TestRendering:
    """A reduced run is a claim. The point of printing this is that it can be disputed."""

    @staticmethod
    def reduced(repository: Path) -> object:
        gate_scope.write_receipt(repository, gate_scope.head_commit(repository), basis=None)
        add_a_record(repository)
        return gate_scope.resolve(repository, EVERY)

    def test_it_cannot_be_mistaken_for_the_full_gate(self, repository: Path) -> None:
        text = gate_scope.render(self.reduced(repository), EVERY)

        assert "NECESSITY RUN" in text
        assert "This is not the gate" in text
        assert "Ran every stage" not in text

    def test_it_names_the_commit_the_claim_rests_on(self, repository: Path) -> None:
        head = gate_scope.head_commit(repository)
        text = gate_scope.render(self.reduced(repository), EVERY)

        assert head[:8] in text

    def test_it_names_every_path_it_looked_at_and_the_rule_that_matched(
        self, repository: Path
    ) -> None:
        text = gate_scope.render(self.reduced(repository), EVERY)

        assert "task-043.yaml" in text
        assert "TestRealCorpus" in text

    def test_it_names_every_stage_it_skipped(self, repository: Path) -> None:
        text = gate_scope.render(self.reduced(repository), EVERY)

        for stage in EVERY:
            if stage != "pytest":
                assert stage in text

    def test_an_unchanged_tree_says_so_rather_than_claiming_a_green(self, repository: Path) -> None:
        gate_scope.write_receipt(repository, gate_scope.head_commit(repository), basis=None)

        text = gate_scope.render(gate_scope.resolve(repository, EVERY), EVERY)

        assert "NOTHING CHANGED" in text
        assert "Ran every stage" not in text

    def test_a_refusal_says_it_is_running_everything(self, repository: Path) -> None:
        text = gate_scope.render(gate_scope.resolve(repository, EVERY), EVERY)

        assert "FULL GATE" in text
        assert "Running every stage" in text
