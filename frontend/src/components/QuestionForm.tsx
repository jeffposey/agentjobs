import type { AnswerSubmission, LogEntry } from "../api/generated";

/**
 * The answering half of task-017: open questions rendered as things to tap.
 *
 * The feature exists because Jeff reads these handoffs on a phone. task-076 and
 * task-077 each arrived at `human`/`decision` with four substantive questions in the
 * ball_prompt, and answering them meant typing four paragraphs with a thumb. Asked as
 * choices with a recommendation marked, the same four took seconds.
 *
 * Three rules, each of which came out of that session rather than out of taste:
 *
 *   1. **Free text is on every question, always.** He rejected every option offered on
 *      one of the four and typed a better answer. A form that cannot capture that is
 *      worse than the prose box it replaces, so "Something else" is not conditional on
 *      the options being empty, or on a mode, or on a disclosure being opened.
 *   2. **Nothing is preselected**, including the option the agent recommends. A
 *      prefilled answer is one a tired reader submits without reading. The
 *      recommendation is a visible mark and no more.
 *   3. **Selecting does not submit.** One explicit Submit writes every answer and moves
 *      the ball once; see the decision on task-017. A radio button that fired a state
 *      change would be unrecoverable on a phone.
 */

/** A question entry after its payload has been read out of `data`. */
export type OpenQuestion = {
  id: number;
  body: string;
  options: Array<{ label: string; description: string | null; recommended: boolean }>;
  multiSelect: boolean;
  placeholder: string | null;
};

/** What the human has entered for one question, before it becomes an AnswerSubmission. */
export type Draft = { selected: Array<string>; other: string };

export const EMPTY_DRAFT: Draft = { selected: [], other: "" };

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

/**
 * Read one question entry's payload.
 *
 * The server types and validates this on the way in, so a malformed option list cannot
 * be written. It is still read defensively here, because `LogEntry.data` is
 * `Record<string, unknown>` on this side of the wire and a record written before
 * task-017 -- of which this repository holds several -- carries no payload at all.
 */
export function readQuestion(entry: LogEntry): OpenQuestion {
  const data = asRecord(entry.data);
  const rawOptions = Array.isArray(data.options) ? data.options : [];
  return {
    id: entry.id,
    body: entry.body ?? "",
    options: rawOptions.flatMap((raw) => {
      const option = asRecord(raw);
      return typeof option.label === "string" && option.label
        ? [
            {
              label: option.label,
              description: typeof option.description === "string" ? option.description : null,
              recommended: option.recommended === true,
            },
          ]
        : [];
    }),
    multiSelect: data.multi_select === true,
    placeholder: typeof data.placeholder === "string" ? data.placeholder : null,
  };
}

/**
 * The questions with no answer threaded to them, oldest first.
 *
 * Deliberately every open question on the task, not only the ones this handoff raised.
 * A question that outlived its handoff is still open -- decision 3 on task-017 chose to
 * let it be a backlog rather than sweep it -- and hiding it here would make it a leak.
 */
export function openQuestions(entries: Array<LogEntry>): Array<OpenQuestion> {
  const answered = new Set(
    entries.filter((entry) => entry.type === "answer" && entry.re).map((entry) => entry.re),
  );
  return entries
    .filter((entry) => entry.type === "question" && !answered.has(entry.id))
    .sort((left, right) => left.id - right.id)
    .map(readQuestion);
}

/** The drafts that carry something, as the API wants them. Untouched questions are omitted. */
export function submissions(drafts: Record<number, Draft>): Array<AnswerSubmission> {
  return Object.entries(drafts).flatMap(([id, draft]) => {
    const other = draft.other.trim();
    if (draft.selected.length === 0 && !other) return [];
    return [{ re: Number(id), selected: draft.selected, other: other || null }];
  });
}

function toggle(draft: Draft, label: string, multiSelect: boolean): Draft {
  if (!multiSelect) {
    // Tapping the chosen option again clears it. Without this a single-select question
    // cannot be un-answered once touched, and the only way back is reloading the page.
    return { ...draft, selected: draft.selected[0] === label ? [] : [label] };
  }
  const selected = draft.selected.includes(label)
    ? draft.selected.filter((current) => current !== label)
    : [...draft.selected, label];
  return { ...draft, selected };
}

function Option({
  question,
  option,
  draft,
  disabled,
  onChange,
}: {
  question: OpenQuestion;
  option: OpenQuestion["options"][number];
  draft: Draft;
  disabled: boolean;
  onChange: (next: Draft) => void;
}) {
  const chosen = draft.selected.includes(option.label);
  return (
    <li>
      <button
        type="button"
        disabled={disabled}
        aria-pressed={chosen}
        onClick={() => onChange(toggle(draft, option.label, question.multiSelect))}
        className={`touch-target flex w-full flex-col items-start gap-1 rounded-lg border p-3 text-left disabled:opacity-60 ${
          chosen
            ? "border-emerald-500 bg-emerald-950/40"
            : "border-dark-border bg-dark-bg hover:border-yellow-600/60"
        }`}
      >
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-semibold">{option.label}</span>
          {option.recommended && (
            <span className="rounded border border-blue-500/60 px-2 py-0.5 text-xs uppercase text-blue-300">
              Recommended
            </span>
          )}
        </span>
        {option.description && (
          <span className="text-sm text-dark-muted">{option.description}</span>
        )}
      </button>
    </li>
  );
}

export function QuestionForm({
  questions,
  drafts,
  disabled = false,
  onChange,
}: {
  questions: Array<OpenQuestion>;
  drafts: Record<number, Draft>;
  disabled?: boolean;
  onChange: (id: number, next: Draft) => void;
}) {
  if (questions.length === 0) return null;
  return (
    <div className="space-y-4" data-open-questions={questions.length}>
      <p className="text-sm text-dark-muted">
        {questions.length === 1 ? "One open question" : `${questions.length} open questions`}. Tap an
        answer, or write your own — both are recorded.
      </p>
      {questions.map((question, index) => {
        const draft = drafts[question.id] ?? EMPTY_DRAFT;
        const otherId = `question-${question.id}-other`;
        return (
          <fieldset
            key={question.id}
            className="rounded-lg border border-dark-border bg-dark-surface p-3"
            data-question-id={question.id}
          >
            <legend className="px-1 text-sm font-semibold">
              {index + 1}. {question.body}
            </legend>
            {question.multiSelect && (
              <p className="px-1 pb-2 text-xs uppercase text-dark-muted">Choose any that apply</p>
            )}
            {question.options.length > 0 && (
              <ul className="space-y-2">
                {question.options.map((option) => (
                  <Option
                    key={option.label}
                    question={question}
                    option={option}
                    draft={draft}
                    disabled={disabled}
                    onChange={(next) => onChange(question.id, next)}
                  />
                ))}
              </ul>
            )}
            {/* Unconditional, and last so it reads as the escape from the list above it. */}
            <label htmlFor={otherId} className="mt-3 block text-xs uppercase text-dark-muted">
              {question.options.length > 0 ? "Something else" : "Your answer"}
            </label>
            <textarea
              id={otherId}
              rows={2}
              disabled={disabled}
              value={draft.other}
              onChange={(event) => onChange(question.id, { ...draft, other: event.target.value })}
              placeholder={question.placeholder ?? "Answer in your own words..."}
              className="mt-1 w-full rounded-lg border border-dark-border bg-dark-bg p-3 text-dark-text focus:border-yellow-500 focus:outline-none"
            />
          </fieldset>
        );
      })}
    </div>
  );
}
