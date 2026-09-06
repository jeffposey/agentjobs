import { useState } from "react";
import { Link } from "react-router-dom";

import type {
  AnswerSubmission,
  AttachmentUpload,
  LogEntry,
  TaskDetailResponse,
} from "../api/generated";
// `TaskRead` is the app-facing alias for the output shape; `verbsFor` needs the record
// itself, not just the detail envelope around it. See api/types.ts for why it is aliased.
import type { TaskFinishView, TaskRead } from "../api/types";
import { toUploads, type PendingAttachment } from "../report/attachments";
import { AttachmentPicker } from "./AttachmentPicker";
import {
  QuestionForm,
  openQuestions,
  submissions,
  type Draft,
  type OpenQuestion,
} from "./QuestionForm";
import { DependencyGraph } from "./DependencyGraph";
import { DependencyState } from "./DependencyState";
import { DispatchPanel, type DispatchPanelProps } from "./DispatchPanel";
import { FinishPanel } from "./FinishPanel";
import { identityHeadline } from "./identityProblem";
import { linkSegments } from "./linkify";
import { NoteComposer } from "./NoteComposer";
import { ReviewLinks, cardUrls, reviewPromptFor } from "./ReviewLinks";
import { useWideShell } from "./shellLayout";

const PRIORITY_CLASSES: Record<string, string> = {
  critical: "bg-red-900 text-red-200",
  high: "bg-orange-900 text-orange-200",
  medium: "bg-yellow-900 text-yellow-200",
  low: "bg-slate-700 text-slate-200",
};

const LOG_CLASSES: Record<string, string> = {
  handoff: "border-orange-500",
  decision: "border-purple-500",
  question: "border-yellow-500",
  instruction: "border-red-500",
};

function taskPath(projectId: string, taskId: string) {
  return `/p/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}`;
}

/**
 * The width prose is allowed to reach, wherever in this panel it appears (task-239).
 *
 * `ch` rather than `rem`, because the quantity that decides whether a line is readable
 * is characters per line and not pixels: this panel's prose is `text-sm`, so a `rem`
 * cap tuned here would be the wrong measure the moment anything is set at a different
 * size. 78 is the top of the range typography holds to -- long enough that the working
 * spec of a real task is not a column of five-word lines on a phone, short enough that
 * the eye finds the start of the next line on a 2560px monitor. The panel is ~2000px
 * wide there, which is about 190 characters, and 190-character prose is the thing
 * task-239 exists to stop.
 *
 * It is on the text rather than on the sections holding it, deliberately. Half of what
 * this panel shows -- the dependency graph, the log's rows, the relationship cards, the
 * metadata strip -- is *better* for the extra width, and a section-level cap would take
 * it back from them. See {@link MEASURE} for the blocks that do want one.
 */
const PROSE = "max-w-[78ch]";

/**
 * The width a *block* of prose-and-controls is allowed to reach.
 *
 * Wider than {@link PROSE}, because these are cards: a bordered box holding a heading,
 * a paragraph and a row of five buttons reads badly when the box is cut to the measure
 * of the paragraph inside it. Narrower than the region, because a form whose textarea
 * is 2000px wide is no easier to write in than one that is 900.
 *
 * Left-aligned rather than centred, and that is the whole layout decision: every block
 * in this panel starts at the same left edge, and the wide ones simply run further
 * right. A centred column with occasional full-bleed blocks reads as a mistake.
 */
const MEASURE = "max-w-4xl";

/**
 * The threshold this panel restacks at, asked of the panel and not of the window.
 *
 * Every `@min-[768px]:` below was a `min-[820px]:` until task-239, and every one of them
 * was asking "is there room for this on one line" -- a fair question while the panel
 * *was* the page. It stopped being fair the moment the panel became a region: at a
 * 900px window the record has 508px, the media query still reads 900, and the header
 * renders its title at `text-3xl` in a 380px box. Three lines of thirty-pixel type, and
 * a pinned bar built out of them ate a quarter of the viewport. Measured, not guessed.
 *
 * 768 rather than 820 because the number has to be restated in the new frame of
 * reference: at a 820px window the old full-width page gave its content 820 less two
 * 24px gutters, so the content width these rules were tuned for was ~772px. Keeping 820
 * would have moved the breakpoint 50px without anybody deciding to.
 *
 * `@container` is declared on the panel's own root rather than on the region, so a
 * `TaskDetail` rendered anywhere -- the stacked phone shell, a test, a future surface --
 * measures the box it is actually in. The phone is unaffected either way: 358px was
 * below 820 and is below 768.
 */
const PANEL_CONTAINER = "@container";

/**
 * `break-words` is not decoration, and it is the half of the measure that is load
 * bearing (task-239). `whitespace-pre-wrap` wraps at whitespace and nowhere else, so a
 * token with no whitespace in it -- a 400-character path, a base64 blob, a stack frame
 * somebody pasted into a handoff -- runs straight past the panel and takes the region's
 * horizontal scrollbar with it. Measured at a 900px window with every log entry
 * expanded: 2097px of overflow, from one entry. `break-words` rather than `break-all`,
 * because the first breaks a word only when it cannot fit a line by itself and the
 * second breaks every word, which would shred ordinary prose to make one entry behave.
 *
 * The URLs in it are anchors (task-363). Every prompt and every log body this panel
 * shows comes through here, so linkifying once covers the review panel's prompt, the
 * "Current ask" section and the log -- the three places a handoff puts an address a
 * reviewer has to reach. It stays plain text otherwise: {@link linkSegments} returns
 * strings and URLs rather than markup, so nothing on this path renders agent-authored
 * text as HTML or Markdown, and every href it can produce is absolute and therefore
 * safe as a raw anchor (see linkify.ts, property 3, and InAppLinks.test.tsx).
 *
 * `plain` names the addresses that must **not** become anchors here because something
 * else on the screen already offers them. The review panel passes the ones its card
 * holds: an address on screen twice is the redundancy task-363's second pass exists to
 * remove, and two tap targets for one destination is the worst version of it.
 */
function SpecText({
  children,
  muted = false,
  plain,
}: {
  children: string;
  muted?: boolean;
  plain?: Set<string>;
}) {
  return (
    <div className={`whitespace-pre-wrap break-words text-sm leading-6 ${PROSE} ${muted ? "text-dark-muted" : "text-dark-text"}`}>
      {linkSegments(children).map((segment, index) =>
        segment.kind === "link" && !plain?.has(segment.href) ? (
          <a
            key={index}
            href={segment.href}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-300 underline hover:text-blue-200"
          >
            {segment.value}
          </a>
        ) : (
          segment.value
        ),
      )}
    </div>
  );
}

/**
 * What the panel offers a human, given why the ball arrived (task-231, part 2).
 *
 * Until this existed the panel branched on `lifecycle === "draft"` and nothing else, so
 * every non-draft task -- including one waiting on a decision, with no branch and
 * nothing to merge -- was offered "✓ Approve — agent may merge". Jeff, on a task with
 * four numbered questions in its prompt: *"so you are saying, when I click Approve on
 * 077, I am somehow answering all of this?!?!"* No. It would have recorded an approval
 * with no defined meaning.
 *
 * The rule is that a control appears only where its verb is true of the task in front
 * of it, and its label says what it writes:
 *
 *   ball_reason        primary                          send-back
 *   -----------------  -------------------------------  --------------------------
 *   (draft)            ▲ Promote — make it claimable    ✎ Send feedback   -> revise
 *   review             ✓ Approve — agent may merge      ✎ Request Changes -> revise
 *   approval           ✓ Approve — agent may merge      ✎ Request Changes -> revise
 *   decision, input    none                             ✎ Answer Questions -> answer
 *   spec (not draft)   none                             ✎ Send feedback   -> revise
 *
 * `approval` keeps Approve and keeps the merge wording, which is deliberate rather than
 * an oversight: approve writes one fixed merge clearance for every gate there is, and
 * splitting that -- so a design gate can be approved without implying merge -- is
 * task-001's question, not this one's. The label matching what the route writes is the
 * property being preserved here.
 *
 * task-017 adds one row's worth to that table without changing any of it: a task holding
 * an open question is answerable whatever its ball_reason, because the same rule makes
 * the answering verb *true* there. It joins the extras rather than displacing a primary,
 * so a task at `human`/`review` with a stray question open still offers Approve first.
 */
type SendBackReason = "revise" | "answer" | "redirect" | "hold";

type SendBackVerb = {
  reason: SendBackReason;
  text: string;
  fieldLabel: string;
  placeholder: string;
  hint: string;
};

const REDIRECT_VERB: SendBackVerb = {
  reason: "redirect",
  text: "↪ New Instructions",
  fieldLabel: "New instructions",
  placeholder: "Say what to do instead. What has been done already stands.",
  hint: "Recorded as a re-brief, not a rejection: the work so far stands and the direction changes.",
};

const HOLD_VERB: SendBackVerb = {
  reason: "hold",
  text: "⏸ Hold",
  fieldLabel: "Release condition",
  placeholder: "Say what has to be true before this resumes.",
  hint: "Stops the task. No agent can be dispatched at it, automatically or by hand, until you release it here.",
};

type PanelVerbs = {
  label: string;
  heading: string;
  guidance: string;
  primary: { kind: "approve" | "promote"; text: string } | null;
  /**
   * The send-back this phase leads with, or null where the phase has none.
   *
   * Null only where the questions are already on the page (task-017): a button that
   * opens a copy of a form the reader is looking at is not a second verb, it is the
   * same one twice.
   */
  secondary: SendBackVerb | null;
  /** Re-brief and stop. Offered only where work is underway; see verbsFor. */
  extras: Array<SendBackVerb>;
};

function verbsFor(task: TaskRead, hasOpenQuestions = false): PanelVerbs {
  const planning = task.lifecycle === "draft";
  const guidance =
    "These actions update the task record. Nothing here runs git.";
  if (planning) {
    // A draft has nothing running, so there is nothing to re-brief and nothing to stop.
    // Its own send-back is a revision of the spec, which is what `revise` means.
    return {
      label: "Draft actions",
      heading: "Draft — the spec is with you",
      guidance:
        "These actions update the task record. Promoting puts this task in the pool for an agent to claim; nothing here runs git.",
      primary: { kind: "promote", text: "▲ Promote — make it claimable" },
      secondary: {
        reason: "revise",
        text: "✎ Send feedback",
        fieldLabel: "Feedback on the spec",
        placeholder: "Explain what the spec still needs...",
        hint: "Recorded as a revision: the spec comes back to you when it has been changed.",
      },
      extras: [],
    };
  }
  const heading = `${task.display_status} — the ball is with you`;
  const answering: SendBackVerb = {
    reason: "answer",
    text: "✎ Answer Questions",
    fieldLabel: "Your answer",
    placeholder: "Answer what the prompt above asks...",
    hint: "Recorded as an answer, not a revision: nothing done so far is being rejected.",
  };
  const requesting: SendBackVerb = {
    reason: "revise",
    text: "✎ Request Changes",
    fieldLabel: "Feedback or questions",
    placeholder: "Explain what needs to change...",
    hint: "Recorded as a revision: the agent changes the work and comes back to you for another review.",
  };
  const extras = [REDIRECT_VERB, HOLD_VERB];
  if (task.ball_reason === "decision" || task.ball_reason === "input") {
    // Where questions are open the form is already rendered above these buttons, with
    // its own free-text box and its own Submit, so there is nothing left for an
    // "Answer Questions" button to open.
    return {
      label: "Review actions",
      heading,
      guidance,
      primary: null,
      secondary: hasOpenQuestions ? null : answering,
      extras,
    };
  }
  if (task.ball_reason === "spec") {
    return {
      label: "Review actions",
      heading,
      guidance,
      primary: null,
      secondary: {
        ...requesting,
        text: "✎ Send feedback",
        fieldLabel: "Feedback on the spec",
        placeholder: "Explain what the spec still needs...",
      },
      extras,
    };
  }
  return {
    label: "Review actions",
    heading,
    guidance,
    primary: { kind: "approve", text: "✓ Approve — agent may merge" },
    secondary: requesting,
    extras,
  };
}

/** An optional-note form: promote, approve and resume are all complete acts without one. */
function NoteForm({
  id,
  label,
  placeholder,
  submitText,
  value,
  working,
  onChange,
  onSubmit,
  onCancel,
}: {
  id: string;
  label: string;
  placeholder: string;
  submitText: string;
  value: string;
  working: boolean;
  onChange: (next: string) => void;
  onSubmit: (note: string | null) => void;
  onCancel: () => void;
}) {
  return (
    <form
      className="space-y-3"
      onSubmit={(event) => {
        event.preventDefault();
        // null rather than "" for an empty note, so the server writes its own sentence
        // instead of the UI inventing a blank one.
        onSubmit(value.trim() ? value.trim() : null);
      }}
    >
      <label htmlFor={id} className="block text-sm font-semibold">{label}</label>
      <textarea id={id} value={value} onChange={(event) => onChange(event.target.value)} rows={4} className="w-full rounded-lg border border-dark-border bg-dark-bg p-3 text-dark-text focus:border-yellow-500 focus:outline-none" placeholder={placeholder} />
      <div className="mobile-action-row flex gap-3">
        <button type="submit" disabled={working} className="touch-target rounded-lg bg-emerald-600 px-4 font-semibold text-white disabled:opacity-60">{submitText}</button>
        <button type="button" onClick={onCancel} className="touch-target rounded-lg border border-dark-border px-4 font-semibold">Cancel</button>
      </div>
    </form>
  );
}

/**
 * The questions, on the page, without being asked for (task-017).
 *
 * They were behind the "✎ Answer Questions" button for one release, and Jeff's verdict
 * on that was immediate: *"It should not require pressing answer questions button to get
 * the multiple choice question prompt, that should be in default view."* He is right,
 * and the reason is the one this whole task exists for. The agent has already said, in
 * the handoff, that it is waiting on four specific decisions; a button that reveals them
 * asks the reader to request the thing they came to do. On a phone that is a tap, a
 * scroll and a reorientation before the first option is even visible.
 *
 * So: open questions render whenever the panel does, with their own Submit. The panel's
 * other verbs sit below, unchanged.
 *
 * **Except on a held task**, which is why this is rendered by the caller rather than
 * unconditionally. Answering hands the ball back to `agent`/`answer`, which would lift
 * a hold — a control that quietly undoes the control next to it.
 */
function AnswerSection({
  questions,
  working,
  onSendBack,
}: {
  questions: Array<OpenQuestion>;
  working: boolean;
  onSendBack: TaskDetailProps["onSendBack"];
}) {
  const [drafts, setDrafts] = useState<Record<number, Draft>>({});
  const [note, setNote] = useState("");
  const [attachments, setAttachments] = useState<Array<PendingAttachment>>([]);
  const answers = submissions(drafts);
  // Free text is not required here and is required everywhere else: tapping four
  // options and typing nothing is precisely the act this exists to make possible.
  const canSubmit = answers.length > 0 || Boolean(note.trim());
  return (
    <form
      className="space-y-3 rounded-lg border border-yellow-600/40 bg-dark-bg/40 p-3"
      aria-label="Open questions"
      onSubmit={(event) => {
        event.preventDefault();
        if (!canSubmit) return;
        // Cleared only once the write lands. On failure the taps and the prose stay
        // put, because the banner above says what went wrong and throwing a phone
        // user's answers away is not a way to report it.
        void Promise.resolve(onSendBack("answer", note.trim(), toUploads(attachments), answers)).then(
          () => {
            setDrafts({});
            setNote("");
            setAttachments([]);
          },
          () => undefined,
        );
      }}
    >
      <QuestionForm
        questions={questions}
        drafts={drafts}
        disabled={working}
        onChange={(id, next) => setDrafts((current) => ({ ...current, [id]: next }))}
      />
      <AttachmentPicker
        label="Anything else (optional)"
        hint="Recorded as an answer, not a revision: nothing done so far is being rejected."
        placeholder="Anything the answers above do not cover..."
        value={note}
        onChange={setNote}
        attachments={attachments}
        onAttachmentsChange={setAttachments}
        textareaClassName="mt-1 min-h-20 w-full rounded-lg border border-dark-border bg-dark-bg p-3 text-dark-text focus:border-yellow-500 focus:outline-none"
      />
      <div className="mobile-action-row flex gap-3">
        <button
          type="submit"
          disabled={working || !canSubmit}
          className="touch-target rounded-lg bg-emerald-600 px-4 font-semibold text-white hover:bg-emerald-700 disabled:opacity-60"
        >
          ✓ Send answers
        </button>
      </div>
    </form>
  );
}

/**
 * The one box a human acts through, wearing the vocabulary of the phase the task
 * is in.
 *
 * A draft has not been specified yet, so the primary action is "this spec is
 * finished, make it claimable". A task past draft is being reviewed, so the primary
 * action is "approved, go merge" -- but only where that is true of the task, which is
 * what `verbsFor` above decides.
 *
 * The primary button dispatches to a different verb per phase, and that difference
 * is not cosmetic. `approve` hands the ball back and leaves `lifecycle` alone;
 * `promote` moves `lifecycle` draft -> ready and clears the owner. Sending a draft
 * through approve would leave it draft/agent-work, which `get_next_task()` never
 * returns -- claimable by nobody, forever.
 *
 * A task a human has put on hold shows a different box again: the ball is with the
 * agent, so none of the review verbs apply, and the only true verb is releasing it.
 * Without that this panel would return null on a held task and the hold would have no
 * way out of the browser.
 */
function ReviewPanel({
  detail,
  busy,
  error,
  promoteBusy,
  onApprove,
  onSendBack,
  onReject,
  onPromote,
  onResume,
}: TaskDetailProps) {
  const [mode, setMode] = useState<"none" | "promote" | "approve" | "resume" | "send" | "reject">("none");
  const [sendVerb, setSendVerb] = useState<SendBackVerb | null>(null);
  const [feedback, setFeedback] = useState("");
  const [attachments, setAttachments] = useState<Array<PendingAttachment>>([]);
  const held = detail.task.ball === "agent" && detail.task.ball_reason === "hold";
  // Answering hands the ball back, which would lift a hold, so a held task is not
  // offered the questions however many are open on it.
  const questions = held ? [] : openQuestions(detail.task.log ?? []);
  if (detail.task.ball !== "human" && !held) return null;

  const working = Boolean(busy) || Boolean(promoteBusy);
  const prompt = reviewPromptFor(detail.task);
  const verbs = verbsFor(detail.task, questions.length > 0);
  const reset = () => { setMode("none"); setSendVerb(null); setFeedback(""); setAttachments([]); };
  const toggle = (next: "promote" | "approve" | "resume" | "reject") => {
    setSendVerb(null);
    setFeedback("");
    setAttachments([]);
    setMode(mode === next ? "none" : next);
  };
  const toggleSend = (verb: SendBackVerb) => {
    const same = mode === "send" && sendVerb?.reason === verb.reason;
    setFeedback("");
    setAttachments([]);
    setSendVerb(same ? null : verb);
    setMode(same ? "none" : "send");
  };
  const label = held ? "Hold actions" : verbs.label;

  return (
    <section className={`space-y-4 rounded-xl border-2 border-yellow-600/50 bg-yellow-950/30 p-4 @min-[768px]:p-6 ${MEASURE}`} aria-label={label}>
      <h2 className="text-lg font-semibold text-yellow-300">{held ? "On hold — nothing will run until you release it" : verbs.heading}</h2>
      {/* The prompt with its link lines taken out, and them, below it. The prose no
          longer carries the addresses at all -- see ReviewLinks.tsx for the rule and
          the review that produced it. Above the identity line and everything under it,
          so the first act this panel offers is going to look at the thing; outside the
          identity guard on purpose, because a reviewer whose identity is unresolved
          still wants the address, and being unable to act yet is not a reason to hide
          where to look. */}
      {prompt.prose && <SpecText plain={cardUrls(prompt)}>{prompt.prose}</SpecText>}
      <ReviewLinks links={prompt.links} />
      {detail.identity.ok && detail.identity.user ? (
        <>
          <p className="text-sm text-dark-muted">Acting as <strong className="text-dark-text">{detail.identity.user}</strong>. {held ? "Releasing puts the task back to work; nothing here runs git." : verbs.guidance}</p>
          {/* Above the verb row, not below it: the questions are what the agent said it
              was waiting for, so they are the likely act and a phone shows the first
              thing best. The other verbs keep their order underneath. */}
          {questions.length > 0 && (
            <AnswerSection questions={questions} working={working} onSendBack={onSendBack} />
          )}
          <div className="mobile-action-row flex flex-wrap gap-3">
            {held ? (
              <button type="button" disabled={working} onClick={() => toggle("resume")} className="touch-target rounded-lg bg-emerald-600 px-4 font-semibold text-white hover:bg-emerald-700 disabled:opacity-60">▶ Resume — release the hold</button>
            ) : (
              <>
                {verbs.primary && (
                  <button type="button" disabled={working} onClick={() => toggle(verbs.primary?.kind === "promote" ? "promote" : "approve")} className="touch-target rounded-lg bg-emerald-600 px-4 font-semibold text-white hover:bg-emerald-700 disabled:opacity-60">{verbs.primary.text}</button>
                )}
                {verbs.secondary && (
                  <button type="button" disabled={working} onClick={() => toggleSend(verbs.secondary!)} className="touch-target rounded-lg bg-yellow-600 px-4 font-semibold text-white hover:bg-yellow-700 disabled:opacity-60">{verbs.secondary.text}</button>
                )}
                {verbs.extras.map((verb) => (
                  <button key={verb.reason} type="button" disabled={working} onClick={() => toggleSend(verb)} className="touch-target rounded-lg border border-yellow-600/60 px-4 font-semibold text-yellow-200 hover:bg-yellow-900/40 disabled:opacity-60">{verb.text}</button>
                ))}
              </>
            )}
            <button type="button" disabled={working} onClick={() => toggle("reject")} className="touch-target rounded-lg bg-red-600 px-4 font-semibold text-white hover:bg-red-700 disabled:opacity-60">✕ Reject &amp; Archive</button>
          </div>
          {mode === "promote" && (
            <NoteForm
              id="promote-note"
              label="Promotion note (optional)"
              placeholder="Say why the spec is finished. Left empty, AgentJobs writes its own sentence."
              submitText="Promote"
              value={feedback}
              working={working}
              onChange={setFeedback}
              onSubmit={(note) => void onPromote(note)}
              onCancel={reset}
            />
          )}
          {mode === "approve" && (
            <NoteForm
              id="approve-note"
              label="Approval note (optional) — does not block the merge"
              placeholder="Anything to carry into the merge. Left empty, this is exactly a plain approval."
              submitText="Approve"
              value={feedback}
              working={working}
              onChange={setFeedback}
              onSubmit={(note) => void onApprove(note)}
              onCancel={reset}
            />
          )}
          {mode === "resume" && (
            <NoteForm
              id="resume-note"
              label="Note on releasing the hold (optional)"
              placeholder="Anything the agent should know before it picks this back up."
              submitText="Resume"
              value={feedback}
              working={working}
              onChange={setFeedback}
              onSubmit={(note) => void onResume(note)}
              onCancel={reset}
            />
          )}
          {mode === "send" && sendVerb && (
            <form
              className="space-y-3"
              onSubmit={(event) => {
                event.preventDefault();
                const value = feedback.trim();
                if (!value) return;
                // Close the composer once the write lands, and only then. Every other
                // send-back moves the ball off the human and takes this whole panel
                // with it, so nothing had to clean up after itself -- but a hold
                // leaves the panel rendered as the held one, and the composer that
                // imposed the hold sat open underneath it, offering to submit again.
                // Observed in a browser; no test asked the question.
                //
                // On failure the text stays put: the banner above says what went
                // wrong, and throwing the human's prose away is not a way to report it.
                // Answers never ride this form: where any are open they are rendered
                // above it with a Submit of their own (task-017), and where none are
                // this composer is the prose box it always was.
                void Promise.resolve(
                  onSendBack(sendVerb.reason, value, toUploads(attachments), []),
                ).then(reset, () => undefined);
              }}
            >
              <AttachmentPicker
                label={sendVerb.fieldLabel}
                hint={sendVerb.hint}
                placeholder={sendVerb.placeholder}
                value={feedback}
                onChange={setFeedback}
                attachments={attachments}
                onAttachmentsChange={setAttachments}
                required
                textareaClassName="mt-1 min-h-28 w-full rounded-lg border border-dark-border bg-dark-bg p-3 text-dark-text focus:border-yellow-500 focus:outline-none"
              />
              <div className="mobile-action-row flex gap-3">
                <button type="submit" disabled={working || !feedback.trim()} className="touch-target rounded-lg bg-yellow-600 px-4 font-semibold text-white disabled:opacity-60">Submit</button>
                <button type="button" onClick={reset} className="touch-target rounded-lg border border-dark-border px-4 font-semibold">Cancel</button>
              </div>
            </form>
          )}
          {mode === "reject" && (
            <form
              className="space-y-3"
              onSubmit={(event) => {
                event.preventDefault();
                const value = feedback.trim();
                if (!value) return;
                void onReject(value);
              }}
            >
              <label htmlFor="review-feedback" className="block text-sm font-semibold">Reason for rejection</label>
              <textarea id="review-feedback" required value={feedback} onChange={(event) => setFeedback(event.target.value)} rows={4} className="w-full rounded-lg border border-dark-border bg-dark-bg p-3 text-dark-text focus:border-yellow-500 focus:outline-none" placeholder="Explain why this task should stop..." />
              <div className="mobile-action-row flex gap-3">
                <button type="submit" disabled={working || !feedback.trim()} className="touch-target rounded-lg bg-yellow-600 px-4 font-semibold text-white disabled:opacity-60">Submit</button>
                <button type="button" onClick={reset} className="touch-target rounded-lg border border-dark-border px-4 font-semibold">Cancel</button>
              </div>
            </form>
          )}
        </>
      ) : (
        <div className="rounded-lg border border-yellow-600/50 bg-dark-bg p-4 text-sm">
          <strong className="text-yellow-300">{identityHeadline(detail.identity.problem)}</strong>
          <span className="text-dark-muted">{detail.identity.detail}</span>
        </div>
      )}
      {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
    </section>
  );
}

/**
 * The promote refusal, rendered outside the action panel on purpose.
 *
 * The refusal most worth showing is a revision conflict, and it arrives precisely
 * because somebody else promoted the task first -- which moves the ball to
 * agent/available, and the panel above returns null the moment the ball leaves the
 * human. An in-panel message would therefore vanish at the exact moment it was
 * needed, leaving a click that appeared to do nothing. Observed, not predicted.
 */
function PromoteError({ promoteError }: TaskDetailProps) {
  if (!promoteError) return null;
  return (
    <p role="alert" className={`rounded-xl border-2 border-red-600/50 bg-red-950/30 p-4 text-sm text-red-200 ${MEASURE}`}>{promoteError}</p>
  );
}

function Relationships({ detail, projectId }: { detail: TaskDetailResponse; projectId: string }) {
  if (detail.children.length === 0 && detail.needs.length === 0 && detail.blocks.length === 0 && detail.related.length === 0 && !detail.task.parent) return null;
  return (
    <section className="grid gap-4 @min-[768px]:grid-cols-2" aria-label="Task relationships">
      {(detail.task.parent || detail.children.length > 0) && (
        <div className="rounded-lg border border-dark-border bg-dark-surface">
          <h2 className="border-b border-dark-border p-4 font-semibold">Task hierarchy</h2>
          <div className="divide-y divide-dark-border">
            {detail.task.parent && <div className="p-4 text-sm">Parent: <Link className="touch-target inline-flex text-blue-300 hover:underline" to={taskPath(projectId, detail.task.parent)}>{detail.parent_task?.title ?? detail.task.parent} <span className="font-mono text-xs">({detail.task.parent})</span></Link></div>}
            {detail.children.map((child) => <div className="flex flex-col gap-1 p-4 @min-[768px]:flex-row @min-[768px]:items-center @min-[768px]:justify-between" key={child.id}><Link className="touch-target block text-blue-300 hover:underline" to={taskPath(projectId, child.id)}><span className="block font-medium">{child.title}</span><span className="font-mono text-xs text-dark-muted">{child.id}</span></Link><span className="text-xs text-dark-muted">{child.display_status}</span></div>)}
          </div>
        </div>
      )}
      {detail.related.length > 0 && (
        <div className="rounded-lg border border-dark-border bg-dark-surface">
          <h2 className="border-b border-dark-border p-4 font-semibold">Related</h2>
          <ul className="space-y-3 p-4">{detail.related.map((relation, index) => <li className="text-sm" key={`${relation.task_id}-${index}`}>{relation.exists ? <Link className="touch-target font-mono text-blue-300 hover:underline" to={taskPath(projectId, relation.task_id)}>{relation.task_id}</Link> : <span className="font-mono text-red-300">{relation.task_id} (missing)</span>}{relation.note && <p className="text-dark-muted">{relation.note}</p>}</li>)}</ul>
        </div>
      )}
      {(detail.needs.length > 0 || detail.blocks.length > 0) && (
        <div className="rounded-lg border border-dark-border bg-dark-surface">
          <h2 className="border-b border-dark-border p-4 font-semibold">Needs and blocks</h2>
          <div className="grid gap-px bg-dark-border @min-[768px]:grid-cols-2">
            <div className="bg-dark-surface p-4"><h3 className="font-semibold">This task needs</h3>{detail.needs.length === 0 ? <p className="mt-2 text-sm text-dark-muted">Nothing.</p> : <ul className="mt-2 space-y-3">{detail.needs.map((relation) => <li className="text-sm" key={relation.task_id}>{relation.exists ? <Link className="touch-target font-mono text-blue-300 hover:underline" to={taskPath(projectId, relation.task_id)}>{relation.task_id}</Link> : <span className="font-mono text-red-300">{relation.task_id} (missing)</span>}<p className={relation.state === "open" || relation.state === "missing" ? "text-red-300" : "text-emerald-300"}>{relation.reason}</p>{relation.note && <p className="text-dark-muted">{relation.note}</p>}</li>)}</ul>}</div>
            <div className="bg-dark-surface p-4"><h3 className="font-semibold">This task blocks</h3>{detail.blocks.length === 0 ? <p className="mt-2 text-sm text-dark-muted">Nothing.</p> : <ul className="mt-2 space-y-3">{detail.blocks.map((relation, index) => <li className="text-sm" key={`${relation.task_id}-${index}`}>{relation.exists ? <Link className="touch-target font-mono text-blue-300 hover:underline" to={taskPath(projectId, relation.task_id)}>{relation.task_id}</Link> : <span className="font-mono text-red-300">{relation.task_id} (missing)</span>}<p className="text-dark-muted">{relation.reason}</p>{relation.note && <p className="text-dark-muted">{relation.note}</p>}</li>)}</ul>}</div>
          </div>
        </div>
      )}
    </section>
  );
}

function EntryAttachments({
  entry,
  projectId,
  taskId,
}: {
  entry: LogEntry;
  projectId: string;
  taskId: string;
}) {
  const attachments = entry.attachments ?? [];
  if (attachments.length === 0) return null;
  return (
    <ul aria-label={`Images on entry ${entry.id}`} className="mt-3 flex flex-wrap gap-3">
      {attachments.map((attachment) => {
        // The stored path is relative to the tasks directory; the route addresses the
        // file by task and basename, so the server never takes a path from a client.
        const filename = attachment.path.split("/").pop() ?? "";
        const href = `/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}/attachments/${encodeURIComponent(filename)}`;
        return (
          <li key={attachment.sha256}>
            {/* Shown, not linked. A screenshot nobody clicks is a screenshot nobody
                sees -- the anchor is only so the full-size image is reachable. */}
            <a href={href} target="_blank" rel="noopener noreferrer">
              <img
                src={href}
                alt={attachment.label}
                className="max-h-64 rounded-lg border border-dark-border"
              />
            </a>
          </li>
        );
      })}
    </ul>
  );
}

function Log({ entries, projectId, taskId }: { entries: Array<LogEntry>; projectId: string; taskId: string }) {
  // Per-entry disclosures keep a hundred-entry log scannable, but they also hide the
  // text from Ctrl+F and from select-all-and-copy, which is exactly what a reviewer
  // auditing a long task needs. One control opens every one of them; the `key` carries
  // the flag so React remounts each <details> and the new state wins even over an
  // entry the reader had toggled by hand.
  const [expandAll, setExpandAll] = useState(false);
  const answered = new Set(entries.filter((entry) => entry.type === "answer" && entry.re).map((entry) => entry.re));
  const ordered = [...entries].sort((left, right) => right.id - left.id);
  if (entries.length === 0) return null;
  return (
    <section className="rounded-lg border border-dark-border bg-dark-surface" aria-label="Task log">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-dark-border p-4">
        <h2 className="text-lg font-semibold">Log</h2>
        <button
          type="button"
          aria-pressed={expandAll}
          onClick={() => setExpandAll((current) => !current)}
          className="touch-target rounded-lg border border-dark-border bg-dark-bg px-3 text-xs text-blue-300 hover:border-blue-500"
        >
          {expandAll ? "Collapse long entries" : "Expand all entries"}
        </button>
      </div>
      <div className="space-y-4 p-4 @min-[768px]:p-6">
        {ordered.map((entry, index) => {
          const openQuestion = entry.type === "question" && !answered.has(entry.id);
          return (
            <article className={`border-l-2 pl-4 ${LOG_CLASSES[entry.type] ?? "border-blue-500"}`} key={entry.id} data-log-id={entry.id}>
              <div className="flex flex-wrap items-center gap-2 text-xs uppercase text-dark-muted"><span>#{entry.id}</span><span>•</span><span>{entry.actor}</span><span>•</span><time dateTime={entry.ts}>{new Date(entry.ts).toLocaleString()}</time><span className="rounded border border-dark-border bg-dark-bg px-2 py-0.5 lowercase">{openQuestion ? "open question" : entry.type}</span>{entry.re && <span>re #{entry.re}</span>}</div>
              {entry.body && <details key={String(expandAll)} open={expandAll || index === 0 || entry.body.length <= 400} className="mt-2"><summary className="touch-target cursor-pointer text-xs text-blue-300">{entry.body.length > 400 ? "Entry details" : "Entry"}</summary><SpecText muted>{entry.body}</SpecText></details>}
              <EntryAttachments entry={entry} projectId={projectId} taskId={taskId} />
            </article>
          );
        })}
      </div>
    </section>
  );
}

export type TaskDetailProps = {
  detail: TaskDetailResponse;
  projectId: string;
  busy?: boolean;
  error?: string | null;
  // Promote keeps its own error state rather than sharing `error`: the generic
  // "could not be recorded, reload and try again" hides the one explanation that
  // actually tells a human what happened and what to do about it.
  promoteBusy?: boolean;
  promoteError?: string | null;
  // A note, because approving and saying something about it are one act, not two
  // (task-228). null means no note, and the record is then byte-identical to the
  // one approve wrote before the parameter existed.
  onApprove: (note: string | null) => Promise<void> | void;
  // One prop rather than four near-identical ones. Every send-back is the same act --
  // a note, and the ball moving to the agent -- and the reason is what differs, so the
  // component's contract mirrors the vocabulary instead of paraphrasing it.
  // `answers` is populated only for `answer`, and is the one part of a send-back that is
  // not prose (task-017). It stays on this prop rather than becoming a fifth one because
  // it is still the same act -- the ball moving, with what the human said attached; the
  // difference is that some of what they said was a tap.
  onSendBack: (
    reason: SendBackReason,
    feedback: string,
    attachments: Array<AttachmentUpload>,
    answers: Array<AnswerSubmission>,
  ) => Promise<void> | void;
  onReject: (reason: string) => Promise<void> | void;
  onPromote: (note: string | null) => Promise<void> | void;
  onResume: (note: string | null) => Promise<void> | void;
  // Writing a note is its own act with its own failure, so it keeps its own busy and
  // error rather than sharing the review panel's: a note that could not be saved must
  // still say so on a task whose review actions are not even rendered.
  noteBusy?: boolean;
  noteError?: string | null;
  onAddNote: (body: string) => Promise<void> | void;
  // Dispatch arrives as its own bundle rather than as loose props, so nothing about
  // starting an agent can be mistaken for part of the review panel's contract.
  // Absent, the page renders exactly as it did before dispatch existed.
  //
  // Identity and sufficiency are supplied here rather than in the bundle because both
  // are read straight off the loaded record, which the bundle's hook never sees.
  dispatch?: Omit<DispatchPanelProps, "taskIsDispatchable" | "identity" | "recordCanBrief">;
  // What is happening to this task's branch right now (task-321). Passed in as data
  // rather than fetched by the panel, for the same reason the dispatch bundle is: this
  // component is given everything it renders, so it can be rendered in a test without a
  // query client answering for a surface the test is not about.
  //
  // Null is the ordinary value and means no finish has ever run for this task, which is
  // true of almost every one. The panel renders nothing for it.
  finish?: TaskFinishView | null;
};

export function TaskDetail(props: TaskDetailProps) {
  const { detail, projectId } = props;
  const { task } = detail;
  const pinned = useWideShell();
  const metadata = [
    { label: "Created", value: task.created, date: true },
    { label: "Updated", value: task.updated, date: true },
    { label: "Owner", value: task.assignment?.owner ?? "Unclaimed", date: false },
    { label: "Effort", value: task.effort ?? "Not estimated", date: false },
  ];
  return (
    <div className={`space-y-6 ${PANEL_CONTAINER}`}>
      {/* Pinned to the top of the region that scrolls it, and only where such a region
          exists (task-239). Which task you are reading is the thing you look back up
          for while working down a hundred-entry log, and in the two-region shell the
          record scrolls inside its own box -- so `sticky top-0` here sticks to the top
          of the record, which is exactly the offer.

          `useWideShell` rather than a `min-[600px]:sticky` class, because the shell's
          threshold is a device class and not a width: it tests height too, so a class
          would pin this header on a short desktop window that is rendering the stacked
          shell, where the document scrolls under a nav bar that is *already* pinned and
          two pinned bars would fight for `top: 0`. One source of truth for "which shell
          is this", and it is the module that owns the question.

          The id, title and status ride up here together. They are what the spec asked
          for, and the chips beside them cost nothing extra because they are already on
          this line.

          A pinned bar is a row at every width it can appear at, which is the one thing
          here that is not simply the old header with `sticky` added. Unpinned it keeps
          the stacked-then-row rule exactly as it was, because that rule is what the
          phone renders and the phone is not supposed to change. Pinned, stacking would
          put "← Back to Tasks" on a line of its own and make the bar taller in the
          narrow case -- which is the case where its height is a problem. */}
      <header
        className={`flex gap-4 ${
          pinned
            ? "sticky top-0 z-10 flex-row items-start justify-between border-b border-dark-border bg-dark-bg pb-3"
            : "flex-col @min-[768px]:flex-row @min-[768px]:items-start @min-[768px]:justify-between"
        }`}
        data-pinned={pinned ? "yes" : "no"}
      >
        <div className="min-w-0"><div className="select-all font-mono text-sm text-blue-300">{task.id}</div><h1 className="break-words text-2xl font-bold @min-[768px]:text-3xl">{task.title}</h1><div className="mt-3 flex flex-wrap items-center gap-2"><span className="rounded bg-dark-surface px-2 py-1 text-xs">{task.display_status}</span><span className={`rounded px-2 py-1 text-xs ${PRIORITY_CLASSES[task.priority ?? "medium"]}`}>{task.priority ?? "medium"}</span><span className="text-sm text-dark-muted">{task.category}</span>{task.tags?.map((tag) => <span className="rounded border border-dark-border bg-dark-bg px-2 py-0.5 text-xs" key={tag}>{tag}</span>)}</div></div>
        <Link to={`/p/${encodeURIComponent(projectId)}/tasks`} className="touch-target shrink-0 rounded-lg border border-dark-border bg-dark-surface px-4 text-sm hover:bg-dark-border">← Back to Tasks</Link>
      </header>

      <section className="grid grid-cols-2 overflow-hidden rounded-lg border border-dark-border bg-dark-surface @min-[768px]:grid-cols-4" aria-label="Task metadata">
        {metadata.map(({ label, value, date }) => <div className="border-b border-r border-dark-border p-3 @min-[768px]:border-b-0" key={label}><div className="text-xs text-dark-muted">{label}</div><div className="mt-1 break-words text-sm">{date ? new Date(value).toLocaleString() : value}</div></div>)}
      </section>

      <PromoteError {...props} />
      <ReviewPanel {...props} />
      {/* Between the review verbs and Dispatch, because that is where the eye already
          is: a finish is what pressing Approve two feet above this starts, and the
          answer to "did that do anything" has to be where the question was asked. */}
      {props.finish && (
        <div className={MEASURE}>
          <FinishPanel finish={props.finish} />
        </div>
      )}
      {/* Not capped. The dispatch panel holds a run's transcript and its raw output,
          which are the widest things this page ever shows; they scroll inside their own
          boxes either way, and a wider box means less scrolling. */}
      {props.dispatch && (
        <DispatchPanel
          {...props.dispatch}
          taskIsDispatchable={
            // `agent/hold` is the one agent-ball state an agent may not be started
            // on, and the server refuses it (`task_on_hold`). Offering the button
            // anyway would put the way around a hold next to the control that
            // imposed it.
            task.ball === "agent" && task.ball_reason !== "hold" && task.lifecycle !== "closed"
          }
          identity={detail.identity}
          finishLive={Boolean(props.finish?.live)}
          // The same field the server checks, so the page and the guard agree without a
          // second round trip. `spec.description` is the working specification; an empty
          // one is the only state that means there is nothing here to work from. Notably
          // *not* `ball_prompt`, which is empty on every ready task by design.
          recordCanBrief={Boolean(task.spec.description?.trim())}
        />
      )}
      {/* Directly under the dispatch panel on purpose. Dispatching no longer needs a
          note written first — the button writes its own authorising entry — but a
          refusal that can still land there (no signed-in user, or a CLI-shaped task
          somebody is unpicking) names this control, and a page that names a control it
          does not show is the defect task-185 closed. */}
      <div className={MEASURE}>
        <NoteComposer
          identity={detail.identity}
          busy={props.noteBusy}
          error={props.noteError}
          onAddNote={props.onAddNote}
        />
      </div>
      {task.ball !== "human" && task.ball_prompt &&<section className={`rounded-xl border border-dark-border bg-dark-surface p-4 ${MEASURE}`}><h2 className="mb-2 text-xs font-semibold uppercase text-dark-muted">Current ask ({task.ball}/{task.ball_reason})</h2><SpecText>{task.ball_prompt}</SpecText></section>}
      <section className={`rounded-lg border border-dark-border bg-dark-surface p-4 ${MEASURE}`} aria-label="Dependency state"><h2 className="mb-2 text-sm font-semibold">Work state</h2><DependencyState task={task} /></section>
      <Relationships detail={detail} projectId={projectId} />
      <DependencyGraph children={detail.children} edges={detail.child_dependency_edges} projectId={projectId} umbrellaTitle={task.title} />

      {/* The specification is the one block that is prose end to end, so it takes the
          measure at the block as well as on every SpecText inside it. */}
      <section className={`space-y-6 rounded-lg border border-dark-border bg-dark-surface p-4 @min-[768px]:p-6 ${MEASURE}`} aria-label="Full specification">
        <div><h2 className="mb-2 text-lg font-semibold">Summary</h2><SpecText>{task.spec.summary}</SpecText></div>
        {task.spec.intent && <div><h2 className="mb-2 text-lg font-semibold">Why this task exists</h2><SpecText muted>{task.spec.intent}</SpecText></div>}
        <div><h2 className="mb-2 text-lg font-semibold">Working spec</h2><SpecText>{task.spec.description}</SpecText></div>
        {task.spec.constraints && <div><h2 className="mb-2 text-lg font-semibold">Constraints</h2><SpecText>{task.spec.constraints}</SpecText></div>}
        {task.spec.out_of_scope && <div><h2 className="mb-2 text-lg font-semibold">Out of scope</h2><SpecText muted>{task.spec.out_of_scope}</SpecText></div>}
        {(task.spec.context?.length ?? 0) > 0 && <div><h2 className="mb-2 text-lg font-semibold">Read this first</h2><ul className="space-y-2">{task.spec.context?.map((pointer) => <li className="text-sm" key={pointer.path}><code className="break-all text-blue-300">{pointer.path}</code><span className="text-dark-muted"> — {pointer.why}</span></li>)}</ul></div>}
        {(task.acceptance?.length ?? 0) > 0 && <div><h2 className="mb-2 text-lg font-semibold">Acceptance</h2><ul className="space-y-2">{task.acceptance?.map((criterion) => <li className="rounded-lg border border-dark-border bg-dark-bg p-3 text-sm" key={criterion.id}><span className="mr-2 uppercase text-dark-muted">{criterion.status ?? "pending"}</span>{criterion.text}{criterion.verify && <code className="mt-1 block text-xs text-dark-muted">{criterion.verify}</code>}</li>)}</ul></div>}
      </section>

      {(task.deliverables?.length ?? 0) > 0 && <section className="rounded-lg border border-dark-border bg-dark-surface" aria-label="Deliverables"><h2 className="border-b border-dark-border p-4 text-lg font-semibold">Deliverables</h2><div className="divide-y divide-dark-border">{task.deliverables?.map((item) => <div className="p-4" key={item.path}><code className="break-all text-sm">{item.path}</code><span className="ml-2 text-xs uppercase text-dark-muted">{item.status ?? "pending"}</span>{item.note && <p className="mt-1 text-sm text-dark-muted">{item.note}</p>}</div>)}</div></section>}
      <Log entries={task.log ?? []} projectId={projectId} taskId={task.id} />
      {(task.links?.length ?? 0) > 0 && <section className="rounded-lg border border-dark-border bg-dark-surface" aria-label="External links"><h2 className="border-b border-dark-border p-4 text-lg font-semibold">Links</h2><ul>{task.links?.map((link) => <li className="p-4" key={link.url}><a className="touch-target break-all text-blue-300 underline" href={link.url} target="_blank" rel="noopener noreferrer">{link.title ?? link.url}</a><span className="ml-2 text-xs uppercase text-dark-muted">{link.rel ?? "other"}</span></li>)}</ul></section>}
    </div>
  );
}
