"""Run AgentJobs against a real temporary project for the Playwright path."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

import uvicorn
import yaml

from agentjobs.dispatch import pids
from agentjobs.project_setup import build_project_config

PORT_ENV = "AGENTJOBS_E2E_PORT"
OWNER_ENV = "AGENTJOBS_E2E_OWNER_PID"
"""The Playwright runner that started this server. See :func:`watch_owner`."""
OWNER_POLL_SECONDS = 1.0
STOP_GRACE_SECONDS = 10.0
"""How long a graceful stop may take before the watcher ends the process outright."""
CHECKOUT = Path(__file__).resolve().parents[2]
DIST_PREFIX = "agentjobs-e2e-dist-"
"""Names the per-server copy of the built frontend. See :func:`private_bundle`."""


def private_bundle_dir(port: int) -> Path:
    """Where the server on ``port`` keeps its own copy of the built frontend.

    Derived from the port rather than from the server's temporary project directory,
    because ``capture-draft.spec.ts`` has to *write* to this directory and can only
    work out where it is from something it already knows. The port is the one thing
    the two halves have always agreed on.
    """
    return Path(tempfile.gettempdir()) / f"{DIST_PREFIX}{port}"


def private_bundle(port: int) -> Optional[Path]:
    """Give this server its own copy of ``frontend_dist``, and serve that instead.

    **Two specs rewrite the bundle while they run.** ``capture-draft.spec.ts`` appends
    to ``sw.js`` and replaces ``build-info.json`` to make a running tab believe the app
    has been rebuilt under it, then puts both back. That is the only honest way to test
    a reload prompt whose whole subject is a bundle changing on disk.

    Served straight out of the checkout, as it was until task-369, that write is visible
    to **every** server -- and once the suite runs on several workers, three other
    browsers are on pages whose service workers read exactly that file and reload when
    its id moves. The neighbour does not fail for its own reason; it fails somewhere in
    the middle of an unrelated interaction, which is the least diagnosable shape a flake
    has.

    So each server copies the bundle and serves the copy, and the spec writes to the
    copy belonging to its own worker. 700 KB and nine files at the time of writing, so
    the copy costs nothing worth measuring.

    Returns the directory, or ``None`` when this checkout has no bundle at all -- a
    clone that has never run ``npm run build``, where the app already answers ``/app/``
    with the sentence that says so, and where the copy would only hide it.
    """
    from agentjobs.api import spa

    source = spa.default_frontend_dist()
    if not source.is_dir():
        return None
    target = private_bundle_dir(port)
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target)
    # Every read in `spa` goes through this function rather than through a value
    # captured at import, so replacing it reaches the mount and the per-request
    # `bundle_id` read alike.
    spa.default_frontend_dist = lambda: target
    return target


def resolve_port() -> int:
    """Take the port from the environment, and refuse to invent one.

    ``playwright.config.ts`` derives a port from the checkout's path and passes it
    here, so several worktrees can run the gate at once without contending for one
    socket. A default in this file would be a second opinion about which port is in
    play: the config would watch one address while the server bound another, and the
    run would time out looking at nothing. There is no default for that reason.
    """
    raw = os.environ.get(PORT_ENV)
    if raw is None or raw.strip() == "":
        raise SystemExit(
            f"{PORT_ENV} is not set, so this server does not know which port to bind.\n"
            f"It is normally started by Playwright, which derives the port for {CHECKOUT} "
            "and passes it in. To run it by hand, set the variable first:\n"
            f"    {PORT_ENV}=20000 poetry run python e2e/run_server.py"
        )
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"{PORT_ENV} must be a port number, got {raw!r}.") from None
    if not 1 <= port <= 65535:
        raise SystemExit(f"{PORT_ENV} must be between 1 and 65535, got {port}.")
    return port


@dataclass(frozen=True)
class Owner:
    """The runner this server belongs to: its pid, and the receipt that it is that one."""

    pid: int
    identity: Optional[str]


def read_owner() -> Optional[Owner]:
    """Take the owning runner from the environment, or ``None`` when run by hand.

    Refuses to start for an owner that is already gone, or whose pid is now held by a
    process created after this one -- a stranger, since the owner started this server
    and so existed first. Serving for an owner that cannot stop it is the orphan this
    is here to prevent. An unreadable creation time proves nothing, so it is not a
    refusal (``pids.process_created_after`` answers no on doubt).
    """
    raw = os.environ.get(OWNER_ENV)
    if raw is None or raw.strip() == "":
        return None
    try:
        pid = int(raw)
    except ValueError:
        raise SystemExit(f"{OWNER_ENV} must be a process id, got {raw!r}.") from None
    born = pids.process_started_at(os.getpid())
    if not pids.process_alive(pid) or (born is not None and pids.process_created_after(pid, born)):
        raise SystemExit(
            f"[e2e] the runner that started this server (pid {pid}) is already gone, "
            "so nothing would stop it; not serving."
        )
    return Owner(pid=pid, identity=pids.process_identity(pid))


def owner_gone(owner: Owner) -> bool:
    """Whether the owning runner has provably ended. Doubt answers no.

    A pid is recycled in seconds on this machine (task-505), so liveness of the number
    alone would keep an orphan serving for whichever stranger inherits it. The identity
    recorded at startup settles that: a live pid with a different creation time is not
    the owner. A creation time that cannot be read is not evidence either way, and the
    cost of a wrong "gone" is a red e2e stage, so it keeps serving.
    """
    if not pids.process_alive(owner.pid):
        return True
    if owner.identity is None:
        return False
    now = pids.process_identity(owner.pid)
    return now is not None and now != owner.identity


def watch_owner(
    owner: Owner,
    server: uvicorn.Server,
    *,
    poll: float = OWNER_POLL_SECONDS,
    grace: float = STOP_GRACE_SECONDS,
) -> threading.Thread:
    """Stop serving when the runner that started this server is gone (task-515).

    Playwright stops its ``webServer`` processes when a run ends, failed or not -- but
    only if the runner itself gets to the end. Killed from outside, it stops nothing,
    and this server went on holding the checkout's port: once for fifteen minutes on
    2026-09-21, turning every later e2e run in that checkout red with a message about
    the port rather than the branch. Its parent is no help on Windows, because the
    ``poetry run`` in between outlives the runner.

    So the server asks after the runner itself. The stop is uvicorn's own, so the
    temporary project and the private bundle are cleaned up the usual way; if that has
    not finished within ``grace`` seconds the process ends outright, because a server
    nobody owns holding the port is the one outcome this exists to rule out. Nothing is
    killed but this process.
    """

    def run() -> None:
        while not server.should_exit:
            if owner_gone(owner):
                server.should_exit = True
                # Said after the stop, and allowed to fail: this process's stdout is a
                # pipe to the runner that just died, and a write to it raises. Printed
                # first, that exception ended this thread before the stop -- observed on
                # the first version of this watcher, which left all four servers up.
                try:
                    print(
                        f"[e2e] runner {owner.pid} is gone; stopping so the port is freed",
                        flush=True,
                    )
                except (OSError, ValueError):
                    pass
                time.sleep(grace)
                os._exit(3)
            time.sleep(poll)

    thread = threading.Thread(target=run, name="e2e-owner-watch", daemon=True)
    thread.start()
    return thread


def write_dispatch_config(home: Path) -> None:
    """Define one runner this machine will admit to, and enable nothing.

    The master switch is on and the project is deliberately *off*, so the browser path
    starts where a real machine starts: dispatch installed, this project not yet
    trusted with it. Turning it on is the first thing the spec does, which is the only
    way to prove the toggle actually writes this file.

    The runner sleeps rather than exiting, so a run is still live when the page renders
    and the cancel button has something real to stop. ``require_clean_tree`` is off
    because the temporary project is not a git repository at all -- the clean-tree gate
    has its own tests, and standing up a repo here would only test git.

    ``actor`` is not decoration. A runner is named for the invocation and writes as an
    identity from the project's ``actors:``, and task-159's guard refuses a dispatch
    whose runner would act as an id the project does not configure. Without it this
    runner would act as ``e2e-sleeper``, the dispatch is refused before any run exists,
    and the spec's run list is empty rather than wrong -- which is exactly how this
    harness broke. ``claude`` is one of the actors ``build_project_config`` writes.
    """
    home.mkdir(parents=True, exist_ok=True)
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "runners": {
                    "e2e-sleeper": {
                        "argv": [sys.executable, "-c", "import time; time.sleep(120)"],
                        "actor": "claude",
                    },
                },
                # Two slots, so the Dashboard's slot board has a cell to spare while a
                # run holds one -- which is the only arrangement in which a browser can
                # see both halves of the board at once (task-092). One would be a board
                # that is either all run or all queue and never both.
                "limits": {"max_concurrent_runs": 2},
                "projects": {"_local": {"enabled": False, "require_clean_tree": False}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


DRAFT = {
    "summary": "The task list pages badly once a project holds a few hundred tasks.",
    "intent": "Filing more work should not make the backlog harder to read.",
    "description": "## What to do\n\nPage the listing endpoint and the list that renders it.",
    "constraints": "No schema change.",
    "out_of_scope": "Search, which pages separately.",
    "acceptance": [
        "A project with 500 tasks renders its first page without loading all of them.",
        "Paging back and forth does not change the order.",
    ],
}
"""The one draft the stub provider answers with.

Canned on purpose. What the browser path proves is the *plumbing* -- form to route to
provider to parse to form fields to a filed record -- and a canned answer is what lets it
prove that deterministically, on a machine with no credential, spending nothing. It says
nothing about whether a real model writes a good spec, which is a different question and
needs a real one; see `docs/model-access-design.md` section 9.
"""


class StubProvider(BaseHTTPRequestHandler):
    """A few lines of the Messages API: enough for one drafting call to succeed."""

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        if self.headers.get("x-api-key") is None:
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps({"content": [{"type": "text", "text": json.dumps(DRAFT)}]})
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args: object) -> None:
        """Quiet: this is a fixture, and its access log is noise in a gate."""


def start_stub_provider() -> str:
    """Serve the stub on an ephemeral port and return its base URL.

    Port zero rather than a number derived from the server's own: several worktrees gate
    at once, and a derived offset could land on a sibling checkout's chosen port, which
    is the collision `playwright.config.ts` went to some trouble to avoid.
    """
    server = HTTPServer(("127.0.0.1", 0), StubProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = int(server.server_address[1])
    return f"http://127.0.0.1:{port}"


def write_model_config(home: Path, base_url: str) -> None:
    """Point the drafting feature at the stub, with a credential that is not one.

    The key is a literal placeholder and the endpoint is on loopback, so the browser
    path exercises the configured branch -- which is otherwise unreachable on a machine
    with no model -- without a credential existing anywhere and without a request leaving
    this machine.
    """
    home.mkdir(parents=True, exist_ok=True)
    (home / "model.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "base_url": base_url,
                "model": "e2e-stub-model",
                "api_key": "e2e-not-a-real-key",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def main() -> None:
    """Pin a fresh project before importing the app, then serve until Playwright exits."""
    port = resolve_port()
    owner = read_owner()
    # Named, not just bound: a failure here is read alongside Playwright's own line
    # about the same port, and between them they say which checkout owns it.
    print(f"[e2e] serving {CHECKOUT} on http://127.0.0.1:{port}", flush=True)
    # Cleanup errors ignored: a graceful stop reaches this exit with the app's SQLite
    # files still open, which Windows will not delete, and a traceback there would
    # report a clean stop as a failure. Until task-515 nothing ever stopped this server
    # gracefully, so the directory was always left behind anyway.
    with TemporaryDirectory(prefix="agentjobs-e2e-", ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        os.environ["AGENTJOBS_PROJECT_ROOT"] = str(root)
        os.environ["AGENTJOBS_HOME"] = str(root / ".agentjobs-home")
        # A human actor, because every action the UI attributes to a person is
        # refused when the project has none -- so without one, the browser path
        # could only ever exercise the pages that ask nobody to act.
        config_path = root / ".agentjobs" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.safe_dump(
                build_project_config(project_name="End-to-end project", user="E2E Human"),
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        write_dispatch_config(root / ".agentjobs-home")
        write_model_config(root / ".agentjobs-home", start_stub_provider())
        bundle = private_bundle(port)
        # Named so a spec that cannot find the directory, and a reader of a red run,
        # both learn where this server's bundle actually is.
        print(f"[e2e] bundle for {port}: {bundle or 'none built in this checkout'}", flush=True)
        server = uvicorn.Server(
            uvicorn.Config(
                "agentjobs.api.main:app",
                host="127.0.0.1",
                port=port,
                log_level="warning",
            )
        )
        if owner is not None:
            watch_owner(owner, server)
        try:
            server.run()
        finally:
            shutil.rmtree(private_bundle_dir(port), ignore_errors=True)


if __name__ == "__main__":
    main()
