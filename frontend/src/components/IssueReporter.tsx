import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";

import {
  createTaskApiProjectsProjectIdTasksPostMutation,
  getProjectsApiProjectsGetOptions,
} from "../api/generated/@tanstack/react-query.gen";
import { readRefusal } from "../api/mutation-error";
import { toUploads, type PendingAttachment } from "../report/attachments";
import { buildIssueTaskRequest, readReportContext } from "../report/issueReport";
import { AttachmentPicker } from "./AttachmentPicker";

/**
 * Report Issue: global chrome that turns something you just noticed into a task.
 *
 * Rendered beside the router rather than inside a page, so it is present on the
 * project picker and on a status card as well as on a task -- ac-1 asks for every
 * page, and any placement inside a route element misses the pages that render before
 * a project resolves. It is deliberately not the review panel: that panel means "the
 * ball is with you on this task", while an issue usually is not about the task on
 * screen, and one control that changes meaning with page state is the thing being
 * avoided.
 */

const inputClass =
  "touch-target mt-1 w-full rounded-lg border border-dark-border bg-dark-bg px-3 py-2 text-dark-text placeholder:text-dark-muted focus:border-blue-500 focus:outline-none";

type Submitted = { projectId: string; taskId: string };

export function IssueReporter() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="touch-target fixed bottom-4 right-4 z-40 rounded-full border border-dark-border bg-dark-surface px-4 text-sm font-semibold text-blue-300 shadow-lg hover:bg-dark-border"
      >
        Report issue
      </button>
      {open && <IssueReporterDialog onClose={() => setOpen(false)} />}
    </>
  );
}

function IssueReporterDialog({ onClose }: { onClose: () => void }) {
  const location = useLocation();
  const queryClient = useQueryClient();
  const projectsQuery = useQuery(getProjectsApiProjectsGetOptions());
  const create = useMutation(createTaskApiProjectsProjectIdTasksPostMutation());
  const titleRef = useRef<HTMLInputElement>(null);

  // Captured once, when the dialog opens. Where the reporter was is what they were
  // looking at when they noticed, and it must not drift if the page updates behind
  // the overlay.
  const openedAt = useRef(location.pathname);
  const context = useMemo(() => readReportContext(openedAt.current), []);

  const projects = projectsQuery.data ?? [];
  const [destination, setDestination] = useState(context.projectId ?? "");
  const [title, setTitle] = useState("");
  const [details, setDetails] = useState("");
  const [actionable, setActionable] = useState(false);
  const [attachments, setAttachments] = useState<Array<PendingAttachment>>([]);
  const [error, setError] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState<Submitted | null>(null);

  useEffect(() => {
    titleRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Fall back to the first registered project only when the reporter was on a page
  // with no project of its own; never silently override the one they were viewing.
  const effectiveDestination = destination || projects[0]?.id || "";
  const destinationProject = projects.find((project) => project.id === effectiveDestination);
  const reporter = destinationProject?.default_user ?? null;

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!reporter || !effectiveDestination) return;
    setError(null);
    try {
      const task = await create.mutateAsync({
        path: { project_id: effectiveDestination },
        body: buildIssueTaskRequest({
          draft: { title, details, actionable },
          context,
          destinationProjectId: effectiveDestination,
          reporter,
          // A retry after a timeout resolves to the task the first attempt made
          // instead of filing the same finding twice.
          operationId: crypto.randomUUID(),
          attachments: toUploads(attachments),
        }),
      });
      await queryClient.invalidateQueries();
      setSubmitted({ projectId: effectiveDestination, taskId: task.id });
    } catch (caught) {
      const refusal = readRefusal(caught);
      setError(
        refusal
          ? refusal.message
          : "The issue could not be filed. Check the server, then try again.",
      );
    }
  };

  const reportAnother = () => {
    setSubmitted(null);
    setTitle("");
    setDetails("");
    setActionable(false);
    setAttachments([]);
    setError(null);
    titleRef.current?.focus();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 p-4 sm:items-center">
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="report-issue-heading"
        className="max-h-full w-full max-w-xl overflow-y-auto rounded-2xl border border-dark-border bg-dark-surface p-5"
      >
        <h2 id="report-issue-heading" className="text-2xl font-bold">
          Report an issue
        </h2>
        <p className="mt-1 text-sm text-dark-muted">
          It becomes a normal task, tagged <code>reported-issue</code>, carrying where you were.
        </p>

        {submitted ? (
          <div className="mt-5 space-y-4">
            <p
              role="status"
              className="rounded-lg border border-green-600/60 bg-green-950/40 p-4 text-green-200"
            >
              Filed as <strong>{submitted.taskId}</strong>.
            </p>
            <div className="mobile-action-row flex items-center justify-end gap-3">
              <Link
                to={`/p/${encodeURIComponent(submitted.projectId)}/tasks/${encodeURIComponent(submitted.taskId)}`}
                onClick={onClose}
                className="touch-target rounded-lg px-4 font-semibold text-blue-300 hover:bg-dark-border"
              >
                Open the task
              </Link>
              <button
                type="button"
                onClick={reportAnother}
                className="touch-target rounded-lg px-4 font-semibold text-dark-muted hover:bg-dark-border"
              >
                Report another
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
          <form onSubmit={(event) => void submit(event)} className="mt-5 space-y-4">
            {error && (
              <div
                role="alert"
                className="rounded-lg border border-red-500/60 bg-red-950/50 p-4 text-red-200"
              >
                {error}
              </div>
            )}

            <label className="block font-medium">
              Title
              <input
                ref={titleRef}
                name="title"
                required
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                className={inputClass}
                placeholder="Task list filters match nothing"
              />
            </label>

            <AttachmentPicker
              label="What happened"
              hint="Enough for someone who was not here. The page you were on is recorded for you."
              value={details}
              onChange={setDetails}
              attachments={attachments}
              onAttachmentsChange={setAttachments}
              name="details"
              required
              textareaClassName={`${inputClass} min-h-28`}
            />

            <label className="block font-medium">
              File into project
              <span className="mt-1 block text-xs font-normal text-dark-muted">
                An issue about AgentJobs itself belongs in the AgentJobs project, not in whatever
                you happened to be reading.
              </span>
              <select
                aria-label="File into project"
                value={effectiveDestination}
                onChange={(event) => setDestination(event.target.value)}
                className={inputClass}
              >
                {projects.map((project) => (
                  <option value={project.id} key={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex items-center gap-3 font-medium">
              <input
                type="checkbox"
                checked={actionable}
                onChange={(event) => setActionable(event.target.checked)}
              />
              <span>
                Ready for an agent
                <span className="block text-xs font-normal text-dark-muted">
                  Off by default: a finding usually needs a person to decide it is worth doing.
                </span>
              </span>
            </label>

            <p className="rounded-lg border border-dark-border bg-dark-bg p-3 text-xs text-dark-muted">
              Reported from <code>{context.route}</code>
              {context.taskId && context.projectId === effectiveDestination ? (
                <>
                  {" "}
                  · links <code>{context.taskId}</code> as related
                </>
              ) : null}
              {reporter ? (
                <>
                  {" "}
                  · filing as <strong className="text-dark-text">{reporter}</strong>
                </>
              ) : null}
            </p>

            {!reporter && (
              <p
                role="alert"
                className="rounded-lg border border-amber-500/60 bg-amber-950/40 p-4 text-sm text-amber-200"
              >
                No single human actor is configured for this project, so an issue filed here could
                not say who filed it. Add one entry with <code>kind: human</code> to{" "}
                <code>actors:</code> in <code>.agentjobs/config.yaml</code> and set{" "}
                <code>default_user:</code> to its id.
              </p>
            )}

            <div className="mobile-action-row flex items-center justify-end gap-3">
              <button
                type="button"
                onClick={onClose}
                className="touch-target rounded-lg px-4 font-semibold text-dark-muted hover:bg-dark-border"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={create.isPending || !reporter}
                className="touch-target rounded-lg bg-blue-600 px-5 font-semibold text-white hover:bg-blue-500 disabled:opacity-60"
              >
                {create.isPending ? "Filing…" : "File issue"}
              </button>
            </div>
          </form>
        )}
      </section>
    </div>
  );
}
