import { useEffect, useState } from "react";
import { type Query, type QueryClient, useQueryClient } from "@tanstack/react-query";

import { getProjectRevisionApiProjectsProjectIdRevisionGet } from "../api/generated";

export const NORMAL_POLL_MS = 15_000;
export const FAST_RETRY_MS = 2_000;
export const MISSES_BEFORE_WARNING = 2;

const PROJECT_TASK_QUERY_IDS = new Set([
  "getDashboardApiProjectsProjectIdDashboardGet",
  "searchTasksApiProjectsProjectIdSearchGet",
  "listTasksApiProjectsProjectIdTasksGet",
  "listBrokenTasksApiProjectsProjectIdTasksBrokenGet",
  "getNextTaskApiProjectsProjectIdTasksNextGet",
  "getTaskApiProjectsProjectIdTasksTaskIdGet",
  "getTaskDetailApiProjectsProjectIdTasksTaskIdDetailGet",
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
