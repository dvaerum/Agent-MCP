//! The OIDC per-flow state cookie: state + PKCE verifier + nonce,
//! bound to the operator's browser across the redirect round-trip to
//! the IdP and back. Port target: `agent_mcp/router/sso.py`'s
//! `_FlowState`/`_encode_flow_cookie`/`_decode_flow_cookie` (Phase E2
//! PR22 step 6/8, `conexus-router-oidc-flow-state`).
//!
//! Genuinely independent of PR22 steps 5/7/8 (the `openidconnect`
//! crate integration and the real axum handlers) -- this is pure
//! base64url(JSON) codec logic with no HTTP-client or ID-token-claims
//! dependency, so it doesn't wait on the still-open typed-vs-raw-
//! claims question those steps carry.
//!
//! The state + verifier bind the returned authorization code to THIS
//! browser (so a phishing IdP can't replay another user's in-flight
//! flow); the nonce additionally binds the returned `id_token` to
//! this specific auth attempt (OIDC anti-replay). Packed as
//! base64url(JSON) since every field is a short ASCII string --
//! reuses `login::parse_cookie_header` for extraction and
//! `json_sanitize::decode_untrusted_body` for the untrusted decode
//! (this cookie is UNSIGNED, hence attacker-craftable like any other
//! untrusted decode point).
#![allow(dead_code)]

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use serde_json::json;

use crate::json_sanitize::decode_untrusted_body;
use crate::login::SessionCookie;

/// Port of `_FLOW_COOKIE_NAME`/`_FLOW_COOKIE_PATH`/`_FLOW_COOKIE_MAX_AGE`.
pub const FLOW_COOKIE_NAME: &str = "agent_mcp_sso_flow";
pub const FLOW_COOKIE_PATH: &str = "/agent-mcp/sso/";
/// 10 minutes -- plenty for the round-trip to the IdP and back.
pub const FLOW_COOKIE_MAX_AGE_SECS: i64 = 10 * 60;

/// Port of `_FlowState`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FlowState {
    pub state: String,
    pub code_verifier: String,
    pub nonce: String,
}

/// Port of `_encode_flow_cookie`. base64url(JSON), no padding (a
/// literal `=` in a cookie value needs percent-encoding most clients
/// mishandle -- Python strips the padding on encode and re-derives it
/// on decode for the same reason).
pub fn encode_flow_cookie(state: &FlowState) -> String {
    let payload = json!({
        "state": state.state,
        "verifier": state.code_verifier,
        "nonce": state.nonce,
    })
    .to_string();
    URL_SAFE_NO_PAD.encode(payload.as_bytes())
}

/// Port of `_decode_flow_cookie`. `None` on ANY decode failure --
/// malformed base64, malformed/oversized JSON, a missing `state`/
/// `verifier`, or a missing/empty `nonce`.
///
/// N1 hardening: routed through [`decode_untrusted_body`] (strips
/// hidden-format Unicode, guards excessive nesting) rather than a
/// bare JSON parse, since this cookie is unsigned and therefore
/// attacker-craftable like any other untrusted decode point.
///
/// AC-1 (round-3 finding): fail closed on a missing/empty nonce.
/// Authlib's own `validate_nonce` is gated on `if nonce_value:` -- an
/// EMPTY expected nonce skips the comparison entirely, so an
/// `id_token` minted for a DIFFERENT auth request would be accepted.
/// A nonce-less cookie must be treated as an invalid flow rather than
/// one that silently disables anti-replay.
pub fn decode_flow_cookie(raw: &str) -> Option<FlowState> {
    let decoded = URL_SAFE_NO_PAD.decode(raw).ok()?;
    let parsed = decode_untrusted_body(&decoded).ok()?;

    let nonce = parsed.get("nonce")?.as_str()?.to_string();
    if nonce.is_empty() {
        return None;
    }
    let state = parsed.get("state")?.as_str()?.to_string();
    let code_verifier = parsed.get("verifier")?.as_str()?.to_string();

    Some(FlowState {
        state,
        code_verifier,
        nonce,
    })
}

/// Port of `init_oidc_login_handler`'s `response.set_cookie(...)`
/// call for the flow cookie. Reuses [`SessionCookie`] directly --
/// that struct is already a fully generic name/value/path/attribute
/// bag, not session-specific by construction, so no second cookie
/// type is needed for a second cookie NAME.
pub fn set_flow_cookie(cookie_value: &str, secure: bool) -> SessionCookie {
    SessionCookie {
        name: FLOW_COOKIE_NAME,
        value: cookie_value.to_string(),
        path: FLOW_COOKIE_PATH.to_string(),
        http_only: true,
        secure,
        same_site: crate::login::SameSite::Lax,
        max_age: FLOW_COOKIE_MAX_AGE_SECS,
    }
}

/// Port of the callback handler's own flow-cookie clear (`Max-Age=0`,
/// empty value) -- the cookie is consumed exactly once, by the
/// callback that follows the redirect it was minted for.
pub fn clear_flow_cookie(secure: bool) -> SessionCookie {
    SessionCookie {
        name: FLOW_COOKIE_NAME,
        value: String::new(),
        path: FLOW_COOKIE_PATH.to_string(),
        http_only: true,
        secure,
        same_site: crate::login::SameSite::Lax,
        max_age: 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn state() -> FlowState {
        FlowState {
            state: "abc123".to_string(),
            code_verifier: "verifier-xyz".to_string(),
            nonce: "nonce-456".to_string(),
        }
    }

    #[test]
    fn encode_decode_roundtrips() {
        let original = state();
        let cookie = encode_flow_cookie(&original);
        assert_eq!(decode_flow_cookie(&cookie), Some(original));
    }

    #[test]
    fn encode_produces_unpadded_base64url() {
        let cookie = encode_flow_cookie(&state());
        assert!(!cookie.contains('='));
        assert!(!cookie.contains('+'));
        assert!(!cookie.contains('/'));
    }

    #[test]
    fn decode_rejects_malformed_base64() {
        assert_eq!(decode_flow_cookie("not valid base64!!"), None);
    }

    #[test]
    fn decode_rejects_base64_that_is_not_json() {
        let raw = URL_SAFE_NO_PAD.encode(b"not json at all");
        assert_eq!(decode_flow_cookie(&raw), None);
    }

    #[test]
    fn decode_rejects_a_missing_nonce() {
        let payload = json!({"state": "s", "verifier": "v"}).to_string();
        let raw = URL_SAFE_NO_PAD.encode(payload.as_bytes());
        assert_eq!(decode_flow_cookie(&raw), None);
    }

    #[test]
    fn decode_rejects_an_empty_nonce() {
        // AC-1: an empty nonce must be treated identically to a
        // missing one, not accepted as "no nonce check needed".
        let payload = json!({"state": "s", "verifier": "v", "nonce": ""}).to_string();
        let raw = URL_SAFE_NO_PAD.encode(payload.as_bytes());
        assert_eq!(decode_flow_cookie(&raw), None);
    }

    #[test]
    fn decode_rejects_a_missing_state() {
        let payload = json!({"verifier": "v", "nonce": "n"}).to_string();
        let raw = URL_SAFE_NO_PAD.encode(payload.as_bytes());
        assert_eq!(decode_flow_cookie(&raw), None);
    }

    #[test]
    fn decode_rejects_a_missing_verifier() {
        let payload = json!({"state": "s", "nonce": "n"}).to_string();
        let raw = URL_SAFE_NO_PAD.encode(payload.as_bytes());
        assert_eq!(decode_flow_cookie(&raw), None);
    }

    #[test]
    fn decode_rejects_hidden_unicode_via_the_shared_untrusted_decode_seam() {
        // Confirms this genuinely routes through decode_untrusted_body
        // rather than a bare serde_json::from_slice -- a zero-width
        // space (U+200B) embedded in a string leaf must be stripped,
        // not just tolerated.
        let payload = format!(
            r#"{{"state": "s{}", "verifier": "v", "nonce": "n"}}"#,
            '\u{200b}'
        );
        let raw = URL_SAFE_NO_PAD.encode(payload.as_bytes());
        let decoded = decode_flow_cookie(&raw).unwrap();
        assert_eq!(decoded.state, "s");
    }

    #[test]
    fn set_flow_cookie_has_the_documented_attributes() {
        let cookie = set_flow_cookie("encoded-value", true);
        assert_eq!(cookie.name, FLOW_COOKIE_NAME);
        assert_eq!(cookie.value, "encoded-value");
        assert_eq!(cookie.path, FLOW_COOKIE_PATH);
        assert!(cookie.http_only);
        assert!(cookie.secure);
        assert_eq!(cookie.max_age, FLOW_COOKIE_MAX_AGE_SECS);
    }

    #[test]
    fn clear_flow_cookie_zeroes_the_value_and_max_age() {
        let cookie = clear_flow_cookie(false);
        assert_eq!(cookie.name, FLOW_COOKIE_NAME);
        assert_eq!(cookie.value, "");
        assert_eq!(cookie.max_age, 0);
        assert!(!cookie.secure);
    }
}
