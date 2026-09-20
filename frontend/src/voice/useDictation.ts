import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { insertDictated } from "./insertText";
import {
  currentMode,
  errorSentence,
  installPack,
  isTerminalError,
  readMode,
  readTranscript,
  recognitionCtor,
  watchModes,
  type DictationMode,
  type DictationRecognition,
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
};

export type DictationTarget = () => HTMLInputElement | HTMLTextAreaElement | null;

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

  const stopEverything = useCallback(() => {
    wanted.current = false;
    setListening(false);
    setInterim("");
    const active = recognition.current;
    recognition.current = null;
    if (active) {
      active.onresult = null;
      active.onerror = null;
      active.onend = null;
      active.onstart = null;
      try {
        active.stop();
      } catch {
        // Stopping something already stopped is not a failure worth reporting.
      }
    }
  }, []);

  const begin = useCallback(() => {
    if (!ctor) return;
    const active = new ctor();
    active.lang = lang;
    // Asked for, not relied on: honoured on desktop Chrome, ignored on Android. The
    // restart in `onend` is what actually makes dictation continuous.
    active.continuous = true;
    active.interimResults = true;

    active.onstart = () => {
      startedAt.current = Date.now();
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
      pendingError.current = sentence;
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
        setListening(false);
        // The held complaint, now that it is news: the dictation is over and nothing
        // ever arrived. A dictation that produced words keeps quiet about the silences
        // in between.
        if (pendingError.current && !heardAnything.current) setError(pendingError.current);
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
        active.start();
      } catch {
        setError("Dictation could not be restarted. Press the microphone to try again.");
        stopEverything();
      }
    };

    recognition.current = active;
    try {
      active.start();
      setListening(true);
    } catch {
      recognition.current = null;
      wanted.current = false;
      setError("Dictation could not be started. Press the microphone to try again.");
    }
  }, [ctor, flush, lang, stopEverything]);

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
    wanted.current = true;
    begin();
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
  };
}
