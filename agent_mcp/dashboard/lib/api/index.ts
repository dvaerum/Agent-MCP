// API client for Agent-MCP backend — barrel.
//
// PR-W3 (ORM big-bang, v5.0.19): the canonical row shapes for every
// persistent table live in `../api-types.generated.ts`, emitted by
// `scripts/generate_ts_types.py` from the Pydantic mirrors in
// `agent_mcp/db/pydantic_mirrors.py`. New dashboard code should
// prefer those interfaces (suffixed `Mirror`) because they are
// guaranteed to stay column-accurate with the ORM via the CI
// invariant in tests/test_orm_is_source_of_truth.py.
//
// The hand-maintained `Agent` / `Task` / `Memory` etc interfaces
// declared in the per-resource modules are kept for back-compat. They
// add richer literal unions (status: 'pending' | 'running' | ...) the
// bare DB column types can't express.
//
// W6-followup F1 (api-layer split): the old 1.5k-line `lib/api.ts`
// God-module was split into `lib/api/{client,agents,tasks,memories,
// messages,system,schedules,settings,instance}.ts`. This barrel
// re-exports the whole public surface so every existing
// `import { … } from '@/lib/api'` keeps resolving unchanged.
export * from '../api-types.generated'

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
  type GraphNode,
  type GraphEdge,
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
