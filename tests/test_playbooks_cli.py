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


def test_the_playbook_sub_app_has_exactly_four_commands(project: Path) -> None:
    """list, show, init, run -- and nothing else.

    Until task-215 this asserted the sub-app offered no ``run`` at all, which was the
    correct claim while nothing could be run. The claim now is that running is the one
    thing added and that the surface is otherwise unchanged. It is pinned because
    ``playbook run`` starts an agent: a fifth command here is a design change.
    """
    result = runner.invoke(app, ["playbook", "--help"])
    assert result.exit_code == 0
    from agentjobs.cli import playbook_app

    assert sorted(command.name or "" for command in playbook_app.registered_commands) == [
        "init",
        "list",
        "run",
        "show",
    ]


# ----- running ----------------------------------------------------------------

RUNNABLE = """---
name: groom
description: Find duplicates and propose closures.
target: project
difficulty: hard
verbs: [close, log]
gates: []
run_task:
  title: Groom the {project} backlog
  category: meta
  priority: medium
  tags: [grooming]
  acceptance:
    - text: Nothing outside the approved list was closed.
---

# Groom

The brief a run reads.
"""


@pytest.fixture()
def runnable(tmp_path: Path, monkeypatch) -> Path:
    """A registered, dispatchable project with one project-target playbook in it.

    Separate from ``project`` above because running needs what reading does not: a
    registry entry, a human in the actor vocabulary, a git repository, and a
    machine-local dispatch config naming a runner that exits immediately.
    """
    import subprocess
    import sys

    from agentjobs.projects import HOME_ENV, ProjectRegistry

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(HOME_ENV, str(home))

    root = tmp_path / "beta"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": "Beta",
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
    (root / "tasks").mkdir()
    (root / "playbooks").mkdir()
    (root / "playbooks" / "groom.md").write_text(RUNNABLE, encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)

    script = tmp_path / "runner.py"
    script.write_text("print('started')\n", encoding="utf-8")
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "runners": {
                    "fake": {"argv": [sys.executable, str(script), "{prompt}"], "actor": "claude"}
                },
                "projects": {
                    "beta": {"enabled": True, "runner": "fake", "require_clean_tree": False}
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    ProjectRegistry().add(root, project_id="beta", name="Beta")
    monkeypatch.chdir(root)
    return root


def test_run_creates_the_run_task_and_dispatches_it(runnable: Path) -> None:
    result = runner.invoke(app, ["playbook", "run", "groom", "--actor", "Jeff Posey"])
    assert result.exit_code == 0, result.output
    assert "Created run task" in result.output
    assert "Dispatched" in result.output
    assert "playbooks/groom.md" in result.output
    assert "sha256:" in result.output


def test_run_without_an_actor_refuses_and_names_the_flag(runnable: Path) -> None:
    """No ``default_user`` fallback here, unlike every other ``--actor`` in this CLI.

    The entry it writes is read a moment later as the authorisation for spending money,
    and one attributed to whoever the config happens to name is the signature task-188
    refuses to invent.
    """
    result = runner.invoke(app, ["playbook", "run", "groom"])
    assert result.exit_code == 2
    assert "no_authorizing_human" in result.output
    assert "--actor" in result.output
    assert not list((runnable / "tasks").glob("*.yaml"))


def test_run_refuses_an_agent_as_the_creating_actor(runnable: Path) -> None:
    result = runner.invoke(app, ["playbook", "run", "groom", "--actor", "claude"])
    assert result.exit_code == 1
    assert "authorizer_not_human" in result.output
    assert not list((runnable / "tasks").glob("*.yaml"))


def test_run_refuses_a_task_for_a_project_target_playbook(runnable: Path) -> None:
    result = runner.invoke(
        app, ["playbook", "run", "groom", "--actor", "Jeff Posey", "--task", "task-001"]
    )
    assert result.exit_code == 2
    assert "target_mismatch" in result.output
    assert not list((runnable / "tasks").glob("*.yaml"))


def test_run_refuses_an_unknown_playbook(runnable: Path) -> None:
    result = runner.invoke(app, ["playbook", "run", "nope", "--actor", "Jeff Posey"])
    assert result.exit_code == 2
    assert "unknown_playbook" in result.output
