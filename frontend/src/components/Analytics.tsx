import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import type { AnalyticsResponse } from "../api/types";
import { AgingChart, BacklogChart, HolderChart, ThroughputChart } from "./AnalyticsCharts";
import {
  THIN_HISTORY_DAYS,
  ageWords,
  clampWarning,
  days,
  deltaWindow,
  historyOf,
  holderWords,
  openLevel,
  stuckPhrase,
  summaryTiles,
  type SummaryTile,
} from "./analyticsSeries";

/**
 * The analytics page: six regions, in the order of the four questions (§8.1).
 *
 * `docs/analytics-design.md` §1 is why it exists: *a count of 125 open tasks means
 * nothing on its own; it means something against last week's 116.* Every panel here
 * answers one question a person opens the page to ask, in the order they would ask
 * them, so "is this project OK" is above the fold on a phone.
 *
 * It is presentational on purpose -- it takes a response and renders it, and
 * `AnalyticsPage` in `App.tsx` is what fetches one. That is what lets the three states
 * of §9 be tested against seeded fixtures rather than against a server, and those
 * three are the hardest thing on this page to get right: "nothing has happened here"
 * and "we do not know what happened" are different sentences, and a page that drew a
 * flat line at zero for either would be lying in a way no reader could detect.
 *
 * **Unframed**, deliberately. task-294's `h-dvh` frame is the Dashboard's alone; a
 * page of stacked panels is a document and may legitimately scroll.
 */

/** The four ranges §7.1 offers, which are the whole of the control surface. */
const RANGES: ReadonlyArray<{ key: string; label: string }> = [
  { key: "30d", label: "30 days" },
  { key: "90d", label: "90 days" },
  { key: "12m", label: "12 months" },
  { key: "all", label: "All" },
];

/**
 * Which bucket the readouts are describing, defaulting to the most recent.
 *
 * `null` means "the latest", rather than a number that would have to be corrected
 * every time the range changes the bucket count. A reader who has tapped keeps their
 * choice; one who has not always lands on today.
 */
function useBucket(count: number): [number, (index: number) => void] {
  const [chosen, setChosen] = useState<number | null>(null);
  const latest = Math.max(count - 1, 0);
  const selected = chosen === null ? latest : Math.min(chosen, latest);
  return [selected, setChosen];
}

/** A titled region, matching the Dashboard's panel conventions. */
function Panel({
  title,
  question,
  children,
  className = "",
}: {
  title: string;
  question?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-2xl border border-dark-border bg-dark-surface p-4 sm:p-5 ${className}`}
      aria-label={title}
    >
      <h2 className="text-base font-semibold text-dark-text sm:text-lg">{title}</h2>
      {question && <p className="mt-0.5 text-sm text-dark-muted">{question}</p>}
      {children}
    </section>
  );
}

/** How a tone is coloured. Never the only channel: the words carry it too (§8.2). */
const TONE_CLASS: Record<string, string> = {
  good: "text-emerald-300",
  bad: "text-amber-300",
  neutral: "text-dark-muted",
};

/**
 * One count and its change (§8.2).
 *
 * The delta is the entire reason the page exists, so its absence is rendered as a
 * sentence rather than as a blank: a tile that showed a count and nothing else would
 * be the strip task-294 removed. Direction is in the word -- *more*, *fewer* -- as
 * well as in the glyph and the colour, because red-up is bad for the backlog and good
 * for completions and no colour can say both.
 */
function SummaryCount({
  tile,
  window,
  projectId,
}: {
  tile: SummaryTile;
  window: string;
  projectId: string;
}) {
  const delta = tile.delta;
  let change = tile.suppressed ? `no comparison — ${tile.suppressed}` : "";
  if (delta) {
    const size = Math.abs(delta.value);
    if (delta.kind === "flow") change = `+${size} ${window}`;
    else if (delta.direction === "up") change = `▲ ${size} more ${window}`;
    else if (delta.direction === "down") change = `▼ ${size} fewer ${window}`;
    else change = `no change ${window}`;
  }
  return (
    <Link
      to={`/p/${encodeURIComponent(projectId)}/tasks?status=${tile.status}`}
      className="touch-target flex flex-col rounded-xl border border-dark-border bg-dark-bg/60 px-3 py-2 hover:border-blue-400"
      data-testid={`summary-${tile.key}`}
      aria-label={`${tile.label}: ${tile.count}. ${change}. Counted as ${tile.basis}.`}
      title={tile.basis}
    >
      <span className="text-xs text-dark-muted">{tile.label}</span>
      <span className="text-2xl font-semibold text-dark-text tabular-nums">{tile.count}</span>
      <span className={`text-xs ${delta ? (TONE_CLASS[delta.tone] ?? "") : "text-dark-muted"}`}>
        {change}
      </span>
    </Link>
  );
}

/**
 * §9.1: nothing has happened here yet.
 *
 * One bordered block with a sentence in it. Not an empty axis, not a flat line at
 * zero, and not a spinner -- an empty chart frame reads as a failure, and a sentence
 * reads as a state.
 */
function NoHistory() {
  return (
    <section
      className="rounded-2xl border border-dark-border bg-dark-surface p-6 text-dark-muted"
      aria-label="No history yet"
      data-testid="no-history"
    >
      <p className="text-dark-text">No history yet.</p>
      <p className="mt-1 text-sm">Charts appear once this project has a day of activity.</p>
    </section>
  );
}

export function Analytics({
  data,
  projectId,
  rangeKey,
  onRangeChange,
  now = new Date(),
}: {
  /** Null while the one request for the whole page is still in flight. */
  data: AnalyticsResponse | null;
  projectId: string;
  rangeKey: string;
  onRangeChange: (key: string) => void;
  /** Injected so the three states of §9 are testable against a fixed instant. */
  now?: Date;
}) {
  const backlog = data?.backlog ?? [];
  const throughput = data?.throughput ?? [];
  const holders = data?.holders ?? [];
  const [backlogBucket, selectBacklog] = useBucket(backlog.length);
  const [throughputBucket, selectThroughput] = useBucket(throughput.length);
  const [holderBucket, selectHolder] = useBucket(holders.length);

  const rangeControl = (
    <div
      className="flex flex-wrap items-center gap-1"
      role="group"
      aria-label="How far back to look"
    >
      {RANGES.map((range) => (
        <button
          key={range.key}
          type="button"
          onClick={() => onRangeChange(range.key)}
          aria-pressed={range.key === rangeKey}
          className={`touch-target rounded-lg border px-2.5 py-1 text-sm ${
            range.key === rangeKey
              ? "border-blue-400 bg-blue-400/10 text-dark-text"
              : "border-dark-border text-dark-muted hover:text-dark-text"
          }`}
        >
          {range.label}
        </button>
      ))}
    </div>
  );

  if (!data) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold text-dark-text">Analytics</h1>
        <p className="text-dark-muted">Reading this project's history…</p>
      </div>
    );
  }

  const history = historyOf(data.coverage, now);
  const tiles = summaryTiles(data, history);
  const window = deltaWindow(data.range, data.coverage);
  const level = openLevel(backlog);
  const clamp = clampWarning(level);
  const aging = data.aging ?? [];
  const oldest = data.oldest ?? [];
  const stuck = data.stuck ?? [];
  // §9.2: below the threshold the values are still facts and the trends are not.
  const showTrends = history.depth === "full";

  return (
    <div className="space-y-4 pb-6" data-testid="analytics-page">
      <header className="flex flex-wrap items-baseline justify-between gap-3">
        <h1 className="text-2xl font-bold text-dark-text">Analytics</h1>
        <div className="flex flex-col items-end gap-1">
          {rangeControl}
          {history.depth !== "full" && (
            <p className="text-xs text-dark-muted" data-testid="history-depth">
              {history.days === null
                ? "no history yet"
                : `${history.days} days of history — trends appear at ${THIN_HISTORY_DAYS}`}
            </p>
          )}
        </div>
      </header>

      <section
        className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5"
        aria-label="Counts and their change"
      >
        {tiles.map((tile) => (
          <SummaryCount key={tile.key} tile={tile} window={window} projectId={projectId} />
        ))}
      </section>

      {history.depth === "none" ? (
        <NoHistory />
      ) : (
        <>
          <Panel title="Backlog" question="Is the backlog growing?">
            <BacklogChart
              points={backlog}
              bucket={data.range.bucket}
              selected={backlogBucket}
              onSelect={selectBacklog}
            />
          </Panel>

          <Panel title="Throughput and cycle time" question="Is work finishing faster?">
            <ThroughputChart
              points={throughput}
              bucket={data.range.throughput_bucket}
              showPercentiles={showTrends}
              selected={throughputBucket}
              onSelect={selectThroughput}
            />
          </Panel>

          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="Aging" question="Is anything aging badly?">
              <AgingChart buckets={aging} />
              <p className="mt-1 text-xs text-dark-muted">Open tasks only; archived tasks excluded.</p>
            </Panel>

            <Panel title="The ten oldest open tasks">
              {oldest.length === 0 ? (
                <p className="mt-2 text-sm text-dark-muted">No open tasks.</p>
              ) : (
                <ol className="mt-2 space-y-1" data-testid="oldest-tasks">
                  {oldest.map((task) => (
                    <li key={task.task_id} className="text-sm">
                      <Link
                        to={`/p/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(task.task_id)}`}
                        className="text-blue-300 hover:underline"
                      >
                        {task.title}
                      </Link>
                      <span className="text-dark-muted">
                        {" — "}
                        {ageWords(task.age_days)}
                        {task.ball ? `, ${holderWords(task.ball)}` : ""}
                        {task.ball_reason ? ` — ${task.ball_reason}` : ""}
                      </span>
                    </li>
                  ))}
                </ol>
              )}
            </Panel>
          </div>

          <Panel title="Stuck" question="Where is work stuck?">
            {stuck.length === 0 ? (
              <p className="mt-2 text-sm text-dark-muted">Nothing is held open.</p>
            ) : (
              <ul className="mt-2 space-y-1" data-testid="stuck-groups">
                {stuck.map((group) => (
                  <li key={`${group.ball}/${group.ball_reason}`} className="text-sm text-dark-text">
                    {stuckPhrase(group)}
                    {" · "}
                    <Link
                      to={`/p/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(group.oldest_task_id)}`}
                      className="text-blue-300 hover:underline"
                    >
                      {group.oldest_task_id}
                    </Link>
                    <span className="text-dark-muted">{` · mean ${days(group.mean_days_held)}`}</span>
                  </li>
                ))}
              </ul>
            )}
            <h3 className="mt-4 text-sm font-semibold text-dark-text">Who has held it</h3>
            <HolderChart
              points={holders}
              bucket={data.range.bucket}
              selected={holderBucket}
              onSelect={selectHolder}
            />
          </Panel>
        </>
      )}

      {/* `role="region"` explicitly: a <footer> nested inside the page is generic, and
          this is the one line saying what everything above it is allowed to claim. */}
      <footer
        role="region"
        className="rounded-2xl border border-dark-border bg-dark-bg/40 p-3 text-xs text-dark-muted"
        aria-label="What this page can claim"
        data-testid="coverage-footer"
      >
        <p>
          {data.coverage.note ?? "This project has no recorded history yet."}
          {` Buckets are ${data.range.bucket}s in ${data.range.timezone}.`}
        </p>
        {clamp && (
          <p className="mt-1 text-amber-300" data-testid="clamp-warning">
            {clamp}
          </p>
        )}
      </footer>
    </div>
  );
}
