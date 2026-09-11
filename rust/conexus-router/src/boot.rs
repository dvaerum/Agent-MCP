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

/// Port of `identity.py::init_router_db`'s env-var-bootstrap half
/// (Phase F, prancy-napping-pie -- found missing while retiring the
/// Python router: this path was documented in `login.rs`'s own module
/// doc as "app-wiring's job (PR 23)" but PR 23 never actually wired
/// it, a real gap invisible until the Python router -- which DID
/// implement it -- was retired and a VM test's env-var-seeded
/// `ci-sentinel` login started failing for real).
///
/// Bootstrap fires only when both `AGENT_MCP_BOOTSTRAP_USERNAME` and
/// `AGENT_MCP_BOOTSTRAP_PASSWORD` are set (one alone is a typo, not a
/// half-bootstrap) AND the `users` table is empty. Both env vars are
/// stripped from the process environment afterwards regardless of
/// outcome (mirrors Python's own `try/finally` -- a leaked bootstrap
/// password sitting in this process's env for its whole lifetime is
/// exactly the exposure surface the original fix existed to close).
///
/// Deliberately simpler than Python's own two-step dance
/// (`create_user()` then a separate `bootstrap_first_operator_as_
/// sysadmin()` promotion pass): this crate's `identity::create_user`
/// already takes `bootstrap_sysadmin` as a first-class parameter
/// (Phase G, `conexus-cli router create-operator`'s own established
/// call shape), so the empty-table check and the sysadmin crowning
/// happen atomically in one call, not two.
///
/// `unset_env` mirrors `get_env`'s own explicit-closure convention
/// (every function in this module takes reads this way; nothing here
/// reaches into real process env directly) -- production passes
/// `std::env::remove_var`, tests pass a fake that records calls
/// instead of mutating real global state, avoiding the exact
/// shared-global-under-parallel-tests hazard this workspace has hit
/// (and fixed) twice before.
pub async fn bootstrap_operator_from_env(
    db: &sea_orm::DatabaseConnection,
    registered_projects: &[String],
    get_env: impl Fn(&str) -> Option<String>,
    unset_env: impl Fn(&str),
) -> Result<()> {
    let username = get_env("AGENT_MCP_BOOTSTRAP_USERNAME");
    let password = get_env("AGENT_MCP_BOOTSTRAP_PASSWORD");

    let result = if let (Some(username), Some(password)) = (&username, &password) {
        bootstrap_operator(db, username, password, registered_projects).await
    } else {
        Ok(())
    };

    // Strip even on error/never-attempted-but-one-var-set -- a
    // present bootstrap password must never survive into this
    // process's later lifetime regardless of what happened.
    unset_env("AGENT_MCP_BOOTSTRAP_USERNAME");
    unset_env("AGENT_MCP_BOOTSTRAP_PASSWORD");

    result
}

async fn bootstrap_operator(
    db: &sea_orm::DatabaseConnection,
    username: &str,
    password: &str,
    registered_projects: &[String],
) -> Result<()> {
    if !crate::identity::users_table_is_empty(db)
        .await
        .context("check users table emptiness for bootstrap")?
    {
        eprintln!(
            "conexus-router: bootstrap env vars set, but the users table is non-empty -- \
             skipping bootstrap."
        );
        return Ok(());
    }

    // Canonical single-source policy check -- every path that mints a
    // NEW operator password calls this first (matches Python's own
    // rationale and this crate's `conexus-cli router create-operator`
    // precedent).
    crate::identity::validate_password_strength(password)
        .map_err(|e| anyhow::anyhow!("{e}"))
        .context("bootstrap password failed the strength policy")?;

    let now = chrono::Utc::now().to_rfc3339();
    crate::identity::create_user(
        db,
        username,
        password,
        None,
        false,
        true,
        registered_projects,
        &now,
    )
    .await
    .map_err(|e| anyhow::anyhow!("{e}"))
    .context("create bootstrap operator")?;

    eprintln!(
        "conexus-router: bootstrapped first operator {username:?} from \
         AGENT_MCP_BOOTSTRAP_USERNAME/PASSWORD env vars."
    );
    Ok(())
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

    // -- bootstrap_operator_from_env -------------------------------------
    //
    // Phase F regression coverage: this is the exact gap a real Nix VM
    // test caught (a Python-router-only feature, never ported, only
    // documented as "someone else's job"). A file-backed sea-orm DB is
    // needed (not `:memory:`) so `identity::create_user`'s own writes
    // and this test's own `users_table_is_empty` read-back see the
    // same database.

    async fn router_db() -> (tempfile::TempDir, sea_orm::DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("boot_test.db");
        let conn = Connection::open(&path).unwrap();
        conexus_db::schema::init_router_schema(&conn).unwrap();
        drop(conn);
        let db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, db)
    }

    /// Records every key `unset_env` was called with, standing in for
    /// `std::env::remove_var` without touching real process state.
    fn recording_unsetter() -> (std::sync::Arc<std::sync::Mutex<Vec<String>>>, impl Fn(&str)) {
        let calls = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let calls_clone = std::sync::Arc::clone(&calls);
        (calls, move |key: &str| {
            calls_clone.lock().unwrap().push(key.to_string());
        })
    }

    #[tokio::test]
    async fn bootstraps_the_first_operator_when_both_vars_are_set_and_the_table_is_empty() {
        let (_dir, db) = router_db().await;
        let (calls, unset) = recording_unsetter();

        bootstrap_operator_from_env(
            &db,
            &["proj-a".to_string()],
            env_map(&[
                ("AGENT_MCP_BOOTSTRAP_USERNAME", "ci-sentinel"),
                (
                    "AGENT_MCP_BOOTSTRAP_PASSWORD",
                    "correct horse battery staple",
                ),
            ]),
            unset,
        )
        .await
        .unwrap();

        assert!(!crate::identity::users_table_is_empty(&db).await.unwrap());
        let user = crate::identity::get_user_by_username(&db, "ci-sentinel")
            .await
            .unwrap()
            .expect("bootstrapped user should be readable back");
        assert!(
            user.is_sysadmin,
            "the first operator must be crowned sysadmin"
        );
        assert_eq!(
            *calls.lock().unwrap(),
            vec![
                "AGENT_MCP_BOOTSTRAP_USERNAME".to_string(),
                "AGENT_MCP_BOOTSTRAP_PASSWORD".to_string()
            ],
            "both bootstrap env vars must be unset after a successful bootstrap"
        );
    }

    #[tokio::test]
    async fn skips_silently_when_the_users_table_is_already_non_empty() {
        let (_dir, db) = router_db().await;
        crate::identity::create_user(
            &db,
            "existing-operator",
            "an-already-strong-password",
            None,
            false,
            true,
            &[],
            "2026-01-01T00:00:00.000+00:00",
        )
        .await
        .unwrap();
        let (calls, unset) = recording_unsetter();

        bootstrap_operator_from_env(
            &db,
            &[],
            env_map(&[
                ("AGENT_MCP_BOOTSTRAP_USERNAME", "ci-sentinel"),
                (
                    "AGENT_MCP_BOOTSTRAP_PASSWORD",
                    "correct horse battery staple",
                ),
            ]),
            unset,
        )
        .await
        .unwrap();

        assert!(
            crate::identity::get_user_by_username(&db, "ci-sentinel")
                .await
                .unwrap()
                .is_none(),
            "must not create a second operator once the table is non-empty"
        );
        // Still unset -- a skip is not an error, the vars must not
        // linger either way.
        assert_eq!(
            *calls.lock().unwrap(),
            vec![
                "AGENT_MCP_BOOTSTRAP_USERNAME".to_string(),
                "AGENT_MCP_BOOTSTRAP_PASSWORD".to_string()
            ]
        );
    }

    #[tokio::test]
    async fn does_nothing_when_only_one_of_the_two_vars_is_set() {
        let (_dir, db) = router_db().await;
        let (calls, unset) = recording_unsetter();

        bootstrap_operator_from_env(
            &db,
            &[],
            env_map(&[("AGENT_MCP_BOOTSTRAP_USERNAME", "ci-sentinel")]),
            unset,
        )
        .await
        .unwrap();

        assert!(crate::identity::users_table_is_empty(&db).await.unwrap());
        // Deliberate improvement over Python (documented on the
        // function's own doc): Python's `finally` only runs inside the
        // `if both set` branch, so a lone half-set var lingers forever
        // in its process env. This port always attempts the unset,
        // regardless of whether a bootstrap was even attempted.
        assert_eq!(
            *calls.lock().unwrap(),
            vec![
                "AGENT_MCP_BOOTSTRAP_USERNAME".to_string(),
                "AGENT_MCP_BOOTSTRAP_PASSWORD".to_string()
            ]
        );
    }

    #[tokio::test]
    async fn does_nothing_when_neither_var_is_set() {
        let (_dir, db) = router_db().await;
        let (_calls, unset) = recording_unsetter();

        bootstrap_operator_from_env(&db, &[], env_map(&[]), unset)
            .await
            .unwrap();

        assert!(crate::identity::users_table_is_empty(&db).await.unwrap());
    }

    #[tokio::test]
    async fn rejects_a_weak_bootstrap_password_but_still_unsets_the_env_vars() {
        let (_dir, db) = router_db().await;
        let (calls, unset) = recording_unsetter();

        let err = bootstrap_operator_from_env(
            &db,
            &[],
            env_map(&[
                ("AGENT_MCP_BOOTSTRAP_USERNAME", "ci-sentinel"),
                ("AGENT_MCP_BOOTSTRAP_PASSWORD", "short"),
            ]),
            unset,
        )
        .await
        .unwrap_err();

        assert!(format!("{err:#}").contains("strength policy"));
        assert!(
            crate::identity::users_table_is_empty(&db).await.unwrap(),
            "a rejected weak password must not create a row"
        );
        assert_eq!(
            *calls.lock().unwrap(),
            vec![
                "AGENT_MCP_BOOTSTRAP_USERNAME".to_string(),
                "AGENT_MCP_BOOTSTRAP_PASSWORD".to_string()
            ],
            "the vars must still be stripped even when bootstrap fails (matches Python's own try/finally)"
        );
    }
}
