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

// The listing row. `GET /tasks` answers with these rather than whole records: a list
// draws a title, a badge and a place in line, and the log it was being sent with every
// row is the bulk of the response (task-484). A component that needs the spec, the
// acceptance criteria or the log reads the detail route instead, which is the fetch
// opening a task already makes.
export type { TaskSummaryReadOutput as TaskSummaryRead } from "./generated";
