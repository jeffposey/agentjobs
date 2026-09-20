import { afterEach, describe, expect, it } from "vitest";

import { newOperationId } from "./operationId";

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/** Restore whatever the environment really has after a test takes it away. */
const nativeRandomUUID = globalThis.crypto?.randomUUID;
const nativeGetRandomValues = globalThis.crypto?.getRandomValues;

function setCrypto(patch: Partial<Crypto>) {
  Object.assign(globalThis.crypto, patch);
}

afterEach(() => {
  setCrypto({ randomUUID: nativeRandomUUID, getRandomValues: nativeGetRandomValues });
});

describe("newOperationId", () => {
  it("returns a version 4 UUID where the platform has randomUUID", () => {
    expect(newOperationId()).toMatch(UUID_V4);
  });

  it("still returns one where it does not -- which is every insecure origin", () => {
    // What a browser on a plain-HTTP origin actually has: `getRandomValues`, and no
    // `randomUUID` at all. Before this helper existed, the submit handler threw here.
    setCrypto({ randomUUID: undefined as unknown as Crypto["randomUUID"] });
    expect(globalThis.crypto.randomUUID).toBeUndefined();

    const ids = new Set(Array.from({ length: 200 }, () => newOperationId()));
    for (const id of ids) expect(id).toMatch(UUID_V4);
    // Distinct, because the server treats a repeated operation_id as a retry of the
    // first request and replays its result instead of writing again.
    expect(ids.size).toBe(200);
  });

  it("degrades rather than throws where there is no crypto at all", () => {
    setCrypto({
      randomUUID: undefined as unknown as Crypto["randomUUID"],
      getRandomValues: undefined as unknown as Crypto["getRandomValues"],
    });
    expect(newOperationId()).toMatch(UUID_V4);
  });
});

/**
 * The test that would have caught the defect, rather than the one that proves the fix.
 *
 * `crypto.randomUUID` threw on every mutation from a plain-HTTP origin, and nothing saw
 * it: Playwright's server binds `127.0.0.1`, which is a secure context, and jsdom
 * defines the function unconditionally. Neither harness can represent the origin that
 * fails. A source rule can, and this is the only kind of check that survives somebody
 * adding a seventh call site.
 */
describe("no module calls crypto.randomUUID directly", () => {
  // Vite reads the files, so this needs no Node types and no filesystem walk of its
  // own; `eager` makes it a plain record of path -> source text at transform time.
  const sources = import.meta.glob("../**/*.{ts,tsx}", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>;

  it("has a corpus to check at all, so an empty pass cannot be mistaken for a clean one", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50);
  });

  it("routes every one through newOperationId, which works on an insecure origin", () => {
    const offenders = Object.entries(sources)
      .filter(([path]) => !path.includes("/generated/"))
      .filter(([path]) => !path.includes("/api/operationId."))
      .filter(([, source]) => /crypto\.randomUUID\s*\(/.test(source))
      .map(([path]) => path);

    expect(offenders).toEqual([]);
  });
});
