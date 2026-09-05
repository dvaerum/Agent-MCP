//! One canonical home for the router's request-classification facts
//! (N3 Tier 2). Port of `agent_mcp/router/path_policy.py`.
//!
//! Three questions, each answered in exactly one place (see the
//! Python module's own doc for why the two prefix tuples below are
//! deliberately NOT merged into one list -- `UNAUTH_PREFIXES` answers
//! "skip the operator-session gate?", `REDIRECT_EXEMPT_PREFIXES`
//! answers "skip the fresh-install bounce to /setup?" -- their
//! differences are load-bearing, not drift, most visibly at
//! `/agent-mcp/login`/`/agent-mcp/logout`, auth-bypass ONLY, and
//! `/agent-mcp/api/`, redirect-exempt ONLY).
//!
//! **Re-derivation, not a literal port, for the "is this path
//! public?" EXACT-match set**: Python's `public_route`/
//! `derive_public_paths`/`freeze_public_paths` mark a HANDLER at
//! registration time and walk aiohttp's live routing table to derive
//! the exact-match set (so every mechanical alias of a public route --
//! trailing-slash, ADR-0020 root-mount -- inherits the marking for
//! free). axum has no equivalent "walk the registered route table by
//! handler identity" API, and there is no full routing table to walk
//! yet anyway (that's Phase E2 PR 23, app-wiring, necessarily last).
//! This module instead takes the derived exact-path set as an
//! EXPLICIT parameter (`extra_exact_paths`) to [`is_unauth_path`] --
//! the caller (eventually PR 23, which DOES know the real registered
//! routes and their aliases) computes the same set some other way
//! (most naturally: the same small, hand-reviewed table pattern this
//! migration already uses repeatedly for "can't derive automatically,
//! one reviewed list instead" -- `PUBLIC_TOOL_ALLOWLIST`,
//! `TIER_OVERRIDES`, `MUTATING_TOOL_NAMES`). The SAFETY property
//! Python's mechanism protects (R5-F6: exact match, never an unbounded
//! prefix) is preserved either way -- a hand-reviewed exact-match
//! `&[&str]` is exactly as safe against prefix-fallthrough as a
//! runtime-derived one, since axum's own route registration doesn't
//! have the prefix-vs-exact ambiguity the original aiohttp bug came
//! from.
//!
//! `#![allow(dead_code)]`: this is a BINARY crate and this PR (2/24)
//! has no real consumer yet -- same precedent as `mount.rs`'s own
//! top-of-module note.
#![allow(dead_code)]

// ── Question 1: is this path public? ────────────────────────────────

/// Prefixes that bypass operator-session gating entirely. Every entry
/// here is INTENTIONAL -- adding a new one should come with a written
/// justification in the PR body. See this module's own doc for why
/// `/agent-mcp/login`/`/agent-mcp/logout`/`/agent-mcp/setup`/
/// `/agent-mcp/assets/`/`/agent-mcp/mcp/`/`/agent-mcp/sso/` are each
/// here.
pub const UNAUTH_PREFIXES: &[&str] = &[
    "/agent-mcp/login",
    "/agent-mcp/logout",
    "/agent-mcp/setup",
    "/agent-mcp/assets/",
    "/agent-mcp/mcp/",
    "/agent-mcp/sso/",
];

/// Exact paths that bypass auth -- the bare `/agent-mcp` landing
/// redirect (NOT `/agent-mcp/` itself, which must still run the real
/// redirect handler).
pub const UNAUTH_EXACT: &[&str] = &["/agent-mcp"];

/// Paths that must remain reachable while the `users` table is empty
/// (the first-boot setup wizard bounce). See the Python module's own
/// doc for why each entry is exempt.
pub const REDIRECT_EXEMPT_PREFIXES: &[&str] = &[
    "/agent-mcp/setup",
    "/agent-mcp/assets/",
    "/agent-mcp/api/",
    "/agent-mcp/mcp/",
    "/agent-mcp/sso/",
];

/// Shared prefix matcher for the two policy tuples above.
pub fn matches_prefix(path: &str, prefixes: &[&str]) -> bool {
    prefixes.iter().any(|p| path.starts_with(p))
}

/// True iff `path` (canonical form) skips the operator-session gate.
/// `extra_exact_paths` is the caller-supplied derived public-route set
/// -- see this module's own doc for why that's an explicit parameter
/// here rather than something this module derives itself.
pub fn is_unauth_path(path: &str, extra_exact_paths: &[&str]) -> bool {
    if UNAUTH_EXACT.contains(&path) {
        return true;
    }
    if extra_exact_paths.contains(&path) {
        return true;
    }
    matches_prefix(path, UNAUTH_PREFIXES)
}

/// True iff `path` (canonical form) is exempt from the fresh-install
/// bounce to the setup wizard.
pub fn is_redirect_exempt(path: &str) -> bool {
    matches_prefix(path, REDIRECT_EXEMPT_PREFIXES)
}

// ── Question 3 (shared with 4 below): which project is this? ───────

/// Values of the first `/api/`-or-`/app/` segment that are NOT
/// projects (they're router-level admin endpoints). ADR-0014: the
/// single `router` segment replaces the prior per-route `projects`
/// entry.
pub const NON_PROJECT_API_SEGMENTS: &[&str] = &["router"];

/// Return the raw project URL segment if `path` is project-scoped,
/// else `None`. A reserved non-project segment (`router`) yields
/// `None` too. This is the SYNTACTIC half of "which project is
/// this?" -- turning it into a real project name (ADR-0010 alias
/// resolution) is a later PR's job (the project-registry's own
/// resolver).
pub fn project_segment_from_path(path: &str) -> Option<&str> {
    for base in ["/agent-mcp/api/", "/agent-mcp/app/"] {
        let Some(rest) = path.strip_prefix(base) else {
            continue;
        };
        let project = rest.split_once('/').map_or(rest, |(seg, _)| seg);
        if project.is_empty() {
            // Python's `[^/]+` requires at least one char -- an empty
            // segment here is not a match at all, try the other base.
            continue;
        }
        if NON_PROJECT_API_SEGMENTS.contains(&project) {
            return None;
        }
        return Some(project);
    }
    None
}

// ── Question 2: is this a delivery route? ───────────────────────────

/// ADR-0021 delivery transport: the per-agent fallback channel. These
/// two project-scoped routes are authenticated by the AGENT BEARER at
/// the backend, exactly like `/agent-mcp/mcp/` -- NOT by an operator
/// session -- so they skip both the operator-session gate and the
/// Accept-version gate, while every OTHER `/api/<project>/...` route
/// keeps both. Deliberately tight so it can't reach any other project
/// route unauthed.
pub const DELIVERY_RESTS: &[&str] = &["delivery/stream", "delivery/status"];

fn strip_one_trailing_slash(rest: &str) -> &str {
    rest.strip_suffix('/').unwrap_or(rest)
}

/// True iff `(project_segment, rest)` names a delivery route. `rest`
/// is the backend-facing tail (e.g. `"delivery/stream"` or
/// `"delivery/stream/"`, one trailing slash tolerated). The reserved
/// `router` segment is excluded -- the ADR-0014 admin surface never
/// inherits the delivery carve-out.
pub fn is_delivery(project_segment: &str, rest: &str) -> bool {
    if NON_PROJECT_API_SEGMENTS.contains(&project_segment) {
        return false;
    }
    DELIVERY_RESTS.contains(&strip_one_trailing_slash(rest))
}

/// True iff the canonical `path` is a delivery route -- matches
/// `^/agent-mcp/api/(?P<project>[^/]+)/(?P<rest>delivery/[^/]+/?)$`
/// exactly (a project segment, then `delivery/`, then exactly one more
/// segment, optional trailing slash, end of string) and delegates to
/// [`is_delivery`] so this path-only check and `is_delivery`'s
/// split-match-info check can never disagree.
pub fn is_delivery_path(path: &str) -> bool {
    let Some(after_api) = path.strip_prefix("/agent-mcp/api/") else {
        return false;
    };
    let Some((project, tail)) = after_api.split_once('/') else {
        return false;
    };
    if project.is_empty() {
        return false;
    }
    let Some(after_delivery) = tail.strip_prefix("delivery/") else {
        return false;
    };
    let seg = after_delivery.strip_suffix('/').unwrap_or(after_delivery);
    if seg.is_empty() || seg.contains('/') {
        return false;
    }
    is_delivery(project, tail)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unauth_exact_and_prefixes_gate_the_documented_paths() {
        assert!(is_unauth_path("/agent-mcp", &[]));
        assert!(is_unauth_path("/agent-mcp/login", &[]));
        assert!(is_unauth_path("/agent-mcp/assets/app.css", &[]));
        assert!(!is_unauth_path("/agent-mcp/api/tasks", &[]));
        // Bare `/agent-mcp/` (trailing slash) is NOT in UNAUTH_EXACT --
        // Python deliberately lets the redirect handler run.
        assert!(!is_unauth_path("/agent-mcp/", &[]));
    }

    #[test]
    fn is_unauth_path_honours_the_caller_supplied_derived_set() {
        assert!(!is_unauth_path("/agent-mcp/api/router/health", &[]));
        assert!(is_unauth_path(
            "/agent-mcp/api/router/health",
            &["/agent-mcp/api/router/health"]
        ));
    }

    #[test]
    fn unauth_and_redirect_exempt_deliberately_diverge() {
        // login/logout: auth-bypass ONLY (an operator must be able to
        // log back in), never redirect-exempt.
        assert!(is_unauth_path("/agent-mcp/login", &[]));
        assert!(!is_redirect_exempt("/agent-mcp/login"));
        // /api/: redirect-exempt ONLY (machine-to-machine, never
        // bounced to the HTML wizard), never blanket auth-bypass.
        assert!(is_redirect_exempt("/agent-mcp/api/tasks"));
        assert!(!is_unauth_path("/agent-mcp/api/tasks", &[]));
    }

    #[test]
    fn project_segment_from_path_extracts_from_both_prefixes() {
        assert_eq!(
            project_segment_from_path("/agent-mcp/api/myproject/tasks"),
            Some("myproject")
        );
        assert_eq!(
            project_segment_from_path("/agent-mcp/app/myproject/"),
            Some("myproject")
        );
        assert_eq!(
            project_segment_from_path("/agent-mcp/api/myproject"),
            Some("myproject")
        );
    }

    #[test]
    fn project_segment_from_path_excludes_the_reserved_router_segment() {
        assert_eq!(
            project_segment_from_path("/agent-mcp/api/router/health"),
            None
        );
    }

    #[test]
    fn project_segment_from_path_is_none_outside_project_scoped_prefixes() {
        assert_eq!(project_segment_from_path("/agent-mcp/login"), None);
        assert_eq!(project_segment_from_path("/agent-mcp/api/"), None);
    }

    #[test]
    fn is_delivery_matches_exactly_the_two_reserved_rests() {
        assert!(is_delivery("myproject", "delivery/stream"));
        assert!(is_delivery("myproject", "delivery/status"));
        assert!(is_delivery("myproject", "delivery/stream/")); // one trailing slash tolerated
        assert!(!is_delivery("myproject", "delivery/other"));
        assert!(!is_delivery("myproject", "tasks"));
    }

    #[test]
    fn is_delivery_excludes_the_reserved_router_segment() {
        assert!(!is_delivery("router", "delivery/stream"));
    }

    #[test]
    fn is_delivery_path_matches_the_real_url_shape() {
        assert!(is_delivery_path("/agent-mcp/api/myproject/delivery/stream"));
        assert!(is_delivery_path(
            "/agent-mcp/api/myproject/delivery/status/"
        ));
        assert!(!is_delivery_path(
            "/agent-mcp/api/myproject/delivery/stream/extra"
        ));
        assert!(!is_delivery_path("/agent-mcp/api/router/delivery/stream"));
        assert!(!is_delivery_path("/agent-mcp/api/myproject/tasks"));
    }
}
