/**
 * A viewport a jsdom test can set, and the `matchMedia` that answers from it.
 *
 * jsdom ships no `matchMedia`, so a component that reads one gets nothing to read and
 * "at a phone viewport" becomes a claim only Playwright can make. This answers from
 * `window.innerWidth`/`innerHeight`, which jsdom does maintain and a test can set, so a
 * jsdom test can ask for the phone shell the same way a phone does. It understands the
 * `min-`/`max-` width and height conjunctions this app writes and nothing else: an
 * unrecognised query answers false rather than pretending.
 *
 * Installed once from `setup.ts`; `setViewport` is what a test calls.
 */

/** jsdom's own default, restored between tests so one viewport cannot leak into the next. */
export const DEFAULT_VIEWPORT = { width: 1024, height: 768 };

function evaluate(query: string): boolean {
  return query.split(" and ").every((clause) => {
    const parsed = /\((min|max)-(width|height):\s*(\d+)px\)/.exec(clause);
    if (!parsed) return false;
    const actual = parsed[2] === "width" ? window.innerWidth : window.innerHeight;
    return parsed[1] === "min" ? actual >= Number(parsed[3]) : actual <= Number(parsed[3]);
  });
}

export function installMatchMedia() {
  window.matchMedia = (query: string): MediaQueryList => {
    const relays = new Map<(event: MediaQueryListEvent) => void, () => void>();
    return {
      media: query,
      get matches() {
        return evaluate(query);
      },
      onchange: null,
      // A resize is what a viewport change looks like in jsdom, so a test that calls
      // `setViewport` gets the same re-render a real browser would deliver on a rotate.
      addEventListener: (type: string, listener: EventListenerOrEventListenerObject) => {
        if (type !== "change" || typeof listener !== "function") return;
        const relay = () => listener(new Event("change") as MediaQueryListEvent);
        relays.set(listener as (event: MediaQueryListEvent) => void, relay);
        window.addEventListener("resize", relay);
      },
      removeEventListener: (type: string, listener: EventListenerOrEventListenerObject) => {
        if (type !== "change" || typeof listener !== "function") return;
        const relay = relays.get(listener as (event: MediaQueryListEvent) => void);
        if (!relay) return;
        window.removeEventListener("resize", relay);
        relays.delete(listener as (event: MediaQueryListEvent) => void);
      },
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    } as MediaQueryList;
  };
}

/** Set the viewport every `matchMedia` query in this test is answered from. */
export function setViewport(width: number, height: number) {
  window.innerWidth = width;
  window.innerHeight = height;
  window.dispatchEvent(new Event("resize"));
}
