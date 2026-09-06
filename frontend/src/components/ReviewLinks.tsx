import type { Link, TaskRead } from "../api/types";
import { linkSegments, urlsIn } from "./linkify";

/**
 * The addresses a review needs, hoisted out of the prose and named (task-363).
 *
 * Linkifying the prompt is necessary and not sufficient. Three sandbox addresses in
 * the fourth paragraph of a handoff are tappable and still not a *target*: the first
 * thing a reviewer wants off this screen is "here is the thing to go look at".
 *
 * ## The card is the only place a review link appears
 *
 * The first pass put the addresses in both places -- linkified in the prose *and*
 * listed in the card. Jeff, reviewing it: that "wastes too much space", and the
 * sandbox state he liked was the one where the links were only in the card. He is
 * right, and on a phone the cost is literal: a 60-character address is three lines of
 * a small screen, twice. So this is a rule and not a preference:
 *
 *   **A URL that reaches the card is not rendered in the prose.** A link line is
 *   removed from the prompt entirely; a URL that is in the card by another route is
 *   left as plain text rather than as a second tap target.
 *
 * ## What a link line is, and why the agent writing the handoff decides
 *
 * A **link line** is a line whose whole content is an address, optionally preceded by
 * a name and a colon:
 *
 *     Desktop shell: http://127.0.0.1:8913/app/
 *     Task page for check 3: http://127.0.0.1:8913/app/p/sandbox-shell/tasks/task-143
 *     - Build: https://example.test/ci/runs/4821
 *     http://127.0.0.1:8913/app/
 *
 * The name is what the card row is called, which is the second half of the same
 * feedback: a row saying only `http://127.0.0.1:8913/app/p/sandbox-links/tasks/task-303`
 * does not tell a reviewer what they are about to open. Naming it is the author's job
 * rather than a heuristic's, because only the author knows that one of three
 * identical-looking ports is the tablet one. The convention is written down in
 * ALLAGENTS.md so it is a habit rather than a trick this file plays.
 *
 * A URL in the middle of a sentence is **not** a link line and stays where it was
 * written. Deleting it would leave "Open  and confirm the log expands", and promoting
 * it would put the same address on screen twice -- the thing this rule exists to stop.
 * The policy resolves the case at the source: put review links on their own lines.
 *
 * ## Where else a link comes from, and what was rejected
 *
 * `links[]` entries whose existing `rel` already means "an artefact of this piece of
 * work" -- `pr` and `build` -- join the card, titled by their own `title`.
 *
 * The rejected alternative was to add a `review` (or `sandbox`) value to
 * {@link LinkRel} and hoist links carrying it. It reads better on paper -- titled,
 * unambiguous, no parsing -- and it was rejected for three reasons.
 *
 *   1. **`links[]` is durable and a review address is not.** A sandbox lives on a
 *      throwaway port for the length of one review. Written into `links[]` it
 *      outlives the process it points at, and nothing in the system ever removes it,
 *      so the task acquires a permanent link to a dead port. The `ball_prompt` is
 *      already scoped to exactly the right lifetime: it is replaced at the next
 *      handoff, and so is the card.
 *   2. **It would fix nothing that has been seen to break.** The defect that filed
 *      this task is a handoff that already exists and already named its URLs in the
 *      prompt, where a reader would look. A scheme that works only for handoffs
 *      written after every agent learns a new field leaves that one -- and the corpus
 *      behind it -- exactly as it was.
 *   3. **A schema enum addition costs a migration, schema tolerance, the docs and a
 *      regenerated client**, and buys a title that the `Name:` convention supplies
 *      for free, in the field the author is already writing.
 */

/** `rel` values that already mean "an artefact of the work under review". */
const REVIEW_RELS = new Set(["pr", "build"]);

/** The longest thing that will be accepted as a link's name. */
const MAX_NAME = 60;

/** A row in the card. `name` is what the author called it, where they called it anything. */
export type ReviewLink = { url: string; name: string | null };

/** What the review panel renders: the prompt with its link lines taken out, and them. */
export type ReviewPrompt = { prose: string; links: Array<ReviewLink> };

/**
 * Read one line as a link line, or say it is not one.
 *
 * `null` means "leave this line in the prose". The parse goes through
 * {@link linkSegments} rather than a second URL pattern, so the scheme allow-list and
 * the trailing-punctuation trim are the same ones the prose gets -- one definition of
 * what a URL is, not two that can drift.
 */
function asLinkLine(line: string): ReviewLink | null {
  const segments = linkSegments(line);
  const urls = segments.flatMap((segment) => (segment.kind === "link" ? [segment] : []));
  // Exactly one address. None is prose; two on one line is also prose.
  const only = urls.length === 1 ? urls[0] : undefined;
  if (!only) return null;

  const index = segments.indexOf(only);
  // Anything after the address means the line is a sentence that happens to contain
  // one. Hoisting it would leave a hole in the sentence.
  const after = segments
    .slice(index + 1)
    .map((segment) => segment.value)
    .join("");
  if (after.trim()) return null;

  const before = segments
    .slice(0, index)
    .map((segment) => segment.value)
    .join("")
    .trim()
    .replace(/^[-*•]\s*/, "");
  if (!before) return { url: only.href, name: null };
  if (!before.endsWith(":")) return null;
  const name = before.slice(0, -1).trim();
  return name && name.length <= MAX_NAME ? { url: only.href, name } : null;
}

/** Blank runs collapsed to one, and none left at either end. */
function tidy(lines: Array<string>): string {
  const out: Array<string> = [];
  for (const line of lines) {
    if (!line.trim() && out.length > 0 && !out[out.length - 1]?.trim()) continue;
    out.push(line);
  }
  while (out.length && !out[0]?.trim()) out.shift();
  while (out.length && !out[out.length - 1]?.trim()) out.pop();
  return out.join("\n");
}

/**
 * Split a review's prompt into the prose to show and the links to hoist.
 *
 * Link lines are taken out in the order they were written, because that is the order
 * the agent argued in -- "open the shell, then this task page" -- and re-sorting it
 * would discard that for nothing. `links[]` rows follow, deduplicated by URL; a link
 * named in both places keeps the name the prompt gave it, that being the one written
 * for this review rather than for the task's whole life.
 */
export function reviewPromptFor(
  task: Pick<TaskRead, "ball_prompt" | "links">,
): ReviewPrompt {
  const kept: Array<string> = [];
  const links: Array<ReviewLink> = [];
  const seen = new Set<string>();
  for (const line of (task.ball_prompt ?? "").split("\n")) {
    const link = asLinkLine(line);
    if (link === null) {
      kept.push(line);
      continue;
    }
    if (!seen.has(link.url)) {
      seen.add(link.url);
      links.push(link);
    }
  }
  for (const link of (task.links ?? []) as Array<Link>) {
    if (!REVIEW_RELS.has(link.rel ?? "other")) continue;
    if (seen.has(link.url)) continue;
    seen.add(link.url);
    links.push({ url: link.url, name: link.title ?? null });
  }
  return { prose: tidy(kept), links };
}

/**
 * The addresses the prose must not render as anchors, because the card has them.
 *
 * Only reachable through `links[]`: a link line is gone from the prose entirely, so
 * this covers the one remaining way the same URL could be on screen twice.
 */
export function cardUrls(prompt: ReviewPrompt): Set<string> {
  const inProse = new Set(urlsIn(prompt.prose));
  return new Set(prompt.links.map((link) => link.url).filter((url) => inProse.has(url)));
}

/**
 * Rendered as a `nav`, because that is what it is: a list of places to go, labelled,
 * so a screen reader can jump to it rather than hearing the prompt again.
 *
 * The name leads and the address follows it in small type. Both, rather than the name
 * alone: a reviewer with three sandboxes open needs to see which port a row is, and
 * the address is the only thing that says so.
 *
 * `target="_blank"` on every row. A reviewer who follows a sandbox link and comes back
 * to a panel that has navigated away has lost the questions they were about to answer,
 * and on a phone the back gesture is not reliably cheaper than a tab switch.
 */
export function ReviewLinks({ links }: { links: Array<ReviewLink> }) {
  if (links.length === 0) return null;
  return (
    <nav
      aria-label="Links for this review"
      data-review-links={links.length}
      className="rounded-lg border border-yellow-600/40 bg-dark-bg/60 p-3"
    >
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-dark-muted">
        Links for this review
      </h3>
      <ul className="space-y-1">
        {links.map((link) => (
          <li key={link.url}>
            <a
              href={link.url}
              target="_blank"
              rel="noopener noreferrer"
              className="touch-target block leading-tight text-blue-300 underline decoration-blue-300/40 hover:text-blue-200"
            >
              {link.name && <span className="block font-semibold">{link.name}</span>}
              <span className={`block break-all ${link.name ? "text-xs text-dark-muted" : ""}`}>
                {link.url}
              </span>
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
