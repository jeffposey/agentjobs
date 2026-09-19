// The attention sandbox's control panel (task-422).
//
// Its own file rather than a string inside the Python script, for the reason
// `review_drag_trace.js` is: a JavaScript escape sequence inside a Python literal is
// read by Python first, and that silently broke a review panel once already.
//
// Nothing here is part of the application. It is injected into the served shell by
// `scripts/attention_sandbox.py` and exists so a person can make work stop on
// themselves, and clear it again, while watching the Windows taskbar and the
// bottom-right corner of their own screen -- which is the one thing no automated
// instrument in this repository can verify.
(function () {
  function projectId() {
    var match = window.location.pathname.match(/\/app\/p\/([^/]+)/);
    return match ? decodeURIComponent(match[1]) : null;
  }

  var box = document.createElement("div");
  box.id = "attention-sandbox-panel";
  box.style.cssText = [
    "position:fixed",
    "left:12px",
    "bottom:12px",
    "z-index:2147483647",
    "width:300px",
    "padding:12px",
    "border-radius:10px",
    "border:1px solid #334155",
    "background:#0b1220",
    "color:#e2e8f0",
    "font:12px/1.5 Segoe UI, system-ui, sans-serif",
    "box-shadow:0 10px 30px rgba(0,0,0,.5)",
  ].join(";");

  box.innerHTML = [
    '<div style="font-weight:700;margin-bottom:6px">Attention sandbox</div>',
    '<div id="attention-sandbox-state" style="margin-bottom:8px;color:#94a3b8">reading…</div>',
    '<button type="button" data-act="stop" style="width:100%;margin-bottom:6px;padding:6px;border-radius:6px;border:1px solid #ef4444;background:#7f1d1d;color:#fff;cursor:pointer">Stop one more task on me</button>',
    '<button type="button" data-act="clear" style="width:100%;margin-bottom:6px;padding:6px;border-radius:6px;border:1px solid #334155;background:#1e293b;color:#e2e8f0;cursor:pointer">Clear everything</button>',
    '<div style="color:#64748b">Watch the taskbar icon, the tab icon, and the bottom-right corner. A second stop while the first is unacknowledged must NOT raise a second notification.</div>',
  ].join("");

  function render(state) {
    var readout = document.getElementById("attention-sandbox-state");
    if (!readout) return;
    if (!state) {
      readout.textContent = "no project on this page";
      return;
    }
    var episode = state.episode;
    readout.innerHTML = [
      "waiting: <b>" + state.blocking + "</b>",
      "episode: " + (episode ? episode.id : "none"),
      "acknowledged: " + (episode ? String(episode.acknowledged) : "-"),
    ].join("<br>");
  }

  function refresh() {
    var id = projectId();
    if (!id) return render(null);
    fetch("/api/projects/" + encodeURIComponent(id) + "/attention", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(render)
      .catch(function () { render(null); });
  }

  box.addEventListener("click", function (event) {
    var act = event.target && event.target.getAttribute("data-act");
    if (!act) return;
    var id = projectId();
    if (!id) return;
    fetch("/review/" + act + "/" + encodeURIComponent(id), { method: "POST" })
      .then(function (response) { return response.json(); })
      .then(function (result) {
        if (result && result.message) console.log("[attention sandbox]", result.message);
        refresh();
      })
      .catch(function (error) { console.log("[attention sandbox] failed", error); });
  });

  function install() {
    if (!document.body) return window.setTimeout(install, 50);
    document.body.appendChild(box);
    refresh();
    // Faster than the app's own fifteen-second poll, so the readout is never the thing
    // a reviewer is waiting on while deciding whether the app reacted.
    window.setInterval(refresh, 3000);
  }

  install();
})();
