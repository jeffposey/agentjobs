import { useState } from "react";
import { Link } from "react-router-dom";

import type { Lifecycle, Priority, Task, TaskCreateRequest } from "../api/generated";

type TaskCreateProps = {
  projectId: string;
  existingTaskIds: Array<string>;
  onCreate: (request: TaskCreateRequest) => Promise<Task>;
};

const inputClass = "touch-target mt-1 w-full rounded-lg border border-dark-border bg-dark-bg px-3 py-2 text-dark-text placeholder:text-dark-muted focus:border-blue-500 focus:outline-none";
const textareaClass = `${inputClass} min-h-28`;

function optional(value: string) {
  const trimmed = value.trim();
  return trimmed || undefined;
}

function parseRows(value: string, label: string, requireReason: boolean) {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, index) => {
      const [first, ...rest] = line.split("|").map((part) => part.trim());
      const reason = rest.join(" | ");
      if (!first || (requireReason && !reason)) {
        throw new Error(`${label} line ${index + 1} must use ${requireReason ? "path | why" : "task-id | optional note"}.`);
      }
      return { first, reason };
    });
}

export function TaskCreate({ projectId, existingTaskIds, onCreate }: TaskCreateProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lifecycle, setLifecycle] = useState<Lifecycle>("draft");
  const [priority, setPriority] = useState<Priority>("medium");

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    const form = new FormData(event.currentTarget);
    try {
      const context = parseRows(String(form.get("context") ?? ""), "Context", true)
        .map(({ first, reason }) => ({ path: first, why: reason }));
      const dependencies = parseRows(String(form.get("dependencies") ?? ""), "Dependency", false)
        .map(({ first, reason }) => ({ task: first, type: "needs" as const, ...(reason ? { note: reason } : {}) }));
      const acceptance = String(form.get("acceptance") ?? "")
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((text, index) => ({ id: `ac-${index + 1}`, text, status: "pending" as const }));
      const tags = String(form.get("tags") ?? "")
        .split(",")
        .map((tag) => tag.trim())
        .filter(Boolean);

      const request: TaskCreateRequest = {
        title: String(form.get("title") ?? "").trim(),
        summary: String(form.get("summary") ?? "").trim(),
        description: String(form.get("description") ?? "").trim(),
        lifecycle,
        priority,
        category: String(form.get("category") ?? "general").trim() || "general",
        id: optional(String(form.get("id") ?? "")),
        intent: optional(String(form.get("intent") ?? "")),
        constraints: optional(String(form.get("constraints") ?? "")),
        out_of_scope: optional(String(form.get("out_of_scope") ?? "")),
        parent: optional(String(form.get("parent") ?? "")),
        effort: optional(String(form.get("effort") ?? "")),
        context,
        dependencies,
        acceptance,
        tags,
      };

      setBusy(true);
      await onCreate(request);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The task could not be created. Try again.");
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(event) => void submit(event)} className="mx-auto max-w-3xl space-y-6">
      <header>
        <p className="text-sm font-semibold uppercase tracking-wide text-blue-300">New task</p>
        <h2 className="mt-1 text-3xl font-bold">Give the next reader enough to resume</h2>
        <p className="mt-2 text-dark-muted">The summary orients them; the working description tells them what to do.</p>
      </header>

      {error && <div role="alert" className="rounded-lg border border-red-500/60 bg-red-950/50 p-4 text-red-200">{error}</div>}

      <section className="space-y-4 rounded-lg border border-dark-border bg-dark-surface p-5" aria-labelledby="task-basics-heading">
        <h3 id="task-basics-heading" className="text-xl font-semibold">Core specification</h3>
        <label className="block font-medium">Title <input name="title" required autoFocus className={inputClass} /></label>
        <label className="block font-medium">
          Summary
          <span className="mt-1 block text-xs font-normal text-dark-muted">One or two sentences that orient a reader with no prior context.</span>
          <textarea name="summary" required className={textareaClass} />
        </label>
        <label className="block font-medium">
          Working description
          <span className="mt-1 block text-xs font-normal text-dark-muted">What must be done, including the important behavior and boundaries.</span>
          <textarea name="description" required className={`${textareaClass} min-h-40`} />
        </label>
      </section>

      <fieldset className="rounded-lg border border-dark-border bg-dark-surface p-5">
        <legend className="px-1 text-xl font-semibold">Starting state</legend>
        <p className="mb-4 text-sm text-dark-muted">Choose who acts next. This cannot be inferred safely.</p>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className={`cursor-pointer rounded-lg border p-4 ${lifecycle === "draft" ? "border-blue-500 bg-blue-950/30" : "border-dark-border"}`}>
            <input type="radio" name="lifecycle" value="draft" checked={lifecycle === "draft"} onChange={() => setLifecycle("draft")} />
            <span className="ml-2 font-semibold">Draft — needs human specification</span>
            <span className="mt-2 block text-sm text-dark-muted">Use when questions or decisions remain before an agent should claim it.</span>
          </label>
          <label className={`cursor-pointer rounded-lg border p-4 ${lifecycle === "ready" ? "border-blue-500 bg-blue-950/30" : "border-dark-border"}`}>
            <input type="radio" name="lifecycle" value="ready" checked={lifecycle === "ready"} onChange={() => setLifecycle("ready")} />
            <span className="ml-2 font-semibold">Ready — available to an agent</span>
            <span className="mt-2 block text-sm text-dark-muted">Use only when the specification is complete enough to execute now.</span>
          </label>
        </div>
      </fieldset>

      <details className="rounded-lg border border-dark-border bg-dark-surface p-5">
        <summary className="touch-target cursor-pointer font-semibold">More specification</summary>
        <div className="mt-4 space-y-4">
          <label className="block font-medium">Intent <textarea name="intent" className={textareaClass} placeholder="Why does this task exist?" /></label>
          <label className="block font-medium">Constraints <textarea name="constraints" className={textareaClass} placeholder="Hard requirements and prohibitions" /></label>
          <label className="block font-medium">Out of scope <textarea name="out_of_scope" className={textareaClass} placeholder="Explicit non-goals" /></label>
          <label className="block font-medium">
            Read-first context
            <span className="mt-1 block text-xs font-normal text-dark-muted">One per line: path | why it matters</span>
            <textarea name="context" className={textareaClass} placeholder="src/agentjobs/manager.py | Owns the behavior being changed" />
          </label>
        </div>
      </details>

      <details className="rounded-lg border border-dark-border bg-dark-surface p-5">
        <summary className="touch-target cursor-pointer font-semibold">Planning and relationships</summary>
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <label className="block font-medium">Task ID <span className="text-xs font-normal text-dark-muted">(generated if blank)</span><input name="id" className={inputClass} placeholder="task-123-short-name" /></label>
          <label className="block font-medium">Parent task<input name="parent" list="existing-task-ids" className={inputClass} placeholder="Optional umbrella task ID" /></label>
          <datalist id="existing-task-ids">{existingTaskIds.map((id) => <option value={id} key={id} />)}</datalist>
          <label className="block font-medium">Priority<select name="priority" value={priority} onChange={(event) => setPriority(event.target.value as Priority)} className={inputClass}><option value="critical">Critical</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></select></label>
          <label className="block font-medium">Category<input name="category" defaultValue="general" className={inputClass} /></label>
          <label className="block font-medium">Effort<input name="effort" className={inputClass} placeholder="Half a day" /></label>
          <label className="block font-medium">Tags <span className="text-xs font-normal text-dark-muted">(comma-separated)</span><input name="tags" className={inputClass} placeholder="gui, testing" /></label>
          <label className="block font-medium sm:col-span-2">Acceptance criteria <span className="text-xs font-normal text-dark-muted">(one per line)</span><textarea name="acceptance" className={textareaClass} /></label>
          <label className="block font-medium sm:col-span-2">Dependencies <span className="text-xs font-normal text-dark-muted">(one per line: task-id | optional note)</span><textarea name="dependencies" className={textareaClass} /></label>
        </div>
      </details>

      <div className="mobile-action-row flex items-center justify-end gap-3">
        <Link to={`/p/${encodeURIComponent(projectId)}/tasks`} className="touch-target rounded-lg px-4 font-semibold text-dark-muted hover:bg-dark-border">Cancel</Link>
        <button type="submit" disabled={busy} className="touch-target rounded-lg bg-blue-600 px-5 font-semibold text-white hover:bg-blue-500 disabled:opacity-60">{busy ? "Creating…" : "Create task"}</button>
      </div>
    </form>
  );
}
