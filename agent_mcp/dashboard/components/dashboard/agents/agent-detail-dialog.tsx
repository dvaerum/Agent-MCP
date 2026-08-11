"use client"

import { useEffect, useState } from "react"
import { Copy, Pencil, Send, Trash2 } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { agentPresence, type Agent, type Task } from "@/lib/api"
import { useDataStore } from "@/lib/stores/data-store"
import { formatRelative } from "@/lib/utils"
import { SafeMarkdown } from "@/components/dashboard/memory-value-view"
import {
  ACTIVE_TAB_STORAGE_KEY,
  CLIENT_TABS,
  buildSnippetBlocks,
  deriveMcpUrl,
  type ClientTab,
  type SnippetPart,
} from "@/lib/mcp-snippets"

// SnippetBlock — one titled config block with its own Copy button.
// `block.content` is the only thing copied; `block.note` is muted
// helper text and is never written to the clipboard.
//
// Deliberately NOT extracted to its own module: it is a ~20-line
// presentational leaf with exactly one consumer (this dialog) and no
// state of its own. Splitting it out would move lines without creating
// a seam — the snippet *knowledge* (which is the part worth owning) is
// what moved, into `lib/mcp-snippets.ts`.
const SnippetBlock = ({
  block,
  copied,
  onCopy,
}: {
  block: SnippetPart
  copied: boolean
  onCopy: () => void
}) => (
  <div className="space-y-1">
    <div className="flex items-center justify-between gap-2">
      <span className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">{block.title}</span>
      <Button variant="ghost" size="sm" onClick={onCopy} className="h-7 px-2 text-xs" title={`Copy ${block.title}`}>
        <Copy className="h-3 w-3 mr-1" />
        {copied ? 'Copied' : 'Copy'}
      </Button>
    </div>
    {block.note && <p className="text-[10px] text-muted-foreground leading-snug">{block.note}</p>}
    <pre className="text-xs leading-relaxed font-mono bg-muted/40 rounded p-3 whitespace-pre-wrap break-words [overflow-wrap:anywhere] max-h-[40vh] overflow-y-auto">{block.content}</pre>
  </div>
)

// Read-only agent details modal — replaces the old sidebar drawer.
// Uses the same Dialog primitive as the Messages row-detail popup
// (PR #36). Polished to match the Tasks page dialog idiom (PR #54):
// - sm:!max-w-3xl beats the base sm:max-w-lg
// - max-h-[90vh] + flex-col body with a single
//   flex-1 min-h-0 overflow-y-auto scroll region
// - sticky header / footer (flex-shrink-0)
// - long values use [overflow-wrap:anywhere] (32-hex tokens, snippets)
// - title uses line-clamp-3 break-words (NOT truncate)
//
// MCP-onboarding section — a Tabs primitive with one tab per
// supported client. Active tab persists to localStorage so a user's
// preferred client is sticky across sessions. The per-client config
// strings live in `lib/mcp-snippets.ts`; this dialog only wires them.
export const AgentDetailDialog = ({
  agent,
  open,
  onOpenChange,
  onTaskClick,
  onEdit,
  onTerminate,
  onPurge,
  onSendDirective,
}: {
  agent: Agent | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onTaskClick: (task: Task) => void
  // Optional in-modal actions. When provided, the parent wires these to
  // close this detail dialog and open the sibling edit/terminate/purge
  // confirm dialog (close-then-open avoids stacked-dialog issues). The
  // render conditionals below mirror the row actions: Edit hidden for
  // Admin; Terminate only for a live agent; Purge only once terminated;
  // Send directive only for a live non-Admin agent.
  onEdit?: () => void
  onTerminate?: () => void
  onPurge?: () => void
  onSendDirective?: () => void
}) => {
  const [revealToken, setRevealToken] = useState(false)
  const [copied, setCopied] = useState(false)
  const [copiedToken, setCopiedToken] = useState(false)
  const [copiedSnippet, setCopiedSnippet] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<ClientTab>('claude-code')
  const { getAgentTasks } = useDataStore()

  // Hydrate active tab from localStorage on first mount. We
  // deliberately seed lazily (inside useEffect, not in useState) so
  // SSR doesn't crash on `localStorage`.
  useEffect(() => {
    if (typeof window === 'undefined') return
    try {
      const stored = window.localStorage.getItem(ACTIVE_TAB_STORAGE_KEY)
      if (stored && CLIENT_TABS.some((t) => t.value === stored)) {
        setActiveTab(stored as ClientTab)
      }
    } catch {
      // localStorage can be disabled (private browsing); fall through.
    }
  }, [])

  useEffect(() => {
    if (!open) {
      setRevealToken(false)
      setCopied(false)
      setCopiedToken(false)
      setCopiedSnippet(null)
    }
  }, [open])

  if (!agent) return null

  const currentTask = getAgentTasks(agent.agent_id).find(
    (t) => t.task_id === agent.current_task,
  )

  // Wave 7 PR 2 — presence drives the "Status" badge in the detail
  // header; same derivation as the agents-list row.
  const presence = agentPresence(agent)

  const copyAgentId = () => {
    navigator.clipboard.writeText(agent.agent_id)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  const mcpUrl = deriveMcpUrl()
  const snippetToken = agent.auth_token || ''

  const handleTabChange = (value: string) => {
    const next = value as ClientTab
    setActiveTab(next)
    if (typeof window !== 'undefined') {
      try {
        window.localStorage.setItem(ACTIVE_TAB_STORAGE_KEY, next)
      } catch {
        // localStorage disabled — silently no-op.
      }
    }
  }

  const handleCopySnippet = (key: string, content: string) => {
    navigator.clipboard.writeText(content)
    setCopiedSnippet(key)
    setTimeout(() => setCopiedSnippet(null), 1500)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/*
        Width: sm:!max-w-3xl (Tailwind important) beats the base
        DialogContent's sm:max-w-lg. Height capped at 90vh so a long
        snippet/notes block scrolls inside the modal instead of
        pushing it past the viewport. Body is the single scroll
        region via flex-1 min-h-0 overflow-y-auto.
      */}
      <DialogContent className="sm:!max-w-3xl w-[calc(100vw-2rem)] bg-card border-border text-card-foreground p-0 gap-0 max-h-[90vh] flex flex-col">
        <DialogHeader className="px-6 pt-6 pb-4 border-b border-border flex-shrink-0">
          <DialogTitle className="flex items-start justify-between pr-8 gap-3">
            {/* Title wraps up to 3 lines via line-clamp-3 break-words —
                NOT truncate, which silently drops chars from long
                agent_ids. */}
            <span className="text-lg font-semibold break-words line-clamp-3 leading-snug">
              Agent {agent.agent_id}
            </span>
            <div className="flex items-center gap-2 flex-shrink-0 pt-0.5">
              {/* Wave 7 PR 2 — presence badge mirrors the agents
                  list row. Hover for the rationale string. */}
              <Badge variant="outline" className="text-xs">
                {presence}
              </Badge>
            </div>
          </DialogTitle>
          <DialogDescription>
            All fields for{' '}
            <code className="font-mono [overflow-wrap:anywhere]">
              {agent.agent_id}
            </code>
            , plus copy-paste-ready MCP client config.
          </DialogDescription>
        </DialogHeader>

        {/*
          Scrollable body. flex-1 min-h-0 overflow-y-auto means this
          region expands to fill the remaining DialogContent height
          and is the single thing that scrolls — header + footer are
          flex-shrink-0 and stay pinned.
        */}
        <div className="px-6 py-4 flex-1 min-h-0 overflow-y-auto space-y-4 text-sm">
          {/* Group 1: identity + status */}
          <div className="grid grid-cols-3 gap-3">
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground uppercase tracking-wider">
                Agent ID
              </Label>
              <div className="font-mono text-sm [overflow-wrap:anywhere] flex items-center gap-2">
                <span>{agent.agent_id}</span>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={copyAgentId}
                  className="h-6 w-6 p-0 flex-shrink-0"
                  title="Copy agent id"
                >
                  <Copy className="h-3 w-3" />
                </Button>
                {copied && <span className="text-xs text-primary">copied</span>}
              </div>
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground uppercase tracking-wider">
                Status
              </Label>
              <div>
                {/* Wave 7 PR 2 — presence badge (derived). The
                    underlying row.status is shown below as
                    "Lifecycle" so the operator can still see the
                    legacy spawn-lifecycle column when it matters
                    (terminated rows, mostly). */}
                <Badge variant="outline">{presence}</Badge>
              </div>
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground uppercase tracking-wider">
                Created
              </Label>
              <div className="text-sm [overflow-wrap:anywhere]">
                {agent.created_at && agent.created_at !== 'N/A'
                  ? `${new Date(agent.created_at).toLocaleString()} (${formatRelative(agent.created_at, { emptyLabel: 'unknown' })})`
                  : 'N/A'}
              </div>
            </div>
          </div>

          {/* Wave 7 PR 2 — last MCP connection. ISO from
              session_registry.sessions_for_agent's most recent
              last_seen_at. Null when the agent has never opened a
              /mcp stream in this backend process (i.e. the operator
              registered the agent but hasn't pasted the snippet
              into the user's claude config yet). */}
          <div className="space-y-1">
            <Label className="text-xs text-muted-foreground uppercase tracking-wider">
              Last MCP connection
            </Label>
            <div className="text-sm [overflow-wrap:anywhere]">
              {agent.last_mcp_connection ? (
                <>
                  {new Date(agent.last_mcp_connection).toLocaleString()}{' '}
                  <span className="text-muted-foreground">
                    ({formatRelative(agent.last_mcp_connection, { emptyLabel: 'unknown' })})
                  </span>
                </>
              ) : (
                <span className="text-muted-foreground italic">
                  never connected — paste the .mcp.json snippet into
                  the user’s claude config to bring this agent online
                </span>
              )}
            </div>
          </div>

          {agent.terminated_at && (
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground uppercase tracking-wider">
                Terminated
              </Label>
              <div className="text-sm [overflow-wrap:anywhere]">
                {new Date(agent.terminated_at).toLocaleString()} (
                {formatRelative(agent.terminated_at, { emptyLabel: 'unknown' })})
              </div>
            </div>
          )}

          {/* Group 2: wd / color */}
          <div className="border-t border-border pt-4 space-y-3">
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground uppercase tracking-wider">
                Working Directory
              </Label>
              <div className="font-mono text-xs [overflow-wrap:anywhere]">
                {agent.working_directory || (
                  <span className="text-muted-foreground italic font-sans">unset</span>
                )}
              </div>
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground uppercase tracking-wider">
                Color
              </Label>
              <div className="flex items-center gap-2">
                {agent.color ? (
                  <>
                    <span
                      className="inline-block w-4 h-4 rounded border border-border"
                      style={{ backgroundColor: agent.color }}
                    />
                    <code className="font-mono text-xs">{agent.color}</code>
                  </>
                ) : (
                  <span className="text-muted-foreground italic">unset</span>
                )}
              </div>
            </div>
          </div>

          {/* Group: self-description (agent self-service profile,
              migration 0018). Shown here so an operator viewing an agent
              from the Agents page sees its profile, not only via the
              System-page graph node panel. */}
          <div className="border-t border-border pt-4 space-y-1">
            <Label className="text-xs text-muted-foreground uppercase tracking-wider">
              Self-description
            </Label>
            {/* Rendered as markdown in view mode (the Edit dialog keeps a
                raw textarea for authoring). Profiles are agent-authored →
                untrusted, so SafeMarkdown (no rehype-raw, allowlisted
                links) is the XSS-safe renderer — same one the Memories
                view uses. */}
            {agent.profile ? (
              <div className="[overflow-wrap:anywhere]">
                <SafeMarkdown source={agent.profile} />
              </div>
            ) : (
              <div className="text-sm">
                <span className="text-muted-foreground italic">
                  unset — the agent hasn’t written a profile yet
                </span>
              </div>
            )}
          </div>

          {/* Group 3: current task */}
          <div className="border-t border-border pt-4 space-y-1">
            <Label className="text-xs text-muted-foreground uppercase tracking-wider">
              Current Task
            </Label>
            <div className="text-sm">
              {currentTask ? (
                <button
                  className="text-primary hover:underline text-left [overflow-wrap:anywhere]"
                  onClick={() => onTaskClick(currentTask)}
                >
                  {currentTask.title}{' '}
                  <span className="text-xs text-muted-foreground font-mono">
                    ({currentTask.task_id.slice(-8)})
                  </span>
                </button>
              ) : (
                <span className="text-muted-foreground italic">none</span>
              )}
            </div>
          </div>

          {/* Group 4: token (32-hex blob; uses [overflow-wrap:anywhere]) */}
          <div className="border-t border-border pt-4 space-y-1">
            <Label className="text-xs text-muted-foreground uppercase tracking-wider">
              Token
            </Label>
            <div className="flex items-start gap-2 flex-wrap">
              {agent.auth_token ? (
                <>
                  <code className="font-mono text-xs [overflow-wrap:anywhere] flex-1 min-w-0">
                    {revealToken
                      ? agent.auth_token
                      : `...${agent.auth_token.slice(-4)}`}
                  </code>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setRevealToken((v) => !v)}
                    className="h-6 px-2 text-xs flex-shrink-0"
                  >
                    {revealToken ? 'Hide' : 'Reveal'}
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      navigator.clipboard.writeText(agent.auth_token || '')
                      setCopiedToken(true)
                      setTimeout(() => setCopiedToken(false), 1500)
                    }}
                    className="h-6 w-6 p-0 flex-shrink-0"
                    title="Copy token"
                  >
                    <Copy className="h-3 w-3" />
                  </Button>
                  {copiedToken && (
                    <span className="text-xs text-primary flex-shrink-0">copied</span>
                  )}
                </>
              ) : (
                <span className="text-muted-foreground italic">none</span>
              )}
            </div>
          </div>

          {/* Group 5: MCP-onboarding tabs ------------------------------
              One tab per supported client. Each tab body is a
              copy-paste-ready snippet wired to this agent's id +
              token + the path-prefix-derived URL. Active tab persists
              to localStorage under ACTIVE_TAB_STORAGE_KEY so a user's
              "I always use OpenCode" preference survives reloads. */}
          <div className="border-t border-border pt-4 space-y-2">
            <Label className="text-xs text-muted-foreground uppercase tracking-wider">
              Add as MCP server
            </Label>
            <p className="text-xs text-muted-foreground">
              Streamable HTTP transport (MCP spec rev 2025-03-26). Server name is
              the fixed <code>agent-mcp</code> (slash-command prefix
              <code>agent-mcp:</code>); .mcp.json entries are scoped per project.
            </p>
            {/*
              Tabs are expanded statically (one TabsTrigger / TabsContent
              per client) rather than .map()'d so the literal client
              values are greppable / regression-guard-friendly. The
              buildSnippetBlocks helper (lib/mcp-snippets.ts) still owns
              all the per-client config schema knowledge; this block just
              wires it up.

              Snippet format details:
              - Server name: the fixed string `agent-mcp` (matches the
                user's .claude.json convention → slash-command prefix
                `agent-mcp:`). A single fixed key is fine because
                .mcp.json entries are scoped per cwd/project.
              - URL: derived from window.location.origin +
                projectContext.apiPrefix + '/mcp' (path-prefix adapter,
                PR #56). The /mcp endpoint is Streamable HTTP per
                MCP spec rev 2025-03-26 (PR #61).
              - Authorization: Bearer <agent_token> on every snippet.
            */}
            <Tabs value={activeTab} onValueChange={handleTabChange} className="w-full">
              <TabsList className="flex flex-wrap h-auto justify-start gap-1">
                <TabsTrigger value="claude-code" className="text-xs">Claude Code</TabsTrigger>
                <TabsTrigger value="opencode" className="text-xs">OpenCode</TabsTrigger>
                <TabsTrigger value="cursor" className="text-xs">Cursor</TabsTrigger>
                <TabsTrigger value="cline" className="text-xs">Cline</TabsTrigger>
                <TabsTrigger value="zed" className="text-xs">Zed</TabsTrigger>
                <TabsTrigger value="continue" className="text-xs">Continue.dev</TabsTrigger>
                <TabsTrigger value="generic" className="text-xs">Generic JSON</TabsTrigger>
              </TabsList>
              <TabsContent value="claude-code" className="mt-2 space-y-3">
                {buildSnippetBlocks('claude-code', snippetToken, mcpUrl).map((block, i) => (
                  <SnippetBlock
                    key={i}
                    block={block}
                    copied={copiedSnippet === `claude-code:${i}`}
                    onCopy={() => handleCopySnippet(`claude-code:${i}`, block.content)}
                  />
                ))}
              </TabsContent>
              <TabsContent value="opencode" className="mt-2 space-y-3">
                {buildSnippetBlocks('opencode', snippetToken, mcpUrl).map((block, i) => (
                  <SnippetBlock
                    key={i}
                    block={block}
                    copied={copiedSnippet === `opencode:${i}`}
                    onCopy={() => handleCopySnippet(`opencode:${i}`, block.content)}
                  />
                ))}
              </TabsContent>
              <TabsContent value="cursor" className="mt-2 space-y-3">
                {buildSnippetBlocks('cursor', snippetToken, mcpUrl).map((block, i) => (
                  <SnippetBlock
                    key={i}
                    block={block}
                    copied={copiedSnippet === `cursor:${i}`}
                    onCopy={() => handleCopySnippet(`cursor:${i}`, block.content)}
                  />
                ))}
              </TabsContent>
              <TabsContent value="cline" className="mt-2 space-y-3">
                {buildSnippetBlocks('cline', snippetToken, mcpUrl).map((block, i) => (
                  <SnippetBlock
                    key={i}
                    block={block}
                    copied={copiedSnippet === `cline:${i}`}
                    onCopy={() => handleCopySnippet(`cline:${i}`, block.content)}
                  />
                ))}
              </TabsContent>
              <TabsContent value="zed" className="mt-2 space-y-3">
                {buildSnippetBlocks('zed', snippetToken, mcpUrl).map((block, i) => (
                  <SnippetBlock
                    key={i}
                    block={block}
                    copied={copiedSnippet === `zed:${i}`}
                    onCopy={() => handleCopySnippet(`zed:${i}`, block.content)}
                  />
                ))}
              </TabsContent>
              <TabsContent value="continue" className="mt-2 space-y-3">
                {buildSnippetBlocks('continue', snippetToken, mcpUrl).map((block, i) => (
                  <SnippetBlock
                    key={i}
                    block={block}
                    copied={copiedSnippet === `continue:${i}`}
                    onCopy={() => handleCopySnippet(`continue:${i}`, block.content)}
                  />
                ))}
              </TabsContent>
              <TabsContent value="generic" className="mt-2 space-y-3">
                {buildSnippetBlocks('generic', snippetToken, mcpUrl).map((block, i) => (
                  <SnippetBlock
                    key={i}
                    block={block}
                    copied={copiedSnippet === `generic:${i}`}
                    onCopy={() => handleCopySnippet(`generic:${i}`, block.content)}
                  />
                ))}
              </TabsContent>
            </Tabs>
          </div>
        </div>

        <DialogFooter className="px-6 py-4 border-t border-border flex-shrink-0">
          {onSendDirective && agent.status !== 'terminated' && agent.agent_id !== 'Admin' && (
            <Button type="button" variant="outline" size="sm" onClick={onSendDirective}>
              <Send className="h-4 w-4 mr-1" />
              Send directive
            </Button>
          )}
          {onEdit && agent.agent_id !== 'Admin' && (
            <Button type="button" variant="outline" size="sm" onClick={onEdit}>
              <Pencil className="h-4 w-4 mr-1" />
              Edit
            </Button>
          )}
          {onTerminate && agent.status !== 'terminated' && agent.agent_id !== 'Admin' && (
            <Button type="button" variant="destructive" size="sm" onClick={onTerminate}>
              <Trash2 className="h-4 w-4 mr-1" />
              Terminate
            </Button>
          )}
          {onPurge && agent.status === 'terminated' && agent.agent_id !== 'Admin' && (
            <Button type="button" variant="destructive" size="sm" onClick={onPurge}>
              <Trash2 className="h-4 w-4 mr-1" />
              Purge
            </Button>
          )}
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onOpenChange(false)}
          >
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
