"use client"

import { apiClient } from "@/lib/api"

// Message-type + priority option lists — shared by the compose form and
// the filter bar so the two dropdowns can never drift apart.
export const MESSAGE_TYPES = [
  "text",
  "system",
  "notification",
  "task_update",
  "assistance_request",
]
export const PRIORITIES = ["low", "normal", "high", "urgent"]

// Sentinel values for Select dropdowns (Radix Select cannot use ""
// as an item value). "__all" clears the filter, "__broadcast" picks
// the broadcast recipient (maps to recipient_id="*" on the backend).
export const ALL = "__all"
export const BROADCAST = "__broadcast"

// Wave 2 (cleanup-wave-2): the ``adminToken()`` helper is gone.
// Dashboard mutations authenticate via the operator session cookie
// set on /agent-mcp/login — the browser attaches it to every fetch
// automatically (the apiClient helper and this ``callMessages`` helper
// both opt into ``credentials: 'include'``).
//
// Helper to call /api/messages* under cookie auth.
// Listing uses POST /api/messages/query because browsers strip bodies
// from GET requests per the Fetch spec (this was the original bug).
// Compose stays POST /api/messages; mark-read stays PATCH
// /api/messages/<id>; delete is DELETE /api/messages/<id>.
//
// Wave 2 (cleanup-wave-2): ``credentials: "include"`` ensures the
// ``agent_mcp_session`` cookie travels with the request even on the
// dashboard's cross-origin-but-same-site dev URLs; the request body
// no longer carries a bearer token, so missing the cookie would
// surface as the backend's 401 login_required envelope.
export async function callMessages(
  method: "POST" | "PATCH" | "DELETE",
  pathSuffix: string,
  body: Record<string, unknown>,
): Promise<unknown> {
  const base = apiClient.getServerUrl()
  const res = await fetch(`${base}/messages${pathSuffix}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      // PR-A: REST endpoints require the strict v1 media type.
      "Accept": "application/vnd.agent-mcp.v1+json",
    },
    body: JSON.stringify(body),
    credentials: "include",
  })
  if (!res.ok) {
    const txt = await res.text().catch(() => "")
    throw new Error(txt || `HTTP ${res.status}`)
  }
  return res.json()
}
