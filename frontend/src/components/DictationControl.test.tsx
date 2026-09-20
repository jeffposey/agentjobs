import { useRef, useState } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DictationControl, DictationNote } from "./DictationControl";
import { forgetModes } from "../voice/speech";

/**
 * A recogniser standing in for the browser's, driven by the test rather than by a
 * microphone.
 *
 * It exists because the only instrument that proves the real thing works is a person
 * speaking into a device -- so what is tested here is everything around the audio: that
 * nothing is constructed before a press, that a session's text lands at the caret and
 * only at the caret, that a session ending on its own is stitched rather than treated
 * as the end, and that every failure says something.
 */
class FakeRecognition {
  static instances: Array<FakeRecognition> = [];
  static available?: (query: { langs: Array<string>; processLocally?: boolean }) => Promise<string>;
  static install?: (query: { langs: Array<string>; processLocally?: boolean }) => Promise<boolean>;
  static installs = 0;

  lang = "";
  continuous = false;
  interimResults = false;
  started = 0;
  stopped = 0;
  onresult: ((event: { results: unknown }) => void) | null = null;
  onerror: ((event: { error: string }) => void) | null = null;
  onend: (() => void) | null = null;
  onstart: (() => void) | null = null;

  constructor() {
    FakeRecognition.instances.push(this);
  }

  start() {
    this.started += 1;
    this.onstart?.();
  }

  stop() {
    this.stopped += 1;
  }

  abort() {
    this.stopped += 1;
  }

  /** Hand over one list of phrases, exactly as a recogniser would. */
  say(entries: Array<{ text: string; final: boolean }>) {
    const list = entries.map(({ text, final }) => ({
      length: 1,
      isFinal: final,
      0: { transcript: text },
    }));
    this.onresult?.({ results: Object.assign(list, { length: list.length }) });
  }

  /** The recogniser giving up on its own, which Android does every few seconds. */
  end() {
    this.onend?.();
  }

  fail(code: string) {
    this.onerror?.({ error: code });
  }
}

function installRecogniser(available?: (typeof FakeRecognition)["available"]) {
  FakeRecognition.instances = [];
  FakeRecognition.available = available;
  FakeRecognition.installs = 0;
  FakeRecognition.install = undefined;
  Object.defineProperty(window, "SpeechRecognition", {
    value: FakeRecognition,
    configurable: true,
    writable: true,
  });
}

function removeRecogniser() {
  Reflect.deleteProperty(window, "SpeechRecognition");
  Reflect.deleteProperty(window, "webkitSpeechRecognition");
}

/** The shape of the reporter's fields: React state, written through the DOM. */
function ControlledField({ initial = "" }: { initial?: string }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const [value, setValue] = useState(initial);
  return (
    <>
      <label>
        What happened
        <textarea ref={ref} value={value} onChange={(event) => setValue(event.target.value)} />
      </label>
      <DictationControl label="What happened" target={() => ref.current} />
      <p data-testid="mirror">{value}</p>
    </>
  );
}

/** The shape of the create form's fields: uncontrolled, read at submit. */
function UncontrolledField({ initial = "" }: { initial?: string }) {
  return (
    <form>
      <label>
        Summary
        <textarea name="summary" defaultValue={initial} />
      </label>
      <DictationControl
        label="Summary"
        target={() =>
          document.querySelector<HTMLTextAreaElement>('textarea[name="summary"]') ?? null
        }
      />
    </form>
  );
}

/** A press of the microphone. `fireEvent` rather than user-event, which this
 * project does not carry, and a click is the whole gesture here. */
function press(button: HTMLElement) {
  fireEvent.click(button);
}

function latest(): FakeRecognition {
  const instance = FakeRecognition.instances.at(-1);
  if (!instance) throw new Error("no recogniser was constructed");
  return instance;
}

afterEach(() => {
  removeRecogniser();
  forgetModes();
  vi.restoreAllMocks();
});

describe("where the browser cannot dictate", () => {
  it("renders no microphone at all, rather than a dead one", () => {
    removeRecogniser();
    render(<ControlledField />);
    expect(screen.queryByRole("button", { name: /dictate/i })).not.toBeInTheDocument();
  });

  it("names the keyboard's own microphone, which still works there", () => {
    removeRecogniser();
    render(<DictationNote />);
    expect(screen.getByText(/microphone key on your phone or tablet/i)).toBeInTheDocument();
  });

  it("says nothing extra where the microphone is on the page", async () => {
    installRecogniser();
    const { container } = render(<DictationNote />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});

describe("pressing the microphone", () => {
  it("constructs nothing until it is pressed, so no permission is asked for on load", async () => {
    installRecogniser(async () => "unavailable");
    render(<ControlledField />);
    await screen.findByRole("button", { name: /dictate into what happened/i });
    expect(FakeRecognition.instances).toHaveLength(0);
  });

  it("starts a recogniser on the press and says it is listening", async () => {
    installRecogniser();
    render(<ControlledField />);
    press(screen.getByRole("button", { name: /dictate into what happened/i }));
    expect(latest().started).toBe(1);
    expect(screen.getByTestId("dictation-interim")).toHaveTextContent("Listening…");
    expect(
      screen.getByRole("button", { name: /stop dictating into what happened/i }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("shows what is being said without putting it in the field yet", async () => {
    installRecogniser();
    render(<ControlledField />);
    press(screen.getByRole("button", { name: /dictate/i }));
    act(() => latest().say([{ text: "the filters match nothing", final: false }]));
    expect(screen.getByTestId("dictation-interim")).toHaveTextContent(
      "Hearing: the filters match nothing…",
    );
    expect(screen.getByTestId("mirror")).toHaveTextContent("");
  });
});

describe("what reaches the field", () => {
  it("writes the session's words into a controlled field, updating its React state", async () => {
    installRecogniser();
    render(<ControlledField />);
    press(screen.getByRole("button", { name: /dictate/i }));
    act(() => {
      const recogniser = latest();
      recogniser.say([{ text: "the filters match nothing", final: true }]);
      recogniser.end();
    });
    await waitFor(() =>
      expect(screen.getByTestId("mirror")).toHaveTextContent("the filters match nothing"),
    );
  });

  it("writes into an uncontrolled field, where a form reads its value at submit", async () => {
    installRecogniser();
    render(<UncontrolledField />);
    press(screen.getByRole("button", { name: /dictate/i }));
    act(() => {
      latest().say([{ text: "one capture control", final: true }]);
      latest().end();
    });
    const field = screen.getByLabelText("Summary") as HTMLTextAreaElement;
    await waitFor(() => expect(field.value).toBe("one capture control"));
  });

  it("appends to what was already typed rather than replacing it", async () => {
    installRecogniser();
    render(<ControlledField initial="I noticed that" />);
    const field = screen.getByLabelText("What happened") as HTMLTextAreaElement;
    field.setSelectionRange(field.value.length, field.value.length);
    press(screen.getByRole("button", { name: /dictate/i }));
    act(() => {
      latest().say([{ text: "the queue is out of order", final: true }]);
      latest().end();
    });
    await waitFor(() =>
      expect(screen.getByTestId("mirror")).toHaveTextContent(
        "I noticed that the queue is out of order",
      ),
    );
  });

  it("writes a re-sent, progressively longer phrase once, not three times", async () => {
    // The Android behaviour from task-171. An incremental reader would have written
    // "the menu" then "the menu shell" then the whole thing, one after another.
    installRecogniser();
    render(<ControlledField />);
    press(screen.getByRole("button", { name: /dictate/i }));
    act(() => {
      const recogniser = latest();
      recogniser.say([{ text: "the menu", final: true }]);
      recogniser.say([{ text: "the menu shell", final: true }]);
      recogniser.say([{ text: "the menu shell in task 167", final: true }]);
      recogniser.end();
    });
    await waitFor(() =>
      expect(screen.getByTestId("mirror")).toHaveTextContent("the menu shell in task 167"),
    );
  });

  it("leaves the field alone when a session heard nothing", async () => {
    installRecogniser();
    render(<ControlledField initial="typed by hand" />);
    press(screen.getByRole("button", { name: /dictate/i }));
    act(() => latest().end());
    expect(screen.getByTestId("mirror")).toHaveTextContent("typed by hand");
  });
});

describe("a recogniser that ends on its own", () => {
  it("is restarted and stitched, because Android stops mid-paragraph", async () => {
    installRecogniser();
    render(<ControlledField />);
    press(screen.getByRole("button", { name: /dictate/i }));
    const recogniser = latest();
    act(() => {
      recogniser.say([{ text: "the first part", final: true }]);
      recogniser.end();
    });
    await waitFor(() => expect(recogniser.started).toBe(2));
    act(() => {
      recogniser.say([{ text: "and the second", final: true }]);
      recogniser.end();
    });
    await waitFor(() =>
      expect(screen.getByTestId("mirror")).toHaveTextContent("the first part and the second"),
    );
    expect(screen.getByRole("button", { name: /stop dictating/i })).toBeInTheDocument();
  });

  it("stops restarting once the person presses stop", async () => {
    installRecogniser();
    render(<ControlledField />);
    press(screen.getByRole("button", { name: /dictate/i }));
    const recogniser = latest();
    press(screen.getByRole("button", { name: /stop dictating/i }));
    act(() => {
      recogniser.say([{ text: "the last words", final: true }]);
      recogniser.end();
    });
    await waitFor(() =>
      expect(screen.getByTestId("mirror")).toHaveTextContent("the last words"),
    );
    expect(recogniser.started).toBe(1);
    expect(
      await screen.findByRole("button", { name: /dictate into what happened/i }),
    ).toHaveAttribute("aria-pressed", "false");
  });

  it("gives up rather than restarting forever when nothing is ever heard", async () => {
    installRecogniser();
    render(<ControlledField />);
    press(screen.getByRole("button", { name: /dictate/i }));
    const recogniser = latest();
    for (let round = 0; round < 3; round += 1) act(() => recogniser.end());
    expect(await screen.findByRole("alert")).toHaveTextContent(/kept stopping/i);
    expect(recogniser.started).toBe(3);
  });
});

describe("failures are stated, never silent", () => {
  it("explains a refused microphone and points at the keyboard one", async () => {
    installRecogniser();
    render(<ControlledField />);
    press(screen.getByRole("button", { name: /dictate/i }));
    act(() => {
      latest().fail("not-allowed");
      latest().end();
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(/Microphone access was refused/i);
  });

  it("does not restart after a refusal, because restarting cannot help", async () => {
    installRecogniser();
    render(<ControlledField />);
    press(screen.getByRole("button", { name: /dictate/i }));
    const recogniser = latest();
    act(() => {
      recogniser.fail("not-allowed");
      recogniser.end();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /dictate into what happened/i })).toBeInTheDocument(),
    );
    expect(recogniser.started).toBe(1);
  });

  it("says nothing was heard rather than failing silently", async () => {
    installRecogniser();
    render(<ControlledField />);
    press(screen.getByRole("button", { name: /dictate/i }));
    act(() => latest().fail("no-speech"));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Nothing was heard/i);
  });
});

describe("saying where the audio goes", () => {
  it("claims the device only when the browser says the pack is installed", async () => {
    installRecogniser(async () => "available");
    render(<ControlledField />);
    press(screen.getByRole("button", { name: /dictate/i }));
    expect(await screen.findByText(/recognised on this device/i)).toBeInTheDocument();
  });

  it("says the audio leaves when the pack is unavailable, which is what the phone reported", async () => {
    installRecogniser(async () => "unavailable");
    render(<ControlledField />);
    press(screen.getByRole("button", { name: /dictate/i }));
    expect(await screen.findByText(/browser's speech service/i)).toBeInTheDocument();
  });

  it("offers the one-time download once per form, not once per field", async () => {
    installRecogniser(async () => "downloadable");
    render(
      <>
        <ControlledField />
        <ControlledField />
        <DictationNote />
      </>,
    );
    await screen.findByRole("button", { name: /keep speech on this device/i });
    expect(screen.getAllByRole("button", { name: /keep speech on this device/i })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: /dictate into/i })).toHaveLength(2);
  });

  it("does not offer a download where the pack cannot be fetched at all", async () => {
    installRecogniser(async () => "unavailable");
    const { container } = render(<DictationNote />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("claims the device only after the pack has actually arrived", async () => {
    let state = "downloadable";
    installRecogniser(async () => state);
    FakeRecognition.install = async () => {
      FakeRecognition.installs += 1;
      state = "available";
      return true;
    };
    render(
      <>
        <ControlledField />
        <DictationNote />
      </>,
    );
    press(await screen.findByRole("button", { name: /keep speech on this device/i }));
    await waitFor(() => expect(FakeRecognition.installs).toBe(1));
    press(screen.getByRole("button", { name: /dictate into/i }));
    expect(await screen.findByText(/recognised on this device/i)).toBeInTheDocument();
  });

  it("says so rather than silently staying remote when the download fails", async () => {
    installRecogniser(async () => "downloadable");
    FakeRecognition.install = async () => {
      throw new Error("component never installed");
    };
    render(<DictationNote />);
    press(await screen.findByRole("button", { name: /keep speech on this device/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be installed/i);
  });
});
