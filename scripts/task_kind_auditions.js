// Kind-mark auditions for the task-593 review sandbox. Injected by
// scripts/task_kind_sandbox.py into the served shell; no part of this is in the application.
//
// The shipped mark is A. B to D restyle the same `[data-kind]` element with CSS, so every
// audition is seen in the real list, header and review panel rather than in a mockup.
// The choice is remembered per browser, and `?audition=c` in the URL picks one directly.
(function () {
  "use strict";
  var KEY = "task-kind-audition";

  function glyph(inner) {
    var svg =
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="black" ' +
      'stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round">' + inner + "</svg>";
    return 'url("data:image/svg+xml,' + encodeURIComponent(svg) + '")';
  }
  var COMPASS = glyph(
    '<path d="m12.99 6.74 1.93 3.44"/><path d="M19.136 12a10 10 0 0 1-14.271 0"/>' +
      '<path d="m21 21-2.16-3.84"/><path d="m3 21 8.02-14.26"/><circle cx="12" cy="5" r="2"/>'
  );
  var CODE = glyph('<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>');

  var AUDITIONS = {
    a: { name: "A. Dashed pill, word (shipped)", css: "" },
    b: {
      name: "B. Word only, no outline",
      css:
        '[data-kind]{border-color:transparent!important;padding-left:0!important;' +
        'padding-right:0!important;font-weight:600!important}',
    },
    c: {
      name: "C. Icon only, in a dashed circle",
      css:
        "[data-kind]{font-size:0!important;width:1.5rem;height:1.5rem;padding:0!important;" +
        "justify-content:center}" +
        '[data-kind]::before{content:"";display:block;width:0.95rem;height:0.95rem;' +
        "background-color:currentColor;-webkit-mask:var(--kind-glyph) center/contain no-repeat;" +
        "mask:var(--kind-glyph) center/contain no-repeat}" +
        '[data-kind="design"]{--kind-glyph:' + COMPASS + "}" +
        '[data-kind="implementation"]{--kind-glyph:' + CODE + "}",
    },
    d: {
      name: "D. Dashed pill, word, yellow tint",
      css: '[data-kind="design"]{background:rgba(250,204,21,0.14)!important;font-weight:600!important}',
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
    return "a";
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
      buttons[i].style.background = on ? "#facc15" : "transparent";
      buttons[i].style.color = on ? "#422006" : "#e2e8f0";
    }
    label.textContent = AUDITIONS[choice].name;
  }

  panel.setAttribute("aria-label", "Kind mark auditions");
  panel.style.cssText =
    "position:fixed;left:8px;bottom:8px;z-index:9999;display:flex;align-items:center;gap:4px;" +
    "flex-wrap:wrap;max-width:calc(100vw - 16px);padding:6px 8px;border:1px solid #475569;" +
    "border-radius:8px;background:#0f172a;font:12px system-ui,sans-serif;color:#e2e8f0;" +
    "box-shadow:0 4px 16px rgba(0,0,0,.5)";
  var title = document.createElement("strong");
  title.textContent = "Kind mark:";
  panel.appendChild(title);
  Object.keys(AUDITIONS).forEach(function (key) {
    var button = document.createElement("button");
    button.type = "button";
    button.setAttribute("data-audition", key);
    button.title = AUDITIONS[key].name;
    button.textContent = key.toUpperCase();
    button.style.cssText =
      "min-width:28px;height:28px;border:1px solid #64748b;border-radius:6px;cursor:pointer;font-weight:700";
    button.addEventListener("click", function () {
      apply(key);
    });
    panel.appendChild(button);
  });
  var label = document.createElement("span");
  label.style.cssText = "margin-left:4px;opacity:.85";
  panel.appendChild(label);

  function mount() {
    document.head.appendChild(style);
    document.body.appendChild(panel);
    apply(read());
  }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
