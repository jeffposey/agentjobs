/**
 * A UUID for a mutation's `operation_id`, on every origin this app can be opened from.
 *
 * **`crypto.randomUUID` is secure-context-only.** It exists on `https://` and on
 * `localhost`, and does not exist at all on plain HTTP to anything else -- a LAN
 * address, or a tailnet IP. Every mutation that minted one there died with
 * `crypto.randomUUID is not a function` inside the submit handler, *before* its request
 * was sent: filing a task, moving one in the queue, promoting a draft, editing a field,
 * attaching a screenshot.
 *
 * **Be precise about where that bit.** The dashboard served through the tailnet front
 * door is `https://` and therefore a secure context -- measured 2026-09-20:
 * `isSecureContext: true`, `randomUUID: function`. So this was never broken on the
 * surface actually in daily use. What it broke was any plain-HTTP binding, which is
 * what a review sandbox bound to a tailnet address is, and that is where it was found.
 * Measured on that origin the same day: `isSecureContext: false`,
 * `randomUUID: undefined`, `getRandomValues: function`.
 *
 * `crypto.getRandomValues` is **not** secure-context-only, so the fallback below is as
 * random as the native call; only the formatting is done by hand.
 *
 * **Why nothing in the suite could have caught it.** Playwright's server binds
 * `127.0.0.1`, which is a secure context by origin, and jsdom defines `randomUUID`
 * unconditionally. Both harnesses are right about everything except the one property
 * that matters, which is why the check that guards this is a source rule
 * (`operationId.test.ts`) rather than another browser test.
 */

const UUID_BYTES = 16;

function randomBytes(): Uint8Array {
  const bytes = new Uint8Array(UUID_BYTES);
  // Available on every origin, secure or not -- unlike `randomUUID` beside it.
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
    return bytes;
  }
  // Nothing observed needs this. It is here so a surface nobody has thought of yet
  // degrades to a worse id rather than to a thrown exception in a submit handler.
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = Math.floor(Math.random() * 256);
  }
  return bytes;
}

/**
 * A fresh RFC 4122 version 4 UUID.
 *
 * The server validates `operation_id` as a UUID, so this has to be the real shape and
 * not merely a unique string: the version and variant bits below are what make it one.
 */
export function newOperationId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  const bytes = randomBytes();
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40; // version 4
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80; // variant 10xx
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10, 16).join(""),
  ].join("-");
}
