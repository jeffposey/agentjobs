import type { QueueProblemRead } from "../api/types";

/**
 * The queue is broken, which tasks broke it, and the command that repairs it.
 *
 * Design section 8 in a banner. It renders *above* whatever else the page shows,
 * rather than replacing it, for the same reason `BrokenFiles` does: corruption is a
 * fact about the corpus, not a call to action competing with the others. What it does
 * suppress is any claim about the order — the list stops offering to reorder a band it
 * cannot justify, and the dashboard stops naming a task as next.
 *
 * The repair command is selectable text rather than a button. Repairing guesses — it
 * has to, because a duplicate position holds no record of who was meant to be first —
 * and everything it guesses is exactly what a human should read afterwards. A button
 * here would put that behind one click from a screen showing none of it.
 */
export function QueueBroken({
  problems,
  repairCommand,
}: {
  problems: Array<QueueProblemRead>;
  repairCommand: string;
}) {
  if (problems.length === 0) return null;
  return (
    <section
      role="alert"
      aria-label="Queue is broken"
      className="rounded-lg border-2 border-red-500/50 bg-red-950/30 p-4"
    >
      <h2 className="text-sm font-semibold text-red-300">
        The queue is broken, so nothing here claims to know what comes next
      </h2>
      <p className="mt-1 text-xs text-dark-muted">
        Two tasks claim one place in line, or an open task has none. Until it is repaired,
        reordering is disabled and the position column is not an order anybody can act on.
      </p>
      <ul className="mt-3 space-y-1 text-xs text-red-200">
        {problems.map((problem) => (
          <li key={`${problem.kind}-${problem.band}-${problem.position ?? "none"}-${(problem.tasks ?? []).join(",")}`}>
            {problem.message}
          </li>
        ))}
      </ul>
      <p className="mt-3 text-xs text-dark-muted">Repair it with:</p>
      <pre className="mt-1 select-all overflow-x-auto rounded bg-dark-bg p-2 text-xs text-dark-text">
        {repairCommand}
      </pre>
    </section>
  );
}
