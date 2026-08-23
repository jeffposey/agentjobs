import { useCallback, useEffect, useRef, useState } from "react";
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
 * **It never wraps.** The row of destinations only fits on one line at about 955px
 * (measured, not guessed: the eight items are 736px of content before gaps, and the
 * nav is 128px tall at 950 and 64px at 955). Below {@link NAV_INLINE_MIN_PX} the
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
 * 960 rather than this app's existing 820px breakpoint: 820 is where the tables
 * restack, and the header stops fitting 135px later. A breakpoint at 820 would leave
 * the whole 820-954 band -- landscape tablets, split-screen desktop windows -- with a
 * wrapped, pinned, two-row bar.
 *
 * Kept as a constant beside the class names that encode it so a reader can find both
 * at once; Tailwind needs the literal in the class, so the two are checked against
 * each other by a test rather than by the compiler.
 */
export const NAV_INLINE_MIN_PX = 960;

/** Shown inline above the breakpoint, and inside the panel below it. */
const DESTINATIONS: ReadonlyArray<{ path: string; label: string; className?: string }> = [
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
];

const PANEL_ID = "primary-nav-destinations";

function projectPath(projectId: string | undefined, path = "") {
  return `/p/${encodeURIComponent(projectId ?? "")}${path}`;
}

const linkClass = "touch-target rounded-md px-3 text-sm font-medium hover:bg-dark-border";

export function PrimaryNav({ projectId }: { projectId: string }) {
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
      {destination.label}
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
        className="mx-auto flex min-h-16 max-w-7xl flex-nowrap items-center gap-2 px-4 py-2 min-[960px]:gap-6 sm:px-6 lg:px-8"
        aria-label="Primary navigation"
      >
        {/*
          The breakpoint lives on this wrapper rather than on the button, and that is
          not a stylistic choice. `styles.css` carries `.touch-target:not(.block) {
          display: inline-flex }`, whose specificity (0,2,0) beats a Tailwind utility's
          (0,1,0) -- so `min-[960px]:hidden` on a `touch-target` element loses, and the
          burger stays visible at every width. Caught in a browser at 1280px; jsdom
          would never have shown it.
        */}
        <div className="shrink-0 min-[960px]:hidden">
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
        <div className="hidden items-center gap-6 min-[960px]:flex">
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
          className="absolute inset-x-0 top-full border-b border-dark-border bg-dark-surface shadow-lg min-[960px]:hidden"
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
