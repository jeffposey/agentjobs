"""One-shot converter from task schema v1 to v2.

Implements the mapping in ``docs/schema-design.md`` sections 3 and 8. Run it once per
corpus; v2 files are stamped ``schema: 2`` and the converter refuses to touch them
again.

The governing rule is **no information disappears silently**. Every v1 field is either
mapped somewhere, or named in :data:`INTENTIONALLY_DROPPED` with a reason. A field that
is neither raises :class:`UnmappedFieldError` and fails the run. That is deliberate: a
migrator that quietly skips a field it does not recognise is the most expensive kind of
bug, because the evidence is gone by the time anyone notices.

Where the mapping cannot be derived with confidence -- a ball_prompt with no source
text, an owner with no plausible actor -- the converter records a
:class:`ReviewNote` rather than inventing something. Those notes are surfaced in the
report and are the human's queue, not the migrator's problem to guess at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models_v2 import SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Field accounting
# ---------------------------------------------------------------------------

MAPPED_FIELDS = {
    "id",
    "title",
    "created",
    "updated",
    "status",
    "priority",
    "category",
    "assigned_to",
    "estimated_effort",
    "human_summary",
    "description",
    "phases",
    "success_criteria",
    "prompts",
    "status_updates",
    "comments",
    "deliverables",
    "dependencies",
    "external_links",
    "tags",
    "branches",
}
"""v1 fields this converter reads and carries into v2."""

INTENTIONALLY_DROPPED = {
    # Empty in every file of the corpus (verified 2026-07-29 and again 2026-08-10).
    # D1 deletes the Issue model outright; an issue is a log entry or its own task.
    "issues": "empty corpus-wide; the Issue model is deleted in v2 (D1)",
}
"""v1 fields deliberately not carried over, each with the reason."""


class MigrationError(Exception):
    """Base for problems that stop a conversion."""


class UnmappedFieldError(MigrationError):
    """A v1 field is neither mapped nor explicitly dropped.

    Raised rather than warned about, because the whole point of the accounting is that
    an unrecognised field cannot slip through unnoticed.
    """


class AlreadyV2Error(MigrationError):
    """The file already carries a v2 schema stamp."""


@dataclass
class ReviewNote:
    """Something a human should look at after the conversion."""

    task_id: str
    field: str
    detail: str

    def __str__(self) -> str:
        return f"{self.task_id}: {self.field} -- {self.detail}"


@dataclass
class Conversion:
    """The result of converting one task."""

    task_id: str
    data: Dict[str, Any]
    notes: List[ReviewNote] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

_ACTOR_SPLIT = re.compile(r"\s*(?:\+|,|/|\band\b)\s*", re.IGNORECASE)
_NON_ACTOR = {"tbd", "unassigned", "none", "n/a", ""}


def normalise_actors(assigned_to: Optional[str]) -> List[str]:
    """Turn v1's free-text ``assigned_to`` into a list of actor ids.

    The corpus contains 'Codex', 'claude', 'TBD' and 'Claude + Codex'. Two of those are
    not actor ids and one is two of them, so this is not a lowercase() call.
    """
    if not assigned_to:
        return []
    parts = [part.strip().lower() for part in _ACTOR_SPLIT.split(assigned_to)]
    return [part for part in parts if part and part not in _NON_ACTOR]


def _text(value: Any) -> str:
    return (value or "").strip() if isinstance(value, str) else ""


def _normalise_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _as_datetime(value: Any) -> datetime:
    """Coerce a v1 timestamp to an aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _first_sentence(text: str, limit: int = 240) -> str:
    """A short summary derived from prose, for tasks with no human_summary."""
    flat = re.sub(r"\s+", " ", re.sub(r"[#*`>]", "", text)).strip()
    match = re.search(r"^(.{40,}?[.!?])\s", flat)
    candidate = match.group(1) if match else flat
    return candidate if len(candidate) <= limit else candidate[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# State mapping (design doc section 3)
# ---------------------------------------------------------------------------

_CHECKLIST_STATUS = {
    # in_progress is not a persistent fact about a checklist item, so it collapses to
    # pending (design doc section 8).
    "pending": "pending",
    "in_progress": "pending",
    "completed": "met",
    "failed": "failed",
}
_DELIVERABLE_STATUS = {"pending": "pending", "in_progress": "pending", "completed": "done"}
_DEPENDENCY_TYPE = {"depends_on": "needs", "blocks": "blocks", "related": "related"}

_DECISION_WORDS = re.compile(r"\b(decide|decision|choose|which|approve|approval|sign.?off)\b", re.I)
_SUPERSEDED_WORDS = re.compile(r"\b(in favou?r of|supersede[ds]?|replaced by|split into)\b", re.I)
_DUPLICATE_WORDS = re.compile(r"\bduplicate\b", re.I)


def _is_claimed(v1: Dict[str, Any]) -> bool:
    """Whether a v1 task looks like work someone actually picked up.

    An active branch is the strongest evidence, then any status update at all. Used for
    the `blocked` split: blocked-and-claimed is a wall hit mid-work; blocked-and-
    unclaimed is just a task whose dependencies are not done, which v2 expresses as
    `ready` with unmet needs rather than as state (design doc section 3).
    """
    if any((b or {}).get("status") == "active" for b in v1.get("branches") or []):
        return True
    return bool(v1.get("status_updates"))


def _archived_outcome(v1: Dict[str, Any]) -> str:
    """Why an archived task ended, read from its last status update."""
    updates = v1.get("status_updates") or []
    text = " ".join(_text(u.get("summary")) + " " + _text(u.get("details")) for u in updates[-2:])
    if _SUPERSEDED_WORDS.search(text):
        return "superseded"
    if _DUPLICATE_WORDS.search(text):
        return "duplicate"
    return "cancelled"


def map_state(v1: Dict[str, Any], notes: List[ReviewNote]) -> Dict[str, Any]:
    """Map v1 `status` onto the v2 state axes."""
    task_id = v1.get("id", "?")
    status = v1.get("status") or "draft"
    updates = v1.get("status_updates") or []
    last = updates[-1] if updates else None
    last_text = f"{_text(last.get('summary'))} {_text(last.get('details'))}" if last else ""

    def ask(default: str) -> str:
        """The ball_prompt, taken from the last update or flagged as absent."""
        summary = _text(last.get("summary")) if last else ""
        details = _text(last.get("details")) if last else ""
        if summary:
            return f"{summary}." if not summary.endswith((".", "!", "?")) else summary
        if details:
            return _first_sentence(details)
        notes.append(ReviewNote(task_id, "ball_prompt", "no status update to derive an ask from"))
        return default

    if status == "draft":
        return {
            "lifecycle": "draft",
            "ball": "human",
            "ball_reason": "spec",
            "ball_prompt": ask("NEEDS REVIEW: finish specifying this task."),
        }

    if status == "ready":
        # agent/available is the one case where ball_prompt may be omitted: the spec is
        # itself the ask.
        return {"lifecycle": "ready", "ball": "agent", "ball_reason": "available"}

    if status == "in_progress":
        return {
            "lifecycle": "active",
            "ball": "agent",
            "ball_reason": "work",
            "ball_prompt": ask("Continue the work described in the spec."),
        }

    if status == "blocked":
        if _is_claimed(v1):
            return {
                "lifecycle": "active",
                "ball": "external",
                "ball_reason": "dependency",
                "ball_prompt": ask("NEEDS REVIEW: state what this is blocked on."),
            }
        if not v1.get("dependencies"):
            notes.append(
                ReviewNote(
                    task_id,
                    "status",
                    "was 'blocked' but unclaimed with no dependencies, so the blocker "
                    "was not recorded anywhere; mapped to ready and the original "
                    "status preserved in the log",
                )
            )
        return {"lifecycle": "ready", "ball": "agent", "ball_reason": "available"}

    if status in ("waiting_for_human", "under_review"):
        if status == "under_review":
            reason = "review"
        else:
            reason = "decision" if _DECISION_WORDS.search(last_text) else "review"
        return {
            "lifecycle": "active",
            "ball": "human",
            "ball_reason": reason,
            "ball_prompt": ask("NEEDS REVIEW: state what this task is waiting on."),
        }

    if status == "completed":
        return {"lifecycle": "closed", "outcome": "completed"}

    if status == "archived":
        return {"lifecycle": "closed", "outcome": _archived_outcome(v1), "archived": True}

    raise MigrationError(f"{task_id}: unknown v1 status {status!r}")


# ---------------------------------------------------------------------------
# The unified log (design doc section 4)
# ---------------------------------------------------------------------------


def build_log(v1: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Merge status_updates, comments and prompt followups into one ordered log."""
    entries: List[Tuple[datetime, Dict[str, Any]]] = []

    for update in v1.get("status_updates") or []:
        body = _text(update.get("summary"))
        details = _text(update.get("details"))
        if details:
            body = f"{body}\n\n{details}" if body else details
        entries.append(
            (
                _as_datetime(update.get("timestamp")),
                {
                    "actor": _text(update.get("author")) or "unknown",
                    # transition, because a v1 status update always recorded a status.
                    # The v1 value is kept in data: the v2 axes for a historical entry
                    # cannot be reconstructed, and inventing them would be a lie.
                    "type": "transition",
                    "data": {"v1_status": update.get("status")},
                    "body": body or "(no summary recorded)",
                },
            )
        )

    for comment in v1.get("comments") or []:
        kind = _text(comment.get("kind")) or "comment"
        entries.append(
            (
                _as_datetime(comment.get("created")),
                {
                    "actor": _text(comment.get("author")) or "unknown",
                    "type": "question" if kind == "question" else "note",
                    "body": _text(comment.get("content")) or "(empty comment)",
                },
            )
        )

    for followup in (v1.get("prompts") or {}).get("followups") or []:
        body = _text(followup.get("content"))
        if not body and followup.get("prompt_file"):
            body = f"See prompt file: {followup['prompt_file']}"
        context = _text(followup.get("context"))
        if context:
            body = f"{body}\n\nContext: {context}" if body else f"Context: {context}"
        entries.append(
            (
                _as_datetime(followup.get("timestamp")),
                {
                    "actor": _text(followup.get("author")) or "unknown",
                    "type": "instruction",
                    "body": body or "(empty followup prompt)",
                },
            )
        )

    entries.sort(key=lambda pair: pair[0])
    log: List[Dict[str, Any]] = []
    for index, (timestamp, entry) in enumerate(entries, start=1):
        log.append({"id": index, "ts": timestamp, **entry})
    return log


# ---------------------------------------------------------------------------
# The spec block
# ---------------------------------------------------------------------------


def _phase_appendix(phases: List[Dict[str, Any]]) -> str:
    """Fold v1 phases into a markdown appendix (design doc section 6, issue 3)."""
    lines = [
        "",
        "---",
        "",
        "## Phases (migrated from schema v1)",
        "",
        "`phases[]` was deleted in v2; sub-tasks via `parent` are the one way to",
        "subdivide. This is the historical record, preserved verbatim.",
        "",
    ]
    for phase in phases:
        status = _text(phase.get("status")) or "draft"
        lines.append(
            f"- **{_text(phase.get('title')) or phase.get('id')}** "
            f"(`{phase.get('id')}`, {status})"
        )
        if _text(phase.get("notes")):
            lines.append(f"  - {_text(phase['notes'])}")
        if phase.get("completed_at"):
            lines.append(f"  - completed {phase['completed_at']}")
    return "\n".join(lines)


def build_spec(v1: Dict[str, Any], notes: List[ReviewNote]) -> Dict[str, Any]:
    """Build the v2 spec block from v1's description, summary, starter and phases."""
    task_id = v1.get("id", "?")
    description = _text(v1.get("description"))
    starter = _text((v1.get("prompts") or {}).get("starter"))

    summary = _text(v1.get("human_summary"))
    if not summary:
        summary = _first_sentence(description) or _text(v1.get("title"))
        notes.append(
            ReviewNote(task_id, "spec.summary", "no human_summary; derived from the description")
        )

    body = description

    # The design expected starters to restate the description and be droppable. Over
    # this corpus only one actually is, so the rest are preserved -- they are the agent
    # briefings, and losing 37 of them to a tidy-up would be the worst kind of
    # migration bug.
    if starter and _normalise_ws(starter) not in _normalise_ws(description):
        body = (
            f"{body}\n\n---\n\n## Starter prompt (migrated from schema v1)\n\n{starter}"
            if body
            else starter
        )

    phases = v1.get("phases") or []
    if phases:
        body = f"{body}{_phase_appendix(phases)}"

    spec: Dict[str, Any] = {"summary": summary}
    if body:
        spec["description"] = body
    return spec


# ---------------------------------------------------------------------------
# The converter
# ---------------------------------------------------------------------------


def convert_task(v1: Dict[str, Any], *, source: str = "task") -> Conversion:
    """Convert one v1 task mapping into a v2 mapping.

    Raises AlreadyV2Error if the input is already stamped, and UnmappedFieldError if it
    contains a field this converter does not know about.
    """
    if v1.get("schema") is not None:
        raise AlreadyV2Error(
            f"{source} already declares schema {v1['schema']!r}; the migrator converts "
            "v1 files only and will not run twice"
        )

    unknown = set(v1) - MAPPED_FIELDS - set(INTENTIONALLY_DROPPED)
    if unknown:
        raise UnmappedFieldError(
            f"{source} contains field(s) this migrator does not handle: "
            f"{', '.join(sorted(unknown))}. Refusing to convert rather than drop them "
            "silently -- add them to MAPPED_FIELDS or INTENTIONALLY_DROPPED."
        )

    task_id = v1.get("id")
    if not task_id:
        raise MigrationError(f"{source} has no id")

    notes: List[ReviewNote] = []
    state = map_state(v1, notes)
    actors = normalise_actors(v1.get("assigned_to"))

    if _text(v1.get("assigned_to")) and not actors:
        notes.append(
            ReviewNote(task_id, "assigned_to", f"{v1['assigned_to']!r} is not an actor id; dropped")
        )

    assignment: Dict[str, Any] = {}
    if state["lifecycle"] == "active":
        # Rule 5: an active task must have an owner. assigned_to is the best evidence,
        # then whoever last wrote a status update.
        owner = actors[0] if actors else None
        if owner is None:
            updates = v1.get("status_updates") or []
            owner = _text(updates[-1].get("author")).lower() if updates else None
        if owner is None:
            owner = "unknown"
            notes.append(
                ReviewNote(
                    task_id,
                    "assignment.owner",
                    "active task with no assignee and no updates; set to 'unknown'",
                )
            )
        assignment["owner"] = owner
        if len(actors) > 1:
            assignment["eligible"] = actors
    elif actors:
        assignment["eligible"] = actors

    v2: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "id": task_id,
        "title": v1.get("title"),
        "created": _as_datetime(v1.get("created")),
        "updated": _as_datetime(v1.get("updated")),
        **state,
        "priority": v1.get("priority") or "medium",
        "category": v1.get("category") or "general",
    }
    if v1.get("tags"):
        v2["tags"] = list(v1["tags"])
    if _text(v1.get("estimated_effort")):
        v2["effort"] = _text(v1["estimated_effort"])
    if assignment:
        v2["assignment"] = assignment

    v2["spec"] = build_spec(v1, notes)

    acceptance = []
    for criterion in v1.get("success_criteria") or []:
        acceptance.append(
            {
                "id": criterion.get("id"),
                "text": _text(criterion.get("description")),
                "status": _CHECKLIST_STATUS.get(_text(criterion.get("status")), "pending"),
            }
        )
    if acceptance:
        v2["acceptance"] = acceptance

    deliverables = []
    for item in v1.get("deliverables") or []:
        entry: Dict[str, Any] = {
            "path": item.get("path"),
            "status": _DELIVERABLE_STATUS.get(_text(item.get("status")), "pending"),
        }
        if _text(item.get("description")):
            entry["note"] = _text(item["description"])
        deliverables.append(entry)
    if deliverables:
        v2["deliverables"] = deliverables

    dependencies = []
    for dep in v1.get("dependencies") or []:
        entry = {
            "task": dep.get("task_id"),
            "type": _DEPENDENCY_TYPE.get(_text(dep.get("type")), "needs"),
        }
        if _text(dep.get("note")):
            entry["note"] = _text(dep["note"])
        # v1's Dependency.status had no validator, no vocabulary and no purpose; D1
        # deletes it. Preserved in the note when someone actually filled it in.
        if _text(dep.get("status")):
            entry["note"] = f"{entry.get('note', '')} (v1 status: {dep['status']})".strip()
        dependencies.append(entry)
    if dependencies:
        v2["dependencies"] = dependencies

    links = []
    for link in v1.get("external_links") or []:
        entry = {
            "url": link.get("url"),
            "rel": "pr" if "/pull/" in str(link.get("url")) else "other",
        }
        if _text(link.get("title")):
            entry["title"] = _text(link["title"])
        links.append(entry)
    if links:
        v2["links"] = links

    branches = []
    for branch in v1.get("branches") or []:
        entry = {"name": branch.get("name"), "status": _text(branch.get("status")) or "active"}
        if branch.get("merged_at"):
            entry["merged_at"] = branch["merged_at"]
        branches.append(entry)
    if branches:
        v2["branches"] = branches

    log = build_log(v1)
    # A closing note recording what the migrator did, so the original v1 state is
    # recoverable from the record itself rather than only from git history.
    log.append(
        {
            "id": len(log) + 1,
            "ts": _as_datetime(v1.get("updated")),
            "actor": "system",
            "type": "note",
            "data": {"v1_status": v1.get("status"), "migrated_to_schema": SCHEMA_VERSION},
            "body": _migration_note(v1, state, notes),
        }
    )
    v2["log"] = log

    return Conversion(task_id=task_id, data=v2, notes=notes)


def _migration_note(v1: Dict[str, Any], state: Dict[str, Any], notes: List[ReviewNote]) -> str:
    """The audit entry appended to every migrated task."""
    axes = " / ".join(
        f"{key}: {state[key]}"
        for key in ("lifecycle", "ball", "ball_reason", "outcome")
        if state.get(key)
    )
    lines = [
        f"Migrated from schema v1 to v{SCHEMA_VERSION} by `agentjobs migrate-schema`.",
        "",
        f"- v1 `status: {v1.get('status')}` became {axes}.",
    ]
    if v1.get("phases"):
        lines.append(f"- {len(v1['phases'])} phase(s) folded into spec.description as an appendix.")
    if (v1.get("prompts") or {}).get("starter"):
        lines.append("- prompts.starter preserved in spec.description.")
    if (v1.get("prompts") or {}).get("followups"):
        lines.append(
            f"- {len(v1['prompts']['followups'])} followup prompt(s) became instruction entries."
        )
    if notes:
        lines.append("")
        lines.append("Needs human review:")
        lines.extend(f"- {note.field}: {note.detail}" for note in notes)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Loss verification
#
# The field accounting above proves no field was *ignored*. This proves no content was
# *lost*: every piece of v1 prose has to be findable in the v2 output, and every v1
# collection has to be accounted for by count. Field accounting catches a forgotten
# key; this catches a mapping that reads a field and then quietly writes nothing.
# ---------------------------------------------------------------------------


def _all_text(value: Any) -> str:
    """Flatten every string in a nested structure into one searchable blob."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_all_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_all_text(item) for item in value)
    return ""


def verify_no_loss(v1: Dict[str, Any], v2: Dict[str, Any]) -> List[str]:
    """Return a list of things present in v1 that are missing from v2.

    An empty list means nothing was dropped. Text is compared with whitespace
    normalised, because the converter reflows prose into appendices.
    """
    losses: List[str] = []
    haystack = _normalise_ws(_all_text(v2))

    def must_contain(label: str, text: Any) -> None:
        needle = _normalise_ws(_text(text))
        if needle and needle not in haystack:
            preview = needle[:70]
            losses.append(f"{label} text not found in output: {preview!r}")

    must_contain("title", v1.get("title"))
    must_contain("description", v1.get("description"))
    must_contain("human_summary", v1.get("human_summary"))
    must_contain("prompts.starter", (v1.get("prompts") or {}).get("starter"))

    for phase in v1.get("phases") or []:
        must_contain(f"phase {phase.get('id')} title", phase.get("title"))
        must_contain(f"phase {phase.get('id')} notes", phase.get("notes"))

    for update in v1.get("status_updates") or []:
        must_contain("status_update summary", update.get("summary"))
        must_contain("status_update details", update.get("details"))

    for comment in v1.get("comments") or []:
        must_contain("comment content", comment.get("content"))

    for followup in (v1.get("prompts") or {}).get("followups") or []:
        must_contain("followup content", followup.get("content"))

    for criterion in v1.get("success_criteria") or []:
        must_contain("success_criterion", criterion.get("description"))

    for item in v1.get("deliverables") or []:
        must_contain("deliverable path", item.get("path"))

    for dep in v1.get("dependencies") or []:
        must_contain("dependency task_id", dep.get("task_id"))

    for link in v1.get("external_links") or []:
        must_contain("external_link url", link.get("url"))

    for branch in v1.get("branches") or []:
        must_contain("branch name", branch.get("name"))

    # Counts, so a collection cannot be silently truncated to its first element.
    pairs = [
        ("success_criteria", v1.get("success_criteria"), v2.get("acceptance")),
        ("deliverables", v1.get("deliverables"), v2.get("deliverables")),
        ("dependencies", v1.get("dependencies"), v2.get("dependencies")),
        ("external_links", v1.get("external_links"), v2.get("links")),
        ("branches", v1.get("branches"), v2.get("branches")),
    ]
    for label, before, after in pairs:
        if len(before or []) != len(after or []):
            losses.append(f"{label}: {len(before or [])} in v1 but {len(after or [])} in v2")

    expected_log = (
        len(v1.get("status_updates") or [])
        + len(v1.get("comments") or [])
        + len((v1.get("prompts") or {}).get("followups") or [])
        + 1  # the migration audit note
    )
    if len(v2.get("log") or []) != expected_log:
        losses.append(
            f"log: expected {expected_log} entries "
            f"(updates + comments + followups + audit note) but got {len(v2.get('log') or [])}"
        )

    return losses


# ---------------------------------------------------------------------------
# Corpus migration
# ---------------------------------------------------------------------------


@dataclass
class FileResult:
    """What happened to one file."""

    path: Path
    task_id: str = ""
    converted: bool = False
    skipped_reason: str = ""
    losses: List[str] = field(default_factory=list)
    notes: List[ReviewNote] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.converted and not self.losses and not self.error


@dataclass
class MigrationReport:
    """The outcome of a corpus migration, dry run or otherwise."""

    results: List[FileResult] = field(default_factory=list)
    written: bool = False

    @property
    def converted(self) -> List[FileResult]:
        return [r for r in self.results if r.converted]

    @property
    def failures(self) -> List[FileResult]:
        return [r for r in self.results if r.error or r.losses]

    @property
    def skipped(self) -> List[FileResult]:
        return [r for r in self.results if r.skipped_reason]

    @property
    def all_notes(self) -> List[ReviewNote]:
        return [note for result in self.results for note in result.notes]

    def render(self) -> str:
        """A human-readable summary, printed by the CLI and kept as an artifact."""
        lines = [
            f"Files examined:  {len(self.results)}",
            f"Converted:       {len(self.converted)}",
            f"Skipped:         {len(self.skipped)}",
            f"Failed:          {len(self.failures)}",
            f"Review notes:    {len(self.all_notes)}",
            f"Written to disk: {'yes' if self.written else 'NO (dry run)'}",
        ]
        if self.failures:
            lines += ["", "FAILURES"]
            for result in self.failures:
                lines.append(f"  {result.path.name}: {result.error or ''}")
                lines += [f"    loss: {loss}" for loss in result.losses]
        if self.skipped:
            lines += ["", "SKIPPED"]
            lines += [f"  {r.path.name}: {r.skipped_reason}" for r in self.skipped]
        if self.all_notes:
            lines += ["", "NEEDS HUMAN REVIEW"]
            lines += [f"  {note}" for note in self.all_notes]
        return "\n".join(lines)


def migrate_file(path: Path) -> Tuple[FileResult, Optional[Dict[str, Any]]]:
    """Convert one file, verifying it before returning. Never writes."""
    import yaml

    result = FileResult(path=path)
    try:
        v1 = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # pragma: no cover - unreadable file
        result.error = f"could not read: {exc}"
        return result, None

    result.task_id = str(v1.get("id", ""))
    try:
        conversion = convert_task(v1, source=str(path))
    except AlreadyV2Error as exc:
        result.skipped_reason = str(exc)
        return result, None
    except MigrationError as exc:
        result.error = str(exc)
        return result, None

    result.notes = conversion.notes
    result.losses = verify_no_loss(v1, conversion.data)
    result.converted = True
    return result, conversion.data


def _assign_queue_positions(records: List[Dict[str, Any]]) -> None:
    """Give every open converted task a place in its band, in place.

    Uses :func:`agentjobs.queue.plan_queue_migration`, so a corpus converted from v1
    and a corpus migrated in place end up in the same order for the same reason:
    ``created`` ascending, then id. Closed tasks are left alone -- they are not in
    line (design doc section 3.2).
    """
    from .queue import QueueRecord, plan_queue_migration

    by_id = {str(record.get("id")): record for record in records}
    queue_records = [
        QueueRecord(
            task_id=str(record.get("id")),
            created=str(record.get("created")),
            priority=str(record.get("priority") or "medium"),
            is_open=record.get("lifecycle") != "closed",
            queue_position=None,
        )
        for record in records
    ]
    for assignment in plan_queue_migration(queue_records).assignments:
        record = by_id[assignment.task_id]
        # Rebuilt rather than appended so the key lands where the model puts it,
        # directly after `priority`. `safe_dump(sort_keys=False)` writes insertion
        # order, so appending would produce a file that loads perfectly and is not in
        # canonical form -- which the validator reports as a hand-shaped file.
        rebuilt = {}
        for key, value in list(record.items()):
            rebuilt[key] = value
            if key == "priority":
                rebuilt["queue_position"] = assignment.position
        rebuilt.setdefault("queue_position", assignment.position)
        record.clear()
        record.update(rebuilt)


def migrate_corpus(
    paths: List[Path],
    *,
    output_dir: Optional[Path] = None,
    write: bool = False,
) -> MigrationReport:
    """Convert every file, verify all of them, and only then write anything.

    Nothing is written unless ``write`` is true **and** every file converted cleanly.
    A corpus half in v1 and half in v2 is worse than one that is entirely v1, so a
    single failure aborts the write for all of them.
    """
    import yaml

    from .models_v2 import load_task

    report = MigrationReport()
    pending: List[Tuple[Path, Dict[str, Any]]] = []
    converted: List[Tuple[FileResult, Path, Dict[str, Any]]] = []

    for path in paths:
        result, data = migrate_file(path)
        report.results.append(result)
        if data is None:
            continue
        converted.append((result, path, data))

    # v2 requires an open task to have a queue_position (consistency rule 6), and no
    # v1 file carries one. A converter working one file at a time cannot supply it --
    # the number is a property of the band, which is a property of the whole corpus --
    # so it is assigned here, across everything this run converted, by the same
    # deterministic baseline the corpus migration uses.
    _assign_queue_positions([data for _, _, data in converted])

    for result, path, data in converted:
        # Round-trip through YAML and the model before accepting it: the data has to
        # survive serialisation, not merely exist in memory.
        try:
            serialised = yaml.safe_load(
                yaml.safe_dump(_yaml_safe(data), sort_keys=False, allow_unicode=False)
            )
            load_task(serialised, source=str(path))
        except Exception as exc:
            result.error = f"converted output does not load as v2: {exc}"
            continue
        destination = (output_dir / path.name) if output_dir else path
        pending.append((destination, serialised))

    if write and not report.failures:
        for destination, data in pending:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                yaml.safe_dump(data, sort_keys=False, allow_unicode=False), encoding="utf-8"
            )
        report.written = True

    return report


def _yaml_safe(value: Any) -> Any:
    """Convert datetimes to ISO strings so the output is plain YAML scalars."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, dict):
        return {key: _yaml_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_yaml_safe(item) for item in value]
    return value
