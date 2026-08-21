"""Shaping AgentJobs read documents into MCP tool payloads.

Two shapes, both fixed by section 4 of ``docs/mcp-integration-design.md``.

``TaskSummary`` is what a list returns: enough to decide which task to open, and no
more. It is not a truncated task -- an agent that acts on a summary alone is acting on
partial information, so the summary carries stable identity and state and stops there.

``TaskDocument`` is the whole schema-v2 record, returned by ``task_get``. That is the
resumption contract: a reader with no other context must be able to reconstruct what
is done and what remains from this one payload.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

#: Fields copied verbatim from the service's enriched read record into a summary.
_SUMMARY_PASSTHROUGH = (
    "id",
    "title",
    "lifecycle",
    "ball",
    "ball_reason",
    "ball_prompt",
    "outcome",
    "priority",
    "queue_position",
    "category",
    "parent",
    "updated",
    "display_status",
    "actionable",
    "unmet_needs",
    "open_children_count",
)


def task_summary(record: Mapping[str, Any], *, project_id: str) -> Dict[str, Any]:
    """Build a list-row summary from one enriched task record.

    ``project_id`` is stamped on every row. A task id is unique only within a project,
    so a summary that travelled without one could be fed back to a tool against the
    wrong project and address a different task entirely.
    """
    summary: Dict[str, Any] = {"project_id": project_id}
    for field in _SUMMARY_PASSTHROUGH:
        value = record.get(field)
        if field == "unmet_needs":
            summary[field] = list(value or [])
        elif field == "actionable":
            summary[field] = value if value is not None else False
        elif field == "open_children_count":
            # Absent means the surface did not compute it, which is not the same
            # answer as zero. Filling the gap with 0 is how a search reported "no
            # open children" for a parent with six of them -- the route returned
            # bare stored tasks and this line invented a plausible number for the
            # missing field. Null is the honest answer; the reader can tell.
            summary[field] = value
        else:
            summary[field] = value
    assignment = record.get("assignment") or {}
    summary["owner"] = assignment.get("owner") if isinstance(assignment, Mapping) else None
    return summary


def broken_task(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the BrokenTask entry for a file that exists but will not load."""
    return {
        "task_id": record.get("task_id") or "",
        "filename": record.get("filename") or "",
        "reason": record.get("reason") or "",
    }


def dependency_facts(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Extract the computed dependency state the service attached to a task."""
    return {
        "actionable": bool(record.get("actionable", False)),
        "unmet_needs": list(record.get("unmet_needs") or []),
        "needs_cycles": [list(cycle) for cycle in record.get("needs_cycles") or []],
        "unblocks_count": int(record.get("unblocks_count") or 0),
        # Null, not 0, when the surface did not compute it -- see task_summary.
        "open_children_count": (
            int(count) if (count := record.get("open_children_count")) is not None else None
        ),
    }


def task_document(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Strip the computed fields back off, leaving the stored schema-v2 task.

    The computed values are returned separately as dependency facts rather than mixed
    into the document, so a reader can tell what AgentJobs stored from what it worked
    out. Mixing them is how ``display_status`` ended up looking like a field a caller
    could set.
    """
    computed = set(dependency_facts(record)) | {"display_status"}
    return {key: value for key, value in record.items() if key not in computed}


def limited(rows: List[Dict[str, Any]], limit: int) -> tuple[List[Dict[str, Any]], bool]:
    """Apply a result limit, reporting whether anything was cut.

    ``truncated`` is returned rather than left implicit: a caller that gets exactly
    ``limit`` rows cannot otherwise tell a full page from a complete answer, and would
    have to guess whether more work exists.
    """
    if len(rows) <= limit:
        return rows, False
    return rows[:limit], True


#: JSON Schema for :func:`task_summary`.
TASK_SUMMARY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["project_id", "id", "title", "lifecycle", "priority", "category", "updated"],
    "properties": {
        "project_id": {"type": "string"},
        "id": {"type": "string"},
        "title": {"type": "string"},
        "lifecycle": {"type": "string", "enum": ["draft", "ready", "active", "closed"]},
        "ball": {"type": ["string", "null"], "enum": ["agent", "human", "external", None]},
        "ball_reason": {"type": ["string", "null"]},
        "ball_prompt": {"type": ["string", "null"]},
        "outcome": {"type": ["string", "null"]},
        "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "queue_position": {
            "type": ["integer", "null"],
            "minimum": 1,
            "description": (
                "Where this task stands *within* its priority band. Null once the task "
                "is closed. It is order and nothing else: high/900 comes before "
                "medium/100, because the band decides first and the number only breaks "
                "ties inside it. Change it with task_queue_move, never by patching."
            ),
        },
        "category": {"type": "string"},
        "parent": {"type": ["string", "null"]},
        "owner": {"type": ["string", "null"]},
        "updated": {"type": "string"},
        "display_status": {"type": ["string", "null"]},
        "actionable": {"type": "boolean"},
        "unmet_needs": {"type": "array", "items": {"type": "string"}},
        "open_children_count": {"type": ["integer", "null"], "minimum": 0},
    },
}

#: JSON Schema for :func:`broken_task`.
BROKEN_TASK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["task_id", "filename", "reason"],
    "properties": {
        "task_id": {"type": "string"},
        "filename": {"type": "string"},
        "reason": {"type": "string"},
    },
}

#: JSON Schema for :func:`dependency_facts`.
DEPENDENCY_FACTS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["actionable", "unmet_needs", "needs_cycles", "unblocks_count"],
    "properties": {
        "actionable": {"type": "boolean"},
        "unmet_needs": {"type": "array", "items": {"type": "string"}},
        "needs_cycles": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "string"}},
        },
        "unblocks_count": {"type": "integer", "minimum": 0},
        "open_children_count": {"type": ["integer", "null"], "minimum": 0},
    },
}

#: The stored task record. Left open on purpose: it is generated by the Pydantic
#: model on the service side, and restating its fields here would create a second
#: definition to keep in step with schema v2.
TASK_DOCUMENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["id", "title", "lifecycle", "spec"],
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "lifecycle": {"type": "string"},
        "spec": {"type": "object"},
    },
}


def summary_line(
    count: int,
    singular: str,
    *,
    plural: Optional[str] = None,
    truncated: bool = False,
    broken: int = 0,
) -> str:
    """One sentence for clients that do not read structured results.

    The plural is passed rather than derived. Appending "s" produced "3 matchs" in
    real output, and an English pluralisation rule is not worth writing for a handful
    of nouns fixed at their call sites.
    """
    word = singular if count == 1 else (plural or f"{singular}s")
    parts = [f"{count} {word}"]
    if truncated:
        parts.append("(more available; raise limit or narrow the query)")
    if broken:
        parts.append(f"and {broken} unreadable task file{'' if broken == 1 else 's'}")
    return " ".join(parts) + "."


def optional_int(value: Any, *, default: Optional[int] = None) -> Optional[int]:
    """Coerce an optional integer argument."""
    if value is None:
        return default
    return int(value)
