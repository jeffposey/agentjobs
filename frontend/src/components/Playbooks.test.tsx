import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { DispatchStateView, PlaybookCollection } from "../api/types";
import { Playbooks } from "./Playbooks";

/**
 * What a human can and cannot press, and what they are told when they cannot.
 *
 * Assertions are on the words a person reads and on whether a control is offered --
 * never on the presence of an attribute -- for the reason `DispatchPanel.test.tsx`
 * states: a check for `data-something=` passes happily while the page emits an enum's
 * Python spelling.
 *
 * The rule under test throughout is the one this page shares with the Dispatch button:
 * **a gate that is already closed is named, and the button is not offered.** A page
 * that offers a button into a certain refusal is worse than one that explains itself.
 */

function state(overrides: Partial<DispatchStateView> = {}): DispatchStateView {
  return {
    project_id: "sandbox",
    configured: true,
    master_enabled: true,
    sentinel_active: false,
    project_enabled: true,
    runner: "claude-session",
    posture: "supervised",
    auto_dispatch: false,
    available_runners: ["claude-session"],
    can_dispatch: true,
    refusal: null,
    config_path: "C:/Users/j/.agentjobs/dispatch.yaml",
    sentinel_file: "C:/Users/j/.agentjobs/DISPATCH_DISABLED",
    ...overrides,
  };
}

function collection(overrides: Partial<PlaybookCollection> = {}): PlaybookCollection {
  return {
    directory: "C:/projects/sandbox/playbooks",
    exists: true,
    identity: { ok: true, user: "Jeff Posey", problem: null, detail: "Acting as Jeff Posey." },
    problems: [],
    playbooks: [
      {
        name: "groom",
        description: "Find duplicates and propose closures.",
        target: "project",
        difficulty: "hard",
        verbs: ["close", "log"],
        gates: [{ before: "close", what: "A human approved the closure list." }],
        filename: "groom.md",
      },
      {
        name: "flesh-out",
        description: "Write a full spec onto a thin task.",
        target: "task",
        difficulty: "hard",
        verbs: ["update", "log"],
        gates: [],
        filename: "flesh-out.md",
      },
    ],
    ...overrides,
  };
}

function card(name: string) {
  return within(screen.getByLabelText(`Playbook ${name}`));
}

describe("Playbooks", () => {
  it("lists what each playbook is for and where it stops for a human", () => {
    render(<Playbooks collection={collection()} dispatchState={state()} onRun={vi.fn()} />);

    expect(card("groom").getByText("Find duplicates and propose closures.")).toBeTruthy();
    expect(card("groom").getByText(/Stops before close/)).toBeTruthy();
    expect(card("groom").getByText("the project")).toBeTruthy();
    expect(card("flesh-out").getByText("one task")).toBeTruthy();
  });

  it("runs a project playbook on one click, with no task", async () => {
    const onRun = vi.fn().mockResolvedValue(true);
    render(<Playbooks collection={collection()} dispatchState={state()} onRun={onRun} />);

    fireEvent.click(card("groom").getByRole("button", { name: /Run/ }));

    await waitFor(() => expect(onRun).toHaveBeenCalledWith({ name: "groom", task: undefined }));
  });

  it("will not run a task playbook until a task is named", async () => {
    const onRun = vi.fn().mockResolvedValue(true);
    render(<Playbooks collection={collection()} dispatchState={state()} onRun={onRun} />);
    const scoped = card("flesh-out");

    const button = scoped.getByRole("button", { name: /Run/ }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    fireEvent.change(scoped.getByLabelText(/Task to run flesh-out against/), {
      target: { value: "task-123" },
    });
    fireEvent.click(scoped.getByRole("button", { name: /Run/ }));

    await waitFor(() =>
      expect(onRun).toHaveBeenCalledWith({ name: "flesh-out", task: "task-123" }),
    );
  });

  it("offers no button and names the gate when the project is not enabled", () => {
    render(
      <Playbooks
        collection={collection()}
        dispatchState={state({
          can_dispatch: false,
          project_enabled: false,
          refusal: {
            reason: "project_not_enabled",
            message: "Dispatch is not enabled for 'sandbox'.",
          },
        })}
        onRun={vi.fn()}
      />,
    );

    expect(screen.getByText("Dispatch is not enabled for 'sandbox'.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Run/ })).toBeNull();
  });

  it("offers no button and says why when nobody is signed in", () => {
    render(
      <Playbooks
        collection={collection({
          identity: {
            ok: false,
            user: null,
            problem: "unconfigured",
            detail: "No human actor is configured.",
          },
        })}
        dispatchState={state()}
        onRun={vi.fn()}
      />,
    );

    expect(screen.getByText("No human actor is configured.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Run/ })).toBeNull();
  });

  it("says nothing can run at all when dispatch is not set up on this machine", () => {
    render(
      <Playbooks
        collection={collection()}
        dispatchState={state({ configured: false, can_dispatch: false })}
        onRun={vi.fn()}
      />,
    );

    expect(
      screen.getByText("Dispatch is not set up on this machine, so nothing here can be run."),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Run/ })).toBeNull();
  });

  it("renders a refusal from a press beside the playbook that produced it", () => {
    render(
      <Playbooks
        collection={collection()}
        dispatchState={state()}
        refusal={{
          name: "groom",
          refusal: {
            reason: "concurrency_limit",
            message: "This machine allows 1 concurrent run(s) and 1 are active.",
          },
        }}
        onRun={vi.fn()}
      />,
    );

    expect(card("groom").getByRole("alert").textContent).toContain("1 concurrent run(s)");
    expect(card("flesh-out").queryByRole("alert")).toBeNull();
  });

  it("points at the run task a project run created", () => {
    render(
      <Playbooks
        collection={collection()}
        dispatchState={state()}
        started={{ name: "groom", taskId: "task-301", created: true }}
        onRun={vi.fn()}
      />,
    );

    const link = card("groom").getByRole("link", { name: "task-301" });
    expect(link.getAttribute("href")).toBe("tasks/task-301");
    expect(card("groom").getByText(/a run task this run created/)).toBeTruthy();
  });

  it("points a project with no directory at the command that makes one", () => {
    render(
      <Playbooks
        collection={collection({ exists: false, playbooks: [] })}
        dispatchState={state()}
        onRun={vi.fn()}
      />,
    );

    expect(screen.getByText(/no playbooks directory yet/)).toBeTruthy();
    expect(screen.getByText("agentjobs playbook init")).toBeTruthy();
  });

  it("reports a file that will not load rather than omitting it", () => {
    render(
      <Playbooks
        collection={collection({
          problems: [
            { filename: "watch.md", field: "kind", message: "Extra inputs are not permitted" },
          ],
        })}
        dispatchState={state()}
        onRun={vi.fn()}
      />,
    );

    expect(screen.getByText(/1 file\(s\) in that directory are not valid playbooks/)).toBeTruthy();
    expect(screen.getByText(/Extra inputs are not permitted/)).toBeTruthy();
  });
});
