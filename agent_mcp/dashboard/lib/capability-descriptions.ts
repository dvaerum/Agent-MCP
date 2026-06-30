/**
 * Single source of UI truth for capability descriptions.
 *
 * Wave 9 PR 5 (prancy-napping-pie) — every capability string the
 * dashboard surfaces (in the per-group "Capabilities" section under
 * the groups dashboard) needs a one-line human description so the
 * sysadmin understands what they're ticking. The canonical cap
 * vocabulary lives in ``agent_mcp/core/capabilities.py`` —
 * ``KNOWN_CAPABILITIES`` — and that file is the source of truth.
 * This registry is the dashboard-side projection.
 *
 * Completeness is enforced at build time by
 * ``agent_mcp/dashboard/tests/capability-descriptions-complete.test.ts``:
 *  * every member of ``KNOWN_CAPABILITIES`` must have a description;
 *  * every key in this registry must be a member of
 *    ``KNOWN_CAPABILITIES``.
 *
 * The dashboard UI groups capabilities by their first dotted segment
 * (``agents.*``, ``tasks.*``, ``memories.*``, etc.); the resource
 * label used in the collapsible section header is sourced from
 * :data:`CAPABILITY_RESOURCE_LABELS` below.
 */

/** Stable resource ordering — controls the order of collapsible
 *  sections in the dashboard. Sections render top-to-bottom in this
 *  order; capabilities whose first dotted segment isn't listed here
 *  fall to the end alphabetically (defensive — should never happen
 *  for a member of KNOWN_CAPABILITIES). */
export const CAPABILITY_RESOURCE_ORDER: readonly string[] = [
  "mcp",
  "agents",
  "tasks",
  "memories",
  "messages",
  "files",
  "coordination",
  "rag",
  "system",
]

/** Human-friendly resource name used in collapsible section headers. */
export const CAPABILITY_RESOURCE_LABELS: Readonly<Record<string, string>> = {
  mcp: "MCP transport",
  agents: "Agents",
  tasks: "Tasks",
  memories: "Memories",
  messages: "Messages",
  files: "Files",
  coordination: "Coordination",
  rag: "RAG",
  system: "System management",
}

/** One-line description per cap string. Keys MUST match the
 *  ``KNOWN_CAPABILITIES`` frozenset in ``core/capabilities.py``
 *  exactly; the build-time test fails the dashboard build if a
 *  description is missing or orphan. */
export const CAPABILITY_DESCRIPTIONS: Readonly<Record<string, string>> = {
  // MCP transport
  "mcp.connect": "Open the MCP wire — fundamental gate for every agent call.",

  // Agents
  "agents.view": "Read the agents roster and per-agent metadata.",
  "agents.register": "Register a new agent (mint token, claim slot).",
  "agents.terminate": "Terminate an agent (soft-delete; restorable until purge).",
  "agents.use": "Act as an agent on the MCP wire (worker-tier baseline).",

  // Tasks
  "tasks.view": "Read tasks (own + assigned). Required to render the task list.",
  "tasks.create": "Create new tasks under the project.",
  "tasks.update": "Update a task's status/notes (own tasks for workers; any task with assign).",
  "tasks.delete": "Delete tasks (operator-tier within the project).",
  "tasks.assign": "Assign tasks to other agents (manager-tier supervision).",

  // Memories
  "memories.view": "Read project memories / context entries.",
  "memories.create": "Add new memory entries.",
  "memories.update": "Edit any memory entry (vs. own only).",
  "memories.delete": "Delete memory entries.",

  // Messages
  "messages.view": "Read inter-agent messages.",
  "messages.send": "Send messages to other agents (or broadcast).",

  // Files / coordination / RAG
  "files.use": "Read project files via the file tools.",
  "coordination.assist": "Respond to assistance requests from other agents.",
  "coordination.wait": "Park a coordination request on the agent of expertise queue.",
  "rag.query": "Query the project's RAG index.",
  "rag.rebuild": "Trigger a project-scoped RAG rebuild (operator-only).",

  // System management (router-side admin)
  "system.view": "Read-only access to the system dashboard surface.",
  "system.config.write": "Write per-project config_* toggles (worker permissions, retention, etc.).",
  "system.users.manage": "Create/edit/delete operator users (router-level).",
  "system.groups.manage": "Create/edit/delete groups (router-level).",
  "system.groups.capabilities.manage":
    "Assign capabilities to groups (this very surface).",
  "system.projects.manage": "Create / rename / delete projects on the router.",
  "system.sso.configure": "Read SSO config (env-var sourced today; capability future-proofs).",
}

/** Bucket cap strings by their first dotted segment.
 *  Returns ``{resource: [cap, cap, ...]}`` ordered alphabetically
 *  within each bucket so the dashboard renders stably. Resources
 *  themselves are ordered per :data:`CAPABILITY_RESOURCE_ORDER`. */
export function groupCapabilitiesByResource(
  caps: readonly string[],
): { resource: string; caps: string[] }[] {
  const buckets: Map<string, string[]> = new Map()
  for (const cap of caps) {
    const resource = cap.split(".", 1)[0] || "_"
    const arr = buckets.get(resource) ?? []
    arr.push(cap)
    buckets.set(resource, arr)
  }
  // Sort capabilities within each bucket alphabetically.
  for (const arr of buckets.values()) {
    arr.sort()
  }
  // Emit in the canonical resource order; trailing unknown resources
  // (none expected for KNOWN_CAPABILITIES members) fall to the end
  // alphabetically so the UI doesn't silently drop them.
  const out: { resource: string; caps: string[] }[] = []
  for (const resource of CAPABILITY_RESOURCE_ORDER) {
    const arr = buckets.get(resource)
    if (arr) {
      out.push({ resource, caps: arr })
      buckets.delete(resource)
    }
  }
  const trailing = [...buckets.keys()].sort()
  for (const resource of trailing) {
    out.push({ resource, caps: buckets.get(resource)! })
  }
  return out
}
