//! The stable OIDC `(iss, sub)` reconciliation key, as a value type.
//! Port target: `agent_mcp/router/sso.py`'s `SsoSubject` (Phase E2
//! PR22 step 1, `conexus-router-sso-subject`).
//!
//! Per the OIDC spec `sub` is unique+stable only WITHIN an issuer, so
//! both parts are needed. The real Python type replaced a bare
//! f-string that was the site of five consecutive pentest findings
//! (R16-F1 -> R20-F1) because that one interpolation silently carried
//! FOUR distinct responsibilities; here each is a separate,
//! independently-testable piece, matching the Python source's own
//! documented split:
//!
//!   (a) `encode()`            -- the persisted `users.sso_subject` key.
//!   (b) `type_tag()`          -- the scalar-type discriminator inside it.
//!   (c) `legacy_lookup_key()` -- the pre-R18-F1 UNTAGGED key, for
//!       finding (never minting) a row written before the retag.
//!   (d) `is_ambiguous()`      -- the refuse-on-ambiguity rule that says
//!       when (c) must be withheld entirely.
//!
//! `decode()` is (a)'s inverse, so the persisted format is
//! round-trippable and testable as a property rather than by reading
//! an f-string.
//!
//! **This is a PERSISTED wire format** -- `encode()`'s bytes are a real
//! DB value (`users.sso_subject`). Changing them orphans every
//! existing SSO row (that is R19-F1) and needs a migration, not a
//! refactor.
//!
//! **Equality is TYPE-EXACT** (`SsoSubject(i, true) != SsoSubject(i, 1)`)
//! even though Python's own `True == 1 == 1.0`. Rust's enum-variant
//! equality already gives this for free (an `Int` value and a `Bool`
//! value can never structurally match), unlike Python where `bool`
//! being an `int` subclass needed a deliberate type-tag comparison to
//! avoid collapsing the R18-F1 collision set.
//!
//! **Known, documented divergence**: `sub` is represented as `i64`/
//! `f64` here, not Python's arbitrary-precision `int`. A real OIDC
//! `sub` claim is essentially always an opaque string or a normal-
//! range integer -- no real IdP is expected to send an integer
//! subject exceeding `i64::MAX`. `python_float_str`'s formatting also
//! never switches to scientific notation the way Python's `str(float)`
//! does past roughly 1e16/1e-4 -- again not a realistic shape for a
//! `sub` claim. Both are accepted, narrow divergences from the
//! Python source's own property-fuzz test range, not full wire-format
//! parity claims for pathological values.
//!
//! `#![allow(dead_code)]`: this is a BINARY crate and this PR (1/8 of
//! PR22's own breakdown) has no real consumer yet -- the
//! `oidc-reconcile`/`oidc-handlers` PRs later in the sequence are.
//! Same precedent as `mount.rs`/`login.rs`.
#![allow(dead_code)]

use serde_json::Value;

const OIDC_SUBJECT_PREFIX: &str = "oidc:";

/// The four JSON-scalar shapes an OIDC `sub` (or `iss`, though `iss`
/// is always `str` in practice) claim can take. Mirrors Python's
/// `SsoSubjectValue = str | int | float | bool` union.
#[derive(Debug, Clone)]
pub enum SsoSubjectValue {
    Str(String),
    Int(i64),
    Float(f64),
    Bool(bool),
}

// Manual Eq/Hash: `f64` doesn't derive either (NaN != NaN), but the
// bit pattern does have well-defined equality/hash -- and every other
// variant needs the SAME discriminant-aware treatment `derive` would
// give, so hand-rolling both together keeps them from drifting apart.
impl PartialEq for SsoSubjectValue {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Str(a), Self::Str(b)) => a == b,
            (Self::Int(a), Self::Int(b)) => a == b,
            (Self::Float(a), Self::Float(b)) => a.to_bits() == b.to_bits(),
            (Self::Bool(a), Self::Bool(b)) => a == b,
            _ => false,
        }
    }
}
impl Eq for SsoSubjectValue {}

impl std::hash::Hash for SsoSubjectValue {
    fn hash<H: std::hash::Hasher>(&self, state: &mut H) {
        match self {
            Self::Str(s) => {
                0u8.hash(state);
                s.hash(state);
            }
            Self::Int(i) => {
                1u8.hash(state);
                i.hash(state);
            }
            Self::Float(f) => {
                2u8.hash(state);
                f.to_bits().hash(state);
            }
            Self::Bool(b) => {
                3u8.hash(state);
                b.hash(state);
            }
        }
    }
}

/// Construction-rejection reasons -- port of `SsoSubject.__post_init__`'s
/// two raise sites.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SsoSubjectError {
    EmptyIssuer,
    EmptySubject,
}

impl std::fmt::Display for SsoSubjectError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmptyIssuer => write!(f, "SsoSubject requires a non-empty str issuer"),
            Self::EmptySubject => write!(f, "SsoSubject requires a non-empty sub"),
        }
    }
}
impl std::error::Error for SsoSubjectError {}

/// The stable `(iss, sub)` reconciliation key. See the module doc.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct SsoSubject {
    iss: String,
    sub: SsoSubjectValue,
}

/// Python's `str(float)` -- NOT the same as Rust's `Display` for
/// `f64`, which omits the trailing `.0` on a whole-number float
/// (`format!("{}", 1.0)` is `"1"`, but `str(1.0)` is `"1.0"`). Needed
/// for byte-for-byte wire-format parity on the `encode()` output.
fn python_float_str(f: f64) -> String {
    if f.is_nan() {
        return "nan".to_string();
    }
    if f.is_infinite() {
        return if f > 0.0 { "inf" } else { "-inf" }.to_string();
    }
    let s = format!("{f}");
    if s.contains('.') || s.contains('e') || s.contains('E') {
        s
    } else {
        format!("{s}.0")
    }
}

/// The inverse of `f"{sub}"` for one specific type tag -- the one
/// place that knows which strings a given scalar type could have
/// produced. `None` when `raw` is NOT something that type's `str()`
/// can emit (`"007"` is not a possible `str(int)`, `"1"` is not a
/// possible `str(float)`, `"true"` is not a possible `str(bool)`).
/// Shared by `decode()` and `is_ambiguous()` so they can't drift
/// apart the way R19-F1's fallback and R18-F1's tag did (per the
/// Python source's own module doc).
fn parse_tagged_scalar(tag: &str, raw: &str) -> Option<SsoSubjectValue> {
    match tag {
        "str" => {
            // Every non-empty string is a possible `str(str)`; the
            // empty string is not a usable identity.
            if raw.is_empty() {
                None
            } else {
                Some(SsoSubjectValue::Str(raw.to_string()))
            }
        }
        "bool" => match raw {
            "True" => Some(SsoSubjectValue::Bool(true)),
            "False" => Some(SsoSubjectValue::Bool(false)),
            _ => None,
        },
        "int" => {
            let as_int: i64 = raw.parse().ok()?;
            (as_int.to_string() == raw).then_some(SsoSubjectValue::Int(as_int))
        }
        "float" => {
            let as_float: f64 = raw.parse().ok()?;
            (python_float_str(as_float) == raw).then_some(SsoSubjectValue::Float(as_float))
        }
        _ => None,
    }
}

const SCALAR_TAGS: [&str; 4] = ["str", "int", "float", "bool"];

impl SsoSubject {
    /// Direct construction, enforcing the same acceptance rules as
    /// `from_claims()`. Used by tests and by any call site that
    /// already holds a validated scalar (as opposed to raw,
    /// untrusted claim data -- see `from_claims`).
    pub fn new(iss: impl Into<String>, sub: SsoSubjectValue) -> Result<Self, SsoSubjectError> {
        let iss = iss.into();
        if iss.is_empty() {
            return Err(SsoSubjectError::EmptyIssuer);
        }
        if let SsoSubjectValue::Str(ref s) = sub {
            if s.is_empty() {
                return Err(SsoSubjectError::EmptySubject);
            }
        }
        Ok(Self { iss, sub })
    }

    /// Build from raw id_token claims, or `None` if unusable. `None`
    /// means "this id_token carries no usable stable identity" -- the
    /// caller falls back to the verified-email / JIT-create path
    /// rather than keying reconciliation on a partial identifier.
    ///
    /// R17-F1: `sub` is deliberately wider than `str` -- degrading a
    /// numeric IdP subject id to `None` re-mints an orphan row every
    /// login. A dict/array `sub` (a misserialised multi-valued
    /// attribute) is still refused: a stringified blob is not an
    /// identity.
    pub fn from_claims(iss: Option<&str>, sub: Option<&Value>) -> Option<Self> {
        let iss = iss.filter(|s| !s.is_empty())?;
        let sub = sub?;
        let value = match sub {
            Value::String(s) if !s.is_empty() => SsoSubjectValue::Str(s.clone()),
            Value::Bool(b) => SsoSubjectValue::Bool(*b),
            Value::Number(n) => {
                if let Some(i) = n.as_i64() {
                    SsoSubjectValue::Int(i)
                } else if let Some(f) = n.as_f64() {
                    SsoSubjectValue::Float(f)
                } else {
                    return None;
                }
            }
            _ => return None,
        };
        Self::new(iss, value).ok()
    }

    /// The scalar-type discriminator embedded in the encoded key.
    /// R18-F1: safe to splice in unescaped -- it always comes from
    /// this fixed, code-controlled set, never from IdP-supplied data.
    pub fn type_tag(&self) -> &'static str {
        match self.sub {
            SsoSubjectValue::Str(_) => "str",
            SsoSubjectValue::Int(_) => "int",
            SsoSubjectValue::Float(_) => "float",
            SsoSubjectValue::Bool(_) => "bool",
        }
    }

    /// The `sub`'s content, rendered exactly as Python's `str()`
    /// would -- the piece `encode()`/`legacy_lookup_key()`/
    /// `is_ambiguous()` all splice in.
    fn value_str(&self) -> String {
        match &self.sub {
            SsoSubjectValue::Str(s) => s.clone(),
            SsoSubjectValue::Int(i) => i.to_string(),
            SsoSubjectValue::Bool(true) => "True".to_string(),
            SsoSubjectValue::Bool(false) => "False".to_string(),
            SsoSubjectValue::Float(f) => python_float_str(*f),
        }
    }

    /// The `users.sso_subject` value for this subject. R18-F1: the
    /// type tag sits between issuer and value because a bare
    /// interpolation is NOT type-discriminating (`str(True) ==
    /// "True"`, `str(1) == "1"`, `str(1.0) == "1.0"`), so
    /// `sub=true`/`sub="True"` would otherwise collapse onto one key.
    pub fn encode(&self) -> String {
        format!(
            "{OIDC_SUBJECT_PREFIX}{}:{}:{}",
            self.iss,
            self.type_tag(),
            self.value_str()
        )
    }

    /// Parse a stored key back into a subject, or `None` if it isn't
    /// one. Guaranteed total and non-inventing: for ANY input, the
    /// result is either `None` or a subject whose `encode()` is
    /// byte-identical to the input -- a stored row can never be
    /// attributed to a subject that would have been persisted under a
    /// different key.
    pub fn decode(encoded: &str) -> Option<Self> {
        let body = encoded.strip_prefix(OIDC_SUBJECT_PREFIX)?;

        let mut candidates: Vec<(usize, &'static str)> = Vec::new();
        for tag in SCALAR_TAGS {
            let marker = format!(":{tag}:");
            let mut start = 0usize;
            while let Some(found) = body.get(start..).and_then(|s| s.find(marker.as_str())) {
                let idx = start + found;
                candidates.push((idx, tag));
                start = idx + 1;
            }
        }
        candidates.sort();

        for (index, tag) in candidates {
            let iss = &body[..index];
            if iss.is_empty() {
                continue;
            }
            let marker_len = tag.len() + 2; // the two colons around the tag
            if index + marker_len > body.len() {
                continue;
            }
            let raw_value = &body[index + marker_len..];
            let Some(value) = parse_tagged_scalar(tag, raw_value) else {
                continue;
            };
            let Ok(subject) = Self::new(iss.to_string(), value) else {
                continue;
            };
            if subject.encode() == encoded {
                return Some(subject);
            }
        }
        None
    }

    /// True iff this sub's UNTAGGED content could have been produced
    /// by a scalar type other than its own.
    ///
    /// R20-F1: R19-F1's legacy fallback matches on the pre-R18-F1
    /// untagged key (`oidc:<iss>:<sub>`), which by construction can't
    /// record which scalar type produced it -- reopening a lookup on
    /// the untagged shape reopens the same collision R16-F1/R17-F1/
    /// R18-F1 fixed for the tagged key. Both directions fall out of
    /// one question: could some OTHER accepted scalar type have
    /// produced this exact content?
    pub fn is_ambiguous(&self) -> bool {
        let content = self.value_str();
        SCALAR_TAGS
            .iter()
            .filter(|&&tag| tag != self.type_tag())
            .any(|&tag| parse_tagged_scalar(tag, &content).is_some())
    }

    /// The PRE-R18-F1 (untagged) key, or `None` if it must be
    /// withheld (see `is_ambiguous`). Deliberately reproduces the
    /// OLD, non-type-discriminating shape verbatim -- it exists
    /// solely to LOOK UP a pre-existing row, never to write one.
    pub fn legacy_lookup_key(&self) -> Option<String> {
        if self.is_ambiguous() {
            return None;
        }
        Some(format!(
            "{OIDC_SUBJECT_PREFIX}{}:{}",
            self.iss,
            self.value_str()
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const ISS: &str = "https://idp.example.test";

    // The R18-F1 collision set: distinct claim TYPES whose `str()`
    // forms are identical. A bare f-string collapsed each pair onto
    // one key.
    fn collision_set() -> Vec<SsoSubjectValue> {
        vec![
            SsoSubjectValue::Bool(true),
            SsoSubjectValue::Str("True".into()),
            SsoSubjectValue::Bool(false),
            SsoSubjectValue::Str("False".into()),
            SsoSubjectValue::Int(1),
            SsoSubjectValue::Str("1".into()),
            SsoSubjectValue::Int(0),
            SsoSubjectValue::Str("0".into()),
            SsoSubjectValue::Int(-42),
            SsoSubjectValue::Str("-42".into()),
            SsoSubjectValue::Float(1.0),
            SsoSubjectValue::Str("1.0".into()),
            SsoSubjectValue::Float(0.5),
            SsoSubjectValue::Str("0.5".into()),
        ]
    }

    #[test]
    fn encode_matches_the_persisted_wire_format_byte_for_byte() {
        // GOLDEN: these literals are the pre-refactor Python
        // `_oidc_subject` output, copied verbatim from
        // `tests/router/test_sso_subject_value_type.py`. If this test
        // ever needs updating, that is a schema migration, not a
        // refactor (R19-F1 is the bug you get for changing the key
        // format without one).
        assert_eq!(
            SsoSubject::new(ISS, SsoSubjectValue::Str("abc-123".into()))
                .unwrap()
                .encode(),
            "oidc:https://idp.example.test:str:abc-123"
        );
        assert_eq!(
            SsoSubject::new(ISS, SsoSubjectValue::Int(1))
                .unwrap()
                .encode(),
            "oidc:https://idp.example.test:int:1"
        );
        assert_eq!(
            SsoSubject::new(ISS, SsoSubjectValue::Float(1.0))
                .unwrap()
                .encode(),
            "oidc:https://idp.example.test:float:1.0"
        );
        assert_eq!(
            SsoSubject::new(ISS, SsoSubjectValue::Bool(true))
                .unwrap()
                .encode(),
            "oidc:https://idp.example.test:bool:True"
        );
        assert_eq!(
            SsoSubject::new(ISS, SsoSubjectValue::Bool(false))
                .unwrap()
                .encode(),
            "oidc:https://idp.example.test:bool:False"
        );
        assert_eq!(
            SsoSubject::new(ISS, SsoSubjectValue::Int(-42))
                .unwrap()
                .encode(),
            "oidc:https://idp.example.test:int:-42"
        );
    }

    #[test]
    fn type_tag_is_code_controlled_never_idp_data() {
        let tags = ["str", "int", "float", "bool"];
        for sub in collision_set() {
            let subject = SsoSubject::new(ISS, sub).unwrap();
            assert!(tags.contains(&subject.type_tag()));
        }
    }

    #[test]
    fn from_claims_degrades_unusable_claims_to_none() {
        assert!(SsoSubject::from_claims(None, Some(&json!("abc"))).is_none());
        assert!(SsoSubject::from_claims(Some(""), Some(&json!("abc"))).is_none());
        assert!(SsoSubject::from_claims(Some(ISS), None).is_none());
        // R18-F1: an empty `sub` carries no identity -- same as absent.
        assert!(SsoSubject::from_claims(Some(ISS), Some(&json!(""))).is_none());
        // Non-scalar shapes (a misserialised multi-valued attribute)
        // are not a sane identity key.
        assert!(SsoSubject::from_claims(Some(ISS), Some(&json!({"a": 1}))).is_none());
        assert!(SsoSubject::from_claims(Some(ISS), Some(&json!(["a"]))).is_none());
    }

    #[test]
    fn from_claims_accepts_every_json_scalar() {
        // R17-F1: `sub` is deliberately wider than `str` -- degrading
        // a numeric IdP subject id to None re-mints an orphan row
        // every login.
        for (claim, expect_type) in [
            (json!(true), "bool"),
            (json!("True"), "str"),
            (json!(false), "bool"),
            (json!(1), "int"),
            (json!("1"), "str"),
            (json!(0), "int"),
            (json!(-42), "int"),
            (json!(1.0), "float"),
            (json!(0.5), "float"),
        ] {
            let built = SsoSubject::from_claims(Some(ISS), Some(&claim));
            assert!(built.is_some(), "expected {claim:?} to build");
            assert_eq!(built.unwrap().type_tag(), expect_type, "for {claim:?}");
        }
    }

    #[test]
    fn direct_construction_refuses_an_unusable_subject() {
        assert_eq!(
            SsoSubject::new("", SsoSubjectValue::Str("abc".into())),
            Err(SsoSubjectError::EmptyIssuer)
        );
        assert_eq!(
            SsoSubject::new(ISS, SsoSubjectValue::Str(String::new())),
            Err(SsoSubjectError::EmptySubject)
        );
    }

    #[test]
    fn equality_and_hash_are_type_exact() {
        // Python's own `==` collapses `True == 1 == 1.0`. The value
        // type must NOT -- type discrimination is the whole point of
        // R18-F1.
        use std::collections::HashSet;

        let bool_sub = SsoSubject::new(ISS, SsoSubjectValue::Bool(true)).unwrap();
        let int_sub = SsoSubject::new(ISS, SsoSubjectValue::Int(1)).unwrap();
        let float_sub = SsoSubject::new(ISS, SsoSubjectValue::Float(1.0)).unwrap();
        let str_sub = SsoSubject::new(ISS, SsoSubjectValue::Str("True".into())).unwrap();

        assert_ne!(bool_sub, int_sub);
        assert_ne!(int_sub, float_sub);
        assert_ne!(bool_sub, str_sub);

        let set: HashSet<_> = [
            bool_sub.clone(),
            int_sub.clone(),
            float_sub.clone(),
            str_sub.clone(),
        ]
        .into_iter()
        .collect();
        assert_eq!(set.len(), 4);

        assert_eq!(
            bool_sub,
            SsoSubject::new(ISS, SsoSubjectValue::Bool(true)).unwrap()
        );
        assert_ne!(
            SsoSubject::new(ISS, SsoSubjectValue::Str("x".into())).unwrap(),
            SsoSubject::new("https://other.test", SsoSubjectValue::Str("x".into())).unwrap()
        );
    }

    #[test]
    fn decode_encode_roundtrip_on_the_r18f1_collision_set() {
        for sub in collision_set() {
            let original = SsoSubject::new(ISS, sub).unwrap();
            assert_eq!(SsoSubject::decode(&original.encode()), Some(original));
        }
    }

    /// A small, deterministic (splitmix64-seeded) pseudo-random value
    /// generator -- no `rand`/`proptest` dependency needed for a
    /// module-local property check. Mirrors the KINDS of values
    /// Python's own `_fuzz_pairs` generates (strings, ints, floats,
    /// bools, collision-set values across a handful of issuers), not
    /// a byte-for-byte port of Python's own PRNG sequence -- this
    /// checks the Rust implementation's internal round-trip property,
    /// not cross-language wire-format agreement for arbitrary values
    /// (the golden test above covers that for the fixed literals).
    fn fuzz_pairs(seed: u64, count: usize) -> Vec<(String, SsoSubjectValue)> {
        let issuers = [
            "https://idp.example.test",
            "https://keycloak.corp.example/realms/agent-mcp",
            "https://accounts.google.com",
            "http-less-issuer",
            "https://idp.example.test:8443/oidc",
        ];
        let alphabet: Vec<char> = "abcXYZ019-_.@/+ :\u{e4}\u{e9}\\\"'".chars().collect();

        let mut state = seed;
        let mut next_u64 = move || {
            // splitmix64
            state = state.wrapping_add(0x9E3779B97F4A7C15);
            let mut z = state;
            z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
            z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
            z ^ (z >> 31)
        };

        (0..count)
            .map(|_| {
                let iss = issuers[(next_u64() as usize) % issuers.len()].to_string();
                let kind = next_u64() % 5;
                let sub = match kind {
                    0 => {
                        let len = 1 + (next_u64() as usize) % 24;
                        let s: String = (0..len)
                            .map(|_| alphabet[(next_u64() as usize) % alphabet.len()])
                            .collect();
                        SsoSubjectValue::Str(s)
                    }
                    1 => SsoSubjectValue::Int((next_u64() as i64) % (1 << 40)),
                    2 => {
                        let raw = (next_u64() as i64 as f64) / 1e12;
                        SsoSubjectValue::Float(raw.clamp(-1e6, 1e6))
                    }
                    3 => SsoSubjectValue::Bool(next_u64() % 2 == 0),
                    _ => {
                        let cs = collision_set();
                        cs.into_iter().nth((next_u64() as usize) % 14).unwrap()
                    }
                };
                (iss, sub)
            })
            .collect()
    }

    #[test]
    fn decode_encode_roundtrip_property_fuzzed() {
        for (iss, sub) in fuzz_pairs(20260906, 600) {
            // A `Str` value can never be empty by construction here
            // (the generator always emits len >= 1), so `new` cannot
            // fail on the acceptance rules.
            let original = SsoSubject::new(iss.clone(), sub).unwrap();
            let decoded = SsoSubject::decode(&original.encode());
            assert_eq!(decoded, Some(original.clone()), "{iss:?} {original:?}");
        }
    }

    #[test]
    fn encode_is_injective_over_the_collision_set() {
        // R18-F1 as a property: no two distinct typed subjects may
        // share a persisted key. Pre-fix, `true`/`"True"` collapsed
        // to one.
        use std::collections::HashSet;
        let keys: Vec<String> = collision_set()
            .into_iter()
            .map(|sub| SsoSubject::new(ISS, sub).unwrap().encode())
            .collect();
        let unique: HashSet<_> = keys.iter().collect();
        assert_eq!(unique.len(), keys.len());
    }

    #[test]
    fn decode_never_invents_a_subject_that_reencodes_differently() {
        // Totality guard: for ANY input string, `decode` either
        // refuses or returns a subject that re-encodes to the
        // identical bytes.
        let mut candidates = vec![
            "".to_string(),
            "oidc:".to_string(),
            "oidc::".to_string(),
            "oidc:iss:".to_string(),
            "oidc:iss:str:".to_string(),
            "oidc:iss:str:x".to_string(),
            "oidc:iss:int:007".to_string(),
            "oidc:iss:int:1".to_string(),
            "oidc:iss:float:1".to_string(),
            "oidc:iss:float:1.0".to_string(),
            "oidc:iss:bool:true".to_string(),
            "oidc:iss:bool:True".to_string(),
            "oidc:iss:dict:{}".to_string(),
            "proxy:someone".to_string(),
            "oidc:https://a:str:str:b".to_string(),
            "not-a-subject-at-all".to_string(),
        ];
        for (iss, sub) in fuzz_pairs(20260906, 120) {
            candidates.push(SsoSubject::new(iss, sub).unwrap().encode());
        }
        for raw in candidates {
            let decoded = SsoSubject::decode(&raw);
            match decoded {
                None => {}
                Some(subject) => assert_eq!(subject.encode(), raw, "for input {raw:?}"),
            }
        }
    }

    #[test]
    fn decode_rejects_a_non_oidc_namespace() {
        // The proxy-header key space (`proxy:`) is deliberately
        // disjoint and is not an OIDC subject.
        assert!(SsoSubject::decode("proxy:someone@corp").is_none());
    }

    #[test]
    fn r18f1_distinct_claim_types_never_share_a_key() {
        // R18-F1 (#708): `str(true) == "True"`, `str(1) == "1"` -- a
        // bare interpolation let a SECOND, genuinely distinct OIDC
        // claimant reconcile into the FIRST claimant's account.
        for (typed, as_str) in [
            (SsoSubjectValue::Bool(true), "True"),
            (SsoSubjectValue::Bool(false), "False"),
            (SsoSubjectValue::Int(1), "1"),
            (SsoSubjectValue::Float(1.0), "1.0"),
            (SsoSubjectValue::Int(-42), "-42"),
        ] {
            let typed_subject =
                SsoSubject::from_claims(Some(ISS), Some(&value_of(&typed))).unwrap();
            let str_subject = SsoSubject::from_claims(Some(ISS), Some(&json!(as_str))).unwrap();
            assert_ne!(typed_subject.encode(), str_subject.encode());
        }
    }

    fn value_of(v: &SsoSubjectValue) -> Value {
        match v {
            SsoSubjectValue::Str(s) => json!(s),
            SsoSubjectValue::Int(i) => json!(i),
            SsoSubjectValue::Float(f) => json!(f),
            SsoSubjectValue::Bool(b) => json!(b),
        }
    }

    #[test]
    fn r18f1_int_and_float_stay_distinct() {
        assert_ne!(
            SsoSubject::new(ISS, SsoSubjectValue::Int(1))
                .unwrap()
                .encode(),
            SsoSubject::new(ISS, SsoSubjectValue::Float(1.0))
                .unwrap()
                .encode()
        );
    }

    #[test]
    fn r19f1_legacy_lookup_key_reproduces_the_untagged_format() {
        // R19-F1 (#709): the fallback key must be the EXACT pre-R18-F1
        // shape -- it exists only to match what an old row already
        // stored.
        assert_eq!(
            SsoSubject::new(ISS, SsoSubjectValue::Str("abc-123".into()))
                .unwrap()
                .legacy_lookup_key(),
            Some("oidc:https://idp.example.test:abc-123".to_string())
        );
    }

    #[test]
    fn r20f1_every_non_str_sub_is_unconditionally_ambiguous() {
        // R20-F1 (#710), direction 1: an untagged legacy key can't
        // record the type it was minted from, and a hypothetical
        // `str` sub of the same content always stringifies to
        // itself -- so a non-`str` sub can NEVER safely claim a
        // legacy row.
        for sub in [
            SsoSubjectValue::Int(1),
            SsoSubjectValue::Float(1.0),
            SsoSubjectValue::Bool(true),
            SsoSubjectValue::Bool(false),
            SsoSubjectValue::Int(-42),
            SsoSubjectValue::Int(1i64 << 62),
        ] {
            let subject = SsoSubject::new(ISS, sub).unwrap();
            assert!(subject.is_ambiguous());
            assert_eq!(subject.legacy_lookup_key(), None);
        }
    }

    #[test]
    fn r20f1_str_sub_is_ambiguous_iff_numeric_or_bool_shaped() {
        // R20-F1, direction 2 (the mirror case the fix agent confirmed
        // genuinely matters): a `str` claimant must not hijack a
        // legacy row that could equally have been minted from a
        // same-content int/float/bool sub.
        for (sub, ambiguous) in [
            ("1", true),
            ("-42", true),
            ("0", true),
            ("1.5", true),
            ("1.0", true),
            ("True", true),
            ("False", true),
            ("alice-sub-1", false),
            ("abc-123", false),
            ("007", false),  // not a canonical int repr
            ("1.50", false), // not a canonical float repr
            ("true", false), // bool repr is capitalised
            ("   ", false),
        ] {
            let subject = SsoSubject::new(ISS, SsoSubjectValue::Str(sub.into())).unwrap();
            assert_eq!(subject.is_ambiguous(), ambiguous, "for {sub:?}");
            assert_eq!(
                subject.legacy_lookup_key().is_none(),
                ambiguous,
                "for {sub:?}"
            );
        }
    }

    #[test]
    fn r20f1_ambiguity_refusal_never_touches_the_current_key() {
        // Withholding the legacy fallback must not weaken the CURRENT
        // tagged key -- an ambiguous sub still reconciles normally
        // via encode().
        let subject = SsoSubject::new(ISS, SsoSubjectValue::Int(1)).unwrap();
        assert_eq!(subject.legacy_lookup_key(), None);
        assert_eq!(subject.encode(), "oidc:https://idp.example.test:int:1");
    }
}
