import { describe, expect, it } from "vitest";

import { identityHeadline } from "./identityProblem";

/**
 * The value of this file is the last case. Everything above it is a lookup table, and a
 * test of a lookup table is worth little; the reason to have one is that the table used
 * to be a ternary that quietly labelled every unrecognised refusal "No user configured",
 * which pointed the reader at a file that was already correct.
 */
describe("identityHeadline", () => {
  const cases: Array<[string, string]> = [
    ["unconfigured", "No user configured. "],
    ["unmapped", "Your login is not mapped. "],
    ["retired", "That person has retired. "],
    ["unknown_actor", "Mapped to an actor this project does not define. "],
    ["ambiguous", "Cannot tell who is asking. "],
    ["not_a_person", "This is not a person. "],
  ];

  it.each(cases)("names the %s problem as its own thing", (problem, expected) => {
    expect(identityHeadline(problem)).toBe(expected);
  });

  it("gives every problem a distinct headline", () => {
    const headlines = new Set(cases.map(([problem]) => identityHeadline(problem)));

    expect(headlines.size).toBe(cases.length);
  });

  it("does not claim a specific cause for a code it does not know", () => {
    // The failure this file exists to prevent: a new backend problem code arriving and
    // being announced as one of the old ones. Vague is recoverable; wrong is not.
    for (const unknown of ["something_new", "", null, undefined]) {
      expect(identityHeadline(unknown)).toBe("Cannot act as anyone. ");
    }
  });
});
