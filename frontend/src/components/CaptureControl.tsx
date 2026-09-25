import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useMatch } from "react-router-dom";

import type { Task } from "../api/generated";
import {
  createTaskApiProjectsProjectIdTasksPostMutation,
  dispatchTaskEndpointApiProjectsProjectIdTasksTaskIdDispatchPostMutation,
  draftTaskSpecApiProjectsProjectIdModelDraftPostMutation,
  getDispatchStateApiProjectsProjectIdDispatchGetOptions,
  getModelStatusApiModelGetOptions,
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
import { mergeDraft } from "../report/trayDraft";
import { draftStore, type CaptureDraft } from "../report/draftStore";
import { trayStore } from "../report/trayStore";
import { setUnsentComposition } from "../unsentComposition";
import { CaptureForm, type CaptureDestination } from "./CaptureForm";
import { CaptureTray } from "./CaptureTray";
import { FiledNotice, type FiledOutcome } from "./DispatchOnCreate";
import { EmergencyStop, StoppedBanner } from "./EmergencyStop";

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
  /** The items as they are right now, for a callback that outlived its render. */
  current: () => Array<TrayItem>;
  add: (collected: Collected) => TrayItem;
  /** Replace one item. A no-op once it has been removed or filed. */
  update: (item: TrayItem) => void;
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
  /**
   * The same list, readable synchronously.
   *
   * A draft comes back a few seconds after the collect that asked for it, by which time
   * the person may have removed that card or filed the batch. Both the write-back and
   * the submit therefore need to know what is on the tray *now* rather than what was on
   * it when a closure was made -- and a stale answer here would resurrect a removed
   * finding on disk. Maintained by every mutator below rather than by an effect, so it
   * is never a render behind.
   */
  const held = useRef<Array<TrayItem>>([]);

  const commit = useCallback((next: Array<TrayItem>) => {
    held.current = next;
    setItems(next);
  }, []);

  useEffect(() => {
    let alive = true;
    void store.load().then((stored) => {
      // Merged in front of whatever has been collected since the load was asked for,
      // rather than assigned over it: the load is asynchronous and a fast Ctrl+Enter can
      // land first. Two items can then hold the same `order`, which `inCollectedOrder`
      // resolves by array position -- `sort` is stable -- so the stored ones stay first.
      if (!alive) return;
      const seen = held.current;
      commit([...stored.filter((item) => !seen.some((one) => one.id === item.id)), ...seen]);
    });
    return () => {
      alive = false;
    };
  }, [store, commit]);

  const add = useCallback(
    (collected: Collected) => {
      const item: TrayItem = {
        id: newOperationId(),
        order: nextOrder(held.current),
        ...collected,
      };
      commit([...held.current, item]);
      void store.put(item);
      return item;
    },
    [commit, store],
  );

  const update = useCallback(
    (item: TrayItem) => {
      // Dropped on the floor when the card is gone. A draft that resolved after its
      // finding was removed or filed must not put it back, on screen or on disk.
      if (!held.current.some((one) => one.id === item.id)) return;
      commit(held.current.map((one) => (one.id === item.id ? item : one)));
      void store.put(item);
    },
    [commit, store],
  );

  const remove = useCallback(
    (itemId: string) => {
      commit(held.current.filter((item) => item.id !== itemId));
      void store.remove([itemId]);
    },
    [commit, store],
  );

  return { items, current: () => held.current, add, update, remove, durable: store.durable };
}

/** The draft store key, and the name this dialog's claim on the reload is held under. */
const CAPTURE_DRAFT_KEY = "capture";

/**
 * How long a keystroke waits before it reaches the disk.
 *
 * Short enough that the window in which a reload loses a word is not worth reasoning
 * about, long enough that a typed sentence is one write rather than forty. A *clear* is
 * never debounced -- see below.
 */
const DRAFT_WRITE_MS = 400;

type CaptureDraftHandle = {
  /** Null while the stored draft is still being read; `{ draft }` once it is known. */
  restored: { draft: CaptureDraft | null } | null;
  /** What the form reports on every change, and null when it has nothing left to keep. */
  onChange: (draft: CaptureDraft | null) => void;
};

/**
 * The finding currently being typed, kept on this device (task-512).
 *
 * **Write late, forget immediately.** A save is debounced because it is a copy of state
 * the form already holds and 400ms of it is a word. A clear is not, because it is the
 * record of a finding that has just become something else: delaying that by even a
 * moment opens the window where a reload restores a draft of a finding already sitting
 * on the tray, which is the one duplicate this feature could produce.
 *
 * **The claim on the reload is released when the dialog closes**, not when the draft is
 * deleted. By then the text is on disk and the composition is not on screen, so a
 * reload costs nothing -- whereas a tab holding a stored draft would otherwise refuse to
 * reload for the rest of its life, which is not a guard but a leak.
 */
function useCaptureDraft(): CaptureDraftHandle {
  const store = useMemo(() => draftStore(), []);
  const [restored, setRestored] = useState<{ draft: CaptureDraft | null } | null>(null);
  /** Whether a record exists for this key, so an empty form does not write a delete. */
  const stored = useRef(false);
  const timer = useRef<number | null>(null);
  const pending = useRef<CaptureDraft | null>(null);

  useEffect(() => {
    let alive = true;
    void store.load(CAPTURE_DRAFT_KEY).then((draft) => {
      if (!alive) return;
      stored.current = draft !== null;
      setRestored({ draft });
    });
    return () => {
      alive = false;
    };
  }, [store]);

  const write = useCallback(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
    const draft = pending.current;
    pending.current = null;
    if (!draft) return;
    stored.current = true;
    void store.save(CAPTURE_DRAFT_KEY, draft);
  }, [store]);

  const onChange = useCallback(
    (draft: CaptureDraft | null) => {
      setUnsentComposition(CAPTURE_DRAFT_KEY, draft !== null);
      if (draft === null) {
        pending.current = null;
        if (timer.current !== null) {
          window.clearTimeout(timer.current);
          timer.current = null;
        }
        if (!stored.current) return;
        stored.current = false;
        void store.clear(CAPTURE_DRAFT_KEY);
        return;
      }
      pending.current = draft;
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(write, DRAFT_WRITE_MS);
    },
    [store, write],
  );

  // Closing the dialog, or the page going away, must not be the one gesture that loses
  // the last few hundred milliseconds of typing.
  useEffect(() => {
    window.addEventListener("pagehide", write);
    return () => {
      window.removeEventListener("pagehide", write);
      write();
      setUnsentComposition(CAPTURE_DRAFT_KEY, false);
    };
  }, [write]);

  return { restored, onChange };
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

  const draft = useCaptureDraft();

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

  const modelStatus = useQuery(getModelStatusApiModelGetOptions());
  const draftSpec = useMutation(draftTaskSpecApiProjectsProjectIdModelDraftPostMutation());
  /**
   * The drafts still in the air, so a submit can wait for them.
   *
   * A ref rather than state: nothing renders from it, and it is written from callbacks
   * that outlive the render which created them.
   */
  const drafting = useRef(new Map<string, Promise<void>>());

  /**
   * Flesh one collected finding out, in the background (task-121).
   *
   * Started at collect time rather than at submit time, and that is the whole design
   * decision. Collecting is the idle time -- the next finding is being typed -- and
   * drafting fifteen findings at the moment somebody presses "Create 15 tasks" would put
   * a minute of waiting exactly where they wanted to be finished.
   *
   * **It can never stop a finding being filed.** Every failure path below writes a
   * reason onto the card and leaves the request exactly as it was typed, and the submit
   * waits for outstanding drafts rather than requiring them.
   */
  const flesh = (item: TrayItem, note: string) => {
    const run = (async () => {
      try {
        const result = await draftSpec.mutateAsync({
          path: { project_id: item.projectId },
          // The note as typed. The provenance footer in `request.description` is
          // AgentJobs talking about itself, and is not part of the finding.
          body: { title: item.request.title, description: note },
        });
        if (!result.drafted) {
          // A refusal is a 200 carrying its reason, so it reads as "no draft, here is
          // why" rather than as a failure.
          tray.update({
            ...item,
            draft: { state: "declined", detail: result.detail ?? "No draft was produced." },
          });
          return;
        }
        const model = modelStatus.data?.model ?? null;
        const merged = mergeDraft(item.request, result, model);
        tray.update({
          ...item,
          request: merged.request,
          draft: { state: "applied", model, filled: merged.filled },
        });
      } catch {
        tray.update({
          ...item,
          draft: {
            state: "declined",
            detail: "The model could not be reached. It will be filed as you typed it.",
          },
        });
      } finally {
        drafting.current.delete(item.id);
      }
    })();
    drafting.current.set(item.id, run);
  };

  /** Every task this dialog has created, so a batch and a retry both leave a link. */
  const [filedInSession, setFiledInSession] = useState<Array<TrayFiling>>([]);
  /** The last batch's outcomes: each card's error, and the sentence under the list. */
  const [lastBatch, setLastBatch] = useState<Array<TrayFiling>>([]);
  const [progress, setProgress] = useState<{
    done: number;
    total: number;
    /** Set while a submit is waiting on drafts that have not come back yet. */
    fleshing?: number;
  } | null>(null);

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
    if (tray.current().length === 0 || progress !== null) return;
    setLastBatch([]);
    // Wait for what is already in the air, then read the tray again: a draft that lands
    // during the wait has replaced its item's request, and filing the copy captured
    // before the wait would file the version the model had not expanded yet.
    const outstanding = [...drafting.current.values()];
    if (outstanding.length > 0) {
      setProgress({ fleshing: outstanding.length, done: 0, total: 0 });
      await Promise.allSettled(outstanding);
    }
    const queue = inCollectedOrder(tray.current());
    if (queue.length === 0) {
      setProgress(null);
      return;
    }
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

        {/*
          Above the form, not below it. Below and unbounded was the first arrangement and
          it failed the only test that matters here: three findings on a tall desktop
          window already put the list and its button off the bottom of the dialog, so the
          badge said three and there was nothing on screen to press. It also stays on
          screen through the single-capture receipt, so a list collected earlier is not
          lost behind a "Filed as" card.
        */}
        <div className="mt-5">
          <CaptureTray
            items={tray.items}
            filedInSession={filedInSession}
            lastBatch={lastBatch}
            progress={progress}
            durable={tray.durable}
            draftingUnavailable={
              modelStatus.data && modelStatus.data.available !== true
                ? (modelStatus.data.detail ?? "no model is configured on this machine.")
                : null
            }
            projectName={(projectId) =>
              destinations.find((entry) => entry.id === projectId)?.name ?? projectId
            }
            onSubmit={() => void submitTray()}
            onRemove={tray.remove}
            onNavigate={onClose}
          />
        </div>

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
            {/*
              Nothing is rendered until the stored draft has been read, which takes a
              tick. A form mounted before that answer arrived would have to be told its
              own initial values afterwards, and would fight whatever had been typed in
              the meantime.
            */}
            {draft.restored && (
              <CaptureForm
                key={attempt}
                context={context}
                // Restored into the first form of a session only. The second capture
                // starts empty by construction: the finding before it was collected,
                // which deleted the draft, and re-reading a deleted record is the one
                // way this could offer a duplicate.
                initialDraft={attempt === 0 ? draft.restored.draft : null}
                onDraftChange={draft.onChange}
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
                onCollect={({ note, wantsDraft, ...collected }) => {
                  // Declined up front, with the short reason, when there is nothing to ask
                  // or the person turned it off -- so the card states its own state rather
                  // than sitting on "pending" for a call that is never made.
                  const unavailable = modelStatus.data?.available !== true;
                  const item = tray.add({
                    ...collected,
                    draft:
                      !wantsDraft || unavailable
                        ? {
                            state: "declined",
                            detail: wantsDraft
                              ? "No model is configured, so it is filed as you typed it."
                              : "Fleshing out is switched off.",
                          }
                        : { state: "pending" },
                  });
                  if (wantsDraft && !unavailable) flesh(item, note);
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
            )}
          </div>
        )}

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
  // The stop rides with the capture trigger here too, in the same order as the header
  // (task-573): it has to be on every page, and these pages have no header to hold it.
  // The strip goes to the bottom here: the top-right corner is the controls', and a page
  // with no header has no bar for it to sit under.
  return (
    <>
      <div className="fixed right-4 top-3 z-40 flex items-center gap-1">
        <EmergencyStop />
        <CaptureControl />
      </div>
      <div className="fixed inset-x-0 bottom-0 z-40 pb-[env(safe-area-inset-bottom)]">
        <StoppedBanner />
      </div>
    </>
  );
}
