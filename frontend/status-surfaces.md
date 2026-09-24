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
  it. The server decides the word, including "Finishing" (`derived_display_status`), so
  no file spells a task-state word of its own.
- **A run's state** is the server's `health`, rendered only through `HealthBadge` or
  `healthLabel`. `health` is `finishing` exactly when the task read's `live_finish` would
  make the task say "Finishing", because both come from `finish_status.live_finishes`.

"Finishing" is always the finishing violet, `FINISHING_FILL` in `DependencyState.tsx`.
The check fails when that fill is spelled anywhere else.

## The surfaces

| File | What it renders | Source |
| --- | --- | --- |
| `components/DependencyState.tsx` | The task status chip, and the finishing colour | `display_status` |
| `components/TaskList.tsx` | The status column, the tree row chip, the Finishing filter | `dependencyState` |
| `components/DependencyGraph.tsx` | The chip on each node of the dependency tree | `dependencyState` |
| `components/Dashboard.tsx` | The waiting-on-you cards and the active task chips | `display_status` |
| `components/TaskDetail.tsx` | The task page's header chip, work state and children | `display_status` |
| `components/FinishPanel.tsx` | The task page's finish panel and its state badge | the finish read |
| `App.tsx` | Tells the queue's dispatch card (`QueueDispatch`) a task is finishing | `live_finish` |
| `components/LiveRuns.tsx` | The Runs tab, health words and the finish badge | `health` |
| `components/SlotBoard.tsx` | The dashboard slot board's run tiles and finish cards | `health` |
