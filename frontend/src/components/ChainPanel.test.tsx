import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ChainRead } from "../api/types";
import {
  CHAIN_POLL_MS,
  ChainPanel,
  chainBounds,
  chainCriteria,
  chainPollInterval,
  chainState,
  converged,
  statusAt,
  turnsOf,
} from "./ChainPanel";

/**
 * The chain panel: does it say whether the loop is converging, and can it be stopped.
 *
 * **Every assertion here is on a rendered value**, never on the presence of an
 * attribute -- this task's own constraint, and the one this repository has been caught
 * by before (`data-ball="Ball.HUMAN"` matched every filter and displayed nothing). So a
 * cell is checked for the word `met`, not for having a `data-status` at all.
 */

function chain(overrides: Partial<ChainRead> = {}): ChainRead {
  return {
    chain_id: "chain_a1b2c3d4",
    task_id: "task-150",
    entry_id: 20,
    authorized_by: "Jeff Posey",
    authorized_at: "2026-09-22T10:00:00Z",
    max_iterations: 5,
    wall_clock_seconds: 14_400,
    deadline: "2026-09-22T14:00:00Z",
    check_digest: "abc123def456",
    criteria: ["sc-1", "sc-2"],
    revoked: false,
    revoked_at: null,
    expired: false,
    live: true,
    digest_matches: true,
    iterations: [
      {
        iteration: 0,
        entry_id: 21,
        ts: "2026-09-22T10:00:01Z",
        results: [
          { id: "sc-1", status: "failed", exit_code: 1, duration_seconds: 1 },
          { id: "sc-2", status: "failed", exit_code: 1, duration_seconds: 1 },
        ],
        unchecked: ["sc-3"],
      },
      {
        iteration: 1,
        entry_id: 25,
        ts: "2026-09-22T10:30:00Z",
        results: [
          { id: "sc-1", status: "met", exit_code: 0, duration_seconds: 2 },
          { id: "sc-2", status: "failed", exit_code: 1, duration_seconds: 1 },
        ],
        unchecked: ["sc-3"],
      },
    ],
    ...overrides,
  } as ChainRead;
}

describe("chainState", () => {
  it("names a live chain live", () => {
    expect(chainState(chain())).toBe("Live");
  });

  it("puts a revocation ahead of everything else", () => {
    // A person pressing stop is the answer whatever else is also true of the chain.
    expect(
      chainState(chain({ revoked: true, expired: true, digest_matches: false })),
    ).toBe("Stopped");
  });

  it("names convergence ahead of the revocation that always follows it", () => {
    // The driver revokes a chain it has finished with, so a converged chain and one a
    // person killed are both `revoked`. Only one of them is good news.
    const won = chain({
      revoked: true,
      live: false,
      iterations: [
        {
          iteration: 0,
          entry_id: 21,
          ts: "2026-09-22T10:00:01Z",
          results: [
            { id: "sc-1", status: "failed", exit_code: 1, duration_seconds: 1 },
            { id: "sc-2", status: "failed", exit_code: 1, duration_seconds: 1 },
          ],
          unchecked: [],
        },
        {
          iteration: 1,
          entry_id: 25,
          ts: "2026-09-22T10:30:00Z",
          results: [
            { id: "sc-1", status: "met", exit_code: 0, duration_seconds: 1 },
            { id: "sc-2", status: "met", exit_code: 0, duration_seconds: 1 },
          ],
          unchecked: [],
        },
      ],
    });

    expect(chainState(won)).toBe("Converged");
    expect(converged(won)).toBe(true);
    // And a chain that stopped one criterion short is not converged, however many
    // turns it took.
    expect(converged(chain({ revoked: true }))).toBe(false);
  });

  it("names an edited check rather than calling the chain live", () => {
    expect(chainState(chain({ digest_matches: false }))).toBe("Checks changed");
  });
});

describe("chainBounds", () => {
  it("counts turns taken against the cap, and does not count the baseline", () => {
    expect(chainBounds(chain())).toBe("1 of at most 5 iterations, within 4h");
  });
});

describe("chainCriteria", () => {
  it("keeps the authorisation's order and adds anything a pass decided besides", () => {
    const widened = chain({
      criteria: ["sc-1"],
      iterations: [
        {
          iteration: 1,
          entry_id: 25,
          ts: "2026-09-22T10:30:00Z",
          results: [
            { id: "sc-1", status: "met", exit_code: 0, duration_seconds: 1 },
            { id: "sc-9", status: "failed", exit_code: 1, duration_seconds: 1 },
          ],
          unchecked: [],
        },
      ],
    });

    expect(chainCriteria(widened)).toEqual(["sc-1", "sc-9"]);
  });
});

describe("statusAt", () => {
  it("is empty for a criterion the turn did not decide", () => {
    const turns = turnsOf(chain());
    const baseline = turns[0];
    if (!baseline) throw new Error("the fixture seeds a baseline turn");
    expect(statusAt(baseline, "sc-1")).toBe("failed");
    expect(statusAt(baseline, "sc-99")).toBe("");
  });
});

describe("chainPollInterval", () => {
  it("polls while a chain is live and not otherwise", () => {
    expect(chainPollInterval([chain()])).toBe(CHAIN_POLL_MS);
    expect(chainPollInterval([chain({ live: false })])).toBe(false);
    expect(chainPollInterval([])).toBe(false);
  });
});

describe("ChainPanel", () => {
  it("renders nothing when the task has never had a chain", () => {
    const { container } = render(<ChainPanel chains={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows each criterion's result at each iteration, as words", () => {
    render(<ChainPanel chains={[chain()]} />);

    const baseline = document.querySelector('[data-chain-iteration="0"]')!;
    const first = document.querySelector('[data-chain-iteration="1"]')!;

    // The baseline row is labelled, not hidden: the regression guard compares to it.
    expect(baseline.textContent).toContain("baseline");
    expect(
      baseline.querySelector('[data-criterion="sc-1"]')!.textContent,
    ).toContain("failed");
    // The staircase: sc-1 passed on turn one, sc-2 did not. That is convergence, read
    // down a column, which is the whole reason this is a grid.
    expect(first.querySelector('[data-criterion="sc-1"]')!.textContent).toContain(
      "met",
    );
    expect(first.querySelector('[data-criterion="sc-2"]')!.textContent).toContain(
      "failed",
    );
  });

  it("says a chain has recorded nothing yet rather than drawing an empty grid", () => {
    render(<ChainPanel chains={[chain({ iterations: [] })]} />);

    expect(
      screen.getByText(/No evaluation has been recorded yet/),
    ).toBeInTheDocument();
  });

  it("offers revoke on a live chain, behind one confirmation", async () => {
    const onRevoke = vi.fn().mockResolvedValue(undefined);
    render(<ChainPanel chains={[chain()]} onRevoke={onRevoke} />);

    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    // The confirmation says what revoking does *not* do, because the obvious reading of
    // a stop button is that it stops the run that is executing, and it does not.
    expect(
      screen.getByText(/A run already executing is not cancelled by this/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop it" }));

    await waitFor(() => expect(onRevoke).toHaveBeenCalledWith("chain_a1b2c3d4"));
  });

  it("does not offer revoke on a chain that has already stopped", () => {
    render(
      <ChainPanel chains={[chain({ live: false, revoked: true })]} onRevoke={vi.fn()} />,
    );

    expect(screen.queryByRole("button", { name: "Revoke" })).toBeNull();
    expect(screen.getByText(/Withdrawn/)).toBeInTheDocument();
  });

  it("renders the server's own sentence when a revoke is refused", async () => {
    const onRevoke = vi
      .fn()
      .mockRejectedValue(new Error("task-150 has no live chain to revoke."));
    render(<ChainPanel chains={[chain()]} onRevoke={onRevoke} />);

    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    fireEvent.click(screen.getByRole("button", { name: "Stop it" }));

    expect(
      await screen.findByText("task-150 has no live chain to revoke."),
    ).toBeInTheDocument();
  });

  it("explains an edited check rather than showing a chain that looks live", () => {
    render(<ChainPanel chains={[chain({ digest_matches: false, live: false })]} />);

    expect(screen.getByText("Checks changed")).toBeInTheDocument();
    expect(
      screen.getByText(/no longer the ones this chain was authorised against/),
    ).toBeInTheDocument();
  });

  it("puts the newest chain first", () => {
    render(
      <ChainPanel
        chains={[
          chain({ chain_id: "chain_older", live: false, revoked: true }),
          chain({ chain_id: "chain_newer" }),
        ]}
      />,
    );

    const articles = Array.from(document.querySelectorAll("[data-chain]"));
    expect(articles.map((item) => item.getAttribute("data-chain"))).toEqual([
      "chain_newer",
      "chain_older",
    ]);
  });
});
