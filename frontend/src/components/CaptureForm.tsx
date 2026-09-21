import { useCallback, useEffect, useId, useRef, useState } from "react";

import type {
  AttachmentUpload,
  DispatchStarted,
  Priority,
  SpecDraftResponse,
  Task,
  TaskCreateRequest,
} from "../api/generated";
import { toUploads, type PendingAttachment } from "../report/attachments";
import { draftHasContent, type CaptureDraft } from "../report/draftStore";
import { newOperationId } from "../api/operationId";
import {
  REPORTED_ISSUE_TAG,
  buildCaptureRequest,
  type ReportContext,
} from "../report/issueReport";
import {
  DRAFTABLE_FIELDS,
  EMPTY_VALUES,
  applySpecDraft,
  type DraftableField,
  type DraftableValues,
} from "../report/specDraft";
import type { DispatchStateView } from "../api/types";
import { AttachmentPicker } from "./AttachmentPicker";
import { DictationControl, DictationNote } from "./DictationControl";
import {
  StartOnFileCheckbox,
  fileAndMaybeStart,
  startOnFileGate,
  type FiledOutcome,
} from "./DispatchOnCreate";
import { SpecDraftControl } from "./SpecDraftControl";

/**
 * The one form behind the one capture control (task-346).
 *
 * **Filing a finding and authoring a task were two forms, and they are now one.** They
 * always filed the same thing -- task-120's constraint is that every reported issue
 * becomes an ordinary task through the managed path -- and what differed was only how
 * much you had to type. So this form opens with the two fields a fifteen-second report
 * needs, and one control expands it *in place* to everything `/tasks/new` ever
 * collected. A report that turns out to deserve acceptance criteria is not refiled; the
 * section simply opens under what is already typed.
 *
 * **It is rendered twice and implemented once.** `CaptureControl` renders it inside a
 * dialog from the top bar, and the `/tasks/new` route renders it as a page with the
 * spec section already open, so deep links and the Dashboard's "File a new task" keep
 * working. A second implementation of the same form is the drift `issueReport.ts` was
 * factored out to prevent, and two of them is how the product ended up with two
 * controls in opposite corners in the first place.
 *
 * **The core stays controlled and the specification stays uncontrolled.** Title,
 * details, attachments and destination are React state because the attachment picker
 * and the destination select need it; every spec field is a plain input read through
 * `FormData` at submit, which is what lets a draft write into the inputs and an undo
 * restore the person's own text rather than a normalised copy of it.
 */

/**
 * The specification inputs, which are uncontrolled and therefore live in the DOM.
 *
 * Listed once, here, because a draft has to read and write exactly the fields
 * `buildRequest` reads: a field missing from this list is one that is typed, restored as
 * blank, and silently lost -- the failure this whole feature exists to remove, wearing a
 * smaller hat. `title` and `details` are absent because they are React state.
 */
const DRAFTED_FIELDS = [
  "summary",
  "intent",
  "constraints",
  "out_of_scope",
  "context",
  "acceptance",
  "id",
  "parent",
  "category",
  "effort",
  "tags",
  "dependencies",
] as const;

const inputClass =
  "touch-target mt-1 w-full rounded-lg border border-dark-border bg-dark-bg px-3 py-2 text-dark-text placeholder:text-dark-muted focus:border-blue-500 focus:outline-none";
const textareaClass = `${inputClass} min-h-28`;
const sectionClass = "space-y-4 rounded-lg border border-dark-border bg-dark-bg p-4";

/** A project this form can file into, reduced to what it needs. */
export type CaptureDestination = {
  id: string;
  name: string;
  /** The single human actor configured for the project, or null when there is none. */
  reporter: string | null;
};

export type CaptureFormProps = {
  /** Where the capture happened, which is what the record carries as provenance. */
  context: ReportContext;
  /** Everything that can be filed into, in the order the picker offers them. */
  destinations: ReadonlyArray<CaptureDestination>;
  /** Task ids offered as completions for Parent. */
  existingTaskIds: ReadonlyArray<string>;
  /** Files the request. Returns the stored task, which the caller then shows. */
  onSubmit: (projectId: string, request: TaskCreateRequest) => Promise<Task>;
  /**
   * Called with what the submit did. The dialog shows a receipt; the page navigates away.
   *
   * The whole outcome rather than the task alone, because since task-176 a submit can
   * do two things -- file, and start an agent -- which succeed and fail independently.
   * `outcome.task` is the record, which exists whatever happened to the start.
   */
  onFiled: (projectId: string, outcome: FiledOutcome) => void;
  /**
   * Machine and project dispatch gates for the destination currently selected, or null
   * while the answer is in flight (task-176).
   *
   * Supplied rather than queried here, and the destination is reported back through
   * {@link CaptureFormProps.onDestinationChange} so the caller can ask about the right
   * project. That is this file's existing arrangement for `onExpandedChange`, kept for
   * the same reason: the form owns no requests, so it renders in a test without a
   * server answering for a surface the test is not about.
   */
  dispatchState?: DispatchStateView | null;
  /** Which project the form would file into now. Fires on mount and on every change. */
  onDestinationChange?: (projectId: string) => void;
  /**
   * Start an agent on the task just filed. Absent means the box is never offered.
   *
   * Rejects with the dispatch's refusal, which `fileAndMaybeStart` turns into the
   * "filed, not started" half of the outcome -- it never undoes the create.
   */
  onStart?: (projectId: string, taskId: string) => Promise<DispatchStarted>;
  /** Rendered at the end of the action row's left side, e.g. the dialog's Cancel. */
  cancel?: React.ReactNode;
  /** True on `/tasks/new`, where the whole specification is the point of the page. */
  startExpanded?: boolean;
  /** Focused on mount. The dialog wants it; the page leaves it to the browser. */
  autoFocus?: boolean;
  /**
   * Told when the specification opens or closes.
   *
   * The dialog uses it to decide whether the Parent field's completions are worth
   * fetching: a capture that never expands never needs the project's task list, and
   * paying for one to put a dialog on screen is a request nobody asked for.
   */
  onExpandedChange?: (expanded: boolean) => void;
  /**
   * Put what is typed on the tray instead of filing it now (task-121). Absent means the
   * surface offers no tray, and the form is the single-capture form it has always been.
   *
   * It hands over the assembled request rather than the typed fields, because the whole
   * argument for one builder is that a batch must produce the records a single capture
   * produces -- the same tags, the same attribution, and the provenance of *this* page
   * rather than whichever page the batch is eventually sent from. The images travel
   * beside it: the tray keeps one copy of the bytes and joins them back on when it
   * sends, so the stored list is not carrying each screenshot twice.
   */
  onCollect?: (collected: {
    projectId: string;
    request: TaskCreateRequest;
    attachments: Array<PendingAttachment>;
    route: string;
    /** The note as typed, which is what a model is asked to expand -- never the footer. */
    note: string;
    /** Whether the drafting checkbox is on, so one control governs both paths. */
    wantsDraft: boolean;
  }) => void;
  /**
   * What was being typed here when the page last went away, put back (task-512).
   *
   * Read once, at mount, because that is what it is: the initial value of a form.
   * `CaptureControl` remounts this component for each new capture, so "at mount" is also
   * exactly when a restore is wanted and never after -- the second capture of a session
   * must start empty, not holding the first one's text.
   */
  initialDraft?: CaptureDraft | null;
  /**
   * Called with everything currently typed, or null once there is nothing to lose.
   *
   * Synchronous and undebounced, because it answers two questions at two speeds. One is
   * "keep this", which the caller may defer as long as it likes. The other is "is this
   * tab holding unsent text", which decides whether a service worker may reload the page
   * out from under it -- and an answer 400ms behind the keyboard is wrong at exactly the
   * moment it is asked.
   *
   * Null is also how the form says it has *finished* with a draft: it is sent the moment
   * a finding becomes a tray item or a task, so a restore can never resurrect something
   * that was already collected or filed.
   */
  onDraftChange?: (draft: CaptureDraft | null) => void;
};

function optional(value: string) {
  const trimmed = value.trim();
  return trimmed || undefined;
}

function parseRows(value: string, label: string, requireReason: boolean) {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, index) => {
      const [first, ...rest] = line.split("|").map((part) => part.trim());
      const reason = rest.join(" | ");
      if (!first || (requireReason && !reason)) {
        throw new Error(
          `${label} line ${index + 1} must use ${requireReason ? "path | why" : "task-id | optional note"}.`,
        );
      }
      return { first, reason };
    });
}

export function CaptureForm({
  context,
  destinations,
  existingTaskIds,
  onSubmit,
  onFiled,
  cancel,
  startExpanded = false,
  autoFocus = false,
  onExpandedChange,
  dispatchState = null,
  onDestinationChange,
  onStart,
  onCollect,
  initialDraft = null,
  onDraftChange,
}: CaptureFormProps) {
  const formRef = useRef<HTMLFormElement>(null);
  // The attachment picker owns its own textarea, so the microphone beside it needs a
  // handle on the element. Every other field is reached through `fieldElement`.
  const detailsRef = useRef<HTMLTextAreaElement>(null);
  const specHeadingId = useId();

  const [title, setTitle] = useState(initialDraft?.title ?? "");
  const [details, setDetails] = useState(initialDraft?.details ?? "");
  const [attachments, setAttachments] = useState<Array<PendingAttachment>>(
    initialDraft?.attachments ?? [],
  );
  const [destination, setDestination] = useState(
    initialDraft?.destination ?? context.projectId ?? "",
  );
  const [actionable, setActionable] = useState(initialDraft?.actionable ?? false);
  // Unchecked on every mount, and never persisted anywhere. See `DispatchOnCreate.tsx`
  // for why a remembered "spend money" is the default this feature exists not to have;
  // the dialog remounts this form for a second capture, so it resets there too.
  const [startOnFile, setStartOnFile] = useState(false);
  const [priority, setPriority] = useState<Priority>(initialDraft?.priority ?? "medium");
  const [expanded, setExpandedState] = useState(initialDraft?.expanded ?? startExpanded);
  const setExpanded = useCallback(
    (next: boolean | ((was: boolean) => boolean)) => {
      setExpandedState((was) => {
        const value = typeof next === "function" ? next(was) : next;
        if (value !== was) onExpandedChange?.(value);
        return value;
      });
    },
    [onExpandedChange],
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Lifted out of `SpecDraftControl` so the same checkbox decides both what its own
  // button does and whether a collected finding is fleshed out (task-121). On by
  // default, which is what it has always been.
  const [wantsDraft, setWantsDraft] = useState(initialDraft?.wantsDraft ?? true);

  // Fall back to the first registered project only when the capture happened on a page
  // with no project of its own; never silently override the one being viewed.
  const effectiveDestination = destination || destinations[0]?.id || "";
  const destinationProject = destinations.find((entry) => entry.id === effectiveDestination);
  const reporter = destinationProject?.reporter ?? null;

  // Tell the caller which project to ask about, so the start-an-agent box is gated on
  // the gates of the project actually selected rather than the one the capture started
  // in (task-176). Reported rather than queried here for the reason the props say.
  useEffect(() => {
    if (effectiveDestination) onDestinationChange?.(effectiveDestination);
  }, [effectiveDestination, onDestinationChange]);

  const startGate = startOnFileGate({
    state: dispatchState,
    user: reporter,
    identityDetail:
      "No human actor is configured for this project, so AgentJobs would have nobody to " +
      "attribute a run to.",
    // "Ready for an agent" is this form's lifecycle control, so it is what decides
    // whether starting one in the same gesture makes sense. A draft says a person still
    // has to decide the task is worth doing.
    lifecycle: actionable ? "ready" : "draft",
  });
  // `onStart` absent means the surface does not offer this at all, which is a closed
  // gate rather than a hidden control -- the checkbox still says so.
  const wantsStart = Boolean(onStart) && startGate.allowed && startOnFile;

  const fieldElement = (name: string) => {
    const found = formRef.current?.elements.namedItem(name);
    return found instanceof HTMLInputElement || found instanceof HTMLTextAreaElement
      ? found
      : null;
  };

  /**
   * Put a restored draft's specification back into the inputs (task-512).
   *
   * In an effect rather than as `defaultValue`, because these fields are addressed by
   * name through the DOM everywhere else in this file -- the model's draft writes them,
   * the undo restores them -- and a second mechanism for the same job is a second
   * opinion about what is in the box. Once, at mount: the empty dependency list is the
   * point, since re-running it would overwrite what is being typed.
   */
  useEffect(() => {
    if (!initialDraft) return;
    for (const name of DRAFTED_FIELDS) {
      const element = fieldElement(name);
      const value = initialDraft.fields[name];
      if (element && value !== undefined) element.value = value;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount only; see above.
  }, []);

  /** Everything typed here, in the shape the draft store keeps. */
  const readDraft = (): CaptureDraft => {
    const fields: Record<string, string> = {};
    for (const name of DRAFTED_FIELDS) fields[name] = fieldElement(name)?.value ?? "";
    return {
      title,
      details,
      attachments,
      destination,
      actionable,
      priority,
      expanded,
      wantsDraft,
      fields,
    };
  };

  /**
   * True once this form's content has become a tray item or a task.
   *
   * After that nothing here may write a draft again -- not the effect below, not a
   * stray change event -- or the finding just collected would be offered back as
   * something still to type. The component is remounted for the next capture, so this
   * lives exactly as long as the composition it is about.
   */
  const settled = useRef(false);

  /** Tell the caller what is unsent, or that there is nothing left to keep. */
  const reportDraft = () => {
    if (!onDraftChange || settled.current) return;
    const draft = readDraft();
    onDraftChange(draftHasContent(draft) ? draft : null);
  };

  /** This composition is over: it is on the tray, or on the server. Forget it. */
  const discardDraft = () => {
    if (!onDraftChange || settled.current) return;
    settled.current = true;
    onDraftChange(null);
  };

  // The controlled half. The specification inputs report themselves through the form's
  // own change handler instead, which is what keeps them uncontrolled: mirroring twelve
  // textareas into state to notice a keystroke would re-render the form on every one.
  useEffect(reportDraft, [
    title,
    details,
    attachments,
    destination,
    actionable,
    priority,
    expanded,
    wantsDraft,
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reportDraft is rebuilt each render.
  ]);

  /**
   * What the draft control compares against.
   *
   * `description` is React state because the attachment picker owns that textarea; every
   * other draftable field is read straight off its input. Mixing the two here rather
   * than in the draft module keeps `applySpecDraft` a pure function of strings.
   */
  const readValues = useCallback((): DraftableValues => {
    const values: DraftableValues = { ...EMPTY_VALUES };
    for (const field of DRAFTABLE_FIELDS) {
      values[field] = field === "description" ? details : (fieldElement(field)?.value ?? "");
    }
    return values;
  }, [details]);

  const writeValues = useCallback((values: DraftableValues) => {
    for (const field of DRAFTABLE_FIELDS) {
      if (field === "description") {
        setDetails(values.description);
        continue;
      }
      const element = fieldElement(field);
      if (element) element.value = values[field];
    }
  }, []);

  const readInput = useCallback(() => ({ title, description: details }), [details, title]);

  /**
   * Apply a draft, and open the specification if the draft put anything in it.
   *
   * A generated intent nobody can see is worse than no generated intent: the person has
   * to read what they are about to file. This is the one thing that opens the section
   * without somebody pressing the control.
   */
  const applyDraft = useCallback(
    (draft: SpecDraftResponse, current: DraftableValues) => {
      const outcome = applySpecDraft(current, draft);
      writeValues(outcome.next);
      const touched: Array<DraftableField> = [...outcome.filled, ...outcome.replaced];
      if (touched.some((field) => field !== "description")) setExpanded(true);
      return outcome;
    },
    [writeValues],
  );

  /**
   * The request this form currently describes, assembled once for both paths.
   *
   * Extracted for task-121 so that adding a finding to the tray and filing it now
   * cannot produce different records. Throws the sentence a malformed Context or
   * Dependency line deserves, which both callers show in the same alert box.
   *
   * `requestAttachments` is a parameter rather than read from state because the two
   * paths differ on exactly this: a single capture sends its images with the request,
   * and a collected one leaves them to the tray, which holds one copy of the bytes and
   * joins them back on when the batch is sent.
   */
  const buildRequest = (
    form: FormData,
    destinationProjectId: string,
    filingAs: string,
    operationId: string,
    requestAttachments: Array<AttachmentUpload>,
  ): TaskCreateRequest => {
    const specContext = parseRows(String(form.get("context") ?? ""), "Context", true).map(
      ({ first, reason }) => ({ path: first, why: reason }),
    );
    const dependencies = parseRows(
      String(form.get("dependencies") ?? ""),
      "Dependency",
      false,
    ).map(({ first, reason }) => ({
      task: first,
      type: "needs" as const,
      ...(reason ? { note: reason } : {}),
    }));
    const acceptance = String(form.get("acceptance") ?? "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    const tags = String(form.get("tags") ?? "")
      .split(",")
      .map((tag) => tag.trim())
      .filter(Boolean);

    return buildCaptureRequest({
      draft: { title, details, actionable },
      context,
      destinationProjectId,
      reporter: filingAs,
      operationId,
      attachments: requestAttachments,
      tags,
      priority,
      spec: {
        summary: String(form.get("summary") ?? ""),
        intent: String(form.get("intent") ?? ""),
        constraints: String(form.get("constraints") ?? ""),
        out_of_scope: String(form.get("out_of_scope") ?? ""),
        acceptance,
        context: specContext,
        dependencies,
        id: optional(String(form.get("id") ?? "")),
        parent: optional(String(form.get("parent") ?? "")),
        category: String(form.get("category") ?? "general").trim() || "general",
        effort: optional(String(form.get("effort") ?? "")),
      },
    });
  };

  /**
   * Add what is typed to the tray, without leaving the capture flow.
   *
   * Validated here rather than by the browser, because this is a `type="button"` and
   * native constraint validation only runs on a submit. One readable sentence in the
   * form's own alert box is also what the rest of this form does with a bad value.
   *
   * The caller remounts the form afterwards, which is how every field, every attachment
   * and the expansion state reset and focus returns to Title -- the same mechanism
   * "File another" has used since task-346, rather than a reset function that has to be
   * kept in step with the state.
   */
  const collect = () => {
    const form = formRef.current;
    if (!form || !onCollect || !reporter || !effectiveDestination) return;
    if (!title.trim() || !details.trim()) {
      setError("A finding needs a title and a note before it can go on the list.");
      return;
    }
    setError(null);
    try {
      onCollect({
        projectId: effectiveDestination,
        request: buildRequest(
          new FormData(form),
          effectiveDestination,
          reporter,
          // Minted once, here, and stored with the item: a batch may be sent twice
          // because the first attempt's answer was lost, and the server resolves a
          // repeated operation_id to the task the first attempt made. See `tray.ts`.
          newOperationId(),
          [],
        ),
        attachments,
        route: context.route,
        // What the person actually typed, with no provenance footer on it: that block
        // is AgentJobs talking about itself and is not part of the finding a model is
        // being asked to expand.
        note: details,
        wantsDraft,
      });
      // It is on the tray now, and the tray is itself durable -- so the draft has done
      // its job and must go before the caller remounts this form for the next finding.
      // Second, not first: if the page died between the two, a visible duplicate the
      // person can delete is a far better outcome than prose nobody can get back.
      discardDraft();
    } catch (caught) {
      setError(
        caught instanceof Error && caught.message
          ? caught.message
          : "It could not be added to the list.",
      );
    }
  };

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!reporter || !effectiveDestination) return;
    setError(null);
    const form = new FormData(event.currentTarget);
    try {
      setBusy(true);
      const request = buildRequest(
        form,
        effectiveDestination,
        reporter,
        // A retry after a timeout resolves to the task the first attempt made instead
        // of filing the same thing twice.
        newOperationId(),
        toUploads(attachments),
      );
      // Two requests in order, and two outcomes reported separately: the create, then
      // the ordinary dispatch the task page's button makes. A refused start never
      // undoes the create -- see `fileAndMaybeStart`.
      const outcome = await fileAndMaybeStart({
        wanted: wantsStart,
        create: () => onSubmit(effectiveDestination, request),
        // Unreachable while `wantsStart` is false, which it always is without
        // `onStart` -- written out rather than asserted so a surface that forgets to
        // pass one cannot crash a create that was going to succeed.
        start: (taskId) =>
          onStart
            ? onStart(effectiveDestination, taskId)
            : Promise.reject(new Error("This surface does not start agents.")),
      });
      // The server has it. Same rule as a collect: this composition is over.
      discardDraft();
      onFiled(effectiveDestination, outcome);
    } catch (caught) {
      setError(
        caught instanceof Error && caught.message
          ? caught.message
          : "It could not be filed. Check the server, then try again.",
      );
      setBusy(false);
    }
  };

  return (
    <form
      ref={formRef}
      onSubmit={(event) => void submit(event)}
      // Ctrl+Enter adds to the list, which is the keyboard-first half of task-121: a
      // review pass types a title, a note, Ctrl+Enter, and is in an empty Title box
      // again without a hand having left the keyboard. Plain Enter is left alone, so
      // the form still submits the way every other form in the app does.
      onKeyDown={(event) => {
        if (!onCollect || event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) return;
        event.preventDefault();
        collect();
      }}
      // The uncontrolled half of the draft. A change event from any specification input
      // bubbles up to here, which is how those fields are noticed without mirroring
      // twelve textareas into React state to watch them.
      onChange={reportDraft}
      className="space-y-4"
    >
      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-500/60 bg-red-950/50 p-4 text-red-200"
        >
          {error}
        </div>
      )}

      <div className="relative">
        <label className="block font-medium">
          Title
          <input
            name="title"
            required
            autoFocus={autoFocus}
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            className={inputClass}
            placeholder="Task list filters match nothing"
          />
        </label>
        <DictationControl label="Title" target={() => fieldElement("title")} />
      </div>

      <AttachmentPicker
        label="What happened"
        hint="Enough for someone who was not here. The page you were on is recorded for you."
        placeholder="What went wrong, or what needs doing? Paste a screenshot here with Ctrl+V."
        value={details}
        onChange={setDetails}
        attachments={attachments}
        onAttachmentsChange={setAttachments}
        name="details"
        required
        textareaClassName={`${inputClass} min-h-28`}
        textareaRef={detailsRef}
        under={<DictationControl label="What happened" target={() => detailsRef.current} />}
      />

      <DictationNote />

      <label className="block font-medium">
        File into project
        <span className="mt-1 block text-xs font-normal text-dark-muted">
          Something about AgentJobs itself belongs in the AgentJobs project, not in whatever
          you happened to be reading.
        </span>
        <select
          aria-label="File into project"
          value={effectiveDestination}
          onChange={(event) => setDestination(event.target.value)}
          className={inputClass}
        >
          {destinations.map((entry) => (
            <option value={entry.id} key={entry.id}>
              {entry.name}
            </option>
          ))}
        </select>
      </label>

      {reporter && effectiveDestination && (
        <SpecDraftControl
          projectId={effectiveDestination}
          readValues={readValues}
          readInput={readInput}
          onApply={applyDraft}
          onUndo={writeValues}
          enabled={wantsDraft}
          onEnabledChange={setWantsDraft}
          collecting={Boolean(onCollect)}
        />
      )}

      {/*
        One button, and the whole difference between the two things this form replaced.
        Rendered even when the section is open so the expansion is reversible, and
        `aria-expanded`/`aria-controls` so a screen reader is told there is more rather
        than having to find it.
      */}
      <button
        type="button"
        onClick={() => setExpanded((was) => !was)}
        aria-expanded={expanded}
        aria-controls={specHeadingId}
        className="touch-target rounded-lg border border-dark-border px-4 text-sm font-semibold text-blue-300 hover:bg-dark-border"
      >
        {expanded ? "Hide the full specification" : "Add the full specification"}
      </button>

      {/*
        Hidden rather than unmounted. An input that leaves the document loses its value,
        so collapsing the section after typing in it would silently drop everything in
        it -- and `FormData` would then file a task missing fields the person had
        written. `hidden` keeps every field in the form and out of the tab order.
      */}
      <div id={specHeadingId} hidden={!expanded} className="space-y-4">
        <section className={sectionClass} aria-label="Specification">
          <h3 className="text-lg font-semibold">Specification</h3>
          <div className="relative">
            <label className="block font-medium">
              Summary
              <span className="mt-1 block text-xs font-normal text-dark-muted">
                One or two sentences that orient a reader with no prior context.
              </span>
              <textarea name="summary" className={textareaClass} />
            </label>
            <DictationControl label="Summary" target={() => fieldElement("summary")} />
          </div>
          <div className="relative">
            <label className="block font-medium">
              Intent
              <textarea
                name="intent"
                className={textareaClass}
                placeholder="Why does this task exist?"
              />
            </label>
            <DictationControl label="Intent" target={() => fieldElement("intent")} />
          </div>
          <div className="relative">
            <label className="block font-medium">
              Constraints
              <textarea
                name="constraints"
                className={textareaClass}
                placeholder="Hard requirements and prohibitions"
              />
            </label>
            <DictationControl label="Constraints" target={() => fieldElement("constraints")} />
          </div>
          <div className="relative">
            <label className="block font-medium">
              Out of scope
              <textarea
                name="out_of_scope"
                className={textareaClass}
                placeholder="Explicit non-goals"
              />
            </label>
            <DictationControl label="Out of scope" target={() => fieldElement("out_of_scope")} />
          </div>
          <label className="block font-medium">
            Read-first context
            <span className="mt-1 block text-xs font-normal text-dark-muted">
              One per line: path | why it matters
            </span>
            <textarea
              name="context"
              className={textareaClass}
              placeholder="src/agentjobs/manager.py | Owns the behavior being changed"
            />
          </label>
          <div className="relative">
            <label className="block font-medium">
              Acceptance criteria
              <span className="mt-1 block text-xs font-normal text-dark-muted">One per line.</span>
              <textarea name="acceptance" className={textareaClass} />
            </label>
            <DictationControl label="Acceptance criteria" target={() => fieldElement("acceptance")} />
          </div>
        </section>

        <section className={sectionClass} aria-label="Planning and relationships">
          <h3 className="text-lg font-semibold">Planning and relationships</h3>
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block font-medium">
              Task ID <span className="text-xs font-normal text-dark-muted">(generated if blank)</span>
              <input name="id" className={inputClass} placeholder="task-123-short-name" />
            </label>
            <label className="block font-medium">
              Parent task
              <input
                name="parent"
                list="existing-task-ids"
                className={inputClass}
                placeholder="Optional umbrella task ID"
              />
            </label>
            <datalist id="existing-task-ids">
              {existingTaskIds.map((id) => (
                <option value={id} key={id} />
              ))}
            </datalist>
            <label className="block font-medium">
              Priority
              <select
                name="priority"
                value={priority}
                onChange={(event) => setPriority(event.target.value as Priority)}
                className={inputClass}
              >
                <option value="critical">Critical</option>
                <option value="high">High</option>
                <option value="medium">Medium</option>
                <option value="low">Low</option>
              </select>
            </label>
            <label className="block font-medium">
              Category
              <input name="category" defaultValue="general" className={inputClass} />
            </label>
            <label className="block font-medium">
              Effort
              <input name="effort" className={inputClass} placeholder="Half a day" />
            </label>
            <label className="block font-medium">
              Tags <span className="text-xs font-normal text-dark-muted">(comma-separated)</span>
              {/*
                Prefilled rather than inferred. `reported-issue` is what keeps findings a
                filterable population (task-120), and the alternative -- deciding the tag
                from whether this section was ever opened -- is invisible magic about
                what a record is called. Somebody authoring a task from scratch clears it.
              */}
              <input name="tags" defaultValue={REPORTED_ISSUE_TAG} className={inputClass} />
            </label>
            <label className="block font-medium sm:col-span-2">
              Dependencies{" "}
              <span className="text-xs font-normal text-dark-muted">
                (one per line: task-id | optional note)
              </span>
              <textarea name="dependencies" className={textareaClass} />
            </label>
          </div>
        </section>
      </div>

      {/*
        One control for the starting state, not the create form's pair of radios beside
        this form's checkbox. Off by default: something typed in fifteen seconds usually
        needs a person to decide it is worth doing, and `ready` is the reporter
        asserting it is already executable.
      */}
      <label className="flex items-center gap-3 font-medium">
        <input
          type="checkbox"
          checked={actionable}
          onChange={(event) => setActionable(event.target.checked)}
        />
        <span>
          Ready for an agent
          <span className="block text-xs font-normal text-dark-muted">
            Off means draft: use it while questions or decisions remain. On means the
            specification is complete enough to execute now.
          </span>
        </span>
      </label>

      {/*
        Directly under the lifecycle control, because it is the next question in the
        same sequence -- is this ready, and should it start now -- and because the
        reason it is closed on a draft is the box immediately above it.
      */}
      <StartOnFileCheckbox
        gate={startGate}
        checked={wantsStart}
        onChange={setStartOnFile}
        machineFull={Boolean(dispatchState?.machine_full)}
      />

      <p className="rounded-lg border border-dark-border bg-dark-bg p-3 text-xs text-dark-muted">
        Captured from <code>{context.route}</code>
        {context.taskId && context.projectId === effectiveDestination ? (
          <>
            {" "}
            · links <code>{context.taskId}</code> as related
          </>
        ) : null}
        {reporter ? (
          <>
            {" "}
            · filing as <strong className="text-dark-text">{reporter}</strong>
          </>
        ) : null}
      </p>

      {!reporter && (
        <p
          role="alert"
          className="rounded-lg border border-amber-500/60 bg-amber-950/40 p-4 text-sm text-amber-200"
        >
          No single human actor is configured for this project, so a task filed here could
          not say who filed it. Add one entry with <code>kind: human</code> to{" "}
          <code>actors:</code> in <code>.agentjobs/config.yaml</code> and set{" "}
          <code>default_user:</code> to its id.
        </p>
      )}

      <div className="mobile-action-row flex items-center justify-end gap-3">
        {cancel}
        {onCollect && (
          <button
            type="button"
            onClick={collect}
            // Off while an agent is wanted, rather than quietly dropping the box: a
            // batch files tasks and starts nothing, so a checked "start an agent" and
            // "add to the list" are two different intentions and the button says which
            // one it cannot serve.
            disabled={busy || !reporter || wantsStart}
            title={
              wantsStart
                ? "A batch files tasks without starting agents. Clear the start box, or file this one now."
                : "Add this finding to the list and start another (Ctrl+Enter)"
            }
            className="touch-target rounded-lg border border-blue-500/60 px-4 font-semibold text-blue-200 hover:bg-dark-border disabled:opacity-60"
          >
            Add to the list
          </button>
        )}
        <button
          type="submit"
          disabled={busy || !reporter}
          className="touch-target rounded-lg bg-blue-600 px-5 font-semibold text-white hover:bg-blue-500 disabled:opacity-60"
        >
          {busy ? "Filing…" : "File it"}
        </button>
      </div>
      {onCollect && (
        <p className="text-right text-xs text-dark-muted">
          {wantsStart ? (
            "A batch files tasks without starting agents. Clear the box above to add this to the list."
          ) : (
            <>
              <kbd className="rounded border border-dark-border bg-dark-bg px-1">Ctrl</kbd>+
              <kbd className="rounded border border-dark-border bg-dark-bg px-1">Enter</kbd> adds
              to the list and clears the form for the next finding.
            </>
          )}
        </p>
      )}
    </form>
  );
}
