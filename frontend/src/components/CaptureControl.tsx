import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useMatch } from "react-router-dom";

import type { Task } from "../api/generated";
import {
  createTaskApiProjectsProjectIdTasksPostMutation,
  dispatchTaskEndpointApiProjectsProjectIdTasksTaskIdDispatchPostMutation,
  getDispatchStateApiProjectsProjectIdDispatchGetOptions,
  getProjectsApiProjectsGetOptions,
  listTasksApiProjectsProjectIdTasksGetOptions,
} from "../api/generated/@tanstack/react-query.gen";
import { readRefusal } from "../api/mutation-error";
import { newOperationId } from "../api/operationId";
import { readReportContext } from "../report/issueReport";
import {
  inCollectedOrder,
  nextOrder,
  trayRequest,
  type TrayFiling,
  type TrayItem,
} from "../report/tray";
import { trayStore } from "../report/trayStore";
import { CaptureForm, type CaptureDestination } from "./CaptureForm";
import { CaptureTray } from "./CaptureTray";
import { FiledNotice, type FiledOutcome } from "./DispatchOnCreate";

/**
 * The one capture control (task-346): the single place anything becomes a task.
 *
 * **Create and Report issue were the same act wearing two costumes** -- a nav
 * destination and a floating button in the opposite corner -- and this is what they
 * merged into. One trigger, one dialog, one form that opens small and expands to the
 * whole specification in place. What sits behind it is {@link CaptureForm}, which the
 * `/tasks/new` route renders as a page from the same file.
 *
 * **The trigger lives in the header's actions region, not among the destinations.** An
 * action in a row of destinations is what task-336 found people reading as the selected
 * tab, back when Create was the only coloured entry in the bar. It is also outside the
 * collapsible group, like task-338's attention badge, so the burger below
 * `NAV_INLINE_MIN_PX` never takes it away: on a 375px phone it is still one tap, in the
 * corner, on every screen.
 *
 * **It replaced a `fixed bottom-4 right-4` button, and that corner was the reason.**
 * The floating button was a fine target, but task-295 has the bottom-right occupied by
 * the iOS home indicator, and a button hovering over the page is what made a cheap
 * capture look like a separate feature from authoring a task.
 *
 * **The dialog is portalled to `document.body`, and that is load-bearing.** The trigger
 * is inside the header, and the header is `sticky z-30` -- which makes it a stacking
 * context, so an overlay rendered as a child of it could never paint above anything
 * outside it however high its own `z-index` went. Caught in a browser by
 * `e2e/pinned-header.spec.ts`, whose hit test at the top of the screen found the header
 * over the scrim. A portal also keeps a `role="dialog"` out of the `navigation`
 * landmark, where it does not belong. `PrimaryNav` stays `z-30` for the same reason it
 * always was: below the `z-50` this overlay carries at the body.
 */

/** A plus, drawn rather than imported, matching ActionsMenu's kebab in weight and box. */
function PlusIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <path
        d="M10 3v14M3 10h14"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** What the form hands over when a finding goes on the list. */
type Collected = Omit<TrayItem, "id" | "order">;

export type CaptureTrayHandle = {
  items: ReadonlyArray<TrayItem>;
  add: (collected: Collected) => void;
  remove: (itemId: string) => void;
  durable: boolean;
};

/**
 * The collected findings, loaded from this device and written back as they change
 * (task-121).
 *
 * **Held here rather than in the dialog**, because the count has to be visible on the
 * trigger. A tray that only exists while a modal is open is a tray nobody can tell they
 * still have -- which is the same leak as not persisting it, arriving one step later.
 *
 * React state is what renders; the store is a write-through copy. A refused write
 * therefore costs durability and not the capture, which is why nothing here awaits one.
 */
function useCaptureTray(): CaptureTrayHandle {
  const store = useMemo(() => trayStore(), []);
  const [items, setItems] = useState<Array<TrayItem>>([]);

  useEffect(() => {
    let alive = true;
    void store.load().then((stored) => {
      // Merged in front of whatever has been collected since the load was asked for,
      // rather than assigned over it: the load is asynchronous and a fast Ctrl+Enter can
      // land first. Two items can then hold the same `order`, which `inCollectedOrder`
      // resolves by array position -- `sort` is stable -- so the stored ones stay first.
      if (alive)
        setItems((was) => [
          ...stored.filter((item) => !was.some((seen) => seen.id === item.id)),
          ...was,
        ]);
    });
    return () => {
      alive = false;
    };
  }, [store]);

  // `items` is a dependency rather than read inside the updater: React double-invokes an
  // updater in development, and minting an id in there would store two records for one
  // collect.
  const add = useCallback(
    (collected: Collected) => {
      const item: TrayItem = { id: newOperationId(), order: nextOrder(items), ...collected };
      setItems((was) => [...was, item]);
      void store.put(item);
    },
    [items, store],
  );

  const remove = useCallback(
    (itemId: string) => {
      setItems((was) => was.filter((item) => item.id !== itemId));
      void store.remove([itemId]);
    },
    [store],
  );

  return { items, add, remove, durable: store.durable };
}

/**
 * The trigger, and the dialog it opens.
 *
 * Rendered inside the header by `PrimaryNav`, and beside the routes by
 * {@link GlobalCapture} for the pages that have no header. Exactly one of the two is on
 * screen at a time.
 */
export function CaptureControl({ className = "" }: { className?: string }) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const tray = useCaptureTray();
  const waiting = tray.items.length;

  // By hand, because the overlay is not a browser dialog: the node holding focus is
  // about to leave the document, and the browser would otherwise leave focus on
  // nothing, so the next Tab would start from the top of the page.
  const close = () => {
    setOpen(false);
    triggerRef.current?.focus();
  };

  return (
    <div className={`relative shrink-0 ${className}`}>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-expanded={open}
        // The count is in the name, not only in the badge. A list waiting to be filed is
        // the one thing about this control that is not obvious from its glyph, and a
        // screen reader gets nothing from a coloured dot.
        aria-label={
          waiting > 0 ? `New task or issue (${waiting} collected)` : "New task or issue"
        }
        title={waiting > 0 ? `New task or issue — ${waiting} collected` : "New task or issue"}
        // No accent, and that is task-336 rather than a style preference: blue in
        // this bar means "you are here" and nothing else, which is what stopped a
        // coloured Create link reading as the selected tab. The glyph and the corner
        // carry the affordance, and matching the kebab makes the two read as one
        // actions group rather than as two unrelated controls.
        className="touch-target rounded-md px-3 text-dark-text hover:bg-dark-border focus:outline-none focus:ring-1 focus:ring-blue-400"
      >
        <PlusIcon />
      </button>
      {waiting > 0 && (
        <span
          aria-hidden="true"
          className="pointer-events-none absolute -right-0.5 -top-0.5 min-w-4 rounded-full bg-blue-500 px-1 text-center text-[10px] font-bold leading-4 text-white"
        >
          {waiting}
        </span>
      )}
      {open && createPortal(<CaptureDialog onClose={close} tray={tray} />, document.body)}
    </div>
  );
}

type Filed = { projectId: string; outcome: FiledOutcome };

function CaptureDialog({ onClose, tray }: { onClose: () => void; tray: CaptureTrayHandle }) {
  const location = useLocation();
  const queryClient = useQueryClient();
  const projectsQuery = useQuery(getProjectsApiProjectsGetOptions());
  const create = useMutation(createTaskApiProjectsProjectIdTasksPostMutation());

  // Captured once, when the dialog opens. Where you were is what you were looking at
  // when you noticed, and it must not drift if the page updates behind the overlay.
  const openedAt = useRef(location.pathname);
  const context = useMemo(() => readReportContext(openedAt.current), []);

  const [filed, setFiled] = useState<Filed | null>(null);
  // Bumped to remount the form for a second capture, which is how every field, every
  // attachment and the expansion state are reset without a reset function that has to
  // be kept in step with the form's state.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const destinations: Array<CaptureDestination> = (projectsQuery.data ?? []).map((project) => ({
    id: project.id,
    name: project.name,
    reporter: project.default_user ?? null,
  }));
  /** The human a run filed into this project would be attributed to. */
  const reporterFor = (projectId: string) =>
    destinations.find((entry) => entry.id === projectId)?.reporter ?? null;

  // Only for the Parent field's completions, so it is not asked for until the
  // specification is open and that field is on screen. Most captures never expand, and
  // fetching a project's whole task list to put a dialog up would be a request nobody
  // asked for.
  const [wantsTaskIds, setWantsTaskIds] = useState(false);
  // Which project the form is pointed at now, so the start-an-agent box is gated on the
  // gates of the project actually selected rather than the one the capture started in.
  const [destination, setDestination] = useState(context.projectId ?? "");
  const dispatchState = useQuery({
    ...getDispatchStateApiProjectsProjectIdDispatchGetOptions({
      path: { project_id: destination },
    }),
    enabled: Boolean(destination),
  });
  const start = useMutation(
    dispatchTaskEndpointApiProjectsProjectIdTasksTaskIdDispatchPostMutation(),
  );
  const tasksQuery = useQuery({
    ...listTasksApiProjectsProjectIdTasksGetOptions({
      path: { project_id: context.projectId ?? "" },
    }),
    enabled: wantsTaskIds && Boolean(context.projectId),
  });

  /** Every task this dialog has created, so a batch and a retry both leave a link. */
  const [filedInSession, setFiledInSession] = useState<Array<TrayFiling>>([]);
  /** The last batch's outcomes: each card's error, and the sentence under the list. */
  const [lastBatch, setLastBatch] = useState<Array<TrayFiling>>([]);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);

  /**
   * File the whole tray: one user action, N ordinary creates, one outcome per card.
   *
   * **Sequential on purpose.** Creation takes the project's creation and queue locks to
   * decide the next id and the bottom of the band, so fifteen parallel posts would
   * queue on the server anyway -- and would report their failures in an order nobody can
   * map back to the list. One at a time also means a card is removed the moment its own
   * task exists, so a batch interrupted half way through has left exactly the half it
   * confirmed.
   *
   * **Nothing is removed that was not confirmed**, and nothing is retried that was.
   * Those are the two halves of retry safety; the third is the `operation_id` each item
   * has carried since it was collected, which is what covers a create that succeeded on
   * a request whose answer never arrived.
   */
  const submitTray = async () => {
    const queue = inCollectedOrder(tray.items);
    if (queue.length === 0 || progress !== null) return;
    setLastBatch([]);
    setProgress({ done: 0, total: queue.length });
    const outcomes: Array<TrayFiling> = [];
    for (const item of queue) {
      const shared = {
        itemId: item.id,
        title: item.request.title,
        projectId: item.projectId,
      };
      try {
        const task = await create.mutateAsync({
          path: { project_id: item.projectId },
          body: trayRequest(item),
        });
        outcomes.push({ ...shared, taskId: task.id, error: null });
        setFiledInSession((was) => [...was, { ...shared, taskId: task.id, error: null }]);
        // Confirmed, so it stops being unsent composition state -- here and on disk.
        tray.remove(item.id);
      } catch (caught) {
        const refusal = readRefusal(caught);
        outcomes.push({
          ...shared,
          taskId: null,
          error: refusal
            ? refusal.message
            : "It could not be filed. Check the server, then press the button again.",
        });
      }
      setLastBatch([...outcomes]);
      setProgress({ done: outcomes.length, total: queue.length });
    }
    setProgress(null);
    void queryClient.invalidateQueries();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 p-4 sm:items-center">
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="capture-heading"
        className="max-h-full w-full max-w-xl overflow-y-auto rounded-2xl border border-dark-border bg-dark-surface p-5"
      >
        <h2 id="capture-heading" className="text-2xl font-bold">
          New task
        </h2>
        <p className="mt-1 text-sm text-dark-muted">
          Two sentences files what you just noticed, tagged <code>reported-issue</code> and
          carrying where you were. Add the full specification when it deserves one, or add
          this to the list and keep going.
        </p>

        {filed ? (
          <div className="mt-5 space-y-4">
            <FiledNotice
              taskId={filed.outcome.task.id}
              taskTitle={filed.outcome.task.title}
              taskHref={`/p/${encodeURIComponent(filed.projectId)}/tasks/${encodeURIComponent(filed.outcome.task.id)}`}
              start={filed.outcome.start}
              onNavigate={onClose}
            />
            <div className="mobile-action-row flex items-center justify-end gap-3">
              <button
                type="button"
                onClick={() => {
                  setFiled(null);
                  setAttempt((count) => count + 1);
                }}
                className="touch-target rounded-lg px-4 font-semibold text-dark-muted hover:bg-dark-border"
              >
                File another
              </button>
              <button
                type="button"
                onClick={onClose}
                className="touch-target rounded-lg bg-blue-600 px-5 font-semibold text-white hover:bg-blue-500"
              >
                Done
              </button>
            </div>
          </div>
        ) : (
          <div className="mt-5">
            <CaptureForm
              key={attempt}
              context={context}
              destinations={destinations}
              existingTaskIds={(tasksQuery.data ?? []).map((task) => task.id)}
              onExpandedChange={(expanded) => {
                if (expanded) setWantsTaskIds(true);
              }}
              autoFocus
              onSubmit={async (projectId, request) => {
                try {
                  return await create.mutateAsync({
                    path: { project_id: projectId },
                    body: request,
                  });
                } catch (caught) {
                  // A refusal carries a sentence written for a person; anything else
                  // gets the one the form falls back to.
                  const refusal = readRefusal(caught);
                  throw new Error(refusal ? refusal.message : "");
                }
              }}
              dispatchState={dispatchState.data ?? null}
              onDestinationChange={setDestination}
              // The ordinary dispatch call, with the ordinary body: `user` names the
              // human filing, and the guard layer writes their authorising entry and
              // checks it like any other. Nothing here is exempt from anything.
              onStart={(projectId, taskId) =>
                start.mutateAsync({
                  path: { project_id: projectId, task_id: taskId },
                  body: reporterFor(projectId) ? { user: reporterFor(projectId)! } : {},
                })
              }
              onFiled={(projectId, outcome) => {
                void queryClient.invalidateQueries();
                setFiled({ projectId, outcome });
              }}
              // Collect and stay: the form remounts empty with focus back in Title, so a
              // review pass adds a finding without the dialog closing and without a hand
              // leaving the keyboard. The remount is the same mechanism "File another"
              // uses, rather than a reset that has to track the form's state.
              onCollect={(collected) => {
                tray.add(collected);
                setAttempt((count) => count + 1);
              }}
              cancel={
                <button
                  type="button"
                  onClick={onClose}
                  className="touch-target rounded-lg px-4 font-semibold text-dark-muted hover:bg-dark-border"
                >
                  Cancel
                </button>
              }
            />
          </div>
        )}

        {/*
          Below the form, not above it: the reading order of a review pass is compose,
          then see what you have built, then file the lot. It stays on screen through the
          single-capture receipt too, so a tray collected earlier cannot be lost behind a
          "Filed as" card.
        */}
        <div className="mt-5">
          <CaptureTray
            items={tray.items}
            filedInSession={filedInSession}
            lastBatch={lastBatch}
            progress={progress}
            durable={tray.durable}
            projectName={(projectId) =>
              destinations.find((entry) => entry.id === projectId)?.name ?? projectId
            }
            onSubmit={() => void submitTray()}
            onRemove={tray.remove}
            onNavigate={onClose}
          />
        </div>
      </section>
    </div>
  );
}

/**
 * The control for the pages that have no header to put it in.
 *
 * `/not-found`, the registry-unreachable card and the no-projects card all render
 * before any project resolves, so `PrimaryNav` -- and with it the trigger -- is not
 * there. A finding about the project picker has nowhere else to go, so the coverage the
 * old floating button had is kept rather than dropped.
 *
 * It renders **nothing** inside the project shell, where the header already carries the
 * control: the URL is the test, because this component sits beside the routes and has
 * no other way to know whether a header rendered. Pinned where the header would be, so
 * the control is in the same corner of the screen on every page in the app.
 */
export function GlobalCapture() {
  const insideProject = useMatch("/p/:projectId/*");
  if (insideProject) return null;
  return <CaptureControl className="fixed right-4 top-3 z-40" />;
}
