//! CoNexus always-on URL-keyed router. Port target: `agent_mcp/
//! cli.py::router_cmd` + `agent_mcp/router/app.py`.
//!
//! **App-wiring status (Phase E2, `conexus-router-app-wiring`
//! breakdown)**: steps 1-3 of 10 are done -- real `RouterState`
//! construction + the fail-closed `boot::assert_startup_safe` guard +
//! the router DB boot sequence + background reaper/reconciliation
//! task spawns (step 1); the real security-headers/rate-limit/
//! empty-users-redirect/session-gate middleware stack (step 2); and
//! the real MCP/API reverse-proxy routes, mounted OUTSIDE the
//! session-gate middleware (step 3, `proxy_routes.rs`). Still
//! deferred to LATER steps of the same breakdown (4-10: login-setup,
//! revalidation-fusion, lifecycle-rest, users-groups-rest, dashboard-
//! static, mount-aliases, sso-admin-config), matching this migration's
//! own "smallest, most foundational first" discipline for large
//! phases. Every decision function these later steps will wire is
//! already built and tested across `conexus-router/src/*.rs`.
//!
//! Guiding Principle 2 / ADR-0020: this binary crate depends on
//! `conexus-auth`/`conexus-core` only -- never `conexus-tools` or
//! `conexus-mcp` (see `Cargo.toml`'s own comment). The router treating
//! a per-project backend as an opaque process is enforced at compile
//! time here, not just by convention.

mod admin_group_capabilities;
mod admin_group_members;
mod admin_groups;
mod admin_project_memberships;
mod admin_users_gate;
mod admin_users_users;
mod asset_prefix;
mod boot;
mod client_disconnect;
mod dashboard_handlers;
mod dashboard_static;
mod identity;
mod json_sanitize;
mod lifecycle;
mod lifecycle_rest;
mod login;
mod login_setup_rest;
mod mcp_handler;
mod middleware;
mod mount;
mod oidc_flow_state;
mod oidc_group_mapping;
mod oidc_http_client;
mod oidc_reconcile;
mod orchestrator;
mod path_policy;
mod perm_gates;
mod project_gate;
mod project_reads;
mod project_registry;
mod project_rename;
mod project_teardown;
mod proxy_client;
mod proxy_core;
mod proxy_routes;
mod rate_limit;
mod security_headers;
mod session_gate;
mod single_tenant;
mod sso;
mod sso_config_rest;
mod sso_subject;
mod state;
mod templates;
mod users_groups_rest;

use std::net::SocketAddr;

use anyhow::{Context, Result};
use axum::routing::get;
use axum::Router;
use clap::Parser;

/// `agent-mcp router`'s real flag surface (`router_cmd` in
/// `agent_mcp/cli.py`). `--host`/router-DB-path/single-tenant-safety
/// knobs have NO CLI flag in Python either (env-var-only:
/// `AGENT_MCP_ROUTER_HOST`/`AGENT_MCP_ROUTER_DB`/
/// `AGENT_MCP_ALLOW_INSECURE_BIND`/`AGENT_MCP_REQUIRE_SECURE_COOKIES`)
/// -- this struct deliberately does not invent flags Python's own CLI
/// doesn't have; `main()` reads those straight from the process
/// environment via `boot`, matching the real interface exactly.
#[derive(Parser, Debug)]
#[command(name = "conexus-router")]
struct Cli {
    /// Port to listen on for the URL-keyed router.
    #[arg(long, default_value = "1337")]
    port: u16,

    /// JSON file mapping project name -> workspace path.
    #[arg(long)]
    projects_file: Option<std::path::PathBuf>,

    /// Directory containing per-project Unix-domain backend sockets.
    #[arg(long)]
    sock_dir: Option<std::path::PathBuf>,

    /// Directory holding the Next.js static dashboard export. Wired up
    /// by the `conexus-router-dashboard-static` app-wiring step.
    #[arg(long)]
    dashboard_dir: Option<std::path::PathBuf>,

    /// Base URL the router is reachable at.
    #[arg(long)]
    external_url: Option<String>,

    /// Idle seconds before stopping an inactive backend.
    #[arg(long, default_value = "14400")]
    idle_sec: u64,

    /// Optional installer.sh.in template path. Wired up alongside the
    /// `client_config`/`installer` routes (currently deferred
    /// indefinitely -- confirmed inert token plumbing in production).
    #[arg(long)]
    installer_template: Option<std::path::PathBuf>,

    /// Optional README rendered to HTML. Wired up by the
    /// `conexus-router-dashboard-static` app-wiring step.
    #[arg(long)]
    readme_html: Option<std::path::PathBuf>,

    /// Runtime dashboard asset-prefix substitution.
    #[arg(long)]
    asset_prefix: Option<String>,

    /// Single-tenant mode project name (ADR-0008).
    #[arg(long)]
    single_tenant: Option<String>,

    /// Single-tenant mode workspace path.
    #[arg(long)]
    single_workspace: Option<std::path::PathBuf>,
}

async fn health() -> &'static str {
    "ok"
}

/// Redirect target hardcoded to the `/agent-mcp/`-prefixed URL,
/// regardless of which path (canonical or a root-mounted alias, see
/// `mount_aliases` below) reached this handler -- ports Python's own
/// closure-reuse quirk (`_add_root_aliases` re-registers the IDENTICAL
/// closure object at the alias path, so a root-mounted `/app` request
/// is still redirected to `/agent-mcp/app/`, never a root-relative
/// target). A named fn reused at both registration sites is this
/// crate's equivalent of Python's "same closure object" preservation.
async fn redirect_to_app_index() -> axum::response::Response {
    dashboard_handlers::moved_permanently("/agent-mcp/app/")
}

/// Same hardcoded-target preservation as [`redirect_to_app_index`],
/// for the per-project `/agent-mcp/app/{name}` -> `/agent-mcp/app/{name}/`
/// redirect.
async fn redirect_to_app_page(
    axum::extract::Path(name): axum::extract::Path<String>,
) -> axum::response::Response {
    dashboard_handlers::moved_permanently(&format!("/agent-mcp/app/{name}/"))
}

/// ADR-0020/R5-F6 4-variant alias registration for an
/// `/agent-mcp/api/router/...` route, as a chainable
/// `.admin_api_route(...)` -- see `main`'s own comment above
/// `admin_router` for why all 4 (canonical, trailing-slash, root,
/// root+trailing-slash) are real, distinct routes Python's two-
/// mechanism alias pipeline produces, not 2. `MethodRouter` is
/// `Clone` (an `Arc`-wrapped set of handler fns), so this avoids
/// hand-duplicating each admin route's method set 4 times.
trait AdminApiAliasExt {
    fn admin_api_route(
        self,
        canonical: &str,
        methods: axum::routing::MethodRouter<std::sync::Arc<state::RouterState>>,
    ) -> Self;
}

impl AdminApiAliasExt for Router<std::sync::Arc<state::RouterState>> {
    fn admin_api_route(
        self,
        canonical: &str,
        methods: axum::routing::MethodRouter<std::sync::Arc<state::RouterState>>,
    ) -> Self {
        let root = canonical
            .strip_prefix(mount::INTERNAL_MOUNT)
            .expect("admin_api_route canonical path must start with /agent-mcp");
        self.route(canonical, methods.clone())
            .route(&format!("{canonical}/"), methods.clone())
            .route(root, methods.clone())
            .route(&format!("{root}/"), methods)
    }
}

/// Port of `router_cmd`'s own `--projects-file` default resolution:
/// `$XDG_CONFIG_HOME/agent-mcp/projects.local.json`, falling back to
/// `$HOME/.config/agent-mcp/projects.local.json`. No new dependency
/// (a `dirs`/`home`-style crate) for a two-env-var lookup Python
/// itself does inline.
fn default_projects_file(get_env: impl Fn(&str) -> Option<String>) -> std::path::PathBuf {
    let config_home = get_env("XDG_CONFIG_HOME")
        .filter(|s| !s.is_empty())
        .or_else(|| {
            get_env("HOME")
                .filter(|s| !s.is_empty())
                .map(|h| format!("{h}/.config"))
        })
        .unwrap_or_else(|| ".config".to_string());
    std::path::PathBuf::from(config_home)
        .join("agent-mcp")
        .join("projects.local.json")
}

/// Port of `app.py`'s `DEFAULT_WORKSPACE_PARENT`: `$AGENT_MCP_DEFAULT_WORKSPACE`,
/// falling back to `$HOME/.local/share/agent-mcp/projects` -- unlike
/// `default_projects_file` above, Python's own default here is NOT
/// XDG-aware (`Path.home()` directly), so this deliberately does not
/// reuse that helper's `$XDG_CONFIG_HOME` branch.
fn default_workspace_parent(get_env: impl Fn(&str) -> Option<String>) -> std::path::PathBuf {
    if let Some(v) = get_env("AGENT_MCP_DEFAULT_WORKSPACE").filter(|s| !s.is_empty()) {
        return std::path::PathBuf::from(v);
    }
    let home = get_env("HOME").filter(|s| !s.is_empty());
    match home {
        Some(h) => std::path::PathBuf::from(h)
            .join(".local")
            .join("share")
            .join("agent-mcp")
            .join("projects"),
        None => std::path::PathBuf::from(".local/share/agent-mcp/projects"),
    }
}

/// Port of `admin_api.py::_token_dir()`: `$AGENT_MCP_TOKENS_DIR`,
/// falling back to `$HOME/.config/agent-mcp/tokens`. Branches on the
/// env var's PRESENCE explicitly (SEC FINDING 5's own fix -- an
/// unset var must not resolve to `Path("")`, which is truthy and
/// silently points at the process CWD). Returns `None` only when
/// there is no `$HOME` to fall back to either (never expected in a
/// real production boot).
fn resolve_token_dir(get_env: impl Fn(&str) -> Option<String>) -> Option<std::path::PathBuf> {
    if let Some(v) = get_env("AGENT_MCP_TOKENS_DIR").filter(|s| !s.is_empty()) {
        return Some(std::path::PathBuf::from(v));
    }
    get_env("HOME").filter(|s| !s.is_empty()).map(|h| {
        std::path::PathBuf::from(h)
            .join(".config")
            .join("agent-mcp")
            .join("tokens")
    })
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let get_env = |key: &str| std::env::var(key).ok();

    let host = boot::resolve_bind_host(get_env);
    if let Err(msg) = boot::assert_startup_safe(cli.single_tenant.as_deref(), &host, get_env) {
        anyhow::bail!(msg);
    }
    if let Some(warning) = boot::secure_cookie_warning(&host, get_env) {
        eprintln!("conexus-router: WARNING: {warning}");
    }

    let db_path = boot::router_db_path(get_env);
    let conn = boot::open_and_init_router_db(&db_path)
        .with_context(|| format!("boot router database at {}", db_path.display()))?;

    let projects_file = cli
        .projects_file
        .clone()
        .unwrap_or_else(|| default_projects_file(get_env));
    let registry = project_registry::ProjectRegistry::new(projects_file);

    let rate_limit_config = rate_limit::RateLimitConfig::resolve_from_process_env();
    let ensure_config = orchestrator::ensure::EnsureConfig::from_env(get_env);
    let max_streams_per_agent = get_env("AGENT_MCP_MAX_SSE_PER_AGENT")
        .and_then(|v| v.parse().ok())
        .unwrap_or(4);
    let max_streams_global = get_env("AGENT_MCP_MAX_SSE_GLOBAL")
        .and_then(|v| v.parse().ok())
        .unwrap_or(64);

    let sock_dir = cli
        .sock_dir
        .clone()
        .or_else(|| get_env("AGENT_MCP_SOCK_DIR").map(std::path::PathBuf::from))
        .context(
            "--sock-dir (or $AGENT_MCP_SOCK_DIR) is required: the router can't resolve any \
             project's backend socket without it",
        )?;

    let state = std::sync::Arc::new(state::RouterState::new(
        conn,
        registry,
        rate_limit_config,
        ensure_config,
        state::RouterStateConfig {
            sock_dir,
            dashboard_dir: cli.dashboard_dir.clone(),
            external_url: cli.external_url.clone(),
            idle_sec: cli.idle_sec,
            asset_prefix: cli.asset_prefix.clone(),
            single_tenant_name: cli.single_tenant.clone(),
            single_tenant_workspace: cli.single_workspace.clone(),
            max_streams_per_agent,
            max_streams_global,
            default_workspace_parent: default_workspace_parent(get_env),
            token_dir: resolve_token_dir(get_env),
        },
    ));

    spawn_background_tasks(std::sync::Arc::clone(&state));

    // Two sub-routers, matching the real path-policy split
    // (path_policy::UNAUTH_PREFIXES/REDIRECT_EXEMPT_PREFIXES):
    // `admin_router` sits behind the session gate (every future
    // login/setup/admin-REST route lands here, later steps);
    // `proxy_router` (the MCP/API reverse-proxy) is mounted OUTSIDE
    // it entirely -- both handlers do their own bearer/Accept-header
    // admission before ever touching `proxy_core` (see
    // `proxy_routes.rs`'s own doc). `DefaultBodyLimit` on the proxy
    // router alone matches Python's own per-route `MCP_MAX_BODY_BYTES`
    // enforcement (the admin router has no comparably-sized body yet).
    let admin_router: Router<std::sync::Arc<state::RouterState>> = Router::new()
        .route("/health", get(health))
        // ── admin_api.py / admin_users_api.py REST surface, plus its
        // ADR-0020/R5-F6 mount aliases (Phase E2 PR23 step 9) ───────
        //
        // `admin_api_route` (defined below `main`) registers all 4
        // real, distinct routes Python's own two-mechanism alias
        // pipeline produces for each of these 16 endpoints:
        // canonical (`/agent-mcp/api/router/...`), its R5-F6
        // trailing-slash alias, and BOTH of those root-mounted
        // (ADR-0020) -- `_add_admin_trailing_slash_aliases` runs
        // BEFORE `_add_root_aliases` in the real `make_app()`, so the
        // root-alias pass walks a route table that already includes
        // the trailing-slash alias, producing all 4 for real (not
        // just the 2 an isolated reading of each mechanism would
        // suggest). `mount::canonical_path` (already threaded through
        // `session_gate_layer`/`rate_limit_layer`/
        // `empty_users_redirect_layer`) normalises a root-mounted
        // request back to its `/agent-mcp`-prefixed form before any
        // auth/path-policy check runs, so the 2 root variants gate
        // identically to their 2 `/agent-mcp`-prefixed twins with no
        // policy-table change needed -- confirmed live (see the PR
        // body), not merely assumed.
        .admin_api_route(
            "/agent-mcp/api/router/health",
            get(lifecycle_rest::health_handler),
        )
        .admin_api_route(
            "/agent-mcp/api/router/projects",
            get(lifecycle_rest::list_projects_handler).post(lifecycle_rest::create_project_handler),
        )
        .admin_api_route(
            "/agent-mcp/api/router/projects/{name}",
            axum::routing::delete(lifecycle_rest::delete_project_handler)
                .patch(lifecycle_rest::rename_project_handler),
        )
        .admin_api_route(
            "/agent-mcp/api/router/projects/{name}/stop",
            axum::routing::post(lifecycle_rest::stop_project_handler),
        )
        .admin_api_route(
            "/agent-mcp/api/router/projects/{name}/aliases",
            get(lifecycle_rest::alias_usage_handler),
        )
        .admin_api_route(
            "/agent-mcp/api/router/projects/{name}/aliases/{alias}",
            axum::routing::delete(lifecycle_rest::remove_alias_handler),
        )
        .admin_api_route(
            "/agent-mcp/api/router/overview",
            get(lifecycle_rest::overview_handler),
        )
        .admin_api_route(
            "/agent-mcp/api/router/users",
            get(users_groups_rest::list_users_handler).post(users_groups_rest::create_user_handler),
        )
        .admin_api_route(
            "/agent-mcp/api/router/users/{user_id}",
            axum::routing::patch(users_groups_rest::edit_user_handler)
                .delete(users_groups_rest::delete_user_handler),
        )
        .admin_api_route(
            "/agent-mcp/api/router/groups",
            get(users_groups_rest::list_groups_handler)
                .post(users_groups_rest::create_group_handler),
        )
        .admin_api_route(
            "/agent-mcp/api/router/groups/{group_id}",
            axum::routing::patch(users_groups_rest::edit_group_handler)
                .delete(users_groups_rest::delete_group_handler),
        )
        .admin_api_route(
            "/agent-mcp/api/router/groups/{group_id}/members",
            get(users_groups_rest::list_group_members_handler)
                .post(users_groups_rest::add_group_member_handler),
        )
        .admin_api_route(
            "/agent-mcp/api/router/groups/{group_id}/members/{member_id}",
            axum::routing::delete(users_groups_rest::remove_group_member_handler),
        )
        .admin_api_route(
            "/agent-mcp/api/router/groups/{group_id}/capabilities",
            get(users_groups_rest::list_group_capabilities_handler)
                .put(users_groups_rest::replace_group_capabilities_handler),
        )
        .admin_api_route(
            "/agent-mcp/api/router/projects/{name}/memberships",
            get(users_groups_rest::list_project_memberships_handler)
                .post(users_groups_rest::add_project_membership_handler),
        )
        .admin_api_route(
            "/agent-mcp/api/router/projects/{name}/memberships/{membership_id}",
            axum::routing::patch(users_groups_rest::change_project_membership_role_handler)
                .delete(users_groups_rest::delete_project_membership_handler),
        )
        // admin_sso_api.py's single route (step 10) -- registered
        // before the alias functions in the real `make_app()` too, so
        // it gets the identical 4-variant treatment as every other
        // admin API route above.
        .admin_api_route(
            "/agent-mcp/api/router/sso/config",
            get(sso_config_rest::get_sso_config_handler),
        )
        // ── Dashboard-static surface (step 8) + its own ADR-0020
        // root-mount aliases (step 9) ───────────────────────────────
        //
        // None of these get mechanism 1 (R5-F6 trailing-slash
        // aliasing) -- Python's own `_add_admin_trailing_slash_aliases`
        // scopes to `_ADMIN_API_PREFIX` only (the `/agent-mcp/api/
        // router/...` routes above), confirmed by direct source
        // read. Each still gets its ADR-0020 root-mounted alias,
        // registered by hand below (dashboard routes have no shared
        // 4-variant shape the way the admin API routes do -- a mix of
        // exact paths, a single dynamic segment, and 2 wildcard
        // tail-matches, so `admin_api_route`'s blind `path + "/"`
        // helper doesn't apply here).
        .route("/agent-mcp/", get(dashboard_handlers::index_handler))
        .route(
            "/agent-mcp",
            get(|| async { dashboard_handlers::moved_permanently("/agent-mcp/") }),
        )
        .route(
            "/agent-mcp/assets/{*rest}",
            get(dashboard_handlers::dashboard_assets_handler),
        )
        .route(
            "/agent-mcp/app/",
            get(dashboard_handlers::overview_dashboard_handler),
        )
        .route("/agent-mcp/app", get(redirect_to_app_index))
        .route("/agent-mcp/app/{name}", get(redirect_to_app_page))
        .route(
            "/agent-mcp/app/{name}/",
            get(dashboard_handlers::dashboard_index_handler),
        )
        .route(
            "/agent-mcp/app/{name}/{*rest}",
            get(dashboard_handlers::dashboard_handler),
        )
        // Login / logout / setup-wizard HTML surface (step 4,
        // `conexus-router-login-setup-templates`). Same UNAUTH_PREFIXES
        // exemption every other route in `path_policy.rs` already
        // documents -- `session_gate_layer` PassThroughs these
        // unconditionally.
        .route(
            "/agent-mcp/login",
            get(login_setup_rest::login_get_handler).post(login_setup_rest::login_post_handler),
        )
        .route(
            "/agent-mcp/logout",
            get(login_setup_rest::logout_get_handler).post(login_setup_rest::logout_post_handler),
        )
        .route(
            "/agent-mcp/setup",
            get(login_setup_rest::setup_get_handler).post(login_setup_rest::setup_post_handler),
        )
        // Dedup rule (confirmed against the real Python
        // `_add_root_aliases` loop): `/agent-mcp` (the bare 301
        // redirect) and `/agent-mcp/` (`index_handler`) both compute
        // root_path `/` -- Python's registration-order `seen` set
        // means `/agent-mcp/`'s `index_handler` wins root `/`, so the
        // bare-redirect's OWN root alias is silently skipped. Ported
        // by simply never registering a root alias for the bare
        // `/agent-mcp` redirect below.
        .route("/", get(dashboard_handlers::index_handler))
        .route("/app/", get(dashboard_handlers::overview_dashboard_handler))
        .route("/app", get(redirect_to_app_index))
        .route("/app/{name}", get(redirect_to_app_page))
        .route(
            "/app/{name}/",
            get(dashboard_handlers::dashboard_index_handler),
        )
        // ADR-0020 root-mount aliases for login/logout/setup.
        .route(
            "/login",
            get(login_setup_rest::login_get_handler).post(login_setup_rest::login_post_handler),
        )
        .route(
            "/logout",
            get(login_setup_rest::logout_get_handler).post(login_setup_rest::logout_post_handler),
        )
        .route(
            "/setup",
            get(login_setup_rest::setup_get_handler).post(login_setup_rest::setup_post_handler),
        )
        // Explicit tail-match root alias #1/3 (Python hand-lists
        // these separately since `resource.canonical` strips a
        // `{rest:.*}`'s regex, which a programmatic re-add would
        // corrupt -- their `/agent-mcp/`-prefixed canonical paths are
        // excluded from the plain-prefix-strip set above).
        .route(
            "/assets/{*rest}",
            get(dashboard_handlers::dashboard_assets_handler),
        )
        // Explicit tail-match root alias #2/3.
        .route(
            "/app/{name}/{*rest}",
            get(dashboard_handlers::dashboard_handler),
        )
        .layer(axum::middleware::from_fn_with_state(
            std::sync::Arc::clone(&state),
            middleware::session_gate_layer,
        ));
    let proxy_router: Router<std::sync::Arc<state::RouterState>> = Router::new()
        .route(
            "/agent-mcp/mcp/{name}",
            axum::routing::any(proxy_routes::mcp_proxy_handler),
        )
        .route(
            "/agent-mcp/api/{name}",
            axum::routing::any(proxy_routes::api_proxy_handler_no_rest),
        )
        .route(
            "/agent-mcp/api/{name}/{*rest}",
            axum::routing::any(proxy_routes::api_proxy_handler),
        )
        // ADR-0020 root-mount aliases for the proxy surface -- these
        // routes already do their own bearer/Accept-header admission
        // (never the operator session gate, see this router's own
        // structural split, `proxy_router` mounted OUTSIDE
        // `session_gate_layer` entirely), so their root-mounted twins
        // live on this same un-gated router, not `admin_router`.
        .route(
            "/mcp/{name}",
            axum::routing::any(proxy_routes::mcp_proxy_handler),
        )
        .route(
            "/api/{name}",
            axum::routing::any(proxy_routes::api_proxy_handler_no_rest),
        )
        // Explicit tail-match root alias #3/3.
        .route(
            "/api/{name}/{*rest}",
            axum::routing::any(proxy_routes::api_proxy_handler),
        )
        .layer(axum::extract::DefaultBodyLimit::max(
            state.mcp_handler_config.mcp_max_body_bytes,
        ));

    // Layer order matches Python's real app.py middleware chain
    // exactly: security headers outermost (touches every response,
    // even one an inner layer rejects) -> rate-limit -> empty-users-
    // redirect -> (session-gate, admin_router only, applied above).
    // `Router::layer` wraps the CURRENT service, so the LAST
    // `.layer()` call becomes the OUTERMOST layer -- these three are
    // added in REVERSE of the request's own traversal order, applied
    // AFTER merging so both sub-routers share them.
    let app = admin_router
        .merge(proxy_router)
        .layer(axum::middleware::from_fn_with_state(
            std::sync::Arc::clone(&state),
            middleware::empty_users_redirect_layer,
        ))
        .layer(axum::middleware::from_fn_with_state(
            std::sync::Arc::clone(&state),
            middleware::rate_limit_layer,
        ))
        .layer(axum::middleware::from_fn_with_state(
            std::sync::Arc::clone(&state),
            middleware::security_headers_layer,
        ))
        .with_state(std::sync::Arc::clone(&state));

    let addr = SocketAddr::from(([0, 0, 0, 0], cli.port));
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .with_context(|| format!("bind {addr}"))?;
    eprintln!("conexus-router: listening on {addr} (MCP/API proxy live; admin REST surface app-wiring in progress)");
    axum::serve(
        listener,
        app.into_make_service_with_connect_info::<SocketAddr>(),
    )
    .await
    .context("serve conexus-router")
}

/// Port of `app.py`'s `on_startup` reconciliation pass + the two
/// background sweep loops (`_start_reaper_task`/
/// `_start_alias_reaper_task`) -- `tokio::spawn`'d fire-and-forget
/// tasks, matching every other real-async-loop precedent in this
/// crate (`orchestrator::ensure`/`reaper` themselves; no supervisor/
/// restart-on-panic wrapper exists anywhere in this workspace to
/// reuse, so none is added here either).
fn spawn_background_tasks(state: std::sync::Arc<state::RouterState>) {
    let reconcile_state = std::sync::Arc::clone(&state);
    tokio::spawn(async move {
        orchestrator::reaper::reconcile_on_startup(
            &reconcile_state.runtime,
            std::time::SystemTime::now(),
            &reconcile_state.ensure_config.systemctl_program,
            reconcile_state.ensure_config.systemctl_mode,
            reconcile_state.ensure_config.systemctl_timeout,
        )
        .await;
    });

    let reaper_state = std::sync::Arc::clone(&state);
    tokio::spawn(async move {
        let idle = std::time::Duration::from_secs(reaper_state.idle_sec);
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(60)).await;
            orchestrator::reaper::reaper_tick(
                &reaper_state.runtime,
                &reaper_state.registry,
                idle,
                std::time::SystemTime::now(),
                &reaper_state.ensure_config.systemctl_program,
                reaper_state.ensure_config.systemctl_mode,
                reaper_state.ensure_config.systemctl_timeout,
            )
            .await;
        }
    });

    let alias_reaper_state = std::sync::Arc::clone(&state);
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(60)).await;
            orchestrator::reaper::alias_reaper_tick(
                &alias_reaper_state.registry,
                chrono::Utc::now(),
            );
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn health_returns_ok() {
        assert_eq!(health().await, "ok");
    }

    #[test]
    fn cli_parses_the_real_router_cmd_flag_surface() {
        let cli = Cli::parse_from([
            "conexus-router",
            "--port",
            "9999",
            "--projects-file",
            "/tmp/projects.local.json",
            "--sock-dir",
            "/tmp/sockets",
            "--dashboard-dir",
            "/tmp/dashboard",
            "--external-url",
            "https://example.test",
            "--idle-sec",
            "60",
            "--asset-prefix",
            "/agent-mcp/__dashboard",
            "--single-tenant",
            "demo",
            "--single-workspace",
            "/tmp/demo",
        ]);
        assert_eq!(cli.port, 9999);
        assert_eq!(cli.idle_sec, 60);
        assert_eq!(cli.single_tenant.as_deref(), Some("demo"));
    }

    #[test]
    fn cli_defaults_match_python_router_cmd() {
        let cli = Cli::parse_from(["conexus-router"]);
        assert_eq!(cli.port, 1337);
        assert_eq!(cli.idle_sec, 14400);
        assert!(cli.projects_file.is_none());
        assert!(cli.single_tenant.is_none());
    }
}
