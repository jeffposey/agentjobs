// Kind-mark auditions for the task-593 review sandbox. Injected by
// scripts/task_kind_sandbox.py into the served shell; no part of this is in the application.
//
// The shipped mark is A. B to D restyle the same `[data-kind]` element with CSS, so every
// audition is seen in the real list, header and review panel rather than in a mockup.
// The choice is remembered per browser, and `?audition=c` in the URL picks one directly.
(function () {
  "use strict";
  var KEY = "task-kind-audition";

  // Round two: nothing dashed (too hard to read), every option at full text size. The owner
  // chose C, so C is now the shipped mark and the others restyle it.
  var AUDITIONS = {
    a: {
      name: "A. Solid outline, faint tint",
      css:
        '[data-kind="design"]{box-shadow:inset 0 0 0 1px #facc15;' +
        "background:rgba(250,204,21,0.12)!important}",
    },
    b: {
      name: "B. Bold yellow word, no box",
      css:
        "[data-kind]{border-color:transparent!important;background:transparent!important;" +
        "padding-left:0!important;padding-right:0!important;font-weight:700!important}",
    },
    c: {
      name: "C. Tint only, no outline (shipped)",
      css:
        '[data-kind]{border-color:transparent!important}' +
        '[data-kind="design"]{background:rgba(250,204,21,0.22)!important}',
    },
    d: {
      name: "D. Solid yellow fill, dark text",
      css:
        '[data-kind="design"]{background:#facc15!important;color:#422006!important;' +
        "border-color:#fde047!important}",
    },
  };

  function read() {
    var fromUrl = new URLSearchParams(location.search).get("audition");
    if (fromUrl && AUDITIONS[fromUrl]) return fromUrl;
    try {
      var stored = localStorage.getItem(KEY);
      if (stored && AUDITIONS[stored]) return stored;
    } catch (e) {
      /* storage unavailable: fall back to the shipped mark */
    }
    return "c";
  }

  var style = document.createElement("style");
  var panel = document.createElement("div");

  function apply(choice) {
    style.textContent = AUDITIONS[choice].css;
    try {
      localStorage.setItem(KEY, choice);
    } catch (e) {
      /* not remembered, still applied */
    }
    var buttons = panel.querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) {
      var on = buttons[i].getAttribute("data-audition") === choice;
      buttons[i].style.background = on ? "#facc15" : "#1e293b";
      buttons[i].style.color = on ? "#422006" : "#f8fafc";
      buttons[i].style.borderColor = on ? "#fde047" : "#64748b";
    }
  }

  // Round one's bar was a small box in a corner and the owner never saw it. This one is a
  // full-width strip pinned to the bottom, with every option named on its own button, and
  // the page is padded so the strip covers nothing.
  panel.setAttribute("aria-label", "Kind mark auditions");
  panel.style.cssText =
    "position:fixed;left:0;right:0;bottom:0;z-index:9999;padding:10px 12px;" +
    "border-top:3px solid #facc15;background:#0b1120;font:14px system-ui,sans-serif;" +
    "color:#f8fafc;box-shadow:0 -6px 24px rgba(0,0,0,.6)";
  var title = document.createElement("div");
  title.style.cssText = "font-weight:700;margin-bottom:8px;font-size:15px";
  title.textContent = "AUDITION: pick a Design mark. Tap one; every screen restyles. Reply with the letter.";
  panel.appendChild(title);
  var row = document.createElement("div");
  row.style.cssText = "display:flex;flex-wrap:wrap;gap:8px";
  panel.appendChild(row);
  Object.keys(AUDITIONS).forEach(function (key) {
    var button = document.createElement("button");
    button.type = "button";
    button.setAttribute("data-audition", key);
    button.textContent = AUDITIONS[key].name;
    button.style.cssText =
      "min-height:40px;padding:6px 12px;border:2px solid #64748b;border-radius:8px;" +
      "cursor:pointer;font:600 14px system-ui,sans-serif;text-align:left";
    button.addEventListener("click", function () {
      apply(key);
    });
    row.appendChild(button);
  });

  function mount() {
    document.head.appendChild(style);
    document.body.appendChild(panel);
    // Room for the strip, so the last row of the page is never under it.
    document.body.style.paddingBottom = panel.offsetHeight + 16 + "px";
    apply(read());
  }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
