// Messages resource module — the `Message` row type and the
// conversation-thread reader.
//
// NOTE: the per-message CRUD (send / query / mark-read) lives in
// `components/dashboard/messages/messages-api.ts`, not here — this
// module carries only the shared `Message` shape and the thread reader
// used by <ViewMessageModal>.

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
