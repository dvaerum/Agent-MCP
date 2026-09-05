//! Runtime asset-prefix substitution (Phase 4). Port of
//! `agent_mcp/router/asset_prefix.py` (209 LOC, Phase E2 PR 15,
//! `conexus-router-headers-misc`).
//!
//! The dashboard build emits a literal sentinel string wherever
//! Next.js would normally bake in the build-time `assetPrefix`; the
//! router substitutes the configured deployment prefix into served
//! HTML/JS/CSS bodies on the fly, so one build artifact deploys at any
//! URL prefix without rebuilding. See the Python module's own doc for
//! the full sentinel-vs-baked-prefix rationale -- unchanged here.
//!
//! **Deliberate improvement over a literal port**: Python's on-disk
//! cache is a bare module-level global `dict`. This crate's own
//! convention (`RuntimeStore`, `WaiterRegistry`, `StreamCapRegistry`)
//! is an explicit struct owned by whatever holds process-wide state
//! (`SharedState` in `conexus-backend`) rather than a hidden static --
//! [`AssetPrefixCache`] follows that precedent; PR 23's app-wiring
//! owns the one instance for the process's lifetime, same as Python's
//! module-scope dict living for the process's lifetime.
#![allow(dead_code)]

use std::collections::HashMap;
use std::path::Path;
use std::sync::{LazyLock, Mutex};

use regex::bytes::Regex;

/// The literal sentinel the dashboard build emits at every spot
/// Next.js would normally bake in `assetPrefix`.
pub const SENTINEL: &str = "__AGENT_MCP_ASSET_PREFIX__";

/// Next.js's flight-streaming serializer can flush its output buffer
/// mid-string, splitting the sentinel across a
/// `self.__next_f.push([N, "..."])` boundary at a content-dependent
/// offset. This pattern matches the sentinel with that exact boundary
/// optionally interposed between any two of its characters, so both a
/// contiguous occurrence and a split one are replaced -- when nothing
/// splits the sentinel, every optional group matches zero characters
/// and the pattern degenerates to the sentinel's literal bytes (no
/// separate non-split code path to keep in sync). Confirmed against a
/// real captured split (`__AGENT_MCP_ASSET_PREFIX_` + boundary + `_`)
/// during this migration's own Firefox-MCP verification.
static SENTINEL_WITH_OPTIONAL_SPLIT: LazyLock<Regex> = LazyLock::new(|| {
    const BOUNDARY: &str = r#""\]\)</script><script>self\.__next_f\.push\(\[\d+,""#;
    let joiner = format!("(?:{BOUNDARY})?");
    let escaped: Vec<String> = SENTINEL
        .bytes()
        .map(|b| regex::escape(&(b as char).to_string()))
        .collect();
    Regex::new(&escaped.join(&joiner)).expect("sentinel-split pattern must compile")
});

fn contains_subslice(haystack: &[u8], needle: &[u8]) -> bool {
    if needle.is_empty() {
        return true;
    }
    if needle.len() > haystack.len() {
        return false;
    }
    haystack.windows(needle.len()).any(|w| w == needle)
}

/// Replace every occurrence of [`SENTINEL`] in `body` with `prefix`
/// (a split occurrence included, see [`SENTINEL_WITH_OPTIONAL_SPLIT`]).
/// Pure: bytes in, bytes out, no I/O. `prefix` is UTF-8-encoded for
/// the replacement; an empty `prefix` is legal (site-root-relative
/// URLs, e.g. a single-tenant deploy at the host root).
pub fn substitute_asset_prefix(body: &[u8], prefix: &str) -> Vec<u8> {
    // Fast path: neither a plain nor a split occurrence can be
    // present (a split occurrence always straddles a
    // `self.__next_f.push(...)` boundary, so its absence rules that
    // case out too) -- skips the regex on the common case.
    if !contains_subslice(body, SENTINEL.as_bytes()) && !contains_subslice(body, b"__next_f.push") {
        return body.to_vec();
    }
    SENTINEL_WITH_OPTIONAL_SPLIT
        .replace_all(body, prefix.as_bytes())
        .into_owned()
}

/// Content-Type prefixes that need substitution -- `text/plain`/
/// `text/x-component` cover Next.js's RSC flight payloads, which also
/// carry the sentinel as a plain string.
const SUBSTITUTABLE_CTYPE_PREFIXES: &[&str] = &[
    "text/html",
    "text/css",
    "application/javascript",
    "text/javascript",
    "text/plain",
    "text/x-component",
];

/// True iff a response with this `Content-Type` should have its body
/// passed through [`substitute_asset_prefix`]. Defensive: `None`/
/// empty/unrecognised returns `false` -- substitution is opt-in per
/// type, never opt-out.
pub fn content_type_needs_substitution(content_type: Option<&str>) -> bool {
    let Some(ct) = content_type else {
        return false;
    };
    if ct.is_empty() {
        return false;
    }
    let lower = ct.to_ascii_lowercase();
    SUBSTITUTABLE_CTYPE_PREFIXES
        .iter()
        .any(|p| lower.starts_with(p))
}

type CacheKey = (String, u128, String);

/// On-disk cache for dashboard files, keyed on `(path, mtime_ns,
/// prefix)` -- see this module's own doc for why this is an explicit
/// struct rather than Python's module-global `dict`.
#[derive(Default)]
pub struct AssetPrefixCache {
    entries: Mutex<HashMap<CacheKey, Vec<u8>>>,
}

impl AssetPrefixCache {
    pub fn new() -> Self {
        Self::default()
    }

    /// Read `path` from disk and return its bytes with the sentinel
    /// substituted to `prefix`, memoised on `(path, mtime_ns,
    /// prefix)`. The mtime arm makes an in-place redeploy safe (a
    /// changed file invalidates the entry on next read); the prefix
    /// arm makes a per-deploy prefix override safe.
    pub fn substitute_file_bytes(&self, path: &Path, prefix: &str) -> std::io::Result<Vec<u8>> {
        let metadata = std::fs::metadata(path)?;
        let mtime_ns = metadata
            .modified()?
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let key: CacheKey = (
            path.to_string_lossy().into_owned(),
            mtime_ns,
            prefix.to_string(),
        );

        if let Some(cached) = self.entries.lock().unwrap().get(&key) {
            return Ok(cached.clone());
        }
        let raw = std::fs::read(path)?;
        let out = substitute_asset_prefix(&raw, prefix);
        self.entries.lock().unwrap().insert(key, out.clone());
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn substitutes_a_plain_contiguous_occurrence() {
        let body = format!("<script>var p = \"{SENTINEL}\";</script>").into_bytes();
        let out = substitute_asset_prefix(&body, "/agent-mcp/__dashboard");
        let out_str = String::from_utf8(out).unwrap();
        assert!(out_str.contains("/agent-mcp/__dashboard"));
        assert!(!out_str.contains(SENTINEL));
    }

    #[test]
    fn substitutes_every_occurrence() {
        let body = format!("{SENTINEL} and again {SENTINEL}").into_bytes();
        let out = substitute_asset_prefix(&body, "/x");
        let out_str = String::from_utf8(out).unwrap();
        assert_eq!(out_str, "/x and again /x");
    }

    #[test]
    fn body_with_no_sentinel_is_returned_unchanged() {
        let body = b"just some ordinary html".to_vec();
        assert_eq!(substitute_asset_prefix(&body, "/x"), body);
    }

    #[test]
    fn substitutes_a_sentinel_split_across_a_real_flight_flush_boundary() {
        // The exact real-world split (Python's own
        // `test_substitute_handles_sentinel_split_across_flight_push_boundary`
        // fixture, byte-for-byte): the sentinel splits one character
        // before its end. The whole matched span -- both sentinel
        // halves AND the boundary text between them -- is replaced by
        // the prefix (confirmed as Python's actual, tested behavior,
        // not merely "the sentinel disappears somehow": the split
        // halves were never a real standalone token to begin with,
        // only the reassembled sentinel was).
        let split_at = SENTINEL.len() - 1;
        let (head, tail) = SENTINEL.split_at(split_at);
        let mut payload = b"0:HL[\"".to_vec();
        payload.extend_from_slice(head.as_bytes());
        payload.extend_from_slice(b"\"])</script><script>self.__next_f.push([1,\"");
        payload.extend_from_slice(tail.as_bytes());
        payload.extend_from_slice(b"/_next/static/css/c916fe8822084f8b.css\"])");

        let out = substitute_asset_prefix(&payload, "/agent-mcp/assets");
        assert!(!contains_subslice(&out, SENTINEL.as_bytes()));
        assert!(!contains_subslice(&out, head.as_bytes()));
        assert!(contains_subslice(
            &out,
            b"/agent-mcp/assets/_next/static/css/c916fe8822084f8b.css"
        ));
    }

    #[test]
    fn substitutes_a_sentinel_split_at_every_possible_offset() {
        // Port of Python's own sweep test: the flush point is
        // content-dependent and can land anywhere inside the
        // sentinel, not just the one offset observed live.
        for split_at in 1..SENTINEL.len() {
            let (head, tail) = SENTINEL.split_at(split_at);
            let mut payload = b"x=\"".to_vec();
            payload.extend_from_slice(head.as_bytes());
            payload.extend_from_slice(b"\"])</script><script>self.__next_f.push([2,\"");
            payload.extend_from_slice(tail.as_bytes());
            payload.extend_from_slice(b"/_next/y.js\"");

            let out = substitute_asset_prefix(&payload, "/p");
            assert!(
                !contains_subslice(&out, SENTINEL.as_bytes()),
                "split_at={split_at}"
            );
            assert!(
                contains_subslice(&out, b"/p/_next/y.js"),
                "split_at={split_at}"
            );
        }
    }

    #[test]
    fn empty_prefix_produces_site_root_relative_urls() {
        let body = format!("src=\"{SENTINEL}/app.js\"").into_bytes();
        let out = substitute_asset_prefix(&body, "");
        assert_eq!(String::from_utf8(out).unwrap(), "src=\"/app.js\"");
    }

    #[test]
    fn content_type_needs_substitution_matches_every_documented_prefix() {
        assert!(content_type_needs_substitution(Some(
            "text/html; charset=utf-8"
        )));
        assert!(content_type_needs_substitution(Some("text/css")));
        assert!(content_type_needs_substitution(Some(
            "application/javascript"
        )));
        assert!(content_type_needs_substitution(Some("text/javascript")));
        assert!(content_type_needs_substitution(Some("text/plain")));
        assert!(content_type_needs_substitution(Some("text/x-component")));
        assert!(content_type_needs_substitution(Some("TEXT/HTML")));
    }

    #[test]
    fn content_type_needs_substitution_rejects_everything_else() {
        assert!(!content_type_needs_substitution(None));
        assert!(!content_type_needs_substitution(Some("")));
        assert!(!content_type_needs_substitution(Some("application/json")));
        assert!(!content_type_needs_substitution(Some("image/png")));
    }

    #[test]
    fn substitute_file_bytes_reads_and_substitutes_a_real_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("index.html");
        std::fs::write(&path, format!("<html>{SENTINEL}</html>")).unwrap();

        let cache = AssetPrefixCache::new();
        let out = cache
            .substitute_file_bytes(&path, "/agent-mcp/__dashboard")
            .unwrap();
        let out_str = String::from_utf8(out).unwrap();
        assert_eq!(out_str, "<html>/agent-mcp/__dashboard</html>");
    }

    #[test]
    fn substitute_file_bytes_caches_and_invalidates_on_mtime_change() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("index.html");
        std::fs::write(&path, format!("{SENTINEL}-v1")).unwrap();

        let cache = AssetPrefixCache::new();
        let first = cache.substitute_file_bytes(&path, "/x").unwrap();
        assert_eq!(String::from_utf8(first).unwrap(), "/x-v1");

        // Real mtime resolution can be coarse; force a distinct mtime
        // explicitly (std's own `set_modified`, stable since 1.75)
        // rather than relying on wall-clock elapsed time.
        std::fs::write(&path, format!("{SENTINEL}-v2")).unwrap();
        let newer = std::time::SystemTime::now() + std::time::Duration::from_secs(5);
        std::fs::OpenOptions::new()
            .write(true)
            .open(&path)
            .unwrap()
            .set_modified(newer)
            .unwrap();

        let second = cache.substitute_file_bytes(&path, "/x").unwrap();
        assert_eq!(
            String::from_utf8(second).unwrap(),
            "/x-v2",
            "a changed mtime must invalidate the cached entry"
        );
    }

    #[test]
    fn substitute_file_bytes_keys_on_prefix_too() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("index.html");
        std::fs::write(&path, SENTINEL).unwrap();

        let cache = AssetPrefixCache::new();
        let a = cache.substitute_file_bytes(&path, "/a").unwrap();
        let b = cache.substitute_file_bytes(&path, "/b").unwrap();
        assert_eq!(String::from_utf8(a).unwrap(), "/a");
        assert_eq!(String::from_utf8(b).unwrap(), "/b");
    }
}
