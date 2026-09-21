/**
 * Whether anything on this page is holding text nobody has sent yet (task-512).
 *
 * **It exists so the service worker can decline to reload the tab.** A rebuild puts a
 * new worker in charge, `controllerchange` fires, and `pwa.ts` reloads -- with no
 * prompt and no gesture. Persisting the capture form makes that survivable, and is
 * still not enough on its own: an unannounced reload costs focus, scroll position and
 * a reviewer's place in a long pass, none of which a restored draft brings back.
 *
 * **General rather than capture-specific, and that was the choice.** The obvious
 * alternative was to ask the capture dialog directly, which is fewer moving parts and
 * one import. It was rejected because the hazard is not about capture: a half-written
 * note, an answer to a question and an open field editor are all text somebody would be
 * annoyed to lose, and a guard that names one form is a guard that silently fails to
 * cover the next one. What `pwa.ts` needs to know is "is anything unsent", so that is
 * what this answers. Only the capture dialog registers today; a second caller is one
 * `useEffect` away and needs no change here or in `pwa.ts`.
 *
 * **A module-level set, not React context.** The reader is not a component -- it is a
 * `controllerchange` listener installed before React mounts -- so a value it can only
 * reach through the tree is a value it cannot reach.
 */

const holding = new Set<string>();

/**
 * Say whether `key` is holding unsent text.
 *
 * Idempotent, and keyed so two surfaces cannot clear each other's claim: a `Set` of
 * names rather than a counter, because a component that re-registers on every render
 * would otherwise leak a claim that is never released.
 */
export function setUnsentComposition(key: string, unsent: boolean): void {
  if (unsent) holding.add(key);
  else holding.delete(key);
}

/** True while anything on this page would lose text if the tab reloaded. */
export function hasUnsentComposition(): boolean {
  return holding.size > 0;
}

/** Forget every claim. For tests, and for a page that is being torn down. */
export function clearUnsentComposition(): void {
  holding.clear();
}
