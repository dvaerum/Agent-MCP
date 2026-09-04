//! `X-Agent-MCP-Forwarded-Operator` — the signed header the router
//! attaches when proxying a cookie-authenticated dashboard request to
//! the per-project backend.
//!
//! Faithful port of `agent_mcp/app/forwarding_header.py`'s `sign`/
//! `verify`, verified against real Python-computed golden vectors
//! (not just re-derived independently) — see this module's tests.
//!
//! Wire format: `"<operator_id>.<role>.<expiry-unix-seconds>.<hex-hmac>"`.
//! MAC = HMAC-SHA256 over `"{operator_id}.{role}.{expiry}"` UTF-8
//! bytes, lowercase hex, using the raw key bytes directly (no KDF).
//! `now`/`current time` is always an explicit parameter here, never
//! read from the wall clock internally — this crate's usual
//! discipline (see `conexus-db`'s chrono usage), and it's what makes
//! this module deterministically testable against fixed timestamps.

use hmac::{Hmac, Mac};
use sha2::Sha256;
use subtle::ConstantTimeEq;

/// The header name the router signs and the backend reads.
pub const HEADER_NAME: &str = "X-Agent-MCP-Forwarded-Operator";

/// Default signature lifetime (`sign`'s default TTL in the Python
/// source).
pub const DEFAULT_TTL_SEC: u64 = 30;

/// Default acceptance window (`verify`'s default `replay_window_sec`
/// in the Python source) — the header is accepted only while
/// `now < expiry <= now + replay_window_sec`; there is no separate
/// clock-skew allowance beyond this window.
pub const DEFAULT_REPLAY_WINDOW_SEC: u64 = 30;

/// The two roles a forwarded header may carry. Mirrors Python's
/// `VALID_ROLES = frozenset({"operator", "viewer"})`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ForwardedRole {
    Operator,
    Viewer,
}

impl ForwardedRole {
    fn as_str(self) -> &'static str {
        match self {
            ForwardedRole::Operator => "operator",
            ForwardedRole::Viewer => "viewer",
        }
    }

    fn parse(s: &str) -> Option<Self> {
        match s {
            "operator" => Some(ForwardedRole::Operator),
            "viewer" => Some(ForwardedRole::Viewer),
            _ => None,
        }
    }
}

type HmacSha256 = Hmac<Sha256>;

/// The exact MAC construction both `sign` and `verify` share:
/// HMAC-SHA256 over `"{operator_id}.{role}.{expiry}"`, lowercase hex.
fn hmac_hex(operator_id: &str, role: ForwardedRole, expiry: u64, key: &[u8]) -> String {
    let message = format!("{operator_id}.{}.{expiry}", role.as_str());
    // SAFETY/correctness note: HMAC accepts a key of any length (it
    // internally pads/hashes down as needed per RFC 2104) -- this
    // can never fail for a byte slice key, matching Python's
    // `hmac.new(key, message, hashlib.sha256)` (no length
    // constraint on `key`).
    let mut mac = HmacSha256::new_from_slice(key).expect("HMAC-SHA256 accepts a key of any length");
    mac.update(message.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

/// Sign `operator_id`/`role` for the next `ttl_sec` seconds, from
/// `now` (unix seconds). Returns the full 4-field header value.
pub fn sign(operator_id: &str, role: ForwardedRole, key: &[u8], now: u64, ttl_sec: u64) -> String {
    let expiry = now + ttl_sec;
    let mac = hmac_hex(operator_id, role, expiry, key);
    format!("{operator_id}.{}.{expiry}.{mac}", role.as_str())
}

/// Verify `header_value` against `key` at time `now` (unix seconds),
/// accepting a signature whose `expiry` falls within
/// `replay_window_sec` of `now`. Returns `(operator_id, role)` on
/// success, `None` on ANY failure — malformed shape, unrecognized
/// role, expired, too-far-future expiry, or a MAC mismatch. Never
/// panics; there is no signal to distinguish failure reasons on
/// purpose (matches Python's `verify() -> Optional[tuple]`).
///
/// The role check runs BEFORE the MAC compare (SEC-1, ported
/// verbatim): an unrecognized role is rejected regardless of whether
/// a valid-looking MAC follows it.
pub fn verify(
    header_value: &str,
    key: &[u8],
    now: u64,
    replay_window_sec: u64,
) -> Option<(String, ForwardedRole)> {
    let parts: Vec<&str> = header_value.split('.').collect();
    let [operator_id, role_str, expiry_str, presented_mac] = parts[..] else {
        return None;
    };
    if operator_id.is_empty()
        || role_str.is_empty()
        || expiry_str.is_empty()
        || presented_mac.is_empty()
    {
        return None;
    }
    let role = ForwardedRole::parse(role_str)?;
    let expiry: u64 = expiry_str.parse().ok()?;
    if expiry <= now {
        return None;
    }
    if expiry - now > replay_window_sec {
        return None;
    }
    let expected_mac = hmac_hex(operator_id, role, expiry, key);
    // Constant-time compare -- see the module doc; a literal `==`
    // here would reopen a timing side-channel Python's
    // `hmac.compare_digest` closes.
    if expected_mac
        .as_bytes()
        .ct_eq(presented_mac.as_bytes())
        .into()
    {
        Some((operator_id.to_string(), role))
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Real 32-byte key: bytes 0x00..=0x1f, matching the golden
    // vectors below (generated by actually calling the Python
    // `forwarding_header.sign`/`_hmac_hex` functions, not derived
    // independently -- proves wire-format interop, not just internal
    // self-consistency).
    fn golden_key() -> Vec<u8> {
        (0u8..32).collect()
    }

    #[test]
    fn golden_vector_operator_role_matches_python() {
        // python: fh.sign("alice", "operator", key, ttl_sec=30, _now=1000000000)
        let header = sign(
            "alice",
            ForwardedRole::Operator,
            &golden_key(),
            1_000_000_000,
            30,
        );
        assert_eq!(
            header,
            "alice.operator.1000000030.31237920c6990db39104b533aa7541d848dd7ef4818f1602997f7dd162bc52eb"
        );
    }

    #[test]
    fn golden_vector_viewer_role_matches_python() {
        // python: fh.sign("bob-42", "viewer", key, ttl_sec=30, _now=1700000000)
        let header = sign(
            "bob-42",
            ForwardedRole::Viewer,
            &golden_key(),
            1_700_000_000,
            30,
        );
        assert_eq!(
            header,
            "bob-42.viewer.1700000030.66d542ae59dbdec42e408e1b794f67672f14f6f44af2ee7e5f0d9c54a605969e"
        );
    }

    #[test]
    fn a_python_golden_header_verifies_successfully() {
        let header = "alice.operator.1000000030.31237920c6990db39104b533aa7541d848dd7ef4818f1602997f7dd162bc52eb";
        let result = verify(header, &golden_key(), 1_000_000_010, 30);
        assert_eq!(result, Some(("alice".to_string(), ForwardedRole::Operator)));
    }

    #[test]
    fn sign_then_verify_round_trips() {
        let header = sign("carol", ForwardedRole::Viewer, &golden_key(), 500, 30);
        let result = verify(&header, &golden_key(), 510, 30);
        assert_eq!(result, Some(("carol".to_string(), ForwardedRole::Viewer)));
    }

    #[test]
    fn verify_rejects_wrong_field_count() {
        assert_eq!(verify("a.b.c", &golden_key(), 0, 30), None);
        assert_eq!(verify("a.b.c.d.e", &golden_key(), 0, 30), None);
    }

    #[test]
    fn verify_rejects_an_empty_field() {
        assert_eq!(verify(".operator.100.mac", &golden_key(), 50, 30), None);
        assert_eq!(verify("alice..100.mac", &golden_key(), 50, 30), None);
        assert_eq!(verify("alice.operator..mac", &golden_key(), 50, 30), None);
        assert_eq!(verify("alice.operator.100.", &golden_key(), 50, 30), None);
    }

    #[test]
    fn verify_rejects_an_unrecognized_role_regardless_of_mac() {
        // Even a syntactically-plausible hex string in the MAC
        // position must not matter -- the role check runs first.
        let bogus = format!(
            "alice.admin.{}.{}",
            1000,
            hmac_hex("alice", ForwardedRole::Operator, 1000, &golden_key())
        );
        assert_eq!(verify(&bogus, &golden_key(), 500, 30), None);
    }

    #[test]
    fn verify_rejects_a_non_integer_expiry() {
        let mac = hmac_hex("alice", ForwardedRole::Operator, 0, &golden_key());
        let header = format!("alice.operator.not-a-number.{mac}");
        assert_eq!(verify(&header, &golden_key(), 0, 30), None);
    }

    #[test]
    fn verify_rejects_an_already_expired_header() {
        let header = sign("alice", ForwardedRole::Operator, &golden_key(), 1000, 30);
        // expiry == 1030; now == 1030 means expiry <= now -> reject.
        assert_eq!(verify(&header, &golden_key(), 1030, 30), None);
        assert_eq!(verify(&header, &golden_key(), 1031, 30), None);
    }

    #[test]
    fn verify_accepts_up_to_and_including_the_replay_window_boundary() {
        let header = sign("alice", ForwardedRole::Operator, &golden_key(), 1000, 30);
        // expiry == 1030. now == 1000 -> expiry - now == 30 == window -> accept (NOT `>`).
        assert!(verify(&header, &golden_key(), 1000, 30).is_some());
        // now == 1029 -> expiry - now == 1, well inside the window.
        assert!(verify(&header, &golden_key(), 1029, 30).is_some());
    }

    #[test]
    fn verify_rejects_one_second_past_the_replay_window_boundary() {
        let header = sign("alice", ForwardedRole::Operator, &golden_key(), 1000, 30);
        // A header signed far enough in the future that, from this
        // verifier's clock, expiry - now > replay_window_sec.
        assert_eq!(verify(&header, &golden_key(), 999, 30), None);
    }

    #[test]
    fn verify_rejects_a_tampered_operator_id() {
        let header = sign("alice", ForwardedRole::Operator, &golden_key(), 1000, 30);
        let tampered = header.replacen("alice", "mallory", 1);
        assert_eq!(verify(&tampered, &golden_key(), 1010, 30), None);
    }

    #[test]
    fn verify_rejects_a_tampered_mac() {
        let mut header = sign("alice", ForwardedRole::Operator, &golden_key(), 1000, 30);
        header.pop();
        header.push('0'); // flip the last hex nibble
        assert_eq!(verify(&header, &golden_key(), 1010, 30), None);
    }

    #[test]
    fn verify_rejects_the_wrong_key() {
        let header = sign("alice", ForwardedRole::Operator, &golden_key(), 1000, 30);
        let wrong_key: Vec<u8> = (1u8..=32).collect();
        assert_eq!(verify(&header, &wrong_key, 1010, 30), None);
    }
}
