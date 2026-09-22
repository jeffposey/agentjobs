import { useState } from "react";

import type { ChainIteration, ChainRead } from "../api/types";

/**
 * A bounded chain of dispatches, and whether it is converging (task-150).
 *
 * The panel answers two questions and is shaped by them. *Is this loop getting
 * anywhere* -- answered by the iteration grid, where a column is a criterion and a row
 * is a turn, so convergence reads as a staircase and thrash reads as three identical
 * rows. And *how do I stop it* -- answered by one button, always visible while a chain
 * is live, because design section 9 asks for a kill switch as blunt as `agentjobs
 * dispatch stop` and one behind a menu is not one.
 *
 * **The grid, not a list of entries.** A chain is up to twenty `check_result` entries
 * over up to a handful of criteria, and reading twenty entries to work out whether
 * `sc-2` ever passed is exactly the work this exists to save. Laid out as a grid the
 * answer is a glance down one column.
 *
 * **Iteration 0 is the baseline the authorisation recorded**, not a turn anybody paid
 * for, and it is labelled so rather than being hidden: the regression guard compares
 * against it, so a reader asking "what broke" needs to see where the chain started.
 *
 * Follows `DispatchPanel`'s shape for a live panel with a destructive control: a
 * confirm step in front of the button, the button disabled while the request is in
 * flight, and the server's own sentence rendered on a refusal rather than a generic
 * one.
 */

/** How often a watching browser re-reads a live chain's history. */
export const CHAIN_POLL_MS = 10_000;

/**
 * Poll while a chain is live, never otherwise.
 *
 * Ten seconds rather than the finish panel's two: a chain's turns are whole agent runs
 * and the next `check_result` is minutes away at best, so a faster clock would buy a
 * reader nothing and cost the server a request every two seconds for hours.
 */
export function chainPollInterval(chains: ChainRead[]): number | false {
  return chains.some((chain) => chain.live) ? CHAIN_POLL_MS : false;
}

/**
 * What state a chain is in, in one word, in the order a reader would ask.
 *
 * Revoked before expired before checks-changed, because that is the order of *why it
 * stopped*: somebody pressing stop is the answer whatever else is also true, and a
 * digest that moved on a chain nobody is running is not what a reader wants told first.
 */
export function chainState(chain: ChainRead): string {
  if (converged(chain)) return "Converged";
  if (chain.revoked) return "Stopped";
  if (chain.expired) return "Expired";
  if (!chain.digest_matches) return "Checks changed";
  return "Live";
}

/**
 * Whether this chain's last evaluation found every check passing.
 *
 * Ahead of every other state, because the driver revokes a chain it has finished with --
 * so that a restarted driver does not read a spent authorisation as live -- and without
 * this the one chain that *worked* would be badged "Stopped" beside the one a person
 * killed. Both are revoked; only one of them is good news, and the badge is the first
 * thing read on the card.
 *
 * Derived from the last vector rather than from a field, because there is no field: the
 * server answers with what the log holds, and what the log holds is the vector. A stored
 * "converged" flag would be a second copy of a fact the entries already carry.
 */
export function converged(chain: ChainRead): boolean {
  const turns = turnsOf(chain);
  const last = turns[turns.length - 1];
  if (!last || (last.results ?? []).length === 0) return false;
  return (last.results ?? []).every((outcome) => outcome.status === "met");
}

const STATE_CLASSES: Record<string, string> = {
  Converged: "bg-emerald-900 text-emerald-200",
  Live: "bg-sky-900 text-sky-200",
  Stopped: "bg-slate-700 text-slate-200",
  Expired: "bg-slate-700 text-slate-200",
  "Checks changed": "bg-orange-900 text-orange-100",
};

/** The bounds, as the person who authorised them chose them. */
export function chainBounds(chain: ChainRead): string {
  const hours = chain.wall_clock_seconds / 3600;
  const turns = turnsOf(chain).filter((item) => item.iteration > 0).length;
  return `${turns} of at most ${chain.max_iterations} iteration${
    chain.max_iterations === 1 ? "" : "s"
  }, within ${hours % 1 === 0 ? hours : hours.toFixed(1)}h`;
}

/**
 * The sentence under the heading: what this chain is doing to a reader's task.
 *
 * Every branch says the consequence rather than the state's name. "Checks changed" in
 * the badge is the label; this is the part that says the loop has stopped because of it.
 */
export function chainDetail(chain: ChainRead): string {
  if (converged(chain)) {
    return "Every checked criterion passed, so the loop stopped and handed the task to a person. What it established is that the machine-checkable half of the definition of done now holds -- it has not closed the task and will not, because the prose criteria are the part that needed judgement.";
  }
  if (chain.revoked) {
    return "Withdrawn. No further iteration will start, and any run that was already executing was left alone.";
  }
  if (chain.expired) {
    return "The wall-clock bound has passed, so no further iteration will start.";
  }
  if (!chain.digest_matches) {
    return "The task's checks are no longer the ones this chain was authorised against, so it stops rather than converging on the new ones. Somebody edited a criterion's check after this was agreed.";
  }
  return "Each iteration dispatches an agent, waits for that run to end, then runs the checks. It stops on its own when they all pass, when one regresses, when three turns produce the same result, or at either ceiling.";
}

/** The criteria the digest covers, in the task's own order, across every turn. */
export function chainCriteria(chain: ChainRead): string[] {
  const named = chain.criteria ?? [];
  const seen = new Set(named);
  for (const turn of turnsOf(chain)) {
    for (const outcome of turn.results ?? []) seen.add(outcome.id);
  }
  // The authorisation's own order first, because that is the task's order and the order
  // a reader knows. Anything a pass decided that the digest did not cover goes after it
  // rather than being dropped: a column missing from the grid is a result nobody sees.
  return named.concat(Array.from(seen).filter((id) => !named.includes(id)));
}

/** A chain's iterations, oldest first. The generated type makes the list optional. */
export function turnsOf(chain: ChainRead): ChainIteration[] {
  return chain.iterations ?? [];
}

/** One criterion's status on one turn, or "" when that turn did not decide it. */
export function statusAt(turn: ChainIteration, criterion: string): string {
  const found = (turn.results ?? []).find((item) => item.id === criterion);
  return found ? found.status : "";
}

const MARK: Record<string, string> = { met: "✅", failed: "❌" };

function Grid({ chain }: { chain: ChainRead }) {
  const criteria = chainCriteria(chain);
  const turns = turnsOf(chain);
  if (turns.length === 0 || criteria.length === 0) {
    return (
      <p className="text-sm text-dark-muted" role="status">
        No evaluation has been recorded yet. The first lands when the first iteration
        ends.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table
        className="w-full border-collapse text-sm"
        data-chain-grid={chain.chain_id}
      >
        <caption className="sr-only">
          Each criterion&apos;s result at each iteration of chain {chain.chain_id}
        </caption>
        <thead>
          <tr className="text-left text-dark-muted">
            <th scope="col" className="py-1 pr-4 font-normal">
              Iteration
            </th>
            {criteria.map((id) => (
              <th key={id} scope="col" className="py-1 pr-4 font-mono font-normal">
                {id}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {turns.map((turn) => (
            <tr
              key={turn.entry_id}
              className="border-t border-dark-border"
              data-chain-iteration={turn.iteration}
            >
              <th
                scope="row"
                className="py-1 pr-4 text-left font-normal text-dark-muted"
              >
                {turn.iteration === 0 ? "baseline" : turn.iteration}
              </th>
              {criteria.map((id) => {
                const status = statusAt(turn, id);
                return (
                  <td
                    key={id}
                    className="py-1 pr-4"
                    data-criterion={id}
                    data-status={status || "not-decided"}
                  >
                    {/* The word as well as the mark: an emoji alone is what a screen
                        reader would read as nothing, and the value is what a test
                        should assert on rather than the presence of a cell. */}
                    <span aria-hidden="true">{MARK[status] ?? "·"}</span>{" "}
                    <span className="text-dark-muted">{status || "—"}</span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export type ChainPanelProps = {
  chains: ChainRead[];
  /** Runs the revoke. Rejects with a readable sentence when the server refuses. */
  onRevoke?: (chainId: string) => Promise<void>;
};

export function ChainPanel({ chains, onRevoke }: ChainPanelProps) {
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string>("");

  if (chains.length === 0) return null;
  // Newest first: a task with three chains has had two that ended, and the one a reader
  // came to look at is the one that has not.
  const ordered = chains.slice().reverse();

  async function revoke(chainId: string) {
    if (!onRevoke) return;
    setBusy(chainId);
    setProblem("");
    try {
      await onRevoke(chainId);
      setConfirming(null);
    } catch (error) {
      setProblem(
        error instanceof Error ? error.message : "The chain could not be stopped.",
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <section
      className="space-y-4 rounded-xl border-2 border-indigo-700/50 bg-indigo-950/30 p-4 @min-[768px]:p-6"
      aria-label="Chains"
    >
      <h2 className="text-lg font-semibold text-indigo-200">Agent loop</h2>
      {ordered.map((chain) => {
        const state = chainState(chain);
        return (
          <article
            key={chain.chain_id}
            className="space-y-3 rounded-lg border border-dark-border bg-dark-surface p-3"
            data-chain={chain.chain_id}
            data-chain-state={state}
            data-chain-live={chain.live ? "yes" : "no"}
          >
            <div className="flex flex-wrap items-center gap-3">
              <span
                className={`rounded px-2 py-0.5 text-xs font-semibold ${
                  STATE_CLASSES[state] ?? "bg-slate-700 text-slate-200"
                }`}
              >
                {state}
              </span>
              <span className="font-mono text-sm text-dark-text">
                {chain.chain_id}
              </span>
              <span className="text-sm text-dark-muted">
                {chainBounds(chain)}
              </span>
            </div>

            <p className="text-sm text-dark-muted">
              Authorised by{" "}
              <strong className="font-semibold text-dark-text">
                {chain.authorized_by}
              </strong>{" "}
              against {(chain.criteria ?? []).length} checked criteri
              {(chain.criteria ?? []).length === 1 ? "on" : "a"}.
            </p>
            <p className="text-sm text-dark-muted">{chainDetail(chain)}</p>

            <Grid chain={chain} />

            {chain.live && onRevoke && (
              // The question above the buttons rather than beside them. In one flex row
              // the sentence is long enough to push `Stop it` past the right edge at a
              // task page's width -- observed in the sandbox at 1099px, and worse on a
              // phone, where the destructive control would be the one thing off screen.
              <div className="space-y-2">
                {confirming === chain.chain_id ? (
                  <>
                    <p className="text-sm text-orange-200">
                      Stop this chain? No further iteration starts. A run already
                      executing is not cancelled by this.
                    </p>
                    <div className="flex flex-wrap items-center gap-3">
                      <button
                        type="button"
                        disabled={busy === chain.chain_id}
                        onClick={() => revoke(chain.chain_id)}
                        className="touch-target rounded-lg bg-red-700 px-3 text-sm font-semibold text-white hover:bg-red-600 disabled:opacity-50"
                      >
                        {busy === chain.chain_id ? "Stopping…" : "Stop it"}
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirming(null)}
                        className="touch-target rounded-lg border border-dark-border px-3 text-sm text-dark-text hover:bg-dark-border"
                      >
                        Keep going
                      </button>
                    </div>
                  </>
                ) : (
                  <button
                    type="button"
                    onClick={() => setConfirming(chain.chain_id)}
                    className="touch-target rounded-lg border border-red-800 px-3 text-sm font-semibold text-red-300 hover:bg-red-950"
                  >
                    Revoke
                  </button>
                )}
              </div>
            )}

            {problem && confirming === chain.chain_id && (
              <p className="text-sm text-red-300" role="alert">
                {problem}
              </p>
            )}
          </article>
        );
      })}
    </section>
  );
}
