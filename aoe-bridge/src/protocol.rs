//! Newline-delimited JSON-RPC 2.0 wire types for the AoE plugin worker
//! protocol.
//!
//! The channel is **bidirectional peer-to-peer** over the worker's stdio (see
//! [`crate::plugin`]). Over the same pipe the worker both:
//!
//! - initiates **requests** to the host (`sessions.list`, `config.get`,
//!   `ui.notify`) and reads their **responses**, and
//! - receives host-initiated **requests / notifications** (a `status` command,
//!   `plugin.settings.changed`) and answers the ones that carry an `id`.
//!
//! A single incoming line can therefore be either a request/notification (it
//! has a `method`) or a response to one of our requests (it has an `id` plus
//! `result`/`error` and no `method`). [`Incoming`] is deliberately permissive
//! so one deserialize covers both; the reader in [`crate::plugin`] demuxes on
//! the presence of `method`.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

/// JSON-RPC error codes we emit for host-initiated requests we can't serve.
pub mod codes {
    pub const METHOD_NOT_FOUND: i64 = -32601;
    #[allow(dead_code)]
    pub const INTERNAL_ERROR: i64 = -32603;
}

/// A JSON-RPC error object as it appears in a response.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RpcError {
    pub code: i64,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

/// One inbound line. Every field is optional so a request, a notification, and
/// a response all deserialize through this one type; the reader classifies by
/// which fields are populated (`method` present ⇒ request/notification, else a
/// response keyed by `id`).
#[derive(Debug, Clone, Deserialize)]
pub struct Incoming {
    #[serde(default)]
    #[allow(dead_code)]
    pub jsonrpc: Option<String>,
    #[serde(default)]
    pub id: Option<Value>,
    #[serde(default)]
    pub method: Option<String>,
    #[serde(default)]
    pub params: Value,
    #[serde(default)]
    pub result: Option<Value>,
    #[serde(default)]
    pub error: Option<RpcError>,
}

/// Parse one ndjson line. Blank/whitespace-only lines are `Ok(None)` (skipped);
/// malformed JSON is an error the caller logs (never fatal to the worker — a
/// single bad host line must not take the worker down).
pub fn parse_incoming(line: &str) -> Result<Option<Incoming>, serde_json::Error> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return Ok(None);
    }
    serde_json::from_str(trimmed).map(Some)
}

fn to_line(v: Value) -> String {
    let mut s = v.to_string();
    s.push('\n');
    s
}

/// Build a worker→host request line (we always use numeric ids).
pub fn request_line(id: u64, method: &str, params: Value) -> String {
    to_line(json!({ "jsonrpc": "2.0", "id": id, "method": method, "params": params }))
}

/// Build a success response to a host-initiated request.
pub fn success_line(id: Value, result: Value) -> String {
    to_line(json!({ "jsonrpc": "2.0", "id": id, "result": result }))
}

/// Build an error response to a host-initiated request.
pub fn error_line(id: Value, code: i64, message: &str) -> String {
    to_line(json!({ "jsonrpc": "2.0", "id": id, "error": { "code": code, "message": message } }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blank_line_is_skipped() {
        assert!(parse_incoming("   ").unwrap().is_none());
        assert!(parse_incoming("").unwrap().is_none());
    }

    #[test]
    fn classifies_request_by_method() {
        let m = parse_incoming(r#"{"jsonrpc":"2.0","id":1,"method":"plugin.x.status","params":{}}"#)
            .unwrap()
            .unwrap();
        assert_eq!(m.method.as_deref(), Some("plugin.x.status"));
        assert!(m.error.is_none());
        assert!(m.result.is_none());
    }

    #[test]
    fn classifies_response_without_method() {
        let m = parse_incoming(r#"{"jsonrpc":"2.0","id":7,"result":{"ok":true}}"#)
            .unwrap()
            .unwrap();
        assert!(m.method.is_none());
        assert_eq!(m.id.as_ref().and_then(|v| v.as_u64()), Some(7));
        assert_eq!(m.result.unwrap()["ok"], serde_json::json!(true));
    }

    #[test]
    fn classifies_error_response() {
        let m = parse_incoming(r#"{"jsonrpc":"2.0","id":9,"error":{"code":-32601,"message":"no"}}"#)
            .unwrap()
            .unwrap();
        assert!(m.method.is_none());
        let e = m.error.unwrap();
        assert_eq!(e.code, -32601);
    }

    #[test]
    fn lines_are_single_ndjson() {
        let l = request_line(3, "sessions.list", json!({}));
        assert!(l.ends_with('\n'));
        assert_eq!(l.matches('\n').count(), 1);
        let v: Value = serde_json::from_str(l.trim()).unwrap();
        assert_eq!(v["id"], json!(3));
        assert_eq!(v["method"], json!("sessions.list"));
    }
}
