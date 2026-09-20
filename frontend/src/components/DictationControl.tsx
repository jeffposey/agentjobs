import { useId } from "react";

import { useDictation, type DictationTarget } from "../voice/useDictation";
import { modeBadge, modeSentence, type Microphone } from "../voice/speech";

/**
 * The microphone beside a field. One of these, used everywhere (ac-1).
 *
 * It is a **button next to an ordinary field**, never a replacement for one. The field
 * keeps its own type, its own `name`, its own handlers and its own value; this control
 * only ever writes a finished phrase into it at the caret. That is the binding
 * constraint task-171's decision left behind: the operating system keyboard's
 * microphone is the only dictation path that works in every browser measured, and it
 * stops working the moment something intercepts a key or swaps the textarea for an
 * editor.
 *
 * It takes a **resolver**, not a ref, because the two forms it has to serve are built
 * differently: the reporter's fields are React state and the create form's are
 * uncontrolled and read through `FormData` at submit. A function that finds the
 * element serves both without either form changing shape, and writing through the DOM
 * means a controlled field's `onChange` fires exactly as if the words had been typed.
 *
 * **It costs no vertical space, and that is a requirement rather than a nicety.** The
 * first version was a labelled button on a row of its own, which on the create form is
 * seven extra rows pushing the thing you came to fill in below the fold -- the owner
 * rejected it on sight. So the button is absolutely positioned into the empty right-hand
 * end of the field's own label line: it is in the layout's flow nowhere, it does not
 * cover the field or its resize grip, and it sits beside the name of the box it speaks
 * into. **Its caller supplies the positioned ancestor** -- wrap the field's label block
 * in `relative` -- and that is the whole of what a caller has to do.
 *
 * The 44px hit area is deliberate and free: an absolutely positioned box costs no
 * layout, so the target can meet the touch floor while the visible glyph stays small.
 *
 * **Where the button is absent.** Firefox has neither spelling of the constructor, so
 * there is nothing to render and nothing is rendered -- a disabled microphone is a
 * worse answer than no microphone, and the sentence explaining what to do instead
 * belongs once per form rather than once per field. That is {@link DictationNote}.
 */

type DictationControlProps = {
  /** The field's own label, so the button can say which box it speaks into. */
  label: string;
  target: DictationTarget;
};

export function DictationControl({ label, target }: DictationControlProps) {
  const dictation = useDictation(target);
  if (!dictation.supported) return null;

  const mode = modeSentence(dictation.mode);
  // Where the audio goes, said while it is going there rather than in a box that is
  // on screen when nobody is speaking (task-171: state the mode, never a blanket claim).
  const badge = modeBadge(dictation.mode);
  const name = dictation.listening ? `Stop dictating into ${label}` : `Dictate into ${label}`;

  return (
    <>
      <button
        type="button"
        onClick={dictation.toggle}
        aria-pressed={dictation.listening}
        aria-label={name}
        title={dictation.listening ? name : `${name}. ${mode}`.trim()}
        // Out of the flow entirely: the caller's `relative` wrapper puts this at the
        // right-hand end of the label line, where there is nothing else.
        className={`absolute -top-2 right-0 flex h-11 w-11 items-center justify-center rounded-full ${
          dictation.listening
            ? "text-red-300 hover:bg-red-950/50"
            // The blue every other affordance in this application uses, so a small glyph
            // still reads as something to press rather than as decoration beside a hint.
            : "text-blue-300/80 hover:bg-dark-border hover:text-blue-300"
        }`}
      >
        {/* Shape, not colour: a microphone when idle and a stop square while running,
            so the state survives a monochrome display as well as `aria-pressed` does. */}
        {dictation.listening ? <StopGlyph /> : <MicrophoneGlyph />}
      </button>

      {/* Interim text: on screen while it is being spoken, and visibly not yet part of
          the field. Italic and muted rather than in the box, because putting unfinished
          words into the box is what destroys a half-typed sentence and what breaks
          undo.

          Present before it is needed, because a live region added at the moment of the
          announcement is not reliably announced -- and out of the layout until then,
          because seven empty lines down the create form is the defect above. */}
      <p
        aria-live="polite"
        className={
          dictation.listening ? "mt-1 text-xs italic text-dark-muted" : "sr-only"
        }
        data-testid="dictation-interim"
      >
        {dictation.listening
          ? `${dictation.interim ? `Hearing: ${dictation.interim}…` : "Listening…"}${
              badge ? ` · ${badge}` : ""
            }`
          : ""}
      </p>

      <DictationError
        message={dictation.error}
        onDismiss={dictation.dismissError}
        microphones={dictation.microphones}
        deviceId={dictation.deviceId}
        onChoose={dictation.chooseMicrophone}
      />
    </>
  );
}

/**
 * Which microphone AgentJobs dictates from, offered at the moment it is the answer.
 *
 * **It is inside the failure, not on the form.** A device chooser sitting permanently
 * under every box would be the row the microphone button just stopped being; and until
 * a dictation has failed there is nothing to choose between, because the browser hides
 * device labels until the microphone has been used. So it costs nothing until the day
 * the default device turns out to be the wrong one, and on that day it is already on
 * screen with the complaint.
 *
 * The choice is this origin's alone -- `localStorage`, per browser profile, per site.
 * It changes no operating system setting, so the video call keeps the microphone it
 * had. That is the whole reason this exists rather than a line of advice telling
 * somebody to go and change their default input.
 */
function MicrophoneChoice({
  microphones,
  deviceId,
  onChoose,
}: {
  microphones: Array<Microphone>;
  deviceId: string | null;
  onChoose: (deviceId: string | null) => void;
}) {
  const selectId = useId();
  if (microphones.length < 2) return null;
  return (
    <span className="mt-2 flex flex-wrap items-center gap-2">
      <label htmlFor={selectId} className="text-xs">
        Use this microphone for AgentJobs:
      </label>
      <select
        id={selectId}
        value={deviceId ?? ""}
        onChange={(event) => onChoose(event.target.value || null)}
        className="rounded border border-amber-500/60 bg-dark-bg px-2 py-1 text-xs text-dark-text"
      >
        <option value="">This computer's default</option>
        {microphones.map((microphone) => (
          <option key={microphone.deviceId} value={microphone.deviceId}>
            {microphone.label}
          </option>
        ))}
      </select>
    </span>
  );
}

function MicrophoneGlyph() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      className="h-5 w-5"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
    >
      <rect x="9" y="2.5" width="6" height="11" rx="3" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0" />
      <path d="M12 17.5V21" />
    </svg>
  );
}

function StopGlyph() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="h-5 w-5" fill="currentColor">
      <rect x="6" y="6" width="12" height="12" rx="2" />
    </svg>
  );
}

/**
 * Whatever went wrong, in the form, beside the field it went wrong for.
 *
 * Shared by the two places a dictation can fail, because a failure with nowhere to
 * render is the silent no-op the spec rules out: the control fails while listening,
 * and the note fails while fetching a language pack.
 */
function DictationError({
  message,
  onDismiss,
  microphones = [],
  deviceId = null,
  onChoose,
}: {
  message: string | null;
  onDismiss: () => void;
  microphones?: Array<Microphone>;
  deviceId?: string | null;
  onChoose?: (deviceId: string | null) => void;
}) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="mt-1 rounded-lg border border-amber-500/60 bg-amber-950/40 px-3 py-2 text-xs text-amber-200"
    >
      <span>
        {message}{" "}
        <button type="button" onClick={onDismiss} className="underline hover:text-amber-100">
          Dismiss
        </button>
      </span>
      {onChoose && (
        <MicrophoneChoice microphones={microphones} deviceId={deviceId} onChoose={onChoose} />
      )}
    </div>
  );
}

/**
 * The one line a form shows when the browser needs something said about dictation.
 *
 * Once per form rather than once per field, in both of its states: the answer is a
 * property of the browser, so repeating it under every box would be the same sentence
 * seven times. Kept to a single line of muted text for the same reason the microphone
 * is an icon -- a form is for filling in, not for reading about microphones.
 */
export function DictationNote() {
  const dictation = useDictation(() => null);

  if (!dictation.supported) {
    return (
      <p className="text-xs text-dark-muted">
        No in-page dictation in this browser. Use the microphone key on your phone or tablet
        keyboard — it types into every box here; on a computer,{" "}
        <kbd className="rounded border border-dark-border bg-dark-bg px-1">Win</kbd>+
        <kbd className="rounded border border-dark-border bg-dark-bg px-1">H</kbd> or a double
        press of <kbd className="rounded border border-dark-border bg-dark-bg px-1">Fn</kbd>.
      </p>
    );
  }

  // Offered, never forced. Only desktop Chrome reported a pack it could fetch at all --
  // the phone and Edge reported none, where demanding a local run is permanent failure
  // rather than a first-run download (task-171).
  if (dictation.mode !== "installable" && !dictation.error) return null;
  return (
    <>
      {dictation.mode === "installable" && (
        <p className="text-xs text-dark-muted">
          {modeSentence(dictation.mode)}{" "}
          <button
            type="button"
            onClick={dictation.install}
            disabled={dictation.installing}
            className="underline hover:text-blue-300 disabled:opacity-60"
          >
            {dictation.installing ? "Downloading…" : "Download it"}
          </button>
        </p>
      )}
      <DictationError message={dictation.error} onDismiss={dictation.dismissError} />
    </>
  );
}
