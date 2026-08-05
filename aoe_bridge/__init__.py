"""agent-mcp ⟷ AoE delivery bridge (ADR-0021).

An Agent of Empires plugin worker that reverses the old agent-mcp→AoE push:
the bridge reaches OUT to agent-mcp. Per covered session it (a) holds an SSE
``/delivery/stream`` connection to agent-mcp with that session's token,
(b) reports the session's ``transport-status`` up, and (c) injects delivered
skinny frames into the session via AoE's REST routes — ``/send`` for
terminal (tmux) sessions, ``/acp/prompt`` for structured (ACP) sessions —
so the fallback works in BOTH modes.

Lives in the Agent-MCP repo (the user's call) but ships as an AoE plugin
(``aoe-plugin.toml`` + this Python worker). The pure pieces — frame
rendering and mode-aware injection request building — are unit-tested here;
the SSE client + AoE plugin JSON-RPC stdio glue wrap them.
"""

__all__ = ["render", "inject"]
