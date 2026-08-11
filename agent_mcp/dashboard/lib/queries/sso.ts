"use client"

/**
 * TanStack Query hook for the router SSO config
 * (`GET /agent-mcp/api/router/sso/config`).
 *
 * W6-followup-2 increment G2 (2026-08-11): sso-dashboard used to fetch via
 * the hand-rolled `useRouterQuery` hook. This module moves the read onto
 * the shared `queryClient`, keyed by the bare `['sso-config']` (one config
 * per router — see `ssoConfigQueryKey`). Read-only surface (writes travel
 * via env vars / the nix module), so there is no in-app invalidation
 * caller today; `invalidateSsoConfig()` exists for uniformity.
 *
 * The 403 "sysadmin only" outcome is NOT folded into a `forbidden` flag —
 * `useQuery` surfaces it as a thrown `ApiError` on `error`, and the
 * consumer derives `forbidden` from
 * `error instanceof ApiError && error.status === 403` (see
 * `sso-dashboard.tsx`).
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query"
import { ssoConfigQueryKey } from "../query-client"
import { routerApi } from "@/lib/router-api"
import { routerSsoConfigUrl } from "@/lib/urls"

export type SSOMode = "builtin" | "oidc" | "proxy_header"

export interface OIDCConfig {
  issuer: string
  client_id: string
  client_secret_present: boolean
  provider_name: string
  group_mapping: Record<string, string>
  redirect_url: string | null
  scopes: string[]
}

export interface ProxyConfig {
  trust_header: string
  trusted_ips: string[]
  default_is_sysadmin: boolean
}

export interface SSOConfig {
  mode: SSOMode
  oidc?: OIDCConfig
  proxy?: ProxyConfig
}

interface SSOConfigResponse {
  success: boolean
  config?: SSOConfig
  error?: string
  message?: string
}

export async function fetchSsoConfig(signal?: AbortSignal): Promise<SSOConfig> {
  const body = await routerApi.request<SSOConfigResponse>(
    routerSsoConfigUrl(),
    signal ? { signal } : {},
  )
  if (!body.success || !body.config) {
    throw new Error(body.message ?? "SSO config unavailable")
  }
  return body.config
}

/**
 * The router SSO config query. `retry` is off (the `queryClient` default)
 * so a 403 surfaces immediately as an `ApiError` for the consumer's
 * `forbidden` derivation rather than being retried.
 */
export function useSsoConfigQuery(): UseQueryResult<SSOConfig> {
  return useQuery({
    queryKey: ssoConfigQueryKey(),
    queryFn: ({ signal }) => fetchSsoConfig(signal),
  })
}
