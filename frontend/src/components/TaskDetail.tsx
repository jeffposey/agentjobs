import { useState } from "react";
import { Link } from "react-router-dom";

import type { LogEntry, TaskDetailResponse } from "../api/generated";

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

function ReviewPanel({
  detail,
  busy,
  error,
  onApprove,
  onRequestChanges,
  onReject,
}: TaskDetailProps) {
  const [mode, setMode] = useState<"none" | "changes" | "reject">("none");
  const [feedback, setFeedback] = useState("");
  if (detail.task.ball !== "human") return null;

  return (
    <section className="space-y-4 rounded-xl border-2 border-yellow-600/50 bg-yellow-950/30 p-4 min-[820px]:p-6" aria-label="Review actions">
      <h2 className="text-lg font-semibold text-yellow-300">{detail.task.display_status} — the ball is with you</h2>
      {detail.task.ball_prompt && <SpecText>{detail.task.ball_prompt}</SpecText>}
      {detail.identity.ok && detail.identity.user ? (
        <>
          <p className="text-sm text-dark-muted">Acting as <strong className="text-dark-text">{detail.identity.user}</strong>. These actions update the task record and hand work back to the agent. They do not run git.</p>
          <div className="mobile-action-row flex flex-wrap gap-3">
            <button type="button" disabled={busy} onClick={() => void onApprove()} className="touch-target rounded-lg bg-emerald-600 px-4 font-semibold text-white hover:bg-emerald-700 disabled:opacity-60">✓ Approve — agent may merge</button>
            <button type="button" disabled={busy} onClick={() => setMode(mode === "changes" ? "none" : "changes")} className="touch-target rounded-lg bg-yellow-600 px-4 font-semibold text-white hover:bg-yellow-700 disabled:opacity-60">✎ Request Changes</button>
            <button type="button" disabled={busy} onClick={() => setMode(mode === "reject" ? "none" : "reject")} className="touch-target rounded-lg bg-red-600 px-4 font-semibold text-white hover:bg-red-700 disabled:opacity-60">✕ Reject &amp; Archive</button>
          </div>
          {mode !== "none" && (
            <form
              className="space-y-3"
              onSubmit={(event) => {
                event.preventDefault();
                const value = feedback.trim();
                if (!value) return;
                void (mode === "changes" ? onRequestChanges(value) : onReject(value));
              }}
            >
              <label htmlFor="review-feedback" className="block text-sm font-semibold">{mode === "changes" ? "Feedback or questions" : "Reason for rejection"}</label>
              <textarea id="review-feedback" required value={feedback} onChange={(event) => setFeedback(event.target.value)} rows={4} className="w-full rounded-lg border border-dark-border bg-dark-bg p-3 text-dark-text focus:border-yellow-500 focus:outline-none" placeholder={mode === "changes" ? "Explain what needs to change..." : "Explain why this task should stop..."} />
              <div className="mobile-action-row flex gap-3">
                <button type="submit" disabled={busy || !feedback.trim()} className="touch-target rounded-lg bg-yellow-600 px-4 font-semibold text-white disabled:opacity-60">Submit</button>
                <button type="button" onClick={() => { setMode("none"); setFeedback(""); }} className="touch-target rounded-lg border border-dark-border px-4 font-semibold">Cancel</button>
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

function Relationships({ detail, projectId }: { detail: TaskDetailResponse; projectId: string }) {
  if (detail.children.length === 0 && (detail.task.dependencies?.length ?? 0) === 0 && !detail.task.parent) return null;
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
      {(detail.task.dependencies?.length ?? 0) > 0 && (
        <div className="rounded-lg border border-dark-border bg-dark-surface">
          <h2 className="border-b border-dark-border p-4 font-semibold">Dependencies</h2>
          <div className="divide-y divide-dark-border">{detail.task.dependencies?.map((dependency) => <div className="p-4 text-sm" key={`${dependency.type}-${dependency.task}`}><Link className="touch-target font-mono text-blue-300 hover:underline" to={taskPath(projectId, dependency.task)}>{dependency.task}</Link><div className="capitalize text-dark-muted">{dependency.type ?? "needs"}</div>{dependency.note && <p className="mt-1 text-dark-muted">{dependency.note}</p>}</div>)}</div>
        </div>
      )}
    </section>
  );
}

function Log({ entries }: { entries: Array<LogEntry> }) {
  if (entries.length === 0) return null;
  const answered = new Set(entries.filter((entry) => entry.type === "answer" && entry.re).map((entry) => entry.re));
  const ordered = [...entries].sort((left, right) => right.id - left.id);
  return (
    <section className="rounded-lg border border-dark-border bg-dark-surface" aria-label="Task log">
      <h2 className="border-b border-dark-border p-4 text-lg font-semibold">Log</h2>
      <div className="space-y-4 p-4 min-[820px]:p-6">
        {ordered.map((entry, index) => {
          const openQuestion = entry.type === "question" && !answered.has(entry.id);
          return (
            <article className={`border-l-2 pl-4 ${LOG_CLASSES[entry.type] ?? "border-blue-500"}`} key={entry.id} data-log-id={entry.id}>
              <div className="flex flex-wrap items-center gap-2 text-xs uppercase text-dark-muted"><span>#{entry.id}</span><span>•</span><span>{entry.actor}</span><span>•</span><time dateTime={entry.ts}>{new Date(entry.ts).toLocaleString()}</time><span className="rounded border border-dark-border bg-dark-bg px-2 py-0.5 lowercase">{openQuestion ? "open question" : entry.type}</span>{entry.re && <span>re #{entry.re}</span>}</div>
              {entry.body && <details open={index === 0 || entry.body.length <= 400} className="mt-2"><summary className="touch-target cursor-pointer text-xs text-blue-300">{entry.body.length > 400 ? "Entry details" : "Entry"}</summary><SpecText muted>{entry.body}</SpecText></details>}
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
  onApprove: () => Promise<void> | void;
  onRequestChanges: (feedback: string) => Promise<void> | void;
  onReject: (reason: string) => Promise<void> | void;
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

      <ReviewPanel {...props} />
      {task.ball !== "human" && task.ball_prompt && <section className="rounded-xl border border-dark-border bg-dark-surface p-4"><h2 className="mb-2 text-xs font-semibold uppercase text-dark-muted">Current ask ({task.ball}/{task.ball_reason})</h2><SpecText>{task.ball_prompt}</SpecText></section>}
      <Relationships detail={detail} projectId={projectId} />

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
      <Log entries={task.log ?? []} />
      {(task.links?.length ?? 0) > 0 && <section className="rounded-lg border border-dark-border bg-dark-surface" aria-label="External links"><h2 className="border-b border-dark-border p-4 text-lg font-semibold">Links</h2><ul>{task.links?.map((link) => <li className="p-4" key={link.url}><a className="touch-target break-all text-blue-300 underline" href={link.url} target="_blank" rel="noreferrer">{link.title ?? link.url}</a><span className="ml-2 text-xs uppercase text-dark-muted">{link.rel ?? "other"}</span></li>)}</ul></section>}
    </div>
  );
}
