"use client"

import React, { useState } from "react"
import { Copy, Plus } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { apiClient } from "@/lib/api"
import { toastError, toastSuccess } from "@/components/ui/toast"
import { projectContext } from "@/lib/project-context"
import { deriveMount } from "@/lib/urls"

// agent_id slug rule — mirrors the backend's `_AGENT_ID_RE`
// (agent_mcp/repositories/agent_repository.py): lowercase letter start,
// lowercase letters / digits / hyphens, ends on a letter or digit
// (single-char names allowed). Same shape as the project-name slug in
// add-project-modal.tsx. Used for the live hint so the operator sees the
// format problem before submitting instead of eating a 400 from the
// repo-seam validator.
// '@' and '_' allowed in the interior (e.g. worker@host, pikvm_mcp_server@host),
// not at start/end. Mirrors the server-side `_AGENT_ID_RE` in agent_repository.py.
export const AGENT_ID_RE = /^[a-z](?:[a-z0-9@_-]*[a-z0-9])?$/

// Wave 7 coordinator transition (`prancy-napping-pie.md` § Wave 7).
//
// Two-pane modal for the register-only flow. Pane 1 collects
// `name` + `role`; submit calls `apiClient.registerAgent` (which hits
// POST /api/agents/register on the backend). Pane 2 shows the minted
// agent_id + bearer token + ready-to-paste .mcp.json snippet, with a
// "Copy snippet" button.
//
// agent-mcp does NOT start a claude process; the operator hands the
// snippet to the user, who pastes it into their own `.mcp.json` and
// runs claude themselves. The legacy `CreateAgentModal` (spawn path)
// was deleted in Wave 7 PR 3.
export interface RegisterAgentResult {
  agent_id: string
  agent_token: string
  mcp_snippet: string
  message: string
}

export const RegisterAgentModal = () => {
  const [open, setOpen] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [result, setResult] = useState<RegisterAgentResult | null>(null)
  const [formData, setFormData] = useState<{
    name: string
    role: 'worker' | 'manager'
  }>({ name: '', role: 'worker' })
  const [copyState, setCopyState] = useState<string | null>(null)

  const reset = () => {
    setFormData({ name: '', role: 'worker' })
    setResult(null)
    setSubmitting(false)
  }

  const trimmedName = formData.name.trim()
  const nameValid = AGENT_ID_RE.test(trimmedName)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!nameValid || submitting) return
    setSubmitting(true)
    try {
      // The backend's snippet builder needs both the project name
      // (which the per-project backend can't derive after the router
      // proxy strips Host) and the public origin. The dashboard knows
      // both, so we send them explicitly.
      const projectName = projectContext.projectName
      const host = typeof window !== 'undefined' ? window.location.origin : ''
      const res = await apiClient.registerAgent({
        name: formData.name.trim(),
        role: formData.role,
        project_name: projectName,
        host: host || undefined,
        // ADR-0020: send the current mount prefix so the snippet URL
        // matches this front door ("" at root, "/agent-mcp" on tailnet).
        mount_prefix: deriveMount(),
      })
      if (!res.agent_id || !res.agent_token || !res.mcp_snippet) {
        throw new Error('Backend response missing required fields')
      }
      setResult({
        agent_id: res.agent_id,
        agent_token: res.agent_token,
        mcp_snippet: res.mcp_snippet,
        message: res.message,
      })
      toastSuccess(`Agent "${res.agent_id}" registered.`)
    } catch (err) {
      // Errors surface through the shared toast (the page-wide idiom) —
      // the dialog deliberately stays OPEN with the operator's input
      // intact so they can fix the id and retry.
      toastError(err, 'Failed to register agent')
    } finally {
      setSubmitting(false)
    }
  }

  const copy = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopyState(label)
      setTimeout(
        () => setCopyState((cur) => (cur === label ? null : cur)),
        1500,
      )
    } catch {
      // navigator.clipboard can fail on insecure origins; silent —
      // the snippet is still selectable from the rendered <pre>.
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        setOpen(v)
        if (!v) reset()
      }}
    >
      <DialogTrigger asChild>
        <Button
          size="sm"
          variant="outline"
          className="border-primary/40 text-primary hover:bg-primary/10"
        >
          <Plus className="h-4 w-4 mr-1.5" />
          Register Agent
        </Button>
      </DialogTrigger>
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-lg bg-card border-border text-card-foreground max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="text-lg">
            {result ? 'Agent registered' : 'Register Agent'}
          </DialogTitle>
          <DialogDescription className="text-muted-foreground">
            {result
              ? "Paste the snippet below into the user's claude .mcp.json. agent-mcp doesn't start the claude process for you — the user does."
              : 'Mint an agent identity (DB row + bearer token) and get back a ready-to-paste .mcp.json snippet. Wave 7 coordinator model: agent-mcp never spawns claude.'}
          </DialogDescription>
        </DialogHeader>

        {/* Pane 1 — input */}
        {!result && (
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
                Agent ID
              </label>
              <Input
                value={formData.name}
                onChange={(e) =>
                  setFormData((prev) => ({ ...prev, name: e.target.value }))
                }
                placeholder="worker-analytics-01"
                className="bg-background border-border text-foreground"
                aria-invalid={trimmedName.length > 0 && !nameValid}
                required
              />
              {trimmedName.length > 0 && !nameValid && (
                <p className="text-xs text-destructive mt-1">
                  Lowercase slug only: start with a letter, then lowercase
                  letters, digits, or hyphens (^[a-z][a-z0-9-]*[a-z0-9]?$).
                </p>
              )}
            </div>
            <div>
              <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
                Role
              </label>
              <Select
                value={formData.role}
                onValueChange={(v) =>
                  setFormData((prev) => ({
                    ...prev,
                    role: v as 'worker' | 'manager',
                  }))
                }
              >
                <SelectTrigger className="bg-background border-border text-foreground">
                  <SelectValue placeholder="Select role" />
                </SelectTrigger>
                <SelectContent className="bg-background border-border">
                  <SelectItem value="worker">Worker</SelectItem>
                  <SelectItem value="manager">Manager</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <DialogFooter className="gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => setOpen(false)}
                size="sm"
                disabled={submitting}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                size="sm"
                className="bg-primary hover:bg-primary/90"
                disabled={submitting || !nameValid}
              >
                {submitting ? 'Registering...' : 'Register'}
              </Button>
            </DialogFooter>
          </form>
        )}

        {/* Pane 2 — output snippet */}
        {result && (
          <div className="space-y-4 min-w-0">
            <div className="text-sm">
              <div className="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-1">
                Agent ID
              </div>
              <code className="font-mono">{result.agent_id}</code>
            </div>
            <div className="min-w-0">
              <div className="flex items-center justify-between mb-1">
                <div className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                  .mcp.json snippet
                </div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => copy(result.mcp_snippet, 'snippet')}
                >
                  <Copy className="h-3.5 w-3.5 mr-1.5" />
                  {copyState === 'snippet' ? 'Copied' : 'Copy snippet'}
                </Button>
              </div>
              <pre className="w-full max-w-full bg-muted/40 border border-border rounded-md p-3 text-xs font-mono overflow-x-auto max-h-72">{result.mcp_snippet}</pre>
            </div>
            <div className="text-xs text-muted-foreground">
              Paste this into the user&apos;s <code>.mcp.json</code>, then
              ask them to start <code>claude</code> with the matching
              project. The token can be revoked any time via the
              Terminate button on this agent&apos;s row.
            </div>
            <DialogFooter className="gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => reset()}
              >
                Register another
              </Button>
              <Button
                type="button"
                size="sm"
                onClick={() => {
                  setOpen(false)
                  reset()
                }}
              >
                Done
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
