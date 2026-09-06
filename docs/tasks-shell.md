# The Tasks surface: two regions, one route

**This page supersedes task-235's log entries 3, 8 and 9 as the thing to read before
changing the Tasks surface.** Those three entries are still the record of who decided
what and when — entry 3 is Jeff's shape, entry 8 adds the filter popover, entry 9 is the
device-class rule — but they were written weeks apart, and a decision spread across three
log entries on one task is a decision the next session reads one third of. That
fragmentation is what task-236's fourth acceptance criterion existed to prevent, and this
is that note.

Status: **the shell landed in task-237** (2026-09-06). The children below it are open.

## What the surface is

On the Tasks surface, and nowhere else, the list of tasks and the selected task's record
are on screen together: the list in a region down the left, the record in the region
beside it.

Five things follow from that and none of them is a style preference.

1. **The left region is the task list, not a navigation rail.** It is the shape Claude's
   own app uses for conversations, not the shape Gmail uses for folders. A rail was
   considered and rejected: this app has four top-level destinations and the apps that
   carry a rail have five to fifty, so a permanent column would spend width duplicating a
   header that already works — and the thing actually wanted is a *list*, which a rail
   does not remove the need for. **What would reopen that:** enough top-level surfaces
   that the header nav wraps, or a second list (playbooks, runs) wanting the same
   treatment. Either is a reason to reopen the decision, not to quietly build a rail
   inside a later change.
2. **It belongs to the Tasks surface only.** The Dashboard, Create, Dispatch, Playbooks
   and Runs keep the full width. The region is a master column *over tasks*, so it is
   redundant on the Dashboard — already a view over tasks — and irrelevant on a settings
   page.
3. **The header nav stays**, and remains how you move between those surfaces. A
   per-surface master column does not replace global navigation. This is also what keeps
   the change small.
4. **Opening a task from the Dashboard lands here, with that task selected** — not on a
   detail-only page with no list.
5. **`tasks` and `tasks/:taskId` are one route, not two siblings.** They were siblings,
   which is exactly why opening a task unmounted the list and threw its scroll position
   away. The record renders into the parent's outlet, so selecting a task changes only
   the right-hand region.

## The device-class rule

> On a phone the list is hidden by default. On a tablet or a desktop it is shown by
> default.

Note what that is *not*: it is not "the list exists above X and does not exist below X".
**The list exists at every viewport.** The device class picks only its initial state, and
from task-238 the reader's own toggle overrides it and is remembered per browser in
`localStorage`. Getting the class wrong on some unforeseen device therefore costs one
tap, not a broken layout, which is why the implementation is allowed to be a heuristic.

**Width alone cannot express it.** A phone in landscape is *wider* than a tablet in
portrait:

| Device | portrait | landscape | short side | class |
|---|---|---|---|---|
| iPhone 16 Pro Max | 440x956 | 956x440 | 440 | phone |
| small Android | 360x800 | 800x360 | 360 | phone |
| iPad mini | 744x1133 | 1133x744 | 744 | tablet |
| iPad 10.9 | 820x1180 | 1180x820 | 820 | tablet |

Any pure `min-width` cut gives a sideways 956px phone the tablet layout while denying it
to a 768px iPad in portrait — exactly backwards. The axis that separates the classes in
**both** orientations is the short side, and there is a wide empty gap between the largest
phone (440) and the smallest tablet (744) to put a threshold in. So:

```css
(min-width: 600px) and (min-height: 600px)
```

Both dimensions are tested, which makes the answer orientation-independent by
construction and needs no `orientation:` query. A desktop window dragged to 500px tall is
treated as a phone and starts stacked; that is acceptable and self-correcting.

It lives in `frontend/src/components/shellLayout.ts` as `WIDE_SHELL_QUERY`.

**This is a third breakpoint, deliberately.** `min-[820px]:` in `TaskList` and
`min-[1140px]:` in `PrimaryNav` (task-292) are both real numbers in this codebase and
neither expresses device class: 820 catches no phone but excludes the iPad mini in
portrait, and 1140 excludes every tablet in portrait. Those two ask "does this content fit
on one line". This one asks "is this a phone". **Do not consolidate them.**

Rejected: a user-agent check, and `pointer: coarse`. More precise in principle, wrong in
practice — `pointer: coarse` is true for a touchscreen laptop and for an iPad with a
trackpad, and UA sniffing needs maintaining forever.

## Consequences the implementation has to respect

- **Each region owns its scrolling and the page owns none.** At the two-region shell the
  surface is exactly one viewport tall. Two regions that scroll the page as one unit is
  the failure mode that makes a sidebar feel wrong: reading to the bottom of a record
  would carry the list off the top of the screen.
- **A drag scrolls the region, not the window.** `startDragAutoScroll` takes an element
  and finds the scrollable box around it, falling back to the window in the stacked
  shell. A loop hard-wired to `window.scrollBy` moves nothing here.
- **The broken-file and broken-queue banners live outside both regions.** Inside either
  scrollport they could be scrolled past, and would be invisible from the other region
  entirely.
- **A narrow list is a narrow *box*, not a narrow window.** The task list restacks into
  cards on a container query rather than the viewport one (`stackWhenNarrow` on
  `ResponsiveTable`). A `table-layout: fixed` table gives its one flexible column
  whatever the fixed ones leave, and six columns totalling 39.5rem leave a ~430px region
  nothing — so the task title collapses to zero pixels and the list renders as a column
  of blanks. Only the task list opts in: switching every table over would move the
  boundary by the shell's own padding and restack tables that fit today.
- **The detail region keeps a readable measure.** `max-w-7xl` comes off the surface, but
  a record set in 200-character lines on a 2560px monitor is not an improvement on the
  centred column it replaced.

## What is still open

| Task | What it adds |
|---|---|
| task-238 | The collapsible tree, selection, keyboard movement, the toggle, and the `localStorage` key that makes the default rule above only a default. Also the row a narrow column deserves — today it is the existing six-column row as cards, which is tall. |
| task-239 | Fitting the record to a bounded panel. Check it at ~700px, which is what a 1024px tablet leaves once the list has its share. |
| task-240 | The sandbox review, in a browser, on Jeff's own screen. |
| task-356 | The filter row behind a filter button, so four controls do not push the first task row below the fold of a narrow column. |
