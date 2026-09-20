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

/**
 * The spec fields a model drafted and the reporter kept, if they used the option.
 *
 * Threaded through this builder rather than posted separately, because the point of
 * this module is that one reported issue becomes one ordinary task by one path. An
 * AI-assisted report that skipped it would be a second builder with different tags and
 * different provenance -- the drift this file exists to prevent.
 *
 * It carries *spec* fields only. There is no member here for lifecycle, ball, priority,
 * parent, dependencies or actor: those stay the reporter's, and the `actionable`
 * checkbox above is still the only thing that decides the lifecycle.
 */
export type ReportedSpec = {
  summary: string;
  intent: string;
  constraints: string;
  out_of_scope: string;
  acceptance: Array<string>;
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
  spec,
}: {
  draft: IssueDraft;
  context: ReportContext;
  destinationProjectId: string;
  reporter: string;
  operationId: string;
  attachments?: Array<AttachmentUpload>;
  spec?: ReportedSpec;
}): TaskCreateRequest {
  const details = draft.details.trim();
  const description = [details, provenance(context, reporter, destinationProjectId)]
    .filter(Boolean)
    .join("\n\n");
  const sameProject = context.projectId === destinationProjectId;
  // Empty strings are dropped rather than sent, so a report with no spec produces
  // exactly the request it produced before this option existed.
  const specFields = spec
    ? {
        ...(spec.summary.trim() ? { summary: spec.summary.trim() } : {}),
        ...(spec.intent.trim() ? { intent: spec.intent.trim() } : {}),
        ...(spec.constraints.trim() ? { constraints: spec.constraints.trim() } : {}),
        ...(spec.out_of_scope.trim() ? { out_of_scope: spec.out_of_scope.trim() } : {}),
        ...(spec.acceptance.length
          ? {
              acceptance: spec.acceptance.map((text, index) => ({
                id: `ac-${index + 1}`,
                text,
                status: "pending" as const,
              })),
            }
          : {}),
      }
    : {};
  return {
    title: draft.title.trim(),
    description,
    ...specFields,
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
