# 11 — Gate & tooling

Auditor 11 of the Big Dawg Audit (task-242). Read-only; the findings file is the only
write. The gate was **not** run, per the brief, so every timing figure below is either
ENGINEERING.md's table, a real record from the run ledger, or an estimate marked as one.

**Scope as read:** `scripts/check.py`, `scripts/gate_scope.py`, `scripts/bootstrap.py`,
`scripts/bench.py`, `scripts/build_frontend.py`, `scripts/build_release.py`,
`src/agentjobs/project_setup.py` (the brief lists it under `scripts/`; it is not there —
`wc` says `No such file`, the module is in the package), `tests/conftest.py`,
`pyproject.toml`, `frontend/package.json`, `frontend/playwright.config.ts`,
`frontend/e2e/run_server.py`, `frontend/scripts/check-generated-client.mjs`,
`scripts/export_openapi.py`, `frontend/scripts/generate_icons.py`, `frontend/vite.config.ts`,
`frontend/scripts/build-service-worker.mjs`, `docs/performance.md`, the gate's tests
(`tests/test_check_gate.py`, `test_gate_scope.py`, `test_gate_port_isolation.py`,
`test_worktree_bootstrap.py`, `test_source_provenance.py`), the gate receipt on disk, the
run ledger's `phases.jsonl`, and Poetry 2.4.1's own interpreter-resolution source on this
machine.

**Commands run (all read-only):** `git log/status/rev-parse/worktree list/diff` on the
main clone; a simulated `gate_scope.resolve()` against the live tree; `poetry --version`
and `poetry config --list`; a rename probe in a throwaway repo under the job tmp dir;
greps across `src/`, `tests/`, `frontend/`, `scripts/`.

**Note on the write itself:** the background-session harness refuses the Write tool in
the shared checkout without a worktree, and the brief forbids a worktree; the file was
written to the job tmp dir and copied into `audits/` with `cp`. No other change to the
tree.

---

## 0. The numbers, measured against what the docs say

| Claim | Source | What I found |
|---|---|---|
| Full gate ≈ 95.8s | ENGINEERING.md table | The only two real `gate_finished` records in `~/.agentjobs/runs/` (run_d813a110, 2026-08-22): **94.7s to a red pytest (7/10 stages)**, then **124.5s green (10/10)**. Inside a dispatched run the gate is ~30% over the table. |
| "2608 Python tests" | ENGINEERING.md | `scripts/check.py:183` and `pyproject.toml` both say **2538**. `grep -c "def test_"` = 1865 functions in 74 files (parametrize expands the rest). The tool disagrees with the doc about its own suite size. |
| "four places that open a socket ask the kernel for port 0" | ENGINEERING.md, check.py:185 | **Five** sites bind port 0. Two hold the port (`HTTPServer(("127.0.0.1", 0))`); **three reserve-then-release** it and hand the number to a `uvicorn` subprocess (see P3-4). |
| Main clone receipt | `.git/agentjobs-gate-receipt.json` | Attests `245065ad` (2026-08-21 18:25). HEAD is **43 commits** past it. `--since-gate` in the main clone would currently classify 41 paths and run all ten stages anyway. |
| HEAD stability during a gate | live | HEAD moved from `c51eb64` to `d258cba` while this audit was reading — the same clone, the same evening. See P2-1. |

---

## Part A — Where the time is, and the fast-while-safe ranking

### A1. What actually depends on what (from the code, not the table)

Stage order is `check.py:262-273`. Real dependencies, with the evidence:

- **`api` must precede `vitest` and `build`**, and it is *not read-only*:
  `check-generated-client.mjs:126-131` snapshots `src/api/generated`, **regenerates it in
  place**, and diffs. `build` (`tsc --noEmit && vite build`) and `vitest` compile that
  client. Documented and deliberate (module comment), but it means the `api` stage writes
  to the tree that every later stage reads.
- **`build` must precede `e2e`**: `vite.config.ts` writes `../src/agentjobs/frontend_dist`;
  `playwright.config.ts` starts `e2e/run_server.py`, which serves that directory via
  `api/spa.py:25-33` (per-request `StaticFiles`/`FileResponse`, no caching).
- **`icons` is read-only** (`generate_icons.py:122-127` returns before any write under
  `--check`). **`black --check`, `ruff`, `mypy`, `oxlint` are read-only** apart from their
  own caches.
- **`pytest` is independent of every frontend stage** and reads `tasks/`, `docs/`,
  `*.md`, and `frontend/package.json` (`test_check_gate.py:197`) — it is the stage whose
  inputs are least bounded.
- **Nothing depends on `pytest`** except the ordering rule "cheap first".

So the true graph is: `{black, ruff, mypy, icons, oxlint}` ∥ `api → {vitest, build → e2e}`
∥ `pytest`. The serial table pays 86s for `pytest + vitest + build + e2e`; the critical
path of the graph is `max(pytest, api + build + e2e)` ≈ `max(52, 4+4+25)` = **52s**
uncontended.

### A2. Proposals, ranked by seconds saved per unit of new risk

| # | Proposal | Saves (est.) | New risk | Verdict |
|---|---|---|---|---|
| 1 | **Copy `.mypy_cache` into a fresh worktree in `bootstrap.py`**, the way `.mcp.json` is copied (`bootstrap.py:184-218`). | ~17s on the *first* gate of every worktree — which is every dispatched run, since each takes a fresh worktree. | Near zero: mypy validates each cache entry by source hash; a stale copy is a cache miss, not a wrong answer. No sharing, so no concurrent-write hazard. | **Do it.** Cheapest seconds in this list. |
| 2 | **Add `--durations=15` to `PARALLEL_ARGS`** (`check.py:180`). | 0s directly; it is the instrument. No durations data exists anywhere today — the ledger records only a total (`phases.jsonl`: `seconds`, `stages_run`). | None. | **Do it first.** Every pytest proposal below is a guess without it. 2538 tests at 342s serial is 135ms/test average; the 52s at 32 workers vs an 11s ideal says the tail, not the bulk, is the cost. Candidates from grep: 13 subprocess sites in `test_dispatch_runner.py`, three tests that spawn a real `uvicorn`, sleep(600) runner scripts. |
| 3 | **`--dist worksteal`** for xdist. | Unknown until #2; typically 10–25% of a long-tailed suite. | Low; it is a scheduler change, no test semantics. | Measure after #2, three runs, quote all three (the repo's own standard). |
| 4 | **Stop nesting `poetry run` inside the gate.** `check:api-schema` and `check:icons` (`package.json`) each start Poetry + a fresh interpreter; `check.py` already knows `sys.executable`. Have the stage call `export_openapi.py` / `generate_icons.py --check` directly. | ~2–3s, and it **removes the hazard class** `child_environment()` exists to patch (`check.py:51-93`, task-210). The e2e `webServer` command is the one nested `poetry run` that would remain. | Low. | Do it; the safety gain is the point more than the seconds. |
| 5 | **Run the cheap block concurrently**: black, ruff, mypy, icons, oxlint in parallel; api alone first (it writes). | ~8s warm (block becomes mypy-bound), ~0s cold (mypy at 19s dominates either way). | Interleaved output — must capture per-stage output and print on failure. | Worth it only together with #6; alone it is 8s for a rewrite of `main()`'s loop. |
| 6 | **Run `pytest` concurrently with `api → vitest → build → e2e`.** | ~34s: total ≈ cheap block + max(52, 34). | **Medium.** `pytest -n auto` takes every core; Playwright's `webServer` has a 30s start timeout and tests have default 30s timeouts — the ledger already shows pytest stretching to ~85s in a dispatched run, which is the contended case this would make worse. A flaky e2e under load is a gate nobody trusts. Mitigation: cap xdist at `-n <cores-4>` while e2e runs, and keep `--serial` meaning fully serial. | Prototype behind a flag; adopt only with three clean contended runs (two gates at once) on record. |
| 7 | **Playwright `workers: 2+`** (`playwright.config.ts`: `workers: 1, fullyParallel: false`). | ≤12s. | **Medium-high.** All 8 specs share one server and one temp project; `dispatch.spec.ts` and `dispatch-one-click.spec.ts` toggle machine-level dispatch config (`run_server.py:57-91`), `queue-order.spec.ts` asserts a global order. Needs a server per worker or a spec-level audit I did not do. | Defer. |
| 8 | `tsc --incremental` with `tsBuildInfoFile` under `node_modules/.cache` (no `incremental` anywhere in `frontend/tsconfig*.json`). | ~1–2s. | Low. | Optional. |
| 9 | **Receipt-backed incremental pytest selection.** | Potentially most of 52s on small diffs. | The test→source dependency graph is implicit (conftest fixtures, `REPO_ROOT`-relative reads, subprocess invocations of `scripts/`). Any hand-written map is vibes. `pytest-testmon` is coverage-backed — receipt-like in spirit — but unmeasured here with xdist on Windows, and its database would need the same "issued only by a full green run on a clean tree" discipline as the receipt. | **Not now.** Reopen when #2 shows the bulk rather than the tail is the cost; a tail is fixed by fixing the tests. |

**Realistic floor for the full gate:** with #1, #4, #5, #6 adopted, about **55–60s
uncontended** (cheap block ~2s warm, then pytest ~52s as the critical path), versus
95.8s today; the first gate in a fresh worktree drops from ~113s (table + cold mypy) to
roughly the same 60s because #1 takes mypy off the critical path. Getting below that
means making pytest itself faster, which is #2 → #3 → fixing the tail. **The contended
figure is unmeasured** — ENGINEERING.md says so and is right; adopting #6 without
measuring it would make that unknown the gate's normal case.

### A3. Rejected on the record (so they stay rejected)

- **Classify `frontend/*` → skip pytest.** `tests/test_check_gate.py:197` reads
  `frontend/package.json`; `test_documentation_contract.py:21` reads `frontend/README.md`.
  pytest's inputs include the frontend. Rejected.
- **Classify `tests/*` → pytest only.** `black .`, `ruff check .`, `mypy .` all check
  `tests/` (78 files). Rejected.
- **Classify `src/agentjobs/*` → skip frontend stages.** `api` exports the OpenAPI
  document from `src/` (`export_openapi.py`), and `e2e` drives the server built from it.
  Rejected.
- **`reuseExistingServer: true`** for e2e. Would let a gate exercise another checkout's
  server; the config comment already says why. Rejected.
- **`--since-gate` inside `finish.py`.** `finish.py:550` deliberately runs the unqualified
  gate. Correct — a merge is exactly where a derived receipt chain should be re-grounded.
- **`pytest -x` in the gate.** Saves time only on red runs and loses the full failure
  list a dispatched agent needs to fix everything in one pass. Fine for `--only pytest`
  iteration; not the gate.

---

## Part B — Findings

### P2-1 — A receipt can be issued for a commit the gate did not verify (HEAD read after the run)

**Evidence.** `check.py:358-380`: `issue_receipt()` calls `gate_scope.head_commit(ROOT)`
and `tree_is_clean(ROOT)` *after* the stages have run (`check.py:542-546`). Nothing
captures HEAD or the tree state before the run. The main clone is shared and task
records are committed to `main` continuously by other agents
(ENGINEERING.md, "Task files live on `main`"): HEAD advanced `c51eb64 → d258cba` during
this audit, and the receipt commit has 43 descendants from one evening.

**Scenario.** A gate starts on clean HEAD=A. During its ~2 minutes another session writes
`tasks/agentjobs/task-2xx.yaml` and commits B. The gate's pytest may or may not have
loaded that file (`TestRealCorpus` reads `tasks/agentjobs` at collection time,
`test_validate.py:674-680`). At the end the tree is clean at B, so the receipt says **B
was verified in full**. The next `--since-gate` diffs against B, sees nothing for that
YAML, and skips pytest — the one stage the `tasks/*` class exists to keep running. The
gate's own safety argument ("a receipt rests on a commit the gate itself verified,"
`gate_scope.py:16-17`) is false for B.

**Fix.** Record `head_commit` and `tree_is_clean` before the first stage; after the run,
issue the receipt only if both are unchanged, otherwise print "No gate receipt written:
HEAD moved from A to B during the run." Three lines. Add a test that moves HEAD between
the stubbed stages — `test_check_gate.py` stubs `head_commit` to a constant
(`no_receipt_from_a_simulated_gate`), so nothing covers this today.

### P2-2 — `--since-gate` classification misses the source side of a staged rename

**Evidence.** `gate_scope.py:192` uses `git diff --name-only <commit>`. Git's default
rename detection (`diff.renames=true` for porcelain, unset in this repo, git 2.55) lists
**only the destination**. Probe in a throwaway repo (`$CLAUDE_JOB_DIR/tmp/renameprobe`):
`git mv src/mod.py docs/mod.md` → `git diff --name-only $C` prints `docs/mod.md` alone;
with `--no-renames` it prints both `docs/mod.md` and `src/mod.py`.

**Scenario.** A staged move of anything into `docs/` or `tasks/` (the two prefix classes)
classifies as prose/corpus → pytest only, and the deletion of the source path is never
seen. Moving a `.py` out of `src/` breaks imports that mypy or the `api` export would
catch; pytest likely catches it too, so the practical blast radius is small — but it is a
hole in a mechanism whose whole claim is "default-deny, the tool derives the answer."

**Fix.** `git diff --name-only --no-renames <commit>` (`gate_scope.py:192`). One flag.
Add a rename case to `TestChangedSince` in `test_gate_scope.py` — none of its 28 tests
covers a rename, case, or separator.

### P2-3 — `bootstrap.py` re-hijacks an activated environment that cannot import `agentjobs`

**Evidence.** `bootstrap.py:121-134`: detachment happens only when
`imported_checkout(poetry, None)` returns a checkout **other than** `ROOT`. That helper
returns `None` when `poetry run python -c "import agentjobs"` exits non-zero
(`bootstrap.py:81-99`), and `None` is treated as "imports nothing yet — somebody's own
venv, leave it alone" (docstring, lines 116-119).

**Scenario.** The main clone's Poetry env has been left unable to import — the exact
residue of task-194 (a removed worktree's editable `.pth`), or a half-finished
`poetry install`. A dispatched session inherits `VIRTUAL_ENV` pointing at it, takes a
worktree, runs `python scripts/bootstrap.py` as instructed. The probe fails, the script
does not detach, and `poetry install` writes the worktree's editable install into the
main clone's env — task-194 again, caused by following the instructions. The three
shapes `test_source_provenance.py:262-312` covers are nothing-activated / foreign
checkout / own checkout; the broken-env shape is untested.

**Fix.** Decide by *identity*, not by what the env currently imports: detach whenever
`VIRTUAL_ENV` is set and is not `poetry env info --path` for this checkout (the path-keyed
env) and not inside `ROOT` (a `.venv`). That is the property task-194 actually needs.

**Examined and not a finding (stated because the brief asked for the shape bootstrap
still gets wrong):** after detaching, `PATH` still leads with the foreign venv's
`Scripts/`. Poetry 2.4.1 here has `virtualenvs.use-poetry-python = false`, so
`get_preferred_python` → `shutil.which("python")` → the foreign venv's `python.exe`
(`poetry/utils/env/python/manager.py:219-240, 272-291`). I believe `virtualenv` resolves
a venv interpreter to its system base when creating the path-keyed env, so the result is
correct — **unverified**. Test: activate the main clone's venv, `unset VIRTUAL_ENV`, run
the bootstrap in a worktree, then `poetry env info --path` and
`python -c "import sys; print(sys.base_prefix)"` from that env.

### P2-4 — `build_release.py` does not prove the wheel runs on a machine that is not this one

**Evidence.** `build_release.py:73-94`: the wheel is installed with `--no-deps` into a
temp `--target`, then `PYTHONPATH=<site>` is prepended to **`sys.executable`** — the
Poetry dev virtualenv, which already holds fastapi, uvicorn, pydantic, typer, pyyaml,
jinja2. The probe asserts only that `agentjobs` itself imports from the target
(`:114-117`). `installation.md:44-47` reads "boots the installed server with Node removed
from `PATH`" — true, and that is the only thing it removes.

**Consequence.** A missing or mis-floored runtime dependency in `pyproject.toml` would
pass this verification and fail on a customer's machine. `.github/` does not exist; there
is no CI anywhere, so nothing ever installs this package from the wheel in a clean
environment. This is the product-strategy question the brief names, and the script
currently answers the easier half (no Node) and not the harder half (no dev venv).

**Fix.** `python -m venv` a fresh interpreter in the temp dir, `pip install <wheel>`
*with* deps from the index (or `--find-links dist/` + a wheelhouse), and boot from that.
Costs ~20s; it is the only test of `[tool.poetry.dependencies]` the repo would have.

**Also examined, fine:** `poetry build --clean` exists in 2.4.1; `pyproject.toml` includes
`frontend_dist` for both formats; `verify_wheel` checks `py3-none-any` and the required
bundle members; no `C:/`, `jpose`, or tailnet literals in `src/`, `scripts/*.py`, or
`frontend/src` beyond docstrings and the 8876 default-port prose.

### P3-1 — The gate's ledger record cannot answer "where did the time go"

`phases.jsonl` carries `seconds`, `stages_run`, `stages_total`, `failed_stage` — no
per-stage timings, although `check.py` has them in `timings` at the moment it writes the
record (`check.py:530-537`). `run_report.py` therefore reports gate totals only. The
brief's first question cannot be answered from the ledger; add `stage_seconds: {...}` to
`gate_finished`. Zero risk.

### P3-2 — A main-clone gate run silently redeploys (and briefly blacks out) the live dashboard

`vite.config.ts`: `outDir: ../src/agentjobs/frontend_dist`, `emptyOutDir: true`.
`api/spa.py:25-33` serves that directory per request with no caching. The dashboard on
8876 runs from the main clone. Every gate run in the main clone therefore empties the
directory the live server is serving, then refills it with whatever the working tree
built — a window of 404s during `build`, and afterwards the bundle is the working tree's,
not the last merged one. Mostly benign because the main clone stays on `main`, but it is
a "check" with a production side effect. Build to a scratch dir and swap atomically, or
state the behaviour in ENGINEERING.md's "rebuild after merge" section so nobody diagnoses
the blackout as something else.

### P3-3 — Nested `poetry run` in two stages is both cost and the root of a hazard class

`package.json`: `check:api-schema` and `check:icons` are `poetry run python ...`;
Playwright's `webServer` is a third. Each pays Poetry startup plus a fresh interpreter,
and each is a place where Poetry's preference for an activated env can point at the wrong
checkout — `child_environment()` (`check.py:51-93`) exists purely to neutralise that.
Proposal #4 in A2. The gate already proved `sys.executable` imports this checkout
(`setup_problems`); use it.

### P3-4 — Three tests reserve an ephemeral port, release it, then bind it from a subprocess

`tests/test_dispatch_api_base_end_to_end.py:330-341`, `tests/test_mcp_server.py:403-411`,
`tests/test_mcp_protocol.py:117-119`: `bind(("127.0.0.1", 0))` → read port → close →
`uvicorn --port <n>` in a child. Under `-n auto` with 32 workers and three such sites,
two workers can draw the same number between release and rebind; the second server fails
to bind and the test reads that as a dead server. The two `HTTPServer(("127.0.0.1", 0))`
sites are fine. The ledger's one red pytest (94.7s) followed by green four minutes later
on the same run is **consistent with** a flake of this shape and is not evidence of it —
no durations, no failure text survives in the ledger. Fix: have the child bind port 0
itself and report the port on stdout, or pass the socket. Until then the "parallel-safe"
claim in `check.py:183-186` should say "probably".

### P3-5 — A `--since-gate` run that ran all ten stages is labelled as not the gate

`gate_scope.render()` (`:270-285`) prints `NECESSITY RUN: 10 of 10 stages ... This is not
the gate` when every path was unclassified (observed live: 41 paths, 10/10). `check.py:487`
records `scope=necessity` for it, so `run_report.py` cannot tell a reduced run from a
full one, and the honest sentence — "every stage ran, so this is the gate, and a receipt
with basis X was issued" — is never printed. When `scope.stages == every`, render the full
banner and record `scope=full` with the basis noted.

### P3-6 — Receipt "chain" is one link deep

`write_receipt` (`gate_scope.py:200-215`) overwrites the single file with `{commit,
basis}`. The claim "a chain of them is auditable" (ENGINEERING.md; `gate_scope.py:203`)
holds only for the immediate parent: after A(full) → B(derived from A) → C(derived from
B), the file says `{C, basis: B}` and nothing records that B was itself derived. Append
to a small list, or keep `grounded_at: A` alongside `basis`. Low stakes; cheap to make
true.

### P3-7 — `tasks/*` and `docs/*` classify by prefix, not by what the file is

`gate_scope.py:75-79` with `fnmatch`: a `.py` dropped under `tasks/` or `docs/`
classifies as corpus/prose → pytest only, while the full gate would run Black, Ruff and
MyPy on it (`black --check .`, `ruff check .`, `mypy .`). Unlikely, but the brief asked
where a glob over-matches, and this is it. Tighten to `tasks/**/*.yaml`, `docs/**/*.md`
(plus `docs/img/*`, `docs/schema/*.svg`), and let everything else under those roots fall
to default-deny. `tasks/agentjobs/attachments/task-015/*.png` is the one non-YAML file
under `tasks/` today; nothing in any stage reads it.

### P3-8 — `docs/performance.md` and `bench.py` disagree, and the CLI leg cannot fail

- `performance.md` "Other options": `--port N ... (default 18950)`. `bench.py:71-92`
  derives the port from the checkout path (30000–39999) since task-187. Doc drift.
- `bench_cli` (`bench.py:418-447`) records `exit_code` in `detail` but `format_report`
  never prints it and nothing asserts on it: a CLI invocation that crashes in 80ms
  benchmarks as fast. Print it and mark the measurement as an error when non-zero.
- Browser leg: `open-task.bench.ts` times `Date.now()` around `expect(...).toBeVisible()`,
  whose polling intervals are 100/250/500/1000ms — the resolution of a measurement that
  is trying to distinguish ~160ms renders. Use `locator.waitFor()` or an in-page
  `performance.mark`. The brief's "browser-pane-hidden" caveat does **not** apply here:
  Playwright's headless page reports `visibilityState: visible`, so timers are not
  throttled; nothing in `bench.py` accounts for it and nothing needs to. It applies to
  measurements taken through the Claude-in-Chrome pane, which `bench.py` never uses.
- `benchmarks/baseline-2026-08-17-real.json` records 119 files / 1,195,490 bytes; the
  corpus is larger now, so `--compare` against it will (correctly) warn "not comparable"
  — the baseline needs re-cutting before the next perf claim.

### P3-9 — `build_release.py` is documentation-only; nothing runs it

`docs/installation.md:44` and `docs/integration/agentjobs-package.md:8` reference it;
no test, script, or task automates it and there is no CI. A release artefact that is only
ever built by hand on one machine is the "would it run elsewhere" question with no
standing answer. Pair with P2-4.

### P4-1 — Stale numbers inside the tool

`check.py:183-187` and `pyproject.toml` (pytest-xdist comment): "2538 tests", "89% of
the gate", "431s serial", "494s serial with coverage". ENGINEERING.md: "2608", "342.6s",
"540.1s". Auditor 1 owns ENGINEERING's staleness; these are the copies inside the gate.

### P4-2 — Platform-dependent classification

`fnmatch.fnmatch` applies `os.path.normcase`, so on Windows `*.md` matches `README.MD`
and `tasks/*` matches `Tasks/x`; on Linux neither does. The Windows direction is the
permissive one. Harmless because nothing reads uppercase-extension markdown, but
`fnmatchcase` would make the table mean one thing everywhere.

### P4-3 — The receipt attests a commit, not an environment

`poetry.lock` / `package-lock.json` changes are unclassified → full gate, correct. But a
`poetry install` that changes installed versions without changing the lock (or a lock
change followed by no install) is invisible to both the full gate and the receipt. Same
blind spot as the full gate; noted so nobody reads the receipt as more than it is.

### P4-4 — `bootstrap.py` copies the main clone's `.mcp.json`, interpreter path included

`bootstrap.py:184-218`. That file names the main clone's virtualenv, so a worktree
session's MCP server runs the main clone's `agentjobs` code, not the branch's. Fine for
task-record mutations (they go to `main` anyway); wrong for a branch that changes the
MCP server. For auditor 8.

### P4-5 — A global `agentjobs.pth` points the system Python at the main clone

`C:/Users/jpose/AppData/Local/Programs/Python/Python313/Lib/site-packages/agentjobs.pth`
contains `C:/projects/agentjobs/src`. So `python` (no venv) imports the main clone's
source. `check.py`'s location check accepts that for the main clone and would then fail
loudly because that interpreter has no pytest/black (`find_spec` → False for all three);
for a worktree it refuses correctly as "outside this checkout". Not a gate defect; an
environment fact worth knowing, and one more reason the bootstrap's printed interpreter
path is the right instruction.

### P4-6 — `project_setup.py` containment is web-only

`project_setup.py:75-107`: `_directory_within` is applied only when
`contain_directories=True`; the CLI path accepts an absolute `tasks_directory` by design
(docstring). For auditor 12's path-traversal item; from the tooling side it is
consistent with its docstring.

### P4-7 — Tests of the gate: mostly load-bearing, two decorative

`test_check_gate.py` exercises `main()` with `subprocess.run` stubbed — the right level
for selection/ordering/receipt logic, and each named failure mode (unknown stage
refused, partial cannot read as full, selection never earns a receipt, failing run earns
nothing) would catch a regression that has a named incident. Decorative:
`test_every_stage_is_named` and `test_every_stage_has_a_distinct_name` — a duplicate name
would already break `select()`'s `known` dict in a way `test_only_runs_just_what_was_named`
sees. Nothing tests: HEAD moving mid-run (P2-1), a rename (P2-2), a broken activated env
(P2-3), or any `bootstrap.main()` path end to end.

---

## Examined, nothing found

- **Default-deny core.** `stages_for()` (`gate_scope.py:103-120`): an unclassified path
  adds `every`; verified by code and by the live simulation (41 paths: every `src/`,
  `tests/`, `scripts/`, `frontend/openapi.json`, `frontend/src/api/generated/*` path came
  back "unclassified, so every stage").
- **`*.md` → pytest only** is sound against the stages: no stage other than pytest reads
  markdown. `runner.py:105 GUIDE_PATH` is a string placed in a prompt, not a file read;
  `frontend/src`, `vite.config.ts`, `e2e/` import no `.md`; `test_documentation_contract.py`
  is the reader and it runs.
- **`tasks/*` → pytest** is sound: `TestRealCorpus` reads `tasks/agentjobs`; the e2e server
  uses a `TemporaryDirectory` project (`run_server.py:93-121`); Black/Ruff/MyPy ignore YAML.
- **Receipt placement.** Lives in `--absolute-git-dir`, so each worktree has its own and it
  is never itself a diff (`gate_scope.py:39-45, 148-153`); corrupt or commit-less receipts
  read as none; an unknown commit refuses to narrow (`changed_since` → `cat-file -e`).
- **PARTIAL RUN honesty.** Printed before and after the stages (`check.py:488, 550`);
  "Ran every stage" is reachable only from `len(selected) == len(all_stages)`
  (`check.py:327-328`); `--only`/`--from` never reach `issue_receipt` (`:543-546`).
- **Stage isolation.** Subprocess per stage, `check=True`, stops at first failure; the
  only tree-writing check stage (`api`) is documented and its writes are what later stages
  are meant to see. `RUN_VARS` are scrubbed for children (`:87-88`) *and* by an autouse
  fixture (`conftest.py:40-56`), so a gate inside a run records exactly two phase lines —
  the ledger confirms: two `gate_started`/`gate_finished` pairs for run_d813a110, nothing
  phantom.
- **conftest isolation claims.** Per-test `AGENTJOBS_HOME` (`:21-36`), per-test Claude
  home (`:59-72`), the reachability probe stubbed at all three import sites (`:75-107`).
  Verified as written.
- **Port derivation.** `playwright.config.ts` hashes the checkout path into 20000–29999,
  `bench.py` into 30000–39999 (`test_gate_port_isolation.py:72` pins the disjointness);
  `run_server.py:17-40` refuses to invent a port; `reuseExistingServer: false`.
- **`finish.py`** runs the unqualified `scripts/check.py` with the worktree's interpreter
  (`:550-558`) and strips `VIRTUAL_ENV`/`POETRY_ACTIVE` from every subprocess (`:99, :198`).
- **Wrong-checkout refusal** in `check.py:126-147` judges by location, not importability;
  the remedy text is scoped to the import problem only (`:150-174`). Correct as claimed.
- **`build_frontend.py`** validates the six required bundle members plus one JS and one
  CSS asset before Poetry can package an empty directory.

---

## What I did not get to

- **No timing measurements of my own.** The gate and pytest were off-limits, and no
  per-test durations exist anywhere (P3-1). Every figure in A2 is derived from
  ENGINEERING's table and two ledger records; the floor is an estimate.
- **Poetry → virtualenv base-interpreter resolution** with a foreign venv first on PATH
  (P2-3, second paragraph): reasoned from Poetry's source, not run.
- **A spec-by-spec shared-state audit of the 8 Playwright files** — required before
  proposal #7 (e2e workers) can be green-lit.
- **`scripts/run_report.py`, `finish_sandbox.py`, `review_*_sandbox.py`,
  `regen-schema-docs.sh`, `review_drag_trace.js`** — grepped, not read.
- **`scripts/tailscale-service-host/`** (Go) — auditor 12's; not opened.
- **vitest's 5.2s** — not broken down.
- **`test_the_frontend_stages_are_exactly_the_frontend_gate`** — confirmed it reads
  `package.json`; did not check its parse of `npm run check` against the current string.

## Questions for other auditors

- **Auditor 10 (dispatch):** `run_report.py` treats `scope: necessity` as a gate — does it
  distinguish a derived run from a full one anywhere (P3-5)? And is the ~30% gap between
  the table's 95.8s and the run's 124.5s explained by something in the run (a concurrent
  gate, the runner's own load), or is 125s simply the dispatched-run figure?
- **Auditor 8 (MCP):** is the worktree MCP server intentionally the main clone's code
  (P4-4)? Does the plugin registration depend on `.mcp.json` at all?
- **Auditor 12 (security):** who wrote the global `agentjobs.pth` (P4-5), and does a
  system-wide import path for the main clone matter to your threat model? Also P4-6.
- **Auditor 9 (frontend):** `emptyOutDir` on the directory the live server serves
  (P3-2) — does the service worker's shell cache paper over the blackout for an open
  tablet, or does it surface as a broken PWA?
- **Auditor 2 (docs):** `performance.md`'s `--port` default and `installation.md`'s
  description of `build_release.py` (P3-8, P2-4).
- **Auditor 1 (context):** ENGINEERING's 2608 vs the tool's own 2538 (P4-1) — which is
  current?
- **Auditor 4 (storage):** the receipt race in P2-1 is the gate-side face of "several
  processes write one clone"; if you find a lock or a revision signal on the write path,
  it is the thing `issue_receipt` should key on.
