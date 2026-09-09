//! `conexus-cli router create-operator` -- port of `agent_mcp/cli.py`'s
//! `router_create_operator_cmd`. Same code path the env-var bootstrap
//! and the setup wizard use (`conexus_router::identity::create_user`),
//! so all bootstrap routes share argon2 hashing + retroactive
//! `project_membership` semantics.
//!
//! **Deliberate divergence from Python**: this command does NOT run
//! Alembic migrations first (Python's `run_router_migrations_upgrade()`)
//! -- schema authority for `router.db` stays with Python's Alembic
//! until every Python backend is decommissioned (Phase F's own
//! standing rule, see `conexus_db::schema`'s module doc). This command
//! calls the idempotent `conexus_db::init_router_schema` instead
//! (a no-op against an already-migrated database), so it still works
//! standalone against a genuinely fresh `router.db` but never races or
//! substitutes for the real migration runner.

use std::path::PathBuf;

use rusqlite::Connection;

/// Resolution order: `AGENT_MCP_ROUTER_DB` env var (test isolation /
/// ops override), else the production default -- port of Python's
/// `get_router_db_path()`.
pub fn router_db_path(get_env: impl Fn(&str) -> Option<String>) -> PathBuf {
    if let Some(p) = get_env("AGENT_MCP_ROUTER_DB") {
        return PathBuf::from(p);
    }
    PathBuf::from("/var/lib/agent-mcp/router.db")
}

fn read_password(password_stdin: bool) -> anyhow::Result<String> {
    if password_stdin {
        // First line of stdin only; strip the trailing newline but
        // preserve internal whitespace (a multi-word passphrase).
        let mut line = String::new();
        std::io::stdin().read_line(&mut line)?;
        let password = line.trim_end_matches(['\n', '\r']).to_string();
        if password.is_empty() {
            anyhow::bail!("--password-stdin received an empty first line of stdin.");
        }
        Ok(password)
    } else {
        let password = rpassword::prompt_password("Password: ")?;
        let confirm = rpassword::prompt_password("Repeat for confirmation: ")?;
        if password != confirm {
            anyhow::bail!("Error: the two entered password values do not match.");
        }
        Ok(password)
    }
}

pub async fn run(username: &str, email: Option<&str>, password_stdin: bool) -> anyhow::Result<()> {
    let password = read_password(password_stdin)?;

    // Canonical single-source policy check -- every path that mints a
    // NEW operator password calls this first, matching Python's own
    // "this CLI path was the gap" fix.
    conexus_router::identity::validate_password_strength(&password)
        .map_err(|e| anyhow::anyhow!("{e}"))?;

    let db_path = router_db_path(|k| std::env::var(k).ok());
    if let Some(parent) = db_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    // Schema init/foreign-key setup stays on the rusqlite connection
    // (schema authority for `router.db` stays with Python's Alembic --
    // see this module's own doc); `identity::create_user` itself is
    // sea-orm-backed (Phase G router step 4 PR D), so a SEPARATE
    // `sea_orm::DatabaseConnection` is opened onto the SAME file for
    // that one call -- the same dual-connection shape every other
    // caller in this migration uses once a sync rusqlite setup step
    // and an async sea-orm write need to share one on-disk database.
    {
        let conn = Connection::open(&db_path)?;
        conn.pragma_update(None, "foreign_keys", true)?;
        conexus_db::init_router_schema(&conn)?;
    }
    let db = sea_orm::Database::connect(format!("sqlite://{}", db_path.display())).await?;

    let registry = conexus_router::project_registry::ProjectRegistry::new(
        conexus_router::project_registry::default_registry_path(|k| std::env::var(k).ok()),
    );
    let registered_projects: Vec<String> = registry
        .list()
        .map_err(|e| anyhow::anyhow!("{e}"))?
        .into_iter()
        .map(|p| p.name)
        .collect();

    let now = chrono::Utc::now().to_rfc3339();
    // is_sysadmin=false, bootstrap_sysadmin=true -- matches Python's
    // own create_user() defaults for this exact call site: the first
    // operator on an empty users table is auto-crowned sysadmin
    // (bootstrap_sysadmin), a subsequent one is not (is_sysadmin only
    // forces the bit for the proxy-header default-sysadmin path,
    // never reachable from this CLI).
    let user_id = conexus_router::identity::create_user(
        &db,
        username,
        &password,
        email,
        false,
        true,
        &registered_projects,
        &now,
    )
    .await
    .map_err(|e| anyhow::anyhow!("{e}"))?;

    println!("Created operator {username:?} (user_id={user_id}).");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |key| map.get(key).cloned()
    }

    #[test]
    fn router_db_path_honours_the_env_override() {
        let path = router_db_path(env(&[("AGENT_MCP_ROUTER_DB", "/tmp/custom-router.db")]));
        assert_eq!(path, PathBuf::from("/tmp/custom-router.db"));
    }

    #[test]
    fn router_db_path_falls_back_to_the_production_default() {
        let path = router_db_path(env(&[]));
        assert_eq!(path, PathBuf::from("/var/lib/agent-mcp/router.db"));
    }
}
