"""Stand up the queue-order screens on their own port, with throwaway data.

Three projects on one server, so every state task-207 changed can be compared without
constructing any of them by hand. Switch between them with the project picker.

    sandbox-healthy   a real queue: ordered, reorderable, "Next up" + "Why this one?"
    sandbox-broken    two tasks claiming one place in the critical band, nothing else
                      wrong -- so the banner shows, the critical band loses its grips,
                      every other band stays reorderable, and the dashboard says it
                      cannot name a next task instead of answering 500
    sandbox-blocked   the same breakage with work also stopped on a human, which is the
                      case where the banner has to sit above a more urgent panel rather
                      than replace it

Nothing here touches the live corpus. Everything lives under a temporary directory
that is deleted when this process stops, including its own AGENTJOBS_HOME registry,
so the 8876 dashboard and its registry are not involved at all.

    python scripts/review_queue_sandbox.py [port] [--no-trace]

Stop it with Ctrl-C, or by killing the process.

This file lives in the repository rather than in whichever agent session happened to
write it, because task-207's copy did not: it was reachable only from one session's
temporary directory, and the follow-up that had to reproduce its findings could not
have run it once that directory was cleaned up.

**The drag trace.** Unless ``--no-trace`` is passed, the sandbox injects a small panel
into the page that records what a gesture actually did -- whether the press landed on a
reorder handle or missed it, which drag events fired, and what the reorder request
answered. It is here because task-225 could not reproduce a reported drag failure with
any automated instrument: Playwright drives Chromium's drag through
``Input.setInterceptDrags`` rather than the operating system's drag loop, so a hand on a
mouse is the only instrument left, and a hand needs somewhere to leave its evidence.
The panel is sandbox-only -- it is injected into the served HTML here and no part of it
is in the application.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

DEFAULT_PORT = 8899


def seed(manager, *, corrupt: bool, human_rungs: bool) -> None:
    """A backlog with something in every band and every claimability state."""
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Outcome, Priority

    def make(task_id, title, priority, lifecycle=Lifecycle.READY, **kwargs):
        manager.create_task(
            id=task_id,
            title=title,
            description="Seeded for a queue-order review. Nothing here is real work.",
            summary=f"{title}.",
            priority=priority,
            lifecycle=lifecycle,
            actor="claude",
            **kwargs,
        )

    # critical -- two tasks, because this is the band the broken project corrupts.
    # It has to be the band selection actually reads: the integrity check covers the
    # winning band and the ones above it, so a duplicate further down would (rightly)
    # leave the dashboard able to answer, and the "queue cannot say what is next" panel
    # would never show.
    make("task-001", "Restore the nightly backup job", Priority.CRITICAL)
    make("task-002", "Rotate the leaked API key", Priority.CRITICAL)

    # high -- the band to do the reordering in
    make("task-010", "Queue position on the schema", Priority.HIGH)
    make("task-011", "Reorder the backlog from a phone", Priority.HIGH)
    make("task-012", "Explain why a task is next", Priority.HIGH)
    make("task-013", "Retire the legacy Jinja routes", Priority.HIGH)

    # An umbrella with an open child: claimable, but the explanation has something
    # interesting to say about the ones it passed over.
    make("task-020", "Notifications that survive the session", Priority.HIGH)
    make("task-021", "Pick a delivery channel", Priority.HIGH, parent="task-020")

    # medium and low, so a cross-band drag has somewhere to land
    make("task-030", "Tidy the CLI help text", Priority.MEDIUM)
    make("task-031", "Document the dispatch config", Priority.MEDIUM)
    make("task-040", "Rename the sample project", Priority.LOW)

    # The two human rungs of the dashboard ladder, seeded only where they are wanted.
    #
    # The ladder shows exactly one panel and these two outrank everything else, so a
    # project carrying them can never show "Next up" or "the queue cannot say what is
    # next". That is why they are a flag rather than a fixed part of the fixture: the
    # sandbox-blocked project has them so the banner can be seen above a more urgent
    # panel, and the other two do not so the panels underneath can be seen at all.
    if human_rungs:
        make("task-050", "Approve the pricing page copy", Priority.HIGH)
        manager.claim_task("task-050", agent="claude")
        manager.handoff(
            "task-050",
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Read the two variants and say which one ships.",
        )

        make("task-060", "Sketch the mobile navigation", Priority.MEDIUM, lifecycle=Lifecycle.DRAFT)
        manager.handoff(
            "task-060",
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.SPEC,
            ball_prompt="Decide whether this is worth doing at all.",
        )

    # Closed work, to show it sorting behind the whole live queue rather than by band.
    make("task-070", "Migrate the corpus to schema v2", Priority.CRITICAL)
    manager.close_task("task-070", actor="claude", outcome=Outcome.COMPLETED)

    # Both critical tasks are ahead of everything and neither can be claimed, so the
    # winner comes from `high` and "Why this one?" has something to say. A skipped list
    # is the whole point of that panel; with a clean critical band it would only ever
    # read "nothing stands ahead of it".
    manager.update_task(
        "task-001",
        actor="claude",
        dependencies=[{"task": "task-013", "type": "needs", "note": "Same credentials."}],
    )
    manager.claim_task("task-002", agent="claude")

    if corrupt:
        # By hand, exactly as a bad merge or a stray editor would do it. Every verb in
        # the system refuses to produce this state, which is the point.
        #
        # Only `critical` is broken, so the sandbox shows both halves of the scoping at
        # once: the critical band loses its reorder grips and the dashboard refuses to
        # name a next task, while high, medium and low stay fully reorderable.
        tasks_dir = Path(manager.storage.tasks_dir)
        stolen = yaml.safe_load((tasks_dir / "task-001.yaml").read_text(encoding="utf-8"))
        victim_path = tasks_dir / "task-002.yaml"
        victim = yaml.safe_load(victim_path.read_text(encoding="utf-8"))
        victim["queue_position"] = stolen["queue_position"]
        victim_path.write_text(yaml.safe_dump(victim, sort_keys=False), encoding="utf-8")


# The panel injected into the sandbox's HTML. Deliberately dependency-free and inline:
# it has to work on a page served from a temporary directory, with no build step and no
# extra request, and it has to be obviously not part of the application.
DRAG_TRACE = r"""
<script>
(function () {
  var lines = [];
  var missed = null;
  var panel, body;

  function draw() {
    if (!panel) return;
    body.textContent = lines.length
      ? lines.join("\n")
      : "No gesture yet. Grab a \u283f handle and drag it onto another row.";
  }

  function push(line) {
    if (lines[lines.length - 1] !== line) lines.push(line);
    if (lines.length > 24) lines.shift();
    draw();
  }

  function describe(node) {
    if (!node || !node.tagName) return "nothing";
    var name = node.tagName.toLowerCase();
    if (node.id) name += "#" + node.id;
    var row = node.closest && node.closest("[data-task]");
    if (row) name += " (in " + row.getAttribute("data-task") + ")";
    return name;
  }

  document.addEventListener("mousedown", function (event) {
    var handle = event.target.closest && event.target.closest('[id^="queue-grip-"]');
    lines = [];
    if (handle) {
      var box = handle.getBoundingClientRect();
      missed = false;
      push("PRESS  on the handle " + handle.id +
           "  (" + Math.round(box.width) + "x" + Math.round(box.height) + " px)");
    } else {
      missed = true;
      push("PRESS  NOT on a handle -- landed on " + describe(event.target));
    }
  }, true);

  ["dragstart", "dragover", "drop", "dragend"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var row = event.target.closest && event.target.closest("[data-task]");
      push(type.toUpperCase() + (row ? "  " + row.getAttribute("data-task") : ""));
    }, true);
  });

  document.addEventListener("mouseup", function () {
    setTimeout(function () {
      if (missed) {
        push("-> nothing happened, because the press missed the handle.");
      } else if (lines.indexOf("DRAGSTART") === -1 &&
                 !lines.some(function (l) { return l.indexOf("DRAGSTART") === 0; })) {
        push("-> the browser never started a drag from the handle.");
      }
    }, 50);
  }, true);

  var fetched = window.fetch;
  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : (input && input.url) || "";
    var watched = url.indexOf("queue-move") !== -1 || url.indexOf("reprioritize") !== -1;
    var call = fetched.apply(this, arguments);
    if (watched) {
      push("REQUEST " + url.replace(/^.*\/api\//, "/api/"));
      call.then(
        function (response) { push("   -> HTTP " + response.status); },
        function (error) { push("   -> failed: " + error); }
      );
    }
    return call;
  };

  function build() {
    panel = document.createElement("div");
    panel.style.cssText = [
      "position:fixed", "right:12px", "bottom:12px", "z-index:2147483647",
      "width:380px", "max-height:45vh", "overflow:auto", "padding:10px 12px",
      "background:#0b1220", "color:#cbd5e1", "border:1px solid #334155",
      "border-radius:8px", "font:11px/1.5 ui-monospace,Consolas,monospace",
      "box-shadow:0 8px 24px rgba(0,0,0,.5)", "white-space:pre-wrap"
    ].join(";");
    var head = document.createElement("div");
    head.style.cssText = "color:#fbbf24;font-weight:700;margin-bottom:6px";
    head.textContent = "drag trace \u2014 review sandbox only";
    body = document.createElement("div");
    panel.appendChild(head);
    panel.appendChild(body);
    document.body.appendChild(panel);
    draw();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
</script>
"""


class InjectDragTrace(BaseHTTPMiddleware):
    """Put the trace panel into the application shell, and nowhere near the application.

    The shell is a ``FileResponse`` off disk, so the body is consumed and rewritten here
    rather than by changing anything the server would serve outside this sandbox.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if "text/html" not in response.headers.get("content-type", ""):
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        text = body.decode("utf-8")
        if "</head>" in text:
            text = text.replace("</head>", DRAG_TRACE + "</head>", 1)
        headers = dict(response.headers)
        headers.pop("content-length", None)
        # Never let a service worker or the browser hand back a shell without the panel.
        headers["cache-control"] = "no-store"
        return Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )


def build(root: Path, *, project_id: str, name: str, corrupt: bool, human_rungs: bool) -> Path:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from agentjobs.storage import TaskStorage

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    seed(TaskManager(TaskStorage(project_root / "tasks")), corrupt=corrupt, human_rungs=human_rungs)
    return project_root


def main() -> None:
    argv = [argument for argument in sys.argv[1:] if argument != "--no-trace"]
    trace = "--no-trace" not in sys.argv[1:]
    port = int(argv[0]) if argv else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-review-queue-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    projects = [
        ("sandbox-healthy", "Sandbox: healthy queue", False, False),
        ("sandbox-broken", "Sandbox: broken queue", True, False),
        ("sandbox-blocked", "Sandbox: broken and blocked", True, True),
    ]
    for project_id, name, corrupt, human_rungs in projects:
        registry.add(
            build(root, project_id=project_id, name=name, corrupt=corrupt, human_rungs=human_rungs),
            project_id=project_id,
            name=name,
        )

    import uvicorn

    from agentjobs.api.main import app

    if trace:
        app.add_middleware(InjectDragTrace)

    print(f"[review] queue sandbox at http://127.0.0.1:{port}/app/", flush=True)
    for project_id, _, _, _ in projects:
        print(f"[review]   http://127.0.0.1:{port}/app/p/{project_id}/tasks", flush=True)
    print(f"[review] drag trace panel: {'on' if trace else 'off (--no-trace)'}", flush=True)
    print(f"[review] throwaway data under {root}", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
