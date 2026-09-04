//! `initialize` `instructions` contributors (Phase E1 PR A).
//!
//! Port of `agent_mcp/app/instructions_contributors.py`'s wake-loop
//! contributor, wired through `rmcp`'s real `ServerHandler::
//! initialize()` override in [`crate::server`] instead of Python's
//! `_patched_create_initialization_options` monkeypatch on
//! `MCPLowLevelServer.create_initialization_options` -- `rmcp` gives
//! this a real, documented extension point (`initialize` is an
//! overridable trait default method receiving the full
//! `RequestContext`), closing a refactor Python's own module docstring
//! says it started but couldn't finish because its SDK never exposed
//! one.
//!
//! The bootstrap text itself (`conexus_core::WAKE_LOOP_INSTRUCTIONS`)
//! lives one layer down, not in this crate -- Phase E1 PR B1's prompt
//! catalogue (`conexus-tools`, a sibling of this crate, not a
//! dependent) needs the exact same constant for the `event-loop`
//! prompt-book entry, matching Python's own "one constant, two
//! readers" contract. See that module's own doc for the promotion
//! rationale.
//!
//! **Deliberately NOT ported**: the alias-deprecation contributor
//! (ADR-0010, `_alias_warning_contributor`). It gates on
//! `ctx.alias_info`, populated in Python only when the upstream router
//! proxies a request carrying an `X-Agent-MCP-Alias` header -- no
//! Rust representation exists anywhere in `conexus-backend` today
//! (alias resolution is router-territory, Phase E2). There is no fact
//! to contribute yet, not a dropped feature -- revisit once the router
//! side of alias forwarding is ported.

use conexus_core::principal::Principal;
use conexus_core::WAKE_LOOP_INSTRUCTIONS;

/// The per-request wake-loop contributor. Port of
/// `_wake_loop_contributor`: contributes iff there's a resolved
/// `Principal` whose `can_wake_loop` bit is set (resolved once, at
/// Principal-construction time, by
/// `conexus_auth::resolve_can_wake_loop` -- no second DB hop here).
fn wake_loop_contributor(principal: Option<&Principal>) -> Option<&'static str> {
    let principal = principal?;
    principal.can_wake_loop.then_some(WAKE_LOOP_INSTRUCTIONS)
}

/// The text to append to `InitializeResult.instructions` for this
/// request, or `None` to contribute nothing. Port of `render_all`,
/// narrowed to the one contributor currently applicable in Rust (see
/// module doc) -- concatenation with more than one contributor isn't
/// needed yet, so this returns the single contributor's text directly
/// rather than building a `Vec` to join over one element.
pub fn render_all(principal: Option<&Principal>) -> Option<&'static str> {
    wake_loop_contributor(principal)
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_core::capability::Capabilities;
    use conexus_core::principal::PrincipalKind;
    use std::collections::HashSet;

    fn agent_principal(can_wake_loop: bool) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some("worker-1".to_string()),
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop,
            source_token: Some("tok".to_string()),
            capabilities: Capabilities::Set(HashSet::new()),
        }
    }

    #[test]
    fn no_principal_at_all_contributes_nothing() {
        assert_eq!(render_all(None), None);
    }

    #[test]
    fn a_principal_with_wake_loop_off_contributes_nothing() {
        let principal = agent_principal(false);
        assert_eq!(render_all(Some(&principal)), None);
    }

    #[test]
    fn a_wake_loop_eligible_principal_gets_the_bootstrap_text() {
        let principal = agent_principal(true);
        assert_eq!(render_all(Some(&principal)), Some(WAKE_LOOP_INSTRUCTIONS));
    }
}
