"""A dispatched session's structured transcript, rendered for a person to read.

The Output panel on the task page used to tail ``transcript.log`` -- the pty stream a
``<runner> logs`` subprocess returns. That stream is a **repaint**, not a log. A TUI
draws a space by emitting ``ESC[1C`` (cursor-forward) and a line break by emitting
``ESC[5;3H`` (absolute cursor position), so it rarely emits ``U+0020`` at all. Anything
that removes escape sequences and keeps what is left therefore deletes every space and
every line break, which is how an approval dialog came to render as::

    NewMCPserverfoundinthisproject:agentjobsMCPserversmayexecutecodeoraccess...

A better regex does not fix that, because the information is in the escape sequences
rather than in spite of them. The fix is to stop reading the repaint: Claude Code
already writes a **structured** transcript, one JSON object per line, and everything a
reader wants is in it -- the assistant's prose, each tool call with its input, whether
the call failed, and the patch an edit applied.

**Nothing in AgentJobs decides anything on this file.** Run state comes from the ledger,
as it always has. This module is rendering, and it is deliberately forgiving: the format
belongs to Claude Code and may grow event types and content blocks at any time, so an
unrecognised shape is skipped rather than raised. A transcript this module cannot read
at all is reported as absent, and the caller falls back to the raw capture -- which is
also the only evidence available when a session dies in a way no renderer models.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from agentjobs.dispatch.runner import strip_ansi

CLAUDE_HOME_DIRNAME = ".claude"
PROJECTS_DIRNAME = "projects"

TRANSCRIPT_BYTE_WINDOW = 2_000_000
"""How much of the end of a transcript is parsed.

A session's JSONL grows for as long as the session does -- four megabytes is an ordinary
afternoon -- and a panel showing the last few dozen steps has no use for the beginning.
Reading the tail bounds the cost of a poll to something that does not grow with the
length of the run, which matters because every watching browser triggers one.

The first line inside the window is almost always a fragment and is dropped.
"""

DEFAULT_ENTRY_LIMIT = 40
"""How many entries the panel is handed by default.

"Last 40 lines" was meaningless for a repaint and would be meaningless here too. An
*entry* is one thing the agent did: a paragraph of narration, or a run of tool calls
collapsed into a single summary. Forty of those is a long scroll of real work.
"""

MAX_ENTRY_LIMIT = 200

PROMPT_CHARS = 800
"""A prompt is quoted, not reproduced. A dispatch prompt is several hundred words."""

CALL_DETAIL_CHARS = 600
CALL_OUTPUT_CHARS = 600
"""What a call's input and result are trimmed to before they cross the wire.

The tail is kept rather than the head: a command that failed says why at the end, and
the last lines of a long build are the ones anybody reads.
"""

EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
RUN_TOOLS = frozenset({"Bash", "PowerShell", "BashOutput"})
READ_TOOLS = frozenset({"Read", "NotebookRead"})
SEARCH_TOOLS = frozenset({"Glob", "Grep", "WebFetch", "WebSearch"})

KIND_PROMPT = "prompt"
KIND_NARRATION = "narration"
KIND_TOOLS = "tools"

SOURCE_JSONL = "session-jsonl"
SOURCE_NONE = "none"


# ----- what a reader ends up with ---------------------------------------------


@dataclass
class TranscriptCall:
    """One tool call, and what came back from it."""

    name: str
    title: str = ""
    detail: str = ""
    output: str = ""
    failed: bool = False
    added: int = 0
    removed: int = 0


@dataclass
class TranscriptEntry:
    """One thing the agent did, as the panel renders it.

    ``narration`` and ``prompt`` carry ``text``. ``tools`` carries a run of consecutive
    calls plus the one-line summary that stands in for them until somebody opens it.
    """

    kind: str
    text: str = ""
    summary: str = ""
    added: int = 0
    removed: int = 0
    failed: int = 0
    calls: List[TranscriptCall] = field(default_factory=list)


@dataclass
class StructuredTranscript:
    """The result of trying to read one. ``source`` says whether it worked."""

    source: str = SOURCE_NONE
    note: str = ""
    entries: List[TranscriptEntry] = field(default_factory=list)
    total_entries: int = 0
    truncated: bool = False
    path: Optional[Path] = None
    updated_at: Optional[float] = None


# ----- finding the file -------------------------------------------------------


def project_slug(path: Path) -> str:
    """Claude Code's directory name for a working directory.

    Every character that is not a letter, a digit or a hyphen becomes a hyphen, so
    ``C:\\projects\\agentjobs`` is ``C--projects-agentjobs``. Derived by inspection of
    an existing store rather than from a published contract, which is why nothing here
    fails when it does not match -- :func:`find_session_transcript` widens its search
    instead.
    """
    text = str(path)
    return "".join(char if (char.isalnum() or char == "-") else "-" for char in text)


def claude_projects_dir(home: Optional[Path] = None) -> Path:
    """Where Claude Code keeps per-directory transcript stores."""
    return (home or Path.home()) / CLAUDE_HOME_DIRNAME / PROJECTS_DIRNAME


def find_session_transcript(
    session_id: str,
    cwd: Path,
    *,
    home: Optional[Path] = None,
) -> Optional[Path]:
    """The JSONL for one session, or ``None``.

    A run's recorded ``session_id`` is the first segment of the transcript's UUID
    filename -- ``553f321b`` for ``553f321b-aefe-....jsonl`` -- so the match is on
    prefix. Three attempts, narrowest first:

    1. the store for this project's own root, which is where a dispatched session runs;
    2. stores whose name *begins* with that slug, which is what a session that relocated
       into a worktree gets;
    3. every store, because a session id is sixteen bits shy of unique and finding the
       right file matters more than finding it quickly.

    The newest match wins when there is more than one. A driver that keeps no such file
    -- Codex, or any future one -- simply never matches, and the caller degrades to the
    raw capture without needing to know which drivers those are.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return None
    root = claude_projects_dir(home)
    if not root.is_dir():
        return None
    slug = project_slug(cwd)
    for pattern in (f"{slug}/{session_id}*.jsonl", f"{slug}*/{session_id}*.jsonl"):
        found = _newest(root.glob(pattern))
        if found is not None:
            return found
    return _newest(root.glob(f"*/{session_id}*.jsonl"))


def _newest(candidates: Iterable[Path]) -> Optional[Path]:
    """The most recently modified readable file among some candidates."""
    best: Optional[Path] = None
    best_mtime = -1.0
    for candidate in candidates:
        try:
            mtime = candidate.stat().st_mtime
        except OSError:  # pragma: no cover - vanished between glob and stat
            continue
        if mtime > best_mtime:
            best, best_mtime = candidate, mtime
    return best


# ----- reading it -------------------------------------------------------------


def read_structured_transcript(
    path: Optional[Path],
    limit: int = DEFAULT_ENTRY_LIMIT,
    *,
    window: int = TRANSCRIPT_BYTE_WINDOW,
) -> StructuredTranscript:
    """Parse a session transcript into entries a panel can render.

    Returns an empty :class:`StructuredTranscript` with a ``note`` -- never raises -- for
    every way this can fail to produce anything: no path, no file, an unreadable file,
    or a file whose lines parse but hold nothing this understands. The caller shows the
    note and falls back to the raw capture.
    """
    limit = max(1, min(int(limit or DEFAULT_ENTRY_LIMIT), MAX_ENTRY_LIMIT))
    if path is None:
        return StructuredTranscript(
            note=(
                "No structured transcript for this run yet. A session's is written under "
                "the runner's own directory once it starts, and a run driven by something "
                "that keeps no such file never has one."
            )
        )
    try:
        raw, clipped = _tail_text(path, window)
        mtime: Optional[float] = path.stat().st_mtime
    except OSError as exc:
        return StructuredTranscript(
            note=f"This run's structured transcript could not be read just now ({exc})."
        )

    entries, unparseable = _parse(raw)
    if not entries:
        detail = (
            f" {unparseable} of its lines could not be parsed."
            if unparseable
            else " It holds no assistant turns yet."
        )
        return StructuredTranscript(
            note=f"Nothing readable in this run's structured transcript yet.{detail}",
            path=path,
            updated_at=mtime,
        )

    total = len(entries)
    kept = entries[-limit:]
    return StructuredTranscript(
        source=SOURCE_JSONL,
        entries=kept,
        total_entries=total,
        truncated=clipped or total > len(kept),
        path=path,
        updated_at=mtime,
    )


def _tail_text(path: Path, window: int) -> Tuple[str, bool]:
    """The end of a file as text, plus whether anything was left off the front."""
    size = path.stat().st_size
    start = max(0, size - window)
    with path.open("rb") as handle:
        handle.seek(start)
        blob = handle.read()
    text = blob.decode("utf-8", errors="replace")
    if start > 0:
        # The window almost certainly opened mid-line; a half object is not a record.
        _, _, text = text.partition("\n")
        return text, True
    return text, False


def _parse(raw: str) -> Tuple[List[TranscriptEntry], int]:
    """Walk the events in order, building entries. Returns them and a failure count.

    Consecutive tool calls collapse into one ``tools`` entry, and a paragraph of
    narration closes it -- which is what makes the panel a sequence of steps rather than
    a dump. Results arrive in later events, so calls are indexed by id and filled in as
    their results turn up.
    """
    entries: List[TranscriptEntry] = []
    by_id: Dict[str, TranscriptCall] = {}
    unparseable = 0
    group: Optional[TranscriptEntry] = None

    def close_group() -> None:
        nonlocal group
        if group is not None:
            group.summary = summarize(group.calls)
            group = None

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            unparseable += 1
            continue
        if not isinstance(event, dict):
            unparseable += 1
            continue
        # A subagent's own turns are a separate conversation. Interleaving them with the
        # main one produces a transcript in which nothing follows from what precedes it;
        # the Agent call that started it is in the main chain and stands for it.
        if event.get("isSidechain"):
            continue

        kind = event.get("type")
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None

        if kind == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text = str(block.get("text") or "").strip()
                    if text:
                        close_group()
                        entries.append(TranscriptEntry(kind=KIND_NARRATION, text=text))
                elif block_type == "tool_use":
                    call = _call(block)
                    if group is None:
                        group = TranscriptEntry(kind=KIND_TOOLS)
                        entries.append(group)
                    group.calls.append(call)
                    call_id = block.get("id")
                    if isinstance(call_id, str):
                        by_id[call_id] = call
                # Anything else -- thinking, and whatever is added next -- is skipped
                # rather than guessed at.
            continue

        if kind == "user":
            if isinstance(content, str):
                text = content.strip()
                if text and not event.get("isMeta"):
                    close_group()
                    entries.append(
                        TranscriptEntry(kind=KIND_PROMPT, text=_clip(text, PROMPT_CHARS))
                    )
            elif isinstance(content, list):
                _apply_results(content, event.get("toolUseResult"), by_id)
            continue

    close_group()
    for entry in entries:
        if entry.kind == KIND_TOOLS:
            entry.added = sum(call.added for call in entry.calls)
            entry.removed = sum(call.removed for call in entry.calls)
            entry.failed = sum(1 for call in entry.calls if call.failed)
    return entries, unparseable


def _apply_results(
    content: Sequence[Any], tool_use_result: Any, by_id: Dict[str, TranscriptCall]
) -> None:
    """Attach a user event's tool results to the calls they answer."""
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        call = by_id.get(block.get("tool_use_id") or "")
        if call is None:
            # Its call was before the window we read. Nothing to attach it to.
            continue
        call.failed = bool(block.get("is_error"))
        call.output = _clip(_flatten(block.get("content")), CALL_OUTPUT_CHARS, tail=True)
        call.added, call.removed = _diff_counts(tool_use_result)


def _flatten(content: Any) -> str:
    """A tool result's content as text, whatever shape it arrived in.

    Escape sequences come off here, and this is not the mistake the module docstring
    warns about. A *repaint* encodes its spacing in cursor movements, so removing the
    sequences destroys the text; a **command's own stdout** encodes nothing in them but
    colour, which the panel does not render anyway. Observed on this feature's own
    dispatched run, where a pytest result reached the panel as
    ``\\x1b[32m.\\x1b[0m\\x1b[32m.\\x1b[0m`` -- forty green dots and no readable count.
    """
    if isinstance(content, str):
        return strip_ansi(content).strip()
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return strip_ansi("\n".join(parts)).strip()
    if content is None:
        return ""
    return strip_ansi(str(content)).strip()


def _diff_counts(result: Any) -> Tuple[int, int]:
    """How many lines an edit added and removed, when the record says.

    ``structuredPatch`` is what Claude Code records for an edit; a file created outright
    has no patch, so its whole content counts as additions. Absent or unrecognised, the
    answer is ``(0, 0)`` and the panel shows no counts, which is the honest rendering of
    "this was not an edit" and of "this format changed" alike.
    """
    if not isinstance(result, dict):
        return 0, 0
    patch = result.get("structuredPatch")
    if isinstance(patch, list) and patch:
        added = removed = 0
        for hunk in patch:
            if not isinstance(hunk, dict):
                continue
            for line in hunk.get("lines") or []:
                if not isinstance(line, str):
                    continue
                if line.startswith("+"):
                    added += 1
                elif line.startswith("-"):
                    removed += 1
        return added, removed
    if result.get("type") == "create":
        body = result.get("content")
        if isinstance(body, str) and body:
            return len(body.splitlines()), 0
    return 0, 0


# ----- rendering one call and a run of them -----------------------------------


def _call(block: Dict[str, Any]) -> TranscriptCall:
    """One ``tool_use`` block as a titled, detailed call."""
    name = str(block.get("name") or "tool")
    raw_input = block.get("input")
    payload: Dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
    return TranscriptCall(
        name=name,
        title=_title(name, payload),
        detail=_clip(_detail(name, payload), CALL_DETAIL_CHARS, tail=True),
    )


def _title(name: str, payload: Dict[str, Any]) -> str:
    """The one line that stands for a call when its detail is closed."""
    if name in RUN_TOOLS:
        described = _text(payload.get("description"))
        if described:
            return described
        return _first_line(_text(payload.get("command")))
    if name in EDIT_TOOLS or name in READ_TOOLS:
        return _basename(_text(payload.get("file_path") or payload.get("notebook_path")))
    if name in SEARCH_TOOLS:
        return _text(payload.get("pattern") or payload.get("query") or payload.get("url"))
    described = _text(payload.get("description") or payload.get("prompt"))
    return _first_line(described)


def _detail(name: str, payload: Dict[str, Any]) -> str:
    """What the call actually was, for the disclosure."""
    if name in RUN_TOOLS:
        return _text(payload.get("command"))
    if name in EDIT_TOOLS or name in READ_TOOLS:
        return _text(payload.get("file_path") or payload.get("notebook_path"))
    if not payload:
        return ""
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - a non-serialisable input
        return str(payload)


def summarize(calls: Sequence[TranscriptCall]) -> str:
    """A run of tool calls in one line: "Edited App.tsx, ran 4 commands".

    Grouped by what the reader is being told -- files changed, commands run, things
    looked at -- rather than by tool name, because "ran 4 commands" is the fact and
    "Bash x4" is an implementation detail of how it was run.
    """
    if not calls:
        return ""
    edited = _unique(call.title for call in calls if call.name in EDIT_TOOLS)
    ran = [call for call in calls if call.name in RUN_TOOLS]
    read = _unique(call.title for call in calls if call.name in READ_TOOLS)
    searched = [call for call in calls if call.name in SEARCH_TOOLS]
    other = [
        call
        for call in calls
        if call.name not in EDIT_TOOLS
        and call.name not in RUN_TOOLS
        and call.name not in READ_TOOLS
        and call.name not in SEARCH_TOOLS
    ]

    parts: List[str] = []
    if edited:
        parts.append(f"edited {_names(edited)}")
    if ran:
        parts.append(f"ran {_count(len(ran), 'command')}")
    if read:
        parts.append(f"read {_names(read, noun='file')}")
    if searched:
        parts.append(f"ran {_count(len(searched), 'search', plural='searches')}")
    if other:
        names = _unique(call.name for call in other)
        parts.append(
            f"called {names[0]}" if len(names) == 1 else f"called {_count(len(other), 'tool')}"
        )
    if not parts:  # pragma: no cover - every call falls into one of the buckets above
        parts.append(_count(len(calls), "call"))
    line = ", ".join(parts)
    return line[0].upper() + line[1:]


def _names(titles: Sequence[str], noun: str = "file") -> str:
    """ "App.tsx", "App.tsx and two more files" -- names while naming still helps."""
    named = [title for title in titles if title]
    if not named:
        return _count(len(titles), noun)
    if len(named) == 1:
        return named[0]
    if len(named) == 2:
        return f"{named[0]} and {named[1]}"
    return f"{named[0]} and {len(named) - 1} more {noun}s"


def _count(number: int, noun: str, plural: Optional[str] = None) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {plural or noun + 's'}"


def _unique(values: Iterable[str]) -> List[str]:
    """Distinct values, first appearance first. Editing one file twice is one file."""
    seen: Dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return list(seen)


# ----- small text helpers -----------------------------------------------------


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first_line(text: str) -> str:
    return text.splitlines()[0].strip() if text else ""


def _basename(path_text: str) -> str:
    """The file's own name. A reader recognises ``DispatchOutput.tsx``, not its path."""
    if not path_text:
        return ""
    return path_text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _clip(text: str, limit: int, *, tail: bool = False) -> str:
    """Bound a string, saying so where it was cut."""
    if len(text) <= limit:
        return text
    if tail:
        return "…" + text[-limit:]
    return text[:limit] + "…"
