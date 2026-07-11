// Router-admin API client — the single fetch seam for the
// ``/agent-mcp/api/router/*`` surface (users / groups / SSO /
// memberships / aliases / project lifecycle).
//
// WHY this exists: the per-project REST surface has always had a deep
// client (``lib/api.ts`` ``ApiClient.request<T>()`` — strict Accept,
// ``credentials:'include'``, the ``ApiError`` shape, and the
// 401→login bounce). The router-admin surface had none: ~10
// components hand-rolled ``fetch`` with a copy-pasted ``STRICT_HEADERS``
// constant (×4), ``credentials:'include'`` re-typed (~27×), and bare
// ``throw new Error(`HTTP ${r.status}`)`` blocks that NEVER parsed the
// server message and — the real bug — NEVER bounced a 401 to the login
// page (they surfaced an opaque "HTTP 401" instead). This module
// mirrors ``api.ts``'s proven ``request()`` so there is ONE place for
// headers, credentials, the error shape, and the 401 redirect.
//
// Differences from ``ApiClient.request``:
//   * No ``baseUrl`` concatenation. Callers pass an absolute path built
//     from the ``lib/urls.ts`` ``router*`` helpers (the URL source of
//     truth), so the client fetches the path verbatim.
//   * No cold-start 5xx retry. That retry exists because a per-project
//     backend is lazily spawned and its Unix socket takes ~10-15s to
//     appear; the router process is always-on, so a 5xx here is a real
//     error the caller should see immediately.
//
// The ``ApiError`` class is imported from ``./api`` so both surfaces
// throw the SAME typed error (``status`` + ``message`` + ``body``);
// ``toastError`` and the components' ``err.status`` / ``err.body``
// inspection work identically against either client.

import { ApiError } from "./api"
import { loginUrl } from "./urls"

// The strict, version-pinned API media type required by the router's
// Accept-header gate. A plain ``application/json`` value is rejected
// with 406. Defined ONCE here — the per-component ``STRICT_HEADERS``
// copies were deleted in favour of this seam.
const ACCEPT = "application/vnd.agent-mcp.v1+json"

/**
 * Fetch a ``/agent-mcp/api/router/*`` endpoint with the router-admin
 * conventions applied: strict Accept media type, the operator session
 * cookie (``credentials: 'include'``), a typed ``ApiError`` on !ok, and
 * a 401→login bounce.
 *
 * @param url    Absolute path built from a ``lib/urls.ts`` ``router*``
 *               helper (e.g. ``routerUsersUrl()``).
 * @param options Standard ``fetch`` init. ``method`` defaults to GET.
 *               Extra ``headers`` are merged on top of the strict
 *               defaults (so a caller could override Content-Type if it
 *               ever needed to; none currently do).
 *
 * @returns The parsed JSON body. An empty body (e.g. a 204) resolves to
 *          ``undefined`` rather than throwing, so DELETE handlers that
 *          ignore the response don't have to special-case it.
 *
 * @throws {ApiError} On any !ok response. ``status`` is the HTTP code;
 *          ``message`` prefers the server's ``{message}`` → ``{detail}``
 *          → ``{error}`` → raw body → status line so a toast is never
 *          empty; ``body`` is the raw response text (callers that need
 *          a discriminator like the 409 ``active_sessions`` envelope
 *          re-parse it). On a 401 the browser is redirected to the
 *          login page (preserving ``?next=``) BEFORE the error throws.
 */
export async function request<T>(
  url: string,
  options: RequestInit = {},
): Promise<T> {
  // Merge order matters: caller options (method / body / signal / a
  // ``cache`` override) apply first, but the strict Accept header and
  // ``credentials:'include'`` are applied LAST so a call site can never
  // accidentally drop them — they are the invariants this seam exists
  // to guarantee.
  const { headers: callerHeaders, ...callerOptions } = options
  const fetchOptions: RequestInit = {
    cache: "no-cache",
    ...callerOptions,
    headers: {
      "Content-Type": "application/json",
      Accept: ACCEPT,
      ...callerHeaders,
    },
    credentials: "include",
  }

  const r = await fetch(url, fetchOptions)

  if (!r.ok) {
    const errorText = await r.text().catch(() => "Unknown error")

    // On a 401 the operator's session cookie has expired (or was never
    // set). Bounce to /agent-mcp/login and preserve the current path in
    // ?next= so we land back here post-login. Guard with the standard
    // SSR check (typeof window) so this stays safe to call from Next.js
    // server components / node-env tests that import the module, and
    // guard against an infinite loop if we're already on /login.
    if (
      r.status === 401 &&
      typeof window !== "undefined" &&
      !window.location.pathname.endsWith("/agent-mcp/login")
    ) {
      const next = window.location.pathname + window.location.search
      window.location.assign(loginUrl(next))
      throw new ApiError(
        401,
        "session expired; redirecting to login",
        errorText,
      )
    }

    // Prefer the server's JSON ``{message}`` payload, then ``{detail}``
    // (FastAPI default) / ``{error}`` / raw body / status line so the
    // surfaced ``error.message`` is never an empty string.
    let surfaced = `${r.status} ${r.statusText}`.trim()
    try {
      const parsed = JSON.parse(errorText)
      if (parsed && typeof parsed === "object") {
        const candidate =
          (typeof parsed.message === "string" && parsed.message) ||
          (typeof parsed.detail === "string" && parsed.detail) ||
          (typeof parsed.error === "string" && parsed.error) ||
          ""
        if (candidate) {
          surfaced = candidate
        }
      }
    } catch {
      if (errorText && errorText !== "Unknown error") {
        surfaced = errorText
      }
    }
    throw new ApiError(r.status, surfaced, errorText)
  }

  // Tolerant parse: a 204 / empty body resolves to ``undefined`` rather
  // than throwing on ``r.json()``. Router envelopes are JSON when
  // present; callers that ignore the response (e.g. a DELETE that only
  // refreshes) don't have to guard against it.
  const text = await r.text()
  return (text ? JSON.parse(text) : undefined) as T
}

/** Namespaced handle mirroring ``apiClient`` from ``./api`` — lets
 *  call sites read ``routerApi.request(...)`` for symmetry with the
 *  per-project client. */
export const routerApi = { request }
