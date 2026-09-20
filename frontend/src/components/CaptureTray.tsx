import { Link } from "react-router-dom";

import {
  batchSummary,
  errorFor,
  inCollectedOrder,
  submitLabel,
  type TrayFiling,
  type TrayItem,
} from "../report/tray";

/**
 * The collected findings, and the one button that files them (task-121).
 *
 * **It is a list of what has not been sent, not a list of issues.** Everything here is
 * unsent composition state; the instant the server confirms one it leaves this list and
 * exists as an ordinary task, linked below. There is no second population of findings to
 * keep in step with the backlog, which is the constraint the whole epic is built on.
 *
 * **A failure stays exactly where it was, with its prose and its screenshots.** That is
 * the reason this is a list of cards rather than a progress bar: after a partial batch a
 * person has to see which three of fifteen did not land and why, and the answer has to
 * be attached to the card they typed rather than summarised above it. Pressing the
 * button again sends only what is still here, each item carrying the `operation_id` it
 * was collected with, so a success the browser never saw resolves to the task the first
 * attempt made rather than to a duplicate.
 *
 * **It sits above the form, and the card list has a ceiling.** Below it and unbounded was
 * the first arrangement, and three findings on a 1100px-tall window already put both the
 * list and the button off the bottom of the dialog: the badge said three and there was
 * nothing on screen to press or to check. So the count and the one button that files
 * them are the first thing in the dialog, and the cards scroll within a bounded box
 * rather than pushing the form somewhere a review pass has to hunt for it.
 */

type CaptureTrayProps = {
  items: ReadonlyArray<TrayItem>;
  /**
   * Every task this dialog has created, oldest first.
   *
   * Accumulated across batches, and deliberately a different lifetime from
   * {@link CaptureTrayProps.lastBatch}: these items are gone from the tray, so these
   * links are the only thing on screen saying what happened to them.
   */
  filedInSession: ReadonlyArray<TrayFiling>;
  /** The most recent batch's outcomes: what each card's error says, and the summary. */
  lastBatch: ReadonlyArray<TrayFiling>;
  /** Non-null while a batch is in flight. */
  progress: { done: number; total: number } | null;
  /** Display name for a project id, so a card names where it is going. */
  projectName: (projectId: string) => string;
  onSubmit: () => void;
  onRemove: (itemId: string) => void;
  /** Called when a created task's link is followed, so the dialog can close. */
  onNavigate: () => void;
  /** False when this browser will not keep the list across a reload. */
  durable: boolean;
};

function taskHref(projectId: string, taskId: string) {
  return `/p/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}`;
}

export function CaptureTray({
  items,
  filedInSession,
  lastBatch,
  progress,
  projectName,
  onSubmit,
  onRemove,
  onNavigate,
  durable,
}: CaptureTrayProps) {
  const filed = filedInSession;
  const ordered = inCollectedOrder(items);
  const busy = progress !== null;

  if (ordered.length === 0 && filed.length === 0) return null;

  return (
    <div className="space-y-4">
      {ordered.length > 0 && (
        <section
          aria-label="Collected findings"
          className="rounded-lg border border-dark-border bg-dark-bg p-4"
        >
          {/*
            The count and the button, before the cards: these two are what has to be on
            screen at every length of list, and putting them after fifteen cards is what
            put them off the bottom of the dialog.
          */}
          <div className="mobile-action-row flex flex-wrap items-center justify-between gap-3">
            <h3 className="text-lg font-semibold">
              Collected findings{" "}
              <span className="text-sm font-normal text-dark-muted">({ordered.length})</span>
            </h3>
            <button
              type="button"
              onClick={onSubmit}
              disabled={busy}
              className="touch-target rounded-lg bg-blue-600 px-5 font-semibold text-white hover:bg-blue-500 disabled:opacity-60"
            >
              {busy ? "Creating…" : submitLabel(ordered.length)}
            </button>
          </div>
          {!durable && (
            <p className="mt-1 text-xs text-amber-300">
              This browser is not storing the list; a reload will lose it.
            </p>
          )}

          {/*
            Bounded and scrolling. A review pass collects into a list it does not need to
            read, and an unbounded one pushes the form -- the thing it is typing into --
            off the screen by the fourth finding.
          */}
          <ul className="mt-3 max-h-64 space-y-2 overflow-y-auto">
            {ordered.map((item) => {
              const error = errorFor(item.id, lastBatch);
              return (
                <li
                  key={item.id}
                  className={`rounded-lg border p-3 ${
                    error ? "border-red-500/60 bg-red-950/30" : "border-dark-border bg-dark-surface"
                  }`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="truncate font-medium">{item.request.title}</p>
                      <p className="mt-0.5 text-xs text-dark-muted">
                        {projectName(item.projectId)} · captured at{" "}
                        <code className="break-all">{item.route}</code>
                        {item.attachments.length > 0 && (
                          <>
                            {" · "}
                            {item.attachments.length === 1
                              ? "1 screenshot"
                              : `${item.attachments.length} screenshots`}
                          </>
                        )}
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => onRemove(item.id)}
                      disabled={busy}
                      className="shrink-0 rounded px-2 py-1 text-xs text-dark-muted hover:text-red-300 disabled:opacity-60"
                    >
                      Remove
                    </button>
                  </div>

                  {item.attachments.length > 0 && (
                    <ul
                      aria-label={`Screenshots for ${item.request.title}`}
                      className="mt-2 flex flex-wrap gap-2"
                    >
                      {item.attachments.map((attachment) => (
                        <li key={attachment.id}>
                          <img
                            src={attachment.preview}
                            alt={attachment.label}
                            className="h-12 w-16 rounded border border-dark-border object-cover"
                          />
                        </li>
                      ))}
                    </ul>
                  )}

                  {error && (
                    <p role="alert" className="mt-2 text-sm text-red-200">
                      {error}
                    </p>
                  )}
                </li>
              );
            })}
          </ul>

        </section>
      )}

      {/*
        The progress and the summary go through one live region, and it lives outside the
        list rather than inside it. A clean batch empties the tray, so a status line
        rendered within it would unmount at the moment it had something to announce --
        which is exactly the press a screen reader needs told. `aria-live` rather than
        `role="alert"`: this is the expected outcome of a button press, not an
        interruption.
      */}
      {(progress !== null || lastBatch.length > 0) && (
        <p aria-live="polite" className="text-sm text-dark-muted">
          {progress
            ? `Creating ${Math.min(progress.done + 1, progress.total)} of ${progress.total}…`
            : batchSummary(lastBatch)}
        </p>
      )}

      {filed.length > 0 && (
        <section
          aria-label="Tasks created"
          className="rounded-lg border border-emerald-600/50 bg-emerald-950/30 p-4"
        >
          <h3 className="text-sm font-semibold text-emerald-200">
            {filed.length === 1 ? "1 task created" : `${filed.length} tasks created`}
          </h3>
          <ul className="mt-2 space-y-1 text-sm">
            {filed.map((filing) => (
              <li key={filing.itemId} data-task-id={filing.taskId}>
                <Link
                  to={taskHref(filing.projectId, filing.taskId!)}
                  onClick={onNavigate}
                  className="font-mono text-emerald-300 underline hover:text-emerald-200"
                >
                  {filing.taskId}
                </Link>{" "}
                <span className="text-dark-muted">{filing.title}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
