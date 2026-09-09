"""Stand up the version-skew banner on its own port, with throwaway data.

The condition this banner reports is one a healthy checkout cannot be in: a bundle whose
API contract differs from the server's fails `scripts/check.py` long before anybody runs
a server. So a reviewer cannot reach it by clicking around, and the only honest way to
look at it is a server that lies about its own identity on request.

Three states, chosen from a control page and switched without restarting anything:

    in step     what every real install looks like. No banner at all, and it matters
                as much as the other two -- a reviewer who only ever sees the warning
                cannot tell a detector from wallpaper.
    contract    the 2026-08-17 incident. `version` and `schema_version` are left
                exactly as they are and only `api_digest` moves, because that is what
                actually differed on the day: the two identifiers already on the route
                matched on both sides throughout while the response shape did not.
                The banner appears on load and names the restart and the rebuild.
    rebuild     a build landing under a tab that is already open, which is most
                rebuilds -- they touch no API route, so the contract digest never moves
                and only the bundle id does. The tab is allowed to see the real id
                first, so it adopts it the way a real tab does, and the id changes a
                few seconds later. **The banner arrives on the next poll**: click away
                from the tab and back, which refetches immediately, or wait a minute.

Nothing here touches the live corpus. Everything lives under a temporary directory that
is deleted when this process stops, including its own AGENTJOBS_HOME registry, so the
8876 dashboard and its registry are not involved at all.

    python scripts/version_skew_sandbox.py [port]

Stop it with Ctrl-C, or by killing the process.

This file lives in the repository rather than in whichever agent session happened to
write it, for the reason `review_queue_sandbox.py` records: task-207's copy was reachable
only from one session's temporary directory, and the follow-up that had to reproduce its
findings could not run it once that directory was cleaned up.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

DEFAULT_PORT = 8902
PROJECT_ID = "sandbox-skew"
COOKIE = "agentjobs_review_skew"

#: A digest that is a legal digest and is not anybody's. Sixty-four hex characters,
#: because the banner prints the first twelve and a short value would look like a bug.
FOREIGN_CONTRACT = "f0f0" * 16
FOREIGN_BUNDLE = "b105e0000dead"

#: How long the `rebuild` state keeps telling the truth. Long enough for the tab's first
#: poll to land and be adopted as "the build this tab is running", short enough that a
#: reviewer who reads the control page and switches back has already missed it.
HONEST_SECONDS = 5.0


def seed(manager: Any) -> None:
    """A small ordinary backlog, so the app has something to be usable *with*.

    The claim the banner makes is that the page still works while it is up, and a page
    with nothing on it cannot show that.
    """
    from agentjobs.models_v2 import Lifecycle, Priority

    def make(task_id: str, title: str, priority: Any, **kwargs: Any) -> None:
        manager.create_task(
            id=task_id,
            title=title,
            description="Seeded for a version-skew review. Nothing here is real work.",
            summary=f"{title}.",
            priority=priority,
            lifecycle=kwargs.pop("lifecycle", Lifecycle.READY),
            actor="claude",
            **kwargs,
        )

    make("task-001", "Rotate the staging credentials", Priority.CRITICAL)
    make("task-002", "Ship the dispatch runbook", Priority.HIGH)
    make("task-003", "Tidy the CLI help text", Priority.MEDIUM)
    make("task-004", "Refresh the screenshots", Priority.MEDIUM)
    make("task-005", "Drop the unused index", Priority.LOW)
    manager.claim_task("task-002", agent="claude")


class LieAboutVersion(BaseHTTPMiddleware):
    """Rewrite `/api/version` to whatever state this browser asked for.

    A middleware rather than a route, because the real route is registered first and
    would win; and the point of the sandbox is that everything else about the response
    stays exactly as the application produced it.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if request.url.path != "/api/version" or response.status_code != 200:
            return response

        state, since = _read_cookie(request)
        if state == "none":
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        payload = json.loads(body)
        if state == "contract":
            payload["api_digest"] = FOREIGN_CONTRACT
        elif state == "rebuild" and time.time() - since > HONEST_SECONDS:
            payload["bundle_id"] = FOREIGN_BUNDLE

        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(
            content=json.dumps(payload),
            status_code=response.status_code,
            headers=headers,
            media_type="application/json",
        )


def _read_cookie(request: Request) -> tuple[str, float]:
    """The state this browser chose, and when it chose it."""
    raw = request.cookies.get(COOKIE, "")
    state, _, stamp = raw.partition(":")
    if state not in {"contract", "rebuild"}:
        return "none", 0.0
    try:
        return state, float(stamp)
    except ValueError:
        return state, 0.0


CONTROL_PAGE = """<!doctype html>
<title>AgentJobs version-skew sandbox</title>
<style>
 body {{ font: 16px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 40rem;
        background: #14161a; color: #e7e9ee; }}
 a.state {{ display: block; margin: .75rem 0; padding: .9rem 1rem; border-radius: .6rem;
        border: 1px solid #3a3f4b; color: #e7e9ee; text-decoration: none; }}
 a.state:hover {{ background: #1d2029; }}
 b {{ color: #8ab4ff; }} p {{ color: #aeb4c2; }} code {{ color: #f0c674; }}
</style>
<h1>Version-skew sandbox</h1>
<p>Each link sets what this browser will be told about the server, then opens the app.
Come back here to switch. Nothing you click in the app touches real data.</p>

<a class="state" href="/review/skew/none"><b>In step</b>
<p>What every real install looks like. There should be no banner at all.</p></a>

<a class="state" href="/review/skew/contract"><b>A different API contract</b>
<p>The 2026-08-17 incident: same <code>version</code>, same <code>schema_version</code>,
different contract. The banner appears as the page loads.</p></a>

<a class="state" href="/review/skew/rebuild"><b>A rebuild under an open tab</b>
<p>The common case, and the one a contract digest alone would miss. The tab sees the
real build first, then the served build changes. <b>Click away from the tab and back</b>
to refetch immediately, or leave it a minute.</p></a>

<p>Throwaway data under <code>{root}</code>. Stop the sandbox with Ctrl-C.</p>
"""


def add_control_routes(app: Any, root: Path) -> None:
    """Add the control page, and keep it out of the OpenAPI document.

    `include_in_schema=False` is load-bearing here rather than tidiness. The digest is
    taken over the whole document, so a sandbox route that appears in it moves the
    server's `api_digest` and the "in step" state shows a banner -- which is the
    detector working correctly and the sandbox lying. Caught in review of task-014.
    """

    @app.get("/review", response_class=HTMLResponse, include_in_schema=False)
    async def control() -> str:
        return CONTROL_PAGE.format(root=root)

    @app.get("/review/skew/{state}", include_in_schema=False)
    async def choose(state: str) -> RedirectResponse:
        response = RedirectResponse(f"/app/p/{PROJECT_ID}", status_code=303)
        response.set_cookie(COOKIE, f"{state}:{time.time()}", path="/")
        return response


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-review-skew-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from agentjobs.projects import ProjectRegistry
    from agentjobs.storage import TaskStorage

    name = "Sandbox: version skew"
    project_root = root / PROJECT_ID
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    seed(TaskManager(TaskStorage(project_root / "tasks")))
    ProjectRegistry(home).add(project_root, project_id=PROJECT_ID, name=name)

    import uvicorn

    from agentjobs.api.main import app

    app.add_middleware(LieAboutVersion)
    add_control_routes(app, root)

    print(f"[review] version-skew sandbox: http://127.0.0.1:{port}/review", flush=True)
    print(f"[review] throwaway data under {root}", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
