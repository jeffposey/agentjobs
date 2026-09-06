/**
 * Turning the URLs an agent wrote into prose into things a thumb can hit (task-363).
 *
 * A handoff is read on a phone. task-240's review request named three sandbox
 * addresses -- a desktop shell, a tablet one, and the specific task page one of its
 * checks needed -- and every one of them rendered as dead text, so answering the
 * review began with reading `http://127.0.0.1:8910/app/p/sandbox-shell/tasks/task-143`
 * off the screen and typing it into another app. The review waited on transcription.
 *
 * Three properties this module has to keep, each of which is a constraint rather than
 * a preference:
 *
 *   1. **It returns data, never markup.** A task record is agent-authored text
 *      arriving from outside, and the fix for a dead URL is not a licence to
 *      interpret Markdown or HTML out of it. {@link linkSegments} hands back plain
 *      strings and URLs; the caller renders them as React children, so the browser
 *      never parses agent prose as markup. task-326 ruled Markdown out and this does
 *      not reopen it.
 *   2. **The scheme allow-list is the pattern itself.** Only `http://` and `https://`
 *      can match, so there is no `javascript:` or `data:` to filter out afterwards --
 *      a filter somebody could later reorder away.
 *   3. **Nothing relative is ever produced.** The React app is mounted with
 *      `basename="/app"`, so a raw anchor to an in-app path silently drops the prefix
 *      and leaves the application for the legacy Jinja UI -- see InAppLinks.test.tsx,
 *      which exists because that shipped. Because a match must begin with a scheme and
 *      an authority, every href this produces is absolute and a plain anchor is
 *      correct for it. That is a property of the pattern, not of the caller's care.
 */

/**
 * A URL is a scheme, then a run of characters that cannot be whitespace or a quoting
 * character. Backticks and angle brackets are excluded because agents write URLs
 * inside them -- ``` `http://127.0.0.1:8910/app/` ``` was in the handoff that filed
 * this task, and a pattern that swallowed the closing backtick would produce a broken
 * link out of a correctly written one.
 */
const URL_PATTERN = /https?:\/\/[^\s<>"'`]+/gi;

/**
 * Punctuation a URL is far more likely to have been followed by than to end with.
 *
 * Trimmed from the right of a match, repeatedly: `(http://x/a).` has to lose both.
 * The known cost is a URL that genuinely ends in a bracket -- a Wikipedia
 * `..._(disambiguation)` -- which loses its last character. Accepted deliberately:
 * a sentence ending in a URL is the common case here and a parenthesised path is not,
 * and the full text is still on screen beside the link.
 */
const TRAILING_PUNCTUATION = /[.,;:!?)\]}>'"`]+$/;

/** One run of a body: either plain text, or a URL to render as an anchor. */
export type LinkSegment =
  | { kind: "text"; value: string }
  | { kind: "link"; value: string; href: string };

/** A match, once trimmed -- or null where trimming left nothing usable. */
function trim(match: string): string | null {
  const url = match.replace(TRAILING_PUNCTUATION, "");
  // `http://` on its own, or a scheme with nothing after it, is not an address.
  return /^https?:\/\/\S/i.test(url) ? url : null;
}

/**
 * Split a body into text and URL runs, in order, losing nothing.
 *
 * The segments concatenate back to the input exactly: a trailing bracket trimmed off a
 * URL rejoins the text run after it. Callers rely on that -- the panel renders these
 * inside `whitespace-pre-wrap`, so every space and newline the agent wrote has to
 * survive the round trip.
 */
export function linkSegments(text: string): Array<LinkSegment> {
  const segments: Array<LinkSegment> = [];
  let cursor = 0;
  for (const match of text.matchAll(URL_PATTERN)) {
    const raw = match[0];
    const start = match.index ?? 0;
    const url = trim(raw);
    if (url === null) continue;
    if (start > cursor) segments.push({ kind: "text", value: text.slice(cursor, start) });
    segments.push({ kind: "link", value: url, href: url });
    cursor = start + url.length;
  }
  if (cursor < text.length) segments.push({ kind: "text", value: text.slice(cursor) });
  return segments;
}

/** The distinct URLs in a body, in the order they were written. */
export function urlsIn(text: string): Array<string> {
  const seen = new Set<string>();
  for (const segment of linkSegments(text)) {
    if (segment.kind === "link") seen.add(segment.href);
  }
  return [...seen];
}
