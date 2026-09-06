import { describe, expect, it } from "vitest";

import { linkSegments, urlsIn } from "./linkify";

/**
 * Asserting on what the segments *are*, not on any markup around them (task-363).
 *
 * The rendering half is asserted where it renders -- ReviewLinks.test.tsx and the panel
 * tests read hrefs off the DOM. This file is the text half: what counts as a URL, what
 * the trimming gives up, and the two properties that make a raw anchor safe.
 */

/** The segments joined back up. Every case below has to satisfy this. */
function rejoined(text: string): string {
  return linkSegments(text)
    .map((segment) => segment.value)
    .join("");
}

describe("linkSegments", () => {
  it("finds a bare URL in a sentence and leaves the prose either side of it", () => {
    const segments = linkSegments("Open http://127.0.0.1:8910/app/ and look.");

    expect(segments).toEqual([
      { kind: "text", value: "Open " },
      {
        kind: "link",
        value: "http://127.0.0.1:8910/app/",
        href: "http://127.0.0.1:8910/app/",
      },
      { kind: "text", value: " and look." },
    ]);
  });

  it("keeps the sentence's full stop out of the address", () => {
    // A URL at the end of a sentence is the common case in a handoff, and a link that
    // 404s on a trailing dot is not a link.
    expect(urlsIn("Go to https://example.com/a/b.")).toEqual(["https://example.com/a/b"]);
    expect(rejoined("Go to https://example.com/a/b.")).toBe("Go to https://example.com/a/b.");
  });

  it("does not swallow the backticks an agent wrapped the URL in", () => {
    const text = "the shell (`http://127.0.0.1:8910/app/`)";

    expect(urlsIn(text)).toEqual(["http://127.0.0.1:8910/app/"]);
    expect(rejoined(text)).toBe(text);
  });

  it("preserves whitespace exactly, because the panel renders pre-wrapped", () => {
    const text = "Desktop:  http://127.0.0.1:8910/app/\nTablet:   https://example.test/app/\n";

    expect(rejoined(text)).toBe(text);
  });

  it("linkifies nothing but http and https", () => {
    // The allow-list is the pattern, so there is no ordering of filters to get wrong.
    const text =
      "javascript:alert(1) data:text/html;base64,AAA file:///etc/passwd ftp://example.test/x";

    expect(urlsIn(text)).toEqual([]);
    expect(linkSegments(text)).toEqual([{ kind: "text", value: text }]);
  });

  it("never produces a relative href", () => {
    // A raw anchor to an in-app path drops the router basename and leaves the React
    // app for the legacy UI -- see InAppLinks.test.tsx. Nothing here can produce one:
    // a match has to start with a scheme and an authority.
    const text = "Look at /app/p/agentjobs/tasks/task-240 and ./relative and //example.test/x";

    expect(urlsIn(text)).toEqual([]);
  });

  it("treats an absolute address at this app's own origin as an ordinary link", () => {
    // Absolute, so the browser keeps the /app prefix that is written into it. This is
    // the shape a sandbox address takes and a plain anchor is correct for it.
    expect(urlsIn("http://127.0.0.1:8910/app/p/sandbox-shell/tasks/task-143")).toEqual([
      "http://127.0.0.1:8910/app/p/sandbox-shell/tasks/task-143",
    ]);
  });

  it("renders markup an agent wrote as text, not as markup", () => {
    // task-326 ruled out interpreting a task record as Markdown, and linkifying is not
    // a way back in. An angle bracket ends a match rather than starting a tag, and
    // everything that is not a URL comes back as a plain string for React to escape.
    const text = "<script>alert(1)</script> **bold** [x](http://example.test/y)";
    const segments = linkSegments(text);

    expect(segments.filter((segment) => segment.kind === "link")).toEqual([
      { kind: "link", value: "http://example.test/y", href: "http://example.test/y" },
    ]);
    expect(rejoined(text)).toBe(text);
  });

  it("returns no segments at all for an empty body", () => {
    expect(linkSegments("")).toEqual([]);
  });
});

describe("urlsIn", () => {
  it("keeps the order they were written in and drops repeats", () => {
    const text = [
      "Desktop shell: http://127.0.0.1:8910/app/",
      "Tablet: http://127.0.0.1:8910/app/?w=1024",
      "The task page: http://127.0.0.1:8910/app/p/sandbox-shell/tasks/task-143",
      "(again: http://127.0.0.1:8910/app/)",
    ].join("\n");

    expect(urlsIn(text)).toEqual([
      "http://127.0.0.1:8910/app/",
      "http://127.0.0.1:8910/app/?w=1024",
      "http://127.0.0.1:8910/app/p/sandbox-shell/tasks/task-143",
    ]);
  });
});
