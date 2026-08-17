// Application-facing names for the generated API types.
//
// FastAPI emits a separate schema for a model whenever its request and response
// shapes differ, so the task record arrives as `TaskReadInput` and `TaskReadOutput`.
// The app only ever reads tasks, and `display_status` -- a computed, read-only field
// several components render -- exists on the output shape alone. Aliasing it back to
// `TaskRead` here keeps one accurate name in the components instead of scattering a
// generator detail through them, and leaves one place to change if the split moves.
//
// Import from here rather than from `./generated` directly.

export * from "./generated";
export type { TaskReadOutput as TaskRead } from "./generated";
