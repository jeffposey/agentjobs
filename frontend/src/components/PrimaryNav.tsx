import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";

import { ProjectSwitcher } from "./ProjectSwitcher";

/**
 * The app's global navigation, pinned to the top of the viewport.
 *
 * Two things happen here and they are one decision, recorded on task-292.
 *
 * **It is pinned.** `sticky top-0` on the <header>. This header is the whole
 * navigation system -- there is no rail, and no back button that means anything --
 * and the pages you most need to leave are the longest ones in the product. `z-30`
 * rather than a reflexive `z-50`: it must stay *below* IssueReporter's `z-40` button
 * and `z-50` modal, because a header floating above a full-screen dialog punches a
 * hole in it.
 *
 * **It never wraps.** The row of destinations only fits on one line above
 * {@link NAV_INLINE_MIN_PX} (measured, not guessed -- see that constant). Below it the
 * destinations move behind a burger and drop from the bar in a panel; above it they
 * are inline and there is no burger at all. Pinning the wrapped row instead would
 * spend 164px of an 844px phone screen -- 19% -- permanently on navigation, which is
 * a worse problem than the one being fixed.
 *
 * The burger is at the **left** end on purpose. task-168 puts an *actions* menu in
 * the top-right (Report issue, About, later dispatch); navigation-left/actions-right
 * keeps the two apart, and keeps navigation out of the component task-169 wants to
 * embed in somebody else's app.
 */

/**
 * The width at or above which every destination is shown inline.
 *
 * **Re-measured for task-328's Runs entry, which added 82px to the row** (665px of
 * destinations, up from 583px, measured at 960 in Chromium). task-292 settled the
 * mechanism and measured 955px for the contents it had; adding a destination changes
 * the input to that measurement, so the number moves and the mechanism does not.
 *
 * 1090px is where the bar last overflows, with the project switcher at the 224px
 * (`max-w-56`) it reaches for a long project name -- the case the constant has to hold
 * for, not the four-character one a sandbox happens to have. Overflow, not wrapping, is
 * how this now fails: `flex-nowrap` and `min-w-0` mean the switcher is squeezed and then
 * the row runs off the right edge, so a header measured only by its height would have
 * called every width below this fine. 1100 for the same 10px of margin 960 had over 955.
 *
 * The cost is the 960-1099 band -- landscape tablets, split-screen desktop windows --
 * moving from an inline row to the burger. That is the trade task-292 already made once
 * at 960, and the burger keeps every destination one tap away.
 *
 * Kept as a constant beside the class names that encode it so a reader can find both
 * at once; Tailwind needs the literal in the class, so the two are checked against
 * each other by a test rather than by the compiler.
 */
export const NAV_INLINE_MIN_PX = 1100;

/** Shown inline above the breakpoint, and inside the panel below it. */
const DESTINATIONS: ReadonlyArray<{
  path: string;
  label: string;
  className?: string;
  /** Renders the live-run count after the label. Exactly one entry has one. */
  badge?: boolean;
}> = [
  { path: "", label: "Dashboard" },
  { path: "/tasks", label: "Tasks" },
  // Accented because it is the one entry that creates something rather than going
  // somewhere; carried over unchanged from the pre-task-292 header.
  { path: "/tasks/new", label: "Create", className: "text-blue-300" },
  // Its own nav entry, not buried in a menu: this is where the switch that stops
  // every future run lives, and a kill switch you cannot reach is not one. Below the
  // breakpoint it is one tap behind the burger, which is the most the width allows.
  { path: "/dispatch", label: "Dispatch" },
  // Beside Dispatch rather than under it: a playbook run *is* a dispatch, and the two
  // gates a reader needs are the same ones.
  { path: "/playbooks", label: "Playbooks" },
  // Beside them again, and carrying the only badge in the bar (task-328). Dispatch is
  // the switch and Playbooks is what to start; this is what is *already* running, which
  // is the question the other two cannot answer. The badge is here rather than on the
  // Dashboard link because the count matters most while you are somewhere else --
  // reading a task, watching a queue -- and it is the only number in the app that is
  // about the machine rather than about the project the bar is scoped to.
  { path: "/runs", label: "Runs", badge: true },
];

const PANEL_ID = "primary-nav-destinations";

function projectPath(projectId: string | undefined, path = "") {
  return `/p/${encodeURIComponent(projectId ?? "")}${path}`;
}

const linkClass = "touch-target rounded-md px-3 text-sm font-medium hover:bg-dark-border";

export function PrimaryNav({
  projectId,
  badge,
}: {
  projectId: string;
  /**
   * The live-run count, supplied by the shell rather than queried here.
   *
   * Same shape as the Dashboard's `renderWhyThisOne`: this component is otherwise pure
   * presentation, rendered from props in its tests, and a query inside it would make
   * every one of those tests need a query client and a server.
   */
  badge?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const headerRef = useRef<HTMLElement>(null);
  const location = useLocation();

  const close = useCallback(() => setOpen(false), []);

  // Closing on navigation covers the back button and any link that is not one of
  // ours, neither of which runs the panel's own onClick.
  useEffect(close, [close, location.pathname]);

  useEffect(() => {
    if (!open) return undefined;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      close();
      // Focus has to come back to the trigger by hand: the panel is not a dialog and
      // the browser will otherwise leave focus on a node that just left the document.
      triggerRef.current?.focus();
    };
    // `mousedown`, not `click`: a click that starts outside and ends on a link should
    // dismiss the panel rather than following the link underneath it.
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target;
      if (target instanceof Node && headerRef.current?.contains(target)) return;
      close();
    };

    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onPointerDown);
    };
  }, [close, open]);

  // Widening past the breakpoint hides the burger, so a panel left open would come
  // back on the way down with no control that had ever been pressed.
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return undefined;
    const wide = window.matchMedia(`(min-width: ${NAV_INLINE_MIN_PX}px)`);
    const onChange = () => {
      if (wide.matches) close();
    };
    wide.addEventListener("change", onChange);
    return () => wide.removeEventListener("change", onChange);
  }, [close]);

  const destinations = DESTINATIONS.map((destination) => (
    <Link
      key={destination.label}
      to={projectPath(projectId, destination.path)}
      className={destination.className ? `${linkClass} ${destination.className}` : linkClass}
    >
      {/* The label is its own element so a test can address it exactly. Without the
          span the badge's text is part of the link's only text node, and
          `getByText("Runs", { exact: true })` -- how every other destination in
          e2e/pinned-header.spec.ts is found -- matches nothing at all. */}
      <span>{destination.label}</span>
      {destination.badge ? badge : null}
    </Link>
  ));

  // Outside the router: /docs is FastAPI's, not a route this app owns.
  const apiDocs = (
    <a href="/docs" className={linkClass}>
      API Docs
    </a>
  );

  return (
    <header
      ref={headerRef}
      className="sticky top-0 z-30 border-b border-dark-border bg-dark-surface"
    >
      <nav
        className="mx-auto flex min-h-16 max-w-7xl flex-nowrap items-center gap-2 px-4 py-2 min-[1100px]:gap-6 sm:px-6 lg:px-8"
        aria-label="Primary navigation"
      >
        {/*
          The breakpoint lives on this wrapper rather than on the button, and that is
          not a stylistic choice. `styles.css` carries `.touch-target:not(.block) {
          display: inline-flex }`, whose specificity (0,2,0) beats a Tailwind utility's
          (0,1,0) -- so `min-[1100px]:hidden` on a `touch-target` element loses, and the
          burger stays visible at every width. Caught in a browser at 1280px; jsdom
          would never have shown it.
        */}
        <div className="shrink-0 min-[1100px]:hidden">
          <button
            ref={triggerRef}
            type="button"
            onClick={() => setOpen((wasOpen) => !wasOpen)}
            aria-expanded={open}
            aria-controls={PANEL_ID}
            aria-label="Navigation"
            className="touch-target rounded-md px-3 text-dark-text hover:bg-dark-border"
          >
            <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
              <path
                d="M2 5h16M2 10h16M2 15h16"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              />
            </svg>
          </button>
        </div>
        <h1 className="shrink-0 text-2xl font-bold">AgentJobs</h1>
        <ProjectSwitcher projectId={projectId} />
        <div className="hidden items-center gap-6 min-[1100px]:flex">
          {destinations}
          {apiDocs}
        </div>
      </nav>
      {open && (
        <div
          id={PANEL_ID}
          // Absolute rather than in flow, so opening the panel overlays the page
          // instead of pushing it down under a bar that is already pinned. `sticky`
          // is a positioned value, so the header is the containing block already.
          className="absolute inset-x-0 top-full border-b border-dark-border bg-dark-surface shadow-lg min-[1100px]:hidden"
        >
          <div
            className="mx-auto flex max-w-7xl flex-col px-4 py-2 sm:px-6"
            onClick={close}
          >
            {destinations}
            {apiDocs}
          </div>
        </div>
      )}
    </header>
  );
}
