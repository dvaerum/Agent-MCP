//! OIDC's real `find_or_create_sso_user` reconciliation: reconcile-
//! by-subject (with R19-F1's legacy self-heal), verified-email-link,
//! or JIT-create. Port target: `agent_mcp/router/sso.py`'s
//! `find_or_create_sso_user` (Phase E2 PR22 step 3/8,
//! `conexus-router-oidc-reconcile`).
//!
//! **Deliberately NOT a call to `sso::find_or_create_sso_user`** --
//! that function is explicitly scoped to the proxy-header call site
//! (see its own module doc): no legacy-subject fallback, no
//! `SsoSubject` type, and `bootstrap_sysadmin` hardcoded `true`
//! (the proxy path's own always-on policy). OIDC needs a genuinely
//! wider contract -- `bootstrap_sysadmin` is caller-supplied here
//! (`AGENT_MCP_SSO_OIDC_DEFAULT_SYSADMIN`, defaulting OFF), and the
//! reconciliation key is a real [`SsoSubject`], not a bare `&str`.
//!
//! Matching algorithm, in order (byte-for-byte the real Python
//! docstring's own numbering, kept so a future reader can diff this
//! against the source directly):
//!
//! 1. **Stable subject.** [`SsoSubject::encode`] against
//!    `users.sso_subject`. R19-F1: on a miss, also try
//!    [`SsoSubject::legacy_lookup_key`] (already `None` when
//!    [`SsoSubject::is_ambiguous`] -- this function trusts that
//!    contract rather than re-deriving it). A legacy hit self-heals
//!    the row to the current tagged format via
//!    [`identity::upgrade_sso_subject`] so the fallback only ever
//!    fires once per user.
//! 2. **Verified-email link.** Only when the IdP asserted
//!    `email_verified`, closing the account-takeover vector an
//!    unverified email would open.
//! 3. **JIT-create.** A passwordless row (`password_hash` stays
//!    `NULL`); the username collision-suffix loop only ever engages
//!    for a genuinely new subject.
#![allow(dead_code)]

use rusqlite::Connection;

use crate::identity::{self, IdentityError, UserRow};
use crate::sso::sanitise_username;
use crate::sso_subject::SsoSubject;

/// Every input `find_or_create_oidc_user` needs from the real ID
/// token claims + config, grouped into one struct per this crate's
/// own convention for a function with this many parameters
/// (`SendMessageArgs`/`NewTask`'s precedent) rather than a raw
/// `#[allow(clippy::too_many_arguments)]`.
pub struct OidcReconcileInput<'a> {
    pub email: Option<&'a str>,
    pub email_verified: bool,
    pub preferred_username: Option<&'a str>,
    pub subject: &'a SsoSubject,
    /// Flips the sysadmin bit on a freshly JIT-created row --
    /// `AGENT_MCP_SSO_OIDC_DEFAULT_SYSADMIN`, defaulting off.
    pub default_is_sysadmin: bool,
    /// Gates the SEPARATE empty-table first-user sysadmin promotion
    /// inside `identity::create_user`'s own `BEGIN IMMEDIATE`
    /// bootstrap cluster (AC-R9-2) -- a fresh OIDC deploy's first IdP
    /// user is only auto-promoted when the operator opted in via the
    /// same env var above.
    pub bootstrap_sysadmin: bool,
}

fn touch_and_reload(conn: &Connection, user_id: &str, now: &str) -> Result<UserRow, IdentityError> {
    identity::touch_last_login(conn, user_id, now)?;
    identity::get_user_by_id(conn, user_id)?
        .ok_or_else(|| IdentityError::Db(rusqlite::Error::QueryReturnedNoRows))
}

/// Port of `find_or_create_sso_user`, scoped to the OIDC call site.
/// See the module doc for the 3-step algorithm.
pub fn find_or_create_oidc_user(
    conn: &mut Connection,
    input: &OidcReconcileInput,
    now: &str,
) -> Result<UserRow, IdentityError> {
    let encoded = input.subject.encode();

    // 1. Stable-subject reconciliation (current format, then the
    // R19-F1 legacy fallback + self-heal on a hit).
    let existing = match identity::find_user_by_sso_subject(conn, &encoded)? {
        Some(row) => Some(row),
        None => match input.subject.legacy_lookup_key() {
            Some(legacy_key) => match identity::find_user_by_sso_subject(conn, &legacy_key)? {
                Some(legacy_row) => {
                    identity::upgrade_sso_subject(
                        conn,
                        &legacy_row.user_id,
                        &legacy_key,
                        &encoded,
                    )?;
                    Some(legacy_row)
                }
                None => None,
            },
            None => None,
        },
    };
    if let Some(existing) = existing {
        return touch_and_reload(conn, &existing.user_id, now);
    }

    // 2. Verified-email link to a pre-existing local account.
    if input.email_verified {
        if let Some(email) = input.email {
            if let Some(linked) = identity::find_linkable_user_by_email(conn, email)? {
                identity::stamp_sso_subject_if_absent(conn, &linked.user_id, &encoded)?;
                return touch_and_reload(conn, &linked.user_id, now);
            }
        }
    }

    // 3. Genuinely new subject -> JIT-create a passwordless row.
    let base = sanitise_username(input.preferred_username.or(input.email).unwrap_or("user"));
    let mut candidate = base.clone();
    let mut suffix = 2;
    while identity::get_user_by_username(conn, &candidate)?.is_some() {
        candidate = format!("{base}-{suffix}");
        suffix += 1;
    }

    let user_id = identity::create_sso_user(
        conn,
        &candidate,
        &encoded,
        input.email,
        input.default_is_sysadmin,
        input.bootstrap_sysadmin,
        now,
    )?;
    touch_and_reload(conn, &user_id, now)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity::create_user;
    use crate::sso_subject::SsoSubjectValue;
    use conexus_db::schema::init_router_schema;

    const NOW: &str = "2026-09-06T00:00:00Z";
    const ISS: &str = "https://idp.example.test";

    fn conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        init_router_schema(&c).unwrap();
        c
    }

    fn subject(sub: &str) -> SsoSubject {
        SsoSubject::new(ISS, SsoSubjectValue::Str(sub.to_string())).unwrap()
    }

    fn input(subject: &SsoSubject) -> OidcReconcileInput<'_> {
        OidcReconcileInput {
            email: None,
            email_verified: false,
            preferred_username: Some("alice"),
            subject,
            default_is_sysadmin: false,
            bootstrap_sysadmin: false,
        }
    }

    #[test]
    fn a_new_subject_jit_creates_a_passwordless_row() {
        let mut c = conn();
        let sub = subject("alice-1");
        let row = find_or_create_oidc_user(&mut c, &input(&sub), NOW).unwrap();
        assert_eq!(row.username, "alice");
        assert_eq!(row.sso_subject.as_deref(), Some(sub.encode().as_str()));
        assert!(!row.is_sysadmin);
    }

    #[test]
    fn a_second_call_with_the_same_subject_reconciles_to_the_same_row() {
        let mut c = conn();
        let sub = subject("alice-1");
        let first = find_or_create_oidc_user(&mut c, &input(&sub), NOW).unwrap();
        let second = find_or_create_oidc_user(&mut c, &input(&sub), NOW).unwrap();
        assert_eq!(first.user_id, second.user_id);
    }

    #[test]
    fn a_colliding_preferred_username_gets_a_numeric_suffix() {
        let mut c = conn();
        create_user(
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
        let sub = subject("alice-1");
        let row = find_or_create_oidc_user(&mut c, &input(&sub), NOW).unwrap();
        assert_eq!(row.username, "alice-2");
    }

    #[test]
    fn a_verified_email_links_to_a_pre_existing_password_user() {
        let mut c = conn();
        let existing_id = create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            Some("bob@example.test"),
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();

        let sub = subject("bob-oidc-1");
        let mut req = input(&sub);
        req.email = Some("bob@example.test");
        req.email_verified = true;
        req.preferred_username = None;

        let row = find_or_create_oidc_user(&mut c, &req, NOW).unwrap();
        assert_eq!(row.user_id, existing_id);
        assert_eq!(row.sso_subject.as_deref(), Some(sub.encode().as_str()));
    }

    #[test]
    fn an_unverified_email_never_links_to_a_pre_existing_account() {
        // The verification gate closes the account-takeover vector: an
        // IdP-asserted but UNVERIFIED email must not seize a local
        // operator of that address.
        let mut c = conn();
        let existing_id = create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            Some("bob@example.test"),
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();

        let sub = subject("mallory-1");
        let mut req = input(&sub);
        req.email = Some("bob@example.test");
        req.email_verified = false;
        req.preferred_username = Some("mallory");

        let row = find_or_create_oidc_user(&mut c, &req, NOW).unwrap();
        assert_ne!(row.user_id, existing_id);
        assert_eq!(row.username, "mallory");
    }

    #[test]
    fn r19f1_a_legacy_untagged_row_reconciles_and_self_heals() {
        let mut c = conn();
        let legacy_key = "oidc:https://idp.example.test:alice-1";
        let existing_id = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            true,
            true,
            &[],
            NOW,
        )
        .unwrap();
        identity::stamp_sso_subject_if_absent(&c, &existing_id, legacy_key).unwrap();

        let sub = subject("alice-1");
        let row = find_or_create_oidc_user(&mut c, &input(&sub), NOW).unwrap();

        assert_eq!(row.user_id, existing_id, "must reconcile, not JIT-create");
        assert_eq!(row.sso_subject.as_deref(), Some(sub.encode().as_str()));
        assert!(
            row.is_sysadmin,
            "the pre-existing sysadmin bit must survive the self-heal"
        );
    }

    #[test]
    fn r20f1_a_differently_typed_claimant_cannot_take_over_a_legacy_row() {
        // An int sub is unconditionally ambiguous (SsoSubject::is_ambiguous),
        // so `legacy_lookup_key()` is None and this must NOT reconcile
        // into -- nor retag -- the victim's legacy row.
        let mut c = conn();
        let legacy_key = "oidc:https://idp.example.test:1";
        let victim_id = create_user(
            &mut c,
            "victim",
            "correct horse battery staple",
            None,
            true,
            true,
            &[],
            NOW,
        )
        .unwrap();
        identity::stamp_sso_subject_if_absent(&c, &victim_id, legacy_key).unwrap();

        let sub = SsoSubject::new(ISS, SsoSubjectValue::Int(1)).unwrap();
        assert_eq!(sub.legacy_lookup_key(), None);
        let mut req = input(&sub);
        req.preferred_username = Some("mallory");

        let row = find_or_create_oidc_user(&mut c, &req, NOW).unwrap();

        assert_ne!(row.user_id, victim_id);
        assert!(!row.is_sysadmin);
        let victim_after = identity::get_user_by_id(&c, &victim_id).unwrap().unwrap();
        assert_eq!(
            victim_after.sso_subject.as_deref(),
            Some(legacy_key),
            "the victim row must not have been retagged by another claimant"
        );
    }

    #[test]
    fn default_is_sysadmin_flips_the_bit_on_a_jit_created_row() {
        let mut c = conn();
        let sub = subject("alice-1");
        let mut req = input(&sub);
        req.default_is_sysadmin = true;
        let row = find_or_create_oidc_user(&mut c, &req, NOW).unwrap();
        assert!(row.is_sysadmin);
    }

    #[test]
    fn a_second_call_touches_last_login_each_time() {
        let mut c = conn();
        let sub = subject("alice-1");
        let first = find_or_create_oidc_user(&mut c, &input(&sub), NOW).unwrap();
        assert!(first.last_login_at.is_some());
        let second =
            find_or_create_oidc_user(&mut c, &input(&sub), "2026-09-07T00:00:00Z").unwrap();
        assert_eq!(
            second.last_login_at.as_deref(),
            Some("2026-09-07T00:00:00Z")
        );
    }
}
