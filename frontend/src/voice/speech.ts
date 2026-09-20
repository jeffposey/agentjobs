/**
 * The Web Speech API, as much of it as AgentJobs is willing to depend on.
 *
 * Everything here is structural and hand-declared rather than taken from `lib.dom`:
 * the prefixed `webkitSpeechRecognition` spelling is not in any TypeScript lib, and
 * neither is Chrome 139's on-device pair (`available`/`install`). Declaring the shape
 * we use keeps the casts in one file instead of at every call site.
 *
 * The behaviour encoded here is what task-171 measured on real devices, not what the
 * specification says:
 *
 * - **Feature-detect both spellings, and render nothing when neither exists.** Firefox
 *   153 has neither, so a button there would be dead rather than degraded.
 * - **Never force `processLocally`.** On the phone and on Edge the language pack came
 *   back `unavailable`, so forcing it is permanent failure rather than a first-run
 *   download. We ask what mode we are in and say so; we do not demand one.
 * - **Rebuild the transcript from the whole result list.** Android marks each
 *   progressively longer result final and re-sends it, so appending from
 *   `event.resultIndex` produces a corrupted, duplicated string. The probe in task-171
 *   shipped with that bug and scored its first phone run on the damage.
 * - **`continuous` is not honoured.** Recognition ended mid-paragraph at 7.4 s on
 *   Android. Continuous dictation therefore means restarting and stitching, which is
 *   {@link useDictation}'s job rather than this file's.
 */

/** One alternative of one recognised phrase. */
type RecognitionAlternative = { transcript: string };

/** One phrase, which may still be revised while `isFinal` is false. */
type RecognitionResult = {
  readonly length: number;
  readonly isFinal: boolean;
  [index: number]: RecognitionAlternative | undefined;
};

/** The whole list of phrases this recogniser session has produced so far. */
export type RecognitionResultList = {
  readonly length: number;
  [index: number]: RecognitionResult | undefined;
};

export type RecognitionEvent = { results: RecognitionResultList };

export type RecognitionErrorEvent = { error: string; message?: string };

export type DictationRecognition = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives?: number;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((event: RecognitionEvent) => void) | null;
  onerror: ((event: RecognitionErrorEvent) => void) | null;
  onend: (() => void) | null;
  onstart: (() => void) | null;
};

/**
 * Chrome 139's on-device query. `langs` is required; omitting `processLocally` asks
 * whether recognition is possible at all rather than whether it can stay local.
 */
type AvailabilityQuery = { langs: Array<string>; processLocally?: boolean };

export type DictationRecognitionCtor = {
  new (): DictationRecognition;
  available?: (query: AvailabilityQuery) => Promise<string>;
  install?: (query: AvailabilityQuery) => Promise<boolean>;
};

/**
 * Where the audio goes, in the only terms we can honestly offer a person.
 *
 * `local` is claimed **only** when the browser itself reported the language pack is
 * installed. Anything less is `remote` — task-171's decision is that the control says
 * which mode it is in and never makes a blanket privacy claim, because on the one
 * mobile device measured the pack was `unavailable` and the audio did leave.
 */
export type DictationMode = "local" | "installable" | "remote" | "unknown";

/** The constructor under whichever spelling this browser has, or null for neither. */
export function recognitionCtor(view: Window = window): DictationRecognitionCtor | null {
  const scope = view as unknown as {
    SpeechRecognition?: DictationRecognitionCtor;
    webkitSpeechRecognition?: DictationRecognitionCtor;
  };
  return scope.SpeechRecognition ?? scope.webkitSpeechRecognition ?? null;
}

/**
 * Ask whether recognition can run on this device without sending audio anywhere.
 *
 * This is a capability query about a language pack, not a request for the microphone,
 * so it is safe to run before any press -- ac-7 is about the permission prompt, and
 * nothing here can raise one. A browser without the on-device pair answers `remote`,
 * which is the truthful reading: we cannot show it is local, so we do not say it is.
 */
export async function readMode(
  ctor: DictationRecognitionCtor | null,
  lang: string,
): Promise<DictationMode> {
  if (!ctor) return remember(lang, "unknown");
  if (typeof ctor.available !== "function") return remember(lang, "remote");
  // Asked once, because a form carries one of these controls per field and the answer
  // is a property of the browser rather than of the box: the create form would
  // otherwise ask the same question seven times on every mount.
  const cached = modeCache.get(lang);
  if (cached) return cached;
  const asked = askMode(ctor, lang).then((answer) => remember(lang, answer));
  modeCache.set(lang, asked);
  return asked;
}

/**
 * The answer, shared by every control on the page rather than held per field.
 *
 * It has to be shared because installing a language pack changes it for all of them at
 * once: an offer taken up in one place while six fields went on saying the audio leaves
 * would be six wrong sentences, and the wrong ones are the sentences about privacy.
 */
const modeCache = new Map<string, Promise<DictationMode>>();
const modes = new Map<string, DictationMode>();
const watchers = new Set<() => void>();

function remember(lang: string, mode: DictationMode): DictationMode {
  if (modes.get(lang) !== mode) {
    modes.set(lang, mode);
    for (const watcher of watchers) watcher();
  }
  return mode;
}

/** What is known right now, for a component that renders before the query answers. */
export function currentMode(lang: string): DictationMode {
  return modes.get(lang) ?? "unknown";
}

/** Told when any language's answer changes, so every control re-reads at once. */
export function watchModes(watcher: () => void): () => void {
  watchers.add(watcher);
  return () => {
    watchers.delete(watcher);
  };
}

/** Drops the answers. For tests, which change what the browser claims to be. */
export function forgetModes(): void {
  modeCache.clear();
  modes.clear();
  for (const watcher of watchers) watcher();
}

/**
 * Fetch the language pack, so recognition can stop sending audio anywhere.
 *
 * Offered rather than forced, and only where {@link readMode} already answered
 * `installable`. task-171's measurement is the reason for both halves: desktop Chrome
 * reported `downloadable` and `install()` then returned true, while the phone and Edge
 * reported `unavailable`, where forcing a local run is permanent failure rather than a
 * first-run download.
 */
export async function installPack(
  ctor: DictationRecognitionCtor | null,
  lang: string,
): Promise<DictationMode> {
  if (!ctor || typeof ctor.install !== "function") return "remote";
  try {
    await ctor.install({ langs: [lang], processLocally: true });
  } catch {
    // The answer is whatever `available()` says afterwards, not what `install()` threw.
  }
  modeCache.delete(lang);
  modes.delete(lang);
  return readMode(ctor, lang);
}

async function askMode(ctor: DictationRecognitionCtor, lang: string): Promise<DictationMode> {
  if (typeof ctor.available !== "function") return "remote";
  try {
    const answer = await ctor.available({ langs: [lang], processLocally: true });
    if (answer === "available") return "local";
    if (answer === "downloadable" || answer === "downloading") return "installable";
    return "remote";
  } catch {
    // A throwing `available()` is a browser bug, not an answer. task-171 recorded
    // reported failures in exactly this call on macOS and in Brave; treat it the same
    // way as a browser that never had the method.
    return "remote";
  }
}

/** One sentence naming where the audio goes, shown beside the button before it is pressed. */
export function modeSentence(mode: DictationMode): string {
  switch (mode) {
    case "local":
      return "Speech is recognised on this device.";
    case "installable":
      return "Speech goes to your browser's speech service. A one-time download would let it stay on this device.";
    case "remote":
      return "Speech goes to your browser's speech service, not to AgentJobs.";
    case "unknown":
      return "";
  }
}

/**
 * The same fact in three or four words, for the line that is only on screen while the
 * microphone is running.
 *
 * task-171's decision is that the control states its mode rather than making a blanket
 * privacy claim, and the honest moment to state it is while audio is actually being
 * captured. A full sentence there would wrap on a phone, so the short form carries it
 * and {@link modeSentence} stays for the once-per-form note.
 */
export function modeBadge(mode: DictationMode): string {
  switch (mode) {
    case "local":
      return "on this device";
    case "installable":
    case "remote":
      return "via your browser's speech service";
    case "unknown":
      return "";
  }
}

/**
 * The transcript so far, rebuilt from the whole list.
 *
 * Deliberately not incremental. See the note at the top of this file: on Android each
 * result is re-sent with more words and marked final, so anything that remembers where
 * it got to duplicates text.
 */
export function readTranscript(results: RecognitionResultList): {
  final: string;
  interim: string;
} {
  const final: Array<string> = [];
  const interim: Array<string> = [];
  for (let index = 0; index < results.length; index += 1) {
    const result = results[index];
    if (!result) continue;
    const text = result[0]?.transcript ?? "";
    if (!text) continue;
    (result.isFinal ? final : interim).push(text.trim());
  }
  return { final: joinPhrases(final), interim: joinPhrases(interim) };
}

function joinPhrases(phrases: Array<string>): string {
  return phrases.filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
}

/**
 * What to tell the person, per error code. Never a silent no-op: a code with no
 * sentence of its own still gets one.
 *
 * `aborted` returns null on purpose -- it is what the recogniser reports when the
 * person presses stop, and an error message for doing what you asked is noise.
 */
export function errorSentence(code: string): string | null {
  switch (code) {
    case "aborted":
      return null;
    case "not-allowed":
    case "service-not-allowed":
      return "Microphone access was refused. Allow it for this site in your browser's settings — or use the microphone key on your keyboard, which works either way.";
    case "audio-capture":
      return "No microphone was found, so nothing was recorded.";
    case "no-speech":
      return "Nothing was heard. Your text is unchanged.";
    case "network":
      return "The speech service could not be reached, so nothing was transcribed.";
    case "language-not-supported":
      return "This browser has no dictation for this language.";
    case "bad-grammar":
      return "The recogniser rejected the request, so nothing was transcribed.";
    default:
      return `Dictation stopped: ${code}.`;
  }
}

/** Error codes where restarting the recogniser cannot help, so stitching must stop. */
const TERMINAL_ERRORS = new Set([
  "not-allowed",
  "service-not-allowed",
  "audio-capture",
  "language-not-supported",
  "bad-grammar",
]);

export function isTerminalError(code: string): boolean {
  return TERMINAL_ERRORS.has(code);
}
