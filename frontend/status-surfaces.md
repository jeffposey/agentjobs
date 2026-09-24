# Where a task's state is rendered

Every React file under `src/` that shows a person what state a task or a run is in, and the one
source each is allowed to draw that word from. `frontend/src/test/statusSurfaces.test.ts`
reads this table: a file that renders state without a row here fails it, and so does a
row whose file no longer renders state.

**Why this exists.** Task-509 fixed "Finishing" on five surfaces by listing them, and
missed the slot board's run tile, which renders a run's health and not a task status at
all. The owner then reported the same defect again (task-533). A list somebody has to
remember to update is how the sixth surface was missed, so the list is now checked.

## The two agreed sources

- **A task's state** is the server's `display_status`, or `dependencyState()` built on
  it. The server decides the word, including "Landing" (`models_v2.task_status`), so
  no file spells a task-state word of its own. "Landing" was "Finishing" until task-578;
  the check refuses either spelling in a component.
- **A run's state** is the server's `health`, rendered only through `HealthBadge` or
  `healthLabel`. `health` is `finishing` exactly when the task read's `live_finish` would
  make the task say "Landing", because both come from `finish_status.live_finishes`.

## The one colour source

**Every status chip's word and colour is in `src/agentjobs/status_vocabulary.json`**
(task-562). The server reads it for `display_status` and `status_category`; the React app
reads the same file, in `StatusChip.tsx`, for the colours. One colour per category, every
status in exactly one category, and grey for closed tasks and nothing else. The design
table is in `docs/task-schema.md`. Render a task status with `<StatusChip>` or
`categoryStyle()`; never pick a colour for one.

**Icons come from the same file** (task-578): an entry's optional `icon` is a Lucide name,
drawn by `StatusChip` (or `ChipIcon`, in the three badges that build their own span)
from the registry in `statusIcons.ts`. An entry without one draws the chip exactly as
before icons existed.

The run-health words that name a task status, and the epic-walk badges, come from the
same file (`RUN_HEALTH`, `WALK_STATES`); the process-only health states keep their own
colours in `LiveRuns.tsx`.

The check fails when a component defines its own `Record<StatusCategory, string>` colour
map, and when a word the chip used to be rewritten to ("Actionable now", "In flight",
"Waiting on sub-tasks") appears anywhere in the source.

## The surfaces

| File | What it renders | Source |
| --- | --- | --- |
| `components/StatusChip.tsx` | The one status chip, drawn from the status data file | `display_status` |
| `components/DependencyState.tsx` | The task status chip and its reason line | `display_status` |
| `components/RecentlyFinished.tsx` | The dashboard's Recently finished rows | `display_status` |
| `components/TaskList.tsx` | The status column, the tree row chip, the Landing filter | `dependencyState` |
| `components/DependencyGraph.tsx` | The chip on each node of the dependency tree | `dependencyState` |
| `components/Dashboard.tsx` | The waiting-on-you cards and the active task chips | `display_status` |
| `components/TaskDetail.tsx` | The task page's header chip, work state and children | `display_status` |
| `components/FinishPanel.tsx` | The task page's finish panel and its state badge | the finish read |
| `App.tsx` | Tells the queue's dispatch card (`QueueDispatch`) a task is finishing | `live_finish` |
| `components/LiveRuns.tsx` | The Runs tab, health words and the finish badge | `health` |
| `components/SlotBoard.tsx` | The dashboard slot board's run tiles and finish cards | `health` |
