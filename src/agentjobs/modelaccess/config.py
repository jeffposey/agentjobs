"""What this machine will call, and the credential it calls with.

``~/.agentjobs/model.yaml``, machine-local, never committed, and never reachable from a
project's ``.agentjobs/config.yaml``. That split is ``dispatch.yaml``'s, taken for
``dispatch.yaml``'s reason rather than by analogy: *what a project is* is versioned with
the project, *what this machine will spend* is not. A credential readable from a
project's own config would mean that cloning a repository carries a "spend money on my
machine" payload the project's config legitimises, which is the thing dispatch exists to
make unrepresentable (design §4).

**The credential rule, which nothing here may weaken:**

    It is read at call time and exists only in the outbound request header. It is never
    written to a task record, a log entry, an error message, an API response, the run
    ledger, or the frontend bundle -- and it is never echoed back by any route that
    reports configuration.

Two consequences are structural rather than intended. :class:`ModelConfig` **does not
carry the credential**: it is a description of what to call, safe to log and safe to
serialise, and :func:`resolve_credential` is a separate call that returns the secret to
exactly the one caller that puts it in a header. And :class:`Availability` answers a
boolean and a sentence -- there is no field it could put a key, a prefix or a length in,
so redaction is never the mechanism.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import yaml

from agentjobs.dispatch.config import sentinel_active, sentinel_path
from agentjobs.projects import default_home

CONFIG_FILENAME = "model.yaml"
SUPPORTED_VERSION = 1

ENV_CREDENTIAL = "ANTHROPIC_API_KEY"
"""The environment variable a container or a service unit sets instead of the file."""

DEFAULT_PROVIDER = "anthropic"
DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 2048
DEFAULT_CALLS_PER_HOUR = 60
DEFAULT_TIMEOUT_SECONDS = 30


# ----- the closed set of reasons ----------------------------------------------

Reason = str
"""A stable code naming why a call is unavailable or failed. See :data:`REASONS`."""

UNCONFIGURED: Reason = "unconfigured"
SENTINEL: Reason = "sentinel"
INVALID_CONFIG: Reason = "invalid_config"
OVER_CAP: Reason = "over_cap"
REFUSED: Reason = "refused"
RATE_LIMITED: Reason = "rate_limited"
UPSTREAM: Reason = "upstream"
TIMEOUT: Reason = "timeout"
MALFORMED: Reason = "malformed"

REASONS: frozenset = frozenset(
    {
        UNCONFIGURED,
        SENTINEL,
        INVALID_CONFIG,
        OVER_CAP,
        REFUSED,
        RATE_LIMITED,
        UPSTREAM,
        TIMEOUT,
        MALFORMED,
    }
)
"""Every reason that may cross the API boundary, and there is no tenth.

The design (§4) names six: ``unconfigured``, ``refused``, ``rate_limited``,
``over_cap``, ``upstream``, ``timeout``. Three more are here and each earns its place by
telling an operator something the six would misreport.

``sentinel`` is not a provider error at all -- it is the operator's own kill switch, and
folding it into ``unconfigured`` would tell someone to go and configure a credential they
already have. ``invalid_config`` separates "there is a file and it is wrong" from "there
is no file", which are different five-second fixes. ``malformed`` is the case the design
predicted for option A and that option B does not eliminate: the provider answered
successfully and the answer was not the shape it was asked for. Reporting that as
``upstream`` would send someone to a status page over a reply that arrived intact.

**The set is closed because an upstream error body can echo request metadata.** A caller
learns which of these nine happened and one sentence written here; it never learns what
the provider said.
"""

REASON_DETAIL: Dict[Reason, str] = {
    UNCONFIGURED: (
        "No model is configured on this machine, so AgentJobs cannot draft. "
        "Add ~/.agentjobs/model.yaml or set ANTHROPIC_API_KEY for the server process."
    ),
    SENTINEL: (
        "This machine is stopped: the DISPATCH_DISABLED sentinel is present, which "
        "halts model calls as well as runs. Delete it to re-enable both."
    ),
    INVALID_CONFIG: (
        "~/.agentjobs/model.yaml exists but cannot be read as a valid configuration."
    ),
    OVER_CAP: (
        "This machine has made its configured number of model calls in the last hour. "
        "Raise calls_per_hour in ~/.agentjobs/model.yaml, or wait."
    ),
    REFUSED: "The model provider refused the request. Check the credential and the model id.",
    RATE_LIMITED: "The model provider is rate-limiting this machine. Try again shortly.",
    UPSTREAM: "The model provider could not be reached, or answered with an error.",
    TIMEOUT: "The model provider did not answer within the configured timeout.",
    MALFORMED: "The model answered, but not in the shape a task spec could be read from.",
}
"""One sentence per reason, written here so no provider text is ever forwarded.

Every sentence says what a person can *do*, because the likeliest cause of any of them is
a configuration mistake and a message that says only "unavailable" turns a one-line fix
into a debugging session.
"""


class ModelConfigError(Exception):
    """``model.yaml`` exists but cannot be trusted.

    Never carries the credential: every message here is built from the path and from a
    field name, never from a loaded value.
    """


@dataclass(frozen=True)
class ModelConfig:
    """What to call, and under what bounds. **Carries no credential.**

    Safe to log, to compare in a test, and to render into a status response field by
    field. The secret is reached only through :func:`resolve_credential`, which is a
    separate call precisely so that "did this object end up somewhere it should not
    have" is never a question anyone has to answer about a key.
    """

    provider: str = DEFAULT_PROVIDER
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    calls_per_hour: int = DEFAULT_CALLS_PER_HOUR
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    @property
    def messages_url(self) -> str:
        """The endpoint one drafting call posts to."""
        return f"{self.base_url.rstrip('/')}/v1/messages"


@dataclass(frozen=True)
class Availability:
    """Whether a drafting call can be made right now, and why not when it cannot.

    **There is no field here a credential could occupy.** ``model`` is a configured
    identifier, which is not a secret and is worth showing -- a person who asked for a
    draft is entitled to know what drafted it.
    """

    available: bool
    reason: Optional[Reason] = None
    detail: Optional[str] = None
    model: Optional[str] = None
    calls_per_hour: Optional[int] = None
    calls_used: Optional[int] = None


# ----- locations --------------------------------------------------------------


def _home(home: Optional[Path]) -> Path:
    """Resolve the AgentJobs home, honouring ``AGENTJOBS_HOME`` via ``projects.py``."""
    return Path(home).expanduser().resolve() if home else default_home()


def model_config_path(home: Optional[Path] = None) -> Path:
    """Location of the machine-local model configuration."""
    return _home(home) / CONFIG_FILENAME


# ----- loading ----------------------------------------------------------------


def load_model_config(home: Optional[Path] = None) -> Optional[ModelConfig]:
    """Read ``model.yaml``, or return ``None`` when there is none.

    Read from disk on every call and never cached, for ``load_dispatch_config``'s
    reason: this file is edited by hand far more often than it is read, and a stale
    model id after an edit is the kind of failure that costs an hour to find.

    ``None`` is not an error. A machine with no model configured is the ordinary state
    and the one every install starts in.
    """
    path = model_config_path(home)
    if not path.is_file():
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ModelConfigError(f"Cannot read model config at {path}: {exc}") from exc
    if loaded is None:
        return ModelConfig()
    if not isinstance(loaded, dict):
        raise ModelConfigError(f"Model config at {path} must be a mapping.")

    version = loaded.get("version", SUPPORTED_VERSION)
    if version != SUPPORTED_VERSION:
        raise ModelConfigError(
            f"Model config at {path} declares version {version!r}; "
            f"this build understands version {SUPPORTED_VERSION}."
        )

    provider = str(loaded.get("provider", DEFAULT_PROVIDER))
    if provider != DEFAULT_PROVIDER:
        raise ModelConfigError(
            f"Model config at {path} names provider {provider!r}; only "
            f"{DEFAULT_PROVIDER!r} is implemented. A local model is reached by pointing "
            "base_url at an endpoint that speaks the same wire format "
            "(docs/model-access-design.md §1, option C)."
        )

    return ModelConfig(
        provider=provider,
        base_url=str(loaded.get("base_url", DEFAULT_BASE_URL)),
        model=str(loaded.get("model", DEFAULT_MODEL)),
        max_tokens=_positive_int(loaded, "max_tokens", DEFAULT_MAX_TOKENS, path),
        calls_per_hour=_positive_int(loaded, "calls_per_hour", DEFAULT_CALLS_PER_HOUR, path),
        timeout_seconds=_positive_int(loaded, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS, path),
    )


def _positive_int(loaded: Dict[str, object], key: str, fallback: int, path: Path) -> int:
    """One integer field, refused rather than coerced when it is not a positive integer.

    The message names the key and the path and never the value, which keeps this helper
    safe to call on a file that also contains ``api_key``.
    """
    if key not in loaded:
        return fallback
    value = loaded[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ModelConfigError(f"Model config at {path}: {key} must be a positive integer.")
    return value


def resolve_credential(home: Optional[Path] = None) -> Optional[str]:
    """The credential, from the file or from the environment, or ``None``.

    **The only function in this package that returns the secret**, and its one caller is
    the request builder in :mod:`~agentjobs.modelaccess.client`. The file wins over the
    environment: a machine that has been given a file has been configured deliberately,
    where an inherited environment variable may be a leftover from something else.

    A project's ``.agentjobs/config.yaml`` is not consulted and must never be -- see this
    module's docstring for why that is a rule rather than an omission.
    """
    path = model_config_path(home)
    if path.is_file():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            # A file that cannot be parsed has no credential in it as far as this
            # function is concerned. `load_model_config` is what reports the breakage,
            # and it raises with the path rather than with anything read out of it.
            loaded = None
        if isinstance(loaded, dict):
            from_file = loaded.get("api_key")
            if isinstance(from_file, str) and from_file.strip():
                return from_file.strip()
    from_env = os.environ.get(ENV_CREDENTIAL)
    return from_env.strip() if from_env and from_env.strip() else None


# ----- can a call be made right now -------------------------------------------


def availability(home: Optional[Path] = None) -> Availability:
    """Whether a drafting call can be made on this machine right now.

    The order is deliberate and each step answers something the next cannot.

    1. **The sentinel first**, before anything reads configuration. Someone who created
       ``~/.agentjobs/DISPATCH_DISABLED`` wants this machine to stop causing model work,
       and reporting ``unconfigured`` at them would be the wrong instruction. The design
       (§5) takes the sentinel one rung more general than its filename for exactly this:
       it is the operator's single blunt stop, and a kill switch that leaves a second
       spending path running is not a kill switch.
    2. **A broken file is not an absent one**, so ``invalid_config`` is reported before
       the credential is looked for.
    3. **No credential is ``unconfigured``**, which is the state of a fresh install and
       the one path guaranteed to be exercised on day one.
    4. **The hourly cap last**, because it is the only one that changes by itself. It is
       reported here rather than only at call time so the control can say "not right now"
       instead of failing a press.

    The three dispatch gates that are *not* consulted are as load-bearing as the one that
    is: ``enabled:``, the runner map and per-project enablement all authorise spawning a
    process for repository work, and a machine with no runner configured at all should
    still be able to draft a task.
    """
    if sentinel_active(home):
        return Availability(False, SENTINEL, REASON_DETAIL[SENTINEL])

    try:
        config = load_model_config(home)
    except ModelConfigError:
        # The exception's message names the path; the response says only that the file
        # is unreadable, because a parse error can quote the line it failed on.
        return Availability(False, INVALID_CONFIG, REASON_DETAIL[INVALID_CONFIG])

    if resolve_credential(home) is None:
        return Availability(False, UNCONFIGURED, REASON_DETAIL[UNCONFIGURED])

    resolved = config or ModelConfig()

    from .budget import calls_in_last_hour

    used = calls_in_last_hour(home)
    if used >= resolved.calls_per_hour:
        return Availability(
            False,
            OVER_CAP,
            REASON_DETAIL[OVER_CAP],
            model=resolved.model,
            calls_per_hour=resolved.calls_per_hour,
            calls_used=used,
        )

    return Availability(
        True,
        None,
        None,
        model=resolved.model,
        calls_per_hour=resolved.calls_per_hour,
        calls_used=used,
    )


def sentinel_location(home: Optional[Path] = None) -> Path:
    """Where the shared kill switch lives, re-exported so callers need not import dispatch."""
    return sentinel_path(home)
