"""Which checkout's source this process actually imported.

An editable install puts one line in a virtualenv's ``site-packages`` naming the
directory to import from. Poetry rewrites that line on every ``poetry install``, and
Poetry installs into an *activated* virtualenv in preference to the one it keys on the
project path -- so running the documented bootstrap inside a worktree, while
``VIRTUAL_ENV`` points at the main clone's environment, silently repoints the main clone
at the worktree's branch.

The result is a server that reads the right files with the wrong code. ``git log`` in the
clone it was started from looks correct, the files on disk are correct, and every
behaviour comes from an unmerged branch. It cost a forensic session to find once
(task-192, task-194); this module exists so it announces itself instead.

Nothing here inspects the virtualenv. The question is answered from ``agentjobs.__file__``,
which is the only thing that decides what the process actually runs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

import agentjobs


class SourceMismatchError(RuntimeError):
    """Raised when the imported package belongs to a checkout that is not being served."""


def _checkout_root(package_dir: Path) -> Optional[Path]:
    """Return the checkout `package_dir` belongs to, or None if it is not one.

    A source checkout is ``<root>/src/agentjobs`` beside a ``<root>/pyproject.toml``.
    Anything else -- a wheel unpacked into ``site-packages``, a zipapp -- is a normal
    installation with no checkout behind it, and there is nothing to be wrong about.
    """
    if package_dir.parent.name != "src":
        return None
    root = package_dir.parents[1]
    return root if (root / "pyproject.toml").is_file() else None


def imported_source_root() -> Optional[Path]:
    """Return the checkout this process imported ``agentjobs`` from, if any.

    None means the package came from an ordinary installation rather than from a
    checkout, which is the normal case for anyone who installed AgentJobs rather than
    cloning it.
    """
    package_dir = Path(agentjobs.__file__).resolve().parent
    return _checkout_root(package_dir)


def enclosing_checkout(start: Path) -> Optional[Path]:
    """Return the nearest AgentJobs checkout at or above `start`, if there is one."""
    resolved = start.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / "src" / "agentjobs" / "__init__.py").is_file():
            if (candidate / "pyproject.toml").is_file():
                return candidate
    return None


def _expected_roots(cwd: Path, project_roots: Iterable[Path]) -> list[Path]:
    """Which checkout(s) this process ought to have imported from.

    The launch directory wins when it is itself a checkout. That is what makes a
    deliberate review server run from a worktree legal -- it is serving the branch it
    was started in, which is the whole point of standing one up -- while the failure this
    module was written for stays illegal: that server was started from the main clone and
    imported a worktree.

    When the launch directory is not inside a checkout, fall back to any registered
    project whose root is one. That is the case the acceptance criterion names: the
    imported package is not the project being served.
    """
    launched_in = enclosing_checkout(cwd)
    if launched_in is not None:
        return [launched_in]
    registered = [root.resolve() for root in project_roots]
    return [root for root in registered if enclosing_checkout(root) == root]


def source_mismatch(
    *,
    cwd: Optional[Path] = None,
    project_roots: Iterable[Path] = (),
) -> Optional[str]:
    """Explain why the imported source is the wrong checkout, or None if it is right.

    Returns None -- deliberately, not an error -- whenever the question is unanswerable:
    an installed (non-checkout) package, or a launch directory and registry that name no
    checkout at all. A guard that guesses in those cases would refuse to start for people
    who are simply using the tool.
    """
    imported = imported_source_root()
    if imported is None:
        return None

    expected = _expected_roots(Path.cwd() if cwd is None else cwd, project_roots)
    if not expected or imported in expected:
        return None

    names = "\n".join(f"    {root}" for root in expected)
    return (
        "AgentJobs imported its own source from the wrong checkout.\n\n"
        f"  running code from: {imported / 'src' / 'agentjobs'}\n"
        f"  expected it under:\n{names}\n\n"
        "The virtualenv on this interpreter has an editable install pointing at a "
        "different checkout, so this process reads the right task files and runs a "
        "different branch's code. That mismatch is invisible in `git log` and in the "
        "files on disk.\n\n"
        "Repair it by reinstalling from the checkout that should be running:\n\n"
        f"    cd {expected[0]}\n"
        "    poetry install\n\n"
        "Then restart. If a worktree caused this, `VIRTUAL_ENV` was set when its "
        "bootstrap ran; unset it there so Poetry gives that worktree its own "
        "environment."
    )


def describe_source() -> str:
    """A one-line answer to 'which code is this?', for diagnostics and API responses."""
    imported = imported_source_root()
    if imported is not None:
        return str(imported)
    return str(Path(agentjobs.__file__).resolve().parent)


def verify_source_or_die(project_roots: Iterable[Path] = ()) -> None:
    """Refuse to run when the imported source is not the checkout being served.

    Raises rather than warns. The failure it catches is silent by construction and its
    consequences are not: a dashboard serving an unmerged branch, and dispatches composed
    by code nobody reviewed. A process that stops is trivially recoverable; one that runs
    the wrong code is what took a forensic session to notice.

    ``AGENTJOBS_SKIP_SOURCE_CHECK`` exists for the one case the check cannot reason
    about -- an unusual install layout that trips it -- and prints its own warning so a
    machine running with the guard off says so.
    """
    if os.environ.get("AGENTJOBS_SKIP_SOURCE_CHECK"):
        print(
            "AGENTJOBS_SKIP_SOURCE_CHECK is set: not verifying which checkout this "
            f"process imported. Running {describe_source()}.",
            flush=True,
        )
        return
    problem = source_mismatch(project_roots=project_roots)
    if problem is not None:
        raise SourceMismatchError(problem)
