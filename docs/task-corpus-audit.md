# AgentJobs task corpus audit

Audit date: 2026-08-13

This report records the repository-wide task audit performed by
[task 103](https://github.com/jeffposey/agentjobs/blob/main/tasks/agentjobs/task-103-agentjobs-task-corpus-audit.yaml). The audit
started with 76 YAML records under `tasks/agentjobs/`, inspected every record, and
created three explicit follow-ups. The review snapshot therefore contains 79 records:
51 closed, 22 ready, five draft, and task 103 active.

## Checks performed

- Loaded every YAML record through the schema-v2 model rather than sampling files.
- Accounted for lifecycle, ball, outcome, acceptance, deliverable, branch, parent,
  dependency, context-path, and log state.
- Checked every `parent` and dependency target against the complete id set; none
  dangle.
- Checked every concrete repository-relative `spec.context` path; none are missing.
- Reviewed all open summaries, descriptions, acceptance criteria, deliverables, and
  context pointers for pre-React UI instructions.
- Reviewed closed records with pending acceptance for later evidence or an explicit
  reason the criterion remains pending.
- Reviewed the exceptional open task with a merged branch and confirmed that task 075
  intentionally shipped its documentation layer while retaining dispatch work.
- Ran the task-corpus tests and the complete repository gate after reconciliation.

## Records retired or reconciled

### Superseded open work

- **Task 054** is closed `superseded`. Its remaining work was a new Jinja inbox and
  Jinja smoke coverage. React tasks 087 and 089 delivered the durable schema-v2
  dashboard and task-detail surfaces, so claiming 054 would rebuild legacy UI work.
- **Task 058** is closed `superseded`. Task 061 delivered project selection, scoped
  routes, same-id isolation, and scoped review actions; React tasks 088 and 089
  delivered the hierarchy and current review surface. Its acceptance and deliverable
  state now records that later evidence.

No completed historical task was deleted. The temporary `task-002` browser-creation
record had already been deleted under its explicit test-only instruction in task 090.
Tasks 031 and 032 remain archived because their records identify why the earlier plans
ended; the other completed and superseded records remain readable history.

### Current-state corrections

- **Task 063** now says schema v2 is live in the API, Python client, and React UI;
  task 053 is its only remaining child. Its Jinja GUI child is labelled superseded.
- **Task 075** now describes its genuinely unfinished scope: task-specific worktrees
  for dispatched agents. The shipped interactive-agent convention and the main-only
  task-record rule are recorded as complete; the obsolete GUI branch-banner proposal
  is replaced by the actual main-only solution.
- **Task 082** now presents the packaged React app as the primary interface and labels
  its original Jinja-to-React migration plan as history. Shipped epic acceptance is
  reconciled; physical-device evidence stays pending.
- **Task 098** now marks browser creation, the real-server end-to-end test, universal
  wheel packaging, offline-local assets, and React-first documentation complete.
  Only this corpus audit remains at the handoff snapshot.
- **Tasks 065, 066, 067, 073, and 092** now point implementers to the current React
  components and shared Python dashboard logic rather than Jinja templates or the
  retired route-level renderer.

### Later evidence applied to closed records

- Task 051's deferred corpus-write criterion is met by task 052's atomic migration
  and switchover.
- Task 056's dependency/build-policy criterion is met by its recorded human review
  and checked-in generated-artifact decision.
- Task 059's deferred project-scoped review-action criterion is met by task 061.

Pending criteria on superseded tasks 038, 041, and 054 remain pending deliberately:
their abandoned scope was not performed. Physical-device criteria on tasks 093 and
096 also remain pending deliberately; desktop emulation and automated PWA contracts
are not represented as real-device evidence.

## Follow-up tasks created

- [Task 104](https://github.com/jeffposey/agentjobs/blob/main/tasks/agentjobs/task-104-react-readme-screenshots.yaml) stages realistic
  data and restores at least one polished current React screenshot to the README. A
  second phone/tablet view is allowed only when it communicates something distinct.
- [Task 105](https://github.com/jeffposey/agentjobs/blob/main/tasks/agentjobs/task-105-next-task-id-all-slugged-records.yaml) fixes
  automatic id allocation. The current parser ignores normal `task-NNN-slug` files,
  which is why the browser created `task-002` in a corpus already above task 100.
- [Task 106](https://github.com/jeffposey/agentjobs/blob/main/tasks/agentjobs/task-106-physical-device-pwa-verification.yaml) collects
  the intentionally pending real-phone/tablet installation, private HTTPS, upgrade,
  suspend/resume, and second-device documentation checks.

The duplicate historical numeric prefixes for task 080 and task 081 are preserved.
Their full ids are unique and widely referenced; renaming them would create more risk
than value. Task 105 makes future allocation use the maximum leading number without
rewriting history.

## Intentionally preserved history

Completed task specs and logs retain contemporaneous Jinja, HTML, migration, and
pre-React statements when they explain what was true or constrained at the time. The
audit does not rewrite those records into present tense. The current open corpus was
separately scanned, and no open task now describes Jinja/HTML as AgentJobs' primary UI.

Task 060 remains closed while its dispatch implementation children remain open. That
is intentional: task 060 is the completed design decision, while tasks 068 through
078 and task 080-dispatch-model-profiles are the implementation and follow-up work.
Likewise, task 075's merged branch records its completed documentation layer while the
task remains ready for the dispatch layer.

## Open inventory at handoff

Draft, awaiting real human design choices:

`task-001`, `task-066-multiple-human-users`, `task-067-attachments-on-feedback`,
`task-092-dashboard-design-pass`, `task-101-merge-review-policy`.

Ready:

`task-053-schema-v2-cli`, `task-063-schema-v2`, `task-065-report-issue-action`,
`task-068-dispatch-config`, `task-069-dispatch-log-entries`,
`task-070-dispatch-runner`, `task-071-dispatch-api-guards`,
`task-072-dispatch-ledger-cancel`, `task-073-dispatch-gui`,
`task-074-auto-dispatch`, `task-075-agent-worktree-isolation`,
`task-076-dispatch-permission-posture`, `task-077-dispatch-session-launcher`,
`task-078-agent-loops`, `task-080-dispatch-model-profiles`,
`task-081-task-selection-ranking`, `task-082-react-frontend`,
`task-098-react-phase-4-ship`, `task-100-dependency-cycle-deadlock`,
`task-104-react-readme-screenshots`, `task-105-next-task-id-all-slugged-records`,
`task-106-physical-device-pwa-verification`.

Task 103 is the sole active record at this snapshot. Tasks 063, 082, and 098 are
umbrella records and are not independently claimable while their open child work
remains. After task 103 is approved and merged, task 098 and then task 082 can be
closed with their recorded physical-device limitation linked to task 106.
