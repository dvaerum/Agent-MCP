"use client"

// SSO config view — Phase 3 Wave 3 (prancy-napping-pie).
//
// Read-only display of the router's current SSO configuration.
// Writes are deliberately not shipped in this PR: the config
// itself travels via env vars (the home-manager / nix module owns
// the canonical source). Letting the dashboard mutate them would
// either require writing back to the host config or maintaining a
// parallel config file the nix module wouldn't know about. See
// ADR-0015 for the rationale + the deferred follow-up.
//
// Sysadmin gate: the backend endpoint enforces sysadmin via
// ``perm_gates.require_sysadmin``; non-sysadmin operators see the
// 403 envelope and we render an explanatory message rather than the
// config table.

import React, { useCallback } from "react"
import { Loader2, ShieldAlert, ShieldCheck, Server } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { routerSsoConfigUrl } from "@/lib/urls"
import { routerApi } from "@/lib/router-api"
import { useRouterQuery } from "@/hooks/use-router-query"

type SSOMode = "builtin" | "oidc" | "proxy_header"

interface OIDCConfig {
  issuer: string
  client_id: string
  client_secret_present: boolean
  provider_name: string
  group_mapping: Record<string, string>
  redirect_url: string | null
  scopes: string[]
}

interface ProxyConfig {
  trust_header: string
  trusted_ips: string[]
  default_is_sysadmin: boolean
}

interface SSOConfig {
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

export function SsoDashboard() {
  const {
    data: config,
    loading,
    error: fetchError,
    forbidden,
  } = useRouterQuery<SSOConfig>(
    useCallback(async (signal) => {
      const body = await routerApi.request<SSOConfigResponse>(
        routerSsoConfigUrl(),
        { signal },
      )
      if (!body.success || !body.config) {
        throw new Error(body.message ?? "SSO config unavailable")
      }
      return body.config
    }, []),
  )
  const error = fetchError?.message ?? null

  if (loading) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading SSO configuration...
      </div>
    )
  }
  if (forbidden) {
    return (
      <Card className="m-4">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldAlert className="h-4 w-4 text-destructive" />
            Sysadmin only
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          You don&apos;t have sysadmin privileges. Ask a sysadmin to view or
          change the SSO configuration on your behalf.
        </CardContent>
      </Card>
    )
  }
  if (error) {
    return (
      <Card className="m-4 border-destructive">
        <CardHeader>
          <CardTitle>Failed to load SSO config</CardTitle>
        </CardHeader>
        <CardContent className="text-sm">{error}</CardContent>
      </Card>
    )
  }
  if (!config) {
    return null
  }

  return (
    <div className="p-4 space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4" />
            Single sign-on mode
            <Badge variant="outline" className="ml-2">
              {prettyMode(config.mode)}
            </Badge>
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          {modeBlurb(config.mode)}
        </CardContent>
      </Card>

      {config.oidc && <OidcSection cfg={config.oidc} />}
      {config.proxy && <ProxySection cfg={config.proxy} />}

      <Card>
        <CardHeader>
          <CardTitle className="text-sm flex items-center gap-2">
            <Server className="h-4 w-4" />
            Where is this configured?
          </CardTitle>
        </CardHeader>
        <CardContent className="text-xs text-muted-foreground space-y-2">
          <p>
            SSO settings are sourced from environment variables on the
            router process (set by the home-manager / NixOS module). The
            dashboard displays the live values; edits land via the host
            config.
          </p>
          <p>
            <code>AGENT_MCP_SSO_OIDC_*</code> turns on OIDC;{" "}
            <code>AGENT_MCP_SSO_PROXY_HEADER</code> turns on proxy-header
            trust. The two modes are mutually exclusive — the router
            refuses to start when both are set.
          </p>
        </CardContent>
      </Card>
    </div>
  )
}

function OidcSection({ cfg }: { cfg: OIDCConfig }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>OIDC identity provider</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="text-sm grid grid-cols-[10rem_1fr] gap-y-2 gap-x-4">
          <dt className="text-muted-foreground">Issuer</dt>
          <dd className="font-mono break-all">{cfg.issuer}</dd>
          <dt className="text-muted-foreground">Client ID</dt>
          <dd className="font-mono break-all">{cfg.client_id}</dd>
          <dt className="text-muted-foreground">Client secret</dt>
          <dd>
            {cfg.client_secret_present ? (
              <Badge variant="outline">configured</Badge>
            ) : (
              <Badge variant="destructive">missing</Badge>
            )}
          </dd>
          <dt className="text-muted-foreground">Provider name</dt>
          <dd>{cfg.provider_name}</dd>
          <dt className="text-muted-foreground">Redirect URL</dt>
          <dd className="font-mono break-all">
            {cfg.redirect_url ?? <em>derived from request</em>}
          </dd>
          <dt className="text-muted-foreground">Scopes</dt>
          <dd className="font-mono">{cfg.scopes.join(" ")}</dd>
          <dt className="text-muted-foreground">Group mapping</dt>
          <dd>
            {Object.keys(cfg.group_mapping).length === 0 ? (
              <em className="text-muted-foreground">
                none — group claims are ignored
              </em>
            ) : (
              <ul className="space-y-1">
                {Object.entries(cfg.group_mapping).map(([from, to]) => (
                  <li key={from} className="font-mono text-xs">
                    <span className="text-muted-foreground">{from}</span>
                    <span className="mx-2">→</span>
                    <span>
                      {to === "" && from === "*"
                        ? "(JIT-create per claim)"
                        : to}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </dd>
        </dl>
      </CardContent>
    </Card>
  )
}

function ProxySection({ cfg }: { cfg: ProxyConfig }) {
  const localhostOnly =
    cfg.trusted_ips.length === 2 &&
    cfg.trusted_ips.includes("127.0.0.1") &&
    cfg.trusted_ips.includes("::1")
  return (
    <Card>
      <CardHeader>
        <CardTitle>Proxy-header trust</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="text-sm grid grid-cols-[10rem_1fr] gap-y-2 gap-x-4">
          <dt className="text-muted-foreground">Trusted header</dt>
          <dd className="font-mono">{cfg.trust_header}</dd>
          <dt className="text-muted-foreground">Trusted source IPs</dt>
          <dd className="font-mono space-x-2">
            {cfg.trusted_ips.length === 0 ? (
              <Badge variant="destructive">none — header is unusable</Badge>
            ) : (
              cfg.trusted_ips.map((ip) => (
                <code key={ip}>{ip}</code>
              ))
            )}
            {localhostOnly && (
              <p className="text-xs text-muted-foreground mt-1">
                Localhost-only — only an upstream proxy on the same host
                can supply the header.
              </p>
            )}
          </dd>
          <dt className="text-muted-foreground">JIT default sysadmin</dt>
          <dd>
            {cfg.default_is_sysadmin ? (
              <Badge variant="destructive">
                yes — every JIT user is a sysadmin
              </Badge>
            ) : (
              <Badge variant="outline">no — operator role only</Badge>
            )}
          </dd>
        </dl>
      </CardContent>
    </Card>
  )
}

function prettyMode(mode: SSOMode): string {
  switch (mode) {
    case "builtin":
      return "Built-in"
    case "oidc":
      return "OIDC"
    case "proxy_header":
      return "Proxy header"
  }
}

function modeBlurb(mode: SSOMode): string {
  switch (mode) {
    case "builtin":
      return "Username + password (Phase 1). No external IdP configured."
    case "oidc":
      return (
        "OpenID Connect authorization-code + PKCE flow against the " +
        "configured issuer. The dashboard login page surfaces the " +
        '"Sign in with ..." button to operators.'
      )
    case "proxy_header":
      return (
        "An upstream reverse proxy authenticates the request and forwards " +
        "the username via the trusted header. The router refuses headers " +
        "from non-trusted source IPs to prevent spoofing."
      )
  }
}
