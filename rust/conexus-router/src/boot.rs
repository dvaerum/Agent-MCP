//! Boot sequence: bind-host resolution + the fail-closed single-tenant
//! safety guard, and the router's own DB path/open/init. Phase E2,
//! `conexus-router-shared-state` (PR23 step 1 of the app-wiring
//! breakdown). Port of `agent_mcp/router/app.py`'s
//! `_resolve_bind_host`/`_host_is_loopback`/`_assert_startup_safe`
//! plus `migrations_runner.py::get_router_db_path`.
//!
//! **Alembic stays the authoritative migration owner for a REAL
//! router.db** until every Python router is decommissioned (Phase F,
//! matching `conexus_db::schema`'s own module doc and
//! `conexus-backend::boot::open_and_init_db`'s identical precedent):
//! [`open_and_init_router_db`] calls `init_router_schema`, which is
//! `CREATE TABLE IF NOT EXISTS` -- a no-op against an already-migrated
//! database, exactly like Python's own `init_router_db()` (Alembic
//! upgrade, itself idempotent) being safe to re-run at every boot.

use std::path::PathBuf;

use anyhow::{Context, Result};
use rusqlite::Connection;

use crate::rate_limit;

/// Production default -- port of `_DEFAULT_ROUTER_DB`.
const DEFAULT_ROUTER_DB: &str = "/var/lib/agent-mcp/router.db";

/// Port of `migrations_runner.get_router_db_path`. `get_env` matches
/// this crate's own established convention (`rate_limit::
/// RateLimitConfig::resolve`) -- resolves fresh, no process-wide
/// cache.
pub fn router_db_path(get_env: impl Fn(&str) -> Option<String>) -> PathBuf {
    get_env("AGENT_MCP_ROUTER_DB")
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(DEFAULT_ROUTER_DB))
}

/// Open (creating if absent) the router DB and apply this crate's
/// schema.
pub fn open_and_init_router_db(path: &std::path::Path) -> Result<Connection> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("create router DB directory {}", parent.display()))?;
    }
    let conn = Connection::open(path)
        .with_context(|| format!("open router database {}", path.display()))?;
    conexus_db::schema::init_router_schema(&conn)
        .with_context(|| format!("initialize schema at {}", path.display()))?;
    Ok(conn)
}

/// A resolved bind host -- port of `_resolve_bind_host`'s own
/// single-string-or-list return shape. A comma-separated
/// `AGENT_MCP_ROUTER_HOST` binds MULTIPLE explicit hosts (tighter than
/// `0.0.0.0`); a single value stays a bare string; present-but-empty
/// (or unset, matching Python's own `default="127.0.0.1"`) resolves
/// to the documented default.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BindHost {
    /// One or more explicit hosts/IPs/UDS paths.
    Hosts(Vec<String>),
    /// Present-but-empty/whitespace-only -- binds every interface
    /// (`0.0.0.0` + `::`). Preserved as its own variant (not folded
    /// into an empty `Hosts(vec![])`) so [`host_is_loopback`] can fail
    /// closed on it explicitly, matching Python's own R6-F1 rationale.
    AllInterfaces,
}

/// Port of `_resolve_bind_host`. Whitespace-only entries are dropped;
/// an entirely-empty result (unset env var defaults to `"127.0.0.1"`,
/// matching Python's own `click` default) is distinguished from a
/// present-but-empty value.
pub fn resolve_bind_host(get_env: impl Fn(&str) -> Option<String>) -> BindHost {
    let raw = get_env("AGENT_MCP_ROUTER_HOST").unwrap_or_else(|| "127.0.0.1".to_string());
    let parts: Vec<String> = raw
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect();
    if parts.is_empty() {
        BindHost::AllInterfaces
    } else {
        BindHost::Hosts(parts)
    }
}

fn single_host_is_loopback(host: &str) -> bool {
    let host = host.trim();
    if host.is_empty() {
        return false;
    }
    if host.starts_with("unix:") || host.starts_with('/') {
        return true;
    }
    if host.eq_ignore_ascii_case("localhost") {
        return true;
    }
    host.parse::<std::net::IpAddr>()
        .map(|ip| ip.is_loopback())
        .unwrap_or(false)
}

/// Port of `_host_is_loopback`. `AllInterfaces` is never loopback
/// (R6-F1: aiohttp/axum bind `""`/unset to every interface, exactly
/// like an explicit `0.0.0.0` -- classifying it as loopback would let
/// the single-tenant guard pass while the runtime actually publishes
/// every interface).
pub fn host_is_loopback(host: &BindHost) -> bool {
    match host {
        BindHost::AllInterfaces => false,
        BindHost::Hosts(hosts) => {
            !hosts.is_empty() && hosts.iter().all(|h| single_host_is_loopback(h))
        }
    }
}

/// Port of `_assert_startup_safe`. Refuses to start (returns `Err`,
/// caller exits) on a single-tenant + non-loopback bind with no
/// explicit insecure-bind opt-in. The secure-cookie warning half
/// (non-loopback, no TLS signal) is intentionally NOT an error in
/// Python either -- logged as a `tracing::warn!` by the caller using
/// [`secure_cookie_warning`], not raised here.
pub fn assert_startup_safe(
    single_tenant_name: Option<&str>,
    host: &BindHost,
    get_env: impl Fn(&str) -> Option<String>,
) -> Result<(), String> {
    let loopback = host_is_loopback(host);
    let allow_insecure =
        rate_limit::env_truthy(get_env("AGENT_MCP_ALLOW_INSECURE_BIND").as_deref());
    if single_tenant_name.is_some() && !loopback && !allow_insecure {
        return Err(format!(
            "Refusing to start: single-tenant mode disables operator authentication, but the \
             router is binding a non-loopback host ({host:?}). This would publish an \
             unauthenticated admin dashboard to the network. Fix one of:\n  * bind loopback \
             (unset AGENT_MCP_ROUTER_HOST or set it to 127.0.0.1) and front the router with a \
             trusted reverse proxy, OR\n  * run in multi-tenant mode (drop --single-tenant) so \
             the operator-session gate is enforced, OR\n  * if this bind is genuinely isolated \
             (e.g. a qemu guest reachable only via host port-forwarding), set \
             AGENT_MCP_ALLOW_INSECURE_BIND=1 to acknowledge the risk."
        ));
    }
    Ok(())
}

/// `Some(warning message)` iff the bind is non-loopback with no TLS
/// signal (neither `AGENT_MCP_REQUIRE_SECURE_COOKIES` nor an
/// `https://` `AGENT_MCP_EXTERNAL_URL`) -- port of
/// `_assert_startup_safe`'s own non-fatal warning half. Split into
/// its own function (rather than a side-effecting `log::warn!` inside
/// `assert_startup_safe`) so the decision stays pure and testable;
/// the caller logs it.
pub fn secure_cookie_warning(
    host: &BindHost,
    get_env: impl Fn(&str) -> Option<String>,
) -> Option<String> {
    if host_is_loopback(host) {
        return None;
    }
    let require_secure =
        rate_limit::env_truthy(get_env("AGENT_MCP_REQUIRE_SECURE_COOKIES").as_deref());
    let https_signal = get_env("AGENT_MCP_EXTERNAL_URL")
        .map(|u| u.to_lowercase().starts_with("https://"))
        .unwrap_or(false);
    if require_secure || https_signal {
        return None;
    }
    Some(format!(
        "Router is binding a non-loopback host ({host:?}) with no TLS signal: \
         AGENT_MCP_REQUIRE_SECURE_COOKIES is unset and AGENT_MCP_EXTERNAL_URL is not https. \
         Session cookies will be set WITHOUT the Secure flag. If this deploy is \
         internet-facing, terminate TLS upstream and set AGENT_MCP_REQUIRE_SECURE_COOKIES=1."
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env_map(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |key: &str| map.get(key).cloned()
    }

    // -- router_db_path --------------------------------------------------

    #[test]
    fn router_db_path_defaults_to_the_production_path() {
        assert_eq!(
            router_db_path(env_map(&[])),
            PathBuf::from("/var/lib/agent-mcp/router.db")
        );
    }

    #[test]
    fn router_db_path_honours_the_override() {
        assert_eq!(
            router_db_path(env_map(&[("AGENT_MCP_ROUTER_DB", "/tmp/test-router.db")])),
            PathBuf::from("/tmp/test-router.db")
        );
    }

    // -- resolve_bind_host / host_is_loopback -----------------------------

    #[test]
    fn resolve_bind_host_defaults_to_loopback() {
        let host = resolve_bind_host(env_map(&[]));
        assert_eq!(host, BindHost::Hosts(vec!["127.0.0.1".to_string()]));
        assert!(host_is_loopback(&host));
    }

    #[test]
    fn resolve_bind_host_empty_value_binds_all_interfaces() {
        let host = resolve_bind_host(env_map(&[("AGENT_MCP_ROUTER_HOST", "")]));
        assert_eq!(host, BindHost::AllInterfaces);
        assert!(!host_is_loopback(&host));
    }

    #[test]
    fn resolve_bind_host_whitespace_only_binds_all_interfaces() {
        let host = resolve_bind_host(env_map(&[("AGENT_MCP_ROUTER_HOST", "   ")]));
        assert_eq!(host, BindHost::AllInterfaces);
    }

    #[test]
    fn resolve_bind_host_splits_a_comma_separated_multi_host_value() {
        let host = resolve_bind_host(env_map(&[(
            "AGENT_MCP_ROUTER_HOST",
            "127.0.0.1, 10.14.255.10",
        )]));
        assert_eq!(
            host,
            BindHost::Hosts(vec!["127.0.0.1".to_string(), "10.14.255.10".to_string()])
        );
        // Fail-closed: any non-loopback entry makes the WHOLE bind
        // non-loopback.
        assert!(!host_is_loopback(&host));
    }

    #[test]
    fn host_is_loopback_true_for_a_uds_path() {
        let host = BindHost::Hosts(vec!["/run/agent-mcp/router.sock".to_string()]);
        assert!(host_is_loopback(&host));
    }

    #[test]
    fn host_is_loopback_true_for_localhost_case_insensitive() {
        assert!(host_is_loopback(&BindHost::Hosts(vec![
            "LocalHost".to_string()
        ])));
    }

    #[test]
    fn host_is_loopback_false_for_a_public_ip() {
        assert!(!host_is_loopback(&BindHost::Hosts(vec![
            "0.0.0.0".to_string()
        ])));
        assert!(!host_is_loopback(&BindHost::Hosts(vec![
            "203.0.113.9".to_string()
        ])));
    }

    #[test]
    fn host_is_loopback_false_for_an_unresolvable_hostname() {
        // Fail-closed: a hostname that isn't a bare IP or "localhost"
        // is treated as a network bind, never assumed loopback.
        assert!(!host_is_loopback(&BindHost::Hosts(vec![
            "router.example.test".to_string()
        ])));
    }

    // -- assert_startup_safe ----------------------------------------------

    #[test]
    fn multi_tenant_mode_is_always_safe_regardless_of_bind() {
        let host = BindHost::AllInterfaces;
        assert!(assert_startup_safe(None, &host, env_map(&[])).is_ok());
    }

    #[test]
    fn single_tenant_on_loopback_is_safe() {
        let host = BindHost::Hosts(vec!["127.0.0.1".to_string()]);
        assert!(assert_startup_safe(Some("demo"), &host, env_map(&[])).is_ok());
    }

    #[test]
    fn single_tenant_on_a_public_bind_refuses_to_start() {
        let host = BindHost::AllInterfaces;
        let err = assert_startup_safe(Some("demo"), &host, env_map(&[])).unwrap_err();
        assert!(err.contains("Refusing to start"));
    }

    #[test]
    fn single_tenant_on_a_public_bind_is_allowed_with_the_explicit_opt_in() {
        let host = BindHost::AllInterfaces;
        assert!(assert_startup_safe(
            Some("demo"),
            &host,
            env_map(&[("AGENT_MCP_ALLOW_INSECURE_BIND", "true")])
        )
        .is_ok());
    }

    // -- secure_cookie_warning ---------------------------------------------

    #[test]
    fn no_warning_on_a_loopback_bind() {
        let host = BindHost::Hosts(vec!["127.0.0.1".to_string()]);
        assert!(secure_cookie_warning(&host, env_map(&[])).is_none());
    }

    #[test]
    fn warns_on_a_public_bind_with_no_tls_signal() {
        let host = BindHost::AllInterfaces;
        assert!(secure_cookie_warning(&host, env_map(&[])).is_some());
    }

    #[test]
    fn no_warning_with_require_secure_cookies_set() {
        let host = BindHost::AllInterfaces;
        assert!(secure_cookie_warning(
            &host,
            env_map(&[("AGENT_MCP_REQUIRE_SECURE_COOKIES", "true")])
        )
        .is_none());
    }

    #[test]
    fn no_warning_with_an_https_external_url() {
        let host = BindHost::AllInterfaces;
        assert!(secure_cookie_warning(
            &host,
            env_map(&[("AGENT_MCP_EXTERNAL_URL", "https://agent-mcp.example.test")])
        )
        .is_none());
    }
}
