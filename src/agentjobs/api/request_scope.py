"""Which project a request addresses, for the facts a read surface derives per request.

Two bindings now answer a question about "this request's project" without being given
one -- the dispatch queue (:mod:`agentjobs.api.queued_dispatch`) and the live finish
(:mod:`agentjobs.api.live_finish`) -- and both resolve it the same way. It lives here
rather than in either of them because the answer carries a visibility rule: an unscoped
request falls back to the caller's default project, resolved through the same filter
every other route uses, so a project this caller may not see answers nothing. A second
copy of that rule is a second place for it to be got wrong, and the failure it would
produce is a label leaking the state of a hidden project.
"""

from __future__ import annotations

from fastapi import Request


def request_project_id(request: Request) -> str:
    """The project this request is about, or ``""`` when it cannot be told.

    The path parameter where there is one, exactly as ``request_project`` reads it,
    because every API router is mounted both unscoped and under
    ``/api/projects/{project_id}``. An unscoped request falls back to the positional
    default, resolved through the same visibility filter.

    Nothing here raises. Every caller is deriving a convenience for a read surface, and
    a project that cannot be resolved must cost that convenience rather than the request.
    """
    from .dependencies import get_principal, try_resolve_default_project

    scoped = request.path_params.get("project_id")
    if scoped:
        return str(scoped)
    try:
        project = try_resolve_default_project(get_principal(request))
    except Exception:  # noqa: BLE001 - a label may not cost the read
        return ""
    return project.id if project is not None else ""
