"use client"

// Wiring snippets tab on the overview (Phase 3.5c — prancy-napping-pie
// decisions #3 + #10 / ADR-0009). Absorbs the operator-facing copy-
// pastable configuration content that used to live in the router's
// HTML index page (`_wiring_help_panel`).
//
// One <details> block per registered project, each rendering:
//
//   * `.mcp.json` snippet — type=http Streamable HTTP transport
//     entry that points an MCP client at the project's /mcp URL.
//     Admin token masked by default with a "Reveal" toggle + a
//     "Copy" button.
//   * Installer one-liner — `curl ... | bash` form fetching the
//     installer script from the router (which substitutes the
//     project's URL + token at request time).
//   * Agent-token generation help — short note explaining where
//     agent tokens come from (admin-only `create_agent` tool).
//
// The content is intentionally read-only / informational. Add /
// rename / remove live on the Projects tab (the cards). This tab
// is the "how do I wire a client up to this project" panel.

import React, { useEffect, useState } from "react"
import {
  Copy, Eye, EyeOff, Loader2, AlertCircle, ExternalLink, FileCode,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { useProjectsStore } from "@/lib/stores/projects-store"
import {
  mcpUrl,
  projectClientConfigUrl,
  projectInstallerUrl,
} from "@/lib/urls"
import { routerApi } from "@/lib/router-api"

interface ClientConfig {
  mcpServers: {
    [k: string]: {
      type: string
      url: string
      headers?: Record<string, string>
    }
  }
}

interface AdminTokenResult {
  token: string | null
  source: "router" | "backend" | "unavailable"
  error?: string
}

function originBase(): string {
  if (typeof window === "undefined") return ""
  return `${window.location.protocol}//${window.location.host}`
}

function buildMcpJsonFor(projectName: string, token: string | null): ClientConfig {
  const entry: ClientConfig["mcpServers"][string] = {
    type: "http",
    url: mcpUrl(projectName, originBase()),
  }
  if (token) {
    entry.headers = { Authorization: `Bearer ${token}` }
  }
  return { mcpServers: { "agent-mcp": entry } }
}

function buildInstallerOneliner(projectName: string): string {
  const path = projectInstallerUrl(projectName)
  return `curl -fsSL "${originBase()}${path}" | bash`
}

function MaskedToken({
  token,
  revealed,
}: {
  token: string | null
  revealed: boolean
}): React.ReactElement {
  if (!token) return <span className="italic">REPLACE_WITH_YOUR_AGENT_TOKEN</span>
  if (revealed) return <span>{token}</span>
  const head = token.slice(0, 4)
  const tail = token.slice(-4)
  return (
    <span className="font-mono">
      {head}…{tail}
    </span>
  )
}

function ProjectWiringPanel({
  projectName,
}: {
  projectName: string
}): React.ReactElement {
  const [token, setToken] = useState<AdminTokenResult>({
    token: null,
    source: "unavailable",
  })
  const [loading, setLoading] = useState(false)
  const [revealed, setRevealed] = useState(false)
  const [copyState, setCopyState] = useState<string | null>(null)

  const loadToken = async () => {
    setLoading(true)
    try {
      // The router's client-config endpoint embeds the admin token
      // when called without an `?agent=` parameter, which is the
      // cheapest way to surface it without hitting the per-project
      // backend directly. It returns the .mcp.json JSON body.
      const body = await routerApi.request<ClientConfig>(
        projectClientConfigUrl(projectName),
        { cache: "no-store" },
      )
      const auth = body?.mcpServers?.["agent-mcp"]?.headers?.Authorization ?? ""
      const m = auth.match(/^Bearer\s+(.+)$/)
      if (m && m[1] && m[1] !== "REPLACE_WITH_YOUR_AGENT_TOKEN") {
        setToken({ token: m[1], source: "router" })
      } else {
        setToken({
          token: null,
          source: "unavailable",
          error: "no token in client-config response",
        })
      }
    } catch (e) {
      setToken({
        token: null,
        source: "unavailable",
        error: e instanceof Error ? e.message : String(e),
      })
    } finally {
      setLoading(false)
    }
  }

  const copy = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopyState(label)
      setTimeout(() => setCopyState((cur) => (cur === label ? null : cur)), 1500)
    } catch {
      // navigator.clipboard can fail on insecure origins (rare for
      // a Tailscale https deployment); silent — the snippet is still
      // visible for manual copy.
    }
  }

  const mcpJson = buildMcpJsonFor(projectName, revealed ? token.token : null)
  const mcpJsonText = JSON.stringify(mcpJson, null, 2)
  const installerOneliner = buildInstallerOneliner(projectName)

  return (
    <div className="space-y-4">
      <div>
        <div className="flex items-center justify-between mb-1">
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            .mcp.json
          </span>
          <div className="flex items-center gap-1">
            {!token.token && !loading && (
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                onClick={loadToken}
              >
                Load admin token
              </Button>
            )}
            {token.token && (
              <Button
                variant="ghost"
                size="sm"
                className="h-7 text-xs"
                onClick={() => setRevealed((v) => !v)}
              >
                {revealed ? (
                  <>
                    <EyeOff className="h-3 w-3 mr-1" />
                    Mask
                  </>
                ) : (
                  <>
                    <Eye className="h-3 w-3 mr-1" />
                    Reveal
                  </>
                )}
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              className="h-7 text-xs"
              onClick={() => copy(mcpJsonText, "mcp-json")}
            >
              <Copy className="h-3 w-3 mr-1" />
              {copyState === "mcp-json" ? "Copied" : "Copy"}
            </Button>
          </div>
        </div>
        <pre className="bg-muted px-3 py-2 rounded text-[11px] overflow-x-auto">
          <code>{mcpJsonText}</code>
        </pre>
        {token.token && (
          <p className="text-[11px] text-muted-foreground mt-1">
            Token: <MaskedToken token={token.token} revealed={revealed} />{" "}
            (source: {token.source})
          </p>
        )}
        {token.error && (
          <p className="text-[11px] text-destructive mt-1 flex items-center gap-1">
            <AlertCircle className="h-3 w-3" />
            {token.error}
          </p>
        )}
        {loading && (
          <p className="text-[11px] text-muted-foreground mt-1 flex items-center gap-1">
            <Loader2 className="h-3 w-3 animate-spin" />
            Fetching admin token from router…
          </p>
        )}
      </div>

      <div>
        <div className="flex items-center justify-between mb-1">
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Installer one-liner
          </span>
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              className="h-7 text-xs"
              asChild
            >
              <a
                href={projectInstallerUrl(projectName)}
                target="_blank"
                rel="noopener noreferrer"
              >
                <ExternalLink className="h-3 w-3 mr-1" />
                View
              </a>
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 text-xs"
              onClick={() => copy(installerOneliner, "installer")}
            >
              <Copy className="h-3 w-3 mr-1" />
              {copyState === "installer" ? "Copied" : "Copy"}
            </Button>
          </div>
        </div>
        <pre className="bg-muted px-3 py-2 rounded text-[11px] overflow-x-auto">
          <code>{installerOneliner}</code>
        </pre>
        <p className="text-[11px] text-muted-foreground mt-1">
          Runs the router-rendered installer script. The script
          writes the .mcp.json into the current directory or wherever
          the template directs.
        </p>
      </div>

      <div className="text-[11px] text-muted-foreground border-t pt-2">
        <p className="flex items-center gap-1 mb-1">
          <FileCode className="h-3 w-3" />
          Agent tokens
        </p>
        <p>
          The Admin token surfaces above. Per-agent tokens are minted
          by the admin <code>create_agent</code> tool via MCP; see the
          dashboard&apos;s Agents tab for the current set.
        </p>
      </div>
    </div>
  )
}

export function WiringSnippetsTab(): React.ReactElement {
  const { envelope, loading, error, fetchOverview } = useProjectsStore()

  useEffect(() => {
    if (!envelope && !loading) void fetchOverview()
  }, [envelope, loading, fetchOverview])

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold">Wiring snippets</h2>
        <p className="text-sm text-muted-foreground">
          Copy-pastable <code>.mcp.json</code> + installer one-liners
          for each registered project. Use these to point a fresh MCP
          client (Claude Code, Cline, etc.) at a project&apos;s backend.
        </p>
      </div>

      {error && (
        <Card>
          <CardContent className="flex items-center gap-2 py-4 text-sm text-destructive">
            <AlertCircle className="h-4 w-4" />
            {error}
          </CardContent>
        </Card>
      )}

      {envelope && envelope.projects.length === 0 && !loading && (
        <Card>
          <CardContent className="py-8 text-sm text-muted-foreground text-center">
            No projects yet. Add one from the Projects tab.
          </CardContent>
        </Card>
      )}

      {envelope?.projects.map((p) => (
        <Card key={p.name}>
          <CardHeader>
            <CardTitle className="text-base">{p.name}</CardTitle>
          </CardHeader>
          <CardContent>
            <ProjectWiringPanel projectName={p.name} />
          </CardContent>
        </Card>
      ))}
    </div>
  )
}
