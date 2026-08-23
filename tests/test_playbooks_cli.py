"""``agentjobs playbook`` -- list, show, and the copy-in that never overwrites."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentjobs.cli import app

runner = CliRunner()

VALID = """---
name: groom
description: Find duplicates and propose closures.
target: project
difficulty: hard
verbs: [close, log]
gates:
  - before: close
    what: A human approved the list.
---

# Groom

The brief a run reads.
"""


@pytest.fixture()
def project(tmp_path: Path, monkeypatch) -> Path:
    """An initialized project directory, with the CLI's working directory inside it."""
    (tmp_path / ".agentjobs").mkdir()
    (tmp_path / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": "Alpha", "tasks_directory": "tasks"}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_list_on_a_project_with_no_playbooks_points_at_init(project: Path) -> None:
    result = runner.invoke(app, ["playbook", "list"])
    assert result.exit_code == 0
    assert "No playbooks directory yet" in result.stdout
    assert "agentjobs playbook init" in result.stdout


def test_init_copies_the_shipped_references_in(project: Path) -> None:
    result = runner.invoke(app, ["playbook", "init"])
    assert result.exit_code == 0
    for name in ("groom.md", "reorder.md", "flesh-out.md"):
        assert (project / "playbooks" / name).is_file()
        assert name in result.stdout


def test_a_second_init_keeps_every_file_and_says_so(project: Path) -> None:
    runner.invoke(app, ["playbook", "init"])
    tuned = project / "playbooks" / "groom.md"
    tuned.write_text(VALID.replace("The brief a run reads.", "Our own rules."), encoding="utf-8")

    result = runner.invoke(app, ["playbook", "init"])
    assert result.exit_code == 0
    assert "not overwritten" in result.stdout
    assert "Our own rules." in tuned.read_text(encoding="utf-8")


def test_list_shows_each_playbook_with_its_contract(project: Path) -> None:
    runner.invoke(app, ["playbook", "init"])
    result = runner.invoke(app, ["playbook", "list"])
    assert result.exit_code == 0
    assert "groom" in result.stdout
    assert "target: project" in result.stdout
    assert "difficulty: hard" in result.stdout


def test_show_prints_the_contract_and_then_the_brief(project: Path) -> None:
    (project / "playbooks").mkdir()
    (project / "playbooks" / "groom.md").write_text(VALID, encoding="utf-8")
    result = runner.invoke(app, ["playbook", "show", "groom"])
    assert result.exit_code == 0
    assert "name: groom" in result.stdout
    assert "The brief a run reads." in result.stdout


def test_show_can_omit_the_brief(project: Path) -> None:
    (project / "playbooks").mkdir()
    (project / "playbooks" / "groom.md").write_text(VALID, encoding="utf-8")
    result = runner.invoke(app, ["playbook", "show", "groom", "--contract"])
    assert result.exit_code == 0
    assert "name: groom" in result.stdout
    assert "The brief a run reads." not in result.stdout


def test_show_on_an_unknown_name_exits_non_zero_and_points_at_list(project: Path) -> None:
    (project / "playbooks").mkdir()
    result = runner.invoke(app, ["playbook", "show", "absent"])
    assert result.exit_code == 2
    assert "playbook list" in result.output


def test_show_on_an_invalid_file_prints_every_finding(project: Path) -> None:
    (project / "playbooks").mkdir()
    (project / "playbooks" / "groom.md").write_text(
        VALID.replace("difficulty: hard", "difficulty: extreme"), encoding="utf-8"
    )
    result = runner.invoke(app, ["playbook", "show", "groom"])
    assert result.exit_code == 1
    assert "difficulty" in result.output


def test_list_reports_a_file_that_will_not_load_beside_the_ones_that_did(project: Path) -> None:
    (project / "playbooks").mkdir()
    (project / "playbooks" / "groom.md").write_text(VALID, encoding="utf-8")
    (project / "playbooks" / "broken.md").write_text(
        VALID.replace("name: groom", "name: mismatched"), encoding="utf-8"
    )
    result = runner.invoke(app, ["playbook", "list"])
    assert result.exit_code == 0
    assert "groom" in result.stdout
    assert "broken.md" in result.output


def test_a_name_that_could_be_a_path_is_refused(project: Path) -> None:
    (project / "playbooks").mkdir()
    result = runner.invoke(app, ["playbook", "show", "../../etc/passwd"])
    assert result.exit_code == 2
    assert "is not a playbook name" in result.output


def test_the_configured_directory_is_used(project: Path) -> None:
    (project / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": "Alpha",
                "tasks_directory": "tasks",
                "playbooks_directory": "briefs",
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["playbook", "init"])
    assert result.exit_code == 0
    assert (project / "briefs" / "groom.md").is_file()
    assert not (project / "playbooks").exists()


def test_the_playbook_sub_app_offers_no_way_to_run_one(project: Path) -> None:
    """Design P10, on the surface an agent is most likely to reach for."""
    result = runner.invoke(app, ["playbook", "--help"])
    assert result.exit_code == 0
    assert "run" not in result.stdout.split("Commands")[-1]
