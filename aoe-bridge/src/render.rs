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
            lines.push(format!("  - #{} from {}: {}", m.message_id, m.sender_id, m.subject));
        }
        if f.unread_messages.len() > MAX_ITEMS {
            lines.push(format!("  … and {} more", f.unread_messages.len() - MAX_ITEMS));
        }
    }

    if !f.open_tasks.is_empty() {
        lines.push("Open tasks (call view_tasks for details):".to_string());
        for t in f.open_tasks.iter().take(MAX_ITEMS) {
            lines.push(format!("  - {} [{}]: {}", t.task_id, t.status, t.title));
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
