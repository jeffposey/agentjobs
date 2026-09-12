# 05 — Gate, receipts, and the performance claims

Big Dawg Audit II, night of 2026-09-11. Auditor 05. Main clone on `main` at `096f33ae`,
read-only. Every probe below ran in the job scratch directory against a throwaway git
repository, a throwaway clone, or a throwaway virtualenv; `AGENTJOBS_HOME` was redirected
and the run variables scrubbed for every invocation. Nothing wrote to the live store, the
main clone, its virtualenv, or the server on 8876. `scripts/check.py` was not run.

The probe scripts are in the session scratch directory (`probe_receipts.py`,
`probe_bootstrap.py`, `probe_banner.py`); their output is quoted inline, since the
scratch directory does not survive the job.

## Where this system stands against the record

The first audit (2026-08-21, auditor 11) filed this system's defects as **task-267**. That
task has sat in `draft` / `human:spec` since 2026-08-22 with an empty log. Every one of
its four P2s is still true on today's code, and three of them are demonstrated below by
running rather than by reading. The synthesis should treat task-267's unspecified state
as the finding above all the others here: the defects were known, written up with fixes,
and nothing has moved for three weeks.

Of the first audit's P3/P4 items in this system: P3-1 (per-stage ledger records) is
**fixed** (task-321 — `gate_stage_finished` records exist). P3-3 (nested `poetry run`)
is **fixed** (task-268). P3-2, P3-5, P3-6, P3-7, P3-9, P4-2, P4-4 and P4-5 are **still
true** and are re-confirmed below. P4-6 is **moot**: `scripts/project_setup.py` was
deleted by task-402 (commit `1e9daa09`); the runbook brief for this auditor still lists
it.

Sound and not worth more budget: the default-deny rule itself (an unclassified path
selects all eleven stages — verified by running), receipt behaviour across a rebase
(verified by running: both sides of the rebase are seen and the run goes full), the
per-worktree receipt location (covered by `tests/test_gate_scope.py`), `gate_slots`'s
arithmetic and cleanup (slot directory empty at audit time, no corpses), and the release
wheel itself (see F12: it installs and serves from a clean virtualenv).

---

## F1 — A receipt is issued for a commit the gate never ran

**Severity** P2. **Status** Confirms task-267 (its P2-1). **Verified by running.**

**Evidence.** `scripts/check.py:954` fingerprints the tree before the stages;
`scripts/check.py:794-818` (`issue_receipt`) reads `head_commit(ROOT)` and
`tree_is_clean(ROOT)` *after* them and never compares either against what was captured.
Probe: a scratch repository, `check.main([])` with the stage commands stubbed, and a
commit landed in the checkout after the third stage:

```
5. issue_receipt() attests to a commit the gate never ran (HEAD moves mid-gate)
  HEAD when the gate started : 4530c5a4
  HEAD when the gate finished: d8cec527
  receipt written for        : d8cec527
  VERDICT: receipt names a commit the gate never verified
```

The next `--since-gate` in that checkout diffs against `d8cec527`, sees nothing for the
file that landed, and skips whatever stages it could reach. `tests/test_check_gate.py`
stubs `head_commit` to a constant (`no_receipt_from_a_simulated_gate` returns `b`\*40),
so no test can see HEAD move.

**How likely.** More likely than in August, not less: a gate is 200-290s (F14), the
sequence in ENGINEERING.md is "commit, rebase, gate", and the ledger shows agents chaining
gates and committing between them (task-336's run; task-392's run on 2026-09-11 launched
six). Any `git commit --amend` or rebase in the worktree during those minutes produces
this.

**Fix.** Capture `head_commit` and `tree_fingerprint` before stage 1; in `issue_receipt`
refuse with "No gate receipt written: HEAD moved from A to B during the run" when either
differs. Add the test task-267's ac-1 names: move HEAD between stubbed stages.

## F2 — Bootstrap installs the worktree into an activated env that cannot import `agentjobs`

**Severity** P2. **Status** Confirms task-267 (its P2-3). **Verified by running.**

**Evidence.** `scripts/bootstrap.py:124-125`: `occupant = imported_checkout(...)`;
`if occupant is None or occupant == ROOT: return None, None`. `None` — the probe import
failed — is treated as "somebody's own venv, leave it alone". Probe, from a scratch
`git clone` of the main clone, installing only into a scratch venv:

```
== A: activated env is the MAIN CLONE's (task-194 shape)
   imported_checkout -> C:\projects\agentjobs
   install_environment -> DETACHED
== B: activated env is a venv that cannot import agentjobs (task-194 residue shape)
   imported_checkout -> None
   install_environment -> INHERIT (poetry install goes into the activated env)
   running `poetry install --only main` from the clone with the inherited env ...
   emptyvenv now imports agentjobs from: ...\tmp\clone\src\agentjobs\__init__.py
   VERDICT: the activated env was written into
```

Scenario A — the exact task-194 shape — is defended correctly. Scenario B is the
task-194 *residue*: the main clone's env left unable to import after a removed worktree's
`.pth` or a half-finished install. A dispatched session inheriting that `VIRTUAL_ENV`,
taking a worktree, and running the bootstrap as instructed repoints the main clone's env
at the worktree, and nothing on either side says so. `tests/test_source_provenance.py`
covers nothing-activated / foreign / own / freshly-created; not broken.

**Fix.** Detach whenever `VIRTUAL_ENV` is set and is neither this checkout's path-keyed
env (`poetry env info --path`) nor a directory inside `ROOT`. Test the broken-env shape
(task-267 ac-3).

## F3 — `--since-gate` sees only the destination of a rename

**Severity** P2. **Status** Confirms task-267 (its P2-2). **Verified by running.**

**Evidence.** `scripts/gate_scope.py:223` runs `git diff --name-only <commit>`; git's
default rename detection lists the destination alone. Probe, `git mv src/mod.py
docs/mod.md` then commit:

```
  changed_since: ['docs/mod.md']
  resolve -> stages: ['roadmap', 'pytest']
  git diff --name-only --no-renames: ['docs/mod.md', 'src/mod.py']
```

A source file moved into `docs/` classifies as prose and the gate skips Black, Ruff,
MyPy, the build and Playwright for a change that deleted Python. `TestChangedSince` has
four tests and none is a rename.

**Fix.** `--no-renames`; a rename case in `TestChangedSince` (task-267 ac-2).

## F4 — The receipt is an unsigned two-field file, and a forged one is laundered into a derived one

**Severity** P3. **Status** New (adjacent to the first audit's P3-6 and P4-3, not the
same). **Verified by running.**

**Evidence.** `gate_scope.py:187-196` accepts any JSON object with a `commit` key.
ENGINEERING.md property 1 (line 90) says the mechanism "rests on a receipt the gate itself
wrote, not on your assessment", and nothing enforces the first half. Probe: commit an
unverified source change, write `{"commit": <HEAD>}` into `.git/agentjobs-gate-receipt.json`
by hand, run `resolve`:

```
4. a hand-written receipt is indistinguishable from the gate's
    NOTHING CHANGED since the gate verified 14e58fce.
    Running roadmap anyway: ...
  stages selected: ['roadmap']
```

That run passes `roadmap`, and `issue_receipt(basis=14e58fce)` then writes a *derived*
receipt whose `basis` is the forged commit — so the forgery now looks like a chain link.
The same laundering happens innocently after F1.

**The honest framing.** The receipt lives inside the agent's own trust domain; an agent
that would forge it would also lie in its handoff. But ENGINEERING.md sells the four
properties as things "a third party can check", and a third party cannot tell a gate-
written receipt from `echo`. Either make it checkable (record the fingerprint, the stage
list, a timestamp, and an HMAC keyed on `~/.agentjobs/front-door-secret`, which already
exists for exactly this kind of claim) or rewrite property 1 to say what it actually is.

## F5 — The receipt chain on disk is one link deep

**Severity** P3. **Status** Confirms first audit P3-6 (folded into task-267's fix list).
**Verified by running.**

**Evidence.** `gate_scope.py:305-316` overwrites the one file with `{commit, basis}`.
After the probe's derived run the file reads `{"commit": "d8cec527…", "basis": null}` —
no stages, no fingerprint, no timestamp. The "auditable chain" ENGINEERING.md line 118
promises exists only in the transcript of each run, which nothing keeps.

**Fix.** Append to a list, or keep `grounded_at` (the last full-gate commit) beside
`basis`. Cheap; belongs in the same change as F1 and F4.

## F6 — ENGINEERING.md's `--since-gate` section describes a table row and a test that no longer exist

**Severity** P3. **Status** New. **Verified by running** (classification) and by reading
(the prose).

**Evidence.**

- ENGINEERING.md:94-95: "Only task records under `tasks/` and prose map to a reduced set".
  `gate_scope.CLASSES` (`:97-101`) has three rows — `ROADMAP.md`, `docs/*`, `*.md` — and
  `tests/test_gate_scope.py:140` (`test_a_task_path_is_no_longer_claimed_by_the_table`)
  asserts the record row is gone (task-380).
- ENGINEERING.md:99-102: property 3 is justified by "`tests/test_validate.py::TestRealCorpus`
  loads this repository's own records ... which is why `tasks/` maps to `pytest`". The row
  is gone, and task-411 (filed 2026-09-11, critical) reports that every store-reading
  corpus check **skips** under pytest because the autouse registry fixture hides the
  store. The sentence is false twice.
- ENGINEERING.md:40 "ten named stages" and :96 "selects all ten"; `scripts/gate_scope.py:238`
  and `scripts/check.py:486,812` also say ten. `poetry run python scripts/check.py --list`
  on 2026-09-11 prints eleven; every `gate_finished` record since 2026-09-11 carries
  `stages_total: 11`. `roadmap` arrived and the prose was not re-counted.
  `tests/test_check_gate.py::TestWhatEngineeringMdMustStillSay` checks that every stage is
  *named* in docs/performance.md and never the count.

**Fix.** Rewrite properties 2 and 3 around what is true now (`UNBOUNDED_STAGES` and the
roadmap) and let task-409/411 settle whether pytest joins it. Assert the stage count in
the handbook test, or stop stating a number.

## F7 — Default-deny holds; two classified rows claim reasons that are not what reads them

**Severity** P3. **Status** Confirms first audit P3-7 and P4-2 (both in task-267's fix
list). **Verified by running.**

**Evidence.** `classify()` on today's table, Windows:

```
  audits/2026-09-11/05-gate-receipts.md          -> *.md ('pytest',)
  src/agentjobs/playbooks/references/groom.md    -> *.md ('pytest',)
  README.MD                                      -> *.md ('pytest',)
  docs/anything.py                               -> docs/* ('pytest',)
  frontend/src/x.md                              -> *.md ('pytest',)
  src/agentjobs/new_module.py                    -> UNCLASSIFIED (every stage)
```

- **The brief's premise about `audits/` is wrong**: it is not unclassified. It matches
  `*.md` and selects `pytest` (plus the unbounded `roadmap`). Nothing in `tests/` reads
  `audits/` (grep), so that is a wasted pytest per audit file, in the safe direction. By
  the table's own docstring rule ("add an entry only when it skips a stage measured in
  minutes") an `audits/*` → `()` row would qualify; optional.
- `*.md` claims `src/agentjobs/playbooks/references/*.md`, which are **runtime data the
  API serves** (`api/routes/playbooks.py`), not prose the documentation contract reads.
  The stage set happens to be adequate — `tests/test_playbooks.py` reads them and no
  Playwright spec asserts on their content (grep of `frontend/e2e`) — but the row's stated
  reason is false for them, and the table's docstring says each row "has to name what
  reads those paths".
- `docs/*` classifies by prefix: a `.py` under `docs/` skips Black, Ruff and MyPy. None
  exists today (`find docs -name "*.py"` is empty).
- `fnmatch` is case-insensitive on Windows: `README.MD` is prose here and unclassified on
  Linux. Permissive direction; `fnmatchcase` fixes it.

**Fix.** `docs/**/*.md` and a `*.md` row that excludes `src/`; `fnmatchcase`. Decide
whether `audits/` should map to nothing.

## F8 — A `--since-gate` run that selects every stage still says it is not the gate

**Severity** P3. **Status** Confirms first audit P3-5. **Verified by running.**

**Evidence.** `Scope.reduced` is `stages is not None`, true even when the stages are all
eleven; `check.py` then records `scope=necessity`. Probe with one unclassified path:

```
Scope.reduced -> True ; stages == every -> True
kind check.main would record -> necessity
NECESSITY RUN: 11 of 11 stages, derived from the diff against aaaaaaaa ...
This is not the gate. ...
```

`run_report.py` therefore cannot tell that run from a reduced one, and the true sentence
— every stage ran, this is the gate, a receipt with basis X was issued — is never printed.

**Fix.** When `stages == every`, print the full banner and record `scope=full`.

## F9 — `NOTHING CHANGED` skips pytest on evidence that cannot cover it — and task-411 is currently hiding the hole

**Severity** P3. **Status** Confirms task-409; cross-references task-411. **Inferred
from reading** (the two task records, `gate_scope.py:68-93`, `tests/conftest.py`).

**Evidence.** `UNBOUNDED_STAGES` is `("roadmap",)` and its docstring says whether
`pytest` belongs there "is a question this deliberately did not settle". The ledger
shows the case is real: `run_e1a79bb4` on 2026-09-11 17:14Z ran a `necessity` gate of
one stage (`roadmap`, 0.6s) and issued a receipt. Today that is *accidentally* sound,
because task-411 says the store-reading checks skip under pytest — so pytest genuinely
reads no store right now. The moment task-411 is fixed, task-409's hole opens: a record
filed with a verbatim quotation turns pytest red with no tree change, and a
`NOTHING CHANGED` receipt attests otherwise. The two tasks are already linked as related;
they should be sequenced, not merely linked — 409 before 411 or in the same change.

## F10 — `bench.py` still measures an empty store

**Severity** P3. **Status** Confirms task-408 on today's `main` (`096f33ae`). **Verified
by running.**

```
poetry run python scripts/bench.py --corpus synthetic --tasks 5 --iterations 1 --skip-browser --skip-cli   # 2026-09-11
  corpus      synthetic: 5 files, 53,869 bytes
  GET /tasks                        6.4ms   0 parses
  GET /tasks/{id}/detail            ERROR   404 Not Found ... task-001-generated-benchmark-task/detail
```

Zero parses on every surface and a 404 on the detail leg, and it prints a table rather
than failing — task-408's ac-2 in one line. ENGINEERING.md's "a speed claim is a
before/after pair from one of these tools or it is an anecdote" names a tool that
currently compares two empty stores.

## F11 — `corpus_stats.py` measures nothing and exits 0

**Severity** P3. **Status** Confirms task-396, with a changed symptom. **Verified by
running.**

```
poetry run python scripts/corpus_stats.py        # 2026-09-11, exit 0
Corpus: C:\projects\agentjobs\tasks\agentjobs
Records: 0 readable, 0 unreadable. Measured 2026-09-11.
```

task-396 says it measures the frozen copy; task-380 has since deleted that directory, so
it now measures a directory that does not exist, prints a fully-formed report of zeros,
and exits 0. That is worse than the filed symptom: a scheduled or scripted call would
never notice. Fix: read the store through `store_factory` (task-396's fix) and exit
non-zero on an empty or missing corpus.

## F12 — The release wheel is sound today; nothing in the repository would notice if it were not

**Severity** P3. **Status** Confirms task-267 (its P2-4) and first audit P3-9 for the
gap; **refutes the fear** for today's artefact. **Verified by running** (a stronger check
than the script's own).

**Evidence.** `scripts/build_release.py:66-92` installs the wheel `--no-deps` into a temp
target and probes it with `sys.executable`'s own environment — the dev venv, which already
has every runtime dependency — so a missing declared dependency would pass. Nothing runs
the script: no `.github/workflows`, no test, no doc step (grep, caches excluded). I did
not run it either — its `build()` step rewrites `src/agentjobs/frontend_dist` under the
live dashboard (F13). Instead:

```
poetry build --output <scratch>/dist                       # 993,973-byte py3-none-any wheel, 2026-09-11
python -m venv <scratch>/cleanvenv && pip install <wheel>   # with declared deps, from PyPI cache
pip check                                                   # No broken requirements found.
agentjobs serve --port 8905  (AGENTJOBS_HOME/PROJECT_ROOT in scratch)
  GET /api/version   200  source_root=...cleanvenv\Lib\site-packages\agentjobs
  GET /app/          200  817B
  GET /api/dashboard 200
  GET /api/projects  200
```

So the artefact installs and runs from a clean interpreter with only its declared
dependencies. One soft spot: `src/agentjobs/mcp/server.py:18` imports `jsonschema`
directly, and `pyproject.toml` declares it only transitively (`pip show jsonschema` →
`Required-by: mcp`). It works because the `mcp` SDK pins it; declare it.

**Fix.** Make `verify_installed_server` do what the four lines above do (task-267 ac-4),
and run it somewhere — a release checklist step at minimum.

## F13 — The gate's `build` stage rewrites the directory the live dashboard serves

**Severity** P3. **Status** Confirms task-260 (draft, "gate builds to a scratch
directory") and first audit P3-2. **Inferred from reading**, deliberately not run.

**Evidence.** `check.py` `DEPENDENCIES` docstring: "`build` writes
`src/agentjobs/frontend_dist`, which is the bundle Playwright's server serves". The
dashboard on 8876 runs from the main clone and serves that directory. An unqualified gate
in the main clone — which ENGINEERING.md's sequence invites on `main` after a merge —
empties and refills what the owner is looking at. It is why this audit timed `vitest` and
the cheap stages but not `build`. task-260 has held the fix since 2026-08-22 as a draft.

## F14 — The numbers: what is current in docs/performance.md and what is not

**Severity** P3 for the three stale sentences; the tables themselves are broadly current.
**Status** New. **Verified by running** for the cheap stages and vitest; **read from the
ledger** for pytest, e2e and whole-gate figures (the full gate was off limits).

Measured on `main` at `096f33ae`, 2026-09-11, main clone, warm caches, via `poetry run`
(adds roughly 0.7s to each):

| Stage | Command | Seconds |
|---|---|---|
| black | `python -m black --check .` | 1.5 |
| ruff | `python -m ruff check .` | 1.0 |
| mypy | `python -m mypy .` (warm cache) | 2.6 |
| icons | `scripts/generate_icons.py --check` | 2.2 |
| roadmap | `scripts/export_roadmap.py ROADMAP.md --check` ×3 | 1.5 / 1.5 / 1.5, **exit 1** |
| oxlint | `npm run lint` | 0.8 |
| vitest | `npm run test` — 44 files, 561 tests | 8.9 |

`roadmap --check` is red on `main` right now ("ROADMAP.md is stale"): tasks were filed
after the last regeneration commit. ENGINEERING.md's warning that the stage goes red for
reasons outside your branch is verified, not asserted.

The most recent full gates, from `~/.agentjobs/runs/*/phases.jsonl` and
`~/.agentjobs/finishes/*/gate.log`, all 2026-09-11 UTC, all eleven stages, all green:

| Where | Started | pytest | vitest | e2e | mypy | Whole gate |
|---|---|---|---|---|---|---|
| run_e1a79bb4 (task-392, 2nd gate) | 17:10 | 83.6 | 8.3 | 95.4 | 1.7 | 199.0 |
| fin_bfbad70b (task-392 finish) | 17:20 | 83.6 | 8.1 | 95.9 | 1.6 | 199.2 |
| run_86e7809e (task-392, 1st) | 17:35 | 121.3 | 32.3 | 96.1 | 21.1 | 286.8 |
| run_86e7809e (2nd) | 17:44 | 100.0 | 9.0 | 97.7 | 15.5 | 232.6 |
| fin_07bfcf61 (task-380 finish) | 23:10 | 122.5 | 32.1 | 98.3 | 15.6 | 281.4 |

Verdicts on the document:

- **The 2026-09-06 column (245.8 / 254.3s) is still representative.** Dispatched gates
  land at 199-287s. The fresh-worktree mypy cost it describes (~14s for the first two runs)
  is exactly what the ledger shows (14.9 / 21.1 / 15.5s); the 32s first-run vitest is too.
- **`e2e` is now faster than documented**: 95-98s today against 135-139s on 2026-09-06.
  task-369 ("139s of a 246s gate") should carry that re-measurement before anyone works
  it; the gate is closer to being bounded by pytest again (122s vs 98s in two of five).
- **Line 534, "The gate is now about a minute"**, contradicts the same document's own
  table (~250s) and today's ledger. Stale since task-268.
- **Line 584, "376s for all ten"**, and every "ten" in the file predate `roadmap`.
- **Line 287, "four of the eight parse every tracked task file under `tasks/`"**: that
  directory was retired by task-380 and, per task-411, the store-reading checks skip.
- **Lines 111-118 already say the bench figures are untrustworthy** (task-408). Good.
- **The `gate_slots.py:66` docstring** cites 581s as the longest gate on record; the
  ledger holds 785.5s (a run) and 862.1s (a finish gate). Still under the 30-minute stale
  ceiling, so the conclusion stands; the number does not.

**Did task-339's "one gate per handoff" land?** Re-measured with the repository's own
tool:

```
poetry run python scripts/run_report.py --since 4        # 2026-09-11
  gate runs             32 across 9 instrumented runs
  gates launched /run   3.6            (all-time: 4.7, from the unfiltered report)
poetry run python scripts/run_report.py --since 6 --list
  run_cac5fcff  task-402  2026-09-10   6 gates (11.6m, 4 failed)
  run_e1a79bb4  task-392  2026-09-11   6 gates (9.2m, 2 failed)
  run_3be05a07  task-371  2026-09-08   7 gates (20.2m, 2 failed)
  run_1bcb7154  task-230  2026-09-07  14 gates (32.2m, 6 failed)
```

Down from the 6-9 task-339 measured, not at one. The prose, `ALREADY GREEN` and the
receipt-naming all shipped on 2026-09-07 and the runs after it still launch three to seven
gates. That is a measurement the document should carry beside the task-339 table, and a
question for whoever owns dispatch prompts: the sequence is in ENGINEERING.md, and the
agents are not following it.

## F15 — Small things, P4

- **Runbook drift.** This auditor's brief lists `scripts/project_setup.py`; it was deleted
  by task-402 (`1e9daa09`). The first audit's P4-6 about it is moot.
- **System Python still imports the main clone.** `…\Python313\Lib\site-packages\agentjobs.pth`
  exists and `python -c "import agentjobs"` from that interpreter resolves to
  `C:\projects\agentjobs\src`. First audit P4-5, still true; environment fact, not a gate
  defect. Verified by listing and running.
- **`bootstrap.py` copies `.mcp.json` verbatim**, interpreter path included
  (`copy_mcp_config`), so a worktree's MCP server runs the main clone's code. First audit
  P4-4, still true by reading; task-319 would retire the file.
- **`dirty_paths` renders a rename as one path** (`'docs/mod.md -> docs/renamed.md'`,
  verified by running). Cosmetic; it is only printed.
- **`tree_fingerprint` hashes `git diff HEAD`**, which for a binary file is the constant
  "Binary files differ", so two different binary edits fingerprint the same. By reading;
  no binary source in the tree today.

---

## What I did not get to

- **The full gate**, by instruction. pytest and e2e figures above are the ledger's, not
  mine, and they were measured under whatever contention those runs had.
- **task-267's open question**: after bootstrap detaches, `PATH` still leads with the
  foreign venv's `Scripts/`; with `virtualenvs.use-poetry-python = false` Poetry may build
  the path-keyed env on that interpreter. Not run — it needs a `poetry install` that
  creates a new environment, and I did not want to leave one behind.
- **A receipt whose commit has been pruned.** My rebase probe kept the receipt commit
  reachable, so the `cat-file -e` refusal path was not exercised; it is covered by
  `test_a_receipt_for_a_commit_git_has_never_heard_of_narrows_nothing`.
- **`build_release.py` itself** (F13 explains why) and `bench.py --corpus real`.
- **`run_report.py`'s arithmetic.** I ran it and read its output; I did not check its
  sums against the ledger by hand.
- **Three-gate contention** and the `--concurrent` runner: nothing was measured, and
  docs/performance.md already says the three-gate case is unmeasured.
- **`scripts/context_eval.py`**: not in the brief, not opened.

## Questions for other auditors

- **Tests auditor**: task-411 says every store-reading corpus check skips. Does
  `tests/test_validate.py::TestRealCorpus` skip too (the task says "worth checking")? If
  so, F6's property-3 sentence and `gate_scope.py`'s `UNBOUNDED_STAGES` docstring are both
  describing a test that has been silent for weeks.
- **Frontend / PWA auditor**: task-260 (gate builds to a scratch directory) is a draft
  from 2026-08-22. Is the blackout-during-`build` observable from the browser today, and
  has the service worker made it better or worse?
- **Dispatch / finish auditor**: the scripted finish's gate writes a receipt into a
  worktree it then removes (harmless, I think) and always runs the unqualified gate
  (`finish.py:928`), so receipts only ever matter to interactive agents. Is anything in
  dispatch reading them? And who owns the fact that runs still launch 3-7 gates after
  task-339 (F14)?
- **Documentation auditor**: ENGINEERING.md lines 40, 90-102 and 118 are the sentences I
  would overturn; docs/performance.md lines 287, 534 and 584. I have not checked
  `docs/agent-workflow.md` for copies of the same claims.
- **MCP auditor**: `jsonschema` is a direct import in `mcp/server.py` and an undeclared
  dependency (F12).
- **Storage auditor**: `corpus_stats.py`'s default corpus path (F11) is one of what I
  suspect are several scripts still defaulting to the retired `tasks/` directory; I only
  checked the ones in my brief.
