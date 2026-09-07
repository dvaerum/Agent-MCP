//! Cookie -> forwarding-header role resolution for `mcp_handler::
//! backend_api_handler`'s REST proxy path (`/agent-mcp/api/<project>/*`
//! -- the dashboard's `all-data`/`events` calls). Closes the gap
//! `mcp_handler.rs`'s own module doc and `proxy_core.rs`'s own module
//! doc both flag: `backend_api_handler` forwarded a browser's
//! `agent_mcp_session` cookie to `conexus-backend` with no forwarding-
//! header bridge at all, and the Rust backend's `rest_gate`/
//! `rest_principal` (2026-09-05, Phase E1) deliberately dropped the
//! raw-cookie admission door Python's old `require_operator_session`
//! kept as a defence-in-depth fallback -- so every cookie-authenticated
//! dashboard request 401s once a project runs the Rust backend.
//!
//! Port of `agent_mcp/router/app.py::_forwarding_header_from_cookie`'s
//! role-resolution half only -- the `_ensure`/HMAC-key-read/`sign()`
//! half stays in `mcp_handler.rs` itself, which already owns those
//! dependencies (`RuntimeStore`, `sock_dir`, `EnsureConfig`) for its
//! existing bearer-path proxy call, so there is nothing to duplicate
//! here.
//!
//! **Deliberately no sysadmin bypass** -- matches Python's own
//! docstring for `_forwarding_header_from_cookie` verbatim: "sysadmin
//! bypass intentionally NOT applied here: an operator who is not a
//! project member should not be silently elevated by the sysadmin flag
//! on this transport." This is a DIFFERENT question from the one
//! `session_gate.rs`'s `evaluate_session_gate` answers (the dashboard's
//! own admin-UI gate, which DOES let a sysadmin through regardless of
//! membership so the admin UI shell renders) -- conflating the two
//! would let a sysadmin's minted forwarding header claim membership in
//! a project they were never granted, which is exactly the check this
//! module exists to enforce.
//!
//! **One collapsed step vs. Python**: Python calls
//! `identity.is_project_member` THEN `group_resolver.
//! resolve_user_project_role` as two separate calls. This crate's
//! [`conexus_db::group_membership_repository::resolve_user_project_role`]
//! already returns `None` for a non-member (checked across both direct
//! rows and every group the user transitively belongs to), making a
//! separate membership probe redundant -- there is no membership state
//! this resolver can see that a `None` role wouldn't already reflect.

use chrono::{DateTime, Utc};
use rusqlite::Connection;

use conexus_auth::forwarding_header::ForwardedRole;

use crate::identity::IdentityError;
use crate::login;

/// Resolve `cookie_header`'s live operator session, then that
/// operator's REAL role on `project_name`.
///
/// Returns `Ok(None)` (never an error) for: no cookie header, an empty
/// cookie value, a cookie that doesn't resolve to a live session/user
/// (matches [`login::resolve_current_user`]'s own "missing/expired is
/// not an error" contract), or a resolved user with no project-role
/// row covering `project_name` at all -- directly or via any group
/// (not a member). A genuine session-lookup DB error still propagates
/// (this crate's "distinguish 'not available' from 'available but
/// broken'" convention); the group/role lookup below degrades to
/// `None` instead on its own DB error, matching Python's own defensive
/// `except Exception: return None` around that specific call.
pub fn resolve_cookie_project_role(
    conn: &Connection,
    cookie_header: Option<&str>,
    project_name: &str,
    now: DateTime<Utc>,
) -> Result<Option<(String, ForwardedRole)>, IdentityError> {
    let now_str = now.to_rfc3339();
    let Some(user) = login::resolve_current_user(conn, cookie_header, &now_str)? else {
        return Ok(None);
    };

    let groups =
        conexus_db::group_membership_repository::resolve_user_groups(conn, &user.user_id).ok();
    let role_str = conexus_db::group_membership_repository::resolve_user_project_role(
        conn,
        &user.user_id,
        project_name,
        groups.as_ref(),
    )
    .ok()
    .flatten();

    let Some(role_str) = role_str else {
        // Not a member -- of THIS project specifically. `groups`/`role_str`
        // are both scoped to `project_name` alone, so hitting this
        // function again with a different URL segment can only ever
        // resolve that OTHER project's own membership row, never reuse
        // this one's.
        return Ok(None);
    };
    let role = match role_str.as_str() {
        "operator" => ForwardedRole::Operator,
        "viewer" => ForwardedRole::Viewer,
        // Defensive: the schema's CHECK constraint only ever allows
        // "operator"/"viewer" to be written, but an unrecognized value
        // must deny, not default -- mirrors Python's own "role is None
        // ... denied rather than defaulted" comment for this exact
        // case.
        _ => return Ok(None),
    };
    Ok(Some((user.user_id, role)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity;
    use conexus_db::schema::init_router_schema;

    const NOW_STR: &str = "2026-01-01T00:00:00.000+00:00";

    fn now_dt() -> DateTime<Utc> {
        "2026-01-01T00:00:00Z".parse().unwrap()
    }

    fn conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        c
    }

    fn seed_user(c: &mut Connection, username: &str) -> String {
        identity::create_user(
            c,
            username,
            "correct horse battery staple",
            None,
            false,
            false,
            &[],
            NOW_STR,
        )
        .unwrap()
    }

    fn cookie_for(c: &Connection, user_id: &str) -> String {
        let sid =
            identity::create_session(c, user_id, NOW_STR, "2026-02-01T00:00:00.000+00:00").unwrap();
        format!("{}={}", login::SESSION_COOKIE_NAME, sid)
    }

    #[test]
    fn no_cookie_header_resolves_to_none() {
        let c = conn();
        let result = resolve_cookie_project_role(&c, None, "proj-a", now_dt()).unwrap();
        assert!(result.is_none());
    }

    #[test]
    fn an_empty_or_garbage_cookie_resolves_to_none() {
        let c = conn();
        assert!(
            resolve_cookie_project_role(&c, Some("agent_mcp_session="), "proj-a", now_dt())
                .unwrap()
                .is_none()
        );
        assert!(resolve_cookie_project_role(
            &c,
            Some("agent_mcp_session=not-a-real-session"),
            "proj-a",
            now_dt()
        )
        .unwrap()
        .is_none());
    }

    #[test]
    fn a_project_member_resolves_their_real_operator_role() {
        let mut c = conn();
        let user_id = seed_user(&mut c, "alice");
        identity::add_project_membership(&c, &user_id, "proj-a").unwrap();
        let cookie = cookie_for(&c, &user_id);

        let (resolved_id, role) =
            resolve_cookie_project_role(&c, Some(&cookie), "proj-a", now_dt())
                .unwrap()
                .expect("a project member must resolve a role");
        assert_eq!(resolved_id, user_id);
        assert_eq!(role, ForwardedRole::Operator);
    }

    #[test]
    fn a_viewer_tier_member_resolves_the_viewer_role_not_operator() {
        // SEC-1 parity with `forwarding_header.rs`'s own module doc:
        // signing a FIXED role would let a viewer-tier operator collect
        // the full operator capability bundle over this transport.
        let mut c = conn();
        let user_id = seed_user(&mut c, "bob");
        identity::grant_project_membership(&c, "proj-a", Some(&user_id), None, "viewer").unwrap();
        let cookie = cookie_for(&c, &user_id);

        let (_id, role) = resolve_cookie_project_role(&c, Some(&cookie), "proj-a", now_dt())
            .unwrap()
            .expect("a viewer member must still resolve a role");
        assert_eq!(role, ForwardedRole::Viewer);
    }

    #[test]
    fn a_non_member_resolves_to_none_even_with_a_live_session() {
        let mut c = conn();
        let user_id = seed_user(&mut c, "carol");
        // No project_membership row for "proj-a" at all.
        let cookie = cookie_for(&c, &user_id);

        let result = resolve_cookie_project_role(&c, Some(&cookie), "proj-a", now_dt()).unwrap();
        assert!(
            result.is_none(),
            "a live session with no membership row must not resolve a role"
        );
    }

    #[test]
    fn membership_in_one_project_does_not_leak_into_another_via_the_url_segment() {
        // The exact cross-tenant scenario the task brief calls out:
        // a member of "proj-a" must not be able to mint a header for
        // "proj-b" by hitting a different URL segment.
        let mut c = conn();
        let user_id = seed_user(&mut c, "dave");
        identity::add_project_membership(&c, &user_id, "proj-a").unwrap();
        let cookie = cookie_for(&c, &user_id);

        assert!(
            resolve_cookie_project_role(&c, Some(&cookie), "proj-b", now_dt())
                .unwrap()
                .is_none(),
            "a proj-a member must not resolve a role for proj-b"
        );
        assert!(
            resolve_cookie_project_role(&c, Some(&cookie), "proj-a", now_dt())
                .unwrap()
                .is_some(),
            "...but must still resolve one for the project they ARE a member of"
        );
    }

    #[test]
    fn an_expired_session_resolves_to_none() {
        let mut c = conn();
        let user_id = seed_user(&mut c, "erin");
        identity::add_project_membership(&c, &user_id, "proj-a").unwrap();
        let sid = identity::create_session(
            &c,
            &user_id,
            NOW_STR,
            "2026-01-01T00:00:01.000+00:00", // expires 1s after NOW_STR
        )
        .unwrap();
        let cookie = format!("{}={}", login::SESSION_COOKIE_NAME, sid);

        // Resolve well past expiry.
        let later: DateTime<Utc> = "2026-06-01T00:00:00Z".parse().unwrap();
        let result = resolve_cookie_project_role(&c, Some(&cookie), "proj-a", later).unwrap();
        assert!(
            result.is_none(),
            "an expired session must not resolve a role"
        );
    }

    #[test]
    fn a_project_member_via_group_membership_resolves_a_role() {
        let mut c = conn();
        let user_id = seed_user(&mut c, "frank");
        let group =
            conexus_db::group_membership_repository::create_group(&c, "team-a", false, NOW_STR)
                .unwrap();
        conexus_db::group_membership_repository::add_group_member(
            &c,
            &group.group_id,
            Some(&user_id),
            None,
            NOW_STR,
        )
        .unwrap();
        identity::grant_project_membership(&c, "proj-a", None, Some(&group.group_id), "operator")
            .unwrap();
        let cookie = cookie_for(&c, &user_id);

        let (_id, role) = resolve_cookie_project_role(&c, Some(&cookie), "proj-a", now_dt())
            .unwrap()
            .expect("a group-inherited member must resolve a role");
        assert_eq!(role, ForwardedRole::Operator);
    }
}
