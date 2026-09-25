import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter, Route, Routes, Link } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { client } from "../api/generated/client.gen";
import { apiMockServer, VERSION_IN_STEP } from "../test/api-mock";
import { ActionsMenu, ACTIONS_MENU_ID } from "./ActionsMenu";

/** What the generated client is pointed at for these tests; About reports it back. */
const BASE_URL = "http://localhost";

/**
 * task-168: the actions menu's behaviour, which is the half jsdom can see.
 *
 * What it cannot see is deliberately elsewhere. That the popup overlays the page
 * rather than pushing it down, and that a phone can reach every entry with a thumb,
 * are both layout -- jsdom lays nothing out, so a test asserting `class="absolute"`
 * would pass just as happily against a popup nested somewhere absolute means nothing.
 * That evidence is in `e2e/actions-menu.spec.ts`, measured in a real browser.
 */

function renderMenu({ at = "/p/demo/tasks" } = {}) {
  client.setConfig({ baseUrl: BASE_URL });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[at]}>
        {/* A destination to navigate to, so "closes on a route change" can be
            provoked by an actual route change rather than by re-rendering the
            component with a different prop. */}
        <Link to="/p/demo/elsewhere">Elsewhere</Link>
        <Routes>
          <Route path="/p/demo/*" element={<ActionsMenu projectId="demo" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const trigger = () => screen.getByRole("button", { name: "Actions" });
const menu = () => document.getElementById(ACTIONS_MENU_ID);

describe("ActionsMenu", () => {
  it("starts closed, and says so to a screen reader", () => {
    renderMenu();
    expect(trigger()).toHaveAttribute("aria-expanded", "false");
    expect(trigger()).toHaveAttribute("aria-haspopup", "menu");
    expect(menu()).toBeNull();
  });

  it("opens a menu holding About, Analytics, the dispatch pair and the API docs", () => {
    renderMenu();
    fireEvent.click(trigger());

    expect(trigger()).toHaveAttribute("aria-expanded", "true");
    const opened = menu();
    expect(opened).not.toBeNull();
    // Read in order, because the order is a decision rather than an accident: About
    // is the menu's own identity, Analytics is the one page here you would go to and
    // read, the dispatch pair is adjacent because a playbook run is a dispatch, and the
    // external reference is last because following it leaves the app.
    const labels = within(opened as HTMLElement)
      .getAllByRole("menuitem")
      .map((item) => item.textContent);
    expect(labels).toEqual([
      "About",
      "Analytics",
      "Dispatch settings",
      "Playbooks",
      "API Docs",
    ]);
  });

  it("links this app's routes through the router and the docs out of it", () => {
    // task-345 moved four entries in here and only three of them are this app's. The
    // distinction is not cosmetic: a router `<Link>` to `/docs` would ask for a route
    // nothing serves and land on the not-found page, and a plain `<a>` to
    // `/p/demo/dispatch` would reload the whole bundle to go one page sideways.
    renderMenu();
    fireEvent.click(trigger());
    const opened = menu() as HTMLElement;

    expect(within(opened).getByRole("menuitem", { name: "Analytics" })).toHaveAttribute(
      "href",
      "/p/demo/analytics",
    );
    expect(within(opened).getByRole("menuitem", { name: "Dispatch settings" })).toHaveAttribute(
      "href",
      "/p/demo/dispatch",
    );
    expect(within(opened).getByRole("menuitem", { name: "Playbooks" })).toHaveAttribute(
      "href",
      "/p/demo/playbooks",
    );
    expect(within(opened).getByRole("menuitem", { name: "API Docs" })).toHaveAttribute(
      "href",
      "/docs",
    );
  });

  it("scopes its route entries to the project it was given, encoded", () => {
    // A project id is arbitrary text, and an unencoded space makes a URL that resolves
    // to nothing. The same trap `currentDestinationPath` guards on the way in.
    client.setConfig({ baseUrl: BASE_URL });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/p/my%20project"]}>
          <ActionsMenu projectId="my project" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fireEvent.click(trigger());
    expect(screen.getByRole("menuitem", { name: "Dispatch settings" })).toHaveAttribute(
      "href",
      "/p/my%20project/dispatch",
    );
  });

  it("puts focus on the first entry, so a keyboard is already inside the menu", () => {
    renderMenu();
    fireEvent.click(trigger());
    expect(document.activeElement).toHaveTextContent("About");
  });

  it("moves between entries with the arrow keys, wrapping at both ends", () => {
    renderMenu();
    fireEvent.click(trigger());
    const opened = menu() as HTMLElement;

    fireEvent.keyDown(opened, { key: "ArrowDown" });
    expect(document.activeElement).toHaveTextContent("Analytics");
    fireEvent.keyDown(opened, { key: "ArrowUp" });
    expect(document.activeElement).toHaveTextContent("About");
    // Wraps rather than stopping: a menu where Up does nothing at the top reads as
    // broken.
    fireEvent.keyDown(opened, { key: "ArrowUp" });
    expect(document.activeElement).toHaveTextContent("API Docs");
    fireEvent.keyDown(opened, { key: "ArrowDown" });
    expect(document.activeElement).toHaveTextContent("About");
    fireEvent.keyDown(opened, { key: "End" });
    expect(document.activeElement).toHaveTextContent("API Docs");
    fireEvent.keyDown(opened, { key: "Home" });
    expect(document.activeElement).toHaveTextContent("About");
  });

  it("closes on Escape and hands focus back to the trigger", () => {
    renderMenu();
    fireEvent.click(trigger());
    fireEvent.keyDown(document, { key: "Escape" });

    expect(menu()).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("closes when something outside it is pressed", () => {
    renderMenu();
    fireEvent.click(trigger());
    fireEvent.mouseDown(document.body);
    expect(menu()).toBeNull();
  });

  it("stays open when the press lands inside the menu itself", () => {
    renderMenu();
    fireEvent.click(trigger());
    fireEvent.mouseDown(menu() as HTMLElement);
    expect(menu()).not.toBeNull();
  });

  it("closes when an entry is chosen", () => {
    renderMenu();
    fireEvent.click(trigger());
    fireEvent.click(screen.getByRole("menuitem", { name: "API Docs" }));
    expect(menu()).toBeNull();
  });

  it("closes on a route change, which is what the back button does", () => {
    renderMenu();
    fireEvent.click(trigger());
    expect(menu()).not.toBeNull();
    fireEvent.click(screen.getByRole("link", { name: "Elsewhere" }));
    expect(menu()).toBeNull();
  });

  it("tells the caller when it opens, so a sibling popup can close", () => {
    const onOpen = vi.fn();
    client.setConfig({ baseUrl: BASE_URL });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/p/demo"]}>
          <ActionsMenu projectId="demo" onOpen={onOpen} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fireEvent.click(trigger());
    expect(onOpen).toHaveBeenCalledTimes(1);
    // Not on the way out: closing this menu has nothing to say about anyone else's.
    fireEvent.click(trigger());
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});

describe("ActionsMenu About", () => {
  async function openAbout() {
    renderMenu();
    fireEvent.click(trigger());
    fireEvent.click(screen.getByRole("menuitem", { name: "About" }));
    return screen.findByRole("dialog", { name: "About AgentJobs" });
  }

  it("reads the version from the server rather than from the bundle", async () => {
    // A version no build of this app has ever had, served by the mock. A constant
    // compiled into the bundle would report the build the *tab* came from -- the one
    // answer guaranteed to be wrong once the server has been restarted underneath it,
    // which is exactly when somebody opens this panel.
    apiMockServer.use(
      http.get("*/api/version", () =>
        HttpResponse.json({ ...VERSION_IN_STEP, version: "9.9.9-from-the-server" }),
      ),
    );
    const panel = await openAbout();
    await waitFor(() =>
      expect(within(panel).getByText("9.9.9-from-the-server")).toBeInTheDocument(),
    );
  });

  it("names the server it is talking to and the project it is scoped to", async () => {
    const panel = await openAbout();
    expect(within(panel).getByText(BASE_URL)).toBeInTheDocument();
    expect(within(panel).getByText("demo")).toBeInTheDocument();
  });

  it("replaces the menu rather than stacking on top of it", async () => {
    await openAbout();
    expect(menu()).toBeNull();
  });

  it("closes on Escape and hands focus back to the trigger", async () => {
    await openAbout();
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "About AgentJobs" })).toBeNull(),
    );
    expect(document.activeElement).toBe(trigger());
  });

  it("says so plainly when the server does not answer", async () => {
    apiMockServer.use(http.get("*/api/version", () => HttpResponse.error()));
    const panel = await openAbout();
    await waitFor(() => expect(within(panel).getByText("unavailable")).toBeInTheDocument());
  });
});
