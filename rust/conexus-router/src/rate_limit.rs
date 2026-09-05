//! Per-IP rate limiting for the router's auth-sensitive surface. Port
//! of `agent_mcp/router/rate_limit.py` (586 LOC, Phase E2 PR 13,
//! `conexus-router-rate-limit`).
//!
//! Threat model (unchanged from Python): the login POST runs an
//! argon2id verify (64 MiB, multi-threaded) on every request, so an
//! unthrottled attacker gets both a password brute-force oracle AND a
//! CPU/memory DoS amplifier for free. An in-process, dependency-free
//! sliding-window counter is the pragmatic, correct-for-one-process
//! choice (confirmed in Python: no shared cache exists, only SQLite +
//! Unix sockets) -- unchanged here.
//!
//! Framework-agnostic like every other handler-layer module in this
//! crate. Two things Python's real code reads directly off a live
//! `aiohttp.web.Request`/raw socket have NO live equivalent to read
//! from yet in this layer, and are threaded as EXPLICIT caller-
//! supplied inputs instead (matching `mount.rs`'s own "explicit trust
//! input over hidden dependency" precedent):
//! - **The direct peer's identity** (`PeerInfo`): a real TCP address
//!   or a UDS peer's kernel-reported UID (`SO_PEERCRED`) needs an
//!   actual live socket to extract from -- this framework-agnostic
//!   layer has no socket type yet (PR 23, app-wiring, is where a real
//!   `tokio`/`hyper` connection exists to call `.peer_cred()`/
//!   `.peer_addr()` on). The TRUST *decision* given an already-
//!   extracted identity ([`PeerInfo::is_trusted`]) is pure logic and
//!   is fully ported here.
//! - **SSO proxy-header trusted IPs** (`_trusted_ip_set`'s union half):
//!   `sso.py` isn't ported yet (PR 22). Every trust-check function here
//!   takes the caller's ALREADY-UNIONED `trusted_ips: &HashSet<IpAddr>`
//!   directly -- callers before PR 22 pass just
//!   `cfg.trusted_proxies.clone()`, callers after it pass the real
//!   union, and this module's own logic never needs to know the
//!   difference.
//!
//! Also deferred, matching every prior PR's own precedent: the real
//! `rate_limit_middleware` (an axum `tower::Layer`) and `attach`'s
//! per-`Application`-instance limiter storage are PR 23's job --
//! [`RateLimitState`] is the plain data this module offers in their
//! place, owned by whatever real middleware wraps it.
#![allow(dead_code)]

use std::collections::{HashMap, HashSet, VecDeque};
use std::net::IpAddr;
use std::time::{Duration, Instant};

use crate::mcp_handler::{HandlerBody, HandlerResponse};

const DEFAULT_TRUSTED_PROXIES: &str = "127.0.0.1,::1";

fn env_truthy(value: Option<&str>) -> bool {
    matches!(
        value.map(|s| s.trim().to_ascii_lowercase()).as_deref(),
        Some("1" | "true" | "yes" | "on")
    )
}

/// A non-negative int env var; garbage or a negative value falls back
/// to `default` -- port of `_env_int` (a `u32::from_str` failure
/// already covers the negative case, since `-5` never parses as
/// `u32`).
fn env_u32(get_env: &impl Fn(&str) -> Option<String>, key: &str, default: u32) -> u32 {
    get_env(key)
        .and_then(|v| v.trim().parse::<u32>().ok())
        .unwrap_or(default)
}

fn env_u64(get_env: &impl Fn(&str) -> Option<String>, key: &str, default: u64) -> u64 {
    get_env(key)
        .and_then(|v| v.trim().parse::<u64>().ok())
        .unwrap_or(default)
}

/// Comma-separated IP list -> canonical set (garbage dropped, no
/// logging -- see this module's own doc; a typo'd IP is a diagnostic
/// concern, not a security one).
fn parse_trusted_proxies(raw: &str) -> HashSet<IpAddr> {
    raw.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .filter_map(|s| s.parse::<IpAddr>().ok())
        .collect()
}

/// Resolved limiter settings -- port of `RateLimitConfig`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RateLimitConfig {
    pub enabled: bool,
    pub auth_max: u32,
    pub auth_window: Duration,
    pub global_max: u32,
    pub global_window: Duration,
    pub trusted_proxies: HashSet<IpAddr>,
}

impl RateLimitConfig {
    /// Port of `RateLimitConfig.from_env`, with the env lookup
    /// threaded explicitly (this crate's own Phase D2 convention --
    /// sidesteps `cargo test`'s parallel-thread env-var-race hazard).
    pub fn resolve(get_env: impl Fn(&str) -> Option<String>) -> Self {
        let enabled = !env_truthy(get_env("AGENT_MCP_RATELIMIT_DISABLED").as_deref());
        let trusted_raw = get_env("AGENT_MCP_RATELIMIT_TRUSTED_PROXIES")
            .unwrap_or_else(|| DEFAULT_TRUSTED_PROXIES.to_string());
        Self {
            enabled,
            auth_max: env_u32(&get_env, "AGENT_MCP_RATELIMIT_AUTH_MAX", 10),
            auth_window: Duration::from_secs(env_u64(
                &get_env,
                "AGENT_MCP_RATELIMIT_AUTH_WINDOW",
                60,
            )),
            global_max: env_u32(&get_env, "AGENT_MCP_RATELIMIT_GLOBAL_MAX", 0),
            global_window: Duration::from_secs(env_u64(
                &get_env,
                "AGENT_MCP_RATELIMIT_GLOBAL_WINDOW",
                60,
            )),
            trusted_proxies: parse_trusted_proxies(&trusted_raw),
        }
    }

    /// Real-deployment convenience -- reads the OS environment
    /// directly. Not used by any test (which must stay
    /// parallel-safe); the real app-wiring caller uses this.
    pub fn resolve_from_process_env() -> Self {
        Self::resolve(|key| std::env::var(key).ok())
    }
}

/// In-process per-key sliding-window counter -- port of
/// `SlidingWindowLimiter`. `now` is an explicit parameter throughout
/// (this crate's own "never read a live clock" convention; Python's
/// `now: float | None = None` defaulting to `time.monotonic()` has no
/// equivalent here).
#[derive(Debug)]
pub struct SlidingWindowLimiter {
    max_events: u32,
    window: Duration,
    hits: HashMap<String, VecDeque<Instant>>,
    checks_since_prune: u32,
}

impl SlidingWindowLimiter {
    pub fn new(max_events: u32, window: Duration) -> Self {
        Self {
            max_events,
            window,
            hits: HashMap::new(),
            checks_since_prune: 0,
        }
    }

    pub fn max_events(&self) -> u32 {
        self.max_events
    }

    /// Record a hit for `key`; return `(allowed, retry_after)`. A
    /// disabled limiter (`max_events == 0`) always admits, matching
    /// Python's `if self.max_events <= 0: return True, 0.0`.
    pub fn check(&mut self, key: &str, now: Instant) -> (bool, Duration) {
        if self.max_events == 0 {
            return (true, Duration::ZERO);
        }
        let cutoff = now.checked_sub(self.window).unwrap_or(now);
        {
            let dq = self.hits.entry(key.to_string()).or_default();
            while matches!(dq.front(), Some(front) if *front <= cutoff) {
                dq.pop_front();
            }
        }
        self.checks_since_prune += 1;
        if self.checks_since_prune >= 256 {
            self.prune(now);
        }
        let dq = self.hits.entry(key.to_string()).or_default();
        if dq.len() as u32 >= self.max_events {
            let retry_after = (dq[0] + self.window).saturating_duration_since(now);
            return (false, retry_after);
        }
        dq.push_back(now);
        (true, Duration::ZERO)
    }

    /// Drop aged-out hits and any key whose deque emptied.
    pub fn prune(&mut self, now: Instant) {
        let cutoff = now.checked_sub(self.window).unwrap_or(now);
        self.checks_since_prune = 0;
        self.hits.retain(|_, dq| {
            while matches!(dq.front(), Some(front) if *front <= cutoff) {
                dq.pop_front();
            }
            !dq.is_empty()
        });
    }

    /// Tracked-key count, for tests + metrics.
    pub fn len(&self) -> usize {
        self.hits.len()
    }

    pub fn is_empty(&self) -> bool {
        self.hits.is_empty()
    }
}

/// The direct peer's already-extracted identity -- see this module's
/// own doc for why extraction itself is deferred to PR 23. Exactly
/// one of `tcp_ip`/`uds_uid` is meaningfully `Some` for a real
/// connection; both `None` models "identity unavailable" (an
/// unsupported platform, an unconnected socket, ...), which fails
/// closed to untrusted, matching Python's own `peer_uid` contract.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct PeerInfo {
    pub tcp_ip: Option<IpAddr>,
    pub uds_uid: Option<u32>,
}

impl PeerInfo {
    /// Port of `is_trusted_peer`'s policy, given already-extracted
    /// inputs: a TCP peer is trusted iff its IP is in `trusted_ips`
    /// (explicit-allowlist-only, no implicit trust); a UDS peer is
    /// trusted iff its kernel UID is `own_uid` or in
    /// `extra_trusted_uids`. Neither present -> untrusted.
    pub fn is_trusted(
        &self,
        trusted_ips: &HashSet<IpAddr>,
        own_uid: u32,
        extra_trusted_uids: &HashSet<u32>,
    ) -> bool {
        if let Some(ip) = self.tcp_ip {
            return trusted_ips.contains(&ip);
        }
        match self.uds_uid {
            Some(uid) => uid == own_uid || extra_trusted_uids.contains(&uid),
            None => false,
        }
    }

    /// The rate-limit key / client-IP fallback for a peer with no
    /// usable `X-Forwarded-For` chain -- port of `request.remote or
    /// "unknown"`.
    fn fallback_key(&self) -> String {
        self.tcp_ip
            .map(|ip| ip.to_string())
            .unwrap_or_else(|| "unknown".to_string())
    }
}

/// Port of `resolve_client_ip`: honours `X-Forwarded-For` only when
/// the direct peer is trusted, walking the chain RIGHT-TO-LEFT
/// (client-controlled left end is never used for keying) to the
/// first untrusted hop.
pub fn resolve_client_ip(
    peer: &PeerInfo,
    xff_header: Option<&str>,
    trusted_ips: &HashSet<IpAddr>,
    own_uid: u32,
    extra_trusted_uids: &HashSet<u32>,
) -> String {
    if peer.is_trusted(trusted_ips, own_uid, extra_trusted_uids) {
        if let Some(xff) = xff_header {
            let hops: Vec<&str> = xff
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .collect();
            for hop in hops.iter().rev() {
                let hop_is_trusted = hop
                    .parse::<IpAddr>()
                    .map(|ip| trusted_ips.contains(&ip))
                    .unwrap_or(false);
                if !hop_is_trusted {
                    return hop.to_string();
                }
            }
            if let Some(first) = hops.first() {
                return first.to_string();
            }
        }
    }
    peer.fallback_key()
}

/// Port of `_is_auth_path`: only the mutating, credential-adjacent
/// verbs count. `path` MUST already be `mount::canonical_path`'d by
/// the caller (this crate's own convention); `method` is uppercase.
pub fn is_auth_path(path: &str, method: &str) -> bool {
    if path == "/agent-mcp/login" && method == "POST" {
        return true;
    }
    if path == "/agent-mcp/setup" && method == "POST" {
        return true;
    }
    path.starts_with("/agent-mcp/sso/")
}

/// Port of `_too_many_requests`: 429 with an integer `Retry-After`
/// (seconds), rounded up.
fn too_many_requests_response(retry_after: Duration) -> HandlerResponse {
    let seconds = retry_after.as_secs_f64().ceil().max(1.0) as u64;
    HandlerResponse {
        status: 429,
        headers: vec![
            ("Retry-After".to_string(), seconds.to_string()),
            ("Cache-Control".to_string(), "no-store".to_string()),
        ],
        body: HandlerBody::Json(serde_json::json!({
            "error": "rate_limited",
            "message": format!(
                "too many requests from your address; retry after {seconds}s"
            ),
        })),
    }
}

/// The two named limiters one process needs -- port of what `attach`
/// stashes on the aiohttp `Application`. Owned by whatever real
/// middleware PR 23 builds; one instance per running router process.
pub struct RateLimitState {
    pub auth: SlidingWindowLimiter,
    pub global: SlidingWindowLimiter,
}

impl RateLimitState {
    pub fn new(cfg: &RateLimitConfig) -> Self {
        Self {
            auth: SlidingWindowLimiter::new(cfg.auth_max, cfg.auth_window),
            global: SlidingWindowLimiter::new(cfg.global_max, cfg.global_window),
        }
    }
}

/// Port of `rate_limit_middleware`'s decision (the response-building
/// half; the real axum layer wraps this, PR 23). `None` means admit;
/// `Some` is the 429 to return immediately. The auth limiter runs
/// before the global one so an auth flood is charged against the
/// strict budget, not the loose one.
pub fn check_rate_limit(
    state: &mut RateLimitState,
    cfg: &RateLimitConfig,
    client_ip: &str,
    path: &str,
    method: &str,
    now: Instant,
) -> Option<HandlerResponse> {
    if !cfg.enabled {
        return None;
    }
    if is_auth_path(path, method) {
        let (allowed, retry_after) = state.auth.check(client_ip, now);
        if !allowed {
            return Some(too_many_requests_response(retry_after));
        }
    }
    if state.global.max_events() > 0 {
        let (allowed, retry_after) = state.global.check(client_ip, now);
        if !allowed {
            return Some(too_many_requests_response(retry_after));
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ip(s: &str) -> IpAddr {
        s.parse().unwrap()
    }

    fn ips(list: &[&str]) -> HashSet<IpAddr> {
        list.iter().map(|s| ip(s)).collect()
    }

    // -- RateLimitConfig::resolve --------------------------------------

    #[test]
    fn resolve_applies_documented_defaults_with_no_env() {
        let cfg = RateLimitConfig::resolve(|_| None);
        assert!(cfg.enabled);
        assert_eq!(cfg.auth_max, 10);
        assert_eq!(cfg.auth_window, Duration::from_secs(60));
        assert_eq!(cfg.global_max, 0);
        assert_eq!(cfg.global_window, Duration::from_secs(60));
        assert_eq!(cfg.trusted_proxies, ips(&["127.0.0.1", "::1"]));
    }

    #[test]
    fn resolve_honours_every_env_override() {
        let cfg = RateLimitConfig::resolve(|key| match key {
            "AGENT_MCP_RATELIMIT_DISABLED" => Some("true".to_string()),
            "AGENT_MCP_RATELIMIT_AUTH_MAX" => Some("5".to_string()),
            "AGENT_MCP_RATELIMIT_AUTH_WINDOW" => Some("30".to_string()),
            "AGENT_MCP_RATELIMIT_GLOBAL_MAX" => Some("100".to_string()),
            "AGENT_MCP_RATELIMIT_GLOBAL_WINDOW" => Some("120".to_string()),
            "AGENT_MCP_RATELIMIT_TRUSTED_PROXIES" => {
                Some("10.0.0.1, garbage, 10.0.0.2".to_string())
            }
            _ => None,
        });
        assert!(!cfg.enabled);
        assert_eq!(cfg.auth_max, 5);
        assert_eq!(cfg.auth_window, Duration::from_secs(30));
        assert_eq!(cfg.global_max, 100);
        assert_eq!(cfg.global_window, Duration::from_secs(120));
        assert_eq!(cfg.trusted_proxies, ips(&["10.0.0.1", "10.0.0.2"]));
    }

    #[test]
    fn resolve_falls_back_to_default_on_a_negative_or_garbage_int() {
        let cfg = RateLimitConfig::resolve(|key| match key {
            "AGENT_MCP_RATELIMIT_AUTH_MAX" => Some("-5".to_string()),
            "AGENT_MCP_RATELIMIT_GLOBAL_MAX" => Some("not-a-number".to_string()),
            _ => None,
        });
        assert_eq!(cfg.auth_max, 10);
        assert_eq!(cfg.global_max, 0);
    }

    // -- SlidingWindowLimiter ---------------------------------------

    #[test]
    fn admits_up_to_max_events_then_rejects_with_a_positive_retry_after() {
        let mut limiter = SlidingWindowLimiter::new(2, Duration::from_secs(60));
        let t0 = Instant::now();
        assert!(limiter.check("1.2.3.4", t0).0);
        assert!(limiter.check("1.2.3.4", t0).0);
        let (allowed, retry_after) = limiter.check("1.2.3.4", t0);
        assert!(!allowed);
        assert!(retry_after > Duration::ZERO && retry_after <= Duration::from_secs(60));
    }

    #[test]
    fn a_disabled_limiter_always_admits() {
        let mut limiter = SlidingWindowLimiter::new(0, Duration::from_secs(60));
        let t0 = Instant::now();
        for _ in 0..1000 {
            assert!(limiter.check("1.2.3.4", t0).0);
        }
        // Never even starts tracking -- the disabled short-circuit
        // returns before touching `hits` at all.
        assert!(limiter.is_empty());
    }

    #[test]
    fn admits_again_once_the_window_has_elapsed() {
        let mut limiter = SlidingWindowLimiter::new(1, Duration::from_secs(10));
        let t0 = Instant::now();
        assert!(limiter.check("1.2.3.4", t0).0);
        assert!(!limiter.check("1.2.3.4", t0 + Duration::from_secs(5)).0);
        assert!(limiter.check("1.2.3.4", t0 + Duration::from_secs(11)).0);
    }

    #[test]
    fn keys_are_independent() {
        let mut limiter = SlidingWindowLimiter::new(1, Duration::from_secs(60));
        let t0 = Instant::now();
        assert!(limiter.check("a", t0).0);
        assert!(limiter.check("b", t0).0);
        assert!(!limiter.check("a", t0).0);
        assert!(!limiter.check("b", t0).0);
    }

    #[test]
    fn prune_drops_aged_out_keys_but_keeps_live_ones() {
        let mut limiter = SlidingWindowLimiter::new(5, Duration::from_secs(10));
        let t0 = Instant::now();
        limiter.check("stale", t0);
        limiter.check("live", t0 + Duration::from_secs(15));
        assert_eq!(limiter.len(), 2);
        limiter.prune(t0 + Duration::from_secs(20));
        assert_eq!(limiter.len(), 1);
    }

    #[test]
    fn periodic_prune_fires_automatically_every_256_checks() {
        let mut limiter = SlidingWindowLimiter::new(1000, Duration::from_secs(10));
        let t0 = Instant::now();
        // 255 distinct one-shot keys that will all age out.
        for i in 0..255 {
            limiter.check(&format!("stale-{i}"), t0);
        }
        assert_eq!(limiter.len(), 255);
        // The 256th check (a fresh key, well past the window) triggers
        // the opportunistic sweep BEFORE this key is inserted.
        let later = t0 + Duration::from_secs(20);
        limiter.check("fresh", later);
        assert_eq!(
            limiter.len(),
            1,
            "the sweep must have dropped all 255 stale keys"
        );
    }

    // -- PeerInfo::is_trusted / resolve_client_ip --------------------

    #[test]
    fn a_tcp_peer_is_trusted_only_via_the_ip_allowlist() {
        let trusted = ips(&["10.0.0.1"]);
        let peer = PeerInfo {
            tcp_ip: Some(ip("10.0.0.1")),
            uds_uid: None,
        };
        assert!(peer.is_trusted(&trusted, 1000, &HashSet::new()));
        let untrusted = PeerInfo {
            tcp_ip: Some(ip("10.0.0.2")),
            uds_uid: None,
        };
        assert!(!untrusted.is_trusted(&trusted, 1000, &HashSet::new()));
    }

    #[test]
    fn a_uds_peer_is_trusted_via_own_uid_or_the_extra_allowlist() {
        let peer = PeerInfo {
            tcp_ip: None,
            uds_uid: Some(1000),
        };
        assert!(peer.is_trusted(&HashSet::new(), 1000, &HashSet::new()));
        assert!(!peer.is_trusted(&HashSet::new(), 999, &HashSet::new()));
        let extra: HashSet<u32> = [1000].into_iter().collect();
        assert!(peer.is_trusted(&HashSet::new(), 999, &extra));
    }

    #[test]
    fn a_peer_with_no_resolvable_identity_is_never_trusted() {
        let peer = PeerInfo::default();
        assert!(!peer.is_trusted(&ips(&["10.0.0.1"]), 1000, &HashSet::new()));
    }

    #[test]
    fn resolve_client_ip_ignores_xff_from_an_untrusted_peer() {
        let peer = PeerInfo {
            tcp_ip: Some(ip("203.0.113.5")),
            uds_uid: None,
        };
        let key = resolve_client_ip(
            &peer,
            Some("9.9.9.9"),
            &HashSet::new(),
            1000,
            &HashSet::new(),
        );
        assert_eq!(key, "203.0.113.5");
    }

    #[test]
    fn resolve_client_ip_walks_xff_right_to_left_past_trusted_hops() {
        let trusted = ips(&["10.0.0.1", "10.0.0.2"]);
        let peer = PeerInfo {
            tcp_ip: Some(ip("10.0.0.1")),
            uds_uid: None,
        };
        // Left-to-right as appended by each hop: real-client,
        // intermediate-trusted-proxy, our-own-trusted-proxy.
        let key = resolve_client_ip(
            &peer,
            Some("203.0.113.9, 10.0.0.2, 10.0.0.1"),
            &trusted,
            1000,
            &HashSet::new(),
        );
        assert_eq!(key, "203.0.113.9");
    }

    #[test]
    fn resolve_client_ip_falls_back_to_the_leftmost_hop_when_every_hop_is_trusted() {
        let trusted = ips(&["10.0.0.1", "10.0.0.2"]);
        let peer = PeerInfo {
            tcp_ip: Some(ip("10.0.0.1")),
            uds_uid: None,
        };
        let key = resolve_client_ip(
            &peer,
            Some("10.0.0.2, 10.0.0.1"),
            &trusted,
            1000,
            &HashSet::new(),
        );
        assert_eq!(key, "10.0.0.2");
    }

    #[test]
    fn resolve_client_ip_falls_back_to_unknown_for_an_unresolvable_uds_peer() {
        let peer = PeerInfo::default();
        let key = resolve_client_ip(&peer, None, &HashSet::new(), 1000, &HashSet::new());
        assert_eq!(key, "unknown");
    }

    // -- is_auth_path -------------------------------------------------

    #[test]
    fn is_auth_path_matches_only_the_mutating_credential_endpoints() {
        assert!(is_auth_path("/agent-mcp/login", "POST"));
        assert!(!is_auth_path("/agent-mcp/login", "GET"));
        assert!(is_auth_path("/agent-mcp/setup", "POST"));
        assert!(!is_auth_path("/agent-mcp/setup", "GET"));
        assert!(is_auth_path("/agent-mcp/sso/callback", "GET"));
        assert!(!is_auth_path("/agent-mcp/api/tasks", "POST"));
    }

    // -- check_rate_limit (the composed decision) -----------------------

    #[test]
    fn check_rate_limit_is_a_pure_pass_through_when_disabled() {
        let cfg = RateLimitConfig {
            enabled: false,
            auth_max: 0,
            auth_window: Duration::from_secs(60),
            global_max: 0,
            global_window: Duration::from_secs(60),
            trusted_proxies: HashSet::new(),
        };
        let mut state = RateLimitState::new(&cfg);
        let now = Instant::now();
        for _ in 0..1000 {
            assert!(
                check_rate_limit(&mut state, &cfg, "1.2.3.4", "/agent-mcp/login", "POST", now)
                    .is_none()
            );
        }
    }

    #[test]
    fn check_rate_limit_throttles_the_auth_path_before_the_global_limiter() {
        let cfg = RateLimitConfig {
            enabled: true,
            auth_max: 1,
            auth_window: Duration::from_secs(60),
            global_max: 1000,
            global_window: Duration::from_secs(60),
            trusted_proxies: HashSet::new(),
        };
        let mut state = RateLimitState::new(&cfg);
        let now = Instant::now();
        assert!(
            check_rate_limit(&mut state, &cfg, "1.2.3.4", "/agent-mcp/login", "POST", now)
                .is_none()
        );
        let resp =
            check_rate_limit(&mut state, &cfg, "1.2.3.4", "/agent-mcp/login", "POST", now).unwrap();
        assert_eq!(resp.status, 429);
        assert!(resp
            .headers
            .iter()
            .any(|(k, v)| k == "Retry-After" && v == "60"));
    }

    #[test]
    fn check_rate_limit_leaves_the_global_limiter_disabled_by_default() {
        let cfg = RateLimitConfig::resolve(|_| None);
        assert_eq!(cfg.global_max, 0);
        let mut state = RateLimitState::new(&cfg);
        let now = Instant::now();
        // A non-auth path with the global limiter at its documented
        // default (0 = disabled) is never throttled.
        for _ in 0..1000 {
            assert!(check_rate_limit(
                &mut state,
                &cfg,
                "1.2.3.4",
                "/agent-mcp/api/tasks",
                "GET",
                now
            )
            .is_none());
        }
    }

    #[test]
    fn check_rate_limit_throttles_the_global_limiter_when_enabled() {
        let cfg = RateLimitConfig {
            enabled: true,
            auth_max: 1000,
            auth_window: Duration::from_secs(60),
            global_max: 1,
            global_window: Duration::from_secs(60),
            trusted_proxies: HashSet::new(),
        };
        let mut state = RateLimitState::new(&cfg);
        let now = Instant::now();
        assert!(check_rate_limit(
            &mut state,
            &cfg,
            "1.2.3.4",
            "/agent-mcp/api/tasks",
            "GET",
            now
        )
        .is_none());
        let resp = check_rate_limit(
            &mut state,
            &cfg,
            "1.2.3.4",
            "/agent-mcp/api/tasks",
            "GET",
            now,
        )
        .unwrap();
        assert_eq!(resp.status, 429);
    }
}
