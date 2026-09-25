import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import type { AnalyticsResponse, SeriesCoverage } from "../api/types";
import { AgingChart, BacklogChart, HolderChart, ThroughputChart } from "./AnalyticsCharts";
import {
  CostPerTaskPanel,
  DurationChart,
  EstimateAccuracyChart,
  FinishChart,
  ReviewChart,
  RunOutcomeChart,
  RunsChart,
  SegmentsChart,
} from "./AnalyticsPanels";
import {
  THIN_HISTORY_DAYS,
  ageWords,
  clampWarning,
  days,
  deltaWindow,
  historyOf,
  holderWords,
  openLevel,
  summaryTiles,
  type SummaryTile,
} from "./analyticsSeries";
import {
  STUCK_BAND_LABEL,
  counted,
  deltaBaseline,
  orderStuck,
  seriesCaption,
  stuckRowPhrase,
  waitWords,
  type StuckBand,
} from "./analyticsSecondSet";

/**
 * The analytics page: the panels of `docs/analytics-design.md` §19.4, in its order.
 *
 * §1 is why it exists: *a count of 125 open tasks means nothing on its own; it means
 * something against last week's 116.* §16 is why this is the second version -- the
 * first answered four questions, and the owner found it not comprehensive and its
 * throughput chart unreadable. §18's metric catalogue is the answer to the first, and
 * §19.2 is the answer to the second: a chart carrying two axes and a percentile band
 * became two charts with one thing on each.
 *
 * The order is still "is this project OK" first, and the calls to action above the
 * machine: counts, backlog, throughput, where the time goes, review, finishes and
 * gates, runs, cost per completed task, and only then the two "right now" lists --
 * aging and stuck. The owner's ask was trends, and a list of what is old today is not
 * one.
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
  caption,
  children,
  className = "",
}: {
  title: string;
  question?: string;
  /**
   * §21.1's per-series sentence, where the series has one. It sits under the question
   * rather than only in the page footer because five sources have five baselines, and
   * one line at the bottom of a page cannot honestly cover all of them.
   */
  caption?: string | null;
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
      {caption && (
        <p className="mt-0.5 text-xs text-amber-300" data-testid="series-caption">
          {caption}
        </p>
      )}
      {children}
    </section>
  );
}

/**
 * A panel whose series has no rows, which is not a panel whose series is zero.
 *
 * §9.3's rule applied per series: a source that has recorded nothing yet says so in a
 * sentence and draws no axis. Padding the series back to the range with zeros is the
 * substitution §9.3 forbids by name, and an empty chart frame reads as a failure.
 */
function NoSeries({ caption }: { caption: string | null }) {
  return (
    <p className="mt-2 text-sm text-dark-muted" data-testid="no-series">
      {caption ?? "Nothing recorded for this window yet."}
    </p>
  );
}

/** How a tone is coloured. Never the only channel: the words carry it too (§8.2). */
const TONE_CLASS: Record<string, string> = {
  good: "text-emerald-300",
  bad: "text-amber-300",
  neutral: "text-dark-muted",
};

/**
 * One count and its change (§8.2, baselined by §19.1).
 *
 * The delta is the entire reason the page exists, so its absence is rendered as a
 * sentence rather than as a blank: a tile that showed a count and nothing else would
 * be the strip task-294 removed. Direction is in the word -- *more*, *fewer* -- as
 * well as in the glyph and the colour, because red-up is bad for the backlog and good
 * for completions and no colour can say both.
 *
 * `window` now names the date the comparison starts from rather than the nominal
 * range, which is the whole of §19.1: *"▲ 4 since 7 Sep"* is a claim a reader can
 * check, and *"▲ 145 in 90 days"* against a backfilled floor was not.
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
    // A rise equal to the whole count is the shape §19.1 is about, and against a
    // native baseline it is sometimes simply true -- every blocked task became
    // blocked this month. Saying which of the two it is costs three words, and
    // leaving it ambiguous is what made the old tile unreadable.
    if (delta.kind === "change" && delta.direction === "up" && size === tile.count && size > 0) {
      change = `${change} — all of them`;
    }
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

/** One line of the coverage footer: what one source family can be asked about (§19.4). */
function CoverageLine({ label, coverage }: { label: string; coverage?: SeriesCoverage }) {
  const note = seriesCaption(coverage);
  return (
    <li>
      <span className="text-dark-text">{label}</span>
      {": "}
      {note ?? "complete for this window"}
    </li>
  );
}

export function Analytics({
  data,
  projectId,
  rangeKey,
  onRangeChange,
  onResetEstimator,
  resettingEstimator = false,
  now = new Date(),
}: {
  /** Null while the one request for the whole page is still in flight. */
  data: AnalyticsResponse | null;
  projectId: string;
  rangeKey: string;
  onRangeChange: (key: string) => void;
  /** Forget the landing estimate's learned correction (task-586). Absent: no button. */
  onResetEstimator?: () => void;
  resettingEstimator?: boolean;
  /** Injected so the three states of §9 are testable against a fixed instant. */
  now?: Date;
}) {
  const backlog = data?.backlog ?? [];
  const throughput = data?.throughput ?? [];
  const holders = data?.holders ?? [];
  const segments = data?.segments ?? [];
  const finishes = data?.finishes ?? [];
  const gates = data?.gates ?? [];
  const estimates = data?.estimates ?? [];
  const runs = data?.runs ?? [];
  const machine = data?.machine ?? [];
  const review = data?.review ?? [];
  const cost = data?.cost_per_task ?? [];
  const [backlogBucket, selectBacklog] = useBucket(backlog.length);
  const [throughputBucket, selectThroughput] = useBucket(throughput.length);
  const [holderBucket, selectHolder] = useBucket(holders.length);
  const [segmentBucket, selectSegment] = useBucket(segments.length);
  const [finishBucket, selectFinish] = useBucket(finishes.length);
  const [durationBucket, selectDuration] = useBucket(Math.max(finishes.length, gates.length));
  const [estimateBucket, selectEstimate] = useBucket(estimates.length);
  const [runBucket, selectRun] = useBucket(runs.length);
  const [outcomeBucket, selectOutcome] = useBucket(runs.length);
  const [reviewBucket, selectReview] = useBucket(review.length);
  const [costBucket, selectCost] = useBucket(cost.length);

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
  const baseline = deltaBaseline(data.range, data.coverage);
  const tiles = summaryTiles(data, history, baseline);
  const level = openLevel(backlog);
  const clamp = clampWarning(level);
  const aging = data.aging ?? [];
  const oldest = data.oldest ?? [];
  const inReview = data.in_review ?? [];
  const openQuestions = data.open_questions ?? [];
  const stuck = orderStuck(data.stuck ?? []);
  const taskLink = (taskId: string) =>
    `/p/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}`;
  // A week-grain series carries its grain on its own coverage (§21.1's `bucket`), so a
  // readout says "week of 14 Sep" whatever the range's spine is doing. Defaulting to
  // the week rather than to `range.bucket` matters: every series in the second set but
  // runs is weekly, and labelling a week as a day would mis-state six panels.
  const grainOf = (coverage?: SeriesCoverage) => coverage?.bucket ?? "week";

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
          <SummaryCount key={tile.key} tile={tile} window={baseline.words} projectId={projectId} />
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

          <Panel title="Throughput" question="How much is getting finished?">
            <ThroughputChart
              points={throughput}
              bucket={data.range.throughput_bucket}
              selected={throughputBucket}
              onSelect={selectThroughput}
            />
          </Panel>

          <Panel
            title="Where the time goes"
            question="Which part of a task's life is growing?"
            caption={seriesCaption(data.segments_coverage)}
          >
            {segments.length === 0 ? (
              <NoSeries caption={seriesCaption(data.segments_coverage)} />
            ) : (
              <SegmentsChart
                points={segments}
                bucket={grainOf(data.segments_coverage)}
                selected={segmentBucket}
                onSelect={selectSegment}
              />
            )}
          </Panel>

          {/* Above the machine sections because its first panel is a list of things
              waiting on the person reading it, and a call to action belongs above a
              report (§19.4). Questions share the panel: both are "how long did a
              person take". */}
          <Panel
            title="Review and questions"
            question="How long does a person take?"
            caption={seriesCaption(data.review_coverage)}
          >
            <h3 className="mt-3 text-sm font-semibold text-dark-text">
              In review now
              {openQuestions.length > 0 && (
                <span
                  className="ml-2 font-normal text-amber-300"
                  data-testid="open-questions-count"
                >
                  · {counted(openQuestions.length, "open question")}
                </span>
              )}
            </h3>
            {inReview.length === 0 ? (
              <p className="mt-1 text-sm text-dark-muted" data-testid="in-review-empty">
                Nothing is waiting on you.
              </p>
            ) : (
              <ol className="mt-1 space-y-1" data-testid="in-review">
                {inReview.map((task) => (
                  <li key={task.task_id} className="text-sm">
                    <Link to={taskLink(task.task_id)} className="text-blue-300 hover:underline">
                      {task.title}
                    </Link>
                    <span className="text-dark-muted">{` — waiting ${waitWords(task.hours_waiting)}`}</span>
                  </li>
                ))}
              </ol>
            )}
            {openQuestions.length > 0 && (
              <ul className="mt-2 space-y-1" data-testid="open-questions">
                {openQuestions.map((question) => (
                  <li key={`${question.task_id}-${question.entry_id}`} className="text-sm">
                    <Link to={taskLink(question.task_id)} className="text-blue-300 hover:underline">
                      {question.task_id}
                    </Link>
                    <span className="text-dark-muted">{` — unanswered for ${waitWords(question.hours_open)}`}</span>
                  </li>
                ))}
              </ul>
            )}
            <h3 className="mt-4 text-sm font-semibold text-dark-text">How long they waited</h3>
            {review.length === 0 ? (
              <NoSeries caption={seriesCaption(data.review_coverage)} />
            ) : (
              <ReviewChart
                points={review}
                bucket={grainOf(data.review_coverage)}
                selected={reviewBucket}
                onSelect={selectReview}
              />
            )}
          </Panel>

          <Panel
            title="Finishes and gates"
            question="Is the delivery mechanism getting better or worse?"
            caption={seriesCaption(data.finishes_coverage)}
          >
            {finishes.length === 0 ? (
              <NoSeries caption={seriesCaption(data.finishes_coverage)} />
            ) : (
              <>
                <FinishChart
                  points={finishes}
                  bucket={grainOf(data.finishes_coverage)}
                  selected={finishBucket}
                  onSelect={selectFinish}
                />
                <h3 className="mt-4 text-sm font-semibold text-dark-text">How long they took</h3>
                {seriesCaption(data.gates_coverage) && (
                  <p className="text-xs text-amber-300" data-testid="gates-caption">
                    {seriesCaption(data.gates_coverage)}
                  </p>
                )}
                <DurationChart
                  finishes={finishes}
                  gates={gates}
                  bucket={grainOf(data.finishes_coverage)}
                  selected={durationBucket}
                  onSelect={selectDuration}
                />
                <h3 className="mt-4 text-sm font-semibold text-dark-text">
                  How good the landing estimate was
                </h3>
                {estimates.length === 0 ? (
                  <NoSeries caption={seriesCaption(data.estimates_coverage)} />
                ) : (
                  <EstimateAccuracyChart
                    points={estimates}
                    state={data.estimator}
                    bucket={grainOf(data.estimates_coverage)}
                    selected={estimateBucket}
                    onSelect={selectEstimate}
                    onReset={onResetEstimator}
                    resetting={resettingEstimator}
                  />
                )}
              </>
            )}
          </Panel>

          <Panel
            title="Runs"
            question="How much machine went into it?"
            caption={seriesCaption(data.runs_coverage)}
          >
            {runs.length === 0 ? (
              <NoSeries caption={seriesCaption(data.runs_coverage)} />
            ) : (
              <>
                <RunsChart
                  points={runs}
                  machine={machine}
                  bucket={grainOf(data.runs_coverage)}
                  selected={runBucket}
                  onSelect={selectRun}
                />
                <h3 className="mt-4 text-sm font-semibold text-dark-text">How they ended</h3>
                <RunOutcomeChart
                  points={runs}
                  bucket={grainOf(data.runs_coverage)}
                  selected={outcomeBucket}
                  onSelect={selectOutcome}
                />
              </>
            )}
          </Panel>

          <Panel
            title="Cost per completed task"
            question="What did one finished task take?"
            caption={seriesCaption(data.cost_coverage)}
          >
            {cost.length === 0 ? (
              <NoSeries caption={seriesCaption(data.cost_coverage)} />
            ) : (
              <CostPerTaskPanel
                points={cost}
                bucket={grainOf(data.cost_coverage)}
                selected={costBucket}
                onSelect={selectCost}
              />
            )}
          </Panel>

          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="Aging" question="Is anything aging badly?">
              <AgingChart buckets={aging} />
              <p className="mt-1 text-xs text-dark-muted">
                Open tasks only; archived tasks excluded.
              </p>
            </Panel>

            <Panel title="The ten oldest open tasks">
              {oldest.length === 0 ? (
                <p className="mt-2 text-sm text-dark-muted">No open tasks.</p>
              ) : (
                <ol className="mt-2 space-y-1" data-testid="oldest-tasks">
                  {oldest.map((task) => (
                    <li key={task.task_id} className="text-sm">
                      <Link to={taskLink(task.task_id)} className="text-blue-300 hover:underline">
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

          {/* §19.3: banded by who is being waited on, not by count. The queue is last
              and is named as the queue -- a backlog waiting its turn is not stuck, and
              sorting by count made "29 ready, unclaimed" the headline of this panel. */}
          <Panel title="Stuck" question="Where is work stuck?">
            {stuck.length === 0 ? (
              <p className="mt-2 text-sm text-dark-muted">Nothing is held open.</p>
            ) : (
              <div className="mt-2 space-y-3" data-testid="stuck-groups">
                {(["human", "blocked", "agent", "queue"] as StuckBand[]).map((band) => {
                  const rows = stuck.filter((row) => row.band === band);
                  if (rows.length === 0) return null;
                  return (
                    <div key={band} data-testid={`stuck-band-${band}`}>
                      <h3 className="text-sm font-semibold text-dark-text">
                        {STUCK_BAND_LABEL[band]}
                        {band === "queue" && (
                          <span className="ml-2 font-normal text-dark-muted">
                            ready, unclaimed — the backlog waiting its turn
                          </span>
                        )}
                      </h3>
                      <ul className="mt-1 space-y-1">
                        {rows.map((row) => (
                          <li
                            key={`${row.group.ball}/${row.group.ball_reason}`}
                            className="text-sm text-dark-text"
                          >
                            {stuckRowPhrase(row)}
                            {" · "}
                            <Link
                              to={taskLink(row.group.oldest_task_id)}
                              className="text-blue-300 hover:underline"
                            >
                              {row.group.oldest_task_id}
                            </Link>
                            <span className="text-dark-muted">{` · mean ${days(row.group.mean_days_held)}`}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  );
                })}
              </div>
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
          this is the one line saying what everything above it is allowed to claim.
          §19.4: one line per source family, because five baselines cannot honestly
          share one sentence. */}
      <footer
        role="region"
        className="rounded-2xl border border-dark-border bg-dark-bg/40 p-3 text-xs text-dark-muted"
        aria-label="What this page can claim"
        data-testid="coverage-footer"
      >
        <p>
          {data.coverage.note ?? "This project has no recorded history yet."}
          {` Buckets are ${data.range.bucket}s in ${data.range.timezone}, and the page is measured ${deltaWindow(data.range, data.coverage)}.`}
        </p>
        <ul className="mt-1 space-y-0.5" data-testid="coverage-sources">
          <CoverageLine label="Task history" coverage={data.segments_coverage} />
          <CoverageLine label="Finishes" coverage={data.finishes_coverage} />
          <CoverageLine label="Gates" coverage={data.gates_coverage} />
          <CoverageLine label="Runs" coverage={data.runs_coverage} />
          <CoverageLine label="Execution journal" coverage={data.machine_coverage} />
        </ul>
        {clamp && (
          <p className="mt-1 text-amber-300" data-testid="clamp-warning">
            {clamp}
          </p>
        )}
      </footer>
    </div>
  );
}
