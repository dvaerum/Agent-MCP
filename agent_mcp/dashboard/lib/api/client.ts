// Shared request core for the per-project REST surface.
//
// W6-followup F1 (api-layer split): the old 1.5k-line `lib/api.ts`
// God-module is broken into per-resource modules (agents / tasks /
// memories / messages / system / schedules / settings). This file is
// the shared core every resource module builds on:
//   - the `request<T>()` fetch funnel (versioned Accept header, cookie
//     auth, cold-start 5xx retry, 401→login bounce, timeout),
//   - the connection-management setters (`setServer` / `setBaseUrl`),
//   - the typed `ApiError` / `ShapeError` errors,
//   - the tiny runtime shape-guard helpers (`isRecord` / `describe`).
//
// `ApiClient` here is the CORE class only — it carries no per-resource
// methods. The composed client (core + every resource bundle) is
// assembled by `createApiClient()` in `./instance`, which is what the
// app's `apiClient` singleton and any per-instance/test client use.

import { loginUrl } from '../urls'

/**
 * Typed error thrown by ``ApiClient.request`` on a !ok HTTP response.
 *
 * Pre-PR (silent-error UX bug surfaced by Firefox-MCP click-through on
 * 2026-06-17 against v5.0.47): the request layer only threw
 * ``new Error('API Error: 400 Bad Request')`` — the status line, no
 * body. The server's carefully-worded validation message (e.g. PR
 * #163's "invalid agent_id 'BadName!@#': must match ...") was logged
 * to console but never reached the UI; mutation handlers'
 * ``console.error`` swallow-pattern then made the failure invisible.
 *
 * ``ApiError`` carries:
 *   - ``status``  HTTP status code (e.g. 400 / 404 / 500).
 *   - ``message`` Best-effort human-readable text — preferring the
 *                 server's JSON ``{message: ...}`` field, falling back
 *                 to ``{detail}`` / ``{error}`` / raw body / status
 *                 line so toasts never end up empty.
 *   - ``body``    Raw response text for callers / logs that want the
 *                 full payload (parsing failures still preserved).
 *
 * Callers in components/ pass the caught error to ``toastError`` from
 * ``components/ui/toast`` which prefers ``err.message`` so the user
 * sees what the server actually said.
 */
export class ApiError extends Error {
  readonly status: number
  readonly body: string

  constructor(status: number, message: string, body: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

/**
 * Thrown by the `request<T>()` response-shape guards (TY-1) when the
 * backend returns a body that does not match the shape the caller
 * expects. Distinct from `ApiError` (which is an HTTP-level failure —
 * a non-2xx status): a `ShapeError` means the HTTP call succeeded (200
 * OK) but the JSON payload is structurally wrong, so trusting it with a
 * bare `as T` cast would push malformed data into the store and blow up
 * far from the seam. Failing loudly here — at the API boundary, with
 * the endpoint and the mismatch named — is the whole point.
 */
export class ShapeError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'ShapeError'
  }
}

/** Narrow an arbitrary value to a plain record (used by the shape guards). */
export function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** A short, log-safe description of an arbitrary value for error text. */
export function describe(v: unknown): string {
  if (v === null) return 'null'
  if (Array.isArray(v)) return `an array of length ${v.length}`
  return typeof v
}

/**
 * The request core: connection state + the single `request<T>()` fetch
 * funnel every resource module flows through. Instantiable per-instance
 * (`new ApiClient(baseUrl)`); the app assembles one composed client via
 * `createApiClient()` in `./instance`.
 */
export class ApiClient {
  private baseUrl: string
  private suppressErrors: boolean = false

  constructor(baseUrl: string = '') {
    this.baseUrl = baseUrl
  }

  // Set whether to suppress connection errors (useful during server discovery)
  setSuppressErrors(suppress: boolean) {
    this.suppressErrors = suppress
  }

  // Dynamic server connection.
  //
  // Convention: `baseUrl` IS the API root, including the `/api`
  // path segment when present. setServer appends `/api` so every
  // other method can just concatenate the endpoint without
  // worrying about where the prefix lives.
  setServer(host: string, port: number) {
    this.baseUrl = `http://${host}:${port}/api`
  }

  /**
   * Set the API root URL directly.
   *
   * Use this instead of `setServer(host, port)` when the caller
   * already knows the absolute or relative URL of the API root
   * (for example, path-prefixed deployments mounted behind a
   * reverse-proxy router where the dashboard fetches resolve via
   * `/agent-mcp/api/<name>` (PR-B renamed from /__api/) rather than
   * a `http://host:port/api`
   * origin).
   *
   * The provided URL should be the API root including any `/api`
   * segment, matching the same convention as `setServer`. Endpoint
   * paths are concatenated to this value directly.
   */
  setBaseUrl(url: string) {
    this.baseUrl = url
  }

  /**
   * Returns the API root URL (includes `/api`, not just the server
   * origin). Callers that build URLs from this should concatenate
   * the endpoint directly without adding `/api/` themselves.
   */
  getServerUrl(): string {
    return this.baseUrl
  }

  async request<T>(
    endpoint: string,
    options: RequestInit = {},
    // TY-1: optional runtime shape guard applied to the parsed JSON at
    // the boundary. When supplied, it either returns the value narrowed
    // to T or throws a ShapeError; when omitted, the body is trusted
    // with a bare cast (back-compat for the many endpoints whose shape
    // is asserted elsewhere). Read paths that feed the store pass a
    // guard so a structurally-wrong 200 fails loudly HERE rather than
    // deep in a consumer.
    validate?: (data: unknown) => T,
  ): Promise<T> {
    // Check if a server is connected
    if (!this.baseUrl) {
      throw new Error('NO_SERVER_CONNECTED')
    }

    const url = `${this.baseUrl}${endpoint}`

    // Enhanced CORS configuration.
    //
    // PR-A: the strict, version-pinned API media type is required by
    // the router's Accept-header gate (/agent-mcp/api/<name>/*). A
    // plain `application/json` Accept value is rejected with 406. The
    // dashboard is a first-class consumer of the v1 surface, so the
    // gate header is part of every request.
    //
    // PR D (prancy-napping-pie): credentials='include' so the
    // ``agent_mcp_session`` cookie is sent with every fetch. The
    // cookie is set by /agent-mcp/login (PR C) and is what
    // authenticates dashboard mutations now that the body-token
    // path is retired. Same-origin requests still attach the cookie
    // with omit (Path matches), but credentials='include' covers
    // cross-origin dev setups too (the cookie's SameSite=Lax
    // attribute keeps it scoped sensibly).
    const fetchOptions: RequestInit = {
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/vnd.agent-mcp.v1+json',
        // Don't set Origin header - let browser handle it automatically
        ...options.headers,
      },
      credentials: 'include',
      mode: 'cors', // Explicitly set CORS mode
      cache: 'no-cache', // Always get fresh data
      ...options,
    }

    // Add timeout support.
    //
    // Cold-start abort fix (P005, 2026-06-19): the per-request timeout
    // must safely outlast the router's lazy-spawn socket-wait. The
    // orchestrator (`agent_mcp/router/project_orchestrator.py`) waits
    // up to 20 s for a freshly-started backend's Unix socket to appear
    // before surfacing 504 Gateway Timeout. A request that lands while
    // the backend is still spawning blocks inside the proxy until the
    // socket exists. If the client's `AbortController` fires first,
    // every in-flight per-project fetch on the dashboard's first paint
    // is cancelled with `NS_BINDING_ABORTED` — the main panel renders
    // empty because the 5xx-retry loop below never sees a status code,
    // only a discarded promise.
    //
    // 30 s covers the orchestrator's 20 s socket budget plus the
    // 200 ms + 400 ms retry backoff and HTTP round-trip overhead, so
    // the first cold request either returns a real response (success
    // or 504) or — far more likely — a 5xx the retry loop catches and
    // retries against an already-warm backend.
    const controller = new AbortController()
    const timeoutId = setTimeout(() => controller.abort(), 30000) // 30 second timeout

    try {
      // Transparent cold-start retry. A lazily-spawned backend takes
      // ~10-15s to create its Unix socket (Python import time +
      // lifespan startup); during that window the router's proxy
      // returns 502/503/504. Retrying on 5xx with exponential backoff
      // (200ms, 400ms) lets the first request transparently wait for
      // the backend instead of bubbling an error up to a boundary-
      // level useEffect retry loop (the pattern this refactor
      // replaces — Candidate C, architecture review 2026-06-01).
      //
      // Bounded at 3 attempts (200ms + 400ms = 600ms total backoff
      // budget plus the original request's own timeout). 4xx and
      // non-5xx are not retried.
      //
      // Method gate: only safe (read-only) methods are retried. The
      // original implementation retried EVERY method, which silently
      // double-fired non-idempotent mutations when the backend
      // processed a POST/PATCH/DELETE, committed the side-effect, and
      // then crashed/disconnected returning 502 on the response phase.
      // Concrete bug shapes that caused: createTask → two identical
      // tasks (server-generated task_id, no uniqueness collision to
      // catch the dup); sendMessage → double fan-out; terminateAgent
      // → safe (idempotent server-side, but still pointless retry).
      // 5xx on a mutation must reach the caller's catch handler so the
      // operator sees the error and decides whether to retry manually.
      const method = (
        typeof fetchOptions.method === 'string' ? fetchOptions.method : 'GET'
      ).toUpperCase()
      const isReadOnly = method === 'GET' || method === 'HEAD'
      let response: Response | null = null
      for (let attempt = 0; attempt < 3; attempt++) {
        response = await fetch(url, {
          ...fetchOptions,
          signal: controller.signal
        })
        if (
          isReadOnly &&
          response.status >= 500 &&
          response.status < 600 &&
          attempt < 2
        ) {
          await new Promise(res => setTimeout(res, 200 * 2 ** attempt))
          continue
        }
        break
      }

      clearTimeout(timeoutId)

      // Non-null after the loop: the loop body always assigns `response`
      // on its first iteration, and we either continue (assigning again)
      // or break.
      const r = response as Response

      if (!r.ok) {
        const errorText = await r.text().catch(() => 'Unknown error')
        // PR D (prancy-napping-pie): on a 401 from any mutation OR
        // read, the operator's session cookie has expired (or was
        // never set). Bounce to /agent-mcp/login and preserve the
        // current path in ?next= so we land back here post-login.
        //
        // Guard the redirect with the standard SSR check (typeof
        // window) so this method stays safe to call from Next.js
        // server components / tests that import the singleton.
        // Also guard against an infinite loop: if we're already on
        // the login page, skip the bounce.
        if (
          r.status === 401 &&
          typeof window !== 'undefined' &&
          // ADR-0020: compare against the mount-derived login path
          // (loginUrl() = `${ROOT}/login`) so the loop-guard holds at
          // both the tailnet (/agent-mcp/login) and root (/login) mounts.
          !window.location.pathname.endsWith(loginUrl())
        ) {
          const next = window.location.pathname + window.location.search
          window.location.assign(loginUrl(next))
          // Throw so the caller's `.catch` doesn't accidentally
          // surface stale data; the navigation will tear down the
          // page before this matters in practice.
          throw new ApiError(401, 'session expired; redirecting to login', errorText)
        }
        // Only log non-404 errors
        if (r.status !== 404) {
          console.error(`API Error [${r.status}]:`, errorText)
        }
        // Prefer the server's JSON ``{message: ...}`` payload (the
        // 400 / 422 / 500 paths in agent_mcp/api/* all emit a
        // ``message`` field — see
        // tests/test_dashboard_create_agent_endpoint.py). Fall back
        // through ``detail`` (FastAPI default) / ``error`` (some
        // legacy endpoints) / raw body / status line so the surfaced
        // ``error.message`` is never an empty string.
        let surfaced = `${r.status} ${r.statusText}`.trim()
        try {
          const parsed = JSON.parse(errorText)
          if (parsed && typeof parsed === 'object') {
            const candidate =
              (typeof parsed.message === 'string' && parsed.message) ||
              (typeof parsed.detail === 'string' && parsed.detail) ||
              (typeof parsed.error === 'string' && parsed.error) ||
              ''
            if (candidate) {
              surfaced = candidate
            }
          }
        } catch {
          // Body wasn't JSON — fall back to the raw text if
          // non-empty, otherwise keep the status-line default.
          if (errorText && errorText !== 'Unknown error') {
            surfaced = errorText
          }
        }
        throw new ApiError(r.status, surfaced, errorText)
      }

      const parsed: unknown = await r.json()
      // TY-1: validate the response shape at the seam when the caller
      // supplied a guard; otherwise fall through to the trusted cast.
      return validate ? validate(parsed) : (parsed as T)
    } catch (error) {
      clearTimeout(timeoutId)

      // Log errors only in debug mode or for non-connection errors
      if (error instanceof Error) {
        // Only log non-connection errors to console when not suppressing
        if (!this.suppressErrors && !error.message.includes('Failed to fetch') && !error.message.includes('ERR_CONNECTION_REFUSED')) {
          console.error(`Request failed to ${url}:`, {
            name: error.name,
            message: error.message,
            stack: error.stack
          })
        }

        if (error.name === 'AbortError') {
          throw new Error('Request timeout')
        }

        if (error.message.includes('Failed to fetch')) {
          // Throw a clean error without triggering additional console logs
          const err = new Error(`Network error: Unable to connect to ${this.baseUrl}`)
          // Mark this error as expected to prevent logging
          ;(err as Error & { isExpected?: boolean }).isExpected = true
          throw err
        }
      }

      throw error
    }
  }

  // Real-time updates via Server-Sent Events
  createEventSource(endpoint: string): EventSource {
    return new EventSource(`${this.baseUrl}${endpoint}`)
  }

  // Utility methods
  async healthCheck(): Promise<{ status: string; timestamp: string }> {
    return this.request('/health')
  }
}
