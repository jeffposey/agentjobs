import { describe, expect, it } from "vitest";

import { isDashboardPath } from "./App";

/**
 * Which surface gets the one-viewport frame (task-294).
 *
 * The predicate decides whether the shell is `h-dvh overflow-hidden` or the ordinary
 * `min-h-dvh` document scroll, so getting it wrong in either direction is a real
 * defect: a false positive frames the Tasks page and silently breaks
 * `dragAutoScroll.ts`, which scrolls with `window.scrollBy`; a false negative leaves
 * the Dashboard scrolling, which is the whole thing the task exists to stop.
 *
 * Asserted directly rather than through a rendered page, because the interesting cases
 * are paths -- a trailing slash, a project id with a slash in it -- and rendering each
 * of them would test the router rather than this rule.
 */
describe("isDashboardPath", () => {
  it("frames the project's index route, with or without a trailing slash", () => {
    expect(isDashboardPath("/p/agentjobs", "agentjobs")).toBe(true);
    expect(isDashboardPath("/p/agentjobs/", "agentjobs")).toBe(true);
  });

  it("leaves every other surface on the document scroll", () => {
    for (const path of [
      "/p/agentjobs/tasks",
      "/p/agentjobs/tasks/task-294",
      "/p/agentjobs/tasks/new",
      "/p/agentjobs/dispatch",
      "/p/agentjobs/playbooks",
      "/p/agentjobs/runs",
    ]) {
      expect(isDashboardPath(path, "agentjobs"), path).toBe(false);
    }
  });

  it("compares the encoded id, so a project whose id needs escaping still matches", () => {
    // Route params arrive decoded and the pathname does not, so comparing the raw id
    // would leave such a project's Dashboard unframed -- and nothing on the page would
    // say why.
    expect(isDashboardPath("/p/my%2Fproject", "my/project")).toBe(true);
    expect(isDashboardPath("/p/my/project", "my/project")).toBe(false);
  });

  it("does not match another project's Dashboard by prefix", () => {
    expect(isDashboardPath("/p/agentjobs-two", "agentjobs")).toBe(false);
  });
});
