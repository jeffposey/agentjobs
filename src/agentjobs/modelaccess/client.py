"""The one outbound request, and the wall between its failures and this application.

Deliberately ``urllib`` rather than a vendor SDK, which is the choice
``scripts/model_access_probe.py`` already made and for the same reason: one POST to one
documented endpoint does not earn a dependency, and a dependency here would have to be
kept current on a schedule this repository does not have. The wire format is the
Anthropic Messages API; pointing ``base_url`` at anything else that speaks it is the
seam the design keeps open for a local model (§1, option C).

**Every failure is translated before it leaves this module.** A provider's error body
can echo request metadata, and the request carries the credential in a header, so
nothing upstream is ever forwarded, re-raised with its own message, or logged. What
crosses the boundary is one code from
:data:`~agentjobs.modelaccess.config.REASONS` and one sentence written in this
repository. The exception type is kept on the error object for a local reader; the
sentence a caller sees is never built from it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .config import (
    MALFORMED,
    RATE_LIMITED,
    REASON_DETAIL,
    REFUSED,
    TIMEOUT,
    UNCONFIGURED,
    UPSTREAM,
    ModelConfig,
    Reason,
    resolve_credential,
)

ANTHROPIC_VERSION = "2023-06-01"


class ModelCallError(Exception):
    """A failed model call, carrying only what may cross the API boundary.

    ``reason`` is one of :data:`~agentjobs.modelaccess.config.REASONS` and ``detail`` is
    the sentence this repository wrote for it. Neither is derived from the provider's
    response, and the provider's response is not kept.
    """

    def __init__(self, reason: Reason, detail: Optional[str] = None) -> None:
        self.reason = reason
        self.detail = detail or REASON_DETAIL.get(reason, REASON_DETAIL[UPSTREAM])
        super().__init__(self.detail)


def _reason_for_status(status: int) -> Reason:
    """Which reason an HTTP status maps to.

    Three buckets, because three are what a person can act on differently: 401/403 means
    the credential or the model id is wrong and they should look at ``model.yaml``; 429
    means wait; anything else means the provider had a problem and there is nothing to
    fix here. A 400 lands in the last bucket deliberately -- a malformed request would be
    a defect in this module, not something an operator can configure their way out of.
    """
    if status in (401, 403):
        return REFUSED
    if status == 429:
        return RATE_LIMITED
    return UPSTREAM


def call_model(
    prompt: str,
    config: ModelConfig,
    *,
    home: Optional[Path] = None,
    opener: Optional[object] = None,
) -> str:
    """Send one prompt, return the reply's text, or raise :class:`ModelCallError`.

    ``opener`` exists so the request can be exercised without a network or a credential:
    the suite substitutes one, and everything above this line -- the header assembly, the
    status mapping, the extraction of text from the response shape -- is then under test
    on a machine that has no model configured at all. It is never set in production; the
    default is :func:`urllib.request.urlopen`.

    The credential is fetched here, one line before it becomes a header, and is not
    stored on ``config``, returned, or included in any raise.
    """
    credential = resolve_credential(home)
    if credential is None:
        raise ModelCallError(UNCONFIGURED)

    body = json.dumps(
        {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        config.messages_url,
        data=body,
        headers={
            "content-type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            "x-api-key": credential,
        },
    )

    send = opener if opener is not None else urllib.request.urlopen
    try:
        with send(request, timeout=config.timeout_seconds) as response:  # type: ignore[operator]
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # `exc` holds the provider's response body. It is not read, not logged and not
        # attached to anything; only its status code is consulted.
        raise ModelCallError(_reason_for_status(exc.code)) from None
    except (TimeoutError, OSError) as exc:
        # `urllib.error.URLError` is an OSError, and a socket timeout surfaces as either
        # depending on where it fired, so both are caught together and told apart by
        # asking what the cause was rather than by which clause matched.
        raise ModelCallError(TIMEOUT if _is_timeout(exc) else UPSTREAM) from None
    except ValueError:
        # The provider answered with something that is not JSON.
        raise ModelCallError(UPSTREAM) from None

    text = _text_of(payload)
    if not text.strip():
        raise ModelCallError(MALFORMED)
    return text


def _is_timeout(exc: BaseException) -> bool:
    """Whether a transport failure was a timeout, at whatever depth it was wrapped.

    ``URLError.reason`` is the wrapped cause when the transport raised one and a plain
    string when it did not, so only an exception is followed. The bounded loop is
    deliberate: a cycle in ``__cause__`` is possible and an unbounded walk here would
    hang the request this module exists to bound.
    """
    seen: BaseException = exc
    for _ in range(4):
        if isinstance(seen, TimeoutError):
            return True
        wrapped = getattr(seen, "reason", None)
        cause = wrapped if isinstance(wrapped, BaseException) else seen.__cause__
        if not isinstance(cause, BaseException) or cause is seen:
            return False
        seen = cause
    return False


def _text_of(payload: object) -> str:
    """The text blocks of a Messages response, concatenated.

    Tolerant of a payload that is not the expected shape rather than indexing into it:
    an unexpected response becomes an empty string, which the caller turns into
    ``malformed``. Raising a ``KeyError`` here would escape as a 500 carrying a traceback
    through a module whose whole job is that nothing upstream escapes.
    """
    if not isinstance(payload, dict):
        return ""
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        return ""
    return "".join(
        str(block.get("text", ""))
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    )
