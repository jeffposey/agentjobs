"""Stand up the one capture control on its own port so it can be pressed.

task-346 merged Create and Report issue into a single control: a `+` in the header's
actions region, beside the actions kebab, over a single form that opens on two fields
and expands in place to everything ``/tasks/new`` ever collected. Everything worth
reviewing about that is gesture and proportion -- whether the trigger is findable where
a nav link used to be, whether the quick capture still feels like fifteen seconds,
whether the expanded form is bearable in a dialog on a phone -- and none of it survives
a diff.

    python scripts/capture_control_sandbox.py [port] [--tailnet]

``--tailnet`` also serves this machine's Tailscale address, in the same process and over
the same corpus, so the page opens on a phone. That is the half a resized desktop window
cannot answer: the control replaced a floating button that was under a thumb, and whether
the top-right corner of a phone is reachable one-handed is a thumb question.

**The two addresses are not equivalent, and the difference is the app's security model
rather than a bug.** A sandbox is not behind the tailnet front door
(``scripts/tailscale-service-host``), which is the component that proves who a remote
caller is; without it, `principals.py` correctly treats a request from the tailnet
address as an unidentified remote caller and **refuses every write with 403**. It is also
plain HTTP, so it is not a secure context. So:

* **loopback -- filing works.** The socket is loopback, so the caller is the owner. This
  is where the function half of a review happens.
* **the tailnet address -- looking works, filing is refused.** Layout, tap targets, what
  the dialog does to the bar at 375px: all real. Pressing *File it* there will show the
  front door's refusal, and that refusal is correct.

Putting a throwaway sandbox behind the real front door would mean a second Tailscale
Service, an auth key and an admin approval, which is far more than a review is worth --
so the sandbox states the split instead of hiding it.

**Both halves of the merge are seeded**, so neither has to be constructed by hand:

* **a task already filed as a quick report** -- tagged ``reported-issue``, still a
  draft, carrying the page it was noticed on. That is what the two-field path produces.
* **a task already filed with the whole specification** -- summary, intent,
  constraints, acceptance criteria, a priority. That is what the expanded path
  produces, and the pair is there so the two can be read against each other.

Then file one of each yourself, which is the actual review:

* the quick path, from a task page, so the report links the task you were reading;
* the expanded path, from the same dialog, without refiling;
* ``/tasks/new``, which is the same form as a page, for a bookmark that still works;
* ``/not-found``, which has no header -- the control is pinned there instead, because
  a finding about a page with no project has nowhere else to go.

Nothing here touches the live corpus or the 8876 dashboard. Everything lives under a
temporary directory with its own ``AGENTJOBS_HOME`` registry, deleted when this process
stops. Stop it with Ctrl-C.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional

import yaml

DEFAULT_PORT = 8921


def tailnet_address() -> Optional[str]:
    """This machine's Tailscale IPv4, or ``None`` when Tailscale is not up.

    Asked of the CLI rather than read from an interface list, because that is the one
    answer that is wrong for the right reason when Tailscale is installed but logged
    out -- an address on the adapter that no peer can route to.
    """
    for candidate in (
        Path("C:/Program Files/Tailscale/tailscale.exe"),
        Path("tailscale"),
    ):
        try:
            result = subprocess.run(
                [str(candidate), "ip", "-4"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        address = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
        if result.returncode == 0 and address:
            return address
    return None


def build_project(root: Path, *, project_id: str, name: str) -> Path:
    """One throwaway project holding one example of each half of the merge."""
    from agentjobs.manager import TaskManager
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    manager = TaskManager(sandbox_store(project_root / "tasks", project_id=project_id))

    # Something to be reading when you notice something, so a report filed from its
    # page has a task to link back to.
    manager.create_task(
        id="task-101",
        title="The queue does not explain itself",
        summary="A long enough record that the header has something to stay pinned over.",
        description=(
            "Seeded by scripts/capture_control_sandbox.py. Nothing here is real work; the "
            "record exists so there is a page to be reading when you press the control.\n\n"
            "File a report from this page and the new task should carry `task-101` as a "
            "related dependency, and this URL in its description."
        ),
        lifecycle=Lifecycle.READY,
        actor="claude",
    )

    # What the two-field path produces: tagged, draft, provenance in the description.
    manager.create_task(
        id="task-102",
        title="The log timestamps are unreadable on a phone",
        description=(
            "Every entry shows a full locale string; on a phone it wraps to three lines.\n\n"
            "---\n"
            "Reported from the AgentJobs UI by Jeff Posey, at `/p/sandbox-capture/tasks/"
            "task-101`.\nNoticed while viewing `task-101`."
        ),
        tags=["reported-issue"],
        lifecycle=Lifecycle.DRAFT,
        actor="Jeff Posey",
    )

    # What the expanded path produces from the same dialog, so the two can be read
    # against each other rather than one at a time.
    manager.create_task(
        id="task-103",
        title="Page the task list instead of loading all of it",
        summary="The list loads every task in the project, which drags past a few hundred.",
        description=(
            "Filed from the same control as task-102, with the specification opened.\n\n"
            "---\n"
            "Reported from the AgentJobs UI by Jeff Posey, at `/p/sandbox-capture/tasks`."
        ),
        spec={
            "intent": "A list that takes two seconds to paint is a list nobody reads twice.",
            "constraints": "The queue's stored order must survive paging.",
            "out_of_scope": "The filter popover.",
        },
        acceptance=[
            {"id": "ac-1", "text": "The first page paints under 200ms", "status": "pending"},
            {"id": "ac-2", "text": "Scrolling loads the next page", "status": "pending"},
        ],
        priority=Priority.HIGH,
        category="perf",
        effort="Half a day",
        tags=["reported-issue"],
        lifecycle=Lifecycle.DRAFT,
        actor="Jeff Posey",
    )

    # One task stopped on a person, so the attention badge is showing. The badge shares
    # the row with the capture trigger, and a bar reviewed without it is the narrow
    # case rather than the one the one-line fit is measured against.
    manager.handoff(
        "task-101",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.DECISION,
        ball_prompt="Decide whether the + reads as capture rather than as navigation.",
    )
    return project_root


def serve(*, port: int, remote: Optional[str]) -> None:
    """Serve loopback, and the tailnet address too when there is one.

    Two uvicorn servers over one application and one store, rather than binding
    ``0.0.0.0``. The bind surface then stays exactly the two addresses named -- a laptop
    on a cafe network does not also start answering on its Wi-Fi address because somebody
    wanted to look at a form on their phone.

    The loopback server runs in a daemon thread and the remote one on the main thread, so
    Ctrl-C reaches the one that owns the terminal and the process exits with it.
    """
    import uvicorn

    from agentjobs.api.main import app

    def server(host: str) -> uvicorn.Server:
        return uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))

    if remote is None:
        server("127.0.0.1").run()
        return

    local = server("127.0.0.1")
    thread = threading.Thread(target=local.run, daemon=True)
    thread.start()
    try:
        server(remote).run()
    finally:
        local.should_exit = True


def main() -> None:
    argv = sys.argv[1:]
    tailnet = "--tailnet" in argv
    positional = [argument for argument in argv if not argument.startswith("--")]
    port = int(positional[0]) if positional else DEFAULT_PORT

    # Loopback is always served, because it is the only address a write is accepted on.
    # The tailnet address, when asked for, is served *as well* rather than instead: a
    # review needs the phone for the layout and the desktop for the filing, and two
    # sandboxes over two corpora would be two different things to look at.
    remote = tailnet_address() if tailnet else None
    if tailnet and remote is None:
        print("[capture] Tailscale is not reporting an address; loopback only", flush=True)

    root = Path(tempfile.mkdtemp(prefix="agentjobs-capture-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    # A long-ish name on purpose: the project switcher is the widest thing in the bar
    # and the first thing squeezed, so a four-character sandbox name would review the
    # bar at a width the real one never has.
    project = build_project(root, project_id="sandbox-capture", name="Capture Control Sandbox")
    registry.add(project, project_id="sandbox-capture", name="Capture Control Sandbox")

    host = "127.0.0.1"
    base = f"http://{host}:{port}/app/p/sandbox-capture"
    print(f"[capture] capture-control sandbox at {base}", flush=True)
    print("[capture]   the + is at the top-right, beside the kebab, at every width", flush=True)
    print("[capture]   quick: title, what happened, File it -- that is the whole path", flush=True)
    print("[capture]   then press 'Add the full specification' and file one of those", flush=True)
    print(
        f"[capture]   report from a task page, which links it back: {base}/tasks/task-101",
        flush=True,
    )
    print("[capture]   task-102 is a quick report; task-103 is a specified one", flush=True)
    print(f"[capture]   the same form as a page: {base}/tasks/new", flush=True)
    print(
        f"[capture]   no header, so the control is pinned instead: "
        f"http://{host}:{port}/app/not-found",
        flush=True,
    )
    if remote is None:
        print("[capture]   --tailnet also serves the Tailscale address, for a phone", flush=True)
    else:
        print(
            f"[capture]   on a phone, to LOOK at it: "
            f"http://{remote}:{port}/app/p/sandbox-capture",
            flush=True,
        )
        print(
            "[capture]   filing is refused there and that is correct -- a sandbox is not",
            flush=True,
        )
        print(
            "[capture]   behind the tailnet front door, so the server cannot identify the",
            flush=True,
        )
        print("[capture]   caller. File from the loopback URL above.", flush=True)
    print("[capture] Ctrl-C stops it; the temporary corpus goes with it", flush=True)

    try:
        serve(port=port, remote=remote)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
