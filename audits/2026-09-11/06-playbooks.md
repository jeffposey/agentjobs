# 06 — Playbooks

Big Dawg Audit II, night of 2026-09-11. Auditor 06. Scope: `src/agentjobs/playbooks/`,
the four files in `playbooks/`, `docs/playbooks-design.md`, `scripts/playbook_sandbox.py`,
the MCP `playbooks_list` tool, and the roadmap generator the roadmap playbook feeds.

**Method.** Read every file in scope plus the consumers of the playbook pointer in
`dispatch/guards.py`, `dispatch/runner.py`, `api/routes/playbooks.py`, `cli.py`,
`capabilities.py` and `docs/authorization.md`. Ran the five playbook test files and the
two roadmap test files (160 passed, 11.8 s). Read the live store read-only: the REST
`search` and `queue` routes on 8876, and the SQLite file opened with `mode=ro`. Ran
`scripts/export_roadmap.py --check`. Ran one probe against a throwaway store built with
`scripts/sandbox_store.py`. Did **not** dispatch anything, run the sandbox server, or
write to any store.

**Backlog already on record for this system:** task-406 (dispatched runs cannot execute
groom or reorder), task-412 (roadmap playbook's commit has no lifecycle), task-413
(roadmap is a flat printout), task-299 (run flesh-out from the task page), task-300
(playbook triggers), task-405 (a hand-made groom run task, still `ready`). Every finding
below is tagged against those.

**One-line verdict.** The storage-and-contract half (frontmatter validation, name
hygiene, pointer hashing, read-only MCP, refusal-before-creation) is sound and well
tested. The execution half has a hole the design did not anticipate: since task-332 a
dispatched run holds its verbs only against its own task, so three of the four playbooks
cannot perform their executable set, and nothing in the 160 tests would notice.

---

## F1 — P1 — A dispatched playbook run is refused at its first real write. Confirms task-406, and extends it to `roadmap` and to flesh-out's duplicate fold

**Inferred from reading**, with the mechanism verified at each link; no dispatched run
was executed by me. Auditor 03 (§8 of `03-authorization.md`) ran it: a run credential
for task-001 got `403 wrong_task` on `queue-move` and `claim` against task-002, so the
refusal itself is **verified by running**, by a neighbour. Auditor 14 confirms task-406
from the corpus side. What is new in this section is the extension to `roadmap` and to
flesh-out's duplicate fold, and the observation that the design's premise is gone.

**Evidence.**

- `src/agentjobs/capabilities.py:124-130` — `OWN_TASK_ONLY = {TASK_EDIT, TASK_VERB,
  TASK_QUEUE}`: a `run` principal holds edit, the task verbs (close among them) and queue
  moves only against the task it was dispatched to.
- `docs/authorization.md:37-41` — the same in the capability table; `wrong_task` 403 at
  line 104.
- The MCP mutation tools are HTTP clients: `src/agentjobs/mcp/mutation_tools.py:22` and
  `mcp/routing.py:22` import `TaskClient`; `src/agentjobs/client.py:26-42`
  (`run_credential_headers`) puts the run's credential on every request when
  `AGENTJOBS_RUN_CREDENTIAL` is set, which `dispatch/session_env.py:142-143` and the
  runner do for every dispatched session. So the tools a playbook brief tells the run to
  use resolve as the run, and are scoped.
- `src/agentjobs/playbooks/run.py:262-282` — a project-target run is dispatched at the
  run task it just created. That is the run's one permitted task.

**Consequence per playbook** (frontmatter `verbs:` in each file):

| Playbook | Executable set | Capability | Under a run credential |
|---|---|---|---|
| groom | `close` other tasks as duplicate/superseded | `task.verb`, own task only | refused `wrong_task` (task-406) |
| reorder | `queue_move` other tasks | `task.queue`, own task only | refused (task-406) |
| roadmap | `update` titles and `spec.summary` of other tasks (`playbooks/roadmap.md:6`, §2) | `task.edit` = `PATCH /tasks/{id}`, own task only | **refused — not on task-406** |
| flesh-out, plain shape | `update`, `promote`, `handoff` on the target | own task | works |
| flesh-out, duplicate fold (`playbooks/flesh-out.md:80-89`, and the gate text at lines 9-15) | `update_content` on the **counterpart**, then `handoff` the counterpart at `human/review` | other task | **refused — task-406 says flesh-out is unaffected; that is true only of the plain shape** |

**What the tests would have caught: nothing.** `grep -c wrong_task` over
`tests/test_playbooks.py`, `test_playbook_run.py`, `test_playbooks_api.py`,
`test_playbooks_cli.py`, `test_mcp_playbook_tools.py` is 0 in every file. The run tests
stop at "a run task was created and a dispatch entry written"
(`test_it_creates_a_ready_run_task_and_dispatches_it`,
`test_the_dispatch_entry_names_the_playbook_and_its_hash`). No test dispatches a playbook
and then performs, as the run, the first write of its executable set.

**Live evidence that the path has never worked since task-332.** The two real playbook
runs in the corpus (task-290 groom, task-291 reorder, both 2026-08-23) predate task-331
and task-332 (September). task-405, the groom run task created 2026-09-11, was never
dispatched; its own spec explains it was carried out inside an interactive session
instead. task-406's description records the actual `wrong_task` refusals that session met
when it tried to claim and log a run task it did not own.

**The design's premise is now false, not merely unenforced.** `docs/playbooks-design.md`
§6.2 (line 594) rests the whole "audited, not enforced" position on *"no per-run identity
exists to hang one on"*. Task-331 created one. The document never mentions task-331,
task-332, credentials or principals (`grep` returns nothing). Decision P5's reopen
condition — build enforcement when a run is first observed acting outside its contract —
has been overtaken: the per-run identity arrived from the authorization side and is
**narrower** than every playbook contract, so the real state is "enforced, and the
enforcement forbids the playbooks".

**The fix, or the question.** Task-406 already frames the four candidates and asks for a
decision entry; this audit adds two requirements to whichever wins:

1. Widen task-406's scope to name `roadmap` and flesh-out's duplicate fold, or its
   acceptance criterion 2 will be satisfied by a fix that leaves two playbooks broken.
2. Add the missing test: dispatch each playbook against a throwaway store with a minted
   run credential (the machinery in `tests/test_authorization.py` and
   `scripts/playbook_sandbox.py` is most of it) and perform the first write of its
   executable set. That is the test that turns "the playbook is runnable" from prose
   into a regression check.
3. Revise §6.2 and P5 so the document stops resting on a premise the code has removed.

---

## F2 — P2 — The gates are prose. The design says so honestly; the UI and the MCP tool description say something stronger. New

**Verified by reading, and by the existing tests.**

- `src/agentjobs/playbooks/model.py:79-84` — `PlaybookGate` is two strings, `before` and
  `what`. Nothing reads `contract.gates` except the listing surfaces: `grep -rn "gates"
  src/agentjobs` hits `model.py`, `api/routes/playbooks.py`, `mcp/playbook_tools.py` and
  nothing in `run.py`, `dispatch/guards.py` or `dispatch/runner.py`.
- `src/agentjobs/dispatch/guards.py:732-739` — the pointer carried into dispatch is
  documented as inert and `tests/test_playbook_run.py::test_the_pointer_is_inert_in_the_guard_chain`
  proves no guard reads it. The gate never reaches the guard chain at all.
- `docs/playbooks-design.md:591-614` (§6.2) states this plainly and well.
- **But** `frontend/src/components/Playbooks.tsx:144` renders each gate as
  **"Stops before close:"** — a declarative sentence a reader takes as a property of the
  system; and `src/agentjobs/mcp/playbook_tools.py:107` tells every agent the playbooks
  are *"each declaring what a run may do and where it must stop for a human"*.
  `PlaybookGate`'s own docstring (`model.py:80`) says "Where a run must stop".

**What stops a groom run closing something outside the approved list, other than the
instruction not to?** Nothing in this package. Today the answer is accidentally
"task-332 refuses every close" (F1); once F1 is resolved by any route that lets the run
close other tasks, the answer is again nothing. The design's argument that an unapproved
close is *detectable* by set comparison (§6.2 point 2) is true, but nothing performs the
comparison — see F3.

**Fix.** Either wording or mechanism, and the choice belongs with task-406's decision:

- Wording: "Human gate declared before `close`" on the page, and "declares the human
  gates its brief asks the run to observe" in the tool description. Cheap, and it makes
  the surfaces as honest as §6.2.
- Mechanism: now that a run has an identity (F1), the "playbook-run capability" task-406
  lists as candidate 1 can be made to carry the gates too — a run whose playbook declares
  `before: close` is granted `close` on foreign tasks only after a human log entry on the
  run task. That is the design's own deferred option, with its precondition now met.

---

## F3 — P2 — The verb list is a spelling check, and the "audit from the record" has no auditor. New

**Verified by reading.**

- `src/agentjobs/playbooks/model.py:37-56` — `MANAGER_VERBS` with the docstring
  *"Spelling only"*; `_verb_findings` at line 224 refuses a verb not in the tuple and does
  nothing else. It is a typo check on nine words.
- §6.2 point 3 and P5 say the contract is *"audited from the record"*. There is no
  auditor: no CLI command, script, test or report compares a run task's log entry types
  against its playbook's `verbs:`. `grep -rn "verbs" src/agentjobs scripts tests` finds
  only the model, the listing surfaces and the frontmatter tests.
- The verb vocabulary does not match the log's. `verbs:` speaks `queue_move`, `update`,
  `log`; the log records `type: transition`, `note`, `progress`, `handoff`
  (`models_v2.py`, `MANAGER_WRITTEN_LOG_TYPES`). A set comparison would first need a
  mapping, and none is written down.
- `roadmap` declares `verbs: [update, log, handoff, close]` and its actual deliverable is
  a `git commit` of a regenerated file — an act outside any manager verb, which is why
  the contract cannot describe it and task-412 exists.

**Fix.** A read-only `agentjobs playbook audit <run-task>` (or a `scripts/` report) that
takes the run task, resolves the playbook it points at, and prints the log entry types
and the tasks they touched beside the declared verbs. It makes the design's claim true
for the cost of an afternoon and is the cheapest "what would this have caught" answer in
this file. Write the verb-to-log-type mapping into `model.py` next to `MANAGER_VERBS`.

---

## F4 — P2 — The roadmap leak scan misses a parent's title that the relations line publishes. New. **Verified by running**

- `src/agentjobs/roadmap.py:109-117` — `published_fields` returns the title and summary,
  with a docstring saying it exists *"so leaks and render cannot drift about what is
  published"*.
- `roadmap.py:213` — `render` scans `leaks(published(tasks))`: only the ready/active
  tasks, and only those two fields.
- `roadmap.py:218` and `:166` — `render` builds `titles` from **every** task, closed and
  draft included, and `_relations` publishes `titles.get(task.parent)` under each child.

They have drifted. Probe (`leak_probe.py`, throwaway store via `scripts/sandbox_store.py`):
a parent titled `Umbrella for the work owner@example.com asked for`, closed; a ready
child with `parent=task-001`. Output:

```
parent closed: True
RENDERED WITHOUT LEAK ERROR. Lines containing the email:
     *part of task-001 (Umbrella for the work owner@example.com asked for)*
```

The same holds for a draft parent (counted, not listed, not scanned, title published).

**Fix.** Scan the rendered string rather than a field list — `leaks` over
`render`'s output before returning it — or add the parent titles to the scanned set.
The first is one line and cannot drift again. Add the probe above as a test in
`tests/test_roadmap.py`.

---

## F5 — P2 — No run in the live store is distinguishable as a playbook run, and a run that crosses a human gate loses its pointer. New. **Verified by running**

The design (§4.3, `run.py`, `runner.py:1911-1912`) pins `playbook` and `playbook_hash`
on the dispatch entry. Read-only query of the live database:

```
task_run rows with playbook: 0 of 195
log_entry data_json mentioning playbook_hash: 0   (dispatch entries: 195)
```

The only traces of the two real runs are prose: task-290 and task-291 carry the pointer
sentence in `spec.description` and the file in `spec.context[]` (written by
`_run_task_payload`), and their tags. Their **only** dispatch log entry is the one Jeff
Posey made from the task page after approving the groom proposal (task-290 entry 7,
`data` keys: agent, argv, caused_by, cwd, git_head, mode, posture, run_id, runner,
selection, session_id, trigger — no playbook), and that dispatch's prompt does not
contain the word "playbook" at all. The session that wrote the proposal left no dispatch
entry, so it was not a dispatch.

Two consequences:

1. **The audit-trail claim in §4.1 point 3 and §6.2 point 3 has never been exercised.**
   The record "names the playbook and pins its hash" only if the run was started by
   `playbook run` *and* never re-dispatched. Every groom run is re-dispatched by design:
   the gate hands off to a human and the human's Approve re-dispatches from the task page.
   The second half of a groom — the half that closes things — runs with no pointer, no
   hash and no brief line in its prompt.
2. **A hand-made run task is indistinguishable from a real one.** task-405 has the tags
   and the title the frontmatter would produce, no pointer, no context entry, and was
   never dispatched. Nothing on any surface says which of task-290, task-291 and task-405
   went through `run_playbook`.

**Fix.** Put the playbook on the **task**, not only on one dispatch entry: a
`spec.context[]` entry is already there, so `dispatch_task` can re-derive the pointer
(re-read the file, re-hash) whenever it dispatches a task whose context names a playbook,
and the prompt line comes back with it. Alternatively a first-class field on the task
record. Either way the second dispatch of task-290 should have carried
`playbook: groom` and a hash, and today it cannot.

---

## F6 — P3 — The roadmap playbook's finish. Confirms task-412, with two additions

**Verified by running** the check; the rest inferred from reading.

- `poetry run python scripts/export_roadmap.py ROADMAP.md --check` on `main` tonight:
  *"ROADMAP.md is stale; run ... and commit the result."* Expected — auditors filed tasks
  tonight — but it means the roadmap playbook's acceptance criterion 4 (*"the roadmap
  stage of scripts/check.py passes against the working tree"*) is a claim that decays
  within hours of being made true. It is an acceptance criterion that cannot stay
  satisfied; task-407 is the same fact seen from the finish.
- `roadmap` is not a shipped reference: `tests/test_playbooks.py:232-233` pins
  `reference_names() == ["flesh-out", "groom", "reorder"]`. Correct — the brief names
  this repository's `scripts/export_roadmap.py` and `ROADMAP.md` — but nothing in
  `playbooks/roadmap.md` or the design says it is repository-local by intent, and a
  reader of §3.1 ("references ship in the package") will look for it in `playbook init`.
- task-412's diagnosis stands: `playbooks/roadmap.md` §6 ends at "Commit the result" and
  the playbook is the only one whose work product is a tracked file.

**Fix.** Resolve task-412 first; then reword acceptance 4 to a fact that stays true
("the generator was run after the last record edit and its output committed"), and add
one sentence to `roadmap.md` saying it is not shipped and why.

---

## F7 — P3 — The published roadmap names the owner by first name in eight entries, which the playbook's own §3 says the run must catch. New. **Verified by running**

`grep -n -i "jeff" ROADMAP.md` on `main`: lines 233, 249, 319, 335, 449, 543, 563 (and
"Jeff's actual phone, tablet and desktop" at 249). 121 entries in the file. The mechanical
floor (`roadmap.py:79-91`, `PRIVATE_MARKERS`) is deliberately narrow and does not scan
for names, which is right; `playbooks/roadmap.md` §3 assigns exactly this to the run
(*"a person's name where a role would do ... are all yours to catch"*). The one roadmap
pass so far (task-392, 2026-09-11) left them in.

This is an editorial call for the owner — the GitHub account is public and carries the
name — and the repository's own paraphrase rule (ALLAGENTS.md) argues for "the owner".
Reported at P3 because the playbook says it is a defect and the file says it happened.

**Fix.** Cheap mechanical assist that stays narrow: the project config already lists its
human actors by display name; scan published fields for those names and report (not
refuse), the way `agentjobs quotations` reports. Then a roadmap run.

---

## F8 — P3 — "Human-gated on every surface" is a flag on the CLI surface. New; likely known to the authorization design

**Verified by reading.** `src/agentjobs/cli.py:3300-3375` — `playbook run --actor <id>`
creates the run task attributed to that id; `run.py:285-292` checks only that the id is a
configured human (`assert_authorizer_is_human`), and that creation entry is then what the
human-clocked rule reads. The CLI speaks no HTTP and resolves as the person at the
machine (`docs/authorization.md`, "an uncredentialed run is still the owner"), so any
process with a shell — a dispatched agent included — can type the owner's name and start
a playbook run, which is a dispatch that spends money. The docstring is candid that the
flag is a self-declaration; task-405's spec shows the norm holding (an agent declined to
invent it). Decision P10's "running a playbook is human-gated on every surface" is
therefore true of the browser and the MCP surface, and a convention on the CLI.

Not new to playbooks — it is the CLI's trust model — but the playbook run is the one CLI
verb that creates *and* dispatches in one step with no stored human entry required
beforehand. Question for auditors 03 and 04 below.

---

## F9 — P4 — File-era language survives in the briefs; one named verb does not exist where the brief says. New

- `playbooks/groom.md:25` "No task file was deleted", `:45` "never by parsing the YAML
  files yourself", `:188` "Never delete a task file"; `playbooks/flesh-out.md:239` "Never
  edit the YAML file". Since 2026-09-07 there is no file. The design has a header note
  covering itself (`docs/playbooks-design.md:11-13`); the briefs, which a run actually
  reads, do not.
- `playbooks/reorder.md:146` "Never call `queue keep`". There is no `queue keep` CLI
  command and no MCP tool; the verb exists only as REST `POST /tasks/{id}/queue-keep`
  (`api/routes/status.py:495`, `client.py:1099`). Harmless as a prohibition, but a run
  told not to call a thing it cannot find will wonder what it missed. Say "the REST
  queue-keep route the browser's Keep button uses".
- The anchor mechanism the reorder brief rests on **does exist and is live**: the queue
  route returns `last_move` on 43 of 150 entries tonight, 3 of them `anchor: strong`.
  Not a finding; recorded so nobody re-checks it.

---

## F10 — P4 — The shipped-reference parity test compares bodies only. New

`tests/test_playbooks.py:436-441` asserts `read_playbook(...).body == load_reference(name).body`.
Frontmatter — the gates, verbs, acceptance criteria a run task is created from — is not
compared, so the shipped `groom.md` could ship a weaker gate than the project's copy and
stay green. Tonight the three pairs are byte-identical (`diff` after CR stripping). Fix:
compare `source` (the model already carries it, `model.py:118-127`).

---

## F11 — P4 — The audit runbook names a CLI command that does not exist

The shared preamble tells every auditor to search the backlog with `agentjobs search`.
`agentjobs --help` lists no such command; search exists as REST `GET /search?q=` and the
MCP `tasks_search` tool. Observation for the synthesis session, since fourteen auditors
were pointed at it.

---

## What is sound, in one line each

- Frontmatter validation is strict, reports every finding at once, refuses unknown keys
  and misspelt verbs, and 42 tests cover it (`tests/test_playbooks.py`).
- A playbook name cannot be a path (`library.py:35`, traversal tests on CLI and API).
- The pointer is inert in the guard chain, tested; a playbook cannot widen what executes.
- Dispatch-off refuses before a run task is created; a busy machine names the run task it
  left (`run.py`, `PlaybookDispatchRefused`).
- MCP exposes one read-only tool and no run tool, and the package `__init__` does not
  import `run` (tested).
- `install_references` never overwrites; the sandbox script builds a whole throwaway
  world including a fake runner (read, not run).

## What I did not get to

- I did not start `scripts/playbook_sandbox.py` or dispatch a playbook under a minted run
  credential, so F1 is a chain of verified links rather than an observed 403. The
  missing test described in F1 is exactly the experiment I did not run.
- I did not read `playbooks/flesh-out.md` or `playbooks/reorder.md` end to end; I read
  their frontmatter and the sections the findings cite. The anchor-ageing rules in
  reorder §6.2 and flesh-out's `record_can_brief` reference (which does exist,
  `guards.py:354`) were not audited against code.
- I did not open the Playbooks page in a browser; F2's UI wording is from the source.
- I did not audit `tests/test_export_roadmap.py`'s clone-without-a-database path, nor
  task-413's projection complaints.
- I did not read task-299 or task-300 beyond their titles.

## Questions for other auditors

- **03 (authorization):** is "any shell can name a configured human on a CLI verb"
  (F8) an accepted boundary of the design, and is it written down anywhere other than
  "the CLI speaks no HTTP"? `playbook run --actor` is the one CLI path that turns that
  self-declaration into a dispatch in a single command.
- **04 (dispatch/finish):** when a task is re-dispatched from the task page after a
  handoff, does anything carry forward from the previous dispatch entry? F5 says the
  playbook pointer does not; I did not check whether posture or group do either.
- **05 (gate receipts):** `ROADMAP.md` is stale on `main` right now, so an unqualified
  gate on a clean `main` is red at the `roadmap` stage tonight. Does the receipt ledger
  record that as a red for the commit, and does task-407's finish problem also bite a
  plain `check.py` on `main`?
- **07 (schema):** is there any schema-level home for "this task is a playbook run"
  (F5)? A tag and a context entry are all that exists today.
- **14 (corpus/process):** the eight first-name mentions in the public `ROADMAP.md` (F7)
  are yours to weigh against the paraphrase rule; I have reported the fact, not the
  policy.
