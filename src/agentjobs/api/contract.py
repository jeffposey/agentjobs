"""The identity of the HTTP contract this process serves.

Two things are built from the same document and then run apart: the FastAPI
application, and the TypeScript client generated from ``frontend/openapi.json``. A
long-lived ``agentjobs serve`` holds its imported code in memory while development
moves underneath it, so the two can silently stop being the same code -- and nothing
in either half is wrong when that happens. The server answers correctly for the code
it has, the bundle is correct for the code it was built from, and the mismatch
surfaces as an unrelated-looking crash somewhere else entirely.

**The digest here is what makes that condition self-announcing.** It is a hash of the
whole OpenAPI document, which is exactly the input the client generator consumes, so
it changes when and only when the contract those two halves share changes.

Two identifiers already on ``/api/version`` cannot do this job, which is why a third
exists. In the incident this was written for -- 2026-08-17, a server started at 20:37
answering a bundle built at 23:25 -- the package ``version`` and the task-record
``schema_version`` were **identical on both sides the entire time**. Comparing them
would have reported agreement while ``TaskDetailResponse.related`` had in fact been
added to the response the client already believed was required.

Stamping the git commit into both halves instead was considered and rejected: it
differs on every commit that does not touch the API, so it reports skew constantly and
is therefore ignored, which is the failure mode of a warning nobody believes.

A docstring edit on a route does move the digest, because FastAPI puts docstrings in
the document and the generator puts them in the client as JSDoc. That is a real
difference between the two builds rather than a false positive, and it is still
enormously narrower than "every commit".
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

CONTRACT_DIGEST_LENGTH = 64
"""Full SHA-256 hex. Callers that display it truncate; comparisons use the whole thing."""


def canonical_document(schema: dict[str, Any]) -> str:
    """Render an OpenAPI document the one way everything here agrees to render it.

    Sorted keys and a fixed indent, because a digest over a serialisation is only
    meaningful if the serialisation is decided rather than incidental. This is the
    exact text ``scripts/export_openapi.py`` writes to ``frontend/openapi.json``, so
    the file on disk and the live application hash to the same value or the gate says
    they have drifted.
    """
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def digest_of(document: str) -> str:
    """The digest of an already-rendered document."""
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def contract_digest(schema: dict[str, Any]) -> str:
    """The digest of an OpenAPI schema, rendered canonically first."""
    return digest_of(canonical_document(schema))


def live_contract_digest(app: Any) -> str:
    """The digest of the contract *this process* serves, computed once and kept.

    Cached on the application rather than recomputed per request, and safe to cache
    for the same reason FastAPI caches ``app.openapi()``: a running process cannot
    change its own routes. That is the whole point -- a process's contract is fixed at
    import, which is why comparing it to a bundle's is worth doing at all.
    """
    cached = getattr(app.state, "contract_digest", None)
    if isinstance(cached, str):
        return cached
    computed = contract_digest(app.openapi())
    app.state.contract_digest = computed
    return computed
