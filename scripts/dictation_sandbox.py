"""Stand up the dictation control on its own port, with throwaway data.

task-172 puts a microphone beside every free-text field the capture form has. The only
instrument that can settle whether it works is a person speaking into a device, so this
sandbox exists to be that person's page: the fields are real, the data is disposable,
and both halves of the change are on screen at once.

**Separate from ``capture_control_sandbox.py`` for one reason**, and it is not that the
two review different screens -- they review the same form. The speech API and the
microphone both require a secure context, and that sandbox's ``--tailnet`` mode is plain
HTTP, so dictation cannot run on it from a phone at all. This one is served through an
HTTPS proxy instead, and carries the two shims below, which have no business in a
sandbox about gesture and proportion.

    python scripts/dictation_sandbox.py [port] [host]

Stop it with Ctrl-C. Its data lives under a temporary directory deleted with the
process, and its ``AGENTJOBS_HOME`` is its own, so the 8876 dashboard and its registry
are not involved at all. Press anything, including the destructive controls.

**Both states are seeded, in one browser.** The change behaves differently depending on
whether the browser has a speech recogniser at all -- Firefox 153 has neither spelling
of the constructor, so the microphone is absent there and one sentence points at the
keyboard's own microphone key instead. Nobody should have to install a second browser to
see that half, so this sandbox serves it on a query string:

    /app/                     the browser as it is -- microphones where it can
    /app/?dictation=off       both constructors deleted before the app boots
    /app/?dictation=fake      a scripted recogniser that "hears" a fixed paragraph

Both are sandbox-only shims injected into the served HTML. No part of either is in the
application; the application only ever feature-detects what the browser really has.

``fake`` is for looking at the interaction without a microphone -- it speaks a
task-shaped paragraph in three chunks, ends its session the way Android does at seven
seconds, and gets restarted and stitched exactly as the real one is. It is the only way
to see the stitching on a desktop with no usable input device, which is the machine this
was built on. It proves the wiring and proves nothing about audio.

**To use this from a phone**, which is the device the whole epic is about, put a proxy
in front rather than widening the bind -- and it has to be HTTPS, because the speech API
and the microphone both require a secure context and a plain-http LAN address is not
one:

    tailscale serve --bg --https=8445 http://127.0.0.1:8918
    python scripts/dictation_sandbox.py 8918

Turn it off afterwards with ``tailscale serve --https=8445 off``; ``--bg`` persists
across reboots until you do.

**On the phone you can dictate but not file, and that is the security model rather
than a bug.** The proxy gives the page a secure context, which is all the speech API
and the microphone need, so every question about dictation is answerable there. It does
not give the request an identity: ``tailscale serve`` forwards with the tailnet source
address, so the socket is not loopback and `principals.py` correctly refuses every write
with ``no_proven_identity`` -- measured against this sandbox, not inferred. Filing is
what the loopback address is for. Putting a throwaway sandbox behind the real tailnet
front door would mean a second Tailscale Service, an auth key and an admin approval,
which `capture_control_sandbox.py` already declined for the same reason.

What to look for, since "a button appeared" is not the property under review:

  * **Dictate into a box you have already typed half a sentence into.** The words must
    land where the caret is and nothing you typed may disappear. That is the failure
    worth catching: a control that replaces the field's contents is worse than no
    control.
  * **The keyboard's own microphone must still work in the same box.** Tap into the
    field, press the microphone key on the keyboard, and speak. If that has stopped
    working, the change has broken the one dictation path that works in every browser
    and it should not merge.
  * **Watch the line under the box while you speak.** What has been heard but not yet
    committed shows there, in italics, and is not in the field yet.
  * **Speak for longer than about ten seconds.** Android's recogniser gives up
    mid-paragraph; the control restarts it and stitches, so the paragraph should keep
    going rather than stopping silently.
  * **Refuse the microphone permission once**, in the browser's prompt. The control must
    say so, in the form, and point at the keyboard microphone -- never fail silently.
  * **Load the page and touch nothing.** No permission prompt may appear. It is asked
    for on the press and only on the press.
  * **Open ``?dictation=off``.** Every field still works and still takes the keyboard's
    microphone; there is no dead button, and one sentence says what to use instead.
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

#: A port of this sandbox's own. Never the dashboard's: a second server on the usual
#: port silently serves stale code from a process nobody restarts, which is the failure
#: the whole convention exists to avoid.
DEFAULT_PORT = 8918

#: Loopback. A sandbox carries no authentication, and a request arriving from another
#: device resolves to no proven identity, so a reviewer on a phone would get a page they
#: can read and no verb they can press. Put a proxy in front instead -- and see the
#: module docstring for why it has to be HTTPS for this particular change.
DEFAULT_HOST = "127.0.0.1"

#: The sandbox-only shim behind ``?dictation=off``.
#:
#: It deletes both spellings of the constructor before the application's bundle runs, so
#: the page boots into exactly the state Firefox is in. Doing it here rather than with a
#: switch inside the control is the point: the application must have no way to pretend,
#: or what gets reviewed is the pretence.
DISABLE_SHIM = """
<script>
  (function () {
    var mode = new URLSearchParams(location.search).get("dictation");
    function flag(text) {
      document.addEventListener("DOMContentLoaded", function () {
        var bar = document.createElement("p");
        bar.textContent = text;
        bar.style.cssText =
          "margin:0;padding:6px 12px;background:#7c2d12;color:#fed7aa;font:600 12px system-ui;";
        document.body.prepend(bar);
      });
    }

    if (mode === "off") {
      delete window.SpeechRecognition;
      delete window.webkitSpeechRecognition;
      Object.defineProperty(window, "SpeechRecognition", { value: undefined });
      Object.defineProperty(window, "webkitSpeechRecognition", { value: undefined });
      flag("Sandbox: this page is pretending the browser has no speech recogniser.");
      return;
    }

    if (mode !== "fake") return;

    // A recogniser that never touches the microphone. Three chunks, then the session
    // ends on its own the way Android's does -- so pressing Dictate once and waiting
    // shows the restart-and-stitch behaviour rather than one tidy sentence.
    var CHUNKS = [
      "the task list filters match nothing",
      "after you reorder the high band",
      "which is task 167 and not task 172",
    ];
    function Fake() {
      this.lang = "";
      this.continuous = false;
      this.interimResults = false;
      this.onresult = null;
      this.onerror = null;
      this.onend = null;
      this.onstart = null;
      this._timers = [];
      this._chunk = 0;
    }
    Fake.available = function () {
      return Promise.resolve("unavailable");
    };
    Fake.prototype._emit = function (text, final) {
      if (!this.onresult) return;
      var results = [{ 0: { transcript: text }, length: 1, isFinal: final }];
      results.length = 1;
      this.onresult({ results: results });
    };
    Fake.prototype.start = function () {
      var self = this;
      var phrase = CHUNKS[this._chunk % CHUNKS.length];
      this._chunk += 1;
      if (this.onstart) this.onstart();
      var words = phrase.split(" ");
      words.forEach(function (_, index) {
        self._timers.push(
          setTimeout(function () {
            self._emit(words.slice(0, index + 1).join(" "), false);
          }, 300 * (index + 1)),
        );
      });
      this._timers.push(
        setTimeout(function () {
          self._emit(phrase, true);
        }, 300 * (words.length + 1)),
      );
      // The seven-second cut-off, scaled down so a reviewer does not have to wait.
      this._timers.push(
        setTimeout(function () {
          self.stop();
        }, 300 * (words.length + 3)),
      );
    };
    Fake.prototype.stop = function () {
      this._timers.forEach(clearTimeout);
      this._timers = [];
      if (this.onend) this.onend();
    };
    Fake.prototype.abort = Fake.prototype.stop;

    Object.defineProperty(window, "SpeechRecognition", { value: Fake, configurable: true });
    Object.defineProperty(window, "webkitSpeechRecognition", { value: Fake, configurable: true });
    flag("Sandbox: a scripted recogniser is standing in for the microphone.");
  })();
</script>
"""


class InjectDictationShim(BaseHTTPMiddleware):
    """Put the shim into the application shell, and nowhere near the application.

    The shell is served off disk as a file response, so the body is consumed and
    rewritten here rather than by changing anything the real server would serve.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if "text/html" not in response.headers.get("content-type", ""):
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        text = body.decode("utf-8")
        if "</head>" in text:
            text = text.replace("</head>", DISABLE_SHIM + "</head>", 1)
        headers = dict(response.headers)
        headers.pop("content-length", None)
        # Never let a service worker hand back a shell without the shim.
        headers["cache-control"] = "no-store"
        return Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )


def seed(manager: Any) -> None:
    """Enough of a backlog that the surfaces under review have somewhere to file into.

    Deliberately small. The screens this sandbox is for are the two capture forms, not
    the list, and a long list is only something to scroll past on a phone.
    """
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority

    manager.create_task(
        id="task-001",
        title="Something to file an issue against",
        summary="A task to open, so Report issue has a page with a project on it.",
        description=(
            "Open this task and press Report issue from here. The reporter records the "
            "page you were on, so filing from a task is the realistic gesture."
        ),
        priority=Priority.MEDIUM,
        lifecycle=Lifecycle.READY,
        actor="claude",
    )
    manager.create_task(
        id="task-002",
        title="A task already under review, so the page is not empty",
        summary="Somewhere for the review panel to render while you are looking around.",
        description="Nothing here is real work. Press anything.",
        priority=Priority.HIGH,
        lifecycle=Lifecycle.READY,
        actor="claude",
    )
    manager.claim_task("task-002", agent="claude")
    manager.handoff(
        "task-002",
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt="Seeded so this sandbox has a task in review. There is nothing to review.",
    )


def build(root: Path, *, project_id: str, name: str) -> Path:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    seed(TaskManager(sandbox_store(project_root / "tasks", project_id=project_id)))
    return project_root


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    host = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_HOST
    root = Path(tempfile.mkdtemp(prefix="agentjobs-dictation-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    project_id, name = "sandbox-dictation", "Sandbox: dictation"
    ProjectRegistry(home).add(
        build(root, project_id=project_id, name=name), project_id=project_id, name=name
    )

    import uvicorn

    from agentjobs.api.main import app

    app.add_middleware(InjectDictationShim)

    base = f"http://{host}:{port}"
    print(f"[review] dictation sandbox at {base}/app/", flush=True)
    print("[review] the two ways into the form the microphone is on:", flush=True)
    print(f"[review]   as a page     {base}/app/p/{project_id}/tasks/new", flush=True)
    print(f"[review]   as a dialog   {base}/app/ - the + beside the header's kebab", flush=True)
    print(f"[review] the no-recogniser half: {base}/app/?dictation=off", flush=True)
    print(f"[review] a scripted recogniser:   {base}/app/?dictation=fake", flush=True)
    print("[review] on a phone it must be HTTPS -- see the docstring for the proxy.", flush=True)
    print(f"[review] throwaway data under {root}", flush=True)
    print("[review] stop with Ctrl-C; the data is deleted with the process.", flush=True)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
