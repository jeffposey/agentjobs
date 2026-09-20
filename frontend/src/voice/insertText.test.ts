import { describe, expect, it } from "vitest";

import { insertDictated, spaced, withPhrase } from "./insertText";

function textarea(value: string, start: number, end = start): HTMLTextAreaElement {
  const element = document.createElement("textarea");
  document.body.append(element);
  element.value = value;
  element.setSelectionRange(start, end);
  return element;
}

describe("spaced", () => {
  it("adds nothing at the start of an empty field", () => {
    expect(spaced("", "hello", "")).toBe("hello");
  });

  it("separates a phrase from the word before it", () => {
    expect(spaced("half a", "sentence", "")).toBe(" sentence");
  });

  it("leaves the space already there alone", () => {
    expect(spaced("half a ", "sentence", "")).toBe("sentence");
  });

  it("separates the phrase from what follows the caret too", () => {
    expect(spaced("start", "middle", "end")).toBe(" middle ");
  });
});

describe("withPhrase", () => {
  it("appends at the caret rather than replacing the field", () => {
    expect(withPhrase("keep this", "and this", 9, 9)).toEqual({
      value: "keep this and this",
      caret: 18,
    });
  });

  it("puts the words where the caret is, not at the end", () => {
    const { value } = withPhrase("before after", "middle", 6, 6);
    expect(value).toBe("before middle after");
  });

  it("replaces only what was selected", () => {
    const { value } = withPhrase("keep drop keep", "said", 5, 9);
    expect(value).toBe("keep said keep");
  });
});

describe("insertDictated", () => {
  it("appends to a field that already has text, keeping every word of it", () => {
    const element = textarea("the filters match nothing", 25);
    insertDictated(element, "on the task list");
    expect(element.value).toBe("the filters match nothing on the task list");
  });

  it("leaves the field untouched when nothing was heard", () => {
    const element = textarea("typed by hand", 13);
    expect(insertDictated(element, "   ")).toBe(false);
    expect(element.value).toBe("typed by hand");
  });

  it("inserts into the middle of a half-typed sentence", () => {
    const element = textarea("the queue is  today", 13);
    insertDictated(element, "broken");
    expect(element.value).toBe("the queue is broken today");
  });

  it("leaves the caret after the words it wrote, so dictating twice reads in order", () => {
    const element = textarea("", 0);
    insertDictated(element, "first");
    insertDictated(element, "second");
    expect(element.value).toBe("first second");
  });

  it("fires an input event, which is what a controlled React field listens for", () => {
    const element = textarea("", 0);
    let heard = 0;
    element.addEventListener("input", () => {
      heard += 1;
    });
    insertDictated(element, "spoken");
    expect(heard).toBe(1);
  });

  it("works on an input as well as a textarea", () => {
    const element = document.createElement("input");
    document.body.append(element);
    element.value = "Task list";
    element.setSelectionRange(9, 9);
    insertDictated(element, "filters match nothing");
    expect(element.value).toBe("Task list filters match nothing");
  });
});
