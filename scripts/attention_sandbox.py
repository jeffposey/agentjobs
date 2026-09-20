"""Stand up the Windows attention experience on its own port, with throwaway data.

Task-422 puts a red mark on the Windows taskbar and a notification in the bottom-right
corner when work stops on a person. **Neither of those is inside the browser**, so no
instrument in this repository can verify them: Playwright cannot see a taskbar, and
`mcp__claude-in-chrome` cannot see the notification area. A person looking at their own
screen is the only instrument there is, and this is where they do it.

Two projects, so both states can be compared without constructing either:

    attention-quiet     nothing waiting. No badge, no notification, ordinary tab icon.
                        This is the control, and it matters: an indicator that is always
                        on says nothing.
    attention-waiting   three tasks already stopped on you, in an episode nobody has
                        acknowledged. Opening it cold is the restart case -- **one**
                        notification summarising three, never three notifications.
    attention-stalled   both sides of task-499 in one panel: one task at the merge gate,
                        which is an ordinary ask, and one claimed twenty-two hours ago
                        that nothing is working. The second reads `agent`/`work` on its
                        own record -- that is the whole finding -- so the point to look
                        at is whether the row says so rather than reading as work in
                        flight filed in the wrong place.

The panel in the bottom-left corner is injected by this script and is no part of the
application. It stops one more task on you, clears everything, and prints the episode as
the server sees it, so the whole rule can be walked by hand:

* **stop one** on the quiet project -> the badge appears and one notification arrives;
* **stop another** while the first is unacknowledged -> the number moves and **nothing**
  interrupts;
* click the red badge in the header, or open one of the waiting tasks -> the episode is
  acknowledged, and the badge *stays up*, because work is still stopped on you;
* **stop another** now -> a new episode, and one more notification;
* **clear everything** -> badge gone, episode gone.

Nothing here touches the live corpus. Everything lives under a temporary directory that
is deleted when this process stops, including its own AGENTJOBS_HOME registry, so the
8876 dashboard and its registry are not involved at all.

    python scripts/attention_sandbox.py [port] [--no-panel]

Stop it with Ctrl-C, or by killing the process.

**To see the taskbar badge you have to install the sandbox as an app** -- in Chrome, the
install icon in the address bar, or the menu's "Cast, save and share" -> "Install page as
app". `navigator.setAppBadge` has no taskbar to draw on for an ordinary tab. The tab
icon goes red either way, and so does the notification.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

import yaml
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

DEFAULT_PORT = 8921

PANEL_SCRIPT = Path(__file__).resolve().parent / "attention_panel.js"


def _make(manager, task_id: str, title: str, *, priority) -> None:
    from agentjobs.models_v2 import Lifecycle

    manager.create_task(
        id=task_id,
        title=title,
        description="Seeded for an attention review. Nothing here is real work.",
        summary=f"{title}.",
        priority=priority,
        lifecycle=Lifecycle.READY,
        actor="claude",
    )


def _stop_on_human(manager, task_id: str, prompt: str) -> None:
    """Put a task on the human the way a real handoff does, through the verbs."""
    from agentjobs.models_v2 import Ball, BallReason

    manager.claim_task(task_id, agent="claude")
    manager.handoff(
        task_id,
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt=prompt,
    )


ABANDONED_HOURS = 22
"""How long ago the abandoned task was claimed. The length of the 2026-09-19 outage."""


def _abandon(manager, task_id: str) -> None:
    """Claim a task and then leave it, the way a session that died leaves one (task-499).

    The claim goes through the verb, so the record is exactly what the incident left
    behind: `active`/`agent`/`work`, a `ball_prompt` addressed to an agent, and nothing
    after it.

    The backdating does not, and cannot. The log is append-only by design and
    ``_replace_log`` silently declines to rewrite an entry that already exists, which is
    the right rule for the product and leaves a sandbox no way to say "this happened
    yesterday" -- and it has to, because the signal is measured from the newest log entry
    and nobody is going to wait twenty-two hours to look at a screen. So the timestamps
    are moved with SQL, against this sandbox's own throwaway database, which
    ``sandbox_store`` has already refused to open anywhere near the real one.
    """
    from datetime import datetime, timedelta, timezone

    manager.claim_task(task_id, agent="claude")
    stale = (datetime.now(timezone.utc) - timedelta(hours=ABANDONED_HOURS)).isoformat()
    store = manager.storage
    with store.database.write() as connection:
        connection.execute(
            "UPDATE log_entry SET ts = ? WHERE project_id = ? AND task_id = ?",
            (stale, store.project_id, task_id),
        )
        connection.execute(
            "UPDATE task SET updated_at = ?, last_activity_at = ? "
            "WHERE project_id = ? AND task_id = ?",
            (stale, stale, store.project_id, task_id),
        )


def seed(manager, *, waiting: int, abandoned: int = 0) -> None:
    """A small backlog, with `waiting` of it stopped on the person and `abandoned` on nobody."""
    from agentjobs.models_v2 import Priority

    titles = [
        ("task-001", "Approve the pricing page copy", Priority.HIGH),
        ("task-002", "Decide the retention window", Priority.HIGH),
        ("task-003", "Review the credential rotation branch", Priority.CRITICAL),
        ("task-004", "Tidy the CLI help text", Priority.MEDIUM),
        ("task-005", "Document the dispatch config", Priority.MEDIUM),
        ("task-006", "Rename the sample project", Priority.LOW),
        ("task-007", "Refresh the screenshots", Priority.LOW),
        ("task-008", "Drop the unused index", Priority.LOW),
    ]
    for task_id, title, priority in titles:
        _make(manager, task_id, title, priority=priority)

    for index in range(waiting):
        task_id = titles[index][0]
        _stop_on_human(manager, task_id, "Read it and say whether it ships.")

    for index in range(waiting, waiting + abandoned):
        _abandon(manager, titles[index][0])


class InjectPanel(BaseHTTPMiddleware):
    """Put the control panel into the application shell, and nowhere near the app.

    The shell is a ``FileResponse`` off disk, so the body is consumed and rewritten here
    rather than by changing anything the server would serve outside this sandbox.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if "text/html" not in response.headers.get("content-type", ""):
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        text = body.decode("utf-8")
        script = f"<script>\n{PANEL_SCRIPT.read_text(encoding='utf-8')}\n</script>"
        if "</head>" in text:
            text = text.replace("</head>", script + "</head>", 1)
        headers = dict(response.headers)
        headers.pop("content-length", None)
        # Never let the service worker or the browser hand back a shell without it.
        headers["cache-control"] = "no-store"
        return Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )


def add_control_routes(app: Any) -> None:
    """The two gestures the review needs, as routes the panel can call.

    Deliberately outside ``/api``, and out of the OpenAPI document: they are sandbox
    scaffolding, they take no principal, and nothing about them should look like part of
    the product's surface. ``include_in_schema=False`` is load-bearing rather than tidy --
    a route added to the app changes the contract digest, and the page then greets the
    reviewer with the version-skew banner instead of the thing they came to look at.
    """
    from agentjobs.api.dependencies import manager_for
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle
    from agentjobs.projects import ProjectRegistry

    def manager(project_id: str):
        return manager_for(ProjectRegistry().get(project_id))

    @app.post("/review/stop/{project_id}", include_in_schema=False)
    async def stop_one(project_id: str) -> Dict[str, Any]:
        tasks = manager(project_id)
        free = [
            task
            for task in tasks.list_tasks()
            if task.ball is not Ball.HUMAN and task.lifecycle is not Lifecycle.CLOSED
        ]
        if not free:
            return {"message": "nothing left to stop; clear everything first"}
        target = free[0]
        _stop_on_human(tasks, target.id, "Read it and say whether it ships.")
        return {"message": f"{target.id} is now waiting on you"}

    @app.post("/review/clear/{project_id}", include_in_schema=False)
    async def clear_all(project_id: str) -> Dict[str, Any]:
        tasks = manager(project_id)
        cleared = []
        for task in tasks.list_tasks(ball=Ball.HUMAN):
            if task.lifecycle is Lifecycle.DRAFT:
                continue
            tasks.handoff(
                task.id,
                actor="Jeff Posey",
                ball=Ball.AGENT,
                ball_reason=BallReason.WORK,
                ball_prompt="Carry on.",
            )
            cleared.append(task.id)
        return {"message": f"cleared {len(cleared)}", "tasks": cleared}


def build(root: Path, *, project_id: str, name: str, waiting: int, abandoned: int = 0) -> Path:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    manager = TaskManager(sandbox_store(project_root / "tasks", project_id=project_id))
    seed(manager, waiting=waiting, abandoned=abandoned)
    return project_root


def main() -> None:
    argv = [argument for argument in sys.argv[1:] if not argument.startswith("--")]
    panel = "--no-panel" not in sys.argv[1:]
    port = int(argv[0]) if argv else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-attention-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    projects = [
        ("attention-quiet", "Sandbox: nothing waiting", 0, 0),
        ("attention-waiting", "Sandbox: three already waiting", 3, 0),
        ("attention-stalled", "Sandbox: one waiting, one abandoned", 1, 1),
    ]
    for project_id, name, waiting, abandoned in projects:
        registry.add(
            build(
                root,
                project_id=project_id,
                name=name,
                waiting=waiting,
                abandoned=abandoned,
            ),
            project_id=project_id,
            name=name,
        )

    import uvicorn

    from agentjobs.api.main import app

    if panel:
        app.add_middleware(InjectPanel)
    add_control_routes(app)

    print(f"[review] attention sandbox at http://127.0.0.1:{port}/app/", flush=True)
    for project_id, name, _waiting, _abandoned in projects:
        print(f"[review]   {name}: http://127.0.0.1:{port}/app/p/{project_id}", flush=True)
    print("[review] install it as an app to see the taskbar badge:", flush=True)
    print("[review]   Chrome menu -> Cast, save and share -> Install page as app", flush=True)
    print(
        "[review] allow notifications when Chrome asks, or the toast half is untested", flush=True
    )
    print(f"[review] control panel: {'on' if panel else 'off (--no-panel)'}", flush=True)
    print(f"[review] throwaway data under {root}", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
