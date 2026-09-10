"""What a dispatcher does about the task record it wrote. Since task-402: nothing.

This module existed because a dispatcher's writes landed in a working tree. A project
that kept task records in the repository being dispatched -- this one did -- had its
tasks directory dirtied at both ends of every run: the claim wrote the record before the
spawn, and the terminal ``dispatch_result`` after the run's last commit. Two consequences
followed, and both were workarounds rather than design:

*   The dispatcher committed the record itself, because nobody else would (task-182).
*   The clean-tree gate excluded the tasks directory, because otherwise every dispatch
    was refused on the strength of AgentJobs' own writes -- and the cost, recorded in the
    design doc, was that real changes under ``tasks/`` stopped being seen.

A record is a row now. A write leaves no working tree dirty, so there is nothing to
commit and nothing to exclude, and the clean-tree gate gets back the coverage task-182
had to give up. The two functions remain as the answers to questions the dispatch code
still asks; they no longer branch, and there is no longer a second answer for them to
give.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from agentjobs.store_factory import TaskManagerLike

__all__ = ["CommitOutcome", "commit_task_record", "task_file_exclusions"]


def task_file_exclusions(manager: TaskManagerLike) -> List[Path]:
    """Paths the clean-tree check must ignore because AgentJobs itself writes them.

    Empty, always. Kept as a call rather than deleted at its two call sites because
    "what does AgentJobs write into this checkout" is a real question for the gate to
    ask, and the answer being *nothing* is the property worth stating in one place.
    """
    del manager
    return []


@dataclass(frozen=True)
class CommitOutcome:
    """What the attempt did, for the caller to log rather than to branch on."""

    committed: bool
    detail: str
    path: Optional[Path] = None

    def __str__(self) -> str:
        return self.detail


def commit_task_record(
    manager: TaskManagerLike,
    task_id: str,
    *,
    subject: Optional[str] = None,
    actor: Optional[str] = None,
) -> CommitOutcome:
    """Report that there is no task file to commit.

    Returns rather than raises, as it always did: the caller is finishing a run, and an
    exception there would turn a bookkeeping detail into a failed dispatch.
    """
    del manager, subject, actor
    return CommitOutcome(
        False,
        f"{task_id} is a record in the AgentJobs database, not a file in this "
        "checkout: nothing to commit",
        None,
    )
