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
        "Hold it near the top or bottom edge and the page should scroll.\n" +
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
    lines.scrollBefore = Math.round(window.scrollY);
    overs = 0;
  }, true);

  // Whether a native drag is currently in flight. `finish` refuses to close a gesture
  // off while this is true -- see the comment on it.
  var inFlight = false;

  ["dragstart", "drop", "dragend"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      if (type === "dragstart") inFlight = true;
      if (type === "dragend") inFlight = false;
      var row = event.target.closest && event.target.closest("[data-task]");
      push(type.toUpperCase() + (row ? "  " + row.getAttribute("data-task") : ""));
    }, true);
  });

  /**
   * `dragover` is collapsed into one line rather than pushed like the others.
   *
   * A drag that crosses a screen fires hundreds of them, and one line each would push
   * the PRESS and DRAGSTART lines out of the panel's 24-line window -- which is the
   * half of the trace anybody reads. The line carries the count and where the pointer
   * currently is, because how close to the edge it is being held is the question
   * task-229's autoscroll is answered by.
   */
  var overs = 0;
  document.addEventListener("dragover", function (event) {
    overs += 1;
    var row = event.target.closest && event.target.closest("[data-task]");
    var line = "DRAGOVER x" + overs +
      "  (pointer y=" + Math.round(event.clientY) +
      " of " + window.innerHeight +
      ", page y=" + Math.round(window.scrollY) +
      (row ? ", over " + row.getAttribute("data-task") : "") + ")";
    if (lines.length && lines[lines.length - 1].indexOf("DRAGOVER") === 0) {
      lines[lines.length - 1] = line;
      render();
    } else {
      push(line);
    }
  }, true);

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
    // Never mid-drag. A long autoscrolling drag saw a `mouseup` arrive while the drag
    // was still in flight, so the gesture was summarised, cleared, and the rest of it
    // recorded as a second gesture -- one that had no PRESS and no DRAGSTART, and so
    // was reported as "the browser never started a drag from the handle" about a drag
    // that had plainly started. A review instrument that says a feature did not work
    // when it did is worse than no instrument, and that is task-225's whole incident.
    if (inFlight) return;
    if (pending) clearTimeout(pending);
    var before = lines.orderBefore;
    var scrolledFrom = lines.scrollBefore;
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
      // task-229: whether the page moved under the drag is as much of the answer as
      // whether the row did, because a drag that cannot leave the viewport can only
      // ever reach the rows that were already on it.
      var scrolledTo = Math.round(window.scrollY);
      push(typeof scrolledFrom !== "number"
        ? "=> where the page started was not recorded; it is at y=" + scrolledTo + " now."
        : scrolledFrom === scrolledTo
        ? "=> the page did NOT scroll (page y stayed at " + scrolledTo + ")."
        : "=> the page scrolled from y=" + scrolledFrom + " to y=" + scrolledTo +
          " (" + Math.abs(scrolledTo - scrolledFrom) + "px).");
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
