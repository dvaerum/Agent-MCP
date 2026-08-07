/**
 * Unit tests for the MCP-onboarding snippet builder.
 *
 * Pre-extraction these strings lived inside the 2661-line
 * `agents-dashboard.tsx`, so the only way to check "does the Cursor
 * snippet actually carry the bearer token" was to render the whole
 * Agents page. Now that `buildSnippetBlocks` is a pure function in
 * `lib/`, the per-client config contract gets a real test surface.
 */
import { describe, expect, it } from "vitest"

import {
  CLIENT_TABS,
  buildSnippetBlocks,
  type ClientTab,
} from "@/lib/mcp-snippets"

const URL = "https://example.test/agent-mcp/mcp/proj"
const TOKEN = "0123456789abcdef0123456789abcdef"

describe("buildSnippetBlocks", () => {
  it("returns at least one copyable block for every advertised client tab", () => {
    for (const { value } of CLIENT_TABS) {
      const blocks = buildSnippetBlocks(value, TOKEN, URL)
      expect(blocks.length, `no blocks for ${value}`).toBeGreaterThan(0)
      for (const block of blocks) {
        expect(block.title).toBeTruthy()
        expect(block.content).toBeTruthy()
      }
    }
  })

  it("embeds the agent token as an Authorization: Bearer header everywhere", () => {
    for (const { value } of CLIENT_TABS) {
      for (const block of buildSnippetBlocks(value, TOKEN, URL)) {
        expect(block.content, `missing bearer in ${value}`).toContain(
          `Bearer ${TOKEN}`,
        )
      }
    }
  })

  it("points every snippet at the supplied MCP endpoint URL", () => {
    for (const { value } of CLIENT_TABS) {
      for (const block of buildSnippetBlocks(value, TOKEN, URL)) {
        expect(block.content, `missing url in ${value}`).toContain(URL)
      }
    }
  })

  it("uses the fixed `agent-mcp` server key, never a per-agent one", () => {
    for (const { value } of CLIENT_TABS) {
      for (const block of buildSnippetBlocks(value, TOKEN, URL)) {
        expect(block.content).toContain("agent-mcp")
        expect(block.content).not.toMatch(/agent-mcp-[0-9a-z]/)
      }
    }
  })

  it("falls back to a <AGENT_TOKEN> placeholder when the agent has no token", () => {
    const blocks = buildSnippetBlocks("claude-code", "", URL)
    expect(blocks[0].content).toContain("Bearer <AGENT_TOKEN>")
  })

  it("declares Streamable HTTP transport for the Claude Code tab", () => {
    const [cli, json] = buildSnippetBlocks("claude-code", TOKEN, URL)
    expect(cli.content).toContain("claude mcp add --transport http agent-mcp")
    expect(json.content).toContain('"type": "http"')
  })

  it("uses each client's own config schema (not one shape for all)", () => {
    const shapes: Record<Exclude<ClientTab, "claude-code">, string> = {
      opencode: '"mcp": {',
      cursor: '"mcpServers": {',
      cline: '"autoApprove": []',
      zed: '"context_servers": {',
      continue: "type: streamable-http",
      generic: '"transport": "http"',
    }
    for (const [tab, marker] of Object.entries(shapes)) {
      const joined = buildSnippetBlocks(tab as ClientTab, TOKEN, URL)
        .map((b) => b.content)
        .join("\n")
      expect(joined, `${tab} lost its schema marker`).toContain(marker)
    }
  })

  it("keeps helper prose out of the copyable content", () => {
    // `note` is rendered but never written to the clipboard — a note
    // leaking into `content` would paste comment prose into a JSON file.
    for (const { value } of CLIENT_TABS) {
      for (const block of buildSnippetBlocks(value, TOKEN, URL)) {
        if (block.note) {
          expect(block.content).not.toContain(block.note)
        }
      }
    }
  })
})
