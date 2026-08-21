/**
 * The generated-client check has to distinguish two failures that used to look alike.
 *
 * Task-189: one message covered both "the client does not match the schema" and "the
 * client matches it but is not committed", and named only the first remedy. An agent
 * followed that remedy exactly, got the identical failure, and paid another four and a
 * half minutes for it. These tests are about what the messages say, because what they
 * said is what cost the time.
 */

import { describe, expect, it } from "vitest";

import { differences, staleMessage, uncommittedMessage } from "./check-generated-client.mjs";

const file = (text) => Buffer.from(text, "utf8");

describe("differences", () => {
  it("sees a file whose contents changed", () => {
    const before = new Map([["types.gen.ts", file("old")]]);
    const after = new Map([["types.gen.ts", file("new")]]);

    expect(differences(before, after)).toEqual(["types.gen.ts"]);
  });

  it("sees a file the generator added", () => {
    const after = new Map([["sdk.gen.ts", file("x")]]);

    expect(differences(new Map(), after)).toEqual(["sdk.gen.ts"]);
  });

  it("sees a file the generator removed", () => {
    // A stray file under the generated directory is drift too, and openapi-ts clears
    // the output directory, so this is the case that catches one.
    const before = new Map([["stale.gen.ts", file("x")]]);

    expect(differences(before, new Map())).toEqual(["stale.gen.ts"]);
  });

  it("says nothing when regenerating changed nothing", () => {
    const files = new Map([["types.gen.ts", file("same")]]);

    expect(differences(files, new Map(files))).toEqual([]);
  });
});

describe("the stale message", () => {
  const message = staleMessage(["types.gen.ts"]);

  it("names the files that changed", () => {
    expect(message).toContain("src/api/generated/types.gen.ts");
  });

  it("does not send the reader back to the generator", () => {
    // The check has just run it. The old message said "Run `npm run generate:api` and
    // commit the result", so following it exactly reproduced the failure -- which is
    // the specific defect this file exists to prevent coming back.
    expect(message).not.toContain("generate:api");
  });

  it("asks for the thing that is actually missing", () => {
    expect(message).toContain("commit");
  });
});

describe("the uncommitted note", () => {
  const note = uncommittedMessage([" M frontend/src/api/generated/types.gen.ts"]);

  it("names the paths git reported", () => {
    expect(note).toContain("frontend/src/api/generated/types.gen.ts");
  });

  it("says it is not a failure, and why", () => {
    expect(note).toContain("runs before the commit");
  });

  it("cannot be mistaken for the stale message", () => {
    expect(note).not.toEqual(staleMessage(["types.gen.ts"]));
    expect(note).not.toContain("did not match");
  });
});
