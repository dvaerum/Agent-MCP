//! Settings + the `session → (endpoint, token, mode)` mapping resolver.
//!
//! ## Where the mapping comes from
//!
//! The bridge needs, per covered session, an agent-mcp **delivery endpoint**, a
//! per-session **bearer token**, and an injection **mode**. The token is the
//! SAME per-session agent-mcp token wired into that session's per-session MCP
//! config by the separate AoE per-session-MCP patch (ADR-0021). The plugin host
//! gives a worker no way to read another component's per-session MCP config, so
//! the bridge sources the mapping from **its own plugin settings**, read over
//! the `config.get` host RPC (which only ever returns this plugin's own table).
//!
//! Concretely: a `sessions` object-list setting, one row per covered session:
//! `{ session_id, token, endpoint?, mode? }`. The companion tooling that wires a
//! session's per-session MCP entry writes the matching row here with the same
//! token; until that automation lands an operator fills it in by hand. `endpoint`
//! falls back to a project-wide `default_endpoint`; `mode` defaults to `auto`,
//! inferred from the live `sessions.list` record.
//!
//! ## Assumptions (see README)
//! - `session_id` matches `sessions.list[].id`.
//! - `endpoint` is the agent-mcp project mount base, e.g.
//!   `https://host/api/<project>`; `/delivery/stream` and `/delivery/status`
//!   are appended.
//! - `token` == the session's agent-mcp bearer (also its MCP-tools token).
//! - `sessions.list` exposes no definitive terminal/structured flag, so `auto`
//!   is best-effort (it inspects `tool` + `status`); set `mode` explicitly to
//!   `structured` for ACP / CityHall / composer sessions.

use std::collections::HashMap;

use serde::Deserialize;
use serde_json::Value;

use crate::mode::{normalize_mode, Mode};
use crate::plugin::PluginConn;

/// Resolved plugin settings for one reconcile pass.
#[derive(Debug, Clone)]
pub struct Settings {
    pub enabled: bool,
    /// AoE serve REST base for injection, e.g. `http://127.0.0.1:8080`.
    pub aoe_base: String,
    /// AoE serve bearer token (injection auth).
    pub aoe_token: String,
    /// Fallback agent-mcp delivery base when a row omits `endpoint`.
    pub default_endpoint: String,
    /// How often to re-resolve routes and re-post transport-status.
    pub status_interval_secs: u64,
    /// Per-session mapping rows.
    pub sessions: Vec<SessionEntry>,
}

/// One row of the `sessions` object-list setting.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct SessionEntry {
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub token: String,
    #[serde(default)]
    pub endpoint: String,
    /// `auto` | `terminal` | `structured`.
    #[serde(default)]
    pub mode: String,
}

/// A live session as seen by `sessions.list` (only the fields we use).
#[derive(Debug, Clone, Default, Deserialize)]
pub struct SessionRecord {
    #[serde(default)]
    pub id: String,
    #[serde(default)]
    pub tool: String,
    #[serde(default)]
    pub status: String,
}

/// A fully-resolved delivery route for one session.
#[derive(Debug, Clone)]
pub struct Route {
    pub session_id: String,
    pub endpoint: String,
    pub token: String,
    pub mode: Mode,
    /// Whether the session is currently present in `sessions.list`.
    pub live: bool,
}

impl Route {
    /// A change in any of these means the running per-session task must be torn
    /// down and respawned (it captured the old values).
    pub fn fingerprint(&self, aoe_base: &str, aoe_token: &str) -> String {
        format!(
            "{}|{}|{}|{}|{}",
            self.endpoint,
            self.token,
            self.mode.as_str(),
            aoe_base,
            aoe_token
        )
    }
}

/// Resolve every configured session entry into a [`Route`], enriching mode and
/// liveness from the live `sessions.list` map. Rows without a usable
/// `(token, endpoint)` are dropped.
pub fn resolve_routes(settings: &Settings, live: &HashMap<String, SessionRecord>) -> Vec<Route> {
    let mut out = Vec::new();
    for entry in &settings.sessions {
        if entry.session_id.trim().is_empty() || entry.token.trim().is_empty() {
            continue;
        }
        let endpoint = if entry.endpoint.trim().is_empty() {
            settings.default_endpoint.trim().to_string()
        } else {
            entry.endpoint.trim().to_string()
        };
        if endpoint.is_empty() {
            continue;
        }
        let record = live.get(&entry.session_id);
        let mode = match entry.mode.trim().to_lowercase().as_str() {
            "terminal" => Mode::Terminal,
            "structured" => Mode::Structured,
            // "auto" (or unset/unknown): infer from the live record's
            // tool+status; default terminal when the session is not live.
            _ => match record {
                Some(r) => normalize_mode(&format!("{} {}", r.tool, r.status)),
                None => Mode::Terminal,
            },
        };
        out.push(Route {
            session_id: entry.session_id.clone(),
            endpoint,
            token: entry.token.clone(),
            mode,
            live: record.is_some(),
        });
    }
    out
}

/// Parse the raw `config.get` value of the `sessions` object-list into entries.
/// Non-array values and malformed rows are dropped (defensive: a bad row must
/// not sink the whole mapping).
pub fn parse_session_entries(value: &Value) -> Vec<SessionEntry> {
    match value.as_array() {
        Some(arr) => arr
            .iter()
            .filter_map(|row| serde_json::from_value::<SessionEntry>(row.clone()).ok())
            .collect(),
        None => Vec::new(),
    }
}

// ---- config.get plumbing -------------------------------------------------

/// The `value` field of a `config.get` response, or `Null` on any RPC error
/// (so a transient host hiccup falls back to defaults rather than crashing).
async fn get_value(conn: &PluginConn, key: &str) -> Value {
    match conn.config_get(key).await {
        Ok(resp) => resp.get("value").cloned().unwrap_or(Value::Null),
        Err(e) => {
            crate::log(&format!("config.get {key} failed: {e}"));
            Value::Null
        }
    }
}

/// Load all settings for one reconcile pass.
pub async fn load_settings(conn: &PluginConn) -> Settings {
    let enabled = get_value(conn, "enabled").await.as_bool().unwrap_or(true);
    let aoe_base = get_value(conn, "aoe_base")
        .await
        .as_str()
        .map(str::to_string)
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "http://127.0.0.1:8080".to_string());
    let aoe_token = get_value(conn, "aoe_token")
        .await
        .as_str()
        .map(str::to_string)
        .unwrap_or_default();
    let default_endpoint = get_value(conn, "default_endpoint")
        .await
        .as_str()
        .map(str::to_string)
        .unwrap_or_default();
    let status_interval_secs = get_value(conn, "status_interval_secs")
        .await
        .as_u64()
        .unwrap_or(30);
    let sessions = parse_session_entries(&get_value(conn, "sessions").await);

    Settings {
        enabled,
        aoe_base,
        aoe_token,
        default_endpoint,
        status_interval_secs,
        sessions,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn live_map(records: &[(&str, &str, &str)]) -> HashMap<String, SessionRecord> {
        records
            .iter()
            .map(|(id, tool, status)| {
                (
                    id.to_string(),
                    SessionRecord {
                        id: id.to_string(),
                        tool: tool.to_string(),
                        status: status.to_string(),
                    },
                )
            })
            .collect()
    }

    fn settings(sessions: Vec<SessionEntry>, default_endpoint: &str) -> Settings {
        Settings {
            enabled: true,
            aoe_base: "http://127.0.0.1:8080".to_string(),
            aoe_token: "aoe-tok".to_string(),
            default_endpoint: default_endpoint.to_string(),
            status_interval_secs: 30,
            sessions,
        }
    }

    #[test]
    fn parse_entries_drops_non_array_and_bad_rows() {
        assert!(parse_session_entries(&Value::Null).is_empty());
        let v = json!([
            { "session_id": "s1", "token": "t1", "endpoint": "https://e/api/p", "mode": "terminal" },
            { "session_id": "s2", "token": "t2" },
            42
        ]);
        let entries = parse_session_entries(&v);
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].session_id, "s1");
        assert_eq!(entries[1].mode, "");
    }

    #[test]
    fn resolve_uses_default_endpoint_and_infers_auto_mode() {
        let entries = vec![
            SessionEntry {
                session_id: "s1".into(),
                token: "t1".into(),
                endpoint: "".into(),
                mode: "auto".into(),
            },
            SessionEntry {
                session_id: "s2".into(),
                token: "t2".into(),
                endpoint: "https://override/api/p".into(),
                mode: "".into(),
            },
        ];
        let live = live_map(&[("s1", "claude", "acp running"), ("s2", "claude", "Running")]);
        let routes = resolve_routes(&settings(entries, "https://default/api/p"), &live);
        assert_eq!(routes.len(), 2);
        // s1: no per-row endpoint -> default; status mentions acp -> structured.
        assert_eq!(routes[0].endpoint, "https://default/api/p");
        assert_eq!(routes[0].mode, Mode::Structured);
        assert!(routes[0].live);
        // s2: explicit endpoint; terminal-ish status -> terminal.
        assert_eq!(routes[1].endpoint, "https://override/api/p");
        assert_eq!(routes[1].mode, Mode::Terminal);
    }

    #[test]
    fn explicit_mode_overrides_inference() {
        let entries = vec![SessionEntry {
            session_id: "s1".into(),
            token: "t1".into(),
            endpoint: "https://e/api/p".into(),
            mode: "structured".into(),
        }];
        // Live status looks terminal, but explicit "structured" wins.
        let live = live_map(&[("s1", "claude", "Running")]);
        let routes = resolve_routes(&settings(entries, ""), &live);
        assert_eq!(routes[0].mode, Mode::Structured);
    }

    #[test]
    fn rows_without_token_or_endpoint_are_dropped() {
        let entries = vec![
            SessionEntry {
                session_id: "s1".into(),
                token: "".into(),
                endpoint: "https://e/api/p".into(),
                mode: "".into(),
            },
            SessionEntry {
                session_id: "s2".into(),
                token: "t2".into(),
                endpoint: "".into(),
                mode: "".into(),
            },
        ];
        // default_endpoint empty -> s2 also unusable.
        let routes = resolve_routes(&settings(entries, ""), &HashMap::new());
        assert!(routes.is_empty());
    }

    #[test]
    fn configured_but_not_live_is_marked_not_live() {
        let entries = vec![SessionEntry {
            session_id: "ghost".into(),
            token: "t".into(),
            endpoint: "https://e/api/p".into(),
            mode: "auto".into(),
        }];
        let routes = resolve_routes(&settings(entries, ""), &HashMap::new());
        assert_eq!(routes.len(), 1);
        assert!(!routes[0].live);
        // No live signal -> auto defaults to terminal.
        assert_eq!(routes[0].mode, Mode::Terminal);
    }
}
