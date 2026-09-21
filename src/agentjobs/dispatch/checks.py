"""Run a task's executable acceptance checks, and say what each one did (task-147).

An acceptance criterion may carry a ``check``: an argv list whose exit code decides
whether the criterion is ``met`` or ``failed``. This module is the only thing that runs
one, and :func:`evaluate_task` is its whole public surface.

Three properties are the point of it, and each is load-bearing for the loop driver that
will read these results (task-150):

1.  **A check that cannot run is ``failed``, never skipped.** A missing executable, a
    timeout, a directory that no longer exists -- all failures, each with the cause
    named. If a broken check were "unknown" and unknown were tolerated, a loop could
    converge on a green run by breaking its own tests.
2.  **Nothing runs until the dispatch gates open.** ``assert_dispatch_permitted`` is the
    same gate a dispatch passes, called here before a single process is started, and its
    refusals keep their own error classes so the caller renders them under the code the
    gate chose rather than as a generic failure.
3.  **Never on a read path.** Nothing in this module is reachable from loading, listing
    or rendering a task. The two callers are ``agentjobs check`` and one POST endpoint,
    both of which a person or an authorised loop has to ask for.

**No shell, ever.** ``check`` is a list and is passed to ``subprocess`` as a list. There
is no string form to split, so there is nothing for a quoting rule to get wrong and no
argument a task record can smuggle past one.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from ..models_v2 import AcceptanceStatus, CheckOutcome, Task
from ..projects import Project
from .config import assert_dispatch_permitted
from .runner import OUTPUT_TAIL_LINES, readable_tail

CHECK_TIMEOUT_SECONDS = 300.0
"""How long one check may run before it is killed and recorded as ``failed``.

Five minutes is a test suite, not a build. A check is meant to answer one criterion, and
one that needs longer than this is better expressed as a criterion a person verifies --
the loop this feeds re-runs the whole set on every turn, and a ten-minute check makes
every turn ten minutes long.
"""

PASS_TIMEOUT_SECONDS = 900.0
"""How long a whole pass may run. Checks not reached by then are ``failed``.

The per-check budget bounds one process and this bounds the set, because ten checks of
four minutes each is not a bounded pass however well behaved each one is.
"""


class NoChecksError(Exception):
    """The task has no executable checks, so there is nothing for a pass to decide.

    Refused rather than answered with an empty vector: zero of zero passing is not a
    definition of done being met, and a caller that got an empty success back would be
    entitled to read it as one.
    """

    reason = "no_checks"


@dataclass(frozen=True)
class CheckReport:
    """What one pass did: an outcome per check, and the criteria it did not decide."""

    results: List[CheckOutcome]
    unchecked: List[str]

    @property
    def failed(self) -> List[CheckOutcome]:
        """The outcomes that did not pass, in the task's own order."""
        return [item for item in self.results if item.status is AcceptanceStatus.FAILED]

    @property
    def ok(self) -> bool:
        """True when every check that ran exited 0."""
        return not self.failed


def _tail(text: str) -> Optional[str]:
    """The last lines of a check's output, or ``None`` when it printed nothing.

    ``readable_tail`` is the runner's, so a tail in a ``check_result`` entry reads the
    same as a tail in a ``dispatch_result`` one -- these end up side by side in the same
    log and a reader should not have to learn two conventions.
    """
    trimmed = readable_tail(text, OUTPUT_TAIL_LINES)
    return trimmed or None


def run_check(
    argv: Sequence[str],
    *,
    cwd: Path,
    task_id: str,
    criterion_id: str,
    timeout: float = CHECK_TIMEOUT_SECONDS,
) -> CheckOutcome:
    """Run one check and say what it did. Never raises for the check's own behaviour.

    Every way a check can go wrong is an outcome rather than an exception, because the
    caller's job is to record all of them on one entry: an exception from the third
    check would discard what the first two proved.

    The environment is inherited plus ``AGENTJOBS_TASK_ID`` and nothing else. No
    credential, token or run secret is injected -- a check is a command out of a task
    record, which is a document several agents can write, and it gets no authority the
    shell that started the server did not already have.
    """
    environment = dict(os.environ)
    environment["AGENTJOBS_TASK_ID"] = task_id
    started = time.perf_counter()
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never a shell string
            list(argv),
            cwd=str(cwd),
            env=environment,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as expired:
        # `expired.stdout` is bytes or str depending on how far it got, so both are
        # coerced rather than assumed: losing the output of a check that hung is losing
        # the only clue about where it hung.
        captured = "".join(
            part.decode("utf-8", "replace") if isinstance(part, bytes) else (part or "")
            for part in (expired.stdout, expired.stderr)
        )
        return CheckOutcome(
            id=criterion_id,
            status=AcceptanceStatus.FAILED,
            exit_code=None,
            duration_seconds=time.perf_counter() - started,
            cause="timeout",
            output_tail=_tail(
                captured + f"\n[agentjobs] killed after {timeout:.0f}s without exiting"
            ),
        )
    except OSError as error:
        # A missing executable, a cwd that is gone, a file that is not executable. The
        # criterion fails and names the cause, which is the rule this module exists for.
        return CheckOutcome(
            id=criterion_id,
            status=AcceptanceStatus.FAILED,
            exit_code=None,
            duration_seconds=time.perf_counter() - started,
            cause="not_started",
            output_tail=_tail(f"[agentjobs] could not start {list(argv)!r}: {error}"),
        )
    duration = time.perf_counter() - started
    return CheckOutcome(
        id=criterion_id,
        status=(
            AcceptanceStatus.MET if completed.returncode == 0 else AcceptanceStatus.FAILED
        ),
        exit_code=completed.returncode,
        duration_seconds=duration,
        output_tail=_tail(f"{completed.stdout}\n{completed.stderr}"),
    )


def evaluate_task(
    task: Task,
    *,
    project: Project,
    home: Optional[Path] = None,
    per_check_timeout: float = CHECK_TIMEOUT_SECONDS,
    pass_timeout: float = PASS_TIMEOUT_SECONDS,
) -> CheckReport:
    """Run every check on ``task``, in the task's own order, behind the dispatch gates.

    The working directory is the project root from the registry -- not a worktree, and
    not the caller's cwd. A check is a property of the task, so it must mean the same
    thing whoever asks for it and from wherever; resolving it against whatever directory
    a CLI happened to be run from would make the same criterion pass in one shell and
    fail in the next.

    Raises :class:`NoChecksError` when nothing on the task carries a check, and whatever
    ``assert_dispatch_permitted`` raises when this machine or this project is not
    permitted to run anything.
    """
    checked = [item for item in task.acceptance if item.check]
    if not checked:
        raise NoChecksError(
            f"{task.id} has no acceptance criteria with a check, so there is nothing to "
            "run. Add a `check` argv to a criterion first."
        )
    # Before a single process starts, and by the same call a dispatch makes: running a
    # command out of a task record is the act the gates exist to bound, and a check is
    # not a smaller version of it.
    assert_dispatch_permitted(project.id, home)

    results: List[CheckOutcome] = []
    deadline = time.perf_counter() + pass_timeout
    for criterion in checked:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            # Failed rather than skipped, for the module's central rule: a criterion
            # nothing decided must not read as one that passed.
            results.append(
                CheckOutcome(
                    id=criterion.id,
                    status=AcceptanceStatus.FAILED,
                    exit_code=None,
                    duration_seconds=0.0,
                    cause="pass_timeout",
                    output_tail=(
                        f"[agentjobs] the pass ran out of its {pass_timeout:.0f}s budget "
                        "before this check was started"
                    ),
                )
            )
            continue
        results.append(
            run_check(
                criterion.check or [],
                cwd=project.root,
                task_id=task.id,
                criterion_id=criterion.id,
                timeout=min(per_check_timeout, remaining),
            )
        )
    return CheckReport(
        results=results,
        unchecked=[item.id for item in task.acceptance if not item.check],
    )


__all__ = [
    "CHECK_TIMEOUT_SECONDS",
    "PASS_TIMEOUT_SECONDS",
    "CheckReport",
    "NoChecksError",
    "evaluate_task",
    "run_check",
]
