"""Answer a fake runner script in-process instead of spawning it (task-525).

The dispatch tests stand a small Python script in for the Claude CLI: it answers
``agents --json``, ``logs``, ``stop``, a probe and a launch from a ``ledger.json`` beside
it. Task-518 found the pytest stage bounded by starting those scripts -- one interpreter
per poll, 0.064s to 16.7s each on this machine depending on load -- and task-525 counted
them: about 1,500 spawns across the suite, none of which was the thing a test asserted
on.

:func:`answer_in_process` runs such a script's source in this process through
:mod:`agentjobs.dispatch.program`'s seam, with its own ``sys.argv``, stdin, stdout and
stderr, and hands back the ``CompletedProcess`` a spawn would have. The script is re-read
on every call, so a test that rewrites it mid-scenario is still obeyed.

Its ``sys``, ``os.getcwd`` and ``pathlib.Path.cwd`` are its own, so a fake that records
the directory it was started in records the one a spawn would have.

**Only for a script that is an answer, never for one that is a process.** A script that
reports its own pid, sleeps, forks, exits with ``os._exit``, or is killed by the test is
about a process's lifetime and keeps its real spawn. Every file that installs this says so
beside the install, which is the split task-525's acceptance asks the test files to
state; :func:`spawn_for_real` is the way back for a test whose subject is the spawn.
"""

from __future__ import annotations

import builtins
import io
import os
import pathlib
import sys
import traceback
import types
from pathlib import Path
from typing import Any, Sequence, Tuple

import pytest

from agentjobs.dispatch import program


class _Stream(io.StringIO):
    """A captured stream a script may call ``reconfigure`` on, as the fakes do."""

    def reconfigure(self, **_: Any) -> None:
        pass


def script_answer(script: Path) -> "program.InProcessProgram":
    """An :data:`program.InProcessProgram` that executes ``script`` in this process."""

    def answer(argv: Sequence[str], stdin: str, cwd: Path) -> Tuple[int, str, str]:
        out, err = _Stream(), _Stream()
        shim = types.ModuleType("sys")
        shim.__dict__.update(sys.__dict__)
        shim.__dict__.update(
            argv=[str(script), *argv], stdin=io.StringIO(stdin), stdout=out, stderr=err
        )

        # A spawned script's working directory is the one it was started in, and a fake
        # launch records it in its row, which registration reads back. This process's
        # own directory is the pytest worker's, so the script is given the right one.
        class HerePath(type(Path())):  # type: ignore[misc]
            @classmethod
            def cwd(cls) -> Any:
                return cls(cwd)

        os_shim = types.ModuleType("os")
        os_shim.__dict__.update(os.__dict__)
        os_shim.__dict__.update(getcwd=lambda: str(cwd))
        pathlib_shim = types.ModuleType("pathlib")
        pathlib_shim.__dict__.update(pathlib.__dict__)
        pathlib_shim.__dict__.update(Path=HerePath)
        shims = {"sys": shim, "os": os_shim, "pathlib": pathlib_shim}

        def importer(name: str, *args: Any, **kwargs: Any) -> Any:
            if name in shims:
                return shims[name]
            return builtins.__import__(name, *args, **kwargs)

        def printer(*values: Any, file: Any = None, **kwargs: Any) -> None:
            builtins.print(*values, file=file if file is not None else out, **kwargs)

        scope_builtins = dict(builtins.__dict__, __import__=importer, print=printer)
        scope = {"__name__": "__main__", "__file__": str(script), "__builtins__": scope_builtins}
        code = 0
        try:
            exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"), scope)
        except SystemExit as exit:
            status = exit.code
            if status is None:
                code = 0
            elif isinstance(status, int):
                code = status
            else:
                err.write(f"{status}\n")
                code = 1
        except Exception:  # noqa: BLE001 - what an interpreter does with an uncaught one
            # A script that raises is a process that printed a traceback and exited 1, and
            # the caller has to see exactly that -- `test_an_unreadable_ledger_ends_nothing`
            # feeds the fake a corrupt ledger to get it.
            err.write(traceback.format_exc())
            code = 1
        return code, out.getvalue(), err.getvalue()

    return answer


def answer_in_process(monkeypatch: pytest.MonkeyPatch, script: Path) -> Path:
    """Install ``script`` as an in-process answer for this test, and return it."""
    monkeypatch.setitem(program.INSTALLED, program.key(str(script)), script_answer(script))
    return script


def spawn_for_real(monkeypatch: pytest.MonkeyPatch, script: Path) -> Path:
    """Take ``script``'s in-process answer out again, for a test about the spawn itself.

    A test that replaces ``subprocess.run`` to watch or refuse the runner's spawn -- the
    retry after ``CreateProcess`` fails, the argv a listing is started with -- has the
    spawn as its subject, so it gets a real one.
    """
    monkeypatch.delitem(program.INSTALLED, program.key(str(script)), raising=False)
    return script
