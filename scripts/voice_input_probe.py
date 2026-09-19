"""A throwaway page that measures what dictation a browser can actually do.

Task-171 asks which of three dictation paths AgentJobs should ship, and says the answer
depends on facts about specific devices that are cheap to check and expensive to guess.
This is the instrument that checks them. It is not the shipped control, and nothing in
it is imported by the application.

    python scripts/voice_input_probe.py [port]

It serves one page on a port of its own. Open it on a device, press the buttons in
order, press **Send results**, and the device's findings land in a JSONL file this
process prints the path of at startup. Nobody screenshots anything and nobody reads a
result aloud to anyone.

## What the page measures, and why each one is a separate question

**Feature detection.** Whether `SpeechRecognition` exists at all, prefixed or not,
whether the Chrome 139 on-device API (`SpeechRecognition.available` / `.install`)
exists beside it, and whether `MediaRecorder` and `getUserMedia` exist -- the last two
being the path that works where the first does not.

**On-device availability.** `available({processLocally: true})` answers one of
`unavailable` / `downloadable` / `downloading` / `available`, which is a different
question from whether the API exists. A browser can have the API and no language pack,
and starting with `processLocally = true` and no pack fails rather than falling back.

**A recognition run, twice.** Once with `processLocally` unset -- what a naive mic
button would do -- and once with it forced true. Both record the transcript, the
per-result confidence, and any error code. The prose to read is fixed and printed on
the page, because "how good was the transcript" is only answerable against a known
target, and it is deliberately task-shaped: it contains `task-167`, `YAML` and
`worktree`, which are the words a general-purpose recogniser is most likely to lose.

**The plain textarea.** Path 1 of the task -- the operating system keyboard's own mic
key -- needs no code and no permission, and the only way to break it is to put a custom
control where the textarea was. So the page has an ordinary `<textarea>` with no
handlers on the input path, and records the `inputType` of every `beforeinput` event it
sees. That is the evidence: keyboard dictation on Android arrives as
`insertCompositionText` or `insertReplacementText`, typing arrives as `insertText`, and
a field that received neither received nothing.

## What it deliberately does not do

It sends no audio anywhere itself. The Web Speech API may, which is the point of
measuring `processLocally`; whether it did is answered by watching the browser's
connections from outside, not by asking the page. There is no cloud transcription
service in here and none is to be added -- task-171 puts that out of scope.

## Serving it over the real HTTPS origin

Secure context is the whole question on a phone: `getUserMedia` and the Web Speech API
both refuse a plain-http origin that is not localhost, so a LAN address proves nothing
about how the shipped control behaves. On this tailnet the cheapest real origin is

    tailscale serve --bg --https=8444 http://127.0.0.1:8914

which publishes it at `https://<host>.<tailnet>.ts.net:8444` with a real certificate,
to the tailnet only. Use `serve`, never `funnel`: funnel publishes to the public
internet. Undo it with `tailscale serve --https=8444 off`.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PORT = 8914

# The prose every device reads. Task-shaped on purpose: a recogniser tuned for ordinary
# English is most likely to lose the identifier, the acronym and the jargon noun, and
# those are exactly the words a bug report filed by voice would contain.
REFERENCE_TEXT = (
    "The menu shell in task one six seven needs a decision before it ships. "
    "The worktree is already bootstrapped and the YAML front matter parses, "
    "but the mic button does nothing in Firefox and I cannot tell whether the "
    "audio is leaving the machine."
)

# The tokens whose survival is the measurement. Scoring a transcript against the whole
# paragraph would mostly measure punctuation; these are what a task filed by voice has
# to keep.
CRITICAL_TOKENS = ["task", "167", "worktree", "yaml", "firefox", "mic", "audio"]

# Raw on purpose: every backslash below is a JavaScript escape, not a Python one.
# Without the r-prefix, an escaped newline inside a JS string literal becomes a real
# one and the whole page script stops parsing -- silently, because a browser reports it
# as one console line and the page still renders.
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice input probe (task-171)</title>
<style>
  :root { color-scheme: light dark; --edge: #8888; }
  * { box-sizing: border-box; }
  body { font: 16px/1.5 system-ui, sans-serif; margin: 0; padding: 16px;
         max-width: 44rem; margin-inline: auto; }
  h1 { font-size: 1.3rem; margin: 0 0 .25rem; }
  h2 { font-size: 1rem; margin: 1.5rem 0 .5rem; }
  .sub { opacity: .7; margin: 0 0 1rem; font-size: .9rem; }
  section { border: 1px solid var(--edge); border-radius: 8px; padding: 12px;
            margin-bottom: 12px; }
  button { font: inherit; padding: .7rem 1rem; border-radius: 8px;
           border: 1px solid var(--edge); background: #8881; cursor: pointer;
           min-height: 44px; }
  button[disabled] { opacity: .45; cursor: default; }
  textarea { width: 100%; font: inherit; padding: .6rem; border-radius: 8px;
             border: 1px solid var(--edge); background: transparent; color: inherit; }
  table { border-collapse: collapse; width: 100%; font-size: .9rem; }
  td, th { border-bottom: 1px solid var(--edge); text-align: left;
           padding: .3rem .4rem; vertical-align: top; }
  td:last-child { font-family: ui-monospace, monospace; word-break: break-all; }
  .say { background: #8881; padding: .6rem .8rem; border-radius: 8px;
         font-size: 1.05rem; }
  .out { font-family: ui-monospace, monospace; font-size: .85rem;
         white-space: pre-wrap; word-break: break-word; min-height: 2.5rem;
         background: #8881; padding: .5rem; border-radius: 6px; margin-top: .5rem; }
  .ok { color: #1a7f37; } .no { color: #b3261e; } .hm { color: #8a6d00; }
  #send { width: 100%; font-weight: 600; }
</style>
</head>
<body>
<h1>Voice input probe</h1>
<p class="sub">task-171 &middot; nothing here ships. Work down the page, then
<b>Send results</b>. Your answers go to the machine serving this page, not anywhere
else.</p>

<section>
  <h2>1 &middot; This device</h2>
  <label>What is this? <input id="label" style="width:100%;font:inherit;padding:.4rem"
    placeholder="e.g. Z Flip 6, Chrome"></label>
  <table id="env"></table>
</section>

<section>
  <h2>2 &middot; What the browser has</h2>
  <table id="feat"></table>
  <div id="ondevice" class="out">checking on-device availability&hellip;</div>
  <button id="install" hidden>Install the on-device language pack</button>
</section>

<section>
  <h2>3 &middot; The mic button</h2>
  <p>Read this aloud, at a normal pace:</p>
  <p class="say" id="target"></p>
  <button id="run-any">Record (however the browser likes)</button>
  <button id="run-local">Record (on-device only)</button>
  <div id="rec" class="out">not run yet</div>
</section>

<section>
  <h2>4 &middot; The keyboard's own mic</h2>
  <p>No page code touches this box. Tap into it, press the <b>microphone key on your
  keyboard</b> and read the same sentence. On a desktop use Win+H or Fn&nbsp;Fn.</p>
  <textarea id="plain" rows="4" placeholder="dictate here"></textarea>
  <div id="plainout" class="out">no input events yet</div>
</section>

<section>
  <h2>5 &middot; Anything else</h2>
  <textarea id="notes" rows="3" placeholder="What did the permission prompt say? Did
anything surprise you?"></textarea>
  <button id="send">Send results</button>
  <div id="sent" class="out">not sent</div>
</section>

<script>
const REFERENCE = %REFERENCE%;
const CRITICAL = %CRITICAL%;
document.getElementById("target").textContent = REFERENCE;

const SR = window.SpeechRecognition || window.webkitSpeechRecognition || null;
// On `window` deliberately: an automated driver reads this object out of the page,
// and a lexical `const` is not reliably reachable from one.
const results = window.results = { env: {}, features: {}, onDevice: null,
    install: null, runs: {}, plain: { events: [], text: "" }, notes: "" };

function row(table, k, v, cls) {
  const tr = table.insertRow();
  tr.insertCell().textContent = k;
  const td = tr.insertCell();
  td.textContent = String(v);
  if (cls) td.className = cls;
}
const yn = (b) => (b ? "ok" : "no");

results.env = {
  userAgent: navigator.userAgent,
  platform: navigator.platform || "",
  language: navigator.language,
  origin: location.origin,
  secureContext: window.isSecureContext,
  touch: navigator.maxTouchPoints || 0,
  screen: screen.width + "x" + screen.height,
  when: new Date().toISOString(),
};
const envT = document.getElementById("env");
for (const [k, v] of Object.entries(results.env)) {
  row(envT, k, v, k === "secureContext" ? yn(v) : null);
}

results.features = {
  SpeechRecognition: !!window.SpeechRecognition,
  webkitSpeechRecognition: !!window.webkitSpeechRecognition,
  "on-device API (available/install)": !!(SR && SR.available && SR.install),
  MediaRecorder: typeof window.MediaRecorder === "function",
  getUserMedia: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
};
const featT = document.getElementById("feat");
for (const [k, v] of Object.entries(results.features)) row(featT, k, v, yn(v));

(async () => {
  const box = document.getElementById("ondevice");
  if (!SR) { box.textContent = "no SpeechRecognition at all"; return; }
  if (!SR.available) {
    box.textContent = "no on-device API on this browser (Chrome 139+ only)";
    results.onDevice = "api-absent";
    return;
  }
  try {
    const state = await SR.available({ langs: [navigator.language || "en-US"],
                                       processLocally: true });
    results.onDevice = state;
    box.textContent = "available({processLocally:true}) -> " + state;
    if (state === "downloadable" || state === "downloading") {
      document.getElementById("install").hidden = false;
    }
  } catch (e) {
    results.onDevice = "threw: " + e;
    box.textContent = "threw: " + e;
  }
})();

document.getElementById("install").onclick = async (ev) => {
  ev.target.disabled = true;
  const box = document.getElementById("ondevice");
  box.textContent = "installing language pack, this can take a while…";
  try {
    const ok = await SR.install({ langs: [navigator.language || "en-US"],
                                  processLocally: true });
    results.install = ok;
    const after = await SR.available({ langs: [navigator.language || "en-US"],
                                       processLocally: true });
    results.onDevice = after;
    box.textContent = "install() -> " + ok + "; available() -> " + after;
  } catch (e) {
    results.install = "threw: " + e;
    box.textContent = "install threw: " + e;
  }
  ev.target.disabled = false;
};

function score(said) {
  const hay = said.toLowerCase();
  const kept = CRITICAL.filter((t) => hay.includes(t));
  return { kept, missed: CRITICAL.filter((t) => !kept.includes(t)) };
}

function record(mode) {
  const box = document.getElementById("rec");
  if (!SR) { box.textContent = "no SpeechRecognition on this browser"; return; }
  const r = new SR();
  r.lang = navigator.language || "en-US";
  r.continuous = true;
  r.interimResults = true;
  if (mode === "local") r.processLocally = true;
  const out = { mode, started: null, transcript: "", confidence: [], error: null,
                events: [], ended: null };
  let finals = "";
  const t0 = performance.now();
  const mark = (n) => out.events.push(n + "@" + Math.round(performance.now() - t0));

  r.onstart = () => { out.started = true; mark("start");
                      box.textContent = "listening (" + mode + ")…"; };
  r.onaudiostart = () => mark("audiostart");
  r.onspeechstart = () => mark("speechstart");
  r.onresult = (e) => {
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const alt = e.results[i][0];
      if (e.results[i].isFinal) {
        finals += alt.transcript;
        out.confidence.push(alt.confidence);
      } else interim += alt.transcript;
    }
    out.transcript = finals;
    box.textContent = "[" + mode + "] " + finals + (interim ? " · " + interim : "");
  };
  r.onerror = (e) => { out.error = e.error + (e.message ? " / " + e.message : "");
                       mark("error:" + e.error); };
  r.onend = () => {
    out.ended = true; mark("end");
    out.score = score(out.transcript);
    results.runs[mode] = out;
    box.textContent = "[" + mode + "] " + (out.error ? "ERROR " + out.error : "done") +
      "\n" + out.transcript + "\nkept: " + out.score.kept.join(", ") +
      "\nmissed: " + (out.score.missed.join(", ") || "nothing");
  };
  try { r.start(); } catch (e) {
    out.error = "start threw: " + e; results.runs[mode] = out;
    box.textContent = "start threw: " + e; return;
  }
  // Recognition ends per utterance on some platforms; stop after a fixed window so the
  // measurement is the same length everywhere.
  setTimeout(() => { try { r.stop(); } catch (e) { /* already ended */ } }, 20000);
}
document.getElementById("run-any").onclick = () => record("any");
document.getElementById("run-local").onclick = () => record("local");

const plain = document.getElementById("plain");
const plainout = document.getElementById("plainout");
plain.addEventListener("beforeinput", (e) => {
  results.plain.events.push(e.inputType);
  plainout.textContent = "inputTypes seen: " +
    [...new Set(results.plain.events)].join(", ");
});

document.getElementById("send").onclick = async () => {
  results.plain.text = plain.value;
  results.notes = document.getElementById("notes").value;
  results.label = document.getElementById("label").value;
  const box = document.getElementById("sent");
  try {
    const res = await fetch("/record", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(results),
    });
    box.textContent = res.ok ? "sent — thank you, you are done"
                             : "server said " + res.status;
  } catch (e) { box.textContent = "could not send: " + e; }
};
</script>
</body>
</html>
"""


def build_page() -> str:
    """The page with its reference prose substituted in as JSON literals."""
    return PAGE.replace("%REFERENCE%", json.dumps(REFERENCE_TEXT)).replace(
        "%CRITICAL%", json.dumps(CRITICAL_TOKENS)
    )


def make_app(results_path: Path) -> Any:
    """A FastAPI app serving the probe page and collecting what devices report."""
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

    app = FastAPI(title="voice input probe (task-171)")
    page = build_page()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(
            page,
            headers={
                # Same-origin is the default for this one, but the probe should not
                # depend on a default it is trying to measure.
                "Permissions-Policy": ("microphone=(self), on-device-speech-recognition=(self)"),
                "Cache-Control": "no-store",
            },
        )

    @app.post("/record")
    async def record(request: Request) -> JSONResponse:
        body = await request.json()
        body["received"] = datetime.now(timezone.utc).isoformat()
        body["remote"] = request.client.host if request.client else None
        with results_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(body) + "\n")
        label = body.get("label") or body.get("env", {}).get("userAgent", "?")
        print(f"[probe] results from {label!r}", flush=True)
        return JSONResponse({"ok": True})

    @app.get("/results", response_class=PlainTextResponse)
    async def results() -> PlainTextResponse:
        if not results_path.exists():
            return PlainTextResponse("nothing reported yet\n")
        return PlainTextResponse(results_path.read_text(encoding="utf-8"))

    return app


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    results_path = Path(
        sys.argv[2]
        if len(sys.argv) > 2
        else Path(tempfile.gettempdir()) / "voice-probe-results.jsonl"
    )

    import uvicorn

    print(f"[probe] http://127.0.0.1:{port}/", flush=True)
    print(f"[probe] results appended to {results_path}", flush=True)
    print(
        "[probe] for a phone, publish it to the tailnet with:\n"
        f"[probe]   tailscale serve --bg --https=8444 http://127.0.0.1:{port}",
        flush=True,
    )
    uvicorn.run(make_app(results_path), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
