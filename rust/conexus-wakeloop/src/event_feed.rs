//! The `wait_for_events`/`fetch_events_since` event-feed pipeline. Port
//! of the pure/stateless half of `agent_mcp/tools/agent_communication_
//! tools.py`'s helpers feeding `assemble_event_feed` (the DB-reading
//! collectors and the full pipeline assembly land in later PRs, per the
//! Phase D3 research report's suggested 3-PR sequence).
//!
//! An event is a plain JSON object (`serde_json::Value`), matching
//! Python's `Dict[str, Any]` shape exactly: `{"type", "ref_id",
//! "timestamp", "payload"}` (or `"data"` for a couple of legacy event
//! shapes `_event_priority_rank` also reads) -- there is no fixed struct
//! because event PAYLOAD shape genuinely varies by `type` (a message
//! event's payload looks nothing like a `stop_listening` event's), and
//! `conexus-tools`' own `Tool::call` boundary already speaks
//! `serde_json::Value` for the same reason (untyped tool arguments).
//!
//! ## `_dedup_events`/`_event_identity` deliberately NOT ported
//!
//! Python's `_dedup_events` exists only because two producers can
//! describe the SAME logical event: a DB re-query (`_collect_
//! unassigned_task_events_for`, timestamped by the row's real
//! `updated_at`) and a synthetic in-memory queue push
//! (`state.dispatch_synthetic_event`, timestamped by wall-clock
//! `now()`) for the identical underlying row. Without dedup, merging
//! both delivers the same task twice per envelope.
//!
//! `conexus-wakeloop::waiter_registry`'s `WakeSignal` is payload-less BY
//! DESIGN (see that module's own doc comment, which independently
//! verified this exact same invariant from `assemble_event_feed`'s own
//! docstring: the DB re-query alone is always sufficient for
//! correctness, so the synthetic push is a latency optimization, not a
//! correctness requirement). A Rust `assemble_event_feed` built on top
//! of `WaiterRegistry` therefore only ever wakes with a bare signal and
//! re-derives EVERY event stream from the DB on every wake -- there is
//! never a second, differently-timestamped copy of the same row to
//! collide with. `_dedup_events` would be dead code here: a `HashMap`
//! keyed by `(type, ref_id)` that could only ever see each key once.
//! This is a design decision made explicit here, not a silent gap --
//! revisit it ONLY if a future change reintroduces a payload-carrying
//! wake channel.

use conexus_core::ToolResult;
use serde_json::{json, Value};

/// Clamp a merged event batch to the message-truncation boundary
/// (BL-R21-1). When the message backlog was truncated (an upstream
/// collector hit its page cap), `msg_cap_ts` is the timestamp of the
/// last message actually returned; every merged event newer than that
/// is dropped so the batch -- and the cursor derived from it
/// (`max(timestamp)`) -- never advances past undelivered messages. The
/// dropped events are all re-derivable on the next poll (re-queried by
/// `updated_at > cursor`), so nothing is lost, it just drains in
/// timestamp order across more polls.
///
/// `msg_cap_ts: None` (no truncation) returns the batch unchanged.
pub fn cap_events_to_boundary(events: Vec<Value>, msg_cap_ts: Option<&str>) -> Vec<Value> {
    let Some(boundary) = msg_cap_ts else {
        return events;
    };
    events
        .into_iter()
        .filter(|e| event_timestamp(e) <= boundary)
        .collect()
}

fn event_timestamp(event: &Value) -> &str {
    event.get("timestamp").and_then(Value::as_str).unwrap_or("")
}

/// Priority rank table: lower sorts first. A stable secondary key on top
/// of timestamp so an `urgent` poke/directive sorts ahead of ordinary
/// same-priority events without disturbing their relative timestamp
/// order.
fn priority_rank(priority: Option<&str>) -> u8 {
    match priority.unwrap_or("normal") {
        "urgent" => 0,
        "high" => 1,
        "low" => 3,
        // "normal" and any unrecognized value both default to normal --
        // matches Python's `_PRIORITY_RANK.get(prio or "normal", ...)`,
        // which falls back to the same rank for both an absent key and
        // an unknown string.
        _ => 2,
    }
}

/// Read an event's priority: top-level `priority` (directive events)
/// first, then `data.priority` (a couple of legacy message-event
/// shapes), defaulting to `"normal"`.
fn event_priority(event: &Value) -> Option<&str> {
    event.get("priority").and_then(Value::as_str).or_else(|| {
        event
            .get("data")
            .and_then(|d| d.get("priority"))
            .and_then(Value::as_str)
    })
}

/// In-place stable sort: priority ASC-rank (urgent first), then
/// timestamp ASC. Rust's `slice::sort_by_key` is stable, matching
/// Python's `list.sort` -- same-priority events keep their merge order.
pub fn sort_events_priority_then_time(events: &mut [Value]) {
    events.sort_by(|a, b| {
        let rank_a = priority_rank(event_priority(a));
        let rank_b = priority_rank(event_priority(b));
        rank_a
            .cmp(&rank_b)
            .then_with(|| event_timestamp(a).cmp(event_timestamp(b)))
    });
}

/// Build the canonical `stop_listening` event -- tells the agent to
/// exit its wake loop and wait for human input (an operator toggle, an
/// idle-stop window, or a reap). `now` is an explicit ISO-8601
/// timestamp, matching this crate's established "explicit input over
/// hidden state" convention (see `hold_ladder::advisory_event`).
pub fn stop_listening_event(reason: &str, now: &str) -> Value {
    json!({
        "type": "stop_listening",
        "ref_id": null,
        "timestamp": now,
        "payload": {"reason": reason},
    })
}

/// Newest-wins: the message returned to the OLDER `wait_for_events`
/// call when a NEWER one for the same agent supersedes it. Deliberately
/// NOT a `stop_listening` event -- the agent must not exit its loop
/// (its newer connection is carrying it); this only closes the stale
/// duplicate call.
const SUPERSEDED_MESSAGE: &str =
    "This wait_for_events connection was superseded by a newer one for the \
    same agent, so this (duplicate) call is being closed — you should have \
    exactly ONE event-loop connection. Do NOT open a second wait_for_events \
    while one is already parked, and do NOT background it: it is meant to \
    stay in the foreground as your idle wait for new work. Your newer \
    connection is still live and carrying the loop; do nothing here.";

/// Build the `connection_superseded` event returned to a waiter that a
/// newer connection replaced. Distinct from `stop_listening_event` so
/// the agent keeps its loop running -- on its newer connection.
pub fn superseded_event(now: &str) -> Value {
    json!({
        "type": "connection_superseded",
        "ref_id": null,
        "timestamp": now,
        "payload": {"reason": SUPERSEDED_MESSAGE},
    })
}

/// Wrap collected events into the standard `wait_for_events`/
/// `fetch_events_since` response envelope: `{"events", "next_cursor"}`
/// plus an optional `profile_review` section. `next_cursor` advances to
/// the max timestamp seen, or stays at `since` if the call returned
/// nothing (preserving the caller's progress through the timeline) --
/// ported bit-for-bit from `_envelope`. Returns `ToolResult::Ok` with
/// BOTH `data` (for REST/structured consumers) and `message` (the same
/// payload JSON-encoded, so the MCP wire renderer's historical
/// text-content shape stays byte-compatible with existing clients).
pub fn envelope(
    events: Vec<Value>,
    since: Option<&str>,
    profile_review: Option<Value>,
) -> ToolResult {
    let next_cursor = events
        .iter()
        .map(event_timestamp)
        .max()
        .filter(|ts| !ts.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| since.unwrap_or("").to_string());
    let mut payload = json!({"events": events, "next_cursor": next_cursor});
    if let Some(review) = profile_review {
        payload["profile_review"] = review;
    }
    ToolResult::Ok {
        message: Some(
            serde_json::to_string(&payload).expect("event-feed payload is always valid JSON"),
        ),
        data: Some(payload),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn event(event_type: &str, timestamp: &str) -> Value {
        json!({"type": event_type, "ref_id": null, "timestamp": timestamp, "payload": {}})
    }

    fn event_with_priority(priority: &str, timestamp: &str) -> Value {
        json!({"type": "directive", "ref_id": null, "timestamp": timestamp, "priority": priority, "payload": {}})
    }

    // -- cap_events_to_boundary --------------------------------------

    #[test]
    fn no_boundary_returns_events_unchanged() {
        let events = vec![event("message", "2026-01-01T00:00:00")];
        let capped = cap_events_to_boundary(events.clone(), None);
        assert_eq!(capped, events);
    }

    #[test]
    fn boundary_drops_events_strictly_after_it() {
        let events = vec![
            event("message", "2026-01-01T00:00:00"),
            event("message", "2026-01-01T00:00:01"),
        ];
        let capped = cap_events_to_boundary(events, Some("2026-01-01T00:00:00"));
        assert_eq!(capped.len(), 1);
        assert_eq!(capped[0]["timestamp"], "2026-01-01T00:00:00");
    }

    #[test]
    fn boundary_keeps_events_exactly_at_it() {
        // <= , not < -- an event timestamped exactly at the boundary is
        // the last delivered message itself, not one past it.
        let events = vec![event("message", "2026-01-01T00:00:00")];
        let capped = cap_events_to_boundary(events, Some("2026-01-01T00:00:00"));
        assert_eq!(capped.len(), 1);
    }

    // -- sort_events_priority_then_time -------------------------------

    #[test]
    fn urgent_sorts_ahead_of_normal_regardless_of_timestamp() {
        let mut events = vec![
            event("message", "2026-01-01T00:00:02"),
            event_with_priority("urgent", "2026-01-01T00:00:01"),
        ];
        sort_events_priority_then_time(&mut events);
        assert_eq!(events[0]["priority"], "urgent");
    }

    #[test]
    fn same_priority_sorts_by_timestamp_ascending() {
        let mut events = vec![
            event_with_priority("high", "2026-01-01T00:00:02"),
            event_with_priority("high", "2026-01-01T00:00:01"),
        ];
        sort_events_priority_then_time(&mut events);
        assert_eq!(events[0]["timestamp"], "2026-01-01T00:00:01");
        assert_eq!(events[1]["timestamp"], "2026-01-01T00:00:02");
    }

    #[test]
    fn missing_priority_defaults_to_normal_rank() {
        // A plain message event (no `priority` key) must rank the same
        // as an explicit "normal" -- both sort between "high" and "low".
        let mut events = vec![
            event_with_priority("low", "2026-01-01T00:00:00"),
            event("message", "2026-01-01T00:00:00"), // no priority key
            event_with_priority("high", "2026-01-01T00:00:00"),
        ];
        sort_events_priority_then_time(&mut events);
        assert_eq!(events[0]["priority"], "high");
        assert_eq!(events[1]["type"], "message");
        assert_eq!(events[2]["priority"], "low");
    }

    #[test]
    fn unknown_priority_string_defaults_to_normal_rank() {
        let mut events = vec![
            event_with_priority("urgent", "2026-01-01T00:00:00"),
            event_with_priority("nonsense", "2026-01-01T00:00:00"),
            event_with_priority("low", "2026-01-01T00:00:00"),
        ];
        sort_events_priority_then_time(&mut events);
        assert_eq!(events[0]["priority"], "urgent");
        assert_eq!(events[1]["priority"], "nonsense"); // ranked as normal
        assert_eq!(events[2]["priority"], "low");
    }

    #[test]
    fn priority_read_from_nested_data_field_when_top_level_absent() {
        let mut events = vec![
            json!({"type": "message", "timestamp": "t", "data": {"priority": "urgent"}}),
            event_with_priority("low", "t"),
        ];
        sort_events_priority_then_time(&mut events);
        assert_eq!(events[0]["data"]["priority"], "urgent");
    }

    // -- stop_listening_event / superseded_event ----------------------

    #[test]
    fn stop_listening_event_shape() {
        let ev = stop_listening_event("idle-stop window exceeded", "2026-01-01T00:00:00");
        assert_eq!(ev["type"], "stop_listening");
        assert_eq!(ev["ref_id"], Value::Null);
        assert_eq!(ev["timestamp"], "2026-01-01T00:00:00");
        assert_eq!(ev["payload"]["reason"], "idle-stop window exceeded");
    }

    #[test]
    fn superseded_event_shape_and_message() {
        let ev = superseded_event("2026-01-01T00:00:00");
        assert_eq!(ev["type"], "connection_superseded");
        assert_eq!(ev["timestamp"], "2026-01-01T00:00:00");
        let reason = ev["payload"]["reason"].as_str().unwrap();
        assert!(reason.contains("superseded"));
        assert!(reason.contains("exactly ONE event-loop connection"));
    }

    // -- envelope -------------------------------------------------------

    #[test]
    fn envelope_with_events_advances_cursor_to_max_timestamp() {
        let events = vec![
            event("message", "2026-01-01T00:00:01"),
            event("message", "2026-01-01T00:00:03"),
            event("message", "2026-01-01T00:00:02"),
        ];
        let result = envelope(events, Some("2025-01-01T00:00:00"), None);
        let ToolResult::Ok { data, message } = result else {
            panic!("expected Ok");
        };
        let data = data.unwrap();
        assert_eq!(data["next_cursor"], "2026-01-01T00:00:03");
        assert_eq!(data["events"].as_array().unwrap().len(), 3);
        // `message` carries the identical payload JSON-encoded.
        let reparsed: Value = serde_json::from_str(&message.unwrap()).unwrap();
        assert_eq!(reparsed, data);
    }

    #[test]
    fn empty_envelope_preserves_the_since_cursor() {
        let result = envelope(vec![], Some("2025-01-01T00:00:00"), None);
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok");
        };
        assert_eq!(data.unwrap()["next_cursor"], "2025-01-01T00:00:00");
    }

    #[test]
    fn empty_envelope_with_no_since_cursor_is_empty_string() {
        let result = envelope(vec![], None, None);
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok");
        };
        assert_eq!(data.unwrap()["next_cursor"], "");
    }

    #[test]
    fn profile_review_rides_the_envelope_when_present() {
        let result = envelope(vec![], None, Some(json!({"overdue": true})));
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok");
        };
        assert_eq!(data.unwrap()["profile_review"]["overdue"], true);
    }

    #[test]
    fn profile_review_absent_when_not_provided() {
        let result = envelope(vec![], None, None);
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok");
        };
        assert!(data.unwrap().get("profile_review").is_none());
    }
}
