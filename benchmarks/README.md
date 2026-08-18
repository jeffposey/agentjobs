# Benchmark baselines

Machine-readable reports from `scripts/bench.py`, kept so that later work can produce
a before/after pair without first checking out old code and re-measuring it.

```bash
poetry run python scripts/bench.py --json after.json --compare benchmarks/baseline-2026-08-17-real.json
```

## What is here

| File | What it captured |
| --- | --- |
| `baseline-2026-08-17-real.json` | The state of the product before any of the task-130 performance work. Real corpus, 119 files, 1,195,490 bytes. |
| `after-2026-08-17-real.json` | The same machine after task-132, 133 and 135 merged. Dashboard 48x, task detail 26x, click-to-rendered 36x. |

## Two cautions

**Wall-clock numbers in these files are machine-specific.** They were recorded on the
development machine described in the report header. Comparing your own run against
them tells you about your machine as much as about the code. The `parses` figures do
not have this problem — they are counts of work done, and a change in them is a real
change.

**A baseline is only comparable to a run over the same corpus.** Each report states
its corpus file count and total bytes, and `--compare` prints a warning when the two
do not match. The real backlog grows, so a baseline taken against `--corpus real`
drifts out of comparability over time; use `--corpus synthetic --tasks N` when a
number needs to stay meaningful for longer.
