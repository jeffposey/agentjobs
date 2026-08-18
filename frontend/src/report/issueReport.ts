import type { AttachmentUpload, TaskCreateRequest } from "../api/generated";

/**
 * Building one reported issue into a normal task request.
 *
 * Kept apart from the component that collects it because task-121's capture tray has
 * to produce exactly the same records from many drafts at once. A second builder
 * would let the batch path drift from the single-report path -- different tags,
 * different context wording -- and reported issues would stop being one filterable
 * population.
 */

/** Tag every reported issue carries, so the population is filterable. */
export const REPORTED_ISSUE_TAG = "reported-issue";

/** Where the reporter was when they noticed something. */
export type ReportContext = {
  /** The in-app route, without the /app basename. */
  route: string;
  /** The project being viewed, or null on a page that has none. */
  projectId: string | null;
  /** The task being viewed, when there was one. */
  taskId: string | null;
};

/** What the reporter typed. */
export type IssueDraft = {
  title: string;
  details: string;
  /** True when the reporter says an agent can pick this up as it stands. */
  actionable: boolean;
};

const TASK_ROUTE = /^\/p\/([^/]+)\/tasks\/([^/]+)$/;
const PROJECT_ROUTE = /^\/p\/([^/]+)(?:\/|$)/;

/** Route segments under /tasks/ that are pages, not task ids. */
const NOT_A_TASK_ID = new Set(["new"]);

/**
 * Read the reporter's location off the current route.
 *
 * Derived from the URL rather than passed down from whichever page is mounted: the
 * reporter is global chrome and must behave identically on a page that knows nothing
 * about it, including the ones that render before any project resolves.
 */
export function readReportContext(pathname: string): ReportContext {
  const taskMatch = TASK_ROUTE.exec(pathname);
  if (taskMatch?.[1] && taskMatch[2] && !NOT_A_TASK_ID.has(taskMatch[2])) {
    return {
      route: pathname,
      projectId: decodeURIComponent(taskMatch[1]),
      taskId: decodeURIComponent(taskMatch[2]),
    };
  }
  const projectMatch = PROJECT_ROUTE.exec(pathname);
  return {
    route: pathname,
    projectId: projectMatch?.[1] ? decodeURIComponent(projectMatch[1]) : null,
    taskId: null,
  };
}

/**
 * The provenance block appended to a reported issue's description.
 *
 * Prose in the description rather than a `links[]` entry, because the durable half of
 * "where I was" is the in-app route; the other half is a host and port that differ
 * between localhost and the tailnet and mean nothing to a reader three weeks later.
 * A `related` dependency still carries the viewed task structurally when the report
 * lands in the same project, so nothing filterable is lost.
 */
function provenance(context: ReportContext, reporter: string, destinationProjectId: string): string {
  const lines = [
    "---",
    `Reported from the AgentJobs UI by ${reporter}, at \`${context.route}\`.`,
  ];
  if (context.taskId && context.projectId !== destinationProjectId) {
    lines.push(
      `Noticed while viewing \`${context.taskId}\` in project \`${context.projectId}\`, ` +
        `which is not the project this issue was filed into.`,
    );
  } else if (context.taskId) {
    lines.push(`Noticed while viewing \`${context.taskId}\`.`);
  } else if (context.projectId && context.projectId !== destinationProjectId) {
    lines.push(`Noticed while viewing project \`${context.projectId}\`.`);
  }
  return lines.join("\n");
}

/**
 * Turn one draft into the create-task request that records it.
 *
 * `draft` lands the issue on a human to finish specifying, which is the honest state
 * for something typed in fifteen seconds; `actionable` is the reporter asserting it is
 * already executable.
 */
export function buildIssueTaskRequest({
  draft,
  context,
  destinationProjectId,
  reporter,
  operationId,
  attachments = [],
}: {
  draft: IssueDraft;
  context: ReportContext;
  destinationProjectId: string;
  reporter: string;
  operationId: string;
  attachments?: Array<AttachmentUpload>;
}): TaskCreateRequest {
  const details = draft.details.trim();
  const description = [details, provenance(context, reporter, destinationProjectId)]
    .filter(Boolean)
    .join("\n\n");
  const sameProject = context.projectId === destinationProjectId;
  return {
    title: draft.title.trim(),
    description,
    lifecycle: draft.actionable ? "ready" : "draft",
    tags: [REPORTED_ISSUE_TAG],
    actor: reporter,
    operation_id: operationId,
    attachments,
    dependencies:
      context.taskId && sameProject
        ? [
            {
              task: context.taskId,
              type: "related",
              note: "Reported while viewing this task.",
            },
          ]
        : [],
  };
}
