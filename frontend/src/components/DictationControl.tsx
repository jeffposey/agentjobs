import { useDictation, type DictationTarget } from "../voice/useDictation";
import { modeSentence } from "../voice/speech";

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
  const name = dictation.listening ? `Stop dictating into ${label}` : `Dictate into ${label}`;

  return (
    <div className="mt-2 space-y-2 text-sm font-normal">
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={dictation.toggle}
          aria-pressed={dictation.listening}
          aria-label={name}
          title={mode || undefined}
          className={`touch-target rounded-lg border px-3 font-semibold ${
            dictation.listening
              ? "border-red-400 bg-red-950/50 text-red-200"
              : "border-dark-border bg-dark-bg text-blue-300 hover:border-blue-500 hover:bg-dark-border"
          }`}
        >
          <span aria-hidden="true" className="mr-2">
            🎤
          </span>
          {/* The recording state is carried by this word and by `aria-pressed`, not by
              the border colour alone. */}
          {dictation.listening ? "Stop" : "Dictate"}
        </button>
        {dictation.listening && <span className="text-xs text-dark-muted">{mode}</span>}
      </div>

      {/* Interim text: on screen while it is being spoken, and visibly not yet part of
          the field. Italic and muted rather than in the box, because putting unfinished
          words into the box is what destroys a half-typed sentence and what breaks
          undo. */}
      {/* Present before it is needed, because a live region added at the moment of the
          announcement is not reliably announced -- and out of the layout until then,
          because seven empty lines down the create form is its own defect. */}
      <p
        aria-live="polite"
        className={dictation.listening ? "min-h-5 text-xs italic text-dark-muted" : "sr-only"}
        data-testid="dictation-interim"
      >
        {dictation.listening
          ? dictation.interim
            ? `Hearing: ${dictation.interim}…`
            : "Listening…"
          : ""}
      </p>

      <DictationError message={dictation.error} onDismiss={dictation.dismissError} />
    </div>
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
}: {
  message: string | null;
  onDismiss: () => void;
}) {
  if (!message) return null;
  return (
    <p
      role="alert"
      className="rounded-lg border border-amber-500/60 bg-amber-950/40 p-3 text-xs text-amber-200"
    >
      {message}{" "}
      <button type="button" onClick={onDismiss} className="underline hover:text-amber-100">
        Dismiss
      </button>
    </p>
  );
}

/**
 * The one sentence a form shows when this browser cannot put a microphone on a field.
 *
 * Rendered once per form rather than once per field: the answer is the same for every
 * box on the page, and repeating it under each one would be noise on exactly the
 * browser that is already worse off. It names the keyboard's own microphone key,
 * because that is the path that still works -- confirmed on Android over the tailnet
 * origin in task-171 -- and it is the honest thing to point at rather than leaving a
 * dead button on screen.
 */
export function DictationNote() {
  const dictation = useDictation(() => null);

  if (!dictation.supported) {
    return (
      <p className="rounded-lg border border-dark-border bg-dark-bg p-3 text-xs text-dark-muted">
        This browser has no in-page dictation. Use the microphone key on your phone or tablet
        keyboard — it types into every box here. On a computer, Windows dictates with{" "}
        <kbd className="rounded border border-dark-border bg-dark-surface px-1">Win</kbd>+
        <kbd className="rounded border border-dark-border bg-dark-surface px-1">H</kbd> and macOS
        with a double press of{" "}
        <kbd className="rounded border border-dark-border bg-dark-surface px-1">Fn</kbd>.
      </p>
    );
  }

  // Offered, never forced, and once per form rather than once per field: the pack is a
  // property of the browser, so seven identical offers down one form would be seven
  // ways to answer the same question. Only desktop Chrome reported a pack it could
  // fetch at all -- the phone and Edge reported none, where demanding a local run is
  // permanent failure rather than a first-run download (task-171).
  if (dictation.mode !== "installable" && !dictation.error) return null;
  return (
    <div className="space-y-2 text-sm font-normal">
      {dictation.mode === "installable" && (
        <p className="flex flex-wrap items-center gap-3 rounded-lg border border-dark-border bg-dark-bg p-3 text-xs text-dark-muted">
          <span>{modeSentence(dictation.mode)}</span>
          <button
            type="button"
            onClick={dictation.install}
            disabled={dictation.installing}
            className="touch-target rounded-lg border border-dark-border px-3 font-semibold text-blue-300 hover:border-blue-500 disabled:opacity-60"
          >
            {dictation.installing ? "Downloading…" : "Keep speech on this device"}
          </button>
        </p>
      )}
      <DictationError message={dictation.error} onDismiss={dictation.dismissError} />
    </div>
  );
}
