"""Where dispatch runs the runner's own program, and the one seam for answering it in-process.

Every question the dispatch subsystem asks a runner -- ``agents --json``, ``logs``,
``stop``, a launch, a resume, an auth probe, a peer message -- is a separate program on
the machine, and in production it stays one: the runner is not ours to import. Those call
sites go through :func:`run` rather than :func:`subprocess.run` directly.

**Why the seam exists (task-525).** A test about a *decision* -- does the controller
reconcile this row, does recovery page once, is this stall past its threshold -- needs the
runner's answer, not a process that prints it. Task-518 measured the dispatch replay
harness spawning 120 short-lived interpreters across three tests, each costing anywhere
from 0.064s to 16.7s on this machine depending on what else was running, and none of them
the subject of the test. Installing an answer in :data:`INSTALLED` lets such a test answer a runner program's
commands in-process: same argv, same stdin, same working directory, same
``CompletedProcess`` back.

**What it must not be used for** is a test whose subject is a process -- a start, a kill,
a grandchild, pid reuse. Those keep their real spawn, and nothing here can reach them:
only the runner-program call sites route through :func:`run`, and an argv naming no
installed program falls straight through to :func:`subprocess.run`.

In production the table is empty, so the cost is one dictionary lookup per spawn.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

#: ``(argv after the program, stdin, cwd) -> (exit code, stdout, stderr)``.
InProcessProgram = Callable[[Sequence[str], str, Path], "tuple[int, str, str]"]

#: Installed answers, keyed by :func:`key` of the program they stand in for. Empty in
#: production. A test installs one with ``monkeypatch.setitem(INSTALLED, key(path),
#: answer)``, so pytest takes it out again whatever the test does.
INSTALLED: Dict[str, InProcessProgram] = {}


def key(program: str) -> str:
    """The table key for a program: an executable, or the script an interpreter runs.

    An argv is matched on its first two elements, so ``python claude.py agents`` is
    answered by whatever was installed under ``key("claude.py")``'s full path.
    """
    return os.path.normcase(os.path.abspath(program))


def _installed_for(argv: Sequence[str]) -> Optional["tuple[InProcessProgram, int]"]:
    if not INSTALLED:
        return None
    for index, element in enumerate(list(argv)[:2]):
        answer = INSTALLED.get(key(str(element)))
        if answer is not None:
            return answer, index + 1
    return None


def run(argv: Sequence[str], **kwargs: Any) -> "subprocess.CompletedProcess[Any]":
    """:func:`subprocess.run`, unless a test installed an answer for this program.

    ``subprocess.run`` is looked up at call time, so a test that replaces it -- the replay
    harness's crash children do -- still sees every call made through here.
    """
    found = _installed_for(argv)
    if found is None:
        return subprocess.run(list(argv), **kwargs)
    answer, skip = found
    cwd = Path(kwargs.get("cwd") or Path.cwd())
    code, stdout, stderr = answer(list(argv)[skip:], kwargs.get("input") or "", cwd)
    return subprocess.CompletedProcess(list(argv), code, stdout, stderr)
