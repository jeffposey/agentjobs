import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";

import { apiVersionApiVersionGetOptions } from "../api/generated/@tanstack/react-query.gen";
import { client } from "../api/generated/client.gen";

/**
 * The app-level actions menu, anchored to the top-right of the header.
 *
 * **This is a shell, and that is the point (task-168).** It holds About and the API
 * docs today; task-345 moves Playbooks and Dispatch settings off the primary nav into
 * it, and task-170 adds dispatch. Adding an entry should be a line in
 * {@link ENTRIES} or one more branch of {@link View}, not another popup.
 *
 * **Top-right, and not a hamburger.** Navigation lives at the left end of this bar and
 * collapses behind a burger below `NAV_INLINE_MIN_PX`; two hamburgers at opposite ends
 * of one row read as a duplicate of each other. A kebab in the opposite corner says
 * "actions" the way Gmail, Slack and Material all say it. The AgentJobs mark was the
 * other candidate and is wrong *here*: a logo labels the app you are already inside
 * and competes with the wordmark three elements to its left. task-169 embeds this same
 * menu in somebody else's app, where the mark becomes right for exactly that inverse
 * reason -- so the trigger's glyph is deliberately the only thing that would change.
 *
 * **It is transient.** Opens on press, closes on Escape, on a press outside, on a
 * route change, and when an entry is chosen. It is not a drawer and never needs hover.
 * The behaviour is copied from `PrimaryNav`'s panel rather than shared with it: one is
 * a full-width sheet and this is an anchored menu, and the two have already drifted
 * apart in what a press outside means.
 *
 * **It never moves the header.** The popup is `absolute`, so opening it overlays the
 * page instead of pushing it down under a bar that is pinned. Its z-order needs no
 * rule of its own: the header is `z-30`, which is a stacking context, so everything
 * here paints below IssueReporter's `z-40` button and `z-50` modal whatever this file
 * says.
 */

/** Which of the menu's surfaces is on screen. Adding one is a branch, not a popup. */
type View = "closed" | "menu" | "about";

export const ACTIONS_MENU_ID = "actions-menu";
export const ACTIONS_ABOUT_ID = "actions-menu-about";

/**
 * Entries that are a plain link out of the app.
 *
 * `/docs` is FastAPI's own page rather than a route this app owns, so it is an
 * `<a>` and not a `<Link>`; a `<Link>` would ask the router for a route that does not
 * exist and land on the not-found page. Task-345 removes the duplicate of this entry
 * from the nav row once this menu exists to hold it -- until then both are present,
 * which is the cheaper of the two orders to do this in.
 */
const ENTRIES: ReadonlyArray<{ href: string; label: string }> = [
  { href: "/docs", label: "API Docs" },
];

const itemClass =
  "touch-target block w-full px-4 text-left text-sm text-dark-text hover:bg-dark-border focus:bg-dark-border focus:outline-none";

function KebabIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <circle cx="10" cy="4" r="1.6" fill="currentColor" />
      <circle cx="10" cy="10" r="1.6" fill="currentColor" />
      <circle cx="10" cy="16" r="1.6" fill="currentColor" />
    </svg>
  );
}

/**
 * What the About panel answers, one row each.
 *
 * A definition list rather than a paragraph: every row is a value somebody is about to
 * read back to someone else over a phone, and a `<dt>`/`<dd>` pair is what lets a test
 * -- and a screen reader -- address the value by its label instead of by position.
 */
function AboutRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <dt className="shrink-0 text-xs uppercase tracking-wide text-dark-muted">{label}</dt>
      <dd className="min-w-0 break-all text-right text-sm font-medium text-dark-text">{value}</dd>
    </div>
  );
}

/**
 * Which server this app is actually calling.
 *
 * Asked of the generated client rather than assumed. In this app it has no `baseUrl`
 * at all -- every call is relative, so the origin serving the page is the server being
 * talked to -- but that is exactly what stops being true in task-169's embedded build,
 * where the page comes from a host app and the calls do not. Reading the client's own
 * configuration is the same line of code and is right in both.
 */
function serverOrigin(): string {
  const configured = client.getConfig().baseUrl;
  return configured !== undefined && configured !== "" ? configured : window.location.origin;
}

/**
 * Which AgentJobs am I looking at -- readable from a phone, without a route.
 *
 * **The version is read from the server every time, never compiled in.** A constant
 * baked into the bundle reports the build the *tab* came from, which is precisely
 * wrong: the question is asked when a tab has been open for hours and the server has
 * been restarted underneath it, and a confident wrong answer there is worse than none.
 * `/api/version` already exists and already carries every field this needs, so nothing
 * was added to the API for this panel -- see the task's decision log.
 */
function AboutPanel({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const version = useQuery(apiVersionApiVersionGetOptions());

  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  let versionText = "unavailable";
  if (version.isPending) versionText = "reading...";
  else if (version.data) versionText = version.data.version;

  return (
    <div
      id={ACTIONS_ABOUT_ID}
      role="dialog"
      aria-label="About AgentJobs"
      className="absolute right-0 top-full z-10 mt-1 w-72 rounded-lg border border-dark-border bg-dark-surface p-4 shadow-lg"
    >
      <div className="flex items-start justify-between gap-2">
        <h2 className="text-base font-semibold text-dark-text">About AgentJobs</h2>
        <button
          ref={closeRef}
          type="button"
          onClick={onClose}
          aria-label="Close About"
          className="touch-target rounded-md px-2 text-dark-muted hover:bg-dark-border hover:text-dark-text focus:outline-none focus:ring-1 focus:ring-blue-400"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            <path
              d="M3 3l10 10M13 3L3 13"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
            />
          </svg>
        </button>
      </div>
      <dl className="mt-2 divide-y divide-dark-border">
        <AboutRow label="Version" value={versionText} />
        <AboutRow label="Server" value={serverOrigin()} />
        <AboutRow label="Project" value={projectId || "none"} />
      </dl>
      {version.isError && (
        <p className="mt-3 text-xs text-dark-muted">
          The server did not answer /api/version. It may be stopped, or too old to report
          one.
        </p>
      )}
    </div>
  );
}

export function ActionsMenu({
  projectId,
  onOpen,
}: {
  projectId: string;
  /**
   * Told when this menu opens, so the caller can close its own popup.
   *
   * `PrimaryNav`'s burger panel dismisses on a press outside *the header*, and this
   * trigger is inside the header -- so without this the two would be open at once,
   * one overlapping the other. The reverse direction needs nothing: a press on the
   * burger is outside this menu's own root and dismisses it already.
   */
  onOpen?: () => void;
}) {
  const [view, setView] = useState<View>("closed");
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const location = useLocation();

  const close = useCallback(() => setView("closed"), []);
  /**
   * Close, and put focus back where it came from.
   *
   * By hand, because neither surface is a browser dialog: the node holding focus is
   * about to leave the document, and the browser would otherwise leave focus on
   * nothing at all -- which on a keyboard means the next Tab starts from the top of
   * the page.
   */
  const closeAndReturn = useCallback(() => {
    setView("closed");
    triggerRef.current?.focus();
  }, []);

  // Covers the back button and any link that does not run an onClick of ours.
  useEffect(close, [close, location.pathname]);

  useEffect(() => {
    if (view === "closed") return undefined;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      closeAndReturn();
    };
    // `mousedown`, not `click`: a press that starts outside and ends on a link should
    // dismiss this rather than following the link underneath it.
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target;
      if (target instanceof Node && rootRef.current?.contains(target)) return;
      // No focus return here. The press has already moved the user's attention
      // somewhere else, and yanking focus back into the header would fight it.
      close();
    };

    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onPointerDown);
    };
  }, [close, closeAndReturn, view]);

  // Opening with a press puts focus on the first entry, which is what makes the menu
  // navigable from a keyboard without a second gesture to get into it.
  useEffect(() => {
    if (view !== "menu") return;
    const first = menuRef.current?.querySelector<HTMLElement>("[role='menuitem']");
    first?.focus();
  }, [view]);

  /**
   * Arrow keys move between entries; Home and End jump to the ends.
   *
   * Roving focus rather than Tab order: entries carry `tabIndex={-1}` so a Tab out of
   * the menu leaves the menu, which is what a menu is for. Wrapping at both ends,
   * because a menu of two entries where Down does nothing at the bottom reads as
   * broken.
   */
  const onMenuKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const keys = ["ArrowDown", "ArrowUp", "Home", "End"];
    if (!keys.includes(event.key)) return;
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLElement>("[role='menuitem']") ?? [],
    );
    if (items.length === 0) return;
    event.preventDefault();
    const at = items.indexOf(document.activeElement as HTMLElement);
    let next = 0;
    if (event.key === "ArrowDown") next = at < 0 ? 0 : (at + 1) % items.length;
    if (event.key === "ArrowUp") next = at <= 0 ? items.length - 1 : at - 1;
    if (event.key === "End") next = items.length - 1;
    items[next]?.focus();
  };

  return (
    <div ref={rootRef} className="relative ml-auto shrink-0">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => {
          setView((current) => {
            if (current === "closed") {
              onOpen?.();
              return "menu";
            }
            return "closed";
          });
        }}
        aria-haspopup="menu"
        aria-expanded={view !== "closed"}
        aria-controls={view === "about" ? ACTIONS_ABOUT_ID : ACTIONS_MENU_ID}
        aria-label="Actions"
        className="touch-target rounded-md px-3 text-dark-text hover:bg-dark-border focus:outline-none focus:ring-1 focus:ring-blue-400"
      >
        <KebabIcon />
      </button>

      {view === "menu" && (
        <div
          id={ACTIONS_MENU_ID}
          ref={menuRef}
          role="menu"
          aria-label="Actions"
          onKeyDown={onMenuKeyDown}
          // `absolute`, so the header keeps its height and the page keeps its place.
          className="absolute right-0 top-full z-10 mt-1 w-64 rounded-lg border border-dark-border bg-dark-surface py-1 shadow-lg"
        >
          <button
            type="button"
            role="menuitem"
            tabIndex={-1}
            onClick={() => setView("about")}
            className={itemClass}
          >
            About
          </button>
          {ENTRIES.map((entry) => (
            <a
              key={entry.href}
              href={entry.href}
              role="menuitem"
              tabIndex={-1}
              // Chosen an entry, so the menu has done its job. Closing on the way out
              // also means coming back never finds it hanging open.
              onClick={close}
              className={itemClass}
            >
              {entry.label}
            </a>
          ))}
        </div>
      )}

      {view === "about" && <AboutPanel projectId={projectId} onClose={closeAndReturn} />}
    </div>
  );
}
