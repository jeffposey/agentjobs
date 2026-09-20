/**
 * Putting dictated words into an ordinary `<input>` or `<textarea>`.
 *
 * The one rule this file exists to keep: **the field is the field.** Nothing here
 * replaces it with a custom editor, intercepts a key, or cancels `beforeinput` — all
 * three are how the operating system keyboard's own microphone stops working, and that
 * microphone is the path task-171 found works in every browser measured, including the
 * one where the in-page button cannot exist at all.
 *
 * Two write paths, tried in that order:
 *
 * 1.  `document.execCommand("insertText")`. Deprecated, universally implemented, and
 *     the only one that puts the insertion on the browser's own undo stack — so
 *     Ctrl+Z takes back a sentence you did not mean to say. It also emits a real
 *     `beforeinput`/`input` pair, which is what React's `onChange` is listening for.
 * 2.  Setting `.value` through the prototype descriptor and dispatching `input`. The
 *     fallback for anywhere `execCommand` is missing, and the path jsdom takes, so it
 *     is the one under test. The descriptor rather than `element.value = …` because
 *     React keeps a value tracker on the node: assigning normally updates the tracker
 *     too, React concludes nothing changed, and a controlled field silently snaps back
 *     to its old state on the next render.
 *
 * Both paths land in the same place for an uncontrolled form: `FormData` reads the
 * element's value at submit, and that is what changed.
 */

/** Where the caret is, defaulting to the end for an element that reports nothing. */
function selection(element: HTMLInputElement | HTMLTextAreaElement): [number, number] {
  const length = element.value.length;
  const start = element.selectionStart ?? length;
  const end = element.selectionEnd ?? start;
  return [Math.min(start, end), Math.max(start, end)];
}

/**
 * The spacing a person would have typed.
 *
 * A recognised phrase arrives with no leading space, so dictating twice into an empty
 * box would otherwise produce `helloworld`. Only a space is added, never punctuation:
 * cleaning a transcript up is task-175's job, and guessing at sentence boundaries here
 * would fight it.
 */
export function spaced(before: string, phrase: string, after: string): string {
  const lead = before.length > 0 && !/\s$/.test(before) ? " " : "";
  const trail = after.length > 0 && !/^\s/.test(after) ? " " : "";
  return `${lead}${phrase}${trail}`;
}

/** What {@link insertDictated} would produce, as plain strings. Exported for testing. */
export function withPhrase(
  value: string,
  phrase: string,
  start: number,
  end: number,
): { value: string; caret: number } {
  const before = value.slice(0, start);
  const after = value.slice(end);
  const insert = spaced(before, phrase, after);
  return { value: `${before}${insert}${after}`, caret: start + insert.length };
}

function nativeValueSetter(
  element: HTMLInputElement | HTMLTextAreaElement,
): ((value: string) => void) | null {
  const prototype =
    element instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
  const setter = descriptor?.set;
  return setter ? (value: string) => setter.call(element, value) : null;
}

/**
 * Insert `phrase` at the caret, leaving everything already in the field alone.
 *
 * Returns false and touches nothing when the phrase is empty, which is what the spec
 * asks for: hearing nothing must not disturb what someone typed.
 */
export function insertDictated(
  element: HTMLInputElement | HTMLTextAreaElement,
  phrase: string,
): boolean {
  const trimmed = phrase.trim();
  if (!trimmed) return false;

  const [start, end] = selection(element);
  const { value, caret } = withPhrase(element.value, trimmed, start, end);
  const insert = value.slice(start, caret);

  // Focus first: `execCommand` acts on the document's selection, and the caret has to
  // be in this field for it to act on the right one. Focusing is also what a person
  // would expect after speaking into a box -- they can carry on typing.
  element.focus();
  element.setSelectionRange(start, end);

  const exec = (document as Document & { execCommand?: (c: string, s: boolean, v: string) => boolean })
    .execCommand;
  if (typeof exec === "function") {
    try {
      if (exec.call(document, "insertText", false, insert)) return true;
    } catch {
      // Fall through. A browser that refuses `insertText` is not a reason to lose the
      // sentence someone just said.
    }
  }

  const setValue = nativeValueSetter(element);
  if (!setValue) return false;
  setValue(value);
  element.setSelectionRange(caret, caret);
  element.dispatchEvent(new Event("input", { bubbles: true }));
  return true;
}
