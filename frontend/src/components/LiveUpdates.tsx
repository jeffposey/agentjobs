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
  // The header's red badge. It is a count of task records, so a task write is exactly
  // and only what moves it -- and the badge sits on every surface, so this is the one
  // query in the set whose staleness is visible without opening anything.
  "getAttentionApiProjectsProjectIdAttentionGet",
  "searchTasksApiProjectsProjectIdSearchGet",
  // The same hits as whole records. No browser surface asks for it -- it exists for
  // `TaskClient.search_tasks`, which must return `Task` and so cannot take the rows
  // (task-495) -- but it answers with task data, so it belongs in the set that refetches
  // rather than in the list of reasons not to.
  "searchTasksFullApiProjectsProjectIdSearchFullGet",
  "listTasksApiProjectsProjectIdTasksGet",
  // The same listing as whole records. No browser surface asks for it -- it exists for
  // `TaskClient.list_tasks`, which must return `Task` and so cannot take the rows
  // (task-484) -- but it answers with task data, so it belongs in the set that
  // refetches rather than in the list of reasons not to.
  "listFullTasksApiProjectsProjectIdTasksFullGet",
  "listBrokenTasksApiProjectsProjectIdTasksBrokenGet",
  "getNextTaskApiProjectsProjectIdTasksNextGet",
  // The whole claimable set rather than its head. Nothing in the app asks for it today
  // -- it exists for the epic walk, which needs the set to start every eligible child --
  // but it is the same answer as `next` with the limit taken off, so it goes in the set
  // that refetches rather than the one that lists reasons for not doing so.
  "getClaimableTasksApiProjectsProjectIdTasksClaimableGet",
  // Both read the queue, and the queue is task files: a move, a reprioritize, a close
  // or a create all change what these answer, and all of them move the revision.
  "explainNextTaskApiProjectsProjectIdTasksNextExplainGet",
  "getQueueApiProjectsProjectIdQueueGet",
  "getTaskApiProjectsProjectIdTasksTaskIdGet",
  "getTaskDetailApiProjectsProjectIdTasksTaskIdDetailGet",
  // A document under review, read off the task's branch (task-594). Which file and which
  // branch are task data, and so is the handoff that follows a new commit; a commit on
  // its own moves no revision, and reopening the section is what refetches after one.
  "getDeliverableApiProjectsProjectIdTasksTaskIdDeliverablesIndexGet",
  // A dispatch and its result are log entries, so starting and finishing a run both
  // move the revision. The runs list additionally polls on its own clock while
  // something is live, because progress within a run is not a task write at all.
  "listDispatchRunsApiProjectsProjectIdDispatchRunsGet",
  // Every panel of the analytics page is a projection of task rows and their history:
  // a create or a close moves the backlog level and the totals, a handoff moves the
  // holder bands, and all of those are task writes that move the revision. It is one
  // request for the whole page (docs/analytics-design.md section 7.1), so one refetch.
  "getAnalyticsApiProjectsProjectIdAnalyticsGet",
  // A chain is log entries and nothing else -- a `chain_authorized`, its `chain_revoked`,
  // and one `check_result` per iteration -- so every write that changes what this answers
  // is a task write that moves the revision. It additionally polls on its own clock while
  // a chain is live, because an iteration ending is minutes away and a reader sitting on
  // the page should not have to wait for a revision poll to see the turn land.
  "readTaskChainsApiProjectsProjectIdTasksTaskIdChainsGet",
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
    "getModelStatusApiProjectsProjectIdModelGet",
    "Whether this machine has a model configured for drafting. It answers from " +
      "~/.agentjobs/model.yaml and a kill-switch file, neither of which a task write " +
      "touches, and the app reads it unscoped anyway -- what is configured is a " +
      "property of the machine, not of a project.",
  ],
  [
    "listWebhooksApiProjectsProjectIdWebhooksGet",
    "Webhook subscriptions are configuration, not task data; task writes never change them.",
  ],
  [
    "getPushStatusApiProjectsProjectIdPushGet",
    "The devices registered for mobile push, and the key they subscribe against. " +
      "Configuration about hardware rather than about work: it changes only when " +
      "somebody presses a button in the notifications panel, and refetching it on " +
      "every task write would be polling for an event that cannot happen without " +
      "that panel already knowing (task-423).",
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
    "readDispatchRunTranscriptApiProjectsProjectIdDispatchRunsRunIdTranscriptGet",
    "The same run as structured entries, and exempt for exactly the reason the tail is: "
      + "it tracks what the session records about itself, which is not a task write, and "
      + "the same panel polls it on the same clock.",
  ],
  [
    "readDispatchRunOutputApiProjectsProjectIdDispatchRunsRunIdOutputGet",
    "A run's captured transcript, opened in its own tab as text rather than fetched "
      + "through the query client. It grows with the process, not with task writes.",
  ],
  [
    "getPlaybooksApiProjectsProjectIdPlaybooksGet",
    "Playbooks are repository files under the project's playbooks/ directory, edited "
      + "and committed like any other source. No task write can change one, so the "
      + "revision is the wrong signal for them.",
  ],
  [
    "getPlaybookApiProjectsProjectIdPlaybooksNameGet",
    "Same as the playbook list: one repository file, changed by a commit rather than "
      + "by a task write.",
  ],
  [
    "readTaskFinishApiProjectsProjectIdDispatchFinishesTaskIdGet",
    "What a scripted finish is doing right now, read out of ~/.agentjobs/finishes. It "
      + "moves every few seconds for three minutes and a task write is neither "
      + "necessary nor sufficient for that -- most of a finish's steps write nothing to "
      + "the record at all. The panel polls it on its own two-second clock while one is "
      + "live, which is the signal that actually tracks it.",
  ],
  [
    "readTaskFinishOutputApiProjectsProjectIdDispatchFinishesTaskIdOutputGet",
    "A finish's output in full, opened in its own tab as text rather than fetched "
      + "through the query client. Written when the process ends, not when a task "
      + "file changes.",
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
