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
  it. The server decides the word, including "Finishing" (`models_v2.task_status`), so
  no file spells a task-state word of its own.
- **A run's state** is the server's `health`, rendered only through `HealthBadge` or
  `healthLabel`. `health` is `finishing` exactly when the task read's `live_finish` would
  make the task say "Finishing", because both come from `finish_status.live_finishes`.

## The one colour source

**Every status chip's colour is `CATEGORY_CLASSES` in `StatusChip.tsx`**, keyed by the
server's `status_category`, which is derived beside `display_status` by the same function
(task-562). One colour per category, every status in exactly one category, and grey for
closed tasks and nothing else. The design table is in `docs/task-schema.md`. Render a
task status with `<StatusChip>` or `statusChipClasses()`; never pick a colour for one.

A run-health state that names the same thing as a task status takes that status's
category in `LiveRuns.tsx` (`HEALTH_CATEGORY`); the process-only states keep their own.

The check fails when the finishing fill is spelled outside `StatusChip.tsx`, and when a
word the chip used to be rewritten to ("Actionable now", "In flight", "Waiting on
sub-tasks") appears anywhere in the source.

## The surfaces

| File | What it renders | Source |
| --- | --- | --- |
| `components/StatusChip.tsx` | The one status chip and the category-to-colour map | `display_status` |
| `components/DependencyState.tsx` | The task status chip and its reason line | `display_status` |
| `components/RecentlyFinished.tsx` | The dashboard's Recently finished rows | `display_status` |
| `components/TaskList.tsx` | The status column, the tree row chip, the Finishing filter | `dependencyState` |
| `components/DependencyGraph.tsx` | The chip on each node of the dependency tree | `dependencyState` |
| `components/Dashboard.tsx` | The waiting-on-you cards and the active task chips | `display_status` |
| `components/TaskDetail.tsx` | The task page's header chip, work state and children | `display_status` |
| `components/FinishPanel.tsx` | The task page's finish panel and its state badge | the finish read |
| `App.tsx` | Tells the queue's dispatch card (`QueueDispatch`) a task is finishing | `live_finish` |
| `components/LiveRuns.tsx` | The Runs tab, health words and the finish badge | `health` |
| `components/SlotBoard.tsx` | The dashboard slot board's run tiles and finish cards | `health` |
