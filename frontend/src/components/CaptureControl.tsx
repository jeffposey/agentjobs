import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useMatch } from "react-router-dom";

import type { Task } from "../api/generated";
import {
  createTaskApiProjectsProjectIdTasksPostMutation,
  getProjectsApiProjectsGetOptions,
  listTasksApiProjectsProjectIdTasksGetOptions,
} from "../api/generated/@tanstack/react-query.gen";
import { readRefusal } from "../api/mutation-error";
import { readReportContext } from "../report/issueReport";
import { CaptureForm, type CaptureDestination } from "./CaptureForm";

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

  // By hand, because the overlay is not a browser dialog: the node holding focus is
  // about to leave the document, and the browser would otherwise leave focus on
  // nothing, so the next Tab would start from the top of the page.
  const close = () => {
    setOpen(false);
    triggerRef.current?.focus();
  };

  return (
    <div className={`shrink-0 ${className}`}>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label="New task or issue"
        title="New task or issue"
        // No accent, and that is task-336 rather than a style preference: blue in
        // this bar means "you are here" and nothing else, which is what stopped a
        // coloured Create link reading as the selected tab. The glyph and the corner
        // carry the affordance, and matching the kebab makes the two read as one
        // actions group rather than as two unrelated controls.
        className="touch-target rounded-md px-3 text-dark-text hover:bg-dark-border focus:outline-none focus:ring-1 focus:ring-blue-400"
      >
        <PlusIcon />
      </button>
      {open && createPortal(<CaptureDialog onClose={close} />, document.body)}
    </div>
  );
}

type Filed = { projectId: string; task: Task };

function CaptureDialog({ onClose }: { onClose: () => void }) {
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

  // Only for the Parent field's completions, so it is not asked for until the
  // specification is open and that field is on screen. Most captures never expand, and
  // fetching a project's whole task list to put a dialog up would be a request nobody
  // asked for.
  const [wantsTaskIds, setWantsTaskIds] = useState(false);
  const tasksQuery = useQuery({
    ...listTasksApiProjectsProjectIdTasksGetOptions({
      path: { project_id: context.projectId ?? "" },
    }),
    enabled: wantsTaskIds && Boolean(context.projectId),
  });

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
          carrying where you were. Add the full specification when it deserves one.
        </p>

        {filed ? (
          <div className="mt-5 space-y-4">
            <p
              role="status"
              className="rounded-lg border border-green-600/60 bg-green-950/40 p-4 text-green-200"
            >
              Filed as <strong>{filed.task.id}</strong>.
            </p>
            <div className="mobile-action-row flex items-center justify-end gap-3">
              <Link
                to={`/p/${encodeURIComponent(filed.projectId)}/tasks/${encodeURIComponent(filed.task.id)}`}
                onClick={onClose}
                className="touch-target rounded-lg px-4 font-semibold text-blue-300 hover:bg-dark-border"
              >
                Open the task
              </Link>
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
              onFiled={(projectId, task) => {
                void queryClient.invalidateQueries();
                setFiled({ projectId, task });
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
