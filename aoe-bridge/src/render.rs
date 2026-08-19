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

use serde::Deserialize;

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
/// This is a byte-level allowlist rather than an ANSI/CSI/OSC grammar
/// matcher on purpose: a sequence matcher only catches the escape variants
/// it was written to recognise, while dropping every C0 control byte
/// (`< 0x20`), DEL (`0x7f`), and the C1 control range (`0x80..=0x9f`, the
/// 8-bit equivalents of CSI/OSC that don't even need an ESC prefix) removes
/// ESC itself — so no CSI/OSC introducer, 7-bit or 8-bit, can ever survive,
/// without having to enumerate escape-sequence grammars that will always
/// miss variants. Each rendered line is meant to stay on one physical
/// line, so embedded newlines/tabs are folded to a single space too —
/// otherwise a subject could inject fake extra nudge lines even without any
/// escape sequence. Runs of stripped bytes collapse to one space so the
/// surrounding legible text stays readable.
fn sanitize_for_pane(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut pending_space = false;
    for ch in s.chars() {
        let cp = ch as u32;
        let is_control = cp < 0x20 || ch == '\u{7f}' || (0x80..=0x9f).contains(&cp);
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

/// Render a frame to a skinny pane nudge. Pure: ids/subjects/status only.
pub fn render_skinny(f: &Frame) -> String {
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
