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
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, NamedTuple, Optional

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


# ----- what this process is running, fixed at the moment it started ------------
#
# `describe_source` answers "which directory did I import from". That is enough to
# catch a wrongly-installed process and not enough to catch a *stale* one: a server
# started before a merge imports from exactly the right directory and runs exactly the
# wrong code. The two values below close that gap, and both are captured once and never
# recomputed, which is the only property that makes them worth anything.
#
# Recomputing on request would defeat the whole point. A running server whose clone has
# since been merged into would read the new HEAD off disk and report the merge commit
# while still executing the code it loaded an hour ago -- which is precisely the answer
# that must not be given, because the scripted finish (task-241) verifies a delivery by
# asking for it.

_GIT_TIMEOUT_SECONDS = 10
"""Ceiling on the one git call this module makes, so a wedged git cannot hang a boot."""

_IDENTITY: Optional["SourceIdentity"] = None


class SourceIdentity(NamedTuple):
    """What this process is running, as of the moment it started.

    ``commit`` is ``None`` when the source is not a git checkout -- an ordinary
    installation, which is the common case for anyone who did not clone. A caller that
    needs to prove which code is live treats ``None`` as "cannot be proven", never as
    "close enough".
    """

    commit: Optional[str]
    started_at: str


def _head_commit(root: Optional[Path]) -> Optional[str]:
    """The git commit at ``root``'s HEAD, or None when that cannot be established."""
    if root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    commit = (result.stdout or "").strip()
    return commit or None


def capture_source_identity() -> SourceIdentity:
    """Fix this process's identity, if it has not already been fixed. Idempotent.

    Call it from a long-lived process's startup, before it begins serving. Every later
    call -- including the ones a request handler makes -- returns what was captured
    then, so the answer describes the code in memory rather than the files on disk.

    A short-lived process that never calls it at startup captures on first use instead.
    That is a weaker guarantee and an honest one: it is still the truth at some point
    before the answer was given, and the alternative is paying for a subprocess at
    import time in every CLI invocation that will never ask.
    """
    global _IDENTITY
    if _IDENTITY is None:
        _IDENTITY = SourceIdentity(
            commit=_head_commit(imported_source_root()),
            started_at=datetime.now(timezone.utc).isoformat(),
        )
    return _IDENTITY


def source_identity() -> SourceIdentity:
    """This process's captured identity. Captures now if startup did not."""
    return capture_source_identity()
