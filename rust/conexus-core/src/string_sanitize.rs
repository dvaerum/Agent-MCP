//! Shared string-leaf sanitizer: control-byte + hidden-format-Unicode
//! stripping, plus a combining-mark run cap. Moved here (Phase E2 PR 3,
//! `conexus-router-identity-schema`) from `conexus-backend::
//! json_sanitize`, where it was originally a private, JSON-Value-only
//! helper (`sanitize_string_leaf`, ported from `agent_mcp/utils/
//! json_utils.py`'s `_strip_control_bytes` pipeline for PR #853's
//! `/api` body-decode chokepoint). `conexus-router::identity`'s
//! `create_user` needs the IDENTICAL stripper for `username`/`email`
//! (Python's real `create_user` reuses `_strip_control_bytes` too --
//! see that function's own docstring) -- since `conexus-backend` and
//! `conexus-router` are separate BINARY crates that can't depend on
//! each other, the shared logic belongs in `conexus-core` (the one
//! crate both already depend on, or can), not duplicated. This is a
//! pure move: byte-for-byte identical behavior, `conexus-backend`'s
//! own `strip_control_bytes` now calls through to this instead of
//! carrying a second copy.

use unicode_general_category::{get_general_category, GeneralCategory};

/// C0 controls (excluding tab/LF/CR, which are legitimate whitespace
/// in e.g. a multi-line task description) + DEL + the full C1 range.
/// Matches Python's `_CONTROL_BYTE_RE` exactly
/// (`[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]`).
fn is_stripped_control_byte(cp: u32) -> bool {
    matches!(cp, 0x00..=0x08 | 0x0B | 0x0C | 0x0E..=0x1F | 0x7F..=0x9F)
}

/// Variation Selectors 1-16 (BMP) and 17-256 (supplementary plane).
fn is_variation_selector(cp: u32) -> bool {
    (0xFE00..=0xFE0F).contains(&cp) || (0xE0100..=0xE01EF).contains(&cp)
}

/// Hidden/format Unicode + variation selectors, stripped by general
/// category (not a hand-enumerated range table) -- matches Python's
/// `_strip_hidden_unicode`'s `Cf` (Format: bidi overrides, ZWSP, BOM,
/// word joiner, ...) and `Zl`/`Zp` (line/paragraph separator) classes.
///
/// Python's fourth class, `Cs` (Surrogate -- a lone/unpaired surrogate
/// escape parses as a valid Python `str` character but is not valid
/// UTF-8, crashing SQLite's TEXT binding downstream), has NO Rust
/// equivalent to strip: Rust's `char` is defined as a valid Unicode
/// SCALAR VALUE, which categorically EXCLUDES the surrogate range
/// (`U+D800..=U+DFFF`) by construction -- there is no `char` value
/// `get_general_category` could ever classify as `Surrogate` in the
/// first place. The whole vulnerability class is therefore
/// structurally impossible here, a stronger guarantee than Python's
/// post-hoc strip achieves, not a gap in this port (verified directly
/// against a live-failing `serde_json` parse test when this was first
/// ported for PR #853, not merely assumed).
fn is_hidden_format_char(ch: char) -> bool {
    if is_variation_selector(ch as u32) {
        return true;
    }
    matches!(
        get_general_category(ch),
        GeneralCategory::Format
            | GeneralCategory::LineSeparator
            | GeneralCategory::ParagraphSeparator
    )
}

/// R14-F3: cap on consecutive combining marks (`Mn`/`Me`) per base
/// character -- limits, not strips, the category: these marks carry
/// real, load-bearing glyph content for many scripts (Vietnamese tone
/// marks, Arabic tashkeel, Devanagari vowel signs), so only
/// pathological run LENGTH ("zalgo" text) is the actual spoofing/
/// layout-DoS primitive, not the category itself. Matches Python's
/// `_MAX_COMBINING_MARKS_PER_BASE`.
const MAX_COMBINING_MARKS_PER_BASE: usize = 4;

fn is_combining_mark(ch: char) -> bool {
    matches!(
        get_general_category(ch),
        GeneralCategory::NonspacingMark | GeneralCategory::EnclosingMark
    )
}

/// Strip control bytes + hidden-format Unicode, then cap combining-
/// mark runs -- the exact order and composition of Python's
/// `_strip_control_bytes`'s per-leaf pipeline
/// (`_cap_combining_marks(_strip_hidden_unicode(_CONTROL_BYTE_RE.sub('', value)))`).
pub fn sanitize_string_leaf(value: &str) -> String {
    let stripped: String = value
        .chars()
        .filter(|&ch| !is_stripped_control_byte(ch as u32) && !is_hidden_format_char(ch))
        .collect();

    let mut out = String::with_capacity(stripped.len());
    let mut run_length = 0usize;
    for ch in stripped.chars() {
        if is_combining_mark(ch) {
            run_length += 1;
            if run_length > MAX_COMBINING_MARKS_PER_BASE {
                continue;
            }
        } else {
            run_length = 0;
        }
        out.push(ch);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strips_control_bytes_but_keeps_legitimate_whitespace() {
        assert_eq!(sanitize_string_leaf("a\x00b\tc\nd"), "ab\tc\nd");
    }

    #[test]
    fn strips_hidden_format_unicode() {
        // U+200B ZERO WIDTH SPACE, U+202E RIGHT-TO-LEFT OVERRIDE.
        assert_eq!(sanitize_string_leaf("ad\u{200B}min"), "admin");
        assert_eq!(sanitize_string_leaf("a\u{202E}dmin"), "admin");
    }

    #[test]
    fn caps_a_pathological_combining_mark_run() {
        let zalgo: String = "e"
            .chars()
            .chain(std::iter::repeat_n('\u{0301}', 20))
            .collect();
        let cleaned = sanitize_string_leaf(&zalgo);
        assert_eq!(cleaned.chars().count(), 1 + MAX_COMBINING_MARKS_PER_BASE);
    }

    #[test]
    fn passes_through_ordinary_text_unchanged() {
        assert_eq!(
            sanitize_string_leaf("alice@example.test"),
            "alice@example.test"
        );
    }
}
