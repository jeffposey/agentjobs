"""A process must run the checkout it claims to, and say so when it does not.

Task-194: `agentjobs.pth` in the main clone's virtualenv read `C:/projects/aj-188/src`,
so the dashboard everyone reads served an unmerged branch's code from the right task
files. `git log` in the clone was correct, the files on disk were correct, and every
behaviour came from somewhere else. It took a forensic session to notice.

The cause is not carelessness. Poetry installs into an *activated* virtualenv in
preference to the one it keys on the project path, and a dispatched agent on this
machine inherits a shell whose `VIRTUAL_ENV` is the main clone's. Running the documented
bootstrap inside a worktree is therefore what breaks the main clone -- reproduced while
this task was being worked, within two minutes of starting.

Two guards, tested here: the server refuses to start on a mismatch, and the bootstrap
cannot cause one.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from agentjobs import environment
from agentjobs.environment import (
    SourceMismatchError,
    enclosing_checkout,
    source_mismatch,
    verify_source_or_die,
)


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> ModuleType:
    """Load a repository script without making scripts/ an installed package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bootstrap = load_script("bootstrap")
check = load_script("check")


def make_checkout(root: Path) -> Path:
    """Build the shape the guards recognise as a checkout, and return its root."""
    package = root / "src" / "agentjobs"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text("[tool.poetry]\n", encoding="utf-8")
    return root


@pytest.fixture
def clone_and_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A main clone and a worktree beside it, as the real machine has."""
    return make_checkout(tmp_path / "agentjobs"), make_checkout(tmp_path / "aj-188")


def imports_from(monkeypatch: pytest.MonkeyPatch, root: Path | None) -> None:
    """Pretend this process imported agentjobs from `root` (None: an installed wheel)."""
    monkeypatch.setattr(environment, "imported_source_root", lambda: root)


# --- what the imported source is ---------------------------------------------------


def test_a_src_layout_beside_a_pyproject_is_a_checkout(tmp_path: Path) -> None:
    root = make_checkout(tmp_path / "clone")

    assert enclosing_checkout(root) == root
    assert enclosing_checkout(root / "src" / "agentjobs") == root


def test_a_directory_under_no_checkout_has_none(tmp_path: Path) -> None:
    assert enclosing_checkout(tmp_path) is None


def test_a_src_layout_without_a_pyproject_is_not_a_checkout(tmp_path: Path) -> None:
    """Half a checkout is not one; an unpacked wheel must not be mistaken for source."""
    package = tmp_path / "site-packages" / "src" / "agentjobs"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")

    assert enclosing_checkout(package) is None


# --- when a mismatch is a mismatch --------------------------------------------------


def test_serving_the_checkout_you_were_launched_in_is_fine(
    monkeypatch: pytest.MonkeyPatch, clone_and_worktree: tuple[Path, Path]
) -> None:
    clone, _ = clone_and_worktree
    imports_from(monkeypatch, clone)

    assert source_mismatch(cwd=clone, project_roots=[clone]) is None


def test_a_review_server_run_from_its_own_worktree_is_fine(
    monkeypatch: pytest.MonkeyPatch, clone_and_worktree: tuple[Path, Path]
) -> None:
    """Standing a server up on a branch is a documented workflow, not the failure.

    It is started *in* the worktree and runs the worktree's code, which is the whole
    point. The guard must not make that impossible while catching the case where a
    server started in the main clone runs a worktree's code.
    """
    clone, worktree = clone_and_worktree
    imports_from(monkeypatch, worktree)

    assert source_mismatch(cwd=worktree, project_roots=[clone]) is None


def test_the_main_clone_running_a_worktrees_code_is_reported(
    monkeypatch: pytest.MonkeyPatch, clone_and_worktree: tuple[Path, Path]
) -> None:
    """The task-194 failure itself."""
    clone, worktree = clone_and_worktree
    imports_from(monkeypatch, worktree)

    problem = source_mismatch(cwd=clone, project_roots=[clone])

    assert problem is not None
    assert str(worktree) in problem
    assert str(clone) in problem
    # A refusal a reader cannot act on is how this went unfixed for a session.
    assert "poetry install" in problem
    assert "VIRTUAL_ENV" in problem


def test_an_installed_package_is_never_a_mismatch(
    monkeypatch: pytest.MonkeyPatch, clone_and_worktree: tuple[Path, Path]
) -> None:
    """Installing AgentJobs and serving a clone of it is a normal thing to do.

    Only an *editable* install can point at the wrong checkout, so a package that came
    from a wheel is outside what this guard can or should judge. Refusing there would
    break ordinary users to protect one machine's worktree habit.
    """
    clone, _ = clone_and_worktree
    imports_from(monkeypatch, None)

    assert source_mismatch(cwd=clone, project_roots=[clone]) is None


def test_a_registered_project_is_used_when_the_cwd_says_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clone_and_worktree: tuple[Path, Path]
) -> None:
    """A service started from an unrelated directory still knows what it serves."""
    clone, worktree = clone_and_worktree
    imports_from(monkeypatch, worktree)

    problem = source_mismatch(cwd=tmp_path, project_roots=[clone])

    assert problem is not None
    assert str(clone) in problem


def test_nothing_is_claimed_when_no_checkout_is_in_sight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clone_and_worktree: tuple[Path, Path]
) -> None:
    """Registered projects that are not AgentJobs checkouts say nothing about source."""
    _, worktree = clone_and_worktree
    ordinary_project = tmp_path / "some-other-project"
    ordinary_project.mkdir()
    imports_from(monkeypatch, worktree)

    assert source_mismatch(cwd=tmp_path, project_roots=[ordinary_project]) is None


# --- refusing to run -----------------------------------------------------------------


def test_verification_raises_on_a_mismatch(
    monkeypatch: pytest.MonkeyPatch, clone_and_worktree: tuple[Path, Path]
) -> None:
    clone, worktree = clone_and_worktree
    imports_from(monkeypatch, worktree)
    monkeypatch.chdir(clone)

    with pytest.raises(SourceMismatchError) as raised:
        verify_source_or_die([clone])

    assert str(worktree) in str(raised.value)


def test_the_escape_hatch_announces_itself(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clone_and_worktree: tuple[Path, Path],
) -> None:
    """A machine running with the guard off must not look like one that passed it."""
    clone, worktree = clone_and_worktree
    imports_from(monkeypatch, worktree)
    monkeypatch.chdir(clone)
    monkeypatch.setenv("AGENTJOBS_SKIP_SOURCE_CHECK", "1")

    verify_source_or_die([clone])

    assert "AGENTJOBS_SKIP_SOURCE_CHECK" in capsys.readouterr().out


def test_this_test_run_is_exercising_this_checkout() -> None:
    """The live case, not a fixture: pytest itself must be running this branch.

    `scripts/check.py` refuses when its interpreter imports elsewhere, but pytest is
    also run directly. A suite that passes on a neighbouring checkout's source is the
    same class of lie as a dashboard serving one.
    """
    assert source_mismatch(cwd=ROOT, project_roots=[ROOT]) is None


def test_the_server_starts_when_its_source_is_its_own() -> None:
    """The guard is on the real startup path, and the real startup path still works."""
    from fastapi.testclient import TestClient

    from agentjobs.api.main import app

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200


def test_the_version_endpoint_reports_which_source_is_running() -> None:
    """Answerable with curl, so the next mismatch does not need a forensic session."""
    from fastapi.testclient import TestClient

    from agentjobs.api.main import app

    with TestClient(app) as client:
        body = client.get("/api/version").json()

    assert Path(body["source_root"]) == ROOT


# --- the bootstrap cannot cause one --------------------------------------------------


def test_bootstrap_recognises_a_checkout(tmp_path: Path) -> None:
    root = make_checkout(tmp_path / "clone")

    assert bootstrap.checkout_of(str(root / "src" / "agentjobs" / "__init__.py")) == root


def test_bootstrap_ignores_an_installed_package(tmp_path: Path) -> None:
    package = tmp_path / "site-packages" / "agentjobs" / "__init__.py"
    package.parent.mkdir(parents=True)
    package.write_text("", encoding="utf-8")

    assert bootstrap.checkout_of(str(package)) is None


def test_bootstrap_leaves_poetry_alone_when_nothing_is_activated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("POETRY_ACTIVE", raising=False)

    env, note = bootstrap.install_environment("poetry")

    assert env is None and note is None


def test_bootstrap_detaches_from_another_checkouts_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reproduction: a worktree bootstrap that would rewrite the main clone.

    Detaching, rather than refusing, is what makes this a fix. The worktree still gets
    the environment it came here for -- its own, path-keyed one -- and the clone it was
    about to hijack is untouched.
    """
    clone = make_checkout(tmp_path / "agentjobs")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "venvs" / "agentjobs-KSKY4Ymk"))
    monkeypatch.setattr(bootstrap, "imported_checkout", lambda poetry, env: clone)

    env, note = bootstrap.install_environment("poetry")

    assert env is not None
    assert "VIRTUAL_ENV" not in env
    assert "POETRY_ACTIVE" not in env
    assert note is not None and str(clone) in note


def test_bootstrap_lets_a_clone_repair_its_own_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`poetry install` from the main clone is the documented repair. It must still run.

    Its activated environment is pointing at a worktree at that moment -- that is the
    damage being undone -- but the environment *is* the clone's own, so detaching would
    send the repair to a different virtualenv and leave the broken one broken.
    """
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "venvs" / "agentjobs-KSKY4Ymk"))
    monkeypatch.setattr(bootstrap, "imported_checkout", lambda poetry, env: bootstrap.ROOT)

    env, note = bootstrap.install_environment("poetry")

    assert env is None and note is None


def test_bootstrap_leaves_a_freshly_created_environment_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An activated venv with nothing installed is somebody's `python -m venv .venv`."""
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))
    monkeypatch.setattr(bootstrap, "imported_checkout", lambda poetry, env: None)

    env, note = bootstrap.install_environment("poetry")

    assert env is None and note is None


def test_bootstrap_prints_an_interpreter_the_shell_cannot_redirect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`poetry run` is not a usable instruction in the shell that caused the problem."""
    venv = tmp_path / "venvs" / "agentjobs-abc123"
    monkeypatch.setattr(bootstrap, "poetry_query", lambda poetry, args, env: str(venv))

    command = bootstrap.verify_command("poetry", None)

    assert str(venv) in command
    assert command.endswith("scripts/check.py")
    assert "poetry run" not in command


# --- and the gate says something useful when it refuses -------------------------------


ORDINARY_ADVICE = "Run `python scripts/bootstrap.py`, then `poetry run python scripts/check.py`."


def test_the_gate_names_the_activated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise its advice is `poetry run`, which is the thing that keeps failing."""
    monkeypatch.setenv("VIRTUAL_ENV", "C:/venvs/agentjobs-KSKY4Ymk-py3.13")

    advice = check.remedy([f"agentjobs imports from elsewhere, {check.FOREIGN_IMPORT}"])

    assert "C:/venvs/agentjobs-KSKY4Ymk-py3.13" in advice
    assert "bootstrap.py" in advice


def test_the_gate_does_not_blame_the_environment_for_missing_node_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An activated virtualenv explains a wrong import. It explains nothing else."""
    monkeypatch.setenv("VIRTUAL_ENV", "C:/venvs/agentjobs-KSKY4Ymk-py3.13")

    assert check.remedy(["frontend/node_modules is missing"]) == ORDINARY_ADVICE


def test_the_gate_gives_the_ordinary_advice_otherwise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    assert check.remedy([f"agentjobs imports from elsewhere, {check.FOREIGN_IMPORT}"]) == (
        ORDINARY_ADVICE
    )


# --- and the gate cannot hand one to its own children ---------------------------------
#
# Task-210. The guard above is correct and stays; what was wrong is the environment the
# gate spawned its children in. `npm run check` and Playwright's `webServer` both start
# nested `poetry run` processes, which prefer an activated `VIRTUAL_ENV` over the
# path-keyed environment -- so a worktree gate run reached the last stage, six minutes
# in, and was refused there for pointing at the main clone.

FOREIGN_VENV = "C:/venvs/agentjobs-KSKY4Ymk-py3.13"


def test_a_foreign_virtualenv_is_not_passed_to_children(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", FOREIGN_VENV)
    monkeypatch.setenv("POETRY_ACTIVE", "1")

    env = check.child_environment()

    assert "VIRTUAL_ENV" not in env
    assert "POETRY_ACTIVE" not in env


def test_pythonhome_is_never_passed_to_children(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child interpreter that reads another installation's stdlib fails obscurely."""
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("PYTHONHOME", "C:/Python311")

    assert "PYTHONHOME" not in check.child_environment()


def test_this_interpreters_own_virtualenv_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """It agrees with us. Removing it would strand a plain `python -m venv .venv`
    checkout, where Poetry has no path-keyed environment to fall back to -- the case
    `scripts/bootstrap.py` also declines to overrule."""
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    monkeypatch.setenv("POETRY_ACTIVE", "1")

    env = check.child_environment()

    assert env["VIRTUAL_ENV"] == sys.prefix
    assert env["POETRY_ACTIVE"] == "1"


def test_the_rest_of_the_environment_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrubbing two names, not sanitising the environment. PATH still has to work."""
    monkeypatch.setenv("VIRTUAL_ENV", FOREIGN_VENV)
    monkeypatch.setenv("AGENTJOBS_E2E_PORT", "24242")

    env = check.child_environment()

    assert env["AGENTJOBS_E2E_PORT"] == "24242"
    assert env.get("PATH") == os.environ.get("PATH")


def test_no_activated_virtualenv_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)

    assert check.child_environment() == dict(os.environ)


def test_every_check_the_gate_runs_gets_the_scrubbed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression guard proper (sc-2).

    `run()` is the single door every stage of the gate goes through, and it takes no
    environment argument, so this holds for stages added later too. Asserting on what
    `subprocess.run` was actually handed is the only way to catch someone quietly
    reinstating the inherited environment.
    """
    monkeypatch.setenv("VIRTUAL_ENV", FOREIGN_VENV)
    monkeypatch.setenv("POETRY_ACTIVE", "1")
    seen: list[dict[str, str]] = []

    def record(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen.append(kwargs["env"])  # type: ignore[arg-type]
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(check.subprocess, "run", record)
    monkeypatch.setattr(check, "setup_problems", lambda root, origin: [])
    monkeypatch.setattr(check.shutil, "which", lambda name: "npm.cmd")

    assert check.main() == 0

    # Black, Ruff, MyPy, pytest, and the frontend's own gate -- which is the one that
    # starts the nested `poetry run` processes this exists for.
    assert len(seen) == 5
    for env in seen:
        assert "VIRTUAL_ENV" not in env
        assert "POETRY_ACTIVE" not in env


def test_the_gate_says_when_it_disowns_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """A silent correction is one nobody can confirm happened."""
    monkeypatch.setenv("VIRTUAL_ENV", FOREIGN_VENV)

    note = check.disowned_environment_note()

    assert note is not None
    assert FOREIGN_VENV in note and sys.prefix in note


def test_the_gate_stays_quiet_when_there_is_nothing_to_disown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert check.disowned_environment_note() is None

    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    assert check.disowned_environment_note() is None
