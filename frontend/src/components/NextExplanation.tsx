import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { explainNextTaskApiProjectsProjectIdTasksNextExplainGetOptions } from "../api/generated/@tanstack/react-query.gen";

/**
 * Why the dashboard is offering *this* task, and what it stood in front of.
 *
 * Section 9 of the task-selection design, on the one screen where the question gets
 * asked out loud. A scheduler that names a task and will not say why is an oracle; the
 * queue has a defensible answer now, so it gives it — and someone who sees their
 * favourite task skipped for "has 7 open children" has just learned a rule nobody had
 * to write down for them.
 *
 * Closed by default and fetched only when opened. The explanation walks every open task
 * ahead of the winner, and the dashboard is polled — paying for that on every refresh
 * to render a panel nobody has expanded is the kind of cost that gets a good idea
 * removed later for being slow.
 */
export function NextExplanation({ projectId }: { projectId: string }) {
  const [open, setOpen] = useState(false);
  const query = useQuery({
    ...explainNextTaskApiProjectsProjectIdTasksNextExplainGetOptions({
      path: { project_id: projectId },
    }),
    enabled: open,
  });

  return (
    <details
      className="mt-3 rounded-lg border border-dark-border bg-dark-bg/40 p-3"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="touch-target cursor-pointer text-xs text-blue-400 hover:text-blue-300">
        Why this one?
      </summary>
      {query.isPending && open && <p className="mt-2 text-xs text-dark-muted">Reading the queue…</p>}
      {query.isError && (
        <p className="mt-2 text-xs text-dark-muted">
          The queue could not explain itself just now. Reload the page and open this again.
        </p>
      )}
      {query.data && (
        <div className="mt-2 space-y-2 text-xs text-dark-muted">
          <p>
            It stands at position{" "}
            <span className="font-mono text-dark-text">{query.data.queue_position ?? "—"}</span> of the{" "}
            <span className="text-dark-text">{query.data.band ?? "—"}</span> band, and it is the first
            task there that can actually be claimed.
          </p>
          {(query.data.empty_bands_above ?? []).length > 0 && (
            <p>
              Nothing at all is open above it:{" "}
              <span className="text-dark-text">{(query.data.empty_bands_above ?? []).join(", ")}</span>{" "}
              {(query.data.empty_bands_above ?? []).length === 1 ? "is" : "are"} empty.
            </p>
          )}
          {(query.data.skipped ?? []).length > 0 ? (
            <div>
              <p className="text-dark-text">Ahead of it in line, and why each was passed over:</p>
              <ul className="mt-1 space-y-1">
                {(query.data.skipped ?? []).map((skipped) => (
                  <li key={skipped.task}>
                    <span className="font-mono text-blue-400">{skipped.task}</span>
                    <span className="text-dark-muted"> ({skipped.position ?? "no position"}) — {skipped.reason}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <p>Nothing stands ahead of it — it is first in line and claimable.</p>
          )}
        </div>
      )}
    </details>
  );
}
