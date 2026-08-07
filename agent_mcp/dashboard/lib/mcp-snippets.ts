/**
 * MCP-onboarding snippet builder — the single owner of the
 * "add me as an MCP server" config strings.
 *
 * Extracted from `agents-dashboard.tsx` (architecture review § Cluster 7
 * / Deep-module opportunity A): the ~200 lines of per-client string
 * assembly lived inside the 2661-line god-file, so nothing but that file
 * could render a snippet and nothing could test the builder without
 * pulling in the store, the API client, and six sibling dialogs. It is
 * pure data-in/data-out, so it belongs in `lib/`, not in a component.
 *
 * The server name is the fixed string `agent-mcp` to match the user's
 * .claude.json convention (slash-command prefix `agent-mcp:`); a single
 * fixed key is fine because .mcp.json entries are scoped per cwd/project,
 * so the project scoping lives in the URL, not the key. URL is derived
 * from the path-prefix adapter (lib/project-context.ts, PR #56).
 * Transport is Streamable HTTP per MCP spec rev 2025-03-26 (PR #61) —
 * POST/GET/DELETE on /mcp with `Authorization: Bearer <agent_token>`.
 *
 * Client schemas were verified against current docs (2026-06):
 *   - OpenCode    https://opencode.ai/docs/mcp-servers
 *   - Cursor      https://cursor.com/docs/context/mcp
 *   - Cline       https://docs.cline.bot/mcp/configuring-mcp-servers
 *   - Zed         https://zed.dev/docs/ai/mcp
 *   - Continue    https://docs.continue.dev/customize/deep-dives/mcp
 *                 (YAML format under .continue/mcpServers/<name>.yaml)
 *   - Claude Code Anthropic CLI `claude mcp add --transport http`
 *
 * Generic JSON is a transport-agnostic fallback for clients we don't
 * explicitly support.
 */
import { projectContext } from "@/lib/project-context"
import { mcpUrl } from "@/lib/urls"

export type ClientTab =
  | 'claude-code'
  | 'opencode'
  | 'cursor'
  | 'cline'
  | 'zed'
  | 'continue'
  | 'generic'

export const CLIENT_TABS: ReadonlyArray<{ value: ClientTab; label: string }> = [
  { value: 'claude-code', label: 'Claude Code' },
  { value: 'opencode', label: 'OpenCode' },
  { value: 'cursor', label: 'Cursor' },
  { value: 'cline', label: 'Cline' },
  { value: 'zed', label: 'Zed' },
  { value: 'continue', label: 'Continue.dev' },
  { value: 'generic', label: 'Generic JSON' },
]

/** localStorage key for the operator's sticky "preferred client" tab. */
export const ACTIVE_TAB_STORAGE_KEY = 'agent-mcp-popup-active-client'

/**
 * Derive the public MCP endpoint URL for this dashboard's project.
 *
 * Under path-prefix deployments (the production shape) the dashboard
 * loads from `/agent-mcp/app/<name>/...` and the MCP transport lives
 * at `<origin>/agent-mcp/<name>/mcp` (PR-D will move it under the
 * top-level /mcp/ prefix). Standalone deployments (single-tenant, no
 * path prefix) expose the MCP transport at `<origin>/mcp` directly.
 *
 * Pre-PR-B this function had a bug (audit §1.1): it concatenated
 * `apiPrefix + "/mcp"`, which produced `/agent-mcp/__api/<name>/mcp`
 * — a path that doesn't exist (the MCP route is a sibling of __api,
 * not a child). PR-B routes the URL build through ``mcpUrl()`` in
 * ``lib/urls.ts`` so the next URL rename (PR-D) is a one-line change.
 */
export function deriveMcpUrl(): string {
  const origin = typeof window !== 'undefined' ? window.location.origin : ''
  if (projectContext.projectName) {
    return mcpUrl(projectContext.projectName, origin)
  }
  return `${origin}/mcp`
}

/**
 * A single independently-copyable config block. `content` is the ONLY
 * text the Copy button writes to the clipboard — clean, no leading
 * #/// comment markers, one thing per block. `note` is muted helper
 * prose (where/how to use it) that is rendered but NEVER copied.
 */
export type SnippetPart = {
  title: string // short label, e.g. "CLI command", "opencode.json"
  note?: string // where/how to use it — rendered as muted helper text
  content: string // the clean text Copy writes to the clipboard
}

export function buildSnippetBlocks(tab: ClientTab, token: string, url: string): SnippetPart[] {
  const name = 'agent-mcp'
  const t = token || '<AGENT_TOKEN>'
  switch (tab) {
    case 'claude-code':
      return [
        {
          title: 'CLI command',
          note: 'One-shot add via the Claude Code CLI.',
          content: `claude mcp add --transport http ${name} ${url} --header "Authorization: Bearer ${t}"`,
        },
        {
          title: 'JSON',
          note: 'Paste into ~/.claude.json under `mcpServers` (user-scope) or `projects["<cwd>"].mcpServers` (project-scope).',
          content: [
            '"' + name + '": {',
            '  "type": "http",',
            `  "url": "${url}",`,
            `  "headers": {"Authorization": "Bearer ${t}"}`,
            '}',
          ].join('\n'),
        },
      ]
    case 'opencode':
      // Verified against https://opencode.ai/docs/mcp-servers
      // Lives in opencode.json (project root) or
      // ~/.config/opencode/opencode.json (user-scope).
      return [
        {
          title: 'opencode.json',
          note: 'Project-root opencode.json — or ~/.config/opencode/opencode.json (user-scope).',
          content: [
            '{',
            '  "$schema": "https://opencode.ai/config.json",',
            '  "mcp": {',
            `    "${name}": {`,
            '      "type": "remote",',
            `      "url": "${url}",`,
            '      "enabled": true,',
            `      "headers": {"Authorization": "Bearer ${t}"}`,
            '    }',
            '  }',
            '}',
          ].join('\n'),
        },
      ]
    case 'cursor':
      // Verified against https://cursor.com/docs/context/mcp
      // Lives in .cursor/mcp.json (project) or ~/.cursor/mcp.json
      // (global).
      return [
        {
          title: '.cursor/mcp.json',
          note: 'Project .cursor/mcp.json — or ~/.cursor/mcp.json (global).',
          content: [
            '{',
            '  "mcpServers": {',
            `    "${name}": {`,
            `      "url": "${url}",`,
            `      "headers": {"Authorization": "Bearer ${t}"}`,
            '    }',
            '  }',
            '}',
          ].join('\n'),
        },
      ]
    case 'cline':
      // Verified against https://docs.cline.bot/mcp/configuring-mcp-servers
      // CLI: ~/.cline/mcp.json. IDE extensions: MCP Settings JSON
      // via the Configure tab.
      return [
        {
          title: 'Cline MCP settings',
          note: 'CLI ~/.cline/mcp.json — or the MCP Settings JSON in the Configure tab of the VS Code / IDE extension.',
          content: [
            '{',
            '  "mcpServers": {',
            `    "${name}": {`,
            `      "url": "${url}",`,
            `      "headers": {"Authorization": "Bearer ${t}"},`,
            '      "disabled": false,',
            '      "autoApprove": []',
            '    }',
            '  }',
            '}',
          ].join('\n'),
        },
      ]
    case 'zed':
      // Verified against https://zed.dev/docs/ai/mcp
      // Lives in ~/.config/zed/settings.json under `context_servers`.
      return [
        {
          title: 'Zed settings.json',
          note: '~/.config/zed/settings.json — under context_servers.',
          content: [
            '{',
            '  "context_servers": {',
            `    "${name}": {`,
            `      "url": "${url}",`,
            `      "headers": {"Authorization": "Bearer ${t}"}`,
            '    }',
            '  }',
            '}',
          ].join('\n'),
        },
      ]
    case 'continue':
      // Verified against https://docs.continue.dev/customize/deep-dives/mcp
      // Continue uses per-server YAML files under
      // `.continue/mcpServers/<server>.yaml`. The docs we could verify
      // do not show explicit Authorization-header syntax for HTTP
      // transports — the `requestOptions.headers` form below matches
      // the broader Continue config convention; verify against your
      // installed Continue version.
      return [
        {
          title: `.continue/mcpServers/${name}.yaml`,
          note: 'Authorization-header syntax for HTTP MCP servers in Continue is not explicitly documented — verify against your installed version.',
          content: [
            'mcpServers:',
            `  - name: ${name}`,
            '    type: streamable-http',
            `    url: ${url}`,
            '    requestOptions:',
            '      headers:',
            `        Authorization: "Bearer ${t}"`,
          ].join('\n'),
        },
      ]
    case 'generic':
      return [
        {
          title: 'Generic JSON',
          note: "Transport-agnostic — adapt to your client's schema.",
          content: [
            '{',
            `  "name": "${name}",`,
            `  "url": "${url}",`,
            '  "transport": "http",',
            `  "headers": {"Authorization": "Bearer ${t}"}`,
            '}',
          ].join('\n'),
        },
      ]
  }
}
