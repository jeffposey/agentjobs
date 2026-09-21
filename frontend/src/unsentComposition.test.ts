import { beforeEach, describe, expect, it } from "vitest";

import {
  clearUnsentComposition,
  hasUnsentComposition,
  setUnsentComposition,
} from "./unsentComposition";

describe("unsent composition", () => {
  beforeEach(clearUnsentComposition);

  it("is quiet on a page where nobody is typing", () => {
    expect(hasUnsentComposition()).toBe(false);
  });

  it("holds while a surface says it is holding text, and lets go when it says so", () => {
    setUnsentComposition("capture", true);
    expect(hasUnsentComposition()).toBe(true);
    setUnsentComposition("capture", false);
    expect(hasUnsentComposition()).toBe(false);
  });

  it("does not let one surface release another's claim", () => {
    // The reason this is a set of names rather than a counter: two forms open at once,
    // one of them emptied, must not amount to "nothing is unsent".
    setUnsentComposition("capture", true);
    setUnsentComposition("note", true);
    setUnsentComposition("note", false);
    expect(hasUnsentComposition()).toBe(true);
  });

  it("survives a surface that re-registers on every render", () => {
    // A component calling this from an effect says the same thing many times over. A
    // counter would leak a claim per render and never reach zero.
    for (let index = 0; index < 5; index += 1) setUnsentComposition("capture", true);
    setUnsentComposition("capture", false);
    expect(hasUnsentComposition()).toBe(false);
  });
});
