"""The playbook contract and the directory it is read from.

task-214 and ``docs/playbooks-design.md`` §3. Everything here is about *reading*: the
one thing this layer must never do is run something, and the last test in the file is
the standing check that no run surface appeared while nobody was looking.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentjobs.playbooks import (
    DEFAULT_PLAYBOOKS_DIRECTORY,
    MANAGER_VERBS,
    PlaybookDifficulty,
    PlaybookError,
    PlaybookTarget,
    install_references,
    list_playbooks,
    load_reference,
    parse_playbook,
    playbooks_directory_name,
    read_playbook,
    reference_names,
    resolve_playbooks_dir,
    split_frontmatter,
    validate_playbook_name,
)
from agentjobs.playbooks.library import UnknownPlaybookError

VALID = """---
name: groom
description: Find duplicates and propose closures.
target: project
difficulty: hard
verbs: [close, log]
gates:
  - before: close
    what: A human approved the list.
run_task:
  title: Groom the backlog
  category: meta
  priority: medium
  tags: [grooming]
  acceptance:
    - text: Nothing closed outside the approved list.
---

# Groom

The brief.
"""


def write(directory: Path, name: str, text: str) -> Path:
    """Write one playbook file and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


class TestParsing:
    """A valid file loads, and every part of the contract survives the round trip."""

    def test_a_valid_playbook_loads(self) -> None:
        playbook = parse_playbook(Path("groom.md"), VALID)
        contract = playbook.contract
        assert contract.name == "groom"
        assert contract.target is PlaybookTarget.PROJECT
        assert contract.difficulty is PlaybookDifficulty.HARD
        assert contract.verbs == ["close", "log"]
        assert contract.gates[0].before == "close"
        assert contract.run_task is not None
        assert contract.run_task.title == "Groom the backlog"
        assert contract.run_task.acceptance[0].text.startswith("Nothing closed")

    def test_the_body_is_kept_verbatim_and_never_interpreted(self) -> None:
        playbook = parse_playbook(Path("groom.md"), VALID)
        assert playbook.body.strip().startswith("# Groom")
        assert "The brief." in playbook.body

    def test_crlf_line_endings_parse(self) -> None:
        """A file authored on Windows is not a broken playbook."""
        playbook = parse_playbook(Path("groom.md"), VALID.replace("\n", "\r\n"))
        assert playbook.name == "groom"

    def test_frontmatter_split_reports_a_missing_fence(self) -> None:
        with pytest.raises(ValueError, match="must start with"):
            split_frontmatter("# no frontmatter\n")

    def test_frontmatter_split_reports_an_unclosed_block(self) -> None:
        with pytest.raises(ValueError, match="never closed"):
            split_frontmatter("---\nname: groom\n")


class TestValidation:
    """Refusals name the file, and report everything wrong with it at once."""

    def test_a_name_that_does_not_match_the_filename_is_refused(self) -> None:
        with pytest.raises(PlaybookError) as excinfo:
            parse_playbook(Path("grooming.md"), VALID)
        findings = excinfo.value.findings
        assert [finding.field for finding in findings] == ["name"]
        assert findings[0].filename == "grooming.md"
        assert "'groom'" in findings[0].message and "'grooming'" in findings[0].message

    def test_an_unknown_enum_value_is_refused(self) -> None:
        text = VALID.replace("difficulty: hard", "difficulty: extreme")
        with pytest.raises(PlaybookError) as excinfo:
            parse_playbook(Path("groom.md"), text)
        finding = excinfo.value.findings[0]
        assert finding.field == "difficulty"
        assert finding.filename == "groom.md"

    def test_an_unknown_target_is_refused(self) -> None:
        text = VALID.replace("target: project", "target: everything")
        with pytest.raises(PlaybookError) as excinfo:
            parse_playbook(Path("groom.md"), text)
        assert excinfo.value.findings[0].field == "target"

    def test_a_bad_enum_and_a_bad_name_are_reported_together(self) -> None:
        """One round trip per file, not one per mistake."""
        text = VALID.replace("difficulty: hard", "difficulty: extreme")
        with pytest.raises(PlaybookError) as excinfo:
            parse_playbook(Path("grooming.md"), text)
        assert {finding.field for finding in excinfo.value.findings} == {"difficulty", "name"}

    def test_an_unknown_frontmatter_key_is_refused(self) -> None:
        """``run_taks:`` under a tolerant reader is a silently ignored contract."""
        text = VALID.replace("verbs: [close, log]", "verbs: [close, log]\nrun_taks: {}")
        with pytest.raises(PlaybookError) as excinfo:
            parse_playbook(Path("groom.md"), text)
        assert any("run_taks" in (finding.field or "") for finding in excinfo.value.findings)

    def test_a_misspelled_verb_is_refused_and_the_message_names_the_real_ones(self) -> None:
        text = VALID.replace("verbs: [close, log]", "verbs: [clsoe]")
        with pytest.raises(PlaybookError) as excinfo:
            parse_playbook(Path("groom.md"), text)
        finding = excinfo.value.findings[0]
        assert finding.field == "verbs.0"
        assert "queue_move" in finding.message

    def test_every_declared_verb_is_a_manager_verb(self) -> None:
        for verb in MANAGER_VERBS:
            text = VALID.replace("verbs: [close, log]", f"verbs: [{verb}]")
            assert parse_playbook(Path("groom.md"), text).contract.verbs == [verb]

    def test_a_missing_required_field_is_refused(self) -> None:
        text = VALID.replace("description: Find duplicates and propose closures.\n", "")
        with pytest.raises(PlaybookError) as excinfo:
            parse_playbook(Path("groom.md"), text)
        assert excinfo.value.findings[0].field == "description"

    def test_frontmatter_that_is_not_yaml_is_refused_by_name(self) -> None:
        with pytest.raises(PlaybookError) as excinfo:
            parse_playbook(Path("groom.md"), "---\nname: [unclosed\n---\n\nbody\n")
        assert excinfo.value.findings[0].filename == "groom.md"

    def test_frontmatter_that_is_not_a_mapping_is_refused(self) -> None:
        with pytest.raises(PlaybookError) as excinfo:
            parse_playbook(Path("groom.md"), "---\n- one\n- two\n---\n\nbody\n")
        assert "not a mapping" in excinfo.value.findings[0].message


class TestDirectory:
    """Where playbooks live, and what listing one reports."""

    def test_the_default_directory_is_playbooks(self, tmp_path: Path) -> None:
        assert playbooks_directory_name({}) == DEFAULT_PLAYBOOKS_DIRECTORY
        assert resolve_playbooks_dir(tmp_path, {}) == (tmp_path / "playbooks").resolve()

    def test_the_configured_directory_is_used(self, tmp_path: Path) -> None:
        config = {"playbooks_directory": "briefs"}
        assert resolve_playbooks_dir(tmp_path, config) == (tmp_path / "briefs").resolve()

    def test_resolving_does_not_create_the_directory(self, tmp_path: Path) -> None:
        """A project with no playbooks has no directory; init is what makes one."""
        resolve_playbooks_dir(tmp_path, {})
        assert not (tmp_path / "playbooks").exists()

    def test_a_missing_directory_lists_as_empty_rather_than_raising(self, tmp_path: Path) -> None:
        listing = list_playbooks(tmp_path / "playbooks")
        assert listing.exists is False
        assert listing.playbooks == []

    def test_valid_and_invalid_files_are_both_reported(self, tmp_path: Path) -> None:
        directory = tmp_path / "playbooks"
        write(directory, "groom", VALID)
        write(directory, "broken", VALID.replace("difficulty: hard", "difficulty: extreme"))
        listing = list_playbooks(directory)
        assert [playbook.name for playbook in listing.playbooks] == ["groom"]
        assert [problem.filename for problem in listing.problems] == ["broken.md", "broken.md"]

    def test_listing_is_sorted_by_name(self, tmp_path: Path) -> None:
        directory = tmp_path / "playbooks"
        for name in ("zulu", "alpha", "mike"):
            write(directory, name, VALID.replace("name: groom", f"name: {name}"))
        assert [item.name for item in list_playbooks(directory).playbooks] == [
            "alpha",
            "mike",
            "zulu",
        ]

    def test_reading_one_by_name(self, tmp_path: Path) -> None:
        directory = tmp_path / "playbooks"
        write(directory, "groom", VALID)
        assert read_playbook(directory, "groom").name == "groom"

    def test_an_unknown_name_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "playbooks").mkdir()
        with pytest.raises(UnknownPlaybookError):
            read_playbook(tmp_path / "playbooks", "absent")

    @pytest.mark.parametrize(
        "name", ["../secret", "a/b", "a\\b", "..", ".hidden", "", "with space"]
    )
    def test_a_name_that_could_be_a_path_is_refused_before_the_filesystem(
        self, tmp_path: Path, name: str
    ) -> None:
        with pytest.raises(ValueError):
            validate_playbook_name(name)
        with pytest.raises(ValueError):
            read_playbook(tmp_path, name)


class TestShippedReferences:
    """The playbooks AgentJobs ships, and the copy-in that never overwrites."""

    def test_the_three_designed_references_ship(self) -> None:
        assert reference_names() == ["flesh-out", "groom", "reorder"]

    @pytest.mark.parametrize("name", ["flesh-out", "groom", "reorder"])
    def test_every_shipped_reference_validates(self, name: str) -> None:
        """A broken reference would be copied into every project that ran init."""
        playbook = load_reference(name)
        assert playbook.name == name
        assert playbook.contract.description
        assert playbook.body.strip()

    def test_groom_declares_the_mandatory_approval_gate(self) -> None:
        """Design §5.1: the gate before `close` is what makes groom safe."""
        groom = load_reference("groom")
        assert groom.contract.target is PlaybookTarget.PROJECT
        assert [gate.before for gate in groom.contract.gates] == ["close"]
        assert groom.contract.run_task is not None

    def test_reorder_requires_no_gate_and_flesh_out_targets_a_task(self) -> None:
        """The asymmetry in §5.1/§5.2 is in the shipped contracts, not only the prose."""
        assert load_reference("reorder").contract.gates == []
        assert load_reference("flesh-out").contract.target is PlaybookTarget.TASK

    def test_init_copies_the_references_in(self, tmp_path: Path) -> None:
        result = install_references(tmp_path / "playbooks")
        assert sorted(result.written) == ["flesh-out", "groom", "reorder"]
        assert result.kept == []
        listing = list_playbooks(tmp_path / "playbooks")
        assert [playbook.name for playbook in listing.playbooks] == [
            "flesh-out",
            "groom",
            "reorder",
        ]
        assert listing.problems == []

    def test_init_never_overwrites_an_existing_file(self, tmp_path: Path) -> None:
        """A project's copy is authoritative from the moment it exists."""
        directory = tmp_path / "playbooks"
        tuned = write(directory, "groom", VALID.replace("The brief.", "Our own rules."))
        result = install_references(directory)
        assert result.kept == ["groom"]
        assert sorted(result.written) == ["flesh-out", "reorder"]
        assert "Our own rules." in tuned.read_text(encoding="utf-8")

    def test_a_second_init_writes_nothing(self, tmp_path: Path) -> None:
        install_references(tmp_path / "playbooks")
        again = install_references(tmp_path / "playbooks")
        assert again.wrote_nothing
        assert sorted(again.kept) == ["flesh-out", "groom", "reorder"]


class TestThisRepositorysOwnPlaybooks:
    """The playbooks *this* project holds, checked the way ``TestRealCorpus`` checks
    its task records.

    A playbook is authored by hand, is never loaded by the test suite otherwise, and
    fails at the one moment it matters -- somebody asks for a run -- with
    ``invalid_playbook`` and nothing started. That is exactly the exposure the real-corpus
    check exists for, so the repository's own directory gets the same treatment.
    """

    @property
    def directory(self) -> Path:
        return Path(__file__).resolve().parents[1] / DEFAULT_PLAYBOOKS_DIRECTORY

    def test_every_playbook_in_this_repository_validates(self) -> None:
        listing = list_playbooks(self.directory)
        assert listing.exists, f"{self.directory} is missing"
        # Rendered rather than compared as objects: a bare `== []` reports a dataclass
        # repr and not the reason the file was rejected, which is the whole message.
        assert [finding.render() for finding in listing.problems] == []
        assert listing.playbooks, "the directory holds no playbooks at all"

    def test_groom_carries_the_gate_the_design_makes_mandatory(self) -> None:
        """task-216 / design §5.1 and decision P7, pinned against the project's own copy.

        The project's copy is authoritative from the moment it exists (§3.1), so the
        assertion on the shipped reference above says nothing about what a run of this
        backlog would actually read.
        """
        groom = read_playbook(self.directory, "groom")
        assert groom.contract.target is PlaybookTarget.PROJECT
        assert [gate.before for gate in groom.contract.gates] == ["close"]
        run_task = groom.contract.run_task
        assert run_task is not None
        assert run_task.acceptance, "a run task with no acceptance has no definition of done"
        # `close` is the gated verb, so a brief that dropped it from the declared
        # contract would leave a gate standing in front of nothing.
        assert "close" in groom.contract.verbs


EXECUTION_MODULE = "run.py"
"""The one module in this package allowed to reach the dispatch machinery.

Task-215 added it. Everything else here -- the contract, the parser, the directory, the
pointer -- reads files, and the check below is what keeps that true as the package
grows.
"""


def test_reading_a_playbook_cannot_reach_an_execution_path() -> None:
    """Reading a playbook reads a file, and only ``run.py`` may do anything else.

    Until task-215 this said *nothing* in the package could reach the dispatch
    machinery. Instantiation is now here, so the check names the module that does it
    rather than being deleted: a second module acquiring a ``dispatch_task`` import is
    exactly what this exists to catch, and so is ``model.py`` or ``library.py`` growing
    a subprocess call.

    The list of read surfaces this protects is the point. ``playbook list``, the GET
    routes and the MCP ``playbooks_list`` tool all go through this package and none of
    them imports ``run`` -- the package ``__init__`` deliberately does not either -- so
    a read genuinely cannot start anything.
    """
    import agentjobs.playbooks as package

    root = Path(package.__file__).resolve().parent
    for source in root.rglob("*.py"):
        if source.name == EXECUTION_MODULE:
            continue
        text = source.read_text(encoding="utf-8")
        for forbidden in ("subprocess", "dispatch_task", "os.system", "eval(", "exec("):
            assert forbidden not in text, f"{source.name} reaches for {forbidden}"


def test_the_package_init_does_not_import_the_run_module() -> None:
    """Importing ``agentjobs.playbooks`` must not pull the dispatch machinery in.

    Two things depend on it. The read surfaces above stay structurally unable to reach
    an execution path, and ``agentjobs.dispatch`` can hold a ``PlaybookPointer`` without
    the two packages importing each other -- which they would, since ``run`` imports the
    guards.
    """
    import agentjobs.playbooks as package

    init = Path(package.__file__).resolve()
    assert "from .run" not in init.read_text(encoding="utf-8")
    assert not hasattr(package, "run_playbook")
