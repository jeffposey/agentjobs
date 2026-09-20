import { afterEach, describe, expect, it } from "vitest";

import {
  errorSentence,
  forgetModes,
  isTerminalError,
  modeSentence,
  readMode,
  readTranscript,
  recognitionCtor,
  type DictationRecognitionCtor,
  type RecognitionResultList,
} from "./speech";

/** A result list shaped the way a recogniser hands one over. */
function results(entries: Array<{ text: string; final: boolean }>): RecognitionResultList {
  const list = entries.map(({ text, final }) => ({
    length: 1,
    isFinal: final,
    0: { transcript: text },
  }));
  return Object.assign(list, { length: list.length }) as unknown as RecognitionResultList;
}

function ctorWith(available?: DictationRecognitionCtor["available"]): DictationRecognitionCtor {
  const fake = function () {} as unknown as DictationRecognitionCtor;
  if (available) fake.available = available;
  return fake;
}

afterEach(() => forgetModes());

describe("recognitionCtor", () => {
  it("finds the unprefixed spelling", () => {
    const view = { SpeechRecognition: ctorWith() } as unknown as Window;
    expect(recognitionCtor(view)).not.toBeNull();
  });

  it("finds the prefixed spelling, which is the only one Safari has", () => {
    const view = { webkitSpeechRecognition: ctorWith() } as unknown as Window;
    expect(recognitionCtor(view)).not.toBeNull();
  });

  it("is null where neither exists, which is Firefox", () => {
    expect(recognitionCtor({} as unknown as Window)).toBeNull();
  });
});

describe("readTranscript", () => {
  it("rebuilds from the whole list rather than from where it got to", () => {
    // The Android behaviour task-171 measured: the same phrase is re-sent, longer each
    // time, each copy marked final. Reading the whole list is what makes that harmless;
    // an incremental reader would have written all three.
    expect(readTranscript(results([{ text: "the menu shell", final: true }])).final).toBe(
      "the menu shell",
    );
    expect(
      readTranscript(results([{ text: "the menu shell in task 167", final: true }])).final,
    ).toBe("the menu shell in task 167");
  });

  it("separates what is settled from what is still being said", () => {
    const { final, interim } = readTranscript(
      results([
        { text: "file an issue", final: true },
        { text: "about the queue", final: false },
      ]),
    );
    expect(final).toBe("file an issue");
    expect(interim).toBe("about the queue");
  });

  it("joins phrases with one space and no stray whitespace", () => {
    const { final } = readTranscript(
      results([
        { text: "  first  ", final: true },
        { text: "second", final: true },
      ]),
    );
    expect(final).toBe("first second");
  });

  it("is empty for a list with nothing in it", () => {
    expect(readTranscript(results([]))).toEqual({ final: "", interim: "" });
  });
});

describe("readMode", () => {
  it("is unknown when the browser has no recogniser at all", async () => {
    await expect(readMode(null, "en-US")).resolves.toBe("unknown");
  });

  it("is remote when the browser has no on-device query to answer with", async () => {
    await expect(readMode(ctorWith(), "en-US")).resolves.toBe("remote");
  });

  it("claims local only when the pack is actually installed", async () => {
    await expect(readMode(ctorWith(async () => "available"), "en-US")).resolves.toBe("local");
  });

  it("offers the download rather than claiming local, for a pack that is merely fetchable", async () => {
    await expect(readMode(ctorWith(async () => "downloadable"), "en-GB")).resolves.toBe(
      "installable",
    );
  });

  it("is remote where the pack is unavailable, which is what the phone reported", async () => {
    await expect(readMode(ctorWith(async () => "unavailable"), "en-AU")).resolves.toBe("remote");
  });

  it("treats a throwing available() as no answer rather than as a local one", async () => {
    const thrower = ctorWith(() => Promise.reject(new Error("not implemented")));
    await expect(readMode(thrower, "en-NZ")).resolves.toBe("remote");
  });

  it("asks the browser once per language, however many fields ask", async () => {
    let asked = 0;
    const counting = ctorWith(async () => {
      asked += 1;
      return "available";
    });
    await Promise.all([
      readMode(counting, "en-US"),
      readMode(counting, "en-US"),
      readMode(counting, "en-US"),
    ]);
    expect(asked).toBe(1);
  });
});

describe("modeSentence", () => {
  it("says the audio stays here only for the local mode", () => {
    expect(modeSentence("local")).toContain("on this device");
    expect(modeSentence("remote")).toContain("browser's speech service");
    expect(modeSentence("installable")).toContain("browser's speech service");
    expect(modeSentence("unknown")).toBe("");
  });
});

describe("errorSentence", () => {
  it("says nothing for a stop the person asked for", () => {
    expect(errorSentence("aborted")).toBeNull();
  });

  it("names the keyboard microphone when permission is refused", () => {
    expect(errorSentence("not-allowed")).toContain("keyboard");
  });

  it("reassures that nothing was lost when nothing was heard", () => {
    expect(errorSentence("no-speech")).toContain("unchanged");
  });

  it("still says something for a code it has never seen", () => {
    expect(errorSentence("something-new")).toBe("Dictation stopped: something-new.");
  });
});

describe("isTerminalError", () => {
  it("stops stitching where restarting cannot help", () => {
    expect(isTerminalError("not-allowed")).toBe(true);
    expect(isTerminalError("audio-capture")).toBe(true);
  });

  it("keeps stitching through a silence or a dropped network call", () => {
    expect(isTerminalError("no-speech")).toBe(false);
    expect(isTerminalError("network")).toBe(false);
  });
});
