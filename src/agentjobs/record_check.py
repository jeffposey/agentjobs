"""The record-quality check: what a write just left behind, said at the moment it did.

Implements task-306, from finding F-14 of the 2026-08-21 context audit
(``audits/2026-08-21/01-context-architecture.md`` section 6). The audit measured three
drifts away from ALLAGENTS' Resumption Contract that ``agentjobs validate`` cannot see:
``spec.summary`` growing from one or two sentences into a second description (corpus
median 35 words in the 031-105 era, 54 in the 186-243 era), ``claim``'s default
``ball_prompt`` surviving on tasks that are actively being worked, and almost no
``question`` entries ever reaching a record at all.

**The shape is deliberately ``queue_check``'s, and so is the governing rule**: silence
is the normal outcome, and that is a requirement rather than an aspiration. A warning
that fires on an ordinary write is wallpaper, and wallpaper is worse than nothing
because it trains a reader to click past the one that mattered.

**These are addressed to the author of a write, at the moment of the write, and they
are not in ``agentjobs validate``.** That is the whole design, and it is why ``verb``
is a parameter rather than an afterthought. The value of the signal is that the session
which caused the drift is still in context and can fix it in one edit; on today's
corpus the same two conditions evaluated over every record light up more than half of
it, and those records were written by sessions that ended days ago. So a condition
fires only for the verb that could have caused it -- claiming a task written by someone
else says nothing about its summary, because the claimer neither wrote that summary nor
should rewrite it.

Nothing here refuses anything, nothing exits non-zero, and nothing rewrites a record.
The corpus-wide view still exists for whoever is auditing rather than authoring:
``scripts/corpus_stats.py`` calls :func:`check_record` with no verb and counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from .models_v2 import Ball, BallReason, LogEntryType, Task

__all__ = [
    "DEFAULT_BALL_PROMPT",
    "DEFAULT_BALL_PROMPTS",
    "LOG_APPENDING_VERBS",
    "LONG_SUMMARY",
    "SPEC_WRITING_VERBS",
    "SUMMARY_WORD_CEILING",
    "WARNING_KINDS",
    "RecordWarning",
    "check_record",
    "summary_words",
    "warning_dicts",
]

#: ``spec.summary`` has grown past the point where it is still "one or two sentences
#: that orient a zero-context reader" and has become a second ``spec.description``.
LONG_SUMMARY = "long_summary"

#: The task is being worked and still carries the ask a verb wrote for it, so nobody
#: has said what it is actually waiting on.
DEFAULT_BALL_PROMPT = "default_ball_prompt"

#: Every kind this module can produce. The closed set a caller may branch on.
WARNING_KINDS: Tuple[str, ...] = (LONG_SUMMARY, DEFAULT_BALL_PROMPT)

#: Where a summary stops being one or two sentences. The audit's number, and the one
#: it measured the corpus against; two long sentences fit comfortably under it.
SUMMARY_WORD_CEILING = 40

#: The prompts AgentJobs writes when nobody has said anything: ``claim``'s ask for an
#: ordinary task, ``create``'s ask for a draft, and the migration's version of the
#: second. Held here rather than imported from the three modules that write them so
#: this module depends on nothing but the model -- ``tests/test_record_check.py`` pins
#: the set against those three sources, which is what would otherwise drift silently
#: and turn the check into a no-op nobody notices.
DEFAULT_BALL_PROMPTS: FrozenSet[str] = frozenset(
    {
        "Execute the spec; log progress and hand off when done.",
        "Finish specifying this task.",
        "Finish specifying this imported task.",
    }
)

#: Verbs that write ``spec``. Only these can have caused a long summary.
SPEC_WRITING_VERBS: FrozenSet[str] = frozenset({"create", "update_content"})

#: Verbs that append to the log of a task somebody is working. Only these can have
#: caused a default prompt to survive: the claim that wrote it was doing its job, and
#: ``handoff`` cannot leave one behind because the schema makes it state an ask.
LOG_APPENDING_VERBS: FrozenSet[str] = frozenset({"log_append"})


@dataclass(frozen=True)
class RecordWarning:
    """One thing a write left the record less useful for, and what to do about it.

    ``message`` is written to be read on its own, in a tool result next to a success
    sentence, by an agent that will not go and look anything up. It therefore says what
    is true, why the contract asks otherwise, and what the fix is -- not a rule id.
    """

    kind: str
    message: str

    def as_dict(self) -> Dict[str, str]:
        """The structured form, for a tool result payload."""
        return {"kind": self.kind, "message": self.message}


def warning_dicts(warnings: Sequence[RecordWarning]) -> List[Dict[str, str]]:
    """Render warnings for a structured result."""
    return [warning.as_dict() for warning in warnings]


def summary_words(summary: str) -> int:
    """How many words a summary is, counted the way the audit counted them.

    Whitespace-separated tokens. Markdown, punctuation and inline code are left alone
    on purpose: a cleverer count would disagree with the figures in
    ``docs/task-corpus-audit.md`` and with the audit that produced the ceiling.
    """
    return len(summary.split())


def check_record(task: Task, *, verb: Optional[str] = None) -> List[RecordWarning]:
    """What is wrong with this record that the write named by ``verb`` could have caused.

    ``verb`` is one of the manager's operation kinds -- ``create``, ``update_content``,
    ``log_append`` and the state verbs. A verb that could not have caused a condition
    does not raise it, which is what keeps the normal write silent.

    ``verb=None`` evaluates every condition, for the corpus view. Do not use that form
    on a write path: it is how the check becomes the lint backlog it exists instead of.
    """
    warnings: List[RecordWarning] = []
    if verb is None or verb in SPEC_WRITING_VERBS:
        warnings.extend(_check_summary(task))
    if verb is None or verb in LOG_APPENDING_VERBS:
        warnings.extend(_check_ball_prompt(task))
    return warnings


def _check_summary(task: Task) -> List[RecordWarning]:
    """``spec.summary`` against the Resumption Contract's "one or two sentences"."""
    words = summary_words(task.spec.summary)
    if words <= SUMMARY_WORD_CEILING:
        return []
    return [
        RecordWarning(
            LONG_SUMMARY,
            f"spec.summary is {words} words. The Resumption Contract asks for one or "
            "two sentences that orient a zero-context reader, distinct from "
            f"spec.description; past {SUMMARY_WORD_CEILING} it has become a second "
            "description, and the reader who needed orienting has to parse it too. "
            "Move the detail into spec.description.",
        )
    ]


def _check_ball_prompt(task: Task) -> List[RecordWarning]:
    """A default ask still standing on a task somebody has been working."""
    if task.ball is not Ball.AGENT or task.ball_reason is not BallReason.WORK:
        return []
    prompt = (task.ball_prompt or "").strip()
    if prompt not in DEFAULT_BALL_PROMPTS:
        return []
    since = _entries_since_claim(task)
    if since < 1:
        # The claim that wrote this prompt is the newest thing on the record, so it is
        # doing exactly its job. Nobody has had a chance to know better yet.
        return []
    entries = "entry has" if since == 1 else "entries have"
    return [
        RecordWarning(
            DEFAULT_BALL_PROMPT,
            f"ball_prompt is still the one the claim wrote, and {since} log {entries} "
            "been added since. A session resuming this task reads ball_prompt "
            "before anything else and would learn nothing from it. Hand off, or say "
            "what the task is waiting on now.",
        )
    ]


def _entries_since_claim(task: Task) -> int:
    """Log entries added after the transition that put this task at ``agent/work``.

    The claim's own transition is the boundary because it is what wrote the prompt.
    Counting from the start of the log instead would fire on the claim itself for any
    task with a history, which is every task.

    A record at ``agent/work`` with no such transition was not built by the verbs, so
    there is no boundary to count from and the whole log counts. That case should not
    exist; it is handled rather than assumed away.
    """
    boundary = -1
    for index, entry in enumerate(task.log):
        if entry.type is not LogEntryType.TRANSITION:
            continue
        data = entry.data or {}
        if data.get("ball_reason") == BallReason.WORK.value:
            boundary = index
    return len(task.log) - boundary - 1
