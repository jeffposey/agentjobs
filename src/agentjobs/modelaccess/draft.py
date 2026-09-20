"""The one prompt this application sends a model, and the parse that reads it back.

What a draft may contain is decided here, structurally, and not by asking the model
nicely. :class:`SpecDraft` has six fields; :func:`parse_draft_reply` reads those six and
discards every other key the reply contains. So a model that returns
``{"priority": "critical", "parent": "task-001", "dependencies": [...]}`` produces a
draft in which those values do not exist -- not a draft that carries them into a form
where somebody might not notice.

**That is the point of the constraint and not merely its implementation.** Lifecycle,
ball, priority, parent, dependencies and actor are the human's. A model guessing a
dependency graph produces a task that looks authoritative and is wrong, and the way to
stop a guess reaching a record is for there to be nowhere to put it.

The prompt states the Resumption Contract because that is the standard the output is
judged against (``ALLAGENTS.md``). It is short on purpose: this is one bounded call, not
a session with the repository in front of it, and a prompt that recited the whole of
``docs/task-schema.md`` would cost tokens to produce text that is going to be edited by
a person anyway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .budget import record_call
from .client import ModelCallError, call_model
from .config import (
    MALFORMED,
    UNCONFIGURED,
    Availability,
    ModelConfig,
    availability,
    load_model_config,
)

MAX_INPUT_CHARS = 8000
"""How much of what a person typed is sent.

A bound rather than a guess: the call is meant to be short, and somebody pasting a log
file into the description should get a draft rather than a timeout. Truncation is stated
in the prompt so the model does not treat a cut-off sentence as the whole thought.
"""

SPEC_FIELDS = (
    "summary",
    "intent",
    "description",
    "constraints",
    "out_of_scope",
)
"""The prose fields a draft may set. ``acceptance`` is a list and is handled separately."""


@dataclass(frozen=True)
class SpecDraft:
    """One drafted spec: prose fields and acceptance criteria, and nothing else.

    Every field is a string or a list of strings that lands in a form input. There is no
    field for lifecycle, ball, priority, parent, dependency or actor, which is how the
    constraint is enforced rather than asserted.
    """

    summary: str = ""
    intent: str = ""
    description: str = ""
    constraints: str = ""
    out_of_scope: str = ""
    acceptance: List[str] = field(default_factory=list)

    def filled(self) -> List[str]:
        """The names of the fields this draft actually has content for."""
        names = [name for name in SPEC_FIELDS if getattr(self, name).strip()]
        if self.acceptance:
            names.append("acceptance")
        return names


PROMPT_TEMPLATE = """\
You are drafting a task record for AgentJobs, a task tracker for software work.

A task record has to be sufficient working memory for someone who picks it up with no
other context: no access to the conversation that created it and no access to whoever
wrote it. That standard is called the Resumption Contract, and it is what your draft is
judged against.

Expand what the human typed into these fields:

- summary: one or two sentences that orient a reader with no prior context. Not a
  clipped first line of the description, and not a restatement of the title.
- intent: why this task exists -- what goes wrong if it is not done, or what becomes
  possible if it is. This is the field a later reader uses to decide whether the task
  still matters.
- description: the working specification. What must be done, the behaviour that must
  hold, and the boundaries. Use Markdown. Be concrete about the parts the human was
  concrete about, and do not invent specifics they did not give you.
- constraints: hard requirements and prohibitions, or an empty string if there are none.
- out_of_scope: explicit non-goals, or an empty string if there are none.
- acceptance: a list of independently verifiable statements. Each one must be something
  a reader could check and get the same answer as anyone else. "Works correctly" is not
  one; "the list returns within one page of results for a project with 500 tasks" is.

Rules you must follow:

- Do not invent facts. Where the human was vague, write a specification that is honest
  about the vagueness, or state the open question inside the description. Do not
  manufacture file paths, module names, numbers or deadlines that were not given to you.
- Do not set lifecycle, ball, priority, parent, dependencies, tags or actor. They are
  not yours to decide and they are not in the output format.
- Write plainly. No marketing tone, no restating the instructions back.

Return ONLY a JSON object with exactly these keys: summary, intent, description,
constraints, out_of_scope, acceptance. `acceptance` is a list of strings; every other
value is a string. No prose outside the JSON.

The task is being filed into the project `{project_id}`.

Title the human typed:
{title}

What the human typed as the description:
{description}
"""


def render_prompt(title: str, description: str, project_id: str) -> str:
    """The prompt for one drafting call.

    Both free-text inputs are truncated to :data:`MAX_INPUT_CHARS` with the cut marked,
    so a very long paste becomes a shorter call rather than a timeout, and the model is
    told that it is reading a fragment rather than a finished thought.
    """
    return PROMPT_TEMPLATE.format(
        project_id=project_id,
        title=_clip(title) or "(the human left the title empty)",
        description=_clip(description) or "(the human left the description empty)",
    )


def _clip(text: str) -> str:
    stripped = (text or "").strip()
    if len(stripped) <= MAX_INPUT_CHARS:
        return stripped
    return stripped[:MAX_INPUT_CHARS] + "\n\n[...truncated; the human wrote more than this]"


def parse_draft_reply(reply: str) -> SpecDraft:
    """Read a reply into a :class:`SpecDraft`, or raise :class:`ModelCallError`.

    A fenced code block is unwrapped, because a model wrapping JSON in ``` is a
    formatting habit rather than a failure and making a person re-press a button over it
    would be the wrong trade. Prose wrapped *around* the object is not recovered: that is
    the case where something has to guess which part of the answer was the answer, and
    the design's whole objection to option A is that guessing there produces a plausible
    paragraph in the wrong shape.
    """
    loaded = _load_object(reply)
    if loaded is None:
        raise ModelCallError(MALFORMED)
    # Every other key in `loaded` is discarded by never being read. See this module's
    # docstring: that is the constraint, not an oversight.
    return SpecDraft(
        summary=_string(loaded.get("summary")),
        intent=_string(loaded.get("intent")),
        description=_string(loaded.get("description")),
        constraints=_string(loaded.get("constraints")),
        out_of_scope=_string(loaded.get("out_of_scope")),
        acceptance=_strings(loaded.get("acceptance")),
    )


def _load_object(reply: str) -> Optional[Dict[str, Any]]:
    stripped = (reply or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        stripped = stripped.rsplit("```", 1)[0]
    try:
        loaded = json.loads(stripped.strip())
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _string(value: Any) -> str:
    """One prose field, with anything that is not a string treated as absent.

    Not coerced with ``str()``: a model returning a list where a string was asked for
    would otherwise put ``['a', 'b']`` into a form field, which reads as a bug in
    AgentJobs rather than as a bad draft.
    """
    return value.strip() if isinstance(value, str) else ""


def _strings(value: Any) -> List[str]:
    """Acceptance criteria, filtered to the entries that are actually text."""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def draft_spec(
    title: str,
    description: str,
    project_id: str,
    *,
    home: Optional[Path] = None,
    config: Optional[ModelConfig] = None,
    opener: Optional[object] = None,
) -> SpecDraft:
    """Draft one spec, checking every gate first. Raises :class:`ModelCallError`.

    The gates are re-checked here rather than trusted from whatever the caller last read
    off the status route: the sentinel can appear between a page loading and a button
    being pressed, and the hourly cap moves on its own. A status route is a hint for the
    interface, never the authority for the call.

    The call is counted *before* it is sent -- see
    :func:`~agentjobs.modelaccess.budget.record_call` -- so a provider that refuses every
    request still runs the counter down instead of being retried without limit.
    """
    state: Availability = availability(home)
    if not state.available:
        raise ModelCallError(state.reason or UNCONFIGURED, state.detail)

    resolved = config or load_model_config(home) or ModelConfig()
    record_call(home)
    reply = call_model(
        render_prompt(title, description, project_id), resolved, home=home, opener=opener
    )
    return parse_draft_reply(reply)
