import { type Query } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import * as generated from "../api/generated/@tanstack/react-query.gen";
import {
  isProjectTaskQuery,
  NON_TASK_PROJECT_QUERY_IDS,
  PROJECT_TASK_QUERY_IDS,
} from "./LiveUpdates";

/**
 * Live updates refetch a hardcoded allowlist of query ids. Nothing about adding a
 * new project-scoped read endpoint forces anyone to notice that allowlist, and the
 * failure mode when they do not is invisible: polls keep succeeding, the revision
 * keeps advancing, and the view silently stops updating. These tests are the thing
 * that fails instead.
 */

// Every project-scoped path parameter the generated key builders might read. Unused
// ones are ignored, so this stays correct as endpoints are added.
const STUB_OPTIONS = {
  path: { project_id: "alpha", task_id: "task-1", webhook_id: "hook-1" },
} as never;

type KeyBuilder = (options: never) => Array<{ _id?: string } | undefined>;

function generatedProjectScopedQueryIds(): string[] {
  const ids: string[] = [];
  for (const [name, value] of Object.entries(generated)) {
    if (!name.endsWith("QueryKey") || typeof value !== "function") continue;
    const id = (value as KeyBuilder)(STUB_OPTIONS)?.[0]?._id;
    // Project-scoped reads are the only ones a project revision can invalidate.
    if (id && id.includes("ApiProjectsProjectId")) ids.push(id);
  }
  return ids.sort();
}

describe("live-update invalidation coverage", () => {
  it("finds project-scoped queries to check, so a broken enumeration cannot pass vacuously", () => {
    expect(generatedProjectScopedQueryIds().length).toBeGreaterThan(5);
  });

  it("classifies every project-scoped generated query as task data or explicitly not", () => {
    const unclassified = generatedProjectScopedQueryIds().filter(
      (id) => !PROJECT_TASK_QUERY_IDS.has(id) && !NON_TASK_PROJECT_QUERY_IDS.has(id),
    );

    expect(
      unclassified,
      "A project-scoped endpoint exists that live updates never refetch. If its data " +
        "changes when a task file changes, add it to PROJECT_TASK_QUERY_IDS. If it does " +
        "not, add it to NON_TASK_PROJECT_QUERY_IDS with the reason.",
    ).toEqual([]);
  });

  it("keeps no classified id that the generated client no longer defines", () => {
    const generatedIds = new Set(generatedProjectScopedQueryIds());
    const stale = [...PROJECT_TASK_QUERY_IDS, ...NON_TASK_PROJECT_QUERY_IDS.keys()]
      .filter((id) => !generatedIds.has(id))
      .sort();

    expect(
      stale,
      "These ids are classified but no longer generated -- the endpoint was renamed or " +
        "removed, so the entry is now dead and whatever replaced it may be unclassified.",
    ).toEqual([]);
  });
});

describe("the predicate against real generated keys", () => {
  // The other suites assert on strings. This one asserts against the actual key
  // objects, so a change to the generated key *shape* -- the nesting of `path`, or
  // where `_id` lives -- fails here rather than silently matching nothing.
  //
  // isProjectTaskQuery only ever reads queryKey, so a stub carrying just that is
  // enough; the double assertion is because a real Query has a large surface this
  // test has no reason to fake.
  const asQuery = (queryKey: unknown) => ({ queryKey }) as unknown as Query;

  const dashboardKey = generated.getDashboardApiProjectsProjectIdDashboardGetQueryKey({
    path: { project_id: "alpha" },
  });

  it("matches a real generated key for the addressed project", () => {
    expect(isProjectTaskQuery(asQuery(dashboardKey), "alpha")).toBe(true);
  });

  it("does not match the same query belonging to another project", () => {
    expect(isProjectTaskQuery(asQuery(dashboardKey), "beta")).toBe(false);
  });

  it("does not match an explicitly excluded project-scoped query", () => {
    const revisionKey = generated.getProjectRevisionApiProjectsProjectIdRevisionGetQueryKey({
      path: { project_id: "alpha" },
    });
    expect(isProjectTaskQuery(asQuery(revisionKey), "alpha")).toBe(false);
  });
});
