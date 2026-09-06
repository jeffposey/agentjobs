import type { Link, TaskRead } from "../api/types";
import { urlsIn } from "./linkify";

/**
 * The addresses a review needs, hoisted to the top of the panel (task-363).
 *
 * Linkifying the prompt is necessary and not sufficient. Three sandbox addresses in
 * the fourth paragraph of a handoff are tappable and still not a *target*: the first
 * thing a reviewer wants off this screen is "here is the thing to go look at", and a
 * review that begins by re-reading the prose to find the URL again has already spent
 * the attention the panel exists to save.
 *
 * ## Where a link in this card comes from, and what was rejected
 *
 * **It is read out of the current `ball_prompt`**, plus any `links[]` entry whose
 * existing `rel` already means "an artefact of this piece of work" -- `pr` and
 * `build`. Nothing new is asked of the agent writing the handoff.
 *
 * The rejected alternative was the explicit one: add a `review` (or `sandbox`) value
 * to {@link LinkRel} and hoist links carrying it. It reads better on paper -- titled,
 * unambiguous, no guessing -- and it was rejected for three reasons.
 *
 *   1. **`links[]` is durable and a review address is not.** A sandbox lives on a
 *      throwaway port for the length of one review. Written into `links[]` it
 *      outlives the process it points at, and nothing in the system ever removes it,
 *      so the task acquires a permanent link to a dead port. The `ball_prompt` is
 *      already scoped to exactly the right lifetime: it is replaced at the next
 *      handoff, and so is the card.
 *   2. **It would fix nothing that has been seen to break.** The defect that filed
 *      this task is a handoff that already exists and already named its URLs, in the
 *      prompt, where a reader would look. A scheme that works only for handoffs
 *      written after every agent learns a new field leaves that one -- and the corpus
 *      behind it -- exactly as it was.
 *   3. **Its population depends on agent discipline**, which is the least reliable
 *      mechanism available here, for a benefit (a title) the URL itself mostly
 *      carries. A schema enum addition also costs a migration, schema tolerance, the
 *      docs and a regenerated client.
 *
 * The cost of reading the prompt instead, stated so nobody rediscovers it as a bug: a
 * URL mentioned in passing is promoted alongside one meant to be visited, and neither
 * gets a title. That is a card with one row too many, which a reviewer resolves in a
 * glance; the rejected alternative's failure mode is an empty card, which they cannot.
 */

/** `rel` values that already mean "an artefact of the work under review". */
const REVIEW_RELS = new Set(["pr", "build"]);

/** A row in the card. `title` is present only where a `links[]` entry supplied one. */
export type ReviewLink = { url: string; title: string | null };

/**
 * The links this review needs, prompt first, deduplicated by URL.
 *
 * Prompt order is preserved because it is the order the agent argued in -- "open the
 * shell, then this task page" -- and re-sorting it would discard that for nothing.
 */
export function reviewLinksFor(task: Pick<TaskRead, "ball_prompt" | "links">): Array<ReviewLink> {
  const rows: Array<ReviewLink> = urlsIn(task.ball_prompt ?? "").map((url) => ({
    url,
    title: null,
  }));
  const seen = new Set(rows.map((row) => row.url));
  for (const link of (task.links ?? []) as Array<Link>) {
    if (!REVIEW_RELS.has(link.rel ?? "other")) continue;
    if (seen.has(link.url)) continue;
    seen.add(link.url);
    rows.push({ url: link.url, title: link.title ?? null });
  }
  return rows;
}

/**
 * Rendered as a `nav`, because that is what it is: a list of places to go, labelled,
 * so a screen reader can jump to it rather than hearing the prompt again.
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
              className="touch-target block break-all text-blue-300 underline hover:text-blue-200"
            >
              {link.title ?? link.url}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
