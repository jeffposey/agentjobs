import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { insertDictated } from "./insertText";
import {
  chooseDeviceId,
  chosenDeviceId,
  currentMode,
  errorSentence,
  installPack,
  isTerminalError,
  listMicrophones,
  openChosenTrack,
  readMode,
  readTranscript,
  recognitionCtor,
  watchModes,
  type DictationMode,
  type DictationRecognition,
  type Microphone,
} from "./speech";

/**
 * The recogniser's lifecycle, kept apart from anything that renders.
 *
 * **Text reaches the field one recogniser session at a time, not one result at a
 * time.** That is the whole design, and it is what makes the Android behaviour
 * task-171 measured harmless: there, each result is re-sent with more words and marked
 * final, so anything inserting deltas as they arrive duplicates text. Waiting for the
 * session to end means `event.results` is read once, complete, and written once.
 *
 * The cost is that words appear in the field in chunks rather than syllable by
 * syllable. The interim line pays for it: while a chunk is being spoken it is on
 * screen, visibly not yet committed, so nobody is left wondering whether the
 * microphone is working.
 *
 * **Stitching** is the other half. `continuous` is not honoured on Android — task-171
 * watched recognition end mid-paragraph at 7.4 s — so a session ending while the
 * person still has the button pressed on is not the end of the dictation. We flush,
 * restart, and carry on until they stop or something terminal happens.
 */

/** How many empty sessions in a row before we conclude restarting is pointless. */
const EMPTY_SESSION_LIMIT = 3;

/** A session shorter than this that produced nothing is a failure, not a pause. */
const EMPTY_SESSION_MS = 1500;

export type Dictation = {
  /** Null when this browser has no recogniser at all; the button is then not rendered. */
  supported: boolean;
  listening: boolean;
  /** Words heard but not yet written into the field. Shown, never inserted from here. */
  interim: string;
  error: string | null;
  mode: DictationMode;
  /** True while the language pack is being fetched, so the offer can say so. */
  installing: boolean;
  /**
   * Fetch the language pack, where the browser said one is fetchable. Present on every
   * dictation, and useful on approximately one browser -- desktop Chrome was the only
   * place task-171 found `downloadable` rather than `unavailable`.
   */
  install: () => void;
  toggle: () => void;
  dismissError: () => void;
  /**
   * The microphones this browser will name, and which one AgentJobs is told to use.
   *
   * Empty until a dictation has been attempted: labels are hidden before the
   * microphone permission is granted, and a list of opaque ids is not something
   * anybody can choose from. `null` means the system default, which is what everybody
   * gets until they say otherwise.
   */
  microphones: Array<Microphone>;
  deviceId: string | null;
  chooseMicrophone: (deviceId: string | null) => void;
};

export type DictationTarget = () => HTMLInputElement | HTMLTextAreaElement | null;

/**
 * Start it, passing a track only when there is one.
 *
 * **The argument has to be absent, not `undefined`.** Chrome 153 declares the
 * parameter as a required `MediaStreamTrack` on a second overload, so `start(undefined)`
 * and `start(null)` both throw `TypeError: parameter 1 is not of type
 * 'MediaStreamTrack'` -- measured in the browser, where an ordinary press then failed
 * with "Dictation could not be started". A hand-written fake accepts `undefined`
 * happily, which is exactly why this is verified against the real thing.
 */
function startRecognition(active: DictationRecognition, track: MediaStreamTrack | null): void {
  if (track) active.start(track);
  else active.start();
}

export function useDictation(target: DictationTarget): Dictation {
  const ctor = useMemo(() => recognitionCtor(), []);
  const lang = useMemo(
    () => (typeof navigator === "undefined" ? "en-US" : navigator.language || "en-US"),
    [],
  );

  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [installing, setInstalling] = useState(false);
  // Read from the shared answer rather than held per control: an install taken up in
  // one place has to change the sentence under every field, not just its own.
  const mode: DictationMode = useSyncExternalStore(
    watchModes,
    () => currentMode(lang),
    () => "unknown" as DictationMode,
  );

  const recognition = useRef<DictationRecognition | null>(null);
  // Whether the person still wants to be dictating. A recogniser that ends on its own
  // is restarted while this is true, and is not while it is false.
  const wanted = useRef(false);
  const emptySessions = useRef(0);
  const startedAt = useRef(0);
  const sessionText = useRef("");
  // A complaint we are not going to make yet, and whether this dictation has produced
  // anything at all. See `onerror` for why a recoverable failure waits.
  const pendingError = useRef<string | null>(null);
  const heardAnything = useRef(false);
  // Whether any sound at all reached the recogniser this dictation. A device that is
  // muted or dead opens cleanly and then sends zeros, so `soundstart` never firing is
  // the difference between "your microphone is not working" and "you said nothing" --
  // and it is the only place that difference is observable.
  const sawSound = useRef(false);
  // The track we opened for a chosen device, kept so it can be stopped: an open
  // capture that outlives the dictation is a recording light nobody asked for.
  const openTrack = useRef<MediaStreamTrack | null>(null);
  const [microphones, setMicrophones] = useState<Array<Microphone>>([]);
  const [deviceId, setDeviceId] = useState<string | null>(() => chosenDeviceId());
  const targetRef = useRef(target);
  targetRef.current = target;

  // A capability query about a language pack, not a request for the microphone, so it
  // raises no permission prompt and is safe before any press (ac-7). It is what lets
  // the control say which mode it is in instead of making a claim it cannot support.
  useEffect(() => {
    void readMode(ctor, lang);
  }, [ctor, lang]);

  const flush = useCallback(() => {
    const phrase = sessionText.current;
    sessionText.current = "";
    if (!phrase) return false;
    const element = targetRef.current();
    if (!element) return false;
    return insertDictated(element, phrase);
  }, []);

  const releaseTrack = useCallback(() => {
    openTrack.current?.stop();
    openTrack.current = null;
  }, []);

  const stopEverything = useCallback(() => {
    wanted.current = false;
    setListening(false);
    setInterim("");
    releaseTrack();
    const active = recognition.current;
    recognition.current = null;
    if (active) {
      active.onresult = null;
      active.onerror = null;
      active.onend = null;
      active.onstart = null;
      active.onsoundstart = null;
      try {
        active.stop();
      } catch {
        // Stopping something already stopped is not a failure worth reporting.
      }
    }
  }, [releaseTrack]);

  /**
   * Start recognising, optionally from a track we opened for a chosen device.
   *
   * **Synchronous on purpose.** The microphone permission is asked for inside the
   * handler for the press, and a prompt raised after an `await` is a prompt the
   * browser may no longer consider user-activated. So the default path -- everybody
   * who has not chosen a device -- reaches `start()` in the same tick as the click,
   * exactly as it did before there was a chooser.
   */
  const begin = useCallback((track: MediaStreamTrack | null) => {
    if (!ctor) return;
    openTrack.current = track;
    const active = new ctor();
    active.lang = lang;
    // Asked for, not relied on: honoured on desktop Chrome, ignored on Android. The
    // restart in `onend` is what actually makes dictation continuous.
    active.continuous = true;
    active.interimResults = true;

    active.onstart = () => {
      startedAt.current = Date.now();
    };

    active.onsoundstart = () => {
      sawSound.current = true;
    };

    active.onresult = (event) => {
      const { final, interim: heard } = readTranscript(event.results);
      // Anything at all answers an earlier complaint, so it is never shown afterwards.
      if (final || heard) pendingError.current = null;
      sessionText.current = final;
      setInterim(heard);
    };

    active.onerror = (event) => {
      const terminal = isTerminalError(event.error);
      if (terminal) wanted.current = false;
      const sentence = errorSentence(event.error);
      if (!sentence) return;
      if (terminal) {
        setError(sentence);
        return;
      }
      // **A recoverable complaint is held, not shown.** Desktop Chrome reports
      // `no-speech` after about eight seconds of quiet, and we restart and carry on --
      // so pausing to gather your thoughts before speaking was answered with "Nothing
      // was heard" while the microphone was plainly still listening. The owner pressed
      // the button, waited, read that, and reported that dictation did not work; on
      // the evidence available to him it did not. It is said only once the dictation
      // is really over with nothing to show for it.
      // Where the browser says `no-speech` and no sound ever arrived, we know more
      // than the browser is telling us: the device delivered nothing. Say that
      // instead, because it is the one the person can act on.
      pendingError.current =
        event.error === "no-speech" && !sawSound.current
          ? (errorSentence("agentjobs-no-sound") ?? sentence)
          : sentence;
    };

    active.onend = () => {
      const produced = flush();
      if (produced) {
        heardAnything.current = true;
        pendingError.current = null;
      }
      setInterim("");
      const brief = Date.now() - startedAt.current < EMPTY_SESSION_MS;
      emptySessions.current = produced || !brief ? 0 : emptySessions.current + 1;

      if (!wanted.current) {
        recognition.current = null;
        releaseTrack();
        setListening(false);
        // The held complaint, now that it is news: the dictation is over and nothing
        // ever arrived. A dictation that produced words keeps quiet about the silences
        // in between.
        if (pendingError.current && !heardAnything.current) {
          setError(pendingError.current);
          // Labels are readable now that the microphone has been used, so the choice
          // can be offered beside the complaint rather than as a setting nobody finds.
          void listMicrophones().then(setMicrophones);
        }
        pendingError.current = null;
        return;
      }
      if (emptySessions.current >= EMPTY_SESSION_LIMIT) {
        setError("Dictation kept stopping without hearing anything, so it has been turned off.");
        stopEverything();
        return;
      }
      // Stitch: the person has not pressed stop, so this was the recogniser giving up
      // rather than the dictation ending.
      try {
        startRecognition(active, openTrack.current);
      } catch {
        setError("Dictation could not be restarted. Press the microphone to try again.");
        stopEverything();
      }
    };

    recognition.current = active;
    try {
      startRecognition(active, track);
      setListening(true);
    } catch {
      recognition.current = null;
      wanted.current = false;
      releaseTrack();
      setError("Dictation could not be started. Press the microphone to try again.");
    }
  }, [ctor, flush, lang, releaseTrack, stopEverything]);

  const toggle = useCallback(() => {
    setError(null);
    if (wanted.current || recognition.current) {
      // Requesting a stop rather than aborting: `stop()` lets the recogniser finalise
      // what it already heard, and the flush in `onend` then writes it into the field.
      wanted.current = false;
      const active = recognition.current;
      if (active) {
        try {
          active.stop();
        } catch {
          stopEverything();
        }
      } else {
        setListening(false);
      }
      return;
    }
    // Permission is asked for here and only here -- inside the handler for the press.
    emptySessions.current = 0;
    sessionText.current = "";
    pendingError.current = null;
    heardAnything.current = false;
    sawSound.current = false;
    wanted.current = true;
    if (!chosenDeviceId()) {
      // The ordinary path: no second capture, no await, no change to what the browser
      // sees as the gesture that asked for the microphone.
      begin(null);
      return;
    }
    // Only somebody who has chosen a device pays for opening it. They have already
    // granted the permission -- that is how the labels they chose from were readable.
    setListening(true);
    void openChosenTrack().then((track) => {
      if (!wanted.current) {
        track?.stop();
        setListening(false);
        return;
      }
      begin(track);
    });
  }, [begin, stopEverything]);

  useEffect(() => stopEverything, [stopEverything]);

  const install = useCallback(() => {
    setInstalling(true);
    void installPack(ctor, lang).then((answer) => {
      setInstalling(false);
      if (answer !== "local") {
        setError("The language pack could not be installed, so speech still goes to your browser's speech service.");
      }
    });
  }, [ctor, lang]);

  const chooseMicrophone = useCallback((next: string | null) => {
    chooseDeviceId(next);
    setDeviceId(next);
    setError(null);
    void listMicrophones().then(setMicrophones);
  }, []);

  return {
    supported: ctor !== null,
    listening,
    interim,
    error,
    mode,
    installing,
    install,
    toggle,
    dismissError: () => setError(null),
    microphones,
    deviceId,
    chooseMicrophone,
  };
}
