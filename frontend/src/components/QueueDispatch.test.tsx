import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { DispatchStateView } from "../api/types";
import { QueueDispatch, QueueDispatchGate } from "./QueueDispatch";

function state(overrides: Partial<DispatchStateView> = {}): DispatchStateView {
  return {
    project_id: "inbox",
    configured: true,
    master_enabled: true,
    sentinel_active: false,
    project_enabled: true,
    can_dispatch: true,
    ...overrides,
  } as DispatchStateView;
}

function renderButton(props: Partial<Parameters<typeof QueueDispatch>[0]> = {}) {
  const onDispatch = vi.fn();
  render(
    <MemoryRouter>
      <QueueDispatch
        state={state()}
        user="jeff"
        identityDetail="Acting as jeff."
        canBrief
        taskHref="/p/inbox/tasks/task-1"
        onDispatch={onDispatch}
        {...props}
      />
    </MemoryRouter>,
  );
  return onDispatch;
}

describe("QueueDispatch", () => {
  it("starts a run on one click, with nothing else to fill in", () => {
    // The record is the brief (task-188). A panel that asked for a group, a posture or
    // a note here would make the Dashboard's one-gesture answer a form.
    const onDispatch = renderButton();

    fireEvent.click(screen.getByRole("button", { name: "▶ Dispatch" }));

    expect(onDispatch).toHaveBeenCalledTimes(1);
  });

  it("disables itself and says why when nothing resolves to a person", () => {
    // Disabled rather than pressable-into-a-refusal: the run needs somebody's name on
    // it and the page already knows it has none to offer.
    renderButton({ user: null });

    expect(screen.getByRole("button", { name: "▶ Dispatch" })).toBeDisabled();
    expect(screen.getByText("Acting as jeff.")).toBeVisible();
  });

  it("sends a record that cannot brief an agent to the page with the brief box", () => {
    renderButton({ canBrief: false });

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Brief and dispatch →" })).toHaveAttribute(
      "href",
      "/p/inbox/tasks/task-1",
    );
  });

  it("shows nothing at all where the machine may not dispatch", () => {
    renderButton({ state: state({ can_dispatch: false }) });

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows nothing at all where dispatch was never configured", () => {
    renderButton({ state: null });

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("names the refusal beside the button that produced it", () => {
    renderButton({
      refusal: { reason: "live_run_exists", message: "A run is already live for this task." },
    });

    expect(screen.getByRole("alert")).toHaveTextContent("A run is already live for this task.");
  });

  it("says it is starting rather than staying pressable", () => {
    renderButton({ busy: true });

    expect(screen.getByRole("button", { name: "Starting…" })).toBeDisabled();
  });
});

describe("QueueDispatchGate", () => {
  it("explains a closed gate once, with a way to open it", () => {
    render(
      <MemoryRouter>
        <QueueDispatchGate
          state={state({
            can_dispatch: false,
            refusal: { reason: "project_disabled", message: "Dispatch is off for this project." },
          } as Partial<DispatchStateView>)}
          projectId="inbox"
        />
      </MemoryRouter>,
    );

    const line = screen.getByRole("status");
    expect(line).toHaveTextContent("Dispatch is off for this project.");
    // The server's `suggested_action` is deliberately not appended: it names a CLI
    // command, and this surface already offers the control.
    expect(line).not.toHaveTextContent("Turn it on under Dispatch");
    expect(screen.getByRole("link", { name: "Dispatch settings" })).toHaveAttribute(
      "href",
      "/p/inbox/dispatch",
    );
  });

  it("is silent on a machine that has never configured dispatch", () => {
    // Nothing to switch on and nothing to explain. A permanent note about a feature the
    // owner never set up is clutter on the page they open most.
    render(
      <MemoryRouter>
        <QueueDispatchGate state={state({ configured: false, can_dispatch: false })} projectId="inbox" />
      </MemoryRouter>,
    );

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("is silent when the gate is open", () => {
    render(
      <MemoryRouter>
        <QueueDispatchGate state={state()} projectId="inbox" />
      </MemoryRouter>,
    );

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
