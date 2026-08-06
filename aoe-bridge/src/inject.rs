//! The mode-aware AoE REST injector — pure request building.
//!
//! Given a mode and a session id, produce the `(url, json body)` for AoE's
//! localhost REST surface. These are the two routes the plugin host exposes for
//! driving a session (verified against the real handlers):
//!
//! - terminal → `POST {aoe_base}/api/sessions/{id}/send` with
//!   `{"message": <text>, "revive": true}` (SendMessageRequest).
//! - structured → `POST {aoe_base}/api/sessions/{id}/acp/prompt` with
//!   `{"text": <text>}` (PromptRequest — the field is `text`, not `prompt`).
//!
//! Both are authenticated with `Authorization: Bearer <aoe_serve_token>`; the
//! header is added by the async sender in `bridge.rs`, not here.

use serde_json::{json, Value};

use crate::mode::Mode;

fn trim_end(s: &str) -> &str {
    s.trim_end_matches('/')
}

/// Build the injection `(url, body)` for a session, dispatched on mode.
pub fn build_injection(
    mode: Mode,
    aoe_base: &str,
    session_id: &str,
    text: &str,
) -> (String, Value) {
    let base = trim_end(aoe_base);
    match mode {
        Mode::Terminal => (
            format!("{base}/api/sessions/{session_id}/send"),
            json!({ "message": text, "revive": true }),
        ),
        Mode::Structured => (
            format!("{base}/api/sessions/{session_id}/acp/prompt"),
            json!({ "text": text }),
        ),
    }
}

/// Join a delivery endpoint base with a sub-path (`delivery/stream`,
/// `delivery/status`), tolerant of a trailing/leading slash on either side.
pub fn delivery_url(endpoint: &str, path: &str) -> String {
    format!("{}/{}", trim_end(endpoint), path.trim_start_matches('/'))
}

/// The fixed server name for the injected agent-mcp entry in a session's
/// per-session MCP layer. The bridge owns this one entry (a full-replace set),
/// so the name is stable.
pub const MCP_SERVER_NAME: &str = "agent-mcp";

/// Build the `session.mcp.set` params that inject agent-mcp's tools into a
/// session as a single http MCP server. `servers` is the COMPLETE per-session
/// layer, so we send exactly our one entry (the bridge owns this layer).
///
/// An empty `token` omits the `Authorization` header entirely — for an
/// agent-mcp reachable without auth (e.g. a `--auth=none` deploy). The AoE
/// host DTO uses `deny_unknown_fields`, so we emit only the four recognised
/// keys (`name`/`transport`/`url`/`headers`) and drop `headers` when empty.
pub fn build_mcp_set_params(session_id: &str, mcp_url: &str, token: &str) -> Value {
    let mut server = json!({
        "name": MCP_SERVER_NAME,
        "transport": "http",
        "url": mcp_url,
    });
    if !token.trim().is_empty() {
        server["headers"] = json!({ "Authorization": format!("Bearer {token}") });
    }
    json!({ "session_id": session_id, "servers": [server] })
}

/// Extract the concatenated `data:` payload from one SSE event block (the text
/// between blank-line separators). Returns `None` for a block with no data
/// lines (e.g. a bare `: ping` comment keepalive).
pub fn extract_sse_data(block: &str) -> Option<String> {
    let mut data: Vec<&str> = Vec::new();
    for line in block.lines() {
        if let Some(rest) = line.strip_prefix("data:") {
            // A single optional leading space after the colon is stripped per
            // the SSE spec; further spaces are significant.
            data.push(rest.strip_prefix(' ').unwrap_or(rest));
        }
    }
    if data.is_empty() {
        None
    } else {
        Some(data.join("\n"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terminal_injection_targets_send_with_revive() {
        let (url, body) = build_injection(Mode::Terminal, "http://127.0.0.1:8080", "sid-1", "hi");
        assert_eq!(url, "http://127.0.0.1:8080/api/sessions/sid-1/send");
        assert_eq!(body, json!({ "message": "hi", "revive": true }));
    }

    #[test]
    fn structured_injection_targets_acp_prompt_with_text() {
        let (url, body) =
            build_injection(Mode::Structured, "http://127.0.0.1:8080/", "sid-2", "yo");
        // Trailing slash on the base is tolerated.
        assert_eq!(url, "http://127.0.0.1:8080/api/sessions/sid-2/acp/prompt");
        assert_eq!(body, json!({ "text": "yo" }));
    }

    #[test]
    fn delivery_url_joins_cleanly() {
        assert_eq!(
            delivery_url("https://mcp.example/api/proj", "delivery/stream"),
            "https://mcp.example/api/proj/delivery/stream"
        );
        assert_eq!(
            delivery_url("https://mcp.example/api/proj/", "/delivery/status"),
            "https://mcp.example/api/proj/delivery/status"
        );
    }

    #[test]
    fn mcp_set_params_with_token_carry_bearer_header() {
        let p = build_mcp_set_params("sid-1", "https://host/agent-mcp/mcp/proj", "tok123");
        assert_eq!(p["session_id"], json!("sid-1"));
        let server = &p["servers"][0];
        assert_eq!(server["name"], json!("agent-mcp"));
        assert_eq!(server["transport"], json!("http"));
        assert_eq!(server["url"], json!("https://host/agent-mcp/mcp/proj"));
        assert_eq!(server["headers"]["Authorization"], json!("Bearer tok123"));
        // Only the four recognised keys (deny_unknown_fields on the host).
        let keys: Vec<&String> = server.as_object().unwrap().keys().collect();
        assert_eq!(keys.len(), 4);
    }

    #[test]
    fn mcp_set_params_without_token_omit_headers() {
        let p = build_mcp_set_params("sid-2", "http://127.0.0.1:8001/mcp/p", "  ");
        let server = &p["servers"][0];
        assert!(server.get("headers").is_none());
        // name/transport/url only.
        assert_eq!(server.as_object().unwrap().len(), 3);
    }

    #[test]
    fn sse_data_extraction() {
        assert_eq!(
            extract_sse_data("data: {\"a\":1}\n"),
            Some("{\"a\":1}".to_string())
        );
        // Multi-line data folds with newlines.
        assert_eq!(
            extract_sse_data("data: line1\ndata: line2\n"),
            Some("line1\nline2".to_string())
        );
        // A comment-only block yields nothing.
        assert_eq!(extract_sse_data(": ping\n"), None);
    }
}
