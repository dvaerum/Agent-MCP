"use client"

// Alias-chip expansion panel (Phase 3.5c — prancy-napping-pie). Renders
// inline below a project card's alias badge when the user clicks the
// chip. Surfaces:
//
//   * The list of agent_ids that have used the alias (from
//     /agent-mcp/__alias-usage backed by mcp_sessions.alias_used).
//   * A "Remove alias now" button calling /agent-mcp/__remove-alias.
//
// "Extend grace" is intentionally NOT implemented here — extending an
// alias is equivalent to issuing a new alias via __rename (which can
// re-add the alias with a fresh expires_at). Operator workflow: if
// the cutover needs more time, hit Rename and re-aim the project at
// the same name with a longer grace_days. A dedicated extend-grace
// endpoint can land in a follow-up once we see real demand.

import React, { useEffect, useState } from "react"
import { Loader2, Trash2, Users, AlertCircle } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useProjectsStore, type ProjectAlias } from "@/lib/stores/projects-store"

interface AliasUsage {
  alias: string
  project: string
  expires_at: string
  agents: string[]
}

export interface AliasChipPanelProps {
  projectName: string
  alias: ProjectAlias
  open: boolean
  onClose: () => void
}

export function AliasChipPanel({
  projectName,
  alias,
  open,
  onClose,
}: AliasChipPanelProps): React.ReactElement | null {
  const [usage, setUsage] = useState<AliasUsage | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [removing, setRemoving] = useState(false)
  const fetchOverview = useProjectsStore((s) => s.fetchOverview)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    setError(null)
    fetch(
      `/agent-mcp/__alias-usage?alias=${encodeURIComponent(alias.name)}`,
      { cache: "no-store" },
    )
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const body = (await r.json()) as AliasUsage
        if (!cancelled) {
          setUsage(body)
          setLoading(false)
        }
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : String(e))
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, alias.name])

  const handleRemove = async () => {
    setRemoving(true)
    setError(null)
    try {
      const body = new URLSearchParams()
      body.set("name", projectName)
      body.set("alias", alias.name)
      const r = await fetch("/agent-mcp/__remove-alias", {
        method: "POST",
        body,
        headers: { Accept: "application/json" },
      })
      if (!r.ok) {
        const text = await r.text().catch(() => "")
        throw new Error(text || `HTTP ${r.status}`)
      }
      await fetchOverview()
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setRemoving(false)
    }
  }

  if (!open) return null

  return (
    <div className="mt-2 p-3 border rounded bg-muted/30 text-xs space-y-2">
      <div className="flex items-center justify-between">
        <div className="font-medium">
          Alias <code>{alias.name}</code> →{" "}
          <span className="text-muted-foreground">
            expires {alias.expires_at.slice(0, 10)}
          </span>
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 text-xs"
          onClick={onClose}
        >
          Close
        </Button>
      </div>

      {loading && (
        <div className="flex items-center gap-2 text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" />
          Loading usage…
        </div>
      )}

      {error && (
        <div className="flex items-center gap-2 text-destructive">
          <AlertCircle className="h-3 w-3" />
          {error}
        </div>
      )}

      {usage && !loading && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <Users className="h-3 w-3" />
            <span>
              Used by{" "}
              <span className="font-medium">{usage.agents.length}</span>{" "}
              agent(s):
            </span>
          </div>
          {usage.agents.length === 0 ? (
            <p className="text-muted-foreground italic">
              No recorded usage. Safe to remove.
            </p>
          ) : (
            <ul className="ml-5 list-disc space-y-0.5">
              {usage.agents.map((a) => (
                <li key={a}>
                  <code>{a}</code>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="pt-1">
        <Button
          variant="destructive"
          size="sm"
          className="h-7 text-xs"
          disabled={removing}
          onClick={handleRemove}
        >
          {removing ? (
            <Loader2 className="h-3 w-3 mr-1 animate-spin" />
          ) : (
            <Trash2 className="h-3 w-3 mr-1" />
          )}
          Remove alias now
        </Button>
      </div>
    </div>
  )
}
