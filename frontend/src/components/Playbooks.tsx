import { useState } from "react";

import type { PlaybookCollection, PlaybookRead } from "../api/types";
import type { DispatchStateView } from "../api/types";
import { REFUSAL_ACTIONS, type DispatchRefusal } from "./DispatchPanel";

/**
 * The playbooks a project keeps, and the button that runs one.
 *
 * Two rules carried over from `DispatchPanel`, because a playbook run *is* a dispatch
 * and a second surface that felt different about spending money would be a bug:
 *
 * **Running is not reading.** The list is a reference -- what briefs exist, what each
 * one is for, where a run stops for a human. The Run button is the only control here
 * that costs anything, it is coloured like the Dispatch button rather than like a link,
 * and it says what it does.
 *
 * **A closed gate is named, never hidden behind a dead button.** Dispatch off, project
 * not enabled, nobody signed in: each is shown as text with the remedy, and the button
 * is not offered. The one refusal that cannot be predicted -- the machine is busy, the
 * tree is dirty -- comes back from the press and is rendered beside the playbook that
 * produced it.
 *
 * A **task-target** playbook needs a task id, so it gets a field rather than a button.
 * The alternative was aiming it at "the task you are looking at", which this page has
 * no notion of, and guessing one would be a dispatch at a task nobody named.
 */

export type PlaybookRunRequest = { name: string; task?: string };

export type PlaybooksProps = {
  collection: PlaybookCollection | null;
  /** Machine and project gates, from the same endpoint the dispatch page reads. */
  dispatchState: DispatchStateView | null;
  /** Name of the playbook a run is in flight for, or null. */
  runningName?: string | null;
  /** The last refusal from pressing Run, and which playbook it was for. */
  refusal?: { name: string; refusal: DispatchRefusal } | null;
  /** What a successful run produced, so the page can point at the task it made. */
  started?: { name: string; taskId: string; created: boolean } | null;
  onRun: (request: PlaybookRunRequest) => Promise<boolean> | boolean;
};

/** The gate that stops a run before a click, with the sentence explaining it. */
function gateRefusal(state: DispatchStateView | null): DispatchRefusal | null {
  if (!state || state.can_dispatch || !state.refusal) return null;
  return { reason: state.refusal.reason, message: state.refusal.message };
}

function Note({
  tone,
  children,
  reason,
  alert = false,
}: {
  tone: "warn" | "good";
  children: React.ReactNode;
  reason?: string;
  alert?: boolean;
}) {
  const palette =
    tone === "warn"
      ? "border-orange-600/50 bg-orange-950/30 text-orange-100"
      : "border-emerald-600/50 bg-emerald-950/30 text-emerald-100";
  return (
    <div
      role={alert ? "alert" : "status"}
      data-refusal-reason={reason}
      className={`rounded-lg border p-3 text-sm ${palette}`}
    >
      {children}
    </div>
  );
}

function RefusalNote({ refusal, alert }: { refusal: DispatchRefusal; alert: boolean }) {
  const action = refusal.suggestedAction || REFUSAL_ACTIONS[refusal.reason];
  return (
    <Note tone="warn" reason={refusal.reason} alert={alert}>
      <p>{refusal.message}</p>
      {action && <p className="mt-2 text-orange-200">{action}</p>}
    </Note>
  );
}

function PlaybookCard({
  playbook,
  canRun,
  busy,
  refusal,
  started,
  onRun,
}: {
  playbook: PlaybookRead;
  canRun: boolean;
  busy: boolean;
  refusal: DispatchRefusal | null;
  started: { taskId: string; created: boolean } | null;
  onRun: (request: PlaybookRunRequest) => Promise<boolean> | boolean;
}) {
  const [taskId, setTaskId] = useState("");
  const needsTask = playbook.target === "task";
  // Optional in the generated types because the API omits empty lists, which is
  // what `response_model_exclude_none` and a default-empty frontmatter field make
  // of a playbook that declares no gates.
  const gates = playbook.gates ?? [];
  const runnable = canRun && (!needsTask || taskId.trim().length > 0);

  return (
    <article
      className="space-y-3 rounded-xl border border-dark-border bg-dark-surface p-4"
      aria-label={`Playbook ${playbook.name}`}
      data-playbook={playbook.name}
      data-playbook-target={playbook.target}
    >
      <div>
        <h3 className="text-lg font-semibold text-dark-text">{playbook.name}</h3>
        <p className="mt-1 text-sm text-dark-muted">{playbook.description}</p>
      </div>

      <dl className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-dark-muted">
        <div>
          <dt className="inline font-semibold">Runs against: </dt>
          <dd className="inline">{needsTask ? "one task" : "the project"}</dd>
        </div>
        <div>
          <dt className="inline font-semibold">Difficulty: </dt>
          <dd className="inline">{playbook.difficulty}</dd>
        </div>
        <div>
          <dt className="inline font-semibold">Human gates: </dt>
          <dd className="inline">{gates.length}</dd>
        </div>
        <div>
          <dt className="inline font-semibold">File: </dt>
          <dd className="inline">{playbook.filename}</dd>
        </div>
      </dl>

      {gates.length > 0 && (
        <ul className="space-y-1 text-xs text-dark-muted">
          {gates.map((gate) => (
            <li key={`${gate.before}-${gate.what}`}>
              <span className="font-semibold text-amber-300">Stops before {gate.before}:</span>{" "}
              {gate.what}
            </li>
          ))}
        </ul>
      )}

      {canRun && (
        <div className="mobile-action-row flex flex-wrap items-center gap-3">
          {needsTask && (
            <label className="text-sm text-dark-muted">
              <span className="mr-2">Task</span>
              <input
                type="text"
                value={taskId}
                onChange={(event) => setTaskId(event.target.value)}
                placeholder="task-123"
                aria-label={`Task to run ${playbook.name} against`}
                className="touch-target rounded-md border border-dark-border bg-dark-bg px-3 text-dark-text"
              />
            </label>
          )}
          <button
            type="button"
            disabled={busy || !runnable}
            onClick={() => void onRun({ name: playbook.name, task: needsTask ? taskId.trim() : undefined })}
            className="touch-target rounded-lg bg-sky-600 px-4 font-semibold text-white hover:bg-sky-500 disabled:opacity-60"
          >
            ▶ Run — start an agent now
          </button>
        </div>
      )}

      {started && (
        <Note tone="good">
          <p>
            Started on{" "}
            <a className="underline" href={`tasks/${encodeURIComponent(started.taskId)}`}>
              {started.taskId}
            </a>
            {started.created ? ", a run task this run created." : "."}
          </p>
        </Note>
      )}

      {refusal && <RefusalNote refusal={refusal} alert />}
    </article>
  );
}

export function Playbooks({
  collection,
  dispatchState,
  runningName = null,
  refusal = null,
  started = null,
  onRun,
}: PlaybooksProps) {
  const gate = gateRefusal(dispatchState);
  // Both lists are optional in the generated types: the API omits an empty one.
  const playbooks = collection?.playbooks ?? [];
  const problems = collection?.problems ?? [];
  const identity = collection?.identity ?? null;
  const user = identity?.ok ? identity.user : null;
  const configured = Boolean(dispatchState?.configured);
  const canRun = configured && Boolean(dispatchState?.can_dispatch) && Boolean(user);

  return (
    <section className="space-y-4" aria-label="Playbooks">
      <header>
        <h1 className="text-2xl font-bold text-dark-text">Playbooks</h1>
        <p className="mt-1 text-sm text-dark-muted">
          Reusable briefs this project keeps in git for recurring judgment work. Running
          one starts an agent on this machine, now — it is not approval, and it spends
          tokens.
        </p>
      </header>

      {collection && !collection.exists && (
        <Note tone="warn">
          <p>This project has no playbooks directory yet ({collection.directory}).</p>
          <p className="mt-2 text-orange-200">
            Copy the shipped references in with <code>agentjobs playbook init</code>.
          </p>
        </Note>
      )}

      {collection && problems.length > 0 && (
        // Before the valid ones, for the reason the CLI prints them first: a file that
        // fails validation and then vanishes reads as a playbook nobody ever wrote.
        <Note tone="warn">
          <p>{problems.length} file(s) in that directory are not valid playbooks:</p>
          <ul className="mt-2 space-y-1">
            {problems.map((problem) => (
              <li key={`${problem.filename}-${problem.field}-${problem.message}`}>
                <code>{problem.filename}</code>
                {problem.field ? ` — ${problem.field}: ` : " — "}
                {problem.message}
              </li>
            ))}
          </ul>
        </Note>
      )}

      {configured && gate && <RefusalNote refusal={gate} alert={false} />}

      {configured && !gate && !user && identity && (
        // Disabled rather than pressable-into-a-refusal, exactly as the Dispatch button
        // is: the run needs somebody's name on it and the page knows before the click
        // that it has none to offer.
        <Note tone="warn" reason="no_signed_in_user">
          <p>Nobody is signed in, so there is no one to attribute a run to.</p>
          <p className="mt-2 text-orange-200">{identity.detail}</p>
        </Note>
      )}

      {!configured && (
        <Note tone="warn" reason="not_configured">
          <p>Dispatch is not set up on this machine, so nothing here can be run.</p>
          <p className="mt-2 text-orange-200">{REFUSAL_ACTIONS.not_configured}</p>
        </Note>
      )}

      <div className="space-y-4">
        {playbooks.map((playbook) => (
          <PlaybookCard
            key={playbook.name}
            playbook={playbook}
            canRun={canRun}
            busy={runningName === playbook.name}
            refusal={refusal?.name === playbook.name ? refusal.refusal : null}
            started={
              started?.name === playbook.name
                ? { taskId: started.taskId, created: started.created }
                : null
            }
            onRun={onRun}
          />
        ))}
      </div>

      {collection?.exists && playbooks.length === 0 && (
        <p className="text-sm text-dark-muted">
          No playbooks in {collection.directory}. Copy the shipped references in with{" "}
          <code>agentjobs playbook init</code>.
        </p>
      )}
    </section>
  );
}
