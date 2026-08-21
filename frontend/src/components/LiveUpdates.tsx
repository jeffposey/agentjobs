import { useEffect, useState } from "react";
import { type Query, type QueryClient, useQueryClient } from "@tanstack/react-query";

import { getProjectRevisionApiProjectsProjectIdRevisionGet } from "../api/generated";

export const NORMAL_POLL_MS = 15_000;
export const FAST_RETRY_MS = 2_000;
export const MISSES_BEFORE_WARNING = 2;

/**
 * Project-scoped queries whose data changes when a task file changes. A revision
 * change refetches exactly these.
 *
 * This is an allowlist, so drift is silent by construction: a read endpoint added
 * later and left out of it never refetches, the poll keeps succeeding, and the only
 * symptom is a screen that quietly stops updating. `LiveUpdates.drift.test.tsx`
 * exists to make that loud -- it requires every project-scoped generated query to
 * appear here or in NON_TASK_PROJECT_QUERY_IDS below.
 */
export const PROJECT_TASK_QUERY_IDS = new Set([
  "getDashboardApiProjectsProjectIdDashboardGet",
  "searchTasksApiProjectsProjectIdSearchGet",
  "listTasksApiProjectsProjectIdTasksGet",
  "listBrokenTasksApiProjectsProjectIdTasksBrokenGet",
  "getNextTaskApiProjectsProjectIdTasksNextGet",
  // Both read the queue, and the queue is task files: a move, a reprioritize, a close
  // or a create all change what these answer, and all of them move the revision.
  "explainNextTaskApiProjectsProjectIdTasksNextExplainGet",
  "getQueueApiProjectsProjectIdQueueGet",
  "getTaskApiProjectsProjectIdTasksTaskIdGet",
  "getTaskDetailApiProjectsProjectIdTasksTaskIdDetailGet",
  // A dispatch and its result are log entries, so starting and finishing a run both
  // move the revision. The runs list additionally polls on its own clock while
  // something is live, because progress within a run is not a task write at all.
  "listDispatchRunsApiProjectsProjectIdDispatchRunsGet",
]);

/**
 * Project-scoped queries that deliberately do NOT refetch on a revision change,
 * each with the reason it is exempt. Listing them explicitly is what lets the drift
 * test tell "considered and excluded" apart from "forgotten".
 */
export const NON_TASK_PROJECT_QUERY_IDS = new Map([
  [
    "getProjectRevisionApiProjectsProjectIdRevisionGet",
    "The poller's own endpoint. Refetching it from its own result would loop.",
  ],
  [
    "listWebhooksApiProjectsProjectIdWebhooksGet",
    "Webhook subscriptions are configuration, not task data; task writes never change them.",
  ],
  [
    "getWebhookApiProjectsProjectIdWebhooksWebhookIdGet",
    "Same as the webhook list: configuration, unaffected by task writes.",
  ],
  [
    "getAttachmentApiProjectsProjectIdTasksTaskIdAttachmentsFilenameGet",
    "An attachment is content-addressed -- its filename is the hash of its bytes -- so "
      + "the response for a given URL can never change. It is also rendered by the "
      + "browser as an <img> rather than fetched through the query client.",
  ],
  [
    "getDispatchStateApiProjectsProjectIdDispatchGet",
    "Machine-local configuration -- ~/.agentjobs/dispatch.yaml and the sentinel file. "
      + "No task write can change it, and it is refetched explicitly after the toggle.",
  ],
  [
    "readDispatchRunTailApiProjectsProjectIdDispatchRunsRunIdTailGet",
    "The end of a run's output while it is being watched. It changes when the process "
      + "writes, not when a task file does, and the panel showing it polls on the "
      + "session poller's own clock -- refetching it on every task write would read the "
      + "same bytes back sooner and more often for nothing.",
  ],
  [
    "readDispatchRunOutputApiProjectsProjectIdDispatchRunsRunIdOutputGet",
    "A run's captured transcript, opened in its own tab as text rather than fetched "
      + "through the query client. It grows with the process, not with task writes.",
  ],
]);

type GeneratedQueryKey = {
  _id?: string;
  path?: { project_id?: string };
};

export function isProjectTaskQuery(query: Query, projectId: string): boolean {
  const key = query.queryKey[0] as GeneratedQueryKey | undefined;
  return Boolean(
    key?._id &&
      PROJECT_TASK_QUERY_IDS.has(key._id) &&
      key.path?.project_id === projectId,
  );
}

export function invalidateProjectTaskQueries(queryClient: QueryClient, projectId: string) {
  return queryClient.invalidateQueries({
    predicate: (query) => isProjectTaskQuery(query, projectId),
    refetchType: "active",
  });
}

export function LiveUpdateStatus({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [consecutiveMisses, setConsecutiveMisses] = useState(0);
  const [recentlyUpdated, setRecentlyUpdated] = useState(false);

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    let currentRevision: string | undefined;
    let misses = 0;
    let inFlight = false;
    let rerunRequested = false;
    let controller: AbortController | undefined;

    setConsecutiveMisses(0);
    setRecentlyUpdated(false);

    const schedule = (delay: number) => {
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(() => void check(), delay);
    };

    const check = async () => {
      if (disposed) return;
      if (inFlight) {
        rerunRequested = true;
        return;
      }

      inFlight = true;
      controller = new AbortController();
      let nextDelay = NORMAL_POLL_MS;
      try {
        const response = await getProjectRevisionApiProjectsProjectIdRevisionGet({
          path: { project_id: projectId },
          signal: controller.signal,
          throwOnError: true,
        });
        if (disposed) return;

        const nextRevision = response.data.revision;
        if (currentRevision === undefined) {
          currentRevision = nextRevision;
        } else if (nextRevision !== currentRevision) {
          currentRevision = nextRevision;
          await invalidateProjectTaskQueries(queryClient, projectId);
          if (disposed) return;
          setRecentlyUpdated(true);
        }

        misses = 0;
        setConsecutiveMisses(0);
      } catch {
        if (disposed) return;
        misses += 1;
        setConsecutiveMisses(misses);
        nextDelay = misses === 1 ? FAST_RETRY_MS : NORMAL_POLL_MS;
      } finally {
        inFlight = false;
        controller = undefined;
        if (!disposed) {
          if (rerunRequested) {
            rerunRequested = false;
            schedule(0);
          } else {
            schedule(nextDelay);
          }
        }
      }
    };

    const checkNow = () => {
      if (inFlight) rerunRequested = true;
      else schedule(0);
    };
    const checkWhenVisible = () => {
      if (document.visibilityState === "visible") checkNow();
    };

    schedule(0);
    window.addEventListener("focus", checkNow);
    window.addEventListener("online", checkNow);
    document.addEventListener("visibilitychange", checkWhenVisible);
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
      controller?.abort();
      window.removeEventListener("focus", checkNow);
      window.removeEventListener("online", checkNow);
      document.removeEventListener("visibilitychange", checkWhenVisible);
    };
  }, [projectId, queryClient]);

  useEffect(() => {
    if (!recentlyUpdated) return;
    const timer = window.setTimeout(() => setRecentlyUpdated(false), 5_000);
    return () => window.clearTimeout(timer);
  }, [recentlyUpdated]);

  if (consecutiveMisses >= MISSES_BEFORE_WARNING) {
    return (
      <div className="mb-4 rounded-lg border border-orange-500/50 bg-orange-950/30 px-4 py-3 text-sm text-orange-100" role="alert">
        <strong>Live updates are paused.</strong>{" "}
        Showing the last successfully loaded task data while AgentJobs reconnects.
      </div>
    );
  }

  if (recentlyUpdated) {
    return (
      <div className="mb-4 rounded-lg border border-blue-500/40 bg-blue-950/30 px-4 py-2 text-sm text-blue-100" role="status" aria-live="polite">
        Task data updated just now.
      </div>
    );
  }

  return null;
}
