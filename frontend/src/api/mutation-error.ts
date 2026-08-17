// Reading the API's structured refusals off a thrown value.
//
// The mutation routes answer a refused precondition with a body carrying a stable
// `code` -- `revision_conflict`, `invalid_transition`, `task_not_found` -- and a
// human-readable `message`. The generated types do not describe it: the OpenAPI
// schema declares only 422 for these operations, so every 409 arrives typed as a
// validation error it is not. Regenerating the client would not fix that, because
// the omission is in the schema.
//
// The fetch client throws the parsed response body verbatim on a non-2xx
// (`client/client.gen.ts`), so what a caller catches is that object. This narrows it
// without asserting a type that would be a lie, and returns null for anything else
// -- a network failure, a thrown string -- so callers can tell "the server refused
// and said why" apart from "something else went wrong".

export type ApiRefusal = {
  code: string;
  message: string;
  suggestedAction: string | null;
};

export function readRefusal(error: unknown): ApiRefusal | null {
  if (typeof error !== "object" || error === null) return null;
  const body = error as Record<string, unknown>;
  const code = body.code;
  // `message` is the structured field; `detail` carries the same text for the
  // benefit of clients that only read FastAPI's default key.
  const message = typeof body.message === "string" ? body.message : body.detail;
  if (typeof code !== "string" || typeof message !== "string") return null;
  return {
    code,
    message,
    suggestedAction: typeof body.suggested_action === "string" ? body.suggested_action : null,
  };
}
