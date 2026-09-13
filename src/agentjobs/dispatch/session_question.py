"""Telling a question addressed to the conversation apart from a permission park (task-441).

Claude Code reports a session showing its multiple-choice question menu exactly as it
reports one waiting on a permission prompt: ``waiting``/``blocked``. The ledger cannot
tell them apart, and the difference matters. A permission prompt is a session that will
wait indefinitely for somebody to notice it. A question is addressed to whoever is in the
conversation -- and on 2026-09-13 that was the owner, typing into the same session through
Remote Control, who was then paged "Needs input" for a question they were already
answering.

**The distinction is read from the session's JSONL, never from its screen.** The question
is an assistant ``tool_use`` named ``AskUserQuestion``, and its answer is the ``tool_result``
that names that ``tool_use`` id, so "a question is pending" is a fact about two parsed
records. Matching the menu's footer text on the terminal was rejected: any session that
prints that text -- this module's own author grepped a transcript for it -- would read as
asking a question, and a false question suppresses a real park.

**Every uncertainty lands on the park.** No transcript, no parseable record, or a pending
tool call of any other name: :func:`read_pending_question` returns ``None`` and the
session is parked exactly as before. That is the constraint the task was written under.

Shares ``dispatch.auth``'s transcript reading -- the same window, the same parse, the same
session check -- because the questions it answers are about the same file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from agentjobs.dispatch.auth import (
    TAIL_BYTES,
    _belongs_to,
    _entries,
    _moment,
    _tail_lines,
    session_log_path,
)

QUESTION_TOOL = "AskUserQuestion"
"""The ``tool_use`` name of Claude Code's multiple-choice question, as its JSONL records it."""

PRESENCE_WINDOW_SECONDS = 600
"""How recent a person's own message must be for a question to count as asked of them live.

Ten minutes: the 2026-09-13 question came three seconds after the owner's message, and a
conversation idle for longer than this is one nobody should assume is still being read.
"""

QUESTION_GRACE_SECONDS = 600
"""How long a question asked in a live conversation waits before it becomes a handoff.

The deadline is what keeps a mistaken "live" from becoming a silent hang: whatever the
presence evidence said, an unanswered question is on the task record ten minutes later.
"""

QUESTION_TEXT_MAX_BYTES = 4096
"""The most question text a ball prompt carries. A few KB, never a transcript."""


@dataclass(frozen=True)
class QuestionOption:
    label: str
    description: str


@dataclass(frozen=True)
class AskedQuestion:
    question: str
    header: str
    options: Tuple[QuestionOption, ...]
    multi_select: bool


@dataclass(frozen=True)
class PendingQuestion:
    """A question tool call with no answer yet, and who was in the conversation when it came."""

    tool_use_id: str
    at: datetime
    """When the question was asked, in UTC. The idempotency key for its handoff."""
    questions: Tuple[AskedQuestion, ...]
    last_human_at: Optional[datetime]
    """The newest message a person typed into this session before the question, if any."""
    log_path: Path

    def live(self) -> bool:
        """Whether a person was in the conversation when the question was asked."""
        if self.last_human_at is None:
            return False
        return (self.at - self.last_human_at).total_seconds() <= PRESENCE_WINDOW_SECONDS


def read_pending_question(
    session_id: str,
    *,
    home: Optional[Path] = None,
    since: Optional[datetime] = None,
    exclude_texts: Iterable[str] = (),
    tail_bytes: int = TAIL_BYTES,
) -> Optional[PendingQuestion]:
    """The question this session is waiting on an answer to, or ``None``.

    ``since`` is the run's start, as for the auth reader: a question asked before this run
    began is not this run's. ``exclude_texts`` are messages that arrive as human turns but
    were not typed by anyone present -- the dispatch prompt itself is recorded with
    ``origin.kind: human``, and counting it would make every question asked in a run's
    first ten minutes look live.
    """
    path = session_log_path(session_id, home=home)
    if path is None:
        return None
    try:
        lines = _tail_lines(path, tail_bytes)
    except OSError:  # pragma: no cover - the transcript went away mid-read
        return None
    pending = pending_question_in(
        lines, session_id=session_id, path=path, exclude_texts=exclude_texts
    )
    if pending is None or (since is not None and pending.at < since):
        return None
    return pending


def pending_question_in(
    lines: Iterable[str],
    *,
    session_id: str,
    path: Path,
    exclude_texts: Iterable[str] = (),
) -> Optional[PendingQuestion]:
    """Walk a transcript window forwards; a question is pending only if nothing came after it.

    Anything the main conversation records after the question -- its answer, or any other
    turn -- means the menu is no longer what the session is waiting on. Sidechain records
    are a subagent's and say nothing about the session's own prompt.
    """
    excluded = {_normalise(text) for text in exclude_texts if text}
    pending: Optional[PendingQuestion] = None
    last_human: Optional[datetime] = None
    for entry in _entries(lines):
        if entry.get("isSidechain") is True or not _belongs_to(entry, session_id):
            continue
        kind = entry.get("type")
        if kind not in {"user", "assistant"}:
            continue
        at = _moment(entry.get("timestamp"))
        if at is None:
            continue
        if kind == "user":
            if pending is not None:
                pending = None
            if _is_human_turn(entry) and _normalise(_user_text(entry)) not in excluded:
                last_human = at
            continue
        asked = _question_call(entry)
        if asked is None:
            pending = None
            continue
        tool_use_id, questions = asked
        pending = PendingQuestion(
            tool_use_id=tool_use_id,
            at=at,
            questions=questions,
            last_human_at=last_human,
            log_path=path,
        )
    return pending


def render_questions(questions: Iterable[AskedQuestion]) -> str:
    """The questions as a person would read them, capped at ``QUESTION_TEXT_MAX_BYTES``."""
    blocks: List[str] = []
    for asked in questions:
        lines = [f"**{asked.header}:** {asked.question}" if asked.header else asked.question]
        for number, option in enumerate(asked.options, start=1):
            detail = f" — {option.description}" if option.description else ""
            lines.append(f"{number}. {option.label}{detail}")
        blocks.append("\n".join(lines))
    text = "\n\n".join(blocks)
    encoded = text.encode("utf-8")
    if len(encoded) <= QUESTION_TEXT_MAX_BYTES:
        return text
    marker = "…"
    room = QUESTION_TEXT_MAX_BYTES - len(marker.encode("utf-8"))
    return encoded[:room].decode("utf-8", errors="ignore") + marker


def _question_call(entry: dict) -> Optional[Tuple[str, Tuple[AskedQuestion, ...]]]:
    """The id and questions of an assistant turn that is a question tool call, else ``None``."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == QUESTION_TOOL
        ):
            tool_use_id = block.get("id")
            if not isinstance(tool_use_id, str) or not tool_use_id:
                return None
            return tool_use_id, _parse_questions(block.get("input"))
    return None


def _parse_questions(raw: object) -> Tuple[AskedQuestion, ...]:
    questions = raw.get("questions") if isinstance(raw, dict) else None
    if not isinstance(questions, list):
        return ()
    parsed: List[AskedQuestion] = []
    for item in questions:
        if not isinstance(item, dict):
            continue
        options = item.get("options")
        parsed.append(
            AskedQuestion(
                question=str(item.get("question") or ""),
                header=str(item.get("header") or ""),
                options=tuple(
                    QuestionOption(
                        label=str(option.get("label") or ""),
                        description=str(option.get("description") or ""),
                    )
                    for option in (options if isinstance(options, list) else [])
                    if isinstance(option, dict)
                ),
                multi_select=item.get("multiSelect") is True,
            )
        )
    return tuple(parsed)


def _is_human_turn(entry: dict) -> bool:
    """A message a person typed, as opposed to a tool result or a harness-injected turn."""
    if entry.get("isMeta") is True:
        return False
    origin = entry.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == "human"


def _user_text(entry: dict) -> str:
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _normalise(text: str) -> str:
    return " ".join(text.split())
