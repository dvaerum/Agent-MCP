//! Port of `agent_mcp/router/sso.py`'s proxy-header mode. Phase E2,
//! `conexus-router-sso-proxy-header` (PR21 of the 24-PR breakdown) --
//! dedicated background research pass (real source reads: the full
//! 1759-LOC file, the real proxy-header test files, cross-checked
//! against every already-ported Rust primitive) done before any code,
//! matching this migration's own discipline for large modules.
//!
//! **Scope, deliberately narrow**: this module covers the "simple
//! half" the plan's own 24-PR breakdown names -- the WHOLE config
//! surface (both `oidc`/`proxy_header` branches of [`load_sso_config`],
//! since the mutual-exclusivity check is one function that must
//! reject "both set" regardless of which mode a PR "owns") plus the
//! complete proxy-header TRUST + JIT-user-resolution flow. The real
//! OIDC authorization-code FLOW (discovery fetch, PKCE, token
//! exchange, id_token decode, `register_sso_routes`) needs a mock-IdP
//! test harness and the still-undecided OIDC/JWT crate choice -- both
//! deferred to PR22, matching the plan's own explicit split.
//!
//! **`find_or_create_sso_user` is ported here WITHOUT `SsoSubject`/
//! the legacy-subject self-heal branch** (Python's R19-F1/R20-F1):
//! that whole mechanism only exists to migrate a pre-R18-F1 UNTAGGED
//! subject format that OIDC alone ever produced -- proxy-header
//! subjects were never in that format, so the branch is unreachable
//! from this module's own real call site. PR22 adds `legacy_subject`
//! as an additional parameter once it introduces `SsoSubject`.
//!
//! **Group-mapping (`apply_group_mapping`/`reconcile_oidc_group_
//! membership`/etc.) is 100% out of scope** -- confirmed by reading
//! every call site: they're exercised exclusively from the OIDC
//! callback handler, and there is no proxy-header equivalent of a
//! group-claim header anywhere in this module's env-var surface.
//!
//! **No caching global** (unlike Python's `get_sso_config`/
//! `_reset_cache_for_tests` module-level cache + reload flag):
//! [`load_sso_config`] resolves fresh from an injected `get_env`
//! closure every call, matching `rate_limit::RateLimitConfig::
//! resolve`'s own established Phase D2 convention (sidesteps `cargo
//! test`'s parallel-thread env-var-race hazard; any real caching
//! decision is PR23 app-wiring's to make, not this module's).

#![allow(dead_code)]

use std::collections::{HashMap, HashSet};
use std::net::IpAddr;

use rusqlite::Connection;

use crate::identity::{self, IdentityError, UserRow};
use crate::rate_limit::{self, PeerInfo};

/// Namespace prefix for proxy-header subjects -- keeps them disjoint
/// from OIDC `sub` claims in the shared `users.sso_subject` column
/// even if both modes leave rows in the same DB across a
/// reconfigure. Port of `_PROXY_SUBJECT_PREFIX`.
pub const PROXY_SUBJECT_PREFIX: &str = "proxy:";

const DEFAULT_TRUSTED_IPS: &str = "127.0.0.1,::1";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SsoMode {
    Builtin,
    Oidc,
    ProxyHeader,
}

/// Port of `OIDCSettings`. Config-loading only in this PR -- the
/// route handlers that CONSUME this (discovery fetch, token exchange)
/// are PR22.
#[derive(Debug, Clone, PartialEq)]
pub struct OidcSettings {
    pub issuer: String,
    pub client_id: String,
    pub client_secret: String,
    pub provider_name: String,
    pub group_mapping: HashMap<String, String>,
    pub redirect_url: Option<String>,
    pub scopes: Vec<String>,
    pub default_is_sysadmin: bool,
}

/// Port of `ProxyHeaderSettings`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProxyHeaderSettings {
    pub trust_header: String,
    pub trusted_ips: HashSet<IpAddr>,
    pub default_is_sysadmin: bool,
}

/// Port of `SSOSettings`.
#[derive(Debug, Clone, PartialEq)]
pub struct SsoSettings {
    pub mode: SsoMode,
    pub oidc: Option<OidcSettings>,
    pub proxy: Option<ProxyHeaderSettings>,
}

/// Port of `SSOConfigError`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SsoConfigError(pub String);

impl std::fmt::Display for SsoConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for SsoConfigError {}

fn parse_trusted_ips(raw: Option<&str>) -> HashSet<IpAddr> {
    let raw = raw
        .filter(|s| !s.trim().is_empty())
        .unwrap_or(DEFAULT_TRUSTED_IPS);
    rate_limit::parse_trusted_proxies(raw)
}

/// Port of `_parse_group_mapping`: malformed JSON, or a non-object
/// top level, degrades to an empty map (a warning-worthy
/// misconfiguration, never a hard failure -- group mapping is a
/// convenience feature, not a security gate); non-string keys/values
/// are individually dropped rather than failing the whole map.
fn parse_group_mapping(raw: Option<&str>) -> HashMap<String, String> {
    let Some(raw) = raw.filter(|s| !s.trim().is_empty()) else {
        return HashMap::new();
    };
    let Ok(serde_json::Value::Object(map)) = serde_json::from_str::<serde_json::Value>(raw) else {
        return HashMap::new();
    };
    map.into_iter()
        .filter_map(|(k, v)| match v {
            serde_json::Value::String(s) => Some((k, s)),
            _ => None,
        })
        .collect()
}

/// Port of `load_sso_config`. `get_env`/`read_secret_file` are both
/// explicit injected closures -- the former matches `rate_limit::
/// RateLimitConfig::resolve`'s own Phase D2 convention; the latter
/// keeps the one real file-read this function performs (the OIDC
/// client-secret file) test-injectable without touching the real
/// filesystem in unit tests, the same explicit-boundary discipline as
/// `orchestrator::primitives::ensure_forwarding_hmac_key`.
pub fn load_sso_config(
    get_env: impl Fn(&str) -> Option<String>,
    read_secret_file: impl Fn(&str) -> std::io::Result<String>,
) -> Result<SsoSettings, SsoConfigError> {
    let oidc_issuer = get_env("AGENT_MCP_SSO_OIDC_ISSUER")
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    let proxy_header = get_env("AGENT_MCP_SSO_PROXY_HEADER")
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());

    if oidc_issuer.is_some() && proxy_header.is_some() {
        return Err(SsoConfigError(
            "both AGENT_MCP_SSO_OIDC_ISSUER and AGENT_MCP_SSO_PROXY_HEADER are set. Pick one: \
             OIDC or proxy-header SSO, not both."
                .to_string(),
        ));
    }

    if let Some(issuer) = oidc_issuer {
        let allow_insecure =
            rate_limit::env_truthy(get_env("AGENT_MCP_SSO_OIDC_ALLOW_INSECURE").as_deref());
        let scheme_ok =
            issuer.starts_with("https://") || (issuer.starts_with("http://") && allow_insecure);
        if !scheme_ok {
            return Err(SsoConfigError(format!(
                "AGENT_MCP_SSO_OIDC_ISSUER {issuer:?} must be an https:// URL (set \
                 AGENT_MCP_SSO_OIDC_ALLOW_INSECURE=true to allow http:// for local testing)"
            )));
        }
        let client_id = get_env("AGENT_MCP_SSO_OIDC_CLIENT_ID")
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| {
                SsoConfigError("AGENT_MCP_SSO_OIDC_CLIENT_ID is required for OIDC mode".to_string())
            })?;
        let secret_path = get_env("AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE").ok_or_else(|| {
            SsoConfigError(
                "AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE is required for OIDC mode".to_string(),
            )
        })?;
        let client_secret = read_secret_file(&secret_path)
            .map_err(|e| SsoConfigError(format!("could not read client secret file: {e}")))?
            .trim()
            .to_string();
        let provider_name = get_env("AGENT_MCP_SSO_OIDC_PROVIDER_NAME")
            .filter(|s| !s.trim().is_empty())
            .unwrap_or_else(|| "SSO".to_string());
        let group_mapping =
            parse_group_mapping(get_env("AGENT_MCP_SSO_OIDC_GROUP_MAPPING").as_deref());
        let redirect_url =
            get_env("AGENT_MCP_SSO_OIDC_REDIRECT_URL").filter(|s| !s.trim().is_empty());
        let scopes = get_env("AGENT_MCP_SSO_OIDC_SCOPES")
            .filter(|s| !s.trim().is_empty())
            .map(|s| s.split_whitespace().map(str::to_string).collect())
            .unwrap_or_else(|| {
                ["openid", "profile", "email", "groups"]
                    .iter()
                    .map(|s| s.to_string())
                    .collect()
            });
        let default_is_sysadmin =
            rate_limit::env_truthy(get_env("AGENT_MCP_SSO_OIDC_DEFAULT_SYSADMIN").as_deref());
        return Ok(SsoSettings {
            mode: SsoMode::Oidc,
            oidc: Some(OidcSettings {
                issuer,
                client_id,
                client_secret,
                provider_name,
                group_mapping,
                redirect_url,
                scopes,
                default_is_sysadmin,
            }),
            proxy: None,
        });
    }

    if let Some(trust_header) = proxy_header {
        let trusted_ips = parse_trusted_ips(get_env("AGENT_MCP_SSO_PROXY_TRUSTED_IPS").as_deref());
        let default_is_sysadmin =
            rate_limit::env_truthy(get_env("AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN").as_deref());
        return Ok(SsoSettings {
            mode: SsoMode::ProxyHeader,
            oidc: None,
            proxy: Some(ProxyHeaderSettings {
                trust_header,
                trusted_ips,
                default_is_sysadmin,
            }),
        });
    }

    Ok(SsoSettings {
        mode: SsoMode::Builtin,
        oidc: None,
        proxy: None,
    })
}

/// Port of `is_trusted_proxy_source`. **N3 Tier 1** (load-bearing,
/// pinned by `test_sso_does_not_inherit_the_rate_limiter_default_
/// loopback_trust`): `settings.trusted_ips` is the operator-configured
/// allowlist and nothing else -- deliberately NOT unioned with the
/// rate limiter's own loopback-defaulted set. `peer`/`own_uid`/
/// `extra_trusted_uids` are threaded explicitly (this crate's
/// established convention; the real socket-level extraction is PR23's
/// job, same as `mount.rs`'s own `is_trusted: bool` precedent).
pub fn is_trusted_proxy_source(
    peer: &PeerInfo,
    settings: &ProxyHeaderSettings,
    own_uid: u32,
    extra_trusted_uids: &HashSet<u32>,
) -> bool {
    peer.is_trusted(&settings.trusted_ips, own_uid, extra_trusted_uids)
}

/// Port of `_sanitise_username`: lowercase, collapse every run of
/// non-`[a-z0-9-]` to a single dash, strip leading/trailing dashes;
/// an all-non-alphanumeric input degrades to `"user"`. Sanitisation
/// applies ONLY to the display username, NEVER to the reconciliation
/// subject -- see [`extract_proxy_header_user`]'s own doc for why
/// that distinction is load-bearing.
pub fn sanitise_username(raw: &str) -> String {
    let lower = raw.to_lowercase();
    let mut out = String::with_capacity(lower.len());
    let mut last_was_dash = false;
    for ch in lower.chars() {
        if ch.is_ascii_alphanumeric() || ch == '-' {
            out.push(ch);
            last_was_dash = ch == '-';
        } else if !last_was_dash {
            out.push('-');
            last_was_dash = true;
        }
    }
    let trimmed = out.trim_matches('-');
    if trimmed.is_empty() {
        "user".to_string()
    } else {
        trimmed.to_string()
    }
}

/// Port of `find_or_create_sso_user`, scoped to the fields this
/// module's own real call site ([`extract_proxy_header_user`])
/// exercises -- see this module's own doc for why `legacy_subject`/
/// `SsoSubject` are deliberately absent here.
///
/// 3-step algorithm, in order: (1) direct subject reconciliation --
/// makes repeated calls with the SAME subject resolve to the SAME
/// row, the only reason proxy-header mode (which has no session
/// cookie) works at all; (2) verified-email link to an existing
/// account; (3) JIT-create with a collision-avoiding username suffix
/// loop.
pub fn find_or_create_sso_user(
    conn: &mut Connection,
    email: Option<&str>,
    email_verified: bool,
    preferred_username: Option<&str>,
    subject: &str,
    default_is_sysadmin: bool,
    now: &str,
) -> Result<UserRow, IdentityError> {
    if let Some(existing) = identity::find_user_by_sso_subject(conn, subject)? {
        identity::touch_last_login(conn, &existing.user_id, now)?;
        return identity::get_user_by_id(conn, &existing.user_id)?
            .ok_or_else(|| IdentityError::Db(rusqlite::Error::QueryReturnedNoRows));
    }
    if email_verified {
        if let Some(email) = email {
            if let Some(existing) = identity::find_linkable_user_by_email(conn, email)? {
                identity::stamp_sso_subject_if_absent(conn, &existing.user_id, subject)?;
                identity::touch_last_login(conn, &existing.user_id, now)?;
                return identity::get_user_by_id(conn, &existing.user_id)?
                    .ok_or_else(|| IdentityError::Db(rusqlite::Error::QueryReturnedNoRows));
            }
        }
    }

    let base = sanitise_username(preferred_username.or(email).unwrap_or("user"));
    let mut candidate = base.clone();
    let mut suffix = 2;
    while identity::get_user_by_username(conn, &candidate)?.is_some() {
        candidate = format!("{base}-{suffix}");
        suffix += 1;
    }

    let user_id = identity::create_sso_user(
        conn,
        &candidate,
        subject,
        email,
        default_is_sysadmin,
        true,
        now,
    )?;
    identity::get_user_by_id(conn, &user_id)?
        .ok_or_else(|| IdentityError::Db(rusqlite::Error::QueryReturnedNoRows))
}

/// Port of `extract_proxy_header_user`. Returns `None` (never an
/// error) when the header should be silently ignored -- untrusted
/// source, absent header, or the bootstrap gate below -- matching
/// Python's own "no per-request log, `is_trusted_proxy_source` is
/// auditable" design.
#[allow(clippy::too_many_arguments)]
pub fn extract_proxy_header_user(
    conn: &mut Connection,
    header_value: Option<&str>,
    peer: &PeerInfo,
    settings: &ProxyHeaderSettings,
    own_uid: u32,
    extra_trusted_uids: &HashSet<u32>,
    now: &str,
) -> Result<Option<UserRow>, IdentityError> {
    if !is_trusted_proxy_source(peer, settings, own_uid, extra_trusted_uids) {
        return Ok(None);
    }
    let Some(raw) = header_value.map(str::trim).filter(|s| !s.is_empty()) else {
        return Ok(None);
    };
    // Bootstrap gate (AC-R9-2 sibling): on an EMPTY users table, only
    // auto-mint the first user when the operator opted into proxy
    // auto-sysadmin. JIT-creating a non-sysadmin row here would both
    // violate the flag and make the table non-empty, locking the
    // setup wizard away (it only renders while the table is empty).
    if !settings.default_is_sysadmin && identity::users_table_is_empty(conn)? {
        return Ok(None);
    }
    // The subject MUST be the RAW (un-sanitised) header value:
    // sanitisation collapses every run of non-[a-z0-9-] to one dash,
    // so `a.b@corp`/`a-b@corp`/`a_b@corp` would all slugify to the
    // SAME subject and the second principal would silently reconcile
    // INTO the first's account (inheriting its groups/grants -- a
    // login-as regression). Sanitisation applies only to the DISPLAY
    // username, computed downstream in `find_or_create_sso_user`.
    let subject = format!("{PROXY_SUBJECT_PREFIX}{raw}");
    Ok(Some(find_or_create_sso_user(
        conn,
        None,
        false,
        Some(raw),
        &subject,
        settings.default_is_sysadmin,
        now,
    )?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::schema::init_router_schema;
    use std::collections::HashMap as StdHashMap;

    fn conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        c
    }
    const NOW: &str = "2026-01-01T00:00:00.000+00:00";

    fn env_map(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: StdHashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |key: &str| map.get(key).cloned()
    }

    fn no_secret_file(_path: &str) -> std::io::Result<String> {
        Ok("shh".to_string())
    }

    // -- load_sso_config ----------------------------------------------

    #[test]
    fn defaults_to_builtin_when_neither_var_is_set() {
        let settings = load_sso_config(env_map(&[]), no_secret_file).unwrap();
        assert_eq!(settings.mode, SsoMode::Builtin);
        assert!(settings.oidc.is_none());
        assert!(settings.proxy.is_none());
    }

    #[test]
    fn both_modes_configured_is_a_startup_error() {
        let err = load_sso_config(
            env_map(&[
                ("AGENT_MCP_SSO_OIDC_ISSUER", "https://idp.example.test"),
                ("AGENT_MCP_SSO_PROXY_HEADER", "X-Remote-User"),
            ]),
            no_secret_file,
        )
        .unwrap_err();
        let lower = err.0.to_lowercase();
        assert!(lower.contains("oidc"));
        assert!(lower.contains("proxy"));
    }

    #[test]
    fn proxy_header_mode_reads_trust_header_and_trusted_ips() {
        let settings = load_sso_config(
            env_map(&[
                ("AGENT_MCP_SSO_PROXY_HEADER", "X-Remote-User"),
                ("AGENT_MCP_SSO_PROXY_TRUSTED_IPS", "10.0.0.5"),
                ("AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN", "true"),
            ]),
            no_secret_file,
        )
        .unwrap();
        assert_eq!(settings.mode, SsoMode::ProxyHeader);
        let proxy = settings.proxy.unwrap();
        assert_eq!(proxy.trust_header, "X-Remote-User");
        assert!(proxy.trusted_ips.contains(&"10.0.0.5".parse().unwrap()));
        assert!(proxy.default_is_sysadmin);
    }

    #[test]
    fn proxy_header_mode_defaults_trusted_ips_to_loopback() {
        let settings = load_sso_config(
            env_map(&[("AGENT_MCP_SSO_PROXY_HEADER", "X-Remote-User")]),
            no_secret_file,
        )
        .unwrap();
        let proxy = settings.proxy.unwrap();
        assert!(proxy.trusted_ips.contains(&"127.0.0.1".parse().unwrap()));
        assert!(proxy.trusted_ips.contains(&"::1".parse().unwrap()));
        assert!(!proxy.default_is_sysadmin);
    }

    #[test]
    fn oidc_mode_rejects_a_plain_http_issuer_by_default() {
        let err = load_sso_config(
            env_map(&[
                ("AGENT_MCP_SSO_OIDC_ISSUER", "http://idp.example.test"),
                ("AGENT_MCP_SSO_OIDC_CLIENT_ID", "abc"),
                ("AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE", "/tmp/secret"),
            ]),
            no_secret_file,
        )
        .unwrap_err();
        assert!(err.0.contains("https"));
    }

    #[test]
    fn oidc_mode_allows_http_issuer_with_the_insecure_opt_in() {
        let settings = load_sso_config(
            env_map(&[
                ("AGENT_MCP_SSO_OIDC_ISSUER", "http://idp.example.test"),
                ("AGENT_MCP_SSO_OIDC_ALLOW_INSECURE", "true"),
                ("AGENT_MCP_SSO_OIDC_CLIENT_ID", "abc"),
                ("AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE", "/tmp/secret"),
            ]),
            no_secret_file,
        )
        .unwrap();
        assert_eq!(settings.mode, SsoMode::Oidc);
        assert_eq!(settings.oidc.unwrap().client_secret, "shh");
    }

    #[test]
    fn oidc_mode_defaults_provider_name_and_scopes() {
        let settings = load_sso_config(
            env_map(&[
                ("AGENT_MCP_SSO_OIDC_ISSUER", "https://idp.example.test"),
                ("AGENT_MCP_SSO_OIDC_CLIENT_ID", "abc"),
                ("AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE", "/tmp/secret"),
            ]),
            no_secret_file,
        )
        .unwrap();
        let oidc = settings.oidc.unwrap();
        assert_eq!(oidc.provider_name, "SSO");
        assert_eq!(oidc.scopes, vec!["openid", "profile", "email", "groups"]);
    }

    #[test]
    fn oidc_mode_requires_a_client_id() {
        let err = load_sso_config(
            env_map(&[
                ("AGENT_MCP_SSO_OIDC_ISSUER", "https://idp.example.test"),
                ("AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE", "/tmp/secret"),
            ]),
            no_secret_file,
        )
        .unwrap_err();
        assert!(err.0.contains("CLIENT_ID"));
    }

    #[test]
    fn parse_group_mapping_degrades_to_empty_on_malformed_json() {
        assert_eq!(parse_group_mapping(Some("not json")), HashMap::new());
        assert_eq!(parse_group_mapping(Some("[1,2,3]")), HashMap::new());
        assert_eq!(parse_group_mapping(None), HashMap::new());
    }

    #[test]
    fn parse_group_mapping_drops_non_string_values() {
        let mapping = parse_group_mapping(Some(r#"{"idp-admins": "engineers", "idp-bad": 42}"#));
        assert_eq!(mapping.len(), 1);
        assert_eq!(
            mapping.get("idp-admins").map(String::as_str),
            Some("engineers")
        );
    }

    // -- is_trusted_proxy_source / N3 Tier 1 -----------------------------

    #[test]
    fn trusted_tcp_peer_within_the_configured_allowlist_is_trusted() {
        let settings = ProxyHeaderSettings {
            trust_header: "X-Remote-User".to_string(),
            trusted_ips: HashSet::from(["10.0.0.5".parse().unwrap()]),
            default_is_sysadmin: false,
        };
        let peer = PeerInfo {
            tcp_ip: Some("10.0.0.5".parse().unwrap()),
            uds_uid: None,
        };
        assert!(is_trusted_proxy_source(
            &peer,
            &settings,
            1000,
            &HashSet::new()
        ));
    }

    #[test]
    fn n3_tier_1_a_loopback_peer_not_in_the_operator_allowlist_is_untrusted() {
        // The rate limiter defaults loopback to trusted; SSO's own
        // allowlist must NOT inherit that default.
        let settings = ProxyHeaderSettings {
            trust_header: "X-Remote-User".to_string(),
            trusted_ips: HashSet::from(["10.0.0.5".parse().unwrap()]),
            default_is_sysadmin: false,
        };
        let peer = PeerInfo {
            tcp_ip: Some("127.0.0.1".parse().unwrap()),
            uds_uid: None,
        };
        assert!(!is_trusted_proxy_source(
            &peer,
            &settings,
            1000,
            &HashSet::new()
        ));
    }

    #[test]
    fn a_uds_peer_is_trusted_via_same_uid() {
        let settings = ProxyHeaderSettings {
            trust_header: "X-Remote-User".to_string(),
            trusted_ips: HashSet::new(),
            default_is_sysadmin: false,
        };
        let peer = PeerInfo {
            tcp_ip: None,
            uds_uid: Some(1000),
        };
        assert!(is_trusted_proxy_source(
            &peer,
            &settings,
            1000,
            &HashSet::new()
        ));
    }

    // -- sanitise_username -----------------------------------------------

    #[test]
    fn sanitise_username_collapses_non_alphanumeric_runs() {
        assert_eq!(sanitise_username("Alice.Bob@Corp"), "alice-bob-corp");
        assert_eq!(sanitise_username("a-b@corp"), "a-b-corp");
        assert_eq!(sanitise_username("a_b@corp"), "a-b-corp");
    }

    #[test]
    fn sanitise_username_degrades_all_symbols_to_user() {
        assert_eq!(sanitise_username("@@@"), "user");
        assert_eq!(sanitise_username(""), "user");
    }

    // -- find_or_create_sso_user / extract_proxy_header_user -------------

    fn trusted_peer() -> PeerInfo {
        PeerInfo {
            tcp_ip: Some("10.0.0.5".parse().unwrap()),
            uds_uid: None,
        }
    }

    fn settings() -> ProxyHeaderSettings {
        ProxyHeaderSettings {
            trust_header: "X-Remote-User".to_string(),
            trusted_ips: HashSet::from(["10.0.0.5".parse().unwrap()]),
            default_is_sysadmin: true,
        }
    }

    #[test]
    fn extract_proxy_header_user_jit_creates_on_first_sight() {
        let mut c = conn();
        let user = extract_proxy_header_user(
            &mut c,
            Some("alice"),
            &trusted_peer(),
            &settings(),
            1000,
            &HashSet::new(),
            NOW,
        )
        .unwrap()
        .unwrap();
        assert_eq!(user.username, "alice");
        assert_eq!(user.sso_subject.as_deref(), Some("proxy:alice"));
    }

    #[test]
    fn repeated_proxy_header_reconciles_to_the_same_user() {
        let mut c = conn();
        let first = extract_proxy_header_user(
            &mut c,
            Some("alice"),
            &trusted_peer(),
            &settings(),
            1000,
            &HashSet::new(),
            NOW,
        )
        .unwrap()
        .unwrap();
        let second = extract_proxy_header_user(
            &mut c,
            Some("alice"),
            &trusted_peer(),
            &settings(),
            1000,
            &HashSet::new(),
            NOW,
        )
        .unwrap()
        .unwrap();
        assert_eq!(first.user_id, second.user_id);
    }

    #[test]
    fn colliding_sanitised_usernames_stay_distinct_accounts() {
        // Both "a.b@corp" and "a-b@corp" sanitise to "a-b-corp", but
        // the RAW header value is the reconciliation key, so they
        // must resolve to two different accounts.
        let mut c = conn();
        let first = extract_proxy_header_user(
            &mut c,
            Some("a.b@corp"),
            &trusted_peer(),
            &settings(),
            1000,
            &HashSet::new(),
            NOW,
        )
        .unwrap()
        .unwrap();
        let second = extract_proxy_header_user(
            &mut c,
            Some("a-b@corp"),
            &trusted_peer(),
            &settings(),
            1000,
            &HashSet::new(),
            NOW,
        )
        .unwrap()
        .unwrap();
        assert_ne!(first.user_id, second.user_id);
    }

    #[test]
    fn extract_proxy_header_user_is_none_for_an_untrusted_source() {
        let mut c = conn();
        let untrusted_peer = PeerInfo {
            tcp_ip: Some("203.0.113.9".parse().unwrap()),
            uds_uid: None,
        };
        let result = extract_proxy_header_user(
            &mut c,
            Some("alice"),
            &untrusted_peer,
            &settings(),
            1000,
            &HashSet::new(),
            NOW,
        )
        .unwrap();
        assert!(result.is_none());
    }

    #[test]
    fn extract_proxy_header_user_is_none_for_an_absent_header() {
        let mut c = conn();
        let result = extract_proxy_header_user(
            &mut c,
            None,
            &trusted_peer(),
            &settings(),
            1000,
            &HashSet::new(),
            NOW,
        )
        .unwrap();
        assert!(result.is_none());
    }

    #[test]
    fn bootstrap_gate_refuses_to_jit_create_on_an_empty_table_without_opt_in() {
        let mut c = conn();
        let mut no_auto_sysadmin = settings();
        no_auto_sysadmin.default_is_sysadmin = false;
        let result = extract_proxy_header_user(
            &mut c,
            Some("alice"),
            &trusted_peer(),
            &no_auto_sysadmin,
            1000,
            &HashSet::new(),
            NOW,
        )
        .unwrap();
        assert!(result.is_none());
        assert!(identity::users_table_is_empty(&c).unwrap());
    }

    #[test]
    fn a_second_colliding_display_username_gets_a_numeric_suffix() {
        let mut c = conn();
        // Pre-seed a real "alice" via the password path so the JIT
        // path's own collision loop must engage.
        identity::create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let jit_user = extract_proxy_header_user(
            &mut c,
            Some("alice"),
            &trusted_peer(),
            &settings(),
            1000,
            &HashSet::new(),
            NOW,
        )
        .unwrap()
        .unwrap();
        assert_eq!(jit_user.username, "alice-2");
    }
}
