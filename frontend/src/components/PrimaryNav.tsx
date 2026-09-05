import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";

import { ProjectSwitcher } from "./ProjectSwitcher";

/**
 * The app's global navigation, pinned to the top of the viewport.
 *
 * The first two things below are one decision, recorded on task-292; the third is
 * task-336.
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
 * **It says where you are.** Until task-336 it did not: every destination rendered
 * identically on every page, so the only styled entry in the bar was Create's blue
 * accent and a reader on the Dashboard took *that* for the selected tab. The current
 * entry is now the brightest thing in the row, tinted and ringed, and carries
 * `aria-current="page"`; see {@link currentDestinationPath} for which entry that is.
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
  /** Renders the live-run count after the label. Exactly one entry has one. */
  badge?: boolean;
}> = [
  { path: "", label: "Dashboard" },
  { path: "/tasks", label: "Tasks" },
  // No accent, deliberately, and this is task-336's finding rather than a tidy-up.
  // It used to be `text-blue-300` -- the only coloured thing in a bar where nothing
  // marked the current page -- so on the Dashboard the one entry that stood out was
  // Create, and it read as the selected tab. Blue in this bar now means "you are
  // here" and nothing else.
  { path: "/tasks/new", label: "Create" },
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

/**
 * Which destination the current URL belongs to, or `null` when none of them owns it.
 *
 * **Longest match wins**, which is what makes `/tasks/new` mark Create rather than
 * lighting up Tasks as well -- both entries match that URL and only the deeper one
 * should win. It also makes Dashboard, whose path is `""`, the fallback for anything
 * under the project that no other entry claims, without needing a rule of its own.
 *
 * A match is compared at a segment boundary in both halves, so project `demo2` is not
 * a match for project `demo`, and a hypothetical `/tasksomething` is not one for
 * `/tasks`. Exported so a test can address the rule directly: a URL is the whole
 * input, so rendering a header to ask about one case is more machinery than the
 * question needs.
 */
export function currentDestinationPath(pathname: string, projectId: string): string | null {
  const prefix = projectPath(projectId);
  if (pathname !== prefix && !pathname.startsWith(`${prefix}/`)) return null;

  const rest = pathname.slice(prefix.length);
  let best: string | null = null;
  for (const { path } of DESTINATIONS) {
    const matches = rest === path || rest.startsWith(`${path}/`);
    if (matches && (best === null || path.length > best.length)) best = path;
  }
  return best;
}

const linkClass = "touch-target rounded-md px-3 text-sm font-medium";
/**
 * Not the page you are on.
 *
 * Muted rather than full-strength `text-dark-text`: before task-336 every destination
 * was rendered identically, so "which one is selected" had no answer at all and the
 * question in the report is literal. Dimming the rest is half of the answer and costs
 * nothing -- #94a3b8 on the bar's #1e293b is 5.7:1, comfortably past AA.
 */
const restClass = "text-dark-muted hover:bg-dark-border hover:text-dark-text";
/**
 * The page you are on.
 *
 * Three channels, not one: brightest text in the bar, a tinted background, and a
 * hairline ring around it. Colour alone would be the usual mistake, and the
 * background alone would collide with `hover:bg-dark-border` -- an inactive entry
 * under the pointer would then look exactly like the current one, which is the bug
 * again with extra steps. `inset-ring` rather than a border because a border is 2px
 * of layout: the row's one-line fit is measured (see {@link NAV_INLINE_MIN_PX}) and a
 * ring is painted inside the box without moving anything.
 */
const currentClass = "bg-blue-500/15 text-white inset-ring-1 inset-ring-blue-400/60";

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

  const currentPath = currentDestinationPath(location.pathname, projectId);

  const destinations = DESTINATIONS.map((destination) => {
    const current = destination.path === currentPath;
    return (
      <Link
        key={destination.label}
        to={projectPath(projectId, destination.path)}
        // The half of this a screen reader gets, and the half a test can assert on
        // without knowing a class name. Absent rather than "false" on the others:
        // `aria-current="false"` is valid and means the same thing, but every entry
        // carrying the attribute makes "which one" a question about its value.
        aria-current={current ? "page" : undefined}
        className={`${linkClass} ${current ? currentClass : restClass}`}
      >
        {/* The label is its own element so a test can address it exactly. Without the
            span the badge's text is part of the link's only text node, and
            `getByText("Runs", { exact: true })` -- how every other destination in
            e2e/pinned-header.spec.ts is found -- matches nothing at all. */}
        <span>{destination.label}</span>
        {destination.badge ? badge : null}
      </Link>
    );
  });

  // Outside the router: /docs is FastAPI's, not a route this app owns, so it is never
  // the current page however the app got here.
  const apiDocs = (
    <a href="/docs" className={`${linkClass} ${restClass}`}>
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
