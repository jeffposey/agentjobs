"""Calling a model from the server, for short, bounded, human-triggered drafts.

``docs/model-access-design.md`` is the decision this package implements (task-174). The
scope it sets is narrow and the narrowness is what every rule here is sized for: one
HTTPS request, started by a person, returning text into a form. There is no agentic loop
here, nothing retries by itself, and nothing calls a model except a route a human hit.

The four modules split along the lines the design argues for:

* :mod:`~agentjobs.modelaccess.config` -- what this machine is configured to call, and
  the credential, which never leaves this module except as an outbound header.
* :mod:`~agentjobs.modelaccess.budget` -- ``calls_per_hour``, counted separately from
  dispatch's run budgets because the thing being bounded is different.
* :mod:`~agentjobs.modelaccess.client` -- the request, and the mapping from every
  failure to a closed set of reasons before anything crosses the API boundary.
* :mod:`~agentjobs.modelaccess.draft` -- the one prompt this package sends, and the
  parse that turns a reply into spec fields and nothing else.

**Nothing here is reachable without configuration**, which is the state of every machine
that has not opted in, including the one this was written on. See
:func:`~agentjobs.modelaccess.config.availability`.
"""

from __future__ import annotations

from .budget import calls_in_last_hour, record_call, within_cap
from .client import ModelCallError, call_model
from .config import (
    Availability,
    ModelConfig,
    ModelConfigError,
    REASON_DETAIL,
    REASONS,
    Reason,
    availability,
    load_model_config,
    model_config_path,
    resolve_credential,
)
from .draft import SpecDraft, draft_spec, parse_draft_reply, render_prompt

__all__ = [
    "Availability",
    "ModelCallError",
    "ModelConfig",
    "ModelConfigError",
    "REASONS",
    "REASON_DETAIL",
    "Reason",
    "SpecDraft",
    "availability",
    "call_model",
    "calls_in_last_hour",
    "draft_spec",
    "load_model_config",
    "model_config_path",
    "parse_draft_reply",
    "record_call",
    "render_prompt",
    "resolve_credential",
    "within_cap",
]
