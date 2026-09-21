import type {
  AttachmentUpload,
  ContextPointer,
  Dependency,
  Priority,
  TaskCreateRequest,
} from "../api/generated";

/**
 * Building one captured thing into a normal task request.
 *
 * Kept apart from the component that collects it because task-121's capture tray has
 * to produce exactly the same records from many drafts at once. A second builder
 * would let the batch path drift from the single-capture path -- different tags,
 * different context wording -- and captures would stop being one filterable
 * population.
 *
 * Since task-346 this is the **only** builder. Filing a finding and authoring a task
 * are one act behind one control, so they are one request assembled in one place: what
 * used to be the create form's own assembly of a `TaskCreateRequest` arrives here as
 * `spec`, and a capture that fills none of it produces exactly the request a reported
 * issue produced before.
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
 * Everything the capture form's specification section holds, as it holds it.
 *
 * Threaded through this builder rather than posted separately, because the point of
 * this module is that one capture becomes one ordinary task by one path. A second
 * assembly for the fuller half would drift in tags and provenance -- exactly what this
 * file exists to prevent.
 *
 * **Every member is optional in effect**: an empty string, an empty list or `undefined`
 * is dropped rather than sent, so the fifteen-second capture produces byte-identically
 * the request it produced when this was two forms. `lifecycle` is deliberately absent:
 * the `actionable` checkbox is still the only thing that decides it.
 */
export type CapturedSpec = {
  summary: string;
  intent: string;
  constraints: string;
  out_of_scope: string;
  acceptance: Array<string>;
  /** Curated read-this-first paths. */
  context?: Array<ContextPointer>;
  /** `needs` edges the author typed; the captured page's `related` edge is added below. */
  dependencies?: Array<Dependency>;
  id?: string;
  parent?: string;
  category?: string;
  effort?: string;
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
 * How the provenance block below opens, and the seam a later insertion finds it by.
 *
 * Exported because task-121 appends a drafted specification to a capture's description
 * *above* this block, and something has to know where "above" is. Producer and consumer
 * are this one module, so the coupling is local and a change to the wording below cannot
 * silently break the insertion.
 */
export const PROVENANCE_OPENING = "---\nReported from the AgentJobs UI by ";

/**
 * Put a block into a capture's description, above its provenance footer.
 *
 * The footer says where the finding was noticed and must stay last, so an expansion
 * arriving later cannot simply be appended. A description with no footer -- which no
 * capture produces, but a test or a future caller might -- gets the block on the end.
 */
export function insertAboveProvenance(description: string, block: string): string {
  const body = description ?? "";
  const at = body.lastIndexOf(`\n\n${PROVENANCE_OPENING}`);
  if (at < 0) return [body, block].filter(Boolean).join("\n\n");
  return `${body.slice(0, at)}\n\n${block}${body.slice(at)}`;
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
  const lines = [`${PROVENANCE_OPENING}${reporter}, at \`${context.route}\`.`];
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
 * Turn one capture into the create-task request that records it.
 *
 * `draft` lands it on a human to finish specifying, which is the honest state for
 * something typed in fifteen seconds; `actionable` is the author asserting it is
 * already executable.
 */
export function buildCaptureRequest({
  draft,
  context,
  destinationProjectId,
  reporter,
  operationId,
  attachments = [],
  tags,
  priority,
  spec,
}: {
  draft: IssueDraft;
  context: ReportContext;
  destinationProjectId: string;
  reporter: string;
  operationId: string;
  attachments?: Array<AttachmentUpload>;
  /** What the Tags field held. Defaults to the reported-issue population when absent. */
  tags?: Array<string>;
  priority?: Priority;
  spec?: CapturedSpec;
}): TaskCreateRequest {
  const details = draft.details.trim();
  const description = [details, provenance(context, reporter, destinationProjectId)]
    .filter(Boolean)
    .join("\n\n");
  const sameProject = context.projectId === destinationProjectId;
  // Empty values are dropped rather than sent, so a capture that opened no
  // specification produces exactly the request a reported issue produced before the
  // two forms became one.
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
        ...(spec.context?.length ? { context: spec.context } : {}),
        ...(spec.id ? { id: spec.id } : {}),
        ...(spec.parent ? { parent: spec.parent } : {}),
        ...(spec.category?.trim() ? { category: spec.category.trim() } : {}),
        ...(spec.effort?.trim() ? { effort: spec.effort.trim() } : {}),
      }
    : {};
  // The captured page's own edge goes last, so an author's typed `needs` lines read
  // first and the provenance edge is never mistaken for one of them.
  const dependencies: Array<Dependency> = [
    ...(spec?.dependencies ?? []),
    ...(context.taskId && sameProject
      ? [
          {
            task: context.taskId,
            type: "related" as const,
            note: "Reported while viewing this task.",
          },
        ]
      : []),
  ];
  return {
    title: draft.title.trim(),
    description,
    ...specFields,
    lifecycle: draft.actionable ? "ready" : "draft",
    ...(priority ? { priority } : {}),
    tags: tags ?? [REPORTED_ISSUE_TAG],
    actor: reporter,
    operation_id: operationId,
    attachments,
    dependencies,
  };
}
