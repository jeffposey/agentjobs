import { QueryClient } from "@tanstack/react-query";

import { NORMAL_POLL_MS } from "./components/LiveUpdates";

/**
 * How long a cached response is reused before a mount will refetch it.
 *
 * Twice the revision poll interval. The poll is what actually keeps this app
 * current -- it notices when a task file changes and invalidates the task-backed
 * queries, which catches writes from other agents, the CLI and git alike. Refetching
 * on every component mount adds no correctness on top of that, only load.
 *
 * Not `Infinity`: deriving this from the poll interval keeps staleness bounded if the
 * poll itself stops, so a page left open behind a dead poller eventually refreshes on
 * navigation instead of showing yesterday's backlog forever.
 */
export const CACHE_STALE_MS = 2 * NORMAL_POLL_MS;

/**
 * The app's query client.
 *
 * Extracted from main.tsx so the caching policy can be asserted in tests. TanStack
 * Query defaults to `staleTime: 0`, which meant every navigation refetched data it
 * already had: returning to the task list refetched the list and the broken-file
 * list, and the detail request for whichever row was clicked next queued behind them.
 * Measured on the real 119-file corpus, that turned a 138 ms task detail into 403 ms
 * -- the request was fine, it was waiting in line behind refetches nobody needed.
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: CACHE_STALE_MS,
      },
    },
  });
}
