/**
 * The review sandbox's drag trace. Injected into the served shell by
 * `review_queue_sandbox.py`; no part of this is in the application.
 *
 * It exists because no automated instrument can answer whether a hand on a mouse
 * starts a drag -- Playwright drives Chromium's drag through `Input.setInterceptDrags`
 * rather than the operating system's drag loop -- so a person has to make the gesture,
 * and a person needs somewhere to leave the evidence.
 *
 * It is a separate file rather than a string inside the Python because a JavaScript
 * escape sequence sitting in a Python string literal is read by Python first: `"\n"`
 * became a real newline inside a JS string and broke the panel before the file was
 * marked raw. There is no version of this that is worth debugging twice.
 *
 * Every completed gesture is POSTed to the sandbox, so the record does not depend on
 * anyone screenshotting a panel before their next click clears it. That is not a
 * hypothetical: the first person to use this lost the trace exactly that way.
 */
(function () {
  var gestures = [];
  var lines = [];
  var missed = null;
  var panel, body;

  function render() {
    if (!panel) return;
    var blocks = gestures.slice(-4).map(function (gesture, index) {
      return "#" + (gestures.length - Math.min(gestures.length, 4) + index + 1) +
        "  " + gesture.join("\n");
    });
    if (lines.length) blocks.push("#" + (gestures.length + 1) + "  " + lines.join("\n"));
    body.textContent = blocks.length
      ? blocks.join("\n\n")
      : "No gesture yet. Grab a ⠿ handle and drag it onto another row.\n" +
        "Everything you do here is recorded on the server, so there is nothing to copy.";
  }

  function push(line) {
    if (lines[lines.length - 1] !== line) lines.push(line);
    if (lines.length > 24) lines.shift();
    render();
  }

  function describe(node) {
    if (!node || !node.tagName) return "nothing";
    var name = node.tagName.toLowerCase();
    if (node.id) name += "#" + node.id;
    var row = node.closest && node.closest("[data-task]");
    if (row) name += " (in " + row.getAttribute("data-task") + ")";
    return name;
  }

  function order() {
    var rows = document.querySelectorAll("[data-task]");
    return Array.prototype.map.call(rows, function (row) {
      return row.getAttribute("data-task");
    });
  }

  document.addEventListener("mousedown", function (event) {
    lines = [];
    var handle = event.target.closest && event.target.closest('[id^="queue-grip-"]');
    if (handle) {
      var box = handle.getBoundingClientRect();
      missed = false;
      push("PRESS  on the handle " + handle.id +
           "  (" + Math.round(box.width) + "x" + Math.round(box.height) + " px)");
    } else {
      missed = true;
      push("PRESS  NOT on a handle -- landed on " + describe(event.target));
    }
    lines.orderBefore = order().join(",");
  }, true);

  ["dragstart", "dragover", "drop", "dragend"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var row = event.target.closest && event.target.closest("[data-task]");
      push(type.toUpperCase() + (row ? "  " + row.getAttribute("data-task") : ""));
    }, true);
  });

  var pending = null;

  /**
   * Close the gesture off, summarise it, and send it.
   *
   * Driven by `dragend` as well as `mouseup` because **a browser does not deliver
   * `mouseup` to the page during a native drag** -- the gesture ends at `dragend`. The
   * first version of this listened for `mouseup` alone, so it summarised and recorded
   * every missed grab and silently dropped every drag that actually worked, which is
   * the one case the panel exists to capture. Found by driving both through the panel
   * rather than by reading it.
   */
  function finish() {
    if (pending) clearTimeout(pending);
    var before = lines.orderBefore;
    // Long enough for the optimistic reorder to land and the request to answer, so the
    // trace says whether the row moved rather than only what was attempted.
    pending = setTimeout(function () {
      pending = null;
      if (!lines.length) return;
      var started = lines.some(function (line) { return line.indexOf("DRAGSTART") === 0; });
      if (missed) {
        push("=> nothing happened, because the press missed the handle.");
      } else if (!started) {
        push("=> the browser never started a drag from the handle.");
      }
      push(before === order().join(",")
        ? "=> the order did NOT change."
        : "=> the order changed.");
      var gesture = lines.slice();
      gestures.push(gesture);
      lines = [];
      render();
      // Recorded on the server. Nobody should have to screenshot a panel in time.
      try {
        fetch("/review/trace", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ lines: gesture })
        });
      } catch (error) {
        /* the panel is still the record if this fails */
      }
    }, 1400);
  }

  document.addEventListener("mouseup", finish, true);
  document.addEventListener("dragend", finish, true);

  var fetched = window.fetch;
  window.fetch = function (input) {
    var url = typeof input === "string" ? input : (input && input.url) || "";
    if (url.indexOf("/review/trace") !== -1) return fetched.apply(this, arguments);
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
      "width:400px", "max-height:55vh", "overflow:auto", "padding:10px 12px",
      "background:#0b1220", "color:#cbd5e1", "border:1px solid #334155",
      "border-radius:8px", "font:11px/1.5 ui-monospace,Consolas,monospace",
      "box-shadow:0 8px 24px rgba(0,0,0,.5)", "white-space:pre-wrap"
    ].join(";");
    var head = document.createElement("div");
    head.style.cssText = "color:#fbbf24;font-weight:700;margin-bottom:6px";
    head.textContent = "drag trace — review sandbox only";
    body = document.createElement("div");
    panel.appendChild(head);
    panel.appendChild(body);
    document.body.appendChild(panel);
    render();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
