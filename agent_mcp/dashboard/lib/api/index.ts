// API client for Agent-MCP backend — barrel.
//
// The `Agent` / `Task` / `Memory` etc interfaces declared in the
// per-resource modules are the canonical row shapes. They add richer
// literal unions (status: 'pending' | 'running' | ...) a bare DB
// column type can't express. (Phase F: the generated
// `api-types.generated.ts` "Mirror" interfaces this barrel used to
// also re-export here were produced from a Python pipeline that
// nothing in the dashboard actually imported and had already drifted
// stale -- deleted, not ported; see docs/learnings/
// ts-type-generation-deferred.md.)
//
// W6-followup F1 (api-layer split): the old 1.5k-line `lib/api.ts`
// God-module was split into `lib/api/{client,agents,tasks,memories,
// messages,system,schedules,settings,instance}.ts`. This barrel
// re-exports the whole public surface so every existing
// `import { … } from '@/lib/api'` keeps resolving unchanged.

// Shared request core + typed errors.
export {
  ApiClient,
  ApiError,
  ShapeError,
} from './client'

// Composed client: the factory + the shared app instance.
export { createApiClient, apiClient } from './instance'
export type { ComposedApiClient } from './instance'

// Per-resource types + helpers + method bundles.
export {
  type Agent,
  type AgentDetails,
  type AgentPresence,
  type TransportStatus,
  agentPresence,
} from './agents'

export {
  type Task,
  type RawTask,
  type TaskFilters,
  normalizeTask,
  normalizeTaskListField,
  buildTasksQuery,
} from './tasks'

export {
  type Memory,
  type RawContextEntry,
  type MemoryHealthAnalysis,
  type GetMemoriesOptions,
  contextEntryToMemory,
} from './memories'

export {
  type Message,
  type MessagesPage,
  getMessages,
  getMessageThread,
} from './messages'

export {
  type SystemStatus,
  type RawAllData,
  systemStatusGuard,
  allDataGuard,
} from './system'

export { type Schedule } from './schedules'

export {
  type ProjectSetting,
  type SettingsSchemaEntry,
  type SettingsSchemaResponse,
} from './settings'
