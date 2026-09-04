//! Shared string-length bounds for MCP tool-argument JSON schemas.
//!
//! Port of `agent_mcp/core/schema_limits.py` (R8-F1 — see that
//! module's docstring for the full pentest-finding rationale: an
//! unbounded string-typed schema property let a single request mint a
//! 200,000-char `agent_id` / persist a 500,000-char task title, a
//! resource-exhaustion / stored-bloat gap). Only the constant a real
//! Rust tool port actually needs so far is ported — add the rest of
//! Python's set here as later tool modules need them, rather than
//! porting the whole file speculatively now.

/// Short identifier / single-token fields: `context_key`, agent_id,
/// task_id, color, hostnames, project/backup names, session ids.
pub const IDENTIFIER_MAX_LEN: usize = 256;

/// Human-authored title fields: task titles, project display names.
pub const TITLE_MAX_LEN: usize = 512;

/// Path-shaped fields: filesystem paths passed to file-claim tools.
pub const PATH_MAX_LEN: usize = 4096;
