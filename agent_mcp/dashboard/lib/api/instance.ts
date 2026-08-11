// Composed API client — assembles the request core (`./client`) with
// every per-resource method bundle into one object, and constructs the
// app's shared instance.
//
// W6-followup F1 (ST-6, per-instance apiClient): the client is now
// assembled from a plain `ApiClient` request core + resource bundles
// bound to it, exposed via the `createApiClient()` factory. That makes
// the client genuinely instantiable per-instance — tests and any future
// multi-endpoint scope can `createApiClient()` their own rather than
// mutating one hidden module global.
//
// The app still uses a single shared `apiClient` instance, and that is
// deliberate: it must be reachable from non-React modules (the server
// store mutates its `baseUrl`; the SSE dispatcher and imperative query
// helpers read through it) — exactly the same constraint that makes the
// TanStack `queryClient` a module singleton (see `lib/query-client.ts`).
// Constructing it here — its own module, not the God-module — means
// importing the API types/classes no longer forces the singleton into
// existence as an import side-effect.

import { ApiClient } from './client'
import { agentsApi } from './agents'
import { tasksApi } from './tasks'
import { memoriesApi } from './memories'
import { systemApi } from './system'
import { schedulesApi } from './schedules'
import { settingsApi } from './settings'

/**
 * Construct a fully-composed API client: a fresh request core with
 * every resource method bundle bound to it. Each call yields an
 * independent instance with its own connection state.
 */
export function createApiClient(baseUrl: string = ''): ApiClient &
  ReturnType<typeof agentsApi> &
  ReturnType<typeof tasksApi> &
  ReturnType<typeof memoriesApi> &
  ReturnType<typeof systemApi> &
  ReturnType<typeof schedulesApi> &
  ReturnType<typeof settingsApi> {
  const core = new ApiClient(baseUrl)
  return Object.assign(
    core,
    agentsApi(core),
    tasksApi(core),
    memoriesApi(core),
    systemApi(core),
    schedulesApi(core),
    settingsApi(core),
  )
}

/** The composed-client type (core + every resource bundle). */
export type ComposedApiClient = ReturnType<typeof createApiClient>

// The single shared app instance. See the module docstring for why one
// shared instance is retained (non-React reachability), and why it now
// lives here rather than in the barrel/God-module.
export const apiClient: ComposedApiClient = createApiClient()
