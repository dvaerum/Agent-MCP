"use client"

/**
 * TanStack Query hook for the messages list (`POST /messages/query`).
 *
 * W6-followup increment F3 (2026-08-11): messages-dashboard used to
 * fetch via the hand-rolled `usePagedQuery` hook (a bespoke
 * data+total+loading+error+refresh state machine that POSTed
 * `{limit, offset, ...filters}` to `/messages/query`, plus a 60s
 * `setInterval` background poll and a `mcp:resources-updated` window
 * listener). This module moves that onto the shared `queryClient`,
 * mirroring the tasks migration (`lib/queries/tasks.ts`): one query per
 * `['messages', project, {filters, limit, offset}]`, one SSE
 * invalidation choke point (`invalidateMessages()` in
 * `lib/query-client.ts`, called from the debounced dispatcher in
 * `lib/mcp-notifications.ts`), and the same PF-3 poll-gating on SSE
 * health.
 *
 * Pagination differs from tasks. `GET /tasks` returns the whole set, so
 * tasks slice client-side and page stays OUT of the key. The messages
 * endpoint is genuinely SERVER-paginated — `POST /messages/query` takes
 * `limit`/`offset` (backend default 50, hard cap 500 rows/request) and
 * returns a separate `total` COUNT — so page CAN'T be an in-memory slice
 * (an inbox past 500 rows can't be fetched whole). `offset`/`limit` are
 * therefore part of the key, and `placeholderData: keepPreviousData`
 * holds the current page on screen while the next one loads (the old
 * hook kept its rows during a refetch — this preserves that feel).
 */

import {
  keepPreviousData,
  useQuery,
  type UseQueryResult,
} from "@tanstack/react-query"
import { getMessages, type MessagesPage } from "../api"
import { projectContext } from "../project-context"
import { messagesQueryKey } from "../query-client"
import { useServerStore } from "../stores/server-store"
import { useSseHealthy } from "../stores/data-store"

/**
 * The messages-list background poll interval (ms). Safety net BEHIND the
 * live-update SSE stream — suppressed while SSE is healthy (PF-3), see
 * the `refetchInterval` gating below. 60s matches the interval the
 * retired `REFRESH_INTERVAL` `setInterval` in messages-dashboard used.
 */
const AUTO_REFRESH_INTERVAL_MS = 60_000

/** True while an active, connected server is selected. */
function useIsConnected(): boolean {
  return useServerStore((s) => {
    const active = s.servers.find((x) => x.id === s.activeServerId)
    return !!s.activeServerId && active?.status === "connected"
  })
}

/**
 * The messages-list query for the given filter snapshot + page window.
 *
 * Gating (identical to `useTasksQuery`):
 *   - `enabled` on server connection: `POST /messages/query` would fail
 *     with no server; the pre-migration `usePagedQuery` short-circuited
 *     to an empty result when disconnected. The `enabled` gate is the
 *     direct equivalent — the query simply doesn't run until connected.
 *   - `refetchInterval` gated on `sseHealthy` (PF-3): while the operator
 *     events stream is up it pushes every mutation within ~300ms (via
 *     `invalidateMessages()`), so the interval poll is redundant load and
 *     is suppressed; when the stream is down it falls back to a 60s tick.
 *
 * The `NO_SERVER_CONNECTED` catch preserves the pre-migration quirk: a
 * transient disconnect returns an empty page rather than painting the
 * full-page error panel (connection state is owned elsewhere).
 */
export function useMessagesQuery(
  filters: object,
  limit: number,
  offset: number,
): UseQueryResult<MessagesPage> {
  const enabled = useIsConnected()
  const sseHealthy = useSseHealthy()
  return useQuery({
    queryKey: messagesQueryKey(projectContext.projectName, {
      filters,
      limit,
      offset,
    }),
    queryFn: async () => {
      try {
        return await getMessages(filters, limit, offset)
      } catch (err) {
        if (err instanceof Error && err.message === "NO_SERVER_CONNECTED") {
          return { messages: [], total: 0 }
        }
        throw err
      }
    },
    enabled,
    // Hold the current page's rows while the next page (or a filter
    // change) resolves — the old usePagedQuery kept `data` during an
    // in-flight fetch, so a page step never flashed empty.
    placeholderData: keepPreviousData,
    refetchInterval: sseHealthy ? false : AUTO_REFRESH_INTERVAL_MS,
    // Don't keep polling a backgrounded tab — the SSE stream closes on
    // tab-hide anyway, and the poll re-arms when the tab returns.
    refetchIntervalInBackground: false,
  })
}
