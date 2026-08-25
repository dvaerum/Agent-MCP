//! The skinny frame renderer.
//!
//! A delivery frame from agent-mcp carries **ids/subjects/status only, never a
//! message body** (ADR-0021). The renderer turns one frame into a short block
//! of text that points the agent at its own MCP tools (`get_agent_messages`,
//! `view_tasks`) — it never inlines a body, so no secret from a message body
//! can reach the pane.
//!
//! Frame shape (from `delivery_scheduler.py::_render_frame`):
//! ```json
//! {"type":"delivery","reason":"unread_messages|unfinished_tasks|unassigned_tasks",
//!  "unread_count":N,"task_count":N,
//!  "unread_messages":[{"message_id","sender_id","subject"}...],
//!  "open_tasks":[{"task_id","title","status"}...],
//!  "unassigned_count":N?}
//! ```
//!
//! A fourth reason, `directive_due` (ADR-0026, from
//! `delivery_scheduler.py::_render_directive_frame`), carries a nested
//! `directive` event instead and is rendered separately — see
//! `render_skinny`'s early-return branch:
//! ```json
//! {"type":"delivery","reason":"directive_due",
//!  "directive":{"data":{"prompt":"..."}}}
//! ```

use serde::Deserialize;
use unicode_general_category::{get_general_category, GeneralCategory};

/// Cap on how many items we list before summarising the rest — keeps the pane
/// nudge short.
const MAX_ITEMS: usize = 10;

#[derive(Debug, Clone, Default, Deserialize)]
pub struct UnreadMessage {
    #[serde(default)]
    pub message_id: String,
    #[serde(default)]
    pub sender_id: String,
    #[serde(default)]
    pub subject: String,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct OpenTask {
    #[serde(default)]
    pub task_id: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub status: String,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct DirectiveData {
    #[serde(default)]
    pub prompt: String,
}

/// The `directive` event nested in a `directive_due` frame
/// (`scheduled_directive_repository.py::_directive_event`, ADR-0026).
#[derive(Debug, Clone, Default, Deserialize)]
pub struct DirectiveEvent {
    #[serde(default)]
    pub data: DirectiveData,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Frame {
    #[serde(default, rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub reason: String,
    #[serde(default)]
    pub unread_count: i64,
    #[serde(default)]
    pub task_count: i64,
    #[serde(default)]
    pub unread_messages: Vec<UnreadMessage>,
    #[serde(default)]
    pub open_tasks: Vec<OpenTask>,
    #[serde(default)]
    pub unassigned_count: Option<i64>,
    /// Present only when `reason == "directive_due"` (ADR-0026).
    #[serde(default)]
    pub directive: Option<DirectiveEvent>,
}

/// Parse one SSE `data:` payload into a [`Frame`].
pub fn parse_frame(data: &str) -> Result<Frame, serde_json::Error> {
    serde_json::from_str(data)
}

/// Strip terminal control bytes from a user-controlled string before it is
/// interpolated into the pane nudge (R3-F1).
///
/// `subject`/`title` (and, defensively, every other frame field rendered
/// below) come straight from agent-mcp's DB with no upstream sanitisation
/// beyond an outer `.strip()` — see `task_tools.py`'s `task_title` and
/// `agent_communication_tools.py`'s message `subject`, which its own comment
/// notes is "persisted verbatim". `inject.rs` then POSTs the rendered nudge
/// verbatim to AoE's `/api/sessions/{id}/send`, which forwards it into a real
/// pty. Confirmed live: an ESC/BEL/CSI/OSC-bearing subject reached the pane
/// unmodified and executed as real terminal control codes.
///
/// This is a `char`-level allowlist (over `s.chars()`, i.e. Unicode scalar
/// values — `&str` is guaranteed valid UTF-8 by Rust's type system, so
/// invalid/partial UTF-8 can never reach this function in the first place)
/// rather than an ANSI/CSI/OSC grammar matcher, on purpose: a sequence
/// matcher only catches the escape variants it was written to recognise,
/// while dropping every C0 control char (`< 0x20`), DEL (`0x7f`), and the C1
/// control range (`0x80..=0x9f`, the 8-bit equivalents of CSI/OSC that don't
/// even need an ESC prefix) removes ESC itself — so no CSI/OSC introducer,
/// 7-bit or 8-bit, can ever survive, without having to enumerate
/// escape-sequence grammars that will always miss variants.
///
/// The same category-not-enumeration philosophy extends to Unicode
/// bidi/format characters (R4-F4): every codepoint in the Unicode `Cf`
/// (Format) general category — the explicit bidi-control block
/// U+202A–U+202E (LRE/RLE/PDF/LRO/RLO) and U+2066–U+2069 (LRI/RLI/FSI/PDI),
/// the zero-width block U+200B–U+200F (ZWSP/ZWNJ/ZWJ/LRM/RLM), and U+FEFF
/// (BOM) — is stripped too. Left unstripped, U+202E (RLO) can visually
/// reverse trailing pane text (the classic filename/extension-spoofing
/// trick, e.g. making `evil.exe` display reversed) and ZWSP/ZWJ can smuggle
/// invisible payloads into the pane; both are well above the 0x9f ceiling
/// the C0/C1 check covers, so a category-aware pass is needed rather than
/// widening the range table by hand.
///
/// Some demonstrated-exploitable codepoints fall outside `Cf`, all
/// classified `Mn` (Mark, nonspacing) by Unicode despite functioning purely
/// as invisible formatting controls — the general-category taxonomy is a
/// legacy-rendering artifact here, not a signal that it's meaningful
/// surviving content:
///
/// - U+034F COMBINING GRAPHEME JOINER (CGJ) carries no visible glyph of its
///   own; its only effect is to block canonical reordering/normalisation of
///   neighbouring combining marks.
/// - U+FE00–U+FE0F (Variation Selectors 1–16) and U+E0100–U+E01EF
///   (Variation Selectors 17–256, supplement plane) also carry no glyph of
///   their own: each only ever modifies/annotates the glyph of the
///   preceding character, or renders as nothing at all when unpaired. That
///   is up to 256 distinct codepoints across both blocks — a full smuggled
///   byte per character, a documented real-world "invisible Unicode"
///   data-hiding technique (R5-F9).
///
/// These are stripped explicitly rather than by widening the check to all
/// of `Mn`/`Mc`/`Me`, which would also catch the combining diacritics that
/// legitimate non-Latin scripts (Vietnamese, Devanagari, Arabic vowel
/// marks, …) render with — over-stripping those would break real content
/// the C0/C1/Cf checks are not meant to touch.
///
/// Each rendered line is meant to stay on one physical line, so embedded
/// newlines/tabs are folded to a single space too — otherwise a subject
/// could inject fake extra nudge lines even without any escape sequence.
/// Runs of stripped chars collapse to one space so the surrounding legible
/// text stays readable.
fn sanitize_for_pane(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut pending_space = false;
    for ch in s.chars() {
        let cp = ch as u32;
        let is_control = cp < 0x20
            || ch == '\u{7f}'
            || (0x80..=0x9f).contains(&cp)
            || get_general_category(ch) == GeneralCategory::Format
            || ch == '\u{034f}' // CGJ — see doc comment above.
            || (0xfe00..=0xfe0f).contains(&cp) // Variation Selectors 1-16
            || (0xe0100..=0xe01ef).contains(&cp); // Variation Selectors 17-256
        if is_control {
            if !out.is_empty() {
                pending_space = true;
            }
            continue;
        }
        if pending_space {
            out.push(' ');
            pending_space = false;
        }
        out.push(ch);
    }
    out
}

/// Render a frame to a skinny pane nudge. Pure: ids/subjects/status only —
/// EXCEPT `directive_due` (ADR-0026), which renders its prompt inline: a
/// scheduled directive's prompt is first-party content the agent/operator
/// itself authored, not a third party's message body, so ADR-0021's
/// "never inline a body" rule doesn't apply the same way here. Handled as
/// its own early return: the shape (no unread/open-task sections, no
/// "marks nothing read or done" footer — a directive fire is a one-shot
/// event, not a re-checkable backlog condition) diverges enough from the
/// other three reasons that folding it into their shared accumulation
/// logic below would be more confusing than a second, short branch.
pub fn render_skinny(f: &Frame) -> String {
    if f.reason == "directive_due" {
        let prompt = f
            .directive
            .as_ref()
            .map(|d| sanitize_for_pane(&d.data.prompt))
            .unwrap_or_default();
        return format!("[agent-mcp delivery] Scheduled directive due: {prompt}");
    }

    let mut lines: Vec<String> = Vec::new();

    let headline = match f.reason.as_str() {
        "unread_messages" => format!("{} unread message(s) waiting.", f.unread_count),
        "unfinished_tasks" => format!("{} open task(s) assigned to you.", f.task_count),
        "unassigned_tasks" => format!(
            "{} unassigned open task(s) in the pool.",
            f.unassigned_count.unwrap_or(0)
        ),
        _ => format!(
            "{} unread message(s), {} open task(s).",
            f.unread_count, f.task_count
        ),
    };
    lines.push(format!("[agent-mcp delivery] {headline}"));

    if !f.unread_messages.is_empty() {
        lines.push("Unread messages (call get_agent_messages to read the body):".to_string());
        for m in f.unread_messages.iter().take(MAX_ITEMS) {
            lines.push(format!(
                "  - #{} from {}: {}",
                sanitize_for_pane(&m.message_id),
                sanitize_for_pane(&m.sender_id),
                sanitize_for_pane(&m.subject)
            ));
        }
        if f.unread_messages.len() > MAX_ITEMS {
            lines.push(format!(
                "  … and {} more",
                f.unread_messages.len() - MAX_ITEMS
            ));
        }
    }

    if !f.open_tasks.is_empty() {
        lines.push("Open tasks (call view_tasks for details):".to_string());
        for t in f.open_tasks.iter().take(MAX_ITEMS) {
            lines.push(format!(
                "  - {} [{}]: {}",
                sanitize_for_pane(&t.task_id),
                sanitize_for_pane(&t.status),
                sanitize_for_pane(&t.title)
            ));
        }
        if f.open_tasks.len() > MAX_ITEMS {
            lines.push(format!("  … and {} more", f.open_tasks.len() - MAX_ITEMS));
        }
    }

    lines.push(
        "(Fallback nudge — act via your agent-mcp tools; this marks nothing read or done.)"
            .to_string(),
    );
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame_json(s: &str) -> Frame {
        parse_frame(s).unwrap()
    }

    #[test]
    fn renders_unread_messages_with_ids_and_subjects() {
        let f = frame_json(
            r#"{"type":"delivery","reason":"unread_messages","unread_count":2,"task_count":0,
                "unread_messages":[
                  {"message_id":"m1","sender_id":"alice","subject":"deploy done"},
                  {"message_id":"m2","sender_id":"bob","subject":"review please"}],
                "open_tasks":[]}"#,
        );
        let out = render_skinny(&f);
        assert!(out.contains("2 unread message(s)"));
        assert!(out.contains("#m1 from alice: deploy done"));
        assert!(out.contains("#m2 from bob: review please"));
        assert!(out.contains("get_agent_messages"));
    }

    #[test]
    fn renders_open_tasks_with_status() {
        let f = frame_json(
            r#"{"type":"delivery","reason":"unfinished_tasks","unread_count":0,"task_count":1,
                "unread_messages":[],
                "open_tasks":[{"task_id":"t9","title":"ship it","status":"in_progress"}]}"#,
        );
        let out = render_skinny(&f);
        assert!(out.contains("1 open task(s)"));
        assert!(out.contains("t9 [in_progress]: ship it"));
        assert!(out.contains("view_tasks"));
    }

    #[test]
    fn unassigned_uses_its_count() {
        let f = frame_json(
            r#"{"type":"delivery","reason":"unassigned_tasks","unread_count":0,"task_count":0,
                "unread_messages":[],"open_tasks":[],"unassigned_count":4}"#,
        );
        let out = render_skinny(&f);
        assert!(out.contains("4 unassigned open task(s)"));
    }

    #[test]
    fn renders_directive_due_with_its_actual_prompt_text() {
        // ADR-0026: unlike the other three reasons (which stay skinny —
        // ids/subjects only, never a body), a directive's prompt is
        // first-party content the agent/operator itself authored, not a
        // third party's message body — so it's rendered inline, not
        // pointed-at via a tool call.
        let f = frame_json(
            r#"{"type":"delivery","reason":"directive_due","unread_count":0,"task_count":0,
                "unread_messages":[],"open_tasks":[],
                "directive":{"ref_id":"sd_abc123","timestamp":"2026-08-25T00:00:00",
                  "priority":"urgent","data":{"prompt":"check in with all workers",
                  "source":"schedule","schedule_id":"sd_abc123"}}}"#,
        );
        let out = render_skinny(&f);
        assert!(
            out.contains("check in with all workers"),
            "directive prompt text missing from rendered nudge; got: {out:?}"
        );
        // Must NOT fall through to the generic message/task wildcard text —
        // that's the actual bug this test reproduces (silently renders
        // "0 unread message(s), 0 open task(s)" and drops the prompt).
        assert!(!out.contains("0 unread message(s)"));
    }

    #[test]
    fn strips_control_bytes_and_ansi_escapes_from_subject_and_title() {
        // Shape of the confirmed R3-F1 exploit: a subject/title carrying
        // ESC-prefixed CSI (screen-clear + color-change) and OSC
        // (terminal-title-set, BEL-terminated) sequences, plus a bare BEL and
        // DEL. These must never reach the rendered nudge unmodified — they'd
        // execute as real terminal control codes when the string is POSTed to
        // AoE's `/api/sessions/{id}/send` and echoed into a live pty.
        let evil_subject = "hi\x1b[2J\x1b[31mowned\x07\x1b]0;pwned\x07bye\x7f";
        let evil_title = "task\x1b[2J\x1b[0mtitle\x07end";
        // Round-trip through the real JSON wire shape (as `parse_frame`
        // deserialises it from delivery_scheduler.py) via serde_json's own
        // encoder, rather than hand-writing JSON text — raw control bytes are
        // not legal unescaped inside a JSON string literal.
        let wire = serde_json::json!({
            "type": "delivery",
            "reason": "unread_messages",
            "unread_count": 1,
            "task_count": 1,
            "unread_messages": [
                {"message_id": "m1", "sender_id": "alice", "subject": evil_subject}
            ],
            "open_tasks": [
                {"task_id": "t1", "status": "open", "title": evil_title}
            ],
        })
        .to_string();
        let f = frame_json(&wire);
        let out = render_skinny(&f);

        // No raw control bytes anywhere in the rendered output.
        assert!(
            !out.bytes().any(|b| b < 0x20 && b != b'\n'),
            "rendered output still contains a raw C0 control byte: {out:?}"
        );
        assert!(
            !out.bytes().any(|b| b == 0x7f),
            "rendered output still contains a raw DEL byte: {out:?}"
        );
        // The escape/CSI/OSC introducer bytes must be gone, not just embedded.
        assert!(!out.contains('\x1b'), "rendered output still contains ESC");
        assert!(!out.contains('\x07'), "rendered output still contains BEL");
        // The surrounding legible text should have survived the strip.
        assert!(out.contains("hi"));
        assert!(out.contains("owned"));
        assert!(out.contains("bye"));
        assert!(out.contains("title"));
    }

    #[test]
    fn strips_bidi_override_and_zero_width_format_characters_from_subject_and_title() {
        // R4-F4: sanitize_for_pane's C0/C1/DEL allowlist (R3-F1) does not
        // touch Unicode Cf-category format characters, which live well above
        // the 0x9f ceiling. U+202E (RLO) can visually reverse trailing pane
        // text (the classic filename/extension-spoofing trick), and
        // ZWSP/ZWJ/CGJ can smuggle invisible payloads into the pane. Both
        // reach the exact same chokepoint (render_skinny -> inject.rs ->
        // AoE's /api/sessions/{id}/send -> a live pty) via subject/title.
        let evil_subject = "safe\u{202E}gnp.exe\u{200B}\u{200D}\u{034F}end";
        let wire = serde_json::json!({
            "type": "delivery",
            "reason": "unread_messages",
            "unread_count": 1,
            "task_count": 0,
            "unread_messages": [
                {"message_id": "m1", "sender_id": "alice", "subject": evil_subject}
            ],
            "open_tasks": [],
        })
        .to_string();
        let f = frame_json(&wire);
        let out = render_skinny(&f);

        for bad in [
            '\u{202E}', // RLO
            '\u{202A}', // LRE
            '\u{202B}', // RLE
            '\u{202C}', // PDF
            '\u{202D}', // LRO
            '\u{2066}', // LRI
            '\u{2067}', // RLI
            '\u{2068}', // FSI
            '\u{2069}', // PDI
            '\u{200B}', // ZWSP
            '\u{200C}', // ZWNJ
            '\u{200D}', // ZWJ
            '\u{200E}', // LRM
            '\u{200F}', // RLM
            '\u{FEFF}', // BOM
            '\u{034F}', // CGJ (combining grapheme joiner)
        ] {
            assert!(
                !out.contains(bad),
                "rendered output still contains bidi/format char {bad:?}: {out:?}"
            );
        }
        // The surrounding legible text should have survived the strip.
        assert!(out.contains("safe"));
        assert!(out.contains("gnp.exe"));
        assert!(out.contains("end"));
    }

    #[test]
    fn strips_variation_selectors_from_subject_and_title() {
        // R5-F9: Unicode Variation Selectors (U+FE00-U+FE0F, and the
        // supplement plane U+E0100-U+E01EF) are general category `Mn`
        // (Mark, nonspacing) — the exact same category as U+034F CGJ, which
        // R4-F4 already special-cases as an explicit exception because `Mn`
        // as a whole is not stripped (it would over-strip legitimate
        // combining diacritics). Like CGJ, variation selectors carry no
        // glyph of their own: they either modify the glyph of the preceding
        // character or render as nothing when unpaired. Up to 256 distinct
        // codepoints across both blocks means a full byte can be smuggled
        // invisibly per character, reaching the same render_skinny ->
        // inject.rs -> AoE /api/sessions/{id}/send -> live pty chokepoint.
        let evil_subject = "safe\u{FE0F}\u{FE01}mid\u{E0100}\u{E01EF}end";
        let wire = serde_json::json!({
            "type": "delivery",
            "reason": "unread_messages",
            "unread_count": 1,
            "task_count": 0,
            "unread_messages": [
                {"message_id": "m1", "sender_id": "alice", "subject": evil_subject}
            ],
            "open_tasks": [],
        })
        .to_string();
        let f = frame_json(&wire);
        let out = render_skinny(&f);

        for bad in [
            '\u{FE00}', // VARIATION SELECTOR-1 (start of BMP block)
            '\u{FE0F}', // VARIATION SELECTOR-16 (end of BMP block, e.g. emoji-style VS)
            '\u{FE01}',
            '\u{E0100}', // VARIATION SELECTOR-17 (start of supplement block)
            '\u{E01EF}', // VARIATION SELECTOR-256 (end of supplement block)
        ] {
            assert!(
                !out.contains(bad),
                "rendered output still contains variation selector {bad:?}: {out:?}"
            );
        }
        // The surrounding legible text should have survived the strip.
        assert!(out.contains("safe"));
        assert!(out.contains("mid"));
        assert!(out.contains("end"));
    }

    #[test]
    fn legitimate_non_ascii_text_renders_unchanged() {
        // Accented Latin and CJK characters are ordinary letters (category
        // Ll/Lo, not Cf/Mn) and must survive the filter untouched — the fix
        // for R4-F4 must not over-strip legitimate non-ASCII content.
        let f = frame_json(
            &serde_json::json!({
                "type": "delivery",
                "reason": "unread_messages",
                "unread_count": 1,
                "task_count": 1,
                "unread_messages": [
                    {"message_id": "m1", "sender_id": "alice", "subject": "café résumé déjà vu"}
                ],
                "open_tasks": [
                    {"task_id": "t1", "status": "open", "title": "修复登录页面的问题"}
                ],
            })
            .to_string(),
        );
        let out = render_skinny(&f);
        assert!(out.contains("café résumé déjà vu"));
        assert!(out.contains("修复登录页面的问题"));
    }

    #[test]
    fn plain_text_subject_and_title_render_unchanged() {
        let f = frame_json(
            r#"{"type":"delivery","reason":"unread_messages","unread_count":1,"task_count":1,
                "unread_messages":[{"message_id":"m1","sender_id":"alice","subject":"deploy done, please review"}],
                "open_tasks":[{"task_id":"t1","status":"open","title":"ship the release notes"}]}"#,
        );
        let out = render_skinny(&f);
        assert!(out.contains("#m1 from alice: deploy done, please review"));
        assert!(out.contains("t1 [open]: ship the release notes"));
    }

    #[test]
    fn long_lists_are_capped() {
        let msgs: Vec<String> = (0..15)
            .map(|i| format!(r#"{{"message_id":"m{i}","sender_id":"s","subject":"x"}}"#))
            .collect();
        let f = frame_json(&format!(
            r#"{{"type":"delivery","reason":"unread_messages","unread_count":15,"task_count":0,
                "unread_messages":[{}],"open_tasks":[]}}"#,
            msgs.join(",")
        ));
        let out = render_skinny(&f);
        assert!(out.contains("… and 5 more"));
    }
}
