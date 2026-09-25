import { Suspense, lazy, useCallback, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { getDeliverableApiProjectsProjectIdTasksTaskIdDeliverablesIndexGetOptions } from "../api/generated/@tanstack/react-query.gen";
import type { TaskRead } from "../api/types";
import { readRefusal } from "../api/mutation-error";
import { FullscreenView, MaximizeButton } from "./FullscreenView";
import { useWideShell } from "./shellLayout";

const MarkdownDocument = lazy(() => import("./MarkdownDocument"));

/**
 * The gates at which a reviewer is judging the work itself. `plan` arrives with task-001;
 * listing it here costs nothing until then and means that task need not find this.
 */
const REVIEW_GATES = new Set(["review", "approval", "plan"]);

/** Whether a deliverable is one the panel renders, rather than lists. Mirrors the route. */
export function isMarkdown(path: string): boolean {
  return path.toLowerCase().endsWith(".md");
}

/**
 * "Documents under review" (task-594): each Markdown file in the task's `deliverables[]`,
 * rendered as it stands at the head of the task's active branch.
 *
 * A design review judges a document, not a diff, and before this the reviewer could
 * read an unmerged document only by checking out the branch -- which is not a thing a
 * phone does. So it is the phone this is laid out for:
 *
 * - **Collapsed on a phone, open elsewhere.** "Is this a phone" is `useWideShell`'s
 *   question, answered once for the whole app; a long document pushed above the verbs
 *   would bury Approve.
 * - **Fetched only when opened.** A collapsed document costs no request and no parser.
 * - **Where it was read, always shown**: branch and short commit, because "the document"
 *   is a claim about a moment, and the reviewer approving it should see which one.
 * - **A refusal is shown in the document's place** -- no branch, two branches, a missing
 *   file, too large -- in the route's own words. An empty section would say "nothing to
 *   read", which is a different and false claim.
 *
 * A deliverable that is not Markdown is listed as a path, with its note, and nothing more.
 */
export function ReviewDocuments({ task, projectId }: { task: TaskRead; projectId: string }) {
  const wide = useWideShell();
  const deliverables = task.deliverables ?? [];
  if (task.ball !== "human" || !REVIEW_GATES.has(task.ball_reason ?? "")) return null;
  if (deliverables.length === 0) return null;

  return (
    <section aria-label="Documents under review" className="space-y-2">
      <h3 className="text-sm font-semibold uppercase tracking-wide text-yellow-200">Documents under review</h3>
      <ul className="space-y-2">
        {deliverables.map((deliverable, index) => (
          <li key={`${index}:${deliverable.path}`}>
            {isMarkdown(deliverable.path) ? (
              <DocumentItem
                projectId={projectId}
                taskId={task.id}
                index={index}
                path={deliverable.path}
                note={deliverable.note ?? null}
                defaultOpen={wide}
              />
            ) : (
              <p className="rounded-lg border border-dark-border bg-dark-bg px-3 py-2 text-sm">
                <code className="break-all font-mono text-dark-text">{deliverable.path}</code>
                {deliverable.note && <span className="text-dark-muted"> — {deliverable.note}</span>}
                <span className="block text-xs text-dark-muted">Not Markdown; listed, not rendered.</span>
              </p>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function DocumentItem({
  projectId,
  taskId,
  index,
  path,
  note,
  defaultOpen,
}: {
  projectId: string;
  taskId: string;
  index: number;
  path: string;
  note: string | null;
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [maximized, setMaximized] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const restore = useCallback(() => setMaximized(false), []);
  return (
    <details
      open={open}
      onToggle={(event) => setOpen((event.currentTarget as HTMLDetailsElement).open)}
      className="rounded-lg border border-dark-border bg-dark-bg"
    >
      <summary className="flex cursor-pointer items-center gap-2 py-1 pl-3 pr-1 text-sm">
        <span className="min-w-0 flex-1">
          <code className="break-all font-mono text-dark-text">{path}</code>
          {note && <span className="text-dark-muted"> — {note}</span>}
        </span>
        {/* Inside the summary so it sits on the document's own line, which is why the
            click must not also toggle the <details>. Works collapsed too: on a phone,
            maximizing is the likely way to read it at all. */}
        <MaximizeButton
          ref={opener}
          label={`Read ${path} full screen`}
          onClick={(event) => {
            event.preventDefault();
            event.stopPropagation();
            setMaximized(true);
          }}
        />
      </summary>
      {open && <DocumentBody projectId={projectId} taskId={taskId} index={index} />}
      {maximized && (
        <FullscreenView title={path} subtitle={note ?? undefined} onClose={restore} returnFocus={opener}>
          <DocumentBody projectId={projectId} taskId={taskId} index={index} bare />
        </FullscreenView>
      )}
    </details>
  );
}

/** `bare` drops the card's rule and padding, for the full-screen view that has its own. */
function DocumentBody({
  projectId,
  taskId,
  index,
  bare = false,
}: {
  projectId: string;
  taskId: string;
  index: number;
  bare?: boolean;
}) {
  const edge = bare ? "" : "border-t border-dark-border";
  const inset = bare ? "" : "px-3";
  const query = useQuery({
    ...getDeliverableApiProjectsProjectIdTasksTaskIdDeliverablesIndexGetOptions({
      path: { project_id: projectId, task_id: taskId, index },
    }),
    // A commit to the branch moves no task revision, so the app-wide cache would keep
    // showing the old text. Reopening a document is how a reviewer asks again.
    staleTime: 0,
    retry: false,
  });

  if (query.isPending) {
    return <p className={`${edge} ${inset} py-2 text-sm text-dark-muted`}>Reading the branch…</p>;
  }
  if (query.isError) {
    const refusal = readRefusal(query.error);
    return (
      <p role="alert" className={`${edge} ${inset} py-2 text-sm text-red-300`}>
        {refusal ? refusal.message : "The document could not be read. Reload and try again."}
        {refusal && <> <span className="font-mono text-xs text-dark-muted">({refusal.code})</span></>}
      </p>
    );
  }
  const document = query.data;
  return (
    <div className={edge}>
      <p className={`${inset} pt-2 text-xs text-dark-muted`}>
        Read from <code className="break-all font-mono">{document.branch}</code> at{" "}
        <code className="font-mono">{document.commit}</code>
      </p>
      <div className={`${inset} pb-3`}>
        <Suspense fallback={<p className="py-2 text-sm text-dark-muted">Rendering…</p>}>
          <MarkdownDocument text={document.text} />
        </Suspense>
      </div>
    </div>
  );
}
