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
  /**
   * Chrome 153 accepts a `MediaStreamTrack` here and recognises from that device
   * instead of the system default. Measured, not assumed: passing a webcam
   * microphone's track on a machine whose default input delivers digital silence
   * produced `soundstart`, `speechstart` and a real transcript.
   *
   * A browser without the overload ignores the argument and uses the default, which
   * is exactly the behaviour of passing nothing -- so this degrades to today.
   */
  start: (track?: MediaStreamTrack) => void;
  stop: () => void;
  abort: () => void;
  onresult: ((event: RecognitionEvent) => void) | null;
  onerror: ((event: RecognitionErrorEvent) => void) | null;
  onend: (() => void) | null;
  onstart: (() => void) | null;
  /** Fires when *any* sound arrives, which is how a dead device is told from a quiet room. */
  onsoundstart: (() => void) | null;
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
 * **Which microphone AgentJobs dictates from, remembered for this origin only.**
 *
 * The Web Speech API has no device selector, so for years the answer was "whatever
 * the operating system's default input is" -- and that default is shared with every
 * other application on the machine. Changing it to fix dictation changes it for the
 * video call too, which is not a trade anybody should be asked to make.
 *
 * Chrome 153 takes a `MediaStreamTrack` in `start()`, so the choice can be ours
 * instead: open the device we were told to use and hand the recogniser its track.
 * The preference lives in this origin's `localStorage`, so it is per browser profile
 * and per site, and touches no operating system setting at all.
 *
 * Storage is wrapped because it throws in a private window and in a page with site
 * data blocked, where the right answer is simply "no preference".
 */
const DEVICE_KEY = "agentjobs.dictation.deviceId";

export function chosenDeviceId(): string | null {
  try {
    return window.localStorage.getItem(DEVICE_KEY);
  } catch {
    return null;
  }
}

export function chooseDeviceId(deviceId: string | null): void {
  try {
    if (deviceId) window.localStorage.setItem(DEVICE_KEY, deviceId);
    else window.localStorage.removeItem(DEVICE_KEY);
  } catch {
    // A browser that will not remember it still dictates; it just forgets the choice.
  }
}

export type Microphone = { deviceId: string; label: string };

/**
 * The microphones this browser will name.
 *
 * Labels are empty until the microphone permission has been granted, which is why
 * this is only ever called after a dictation has already asked for it -- an
 * unlabelled list of opaque ids is not a thing anybody can choose from. The synthetic
 * `default` and `communications` entries are dropped: they are aliases for whatever
 * the system points at, which is the thing being worked around.
 */
export async function listMicrophones(): Promise<Array<Microphone>> {
  if (!navigator.mediaDevices?.enumerateDevices) return [];
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    return devices
      .filter(
        (device) =>
          device.kind === "audioinput" &&
          device.deviceId &&
          device.deviceId !== "default" &&
          device.deviceId !== "communications" &&
          device.label,
      )
      .map((device) => ({ deviceId: device.deviceId, label: device.label }));
  } catch {
    return [];
  }
}

/**
 * The track for the chosen microphone, or null to let the recogniser use the default.
 *
 * Null is the ordinary path and costs nothing: no second capture, no extra permission
 * surface, no battery. A stream is opened only for somebody who has said that the
 * default is not the one they want.
 */
export async function openChosenTrack(): Promise<MediaStreamTrack | null> {
  const deviceId = chosenDeviceId();
  if (!deviceId || !navigator.mediaDevices?.getUserMedia) return null;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { deviceId: { exact: deviceId } },
    });
    return stream.getAudioTracks()[0] ?? null;
  } catch {
    // The device was unplugged, or is in use elsewhere. Fall back to the default
    // rather than refusing to dictate at all.
    return null;
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
    case "agentjobs-no-sound":
      // Not a browser code: ours, for the state the browser has no code for. The
      // stream opened and `soundstart` never fired, so the device delivered silence
      // rather than the person staying quiet -- a muted headset, or a dead endpoint.
      return "That microphone sent no sound at all. Pick a different one below, or check it is not muted.";
    case "no-speech":
      // Two causes, and the sentence used to name only the flattering one. On the
      // machine this was built on the default input device delivers exact digital
      // silence -- measured at peak 0.000000 over four seconds, while a second
      // microphone on the same machine read 0.107 -- so the owner spoke, was told
      // "nothing was heard", and reasonably concluded the feature was broken. Whichever
      // cause it is, the microphone is the thing to check.
      return "No speech reached the browser. Your text is unchanged — if you did speak, check which microphone your system is set to use, and that it is not muted.";
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
