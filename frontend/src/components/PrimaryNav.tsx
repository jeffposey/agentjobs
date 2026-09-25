import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";

import { ActionsMenu } from "./ActionsMenu";
import { CaptureControl } from "./CaptureControl";
import { EmergencyStop, StoppedBanner } from "./EmergencyStop";
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
 * rather than a reflexive `z-50`: it must stay *below* the capture dialog's `z-50`,
 * because a header floating above a full-screen dialog punches a hole in it. Since
 * task-346 the capture trigger is inside this header, so it needs no z-index of its
 * own; the rule survives for the dialog it opens.
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
 * **It carries navigation and nothing else (task-345).** The row had been added to one
 * entry at a time and never subtracted from, so its contents were the union of every
 * feature that wanted a link. Dispatch settings is a settings page, Playbooks is a
 * launcher and API Docs is somebody else's reference document; none of the three is
 * somewhere you go while working, and standing beside the ones that are made those
 * harder to see. Analytics went with them at the owner's call on review. All four are
 * in {@link ActionsMenu} now, one interaction from every page the bar is on.
 *
 * What is left is Dashboard and Tasks. task-345 kept Runs as a third; task-588 retired
 * it, because the owner never went there, and moved its count into the Dashboard tab
 * (`NavCounts`). **Before adding a third, notice that this row is the one part of the app
 * with a documented history of growing by one good reason at a time.** The menu is where
 * a link goes unless you can say what a reader is navigating *to*.
 *
 * The burger is at the **left** end on purpose. The *actions* end is the top-right --
 * task-346's capture trigger and task-168's {@link ActionsMenu} (About, Analytics,
 * Dispatch settings, Playbooks and the API docs), grouped; navigation-left/
 * actions-right keeps the two apart, and keeps navigation out of the component
 * task-169 wants to embed in somebody else's app.
 *
 * **There is no Create destination, and that is task-346 rather than task-345.** The
 * capture control replaced it: one control now reaches both a fifteen-second report and
 * the whole authoring form, so a link to half of it would be a second way to do the
 * same thing. `/tasks/new` still exists as a page and still marks Tasks as the current
 * destination, longest-match having nothing deeper to offer it.
 */

/**
 * The width at or above which every destination is shown inline.
 *
 * **Re-measured for task-338's attention badge, which added 34px to the row** -- a
 * 24px pill and the 10px gap before it. task-292 settled the mechanism and measured
 * 955px for the contents it had; task-328's Runs entry took that to 1090; anything
 * added to the row changes the input to that measurement, so the number moves and the
 * mechanism does not.
 *
 * **Re-measured again for task-465's Analytics entry, which added 81px plus a gap.**
 * That one did not fit by moving the number alone: with the switcher pinned and the
 * badge showing, seven destinations plus API Docs at the old 24px spacing needed 17px
 * more than the `max-w-7xl` header can ever give them, at any viewport -- `API Docs`
 * wrapped onto two lines at 1280 and the row was flush with the edge. So the inline
 * row's gap went from `gap-6` to `gap-4` (56px back across seven gaps) and the number
 * moved less than it otherwise would have.
 *
 * **task-346's capture trigger was deliberately not measured on its own branch**, on
 * the argument that swapping a `Create` link for a 44px icon button is a net narrowing
 * and leaves the number merely untight. That was right, and the measurement below is
 * the one that settles it: this task owns the row's final contents, and re-deriving it
 * twice would have collided for no gain.
 *
 * **Re-measured for task-168's actions menu**, which adds a 44px trigger plus a
 * gap at the right end -- and unlike a destination it is there at *every* width, since
 * a menu that vanishes on a phone is not somewhere Dispatch settings could move to.
 * Dropped straight in, the row fitted only at 1278 of the 1280 the `max-w-7xl` header
 * can ever reach, and a 2px margin is not one. So the **outer** gap went from `gap-6`
 * to `gap-4` at the breakpoint -- the lever task-465 pulled on the inline group, four
 * gaps here rather than seven, 32px back -- and the last overflow landed at 1244, for
 * a constant of 1256.
 *
 * Overflow, not wrapping, is how this fails: `flex-nowrap` and `min-w-0` mean the
 * switcher is squeezed and then the row runs off the right edge, so a header measured
 * only by its height would call every width below the breakpoint fine.
 *
 * **Re-measured for task-345, the first change that subtracts from this row.**
 * Dispatch, Playbooks, API Docs and -- on the owner's call at review -- Analytics all
 * left it for the actions menu, and Create left with task-346. Three destinations
 * remain, beside the capture trigger and the kebab, and the bar last overflows at
 * **810px**, so the constant is **822**: the same 12px of margin over the last overflow
 * that 1256 kept over 1244, and 434px below the number it replaces.
 *
 * Measured in Chromium against the `e2e/run_server.py` sandbox on 2026-09-20, with the
 * project switcher pinned to the 224px (`max-w-56`) it reaches for a long project name,
 * the attention badge showing, every link forced `nowrap`, and the breakpoint itself
 * temporarily set to 360 so the inline row was laid out at every width under test;
 * swept in 1px steps. The switcher is pinned because that is the case this constant has
 * to hold for, not the four-character project a sandbox happens to have.
 *
 * **The interim figure of 762 was a floor and the sweep says by how much.** Taken
 * before task-346 merged, it laid out these same three destinations with no capture
 * control in the bar; adding that control costs 48px, which is the 44px trigger and its
 * gap almost exactly. The number quoted as a floor came out 48px below the answer, in
 * the direction it was predicted to -- which is the cross-check that this instrument
 * measures the row rather than itself.
 *
 * **The prize the task hoped for is still not there.** A row this short was expected to
 * fit a phone, which would have retired the burger; 810 is more than twice a 390px one.
 * What this row costs is mostly not its destinations. The wordmark, the 224px switcher,
 * the badge, the capture trigger and the kebab are some 500px of fixed furniture before
 * the first link is drawn, so deleting links has a floor well above a phone. Shrinking
 * *those* is the only lever that would reach one, and it is nobody's task yet.
 *
 * **The badge is why this first moved and it is also why the move is cheap.** It
 * renders only when work has actually stopped on you, so the 34px is spent on the rare
 * screen rather than every screen -- but the constant has to hold for the screen that
 * spends it, since that is the one a person is being asked to read.
 *
 * The cost is now the 810-821 band rather than the 960-1255 one task-168 left behind:
 * landscape tablets, portrait tablets and split-screen desktop windows are all back to
 * an inline row. The burger is still the phone's experience, and still keeps every
 * destination one tap away.
 *
 * **Re-measured for task-588, which took Runs out and merged the two badges.** The
 * Runs link and the attention badge left; two counts (`NavCounts`) moved inside the
 * Dashboard link, which is 157px wide with them. Unlike the red badge they are drawn on
 * every screen, so there is no rarer case to measure for. Same method as above --
 * switcher pinned at 224px, links `nowrap`, breakpoint temporarily 360, 1px steps, on
 * 2026-09-24 -- and the bar last overflows at **736px**, so the constant is **748**.
 * (The first cut, a separate pill link with a slot fraction, measured 758 and 770; the
 * owner rejected that pill on review.)
 *
 * Below the breakpoint the Dashboard link is drawn beside the switcher as the counts
 * alone (78px), and there the wordmark is drawn only for a screen reader. Measured with
 * the first cut's pill, drawing it left the switcher 19px wide at 390px. Without it the
 * switcher has 120px at 390 and 105px at 375.
 *
 * **Re-measured for task-573's emergency stop**, a 44px trigger plus a gap left of the
 * capture trigger at every width. Switcher pinned at 224px, links `nowrap`, swept at
 * 1px steps on 2026-09-24, in both of the trigger's states, which are the same width by
 * design: the bar last overflows at **784px**, so the constant is **796**. A first cut
 * drew the stopped state as a wider "Stopped" pill. It cost 57px more, and at 375px it
 * squeezed the switcher to nothing, so the stopped state became a strip under the bar
 * instead (`StoppedBanner`). Below the breakpoint the switcher now has 57px at 375.
 *
 * Kept as a constant beside the class names that encode it so a reader can find both
 * at once; Tailwind needs the literal in the class, so the two are checked against
 * each other by a test rather than by the compiler.
 */
export const NAV_INLINE_MIN_PX = 796;

/** Shown inline above the breakpoint, and inside the panel below it. */
const DESTINATIONS: ReadonlyArray<{
  path: string;
  label: string;
  /** Carries the header's status counts inside its link. Exactly one entry does. */
  status?: boolean;
}> = [
  // The status counts are drawn inside this link (task-588): the Dashboard is where
  // both are broken out -- the "stopped on you" panel for red, the slot board for green.
  { path: "", label: "Dashboard", status: true },
  { path: "/tasks", label: "Tasks" },
  // Analytics is **not** here. task-465 put it in this row on 2026-09-18 because the
  // owner could not find the page, and task-345 shipped it here for review on the
  // argument that a reading surface is a destination in a way a settings page is not.
  // He looked at the bar and moved it into ActionsMenu with the rest -- so the row is
  // narrower than that argument would have made it, and the argument lost to the
  // person it was about. Anyone tempted to bring it back should read task-465 first:
  // the failure it was filed against was that the page had no entry point a reader
  // would look for, and a menu row is one where a link in a heading row was not.
  //
  // Create is not here either, and that one is task-346: its capture control replaced
  // the link, so the act is on every page rather than one tap from most of them.

  //
  // Runs is not here either (task-588). task-328 added it as the unconstrained view of
  // every run on the machine, carrying the bar's only badge; the owner never opened it,
  // so the badge was a number hung on a door nobody used. The count is part of
  // NavStatus now and `/runs` redirects to the Dashboard.
];

const PANEL_ID = "primary-nav-destinations";

function projectPath(projectId: string | undefined, path = "") {
  return `/p/${encodeURIComponent(projectId ?? "")}${path}`;
}

/**
 * Which destination the current URL belongs to, or `null` when none of them owns it.
 *
 * **Longest match wins**, so an entry that owns a prefix never lights up alongside a
 * deeper one that owns the whole path. Nothing in the row exercises it today --
 * `/tasks/new` did until task-346 took Create out and left `/tasks` as the deepest
 * entry matching it -- and the rule stays because the next nested destination would
 * otherwise mark two.
 *
 * **Dashboard's `""` matches the project root and nothing else** (task-345). It used
 * to be the fallback for every URL under the project that no other entry claimed,
 * which cost nothing while every route had an entry. Once Dispatch settings and
 * Playbooks moved into the actions menu it became a liar: standing on `/dispatch`, the
 * bar would tell you confidently that you were on the Dashboard -- which is task-336's
 * original bug wearing a different hat, and worse than the honest answer, because a
 * reader can recover from a bar that marks nothing and cannot recover from one that
 * marks the wrong thing. So a route with no entry now marks no entry; see the task's
 * decision entry for what was rejected.
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
    // The empty path is spelled out rather than falling through the general rule,
    // because `"/anything".startsWith("/")` is true and that is exactly the
    // catch-all this no longer wants to be.
    const matches =
      path === "" ? rest === "" || rest === "/" : rest === path || rest.startsWith(`${path}/`);
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

/**
 * Whether the destinations are laid out inline, i.e. the viewport is at least
 * {@link NAV_INLINE_MIN_PX} wide.
 *
 * Read from the same media query the classes encode, so the one place JavaScript needs
 * the answer -- where the status counts mount -- cannot disagree with the CSS. Where
 * `matchMedia` does not exist (jsdom) the answer is the narrow layout, which puts the
 * readout outside the collapsible group: the placement that is visible at every width.
 */
function useInlineRow(): boolean {
  const query = `(min-width: ${NAV_INLINE_MIN_PX}px)`;
  const [wide, setWide] = useState(
    () => typeof window.matchMedia === "function" && window.matchMedia(query).matches,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return undefined;
    const media = window.matchMedia(query);
    const onChange = () => setWide(media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [query]);
  return wide;
}

export function PrimaryNav({
  projectId,
  status,
  statusLabel,
  onDashboardFollow,
}: {
  projectId: string;
  /**
   * The status counts -- waiting on you, being worked (task-588) -- supplied by the
   * shell rather than queried here, so this component stays pure presentation and its
   * tests need no query client and no server. Plain content, never a link: it is drawn
   * *inside* the Dashboard link, so the bar has one way to the Dashboard, not two.
   *
   * **It is drawn in one of two places, never both.** At or above
   * {@link NAV_INLINE_MIN_PX} it is inside the Dashboard tab, after its label. Below it
   * every destination is behind the burger, and the phone -- read over Tailscale -- is
   * where noticing that work has stopped matters most (task-338's argument for the old
   * red badge's placement), so there the Dashboard link is also drawn in the bar beside
   * the project switcher, as the counts alone. One mount either way, so a screen reader
   * and a test each find exactly one.
   */
  status?: ReactNode;
  /** The counts in words; the Dashboard link's accessible name becomes "Dashboard · …". */
  statusLabel?: string;
  /**
   * Called when the link carrying the counts is followed. The shell acknowledges the
   * attention episode with it (task-422), as the red badge's click did before task-588.
   */
  onDashboardFollow?: () => void;
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

  const wide = useInlineRow();

  // Widening past the breakpoint hides the burger, so a panel left open would come
  // back on the way down with no control that had ever been pressed.
  useEffect(() => {
    if (wide) close();
  }, [close, wide]);

  const currentPath = currentDestinationPath(location.pathname, projectId);

  const statusName = statusLabel ? `Dashboard · ${statusLabel}` : undefined;

  const destinations = DESTINATIONS.map((destination) => {
    const current = destination.path === currentPath;
    const carries = Boolean(destination.status && wide && status);
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
        {...(carries
          ? {
              "data-testid": "nav-status",
              "aria-label": statusName,
              title: statusLabel,
              onClick: onDashboardFollow,
            }
          : {})}
      >
        {/* The label is its own element so a test can address it exactly, which is how
            every destination in e2e/pinned-header.spec.ts is found. It mattered while
            Runs carried a badge inside its link (task-328 to task-588); it stays so the
            next thing put beside a label cannot quietly break those lookups. */}
        <span>{destination.label}</span>
        {carries && <span className="ml-2.5">{status}</span>}
      </Link>
    );
  });

  // The API Docs anchor used to be built here and rendered beside the destinations.
  // task-345 moved it into the actions menu, where task-168 argued it belonged: it is
  // FastAPI's own page rather than a route this app owns, so it could never be the
  // current entry however the app got here, and a row whose members are not all the
  // same kind of thing is the shape this task was filed to undo.

  return (
    <header
      ref={headerRef}
      className="sticky top-0 z-30 border-b border-dark-border bg-dark-surface"
    >
      <nav
        className="mx-auto flex min-h-16 max-w-7xl flex-nowrap items-center gap-2 px-4 py-2 min-[796px]:gap-4 sm:px-6 lg:px-8"
        aria-label="Primary navigation"
      >
        {/*
          The breakpoint lives on this wrapper rather than on the button, and that is
          not a stylistic choice. `styles.css` carries `.touch-target:not(.block) {
          display: inline-flex }`, whose specificity (0,2,0) beats a Tailwind utility's
          (0,1,0) -- so `min-[796px]:hidden` on a `touch-target` element loses, and the
          burger stays visible at every width. Caught in a browser at 1280px; jsdom
          would never have shown it.
        */}
        <div className="shrink-0 min-[796px]:hidden">
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
        {/*
          Read by a screen reader at every width, drawn only at or above the breakpoint
          (task-588). The Dashboard link drawn outside the collapsible group costs
          some 80px on a phone, and at 390px it once left the project switcher 19px
          wide -- the project's name gone, beside the product's. On a phone the name of
          the project is the one a reader needs, so the wordmark gives up the room.
        */}
        <h1 className="sr-only text-2xl font-bold min-[796px]:not-sr-only min-[796px]:shrink-0">
          AgentJobs
        </h1>
        <ProjectSwitcher projectId={projectId} />
        {!wide && status && (
          // The Dashboard link, as the counts alone, where a phone can see it. The panel
          // still lists Dashboard by name for a reader looking for the word.
          <Link
            to={projectPath(projectId)}
            data-testid="nav-status"
            aria-label={statusName}
            title={statusLabel}
            onClick={onDashboardFollow}
            className={`${linkClass} shrink-0 ${
              currentPath === "" ? currentClass : restClass
            }`}
          >
            {status}
          </Link>
        )}
        {/*
          `gap-4`, not the `gap-6` the bar itself uses between its regions. task-465
          took it to 16px to buy width back from a row of eight; task-345 left it
          there rather than restoring 24px on a row of four, because the spare width
          is worth more as a lower NAV_INLINE_MIN_PX than as spacing -- the whole
          point of shortening the row was to move that number down. The task's
          constraint is not to restyle the bar, and reverting this would be one.
        */}
        <div className="hidden items-center gap-4 min-[796px]:flex">{destinations}</div>
        {/*
          The actions end: the capture trigger (task-346) and the actions menu
          (task-168), at the opposite end from the burger and present at every width.
          `ml-auto` lives on this wrapper rather than on either child, because two
          auto margins in one flex row share the free space between them and would push
          the pair apart instead of to the edge. Both sit outside the collapsible group
          on purpose -- the burger takes the destinations away below the breakpoint, and
          neither of these is a destination.
        */}
        <div className="ml-auto flex shrink-0 items-center gap-1">
          <EmergencyStop />
          <CaptureControl />
          <ActionsMenu projectId={projectId} onOpen={close} />
        </div>
      </nav>
      {/* Under the bar rather than in it: the stopped state costs the row no width. */}
      <StoppedBanner />
      {open && (
        <div
          id={PANEL_ID}
          // Absolute rather than in flow, so opening the panel overlays the page
          // instead of pushing it down under a bar that is already pinned. `sticky`
          // is a positioned value, so the header is the containing block already.
          className="absolute inset-x-0 top-full border-b border-dark-border bg-dark-surface shadow-lg min-[796px]:hidden"
        >
          <div
            className="mx-auto flex max-w-7xl flex-col px-4 py-2 sm:px-6"
            onClick={close}
          >
            {destinations}
          </div>
        </div>
      )}
    </header>
  );
}
