"""What a finish intended and what it proved, per task, surviving every attempt (task-322).

A finish directory is one attempt. Recovery is a question about *all* of them: did some
earlier attempt merge this branch, and how far did its delivery get? Until this module the
only cross-attempt answer was the task log's merge entry, and that is written *after*
``git merge`` returns -- so a finish killed between the two left a merge that nothing on
disk could account for, and the retry concluded somebody had merged it by hand.

**Intent before the effect, result after it.** Each line is one fact, appended and synced
before the caller moves on. A line naming an intent with no later result is exactly the
crash window §9a of ``docs/agent-dispatch-design.md`` asks a recovery to reconcile, and
it is reconciled against the world (git, the serving process), never assumed.

**Why a file and not the execution store.** Recorded on task-322 as a decision: a finish
may run for a task that was never dispatched, which has no execution row, and inventing
one would put a finish inside admission and the one-open-execution invariant. The run
lock and the merge runway already make the finisher the only writer of this file.

Reads never raise and skip a torn line. ``intend`` is the one write whose failure matters,
so it raises and the caller decides: before ``git merge`` a merge intent that cannot be
written stops the finish, because a merge nothing can later account for is the failure
this exists to prevent.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from agentjobs.dispatch import clock as dispatch_clock
from pathlib import Path
from typing import Any, Dict, List, Optional

RECEIPTS_DIRNAME = "receipts"

INTENT = "intent"
RESULT = "result"

APPLIED = "applied"
NOT_APPLIED = "not_applied"
UNKNOWN = "unknown"


def receipts_path(home: Path, project_id: str, task_id: str) -> Path:
    """``finishes/receipts/<project>~<task>.jsonl``, named the way task locks are."""
    from agentjobs.dispatch.finish import finishes_root

    name = f"{project_id or '_'}~{task_id}.jsonl"
    return finishes_root(home) / RECEIPTS_DIRNAME / name


@dataclass(frozen=True)
class MergeEvidence:
    """A merge some attempt of this finish made, and what it was made from."""

    commit: str
    branch: str
    base: str
    base_before: str
    reviewed_head: str
    finish_id: str
    source: str
    """Where it was proved from: ``receipt``, ``git`` (a reconciled intent) or ``log``."""


class FinishReceipts:
    """The receipts file for one task."""

    def __init__(self, home: Path, project_id: str, task_id: str) -> None:
        self.path = receipts_path(home, project_id, task_id)
        self.project_id = project_id
        self.task_id = task_id

    # ----- writing ------------------------------------------------------------------

    def _append(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, default=str, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def intend(self, finish_id: str, activity: str, key: str, **fields: Any) -> None:
        """Record that ``activity`` is about to happen. Raises ``OSError`` if it cannot."""
        self._append(
            {
                "ts": dispatch_clock.utcnow().isoformat(),
                "phase": INTENT,
                "finish_id": finish_id,
                "activity": activity,
                "key": key,
                "input": fields,
            }
        )

    def settle(self, finish_id: str, activity: str, key: str, state: str, **fields: Any) -> bool:
        """Record what happened. Returns whether it was written; never raises.

        A result that cannot be written leaves an intent without a result, which is the
        state a recovery already reconciles against the world. Failing the finish over it
        would turn a lost receipt into a lost delivery.
        """
        try:
            self._append(
                {
                    "ts": dispatch_clock.utcnow().isoformat(),
                    "phase": RESULT,
                    "finish_id": finish_id,
                    "activity": activity,
                    "key": key,
                    "state": state,
                    "result": fields,
                }
            )
        except (OSError, TypeError, ValueError):
            return False
        return True

    # ----- reading ------------------------------------------------------------------

    def entries(self) -> List[Dict[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        found: List[Dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                loaded = json.loads(line)
            except ValueError:
                continue
            if isinstance(loaded, dict):
                found.append(loaded)
        return found

    def result(self, activity: str, key: str) -> Optional[Dict[str, Any]]:
        """The newest result for this activity and key, or ``None``."""
        for entry in reversed(self.entries()):
            if (
                entry.get("phase") == RESULT
                and entry.get("activity") == activity
                and entry.get("key") == key
            ):
                return entry
        return None

    def applied(self, activity: str, key: str) -> bool:
        found = self.result(activity, key)
        return found is not None and found.get("state") == APPLIED

    def unsettled(self, activity: str) -> List[Dict[str, Any]]:
        """Intents for ``activity`` with no result after them, oldest first."""
        open_intents: Dict[str, Dict[str, Any]] = {}
        for entry in self.entries():
            if entry.get("activity") != activity:
                continue
            identity = f"{entry.get('finish_id')}|{entry.get('key')}"
            if entry.get("phase") == INTENT:
                open_intents[identity] = entry
            elif entry.get("phase") == RESULT:
                open_intents.pop(identity, None)
        return list(open_intents.values())

    def applied_merge(self) -> Optional[MergeEvidence]:
        """The newest merge a receipt says was applied, with the intent it came from."""
        entries = self.entries()
        for index in range(len(entries) - 1, -1, -1):
            entry = entries[index]
            if (
                entry.get("phase") != RESULT
                or entry.get("activity") != "merge"
                or entry.get("state") != APPLIED
            ):
                continue
            result = entry.get("result") or {}
            intent = next(
                (
                    item
                    for item in reversed(entries[:index])
                    if item.get("phase") == INTENT
                    and item.get("activity") == "merge"
                    and item.get("key") == entry.get("key")
                ),
                {},
            )
            source = intent.get("input") or {}
            commit = str(result.get("merge_commit") or "")
            if not commit:
                continue
            return MergeEvidence(
                commit=commit,
                branch=str(source.get("branch") or result.get("branch") or ""),
                base=str(source.get("base") or ""),
                base_before=str(source.get("base_before") or ""),
                reviewed_head=str(source.get("reviewed_head") or ""),
                finish_id=str(entry.get("finish_id") or ""),
                source=str(result.get("reconciled_from") or "receipt"),
            )
        return None


__all__ = [
    "APPLIED",
    "INTENT",
    "NOT_APPLIED",
    "RESULT",
    "UNKNOWN",
    "FinishReceipts",
    "MergeEvidence",
    "receipts_path",
]
