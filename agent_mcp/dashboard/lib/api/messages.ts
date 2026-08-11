// Messages resource module — the `Message` row type, the paginated
// listing reader, and the conversation-thread reader.
//
// NOTE: the per-message MUTATIONS (send / mark-read / delete) live in
// `components/dashboard/messages/messages-api.ts`, not here — this
// module carries the shared `Message` shape plus the two READ paths:
// `getMessages()` (the paginated list) and `getMessageThread()`.

import { apiClient } from './instance'
import { apiUrl } from '../urls'

// A message row returned by POST /api/messages/query.
// v5.0.22: subject (root-only) + parent_message_id (NULL for roots,
// reply→root.message_id for replies). Canonical home for the shape
// shared by messages-dashboard, its row/modal components, and the
// messages mobile list.
export interface Message {
  message_id: string
  sender_id: string
  recipient_id: string
  message_content: string
  message_type: string
  priority: string
  timestamp: string
  delivered: number | boolean
  read: number | boolean
  // Subject display value: a real (sender-chosen or model-generated)
  // subject, OR — when `subject_is_placeholder` is true — a computed
  // 50-char preview of the body standing in for a not-yet-set subject.
  subject: string | null
  // True when `subject` is an auto-generated preview, not a real subject
  // (the backend stores NULL until Phase 2's backfill titles it; the
  // read path returns a preview + this flag). Absent/false = real subject
  // or a reply (which carries no subject). See backend Phase 1/2.
  subject_is_placeholder?: boolean
  parent_message_id: string | null
}

// One page of the messages list, as returned by POST /api/messages/query:
// the `messages` slice for the requested `limit`/`offset` window plus the
// SERVER-computed `total` (a separate COUNT over the same filter set —
// NOT `messages.length`, which is only the current page).
export interface MessagesPage {
  messages: Message[]
  total: number
}

// W6-followup F3: the paginated messages-list read. Previously the
// listing fetch was hand-built inside the generic `usePagedQuery` hook
// (POST body assembled from `{limit, offset, ...filters}`); this lifts it
// into the api layer so the TanStack query hook (`lib/queries/messages.ts`
// `useMessagesQuery`) can call it the same way `useTasksQuery` calls
// `getTasks()`.
//
// Server-side pagination: `POST /messages/query` slices by `limit`/`offset`
// (backend default 50, hard cap 500) and returns a separate `total`. The
// `filters` object is spread into the body verbatim (matching the retired
// hook's `buildPostBody`); `limit`/`offset` are only sent when supplied so
// the backend defaults still apply. Routed through `apiClient.request` so
// it inherits the cookie auth, strict-v1 media type, 30s timeout, and the
// `NO_SERVER_CONNECTED` throw the query hook swallows into an empty page.
export async function getMessages(
  filters: object = {},
  limit?: number,
  offset?: number,
): Promise<MessagesPage> {
  const body: Record<string, unknown> = {}
  if (typeof limit === 'number') body.limit = limit
  if (typeof offset === 'number') body.offset = offset
  Object.assign(body, filters)
  const data = await apiClient.request<{ messages?: unknown; total?: unknown }>(
    '/messages/query',
    { method: 'POST', body: JSON.stringify(body) },
  )
  const messages = Array.isArray(data?.messages)
    ? (data.messages as Message[])
    : []
  const total = typeof data?.total === 'number' ? data.total : messages.length
  return { messages, total }
}

// Feature 1 (message-threads-ui): fetch the WHOLE conversation a message
// belongs to — the root plus every reply transitively descending from it,
// ordered oldest-first (root first). Backs the conversation view in
// <ViewMessageModal>. Hits GET /api/{project}/messages/{id}/thread through
// the same cookie-auth + strict-v1-media-type wrapper the other message
// calls use; returns data.thread. When ``projectName`` is empty (the
// standalone single-tenant deploy) it falls back to the ApiClient's
// configured API root so both deployment shapes resolve correctly.
export async function getMessageThread(
  projectName: string,
  messageId: string,
): Promise<Message[]> {
  const rest = `messages/${encodeURIComponent(messageId)}/thread`
  const url = projectName
    ? apiUrl(projectName, rest)
    : `${apiClient.getServerUrl()}/${rest}`
  const res = await fetch(url, {
    method: 'GET',
    headers: {
      // PR-A: REST endpoints require the strict v1 media type.
      Accept: 'application/vnd.agent-mcp.v1+json',
    },
    credentials: 'include',
  })
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(txt || `HTTP ${res.status}`)
  }
  const data = await res.json()
  return Array.isArray(data?.thread) ? (data.thread as Message[]) : []
}
