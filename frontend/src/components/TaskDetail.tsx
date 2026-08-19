import { useState } from "react";
import { Link } from "react-router-dom";

import type { AttachmentUpload, LogEntry, TaskDetailResponse } from "../api/generated";
import { toUploads, type PendingAttachment } from "../report/attachments";
import { AttachmentPicker } from "./AttachmentPicker";
import { DependencyGraph } from "./DependencyGraph";
import { DependencyState } from "./DependencyState";
import { DispatchPanel, type DispatchPanelProps } from "./DispatchPanel";

const PRIORITY_CLASSES: Record<string, string> = {
  critical: "bg-red-900 text-red-200",
  high: "bg-orange-900 text-orange-200",
  medium: "bg-yellow-900 text-yellow-200",
  low: "bg-slate-700 text-slate-200",
};

const LOG_CLASSES: Record<string, string> = {
  handoff: "border-orange-500",
  decision: "border-purple-500",
  question: "border-yellow-500",
  instruction: "border-red-500",
};

function taskPath(projectId: string, taskId: string) {
  return `/p/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}`;
}

function SpecText({ children, muted = false }: { children: string; muted?: boolean }) {
  return <div className={`whitespace-pre-wrap text-sm leading-6 ${muted ? "text-dark-muted" : "text-dark-text"}`}>{children}</div>;
}

/**
 * The one box a human acts through, wearing the vocabulary of the phase the task
 * is in.
 *
 * A draft has not been specified yet, so the primary action is "this spec is
 * finished, make it claimable". A task past draft is being reviewed, so the primary
 * action is "approved, go merge". The other two actions do not change at all: asking
 * for changes and rejecting mean the same thing while a spec is being written as
 * they do after work is done, and only their wording needed to follow the phase.
 *
 * The primary button dispatches to a different verb per phase, and that difference
 * is not cosmetic. `approve` hands the ball back and leaves `lifecycle` alone;
 * `promote` moves `lifecycle` draft -> ready and clears the owner. Sending a draft
 * through approve would leave it draft/agent-work, which `get_next_task()` never
 * returns -- claimable by nobody, forever.
 */
function ReviewPanel({
  detail,
  busy,
  error,
  promoteBusy,
  onApprove,
  onRequestChanges,
  onReject,
  onPromote,
}: TaskDetailProps) {
  const [mode, setMode] = useState<"none" | "promote" | "changes" | "reject">("none");
  const [feedback, setFeedback] = useState("");
  const [attachments, setAttachments] = useState<Array<PendingAttachment>>([]);
  if (detail.task.ball !== "human") return null;

  const planning = detail.task.lifecycle === "draft";
  const working = busy || promoteBusy;
  const copy = planning
    ? {
        label: "Draft actions",
        heading: "Draft — the spec is with you",
        guidance: "These actions update the task record. Promoting puts this task in the pool for an agent to claim; nothing here runs git.",
        primary: "▲ Promote — make it claimable",
        secondary: "✎ Send feedback",
        feedbackLabel: "Feedback on the spec",
        feedbackPlaceholder: "Explain what the spec still needs...",
      }
    : {
        label: "Review actions",
        heading: `${detail.task.display_status} — the ball is with you`,
        guidance: "These actions update the task record and hand work back to the agent. They do not run git.",
        primary: "✓ Approve — agent may merge",
        secondary: "✎ Request Changes",
        feedbackLabel: "Feedback or questions",
        feedbackPlaceholder: "Explain what needs to change...",
      };
  const toggle = (next: "promote" | "changes" | "reject") => setMode(mode === next ? "none" : next);

  return (
    <section className="space-y-4 rounded-xl border-2 border-yellow-600/50 bg-yellow-950/30 p-4 min-[820px]:p-6" aria-label={copy.label}>
      <h2 className="text-lg font-semibold text-yellow-300">{copy.heading}</h2>
      {detail.task.ball_prompt && <SpecText>{detail.task.ball_prompt}</SpecText>}
      {detail.identity.ok && detail.identity.user ? (
        <>
          <p className="text-sm text-dark-muted">Acting as <strong className="text-dark-text">{detail.identity.user}</strong>. {copy.guidance}</p>
          <div className="mobile-action-row flex flex-wrap gap-3">
            <button type="button" disabled={working} onClick={() => (planning ? toggle("promote") : void onApprove())} className="touch-target rounded-lg bg-emerald-600 px-4 font-semibold text-white hover:bg-emerald-700 disabled:opacity-60">{copy.primary}</button>
            <button type="button" disabled={working} onClick={() => toggle("changes")} className="touch-target rounded-lg bg-yellow-600 px-4 font-semibold text-white hover:bg-yellow-700 disabled:opacity-60">{copy.secondary}</button>
            <button type="button" disabled={working} onClick={() => toggle("reject")} className="touch-target rounded-lg bg-red-600 px-4 font-semibold text-white hover:bg-red-700 disabled:opacity-60">✕ Reject &amp; Archive</button>
          </div>
          {mode === "promote" && (
            <form
              className="space-y-3"
              onSubmit={(event) => {
                event.preventDefault();
                // null rather than "" for an empty note, so the manager writes its
                // own sentence instead of the UI inventing a blank one.
                void onPromote(feedback.trim() ? feedback.trim() : null);
              }}
            >
              <label htmlFor="promote-note" className="block text-sm font-semibold">Promotion note (optional)</label>
              <textarea id="promote-note" value={feedback} onChange={(event) => setFeedback(event.target.value)} rows={4} className="w-full rounded-lg border border-dark-border bg-dark-bg p-3 text-dark-text focus:border-yellow-500 focus:outline-none" placeholder="Say why the spec is finished. Left empty, AgentJobs writes its own sentence." />
              <div className="mobile-action-row flex gap-3">
                <button type="submit" disabled={working} className="touch-target rounded-lg bg-emerald-600 px-4 font-semibold text-white disabled:opacity-60">Promote</button>
                <button type="button" onClick={() => { setMode("none"); setFeedback(""); setAttachments([]); }} className="touch-target rounded-lg border border-dark-border px-4 font-semibold">Cancel</button>
              </div>
            </form>
          )}
          {(mode === "changes" || mode === "reject") && (
            <form
              className="space-y-3"
              onSubmit={(event) => {
                event.preventDefault();
                const value = feedback.trim();
                if (!value) return;
                void (mode === "changes" ? onRequestChanges(value, toUploads(attachments)) : onReject(value));
              }}
            >
              {mode === "changes" ? (
                <AttachmentPicker
                  label={copy.feedbackLabel}
                  hint="Paste a screenshot of what you are describing; it is stored with this entry."
                  placeholder={copy.feedbackPlaceholder}
                  value={feedback}
                  onChange={setFeedback}
                  attachments={attachments}
                  onAttachmentsChange={setAttachments}
                  required
                  textareaClassName="mt-1 min-h-28 w-full rounded-lg border border-dark-border bg-dark-bg p-3 text-dark-text focus:border-yellow-500 focus:outline-none"
                />
              ) : (
                <>
                  <label htmlFor="review-feedback" className="block text-sm font-semibold">Reason for rejection</label>
                  <textarea id="review-feedback" required value={feedback} onChange={(event) => setFeedback(event.target.value)} rows={4} className="w-full rounded-lg border border-dark-border bg-dark-bg p-3 text-dark-text focus:border-yellow-500 focus:outline-none" placeholder="Explain why this task should stop..." />
                </>
              )}
              <div className="mobile-action-row flex gap-3">
                <button type="submit" disabled={working || !feedback.trim()} className="touch-target rounded-lg bg-yellow-600 px-4 font-semibold text-white disabled:opacity-60">Submit</button>
                <button type="button" onClick={() => { setMode("none"); setFeedback(""); setAttachments([]); }} className="touch-target rounded-lg border border-dark-border px-4 font-semibold">Cancel</button>
              </div>
            </form>
          )}
        </>
      ) : (
        <div className="rounded-lg border border-yellow-600/50 bg-dark-bg p-4 text-sm">
          <strong className="text-yellow-300">{detail.identity.problem === "multiple" ? "Multiple users configured. " : "No user configured. "}</strong>
          <span className="text-dark-muted">{detail.identity.detail}</span>
        </div>
      )}
      {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
    </section>
  );
}

/**
 * The promote refusal, rendered outside the action panel on purpose.
 *
 * The refusal most worth showing is a revision conflict, and it arrives precisely
 * because somebody else promoted the task first -- which moves the ball to
 * agent/available, and the panel above returns null the moment the ball leaves the
 * human. An in-panel message would therefore vanish at the exact moment it was
 * needed, leaving a click that appeared to do nothing. Observed, not predicted.
 */
function PromoteError({ promoteError }: TaskDetailProps) {
  if (!promoteError) return null;
  return (
    <p role="alert" className="rounded-xl border-2 border-red-600/50 bg-red-950/30 p-4 text-sm text-red-200">{promoteError}</p>
  );
}

function Relationships({ detail, projectId }: { detail: TaskDetailResponse; projectId: string }) {
  if (detail.children.length === 0 && detail.needs.length === 0 && detail.blocks.length === 0 && detail.related.length === 0 && !detail.task.parent) return null;
  return (
    <section className="grid gap-4 min-[820px]:grid-cols-2" aria-label="Task relationships">
      {(detail.task.parent || detail.children.length > 0) && (
        <div className="rounded-lg border border-dark-border bg-dark-surface">
          <h2 className="border-b border-dark-border p-4 font-semibold">Task hierarchy</h2>
          <div className="divide-y divide-dark-border">
            {detail.task.parent && <div className="p-4 text-sm">Parent: <Link className="touch-target inline-flex text-blue-300 hover:underline" to={taskPath(projectId, detail.task.parent)}>{detail.parent_task?.title ?? detail.task.parent} <span className="font-mono text-xs">({detail.task.parent})</span></Link></div>}
            {detail.children.map((child) => <div className="flex flex-col gap-1 p-4 min-[820px]:flex-row min-[820px]:items-center min-[820px]:justify-between" key={child.id}><Link className="touch-target block text-blue-300 hover:underline" to={taskPath(projectId, child.id)}><span className="block font-medium">{child.title}</span><span className="font-mono text-xs text-dark-muted">{child.id}</span></Link><span className="text-xs text-dark-muted">{child.display_status}</span></div>)}
          </div>
        </div>
      )}
      {detail.related.length > 0 && (
        <div className="rounded-lg border border-dark-border bg-dark-surface">
          <h2 className="border-b border-dark-border p-4 font-semibold">Related</h2>
          <ul className="space-y-3 p-4">{detail.related.map((relation, index) => <li className="text-sm" key={`${relation.task_id}-${index}`}>{relation.exists ? <Link className="touch-target font-mono text-blue-300 hover:underline" to={taskPath(projectId, relation.task_id)}>{relation.task_id}</Link> : <span className="font-mono text-red-300">{relation.task_id} (missing)</span>}{relation.note && <p className="text-dark-muted">{relation.note}</p>}</li>)}</ul>
        </div>
      )}
      {(detail.needs.length > 0 || detail.blocks.length > 0) && (
        <div className="rounded-lg border border-dark-border bg-dark-surface">
          <h2 className="border-b border-dark-border p-4 font-semibold">Needs and blocks</h2>
          <div className="grid gap-px bg-dark-border min-[820px]:grid-cols-2">
            <div className="bg-dark-surface p-4"><h3 className="font-semibold">This task needs</h3>{detail.needs.length === 0 ? <p className="mt-2 text-sm text-dark-muted">Nothing.</p> : <ul className="mt-2 space-y-3">{detail.needs.map((relation) => <li className="text-sm" key={relation.task_id}>{relation.exists ? <Link className="touch-target font-mono text-blue-300 hover:underline" to={taskPath(projectId, relation.task_id)}>{relation.task_id}</Link> : <span className="font-mono text-red-300">{relation.task_id} (missing)</span>}<p className={relation.state === "open" || relation.state === "missing" ? "text-red-300" : "text-emerald-300"}>{relation.reason}</p>{relation.note && <p className="text-dark-muted">{relation.note}</p>}</li>)}</ul>}</div>
            <div className="bg-dark-surface p-4"><h3 className="font-semibold">This task blocks</h3>{detail.blocks.length === 0 ? <p className="mt-2 text-sm text-dark-muted">Nothing.</p> : <ul className="mt-2 space-y-3">{detail.blocks.map((relation, index) => <li className="text-sm" key={`${relation.task_id}-${index}`}>{relation.exists ? <Link className="touch-target font-mono text-blue-300 hover:underline" to={taskPath(projectId, relation.task_id)}>{relation.task_id}</Link> : <span className="font-mono text-red-300">{relation.task_id} (missing)</span>}<p className="text-dark-muted">{relation.reason}</p>{relation.note && <p className="text-dark-muted">{relation.note}</p>}</li>)}</ul>}</div>
          </div>
        </div>
      )}
    </section>
  );
}

function EntryAttachments({
  entry,
  projectId,
  taskId,
}: {
  entry: LogEntry;
  projectId: string;
  taskId: string;
}) {
  const attachments = entry.attachments ?? [];
  if (attachments.length === 0) return null;
  return (
    <ul aria-label={`Images on entry ${entry.id}`} className="mt-3 flex flex-wrap gap-3">
      {attachments.map((attachment) => {
        // The stored path is relative to the tasks directory; the route addresses the
        // file by task and basename, so the server never takes a path from a client.
        const filename = attachment.path.split("/").pop() ?? "";
        const href = `/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}/attachments/${encodeURIComponent(filename)}`;
        return (
          <li key={attachment.sha256}>
            {/* Shown, not linked. A screenshot nobody clicks is a screenshot nobody
                sees -- the anchor is only so the full-size image is reachable. */}
            <a href={href} target="_blank" rel="noreferrer">
              <img
                src={href}
                alt={attachment.label}
                className="max-h-64 rounded-lg border border-dark-border"
              />
            </a>
          </li>
        );
      })}
    </ul>
  );
}

function Log({ entries, projectId, taskId }: { entries: Array<LogEntry>; projectId: string; taskId: string }) {
  // Per-entry disclosures keep a hundred-entry log scannable, but they also hide the
  // text from Ctrl+F and from select-all-and-copy, which is exactly what a reviewer
  // auditing a long task needs. One control opens every one of them; the `key` carries
  // the flag so React remounts each <details> and the new state wins even over an
  // entry the reader had toggled by hand.
  const [expandAll, setExpandAll] = useState(false);
  const answered = new Set(entries.filter((entry) => entry.type === "answer" && entry.re).map((entry) => entry.re));
  const ordered = [...entries].sort((left, right) => right.id - left.id);
  if (entries.length === 0) return null;
  return (
    <section className="rounded-lg border border-dark-border bg-dark-surface" aria-label="Task log">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-dark-border p-4">
        <h2 className="text-lg font-semibold">Log</h2>
        <button
          type="button"
          aria-pressed={expandAll}
          onClick={() => setExpandAll((current) => !current)}
          className="touch-target rounded-lg border border-dark-border bg-dark-bg px-3 text-xs text-blue-300 hover:border-blue-500"
        >
          {expandAll ? "Collapse long entries" : "Expand all entries"}
        </button>
      </div>
      <div className="space-y-4 p-4 min-[820px]:p-6">
        {ordered.map((entry, index) => {
          const openQuestion = entry.type === "question" && !answered.has(entry.id);
          return (
            <article className={`border-l-2 pl-4 ${LOG_CLASSES[entry.type] ?? "border-blue-500"}`} key={entry.id} data-log-id={entry.id}>
              <div className="flex flex-wrap items-center gap-2 text-xs uppercase text-dark-muted"><span>#{entry.id}</span><span>•</span><span>{entry.actor}</span><span>•</span><time dateTime={entry.ts}>{new Date(entry.ts).toLocaleString()}</time><span className="rounded border border-dark-border bg-dark-bg px-2 py-0.5 lowercase">{openQuestion ? "open question" : entry.type}</span>{entry.re && <span>re #{entry.re}</span>}</div>
              {entry.body && <details key={String(expandAll)} open={expandAll || index === 0 || entry.body.length <= 400} className="mt-2"><summary className="touch-target cursor-pointer text-xs text-blue-300">{entry.body.length > 400 ? "Entry details" : "Entry"}</summary><SpecText muted>{entry.body}</SpecText></details>}
              <EntryAttachments entry={entry} projectId={projectId} taskId={taskId} />
            </article>
          );
        })}
      </div>
    </section>
  );
}

export type TaskDetailProps = {
  detail: TaskDetailResponse;
  projectId: string;
  busy?: boolean;
  error?: string | null;
  // Promote keeps its own error state rather than sharing `error`: the generic
  // "could not be recorded, reload and try again" hides the one explanation that
  // actually tells a human what happened and what to do about it.
  promoteBusy?: boolean;
  promoteError?: string | null;
  onApprove: () => Promise<void> | void;
  onRequestChanges: (
    feedback: string,
    attachments: Array<AttachmentUpload>,
  ) => Promise<void> | void;
  onReject: (reason: string) => Promise<void> | void;
  onPromote: (note: string | null) => Promise<void> | void;
  // Dispatch arrives as its own bundle rather than as loose props, so nothing about
  // starting an agent can be mistaken for part of the review panel's contract.
  // Absent, the page renders exactly as it did before dispatch existed.
  dispatch?: Omit<DispatchPanelProps, "taskIsDispatchable">;
};

export function TaskDetail(props: TaskDetailProps) {
  const { detail, projectId } = props;
  const { task } = detail;
  const metadata = [
    { label: "Created", value: task.created, date: true },
    { label: "Updated", value: task.updated, date: true },
    { label: "Owner", value: task.assignment?.owner ?? "Unclaimed", date: false },
    { label: "Effort", value: task.effort ?? "Not estimated", date: false },
  ];
  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-4 min-[820px]:flex-row min-[820px]:items-start min-[820px]:justify-between">
        <div className="min-w-0"><div className="select-all font-mono text-sm text-blue-300">{task.id}</div><h1 className="break-words text-2xl font-bold min-[820px]:text-3xl">{task.title}</h1><div className="mt-3 flex flex-wrap items-center gap-2"><span className="rounded bg-dark-surface px-2 py-1 text-xs">{task.display_status}</span><span className={`rounded px-2 py-1 text-xs ${PRIORITY_CLASSES[task.priority ?? "medium"]}`}>{task.priority ?? "medium"}</span><span className="text-sm text-dark-muted">{task.category}</span>{task.tags?.map((tag) => <span className="rounded border border-dark-border bg-dark-bg px-2 py-0.5 text-xs" key={tag}>{tag}</span>)}</div></div>
        <Link to={`/p/${encodeURIComponent(projectId)}/tasks`} className="touch-target rounded-lg border border-dark-border bg-dark-surface px-4 text-sm hover:bg-dark-border">← Back to Tasks</Link>
      </header>

      <section className="grid grid-cols-2 overflow-hidden rounded-lg border border-dark-border bg-dark-surface min-[820px]:grid-cols-4" aria-label="Task metadata">
        {metadata.map(({ label, value, date }) => <div className="border-b border-r border-dark-border p-3 min-[820px]:border-b-0" key={label}><div className="text-xs text-dark-muted">{label}</div><div className="mt-1 break-words text-sm">{date ? new Date(value).toLocaleString() : value}</div></div>)}
      </section>

      <PromoteError {...props} />
      <ReviewPanel {...props} />
      {props.dispatch && (
        <DispatchPanel
          {...props.dispatch}
          taskIsDispatchable={task.ball === "agent" && task.lifecycle !== "closed"}
        />
      )}
      {task.ball !== "human" && task.ball_prompt &&<section className="rounded-xl border border-dark-border bg-dark-surface p-4"><h2 className="mb-2 text-xs font-semibold uppercase text-dark-muted">Current ask ({task.ball}/{task.ball_reason})</h2><SpecText>{task.ball_prompt}</SpecText></section>}
      <section className="rounded-lg border border-dark-border bg-dark-surface p-4" aria-label="Dependency state"><h2 className="mb-2 text-sm font-semibold">Work state</h2><DependencyState task={task} /></section>
      <Relationships detail={detail} projectId={projectId} />
      <DependencyGraph children={detail.children} edges={detail.child_dependency_edges} projectId={projectId} umbrellaTitle={task.title} />

      <section className="space-y-6 rounded-lg border border-dark-border bg-dark-surface p-4 min-[820px]:p-6" aria-label="Full specification">
        <div><h2 className="mb-2 text-lg font-semibold">Summary</h2><SpecText>{task.spec.summary}</SpecText></div>
        {task.spec.intent && <div><h2 className="mb-2 text-lg font-semibold">Why this task exists</h2><SpecText muted>{task.spec.intent}</SpecText></div>}
        <div><h2 className="mb-2 text-lg font-semibold">Working spec</h2><SpecText>{task.spec.description}</SpecText></div>
        {task.spec.constraints && <div><h2 className="mb-2 text-lg font-semibold">Constraints</h2><SpecText>{task.spec.constraints}</SpecText></div>}
        {task.spec.out_of_scope && <div><h2 className="mb-2 text-lg font-semibold">Out of scope</h2><SpecText muted>{task.spec.out_of_scope}</SpecText></div>}
        {(task.spec.context?.length ?? 0) > 0 && <div><h2 className="mb-2 text-lg font-semibold">Read this first</h2><ul className="space-y-2">{task.spec.context?.map((pointer) => <li className="text-sm" key={pointer.path}><code className="break-all text-blue-300">{pointer.path}</code><span className="text-dark-muted"> — {pointer.why}</span></li>)}</ul></div>}
        {(task.acceptance?.length ?? 0) > 0 && <div><h2 className="mb-2 text-lg font-semibold">Acceptance</h2><ul className="space-y-2">{task.acceptance?.map((criterion) => <li className="rounded-lg border border-dark-border bg-dark-bg p-3 text-sm" key={criterion.id}><span className="mr-2 uppercase text-dark-muted">{criterion.status ?? "pending"}</span>{criterion.text}{criterion.verify && <code className="mt-1 block text-xs text-dark-muted">{criterion.verify}</code>}</li>)}</ul></div>}
      </section>

      {(task.deliverables?.length ?? 0) > 0 && <section className="rounded-lg border border-dark-border bg-dark-surface" aria-label="Deliverables"><h2 className="border-b border-dark-border p-4 text-lg font-semibold">Deliverables</h2><div className="divide-y divide-dark-border">{task.deliverables?.map((item) => <div className="p-4" key={item.path}><code className="break-all text-sm">{item.path}</code><span className="ml-2 text-xs uppercase text-dark-muted">{item.status ?? "pending"}</span>{item.note && <p className="mt-1 text-sm text-dark-muted">{item.note}</p>}</div>)}</div></section>}
      <Log entries={task.log ?? []} projectId={projectId} taskId={task.id} />
      {(task.links?.length ?? 0) > 0 && <section className="rounded-lg border border-dark-border bg-dark-surface" aria-label="External links"><h2 className="border-b border-dark-border p-4 text-lg font-semibold">Links</h2><ul>{task.links?.map((link) => <li className="p-4" key={link.url}><a className="touch-target break-all text-blue-300 underline" href={link.url} target="_blank" rel="noreferrer">{link.title ?? link.url}</a><span className="ml-2 text-xs uppercase text-dark-muted">{link.rel ?? "other"}</span></li>)}</ul></section>}
    </div>
  );
}
