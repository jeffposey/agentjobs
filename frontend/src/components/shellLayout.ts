import { useSyncExternalStore } from "react";

/**
 * Which shell the Tasks surface renders in, and the rule that decides it.
 *
 * The rule is task-235's, from Jeff on 2026-09-06 and binding on this epic: **a phone
 * gets one thing at a time, a tablet or a desktop gets the list and the record
 * together.** It is deliberately not a width cut. A phone held in landscape is 956px
 * wide and an iPad in portrait is 744px, so any `min-width` rule hands the two-region
 * shell to the phone and denies it to the tablet -- exactly backwards. The axis that
 * separates the classes in *both* orientations is the short side, and there is a wide
 * empty gap between the largest phone (440) and the smallest tablet (744) to put a
 * threshold in.
 *
 * So both dimensions are tested, which makes the answer orientation-independent by
 * construction and needs no `orientation:` query. A desktop window dragged to 500px
 * tall is treated as a phone; that is acceptable, because from task-238 the reader can
 * open the list themselves and their choice is remembered.
 *
 * This is a third breakpoint on purpose. `min-[820px]:` in `TaskList` and
 * `min-[1140px]:` in `PrimaryNav` both ask "does this content fit on one line"; this
 * one asks "is this a phone", which is a different question with a different answer.
 * Do not consolidate them.
 */
export const WIDE_SHELL_QUERY = "(min-width: 600px) and (min-height: 600px)";

/**
 * True when `window.matchMedia` is missing rather than false.
 *
 * jsdom ships no `matchMedia`, and the shell a test sees with no viewport of its own
 * should be the one a desktop browser sees. `src/test/setup.ts` installs a stub driven
 * by `window.innerWidth`/`innerHeight` so a test that wants the phone shell can ask for
 * it by setting a phone-sized viewport, which is what the test for that case does.
 */
function matches(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return true;
  return window.matchMedia(WIDE_SHELL_QUERY).matches;
}

function subscribe(onChange: () => void): () => void {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return () => {};
  const query = window.matchMedia(WIDE_SHELL_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

/**
 * Whether this viewport gets the two-region Tasks surface.
 *
 * `useSyncExternalStore` rather than an effect and a state: the first paint has to be
 * right. Reading the query in an effect renders the wrong shell once and corrects it,
 * which on a phone means the whole task list mounting, fetching and unmounting before
 * anybody sees the record they asked for.
 */
export function useWideShell(): boolean {
  return useSyncExternalStore(subscribe, matches, () => true);
}
