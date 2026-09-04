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
use conexus_db::agent_repository::{AgentField, AgentRepository, FieldValue};
use conexus_db::{message_repository, project_settings_repository, task_repository};
use rusqlite::Connection;
use serde_json::{json, Value};

/// Per-poll cap on the message backlog the event feed drains at once.
/// Matches `MessageQueryFilters::limit`'s clamp ceiling. When more than
/// this many messages have accrued since the cursor, one poll returns a
/// contiguous OLDEST-first prefix and the cursor advances only to the
/// prefix boundary, so the next poll drains the remainder in order
/// (BL-R20-1).
pub const MESSAGE_EVENT_QUERY_CAP: i64 = 500;

const BROADCAST_MESSAGE_TYPES: [&str; 3] = ["broadcast", "announcement", "system_alert"];

/// Max seconds a skinny message event is HELD waiting for the async AI
/// subject backfill to title it (only when subject-gen is ON). Past
/// this, the event fires with the 50-char preview so a stalled/failed
/// backfill can never strand a message out of its recipient's event
/// stream.
const TITLE_HOLD_MAX_SECONDS: i64 = 120;

/// The 3-element terminal-task-status set from Python's
/// `features/task_queries.py::TERMINAL_TASK_STATUSES` -- the set
/// `_collect_unassigned_task_events_for` actually uses. NOT the same
/// as `conexus-wakeloop::idle_reminder`'s own 4-element set (which has
/// an extra single-L "canceled" spelling, for a different Python
/// feature) -- see `conexus-db::task_repository::
/// list_unassigned_active_updated_since`'s doc for why that distinction
/// is load-bearing.
pub const UNASSIGNED_TASK_TERMINAL_STATUSES: [&str; 3] = ["cancelled", "completed", "failed"];

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

/// `_collect_events_with_cap`'s result: the merged, timestamp-ASC
/// message/task event batch, plus the truncation boundary (`Some` only
/// when the message backlog filled [`MESSAGE_EVENT_QUERY_CAP`] or an
/// untitled root was held for the AI subject backfill).
pub struct CollectedEvents {
    pub events: Vec<Value>,
    pub msg_cap_ts: Option<String>,
}

/// True while an untitled root message is still inside the title-hold
/// window (its skinny event is held for the AI subject backfill). Any
/// parse failure returns `false` -- fire now rather than risk
/// stranding, matching Python's `_within_title_hold`.
fn within_title_hold(msg_ts: &str, now_iso: &str) -> bool {
    let (Ok(msg_dt), Ok(now_dt)) = (
        conexus_db::scheduled_directive_repository::parse_flexible(msg_ts),
        conexus_db::scheduled_directive_repository::parse_flexible(now_iso),
    ) else {
        return false;
    };
    let age = (now_dt - msg_dt).num_seconds();
    (0..TITLE_HOLD_MAX_SECONDS).contains(&age)
}

/// Collect new message + assigned-task events for `agent_id` strictly
/// after `since`, plus the message-truncation boundary. Port of
/// `_collect_events_with_cap`.
///
/// `get_env` resolves `AGENT_MCP_SUBJECT_MODEL` (the AI subject-gen
/// on/off flag) -- an explicit lookup, not a direct `std::env::var`
/// read, matching this workspace's established convention for sidestepping
/// `cargo test`'s parallel-thread env-var-race hazard (see
/// `conexus-tools::completion_client`'s `resolve` for the precedent).
///
/// BL-R21-1: the caller MUST cap its final (merged) cursor to
/// `msg_cap_ts` when it is `Some` -- this function's own internal
/// clamp only trims ITS OWN events (messages + assigned tasks); the
/// unbounded streams a caller merges in afterwards
/// (`unassigned_task_appeared`, `agent_profile_updated`) would
/// otherwise drag the merged cursor past the un-returned messages.
pub fn collect_events_with_cap(
    conn: &Connection,
    agent_id: &str,
    since: Option<&str>,
    now_iso: &str,
    get_env: impl Fn(&str) -> Option<String>,
) -> rusqlite::Result<CollectedEvents> {
    let since_iso = since.unwrap_or("0000-01-01T00:00:00");
    let mut events: Vec<Value> = Vec::new();

    // BL-R20-1: request the OLDEST messages since the cursor first
    // (ASC, capped) so a truncated batch is a contiguous prefix -- the
    // cursor can then only advance past messages actually delivered.
    let msg_repo = message_repository::MessageRepository::new();
    let msg_rows = msg_repo.query(
        conn,
        &message_repository::MessageQueryFilters {
            to: Some(agent_id),
            since: Some(since_iso),
            limit: MESSAGE_EVENT_QUERY_CAP,
            ..Default::default()
        },
        true,
    )?;
    let mut messages_truncated = (msg_rows.len() as i64) >= MESSAGE_EVENT_QUERY_CAP;
    let mut msg_cap_ts: Option<String> = msg_rows.last().map(|r| r.timestamp.clone());

    let gen_on = get_env("AGENT_MCP_SUBJECT_MODEL").is_some_and(|v| !v.trim().is_empty());
    let mut last_emitted_ts: Option<String> = None;

    for row in &msg_rows {
        // The repo's `since` filter is inclusive (`>=`); re-apply the
        // strict `>` filter here so a message exactly at `since_iso`
        // doesn't fire again on the next poll.
        let ts = row.timestamp.as_str();
        if ts <= since_iso {
            continue;
        }
        let is_reply = row.parent_message_id.is_some();
        let (display_subject, is_placeholder) = if is_reply {
            (None, false)
        } else {
            let (subject, placeholder) = message_repository::message_subject_view(
                row.subject.as_deref(),
                &row.message_content,
            );
            (subject, placeholder)
        };
        // Title gate (roots only): hold an untitled root while subject-gen
        // is on and the backfill window hasn't expired, reusing the
        // truncation boundary so the cursor can't advance past the held
        // row (re-queried next poll).
        if !is_reply && is_placeholder && gen_on && within_title_hold(ts, now_iso) {
            messages_truncated = true;
            msg_cap_ts = Some(
                last_emitted_ts
                    .clone()
                    .unwrap_or_else(|| since_iso.to_string()),
            );
            break;
        }
        let evt_type = if BROADCAST_MESSAGE_TYPES.contains(&row.message_type.as_str()) {
            "broadcast"
        } else {
            "message"
        };
        events.push(json!({
            "type": evt_type,
            "timestamp": ts,
            "data": {
                "message_id": row.message_id,
                "sender_id": row.sender_id,
                "subject": display_subject,
                "is_reply": is_reply,
                "priority": row.priority,
                "timestamp": ts,
            },
        }));
        last_emitted_ts = Some(ts.to_string());
    }

    for row in task_repository::list_assigned_updated_since(conn, agent_id, since_iso)? {
        // v1 heuristic: a row created since the cursor is a fresh
        // assignment; an older row touched since the cursor is a mutation.
        let evt_type = if row.created_at.as_str() > since_iso {
            "task_assigned"
        } else {
            "task_changed"
        };
        events.push(json!({
            "type": evt_type,
            "timestamp": row.updated_at,
            "data": {
                "task_id": row.task_id,
                "title": row.title,
                "status": row.status,
                "priority": row.priority,
                "updated_at": row.updated_at,
            },
        }));
    }

    // BL-R20-1: when the message batch was truncated, clamp the WHOLE
    // batch (messages + tasks) to the prefix boundary so the cursor
    // can't leap past undelivered messages via a newer task event.
    let truncation_boundary = if messages_truncated && msg_cap_ts.is_some() {
        let boundary = msg_cap_ts.clone().unwrap();
        events.retain(|e| event_timestamp(e) <= boundary.as_str());
        Some(boundary)
    } else {
        None
    };

    events.sort_by(|a, b| event_timestamp(a).cmp(event_timestamp(b)));
    Ok(CollectedEvents {
        events,
        msg_cap_ts: truncation_boundary,
    })
}

/// Find unassigned, non-terminal tasks that transitioned after `since`.
/// Port of `_collect_unassigned_task_events_for`. Returns nothing for
/// an unknown/tombstoned `agent_id` (the gate is kept only for that
/// case -- every unassigned task surfaces to every KNOWN agent).
pub fn collect_unassigned_task_events_for(
    conn: &Connection,
    agent_id: &str,
    since: Option<&str>,
) -> rusqlite::Result<Vec<Value>> {
    if AgentRepository::get_by_id(conn, agent_id)?.is_none() {
        return Ok(Vec::new());
    }
    let since_iso = since.unwrap_or("0000-01-01T00:00:00");
    let rows = task_repository::list_unassigned_active_updated_since(
        conn,
        since_iso,
        &UNASSIGNED_TASK_TERMINAL_STATUSES,
    )?;
    Ok(rows
        .into_iter()
        .map(|row| {
            json!({
                "type": "unassigned_task_appeared",
                "ref_id": row.task_id,
                "timestamp": row.updated_at,
                "payload": {
                    "task_id": row.task_id,
                    "title": row.title,
                    "priority": row.priority,
                },
            })
        })
        .collect())
}

/// Find peer profile changes newer than `since`. Port of
/// `_collect_agent_profile_events_for` -- a thin projection over
/// `AgentRepository::list_profile_changes_since`, which already
/// carries every SQL exclusion.
pub fn collect_agent_profile_events_for(
    conn: &Connection,
    agent_id: &str,
    since: Option<&str>,
) -> rusqlite::Result<Vec<Value>> {
    let since_iso = since.unwrap_or("0000-01-01T00:00:00");
    let rows = AgentRepository::list_profile_changes_since(conn, since_iso, agent_id)?;
    Ok(rows
        .into_iter()
        .map(|row| {
            json!({
                "type": "agent_profile_updated",
                "ref_id": row.agent_id,
                "timestamp": row.profile_updated_at,
                "data": {
                    "agent_id": row.agent_id,
                    "agent_role": row.agent_role,
                    "profile": row.profile,
                    "updated_by": row.profile_updated_by,
                },
            })
        })
        .collect())
}

/// `(enabled, reason_when_disabled)`. Port of
/// `_check_auto_event_loop_flags`: the wake loop is enabled iff BOTH
/// `config_auto_event_loop_global` is truthy (default true) AND the
/// per-agent `agents.auto_event_loop` is truthy (default true). Reads
/// fresh on every call (no caching) so a mid-flight toggle flip or
/// termination is observed on the next recheck. A real DB error (as
/// opposed to a clean "not found") degrades to `(true, None)` --
/// matches Python's defensive fallback, since a lookup failure must
/// never itself stop an otherwise-healthy agent's loop.
pub fn check_auto_event_loop_flags(conn: &Connection, agent_id: &str) -> (bool, Option<String>) {
    let global_on =
        project_settings_repository::get_bool(conn, "config_auto_event_loop_global", true);
    if !global_on {
        return (
            false,
            Some("config_auto_event_loop_global is OFF".to_string()),
        );
    }
    match AgentRepository::get_by_id(conn, agent_id) {
        Err(_) => (true, None),
        Ok(None) => (false, Some(format!("agent '{agent_id}' not found"))),
        Ok(Some(row)) if row.status == "terminated" => {
            (false, Some(format!("agent '{agent_id}' terminated")))
        }
        Ok(Some(row)) if !row.auto_event_loop => (
            false,
            Some(format!(
                "Monitoring paused by operator for agent '{agent_id}'. You have been \
                 disconnected for now; you may be told to resume later. Exit the event loop \
                 and wait for human input."
            )),
        ),
        Ok(Some(_)) => (true, None),
    }
}

/// Seconds until this agent's event-loop idle-stop fires, or `None`
/// when idle-stop is disabled (`config_event_idle_stop_seconds == 0`).
/// Port of `_idle_stop_seconds_remaining`.
///
/// On first use (marker NULL) SEEDS `last_activity_at` to `now` and
/// grants a full window -- a brand-new agent starts its idle clock
/// when it begins listening rather than counting as instantly idle.
/// The marker is reset to `now` on every real event (by the caller, via
/// `AgentRepository::update_field(..., LastActivityAt, ...)`), so this
/// measures time-since-last-real-event across reconnects. A return
/// `<= 0.0` means the window is already exceeded.
pub fn idle_stop_seconds_remaining(conn: &Connection, agent_id: &str, now: &str) -> Option<f64> {
    let window =
        project_settings_repository::get_int(conn, "config_event_idle_stop_seconds", 604_800);
    if window <= 0 {
        return None; // 0 = infinite / never stop
    }
    let last = AgentRepository::get_by_id(conn, agent_id)
        .ok()
        .flatten()
        .and_then(|a| a.last_activity_at);
    let seed_and_grant_full_window = || {
        let _ = AgentRepository::update_field(
            conn,
            agent_id,
            AgentField::LastActivityAt,
            FieldValue::Text(now.to_string()),
            now,
        );
        window as f64
    };
    let Some(last) = last else {
        return Some(seed_and_grant_full_window());
    };
    match (
        conexus_db::scheduled_directive_repository::parse_flexible(now),
        conexus_db::scheduled_directive_repository::parse_flexible(&last),
    ) {
        (Ok(now_dt), Ok(last_dt)) => {
            Some(window as f64 - (now_dt - last_dt).num_milliseconds() as f64 / 1000.0)
        }
        _ => Some(seed_and_grant_full_window()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::agent_repository::{AgentRepository, NewAgent};
    use conexus_db::message_repository::{self as msg_repo, NewMessage};
    use conexus_db::schema::init_schema;
    use conexus_db::task_repository::{self, NewTask};

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

    // -- DB-backed collectors ---------------------------------------------

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
    }

    fn seed_agent(conn: &Connection, agent_id: &str) {
        AgentRepository::create(
            conn,
            NewAgent {
                token: &format!("tok-{agent_id}"),
                agent_id,
                created_at: "2026-01-01T00:00:00Z",
                status: "active",
                current_task: None,
                working_directory: "/tmp",
                color: None,
                agent_role: "worker",
            },
        )
        .unwrap();
    }

    fn no_env(_: &str) -> Option<String> {
        None
    }

    fn send_message(conn: &Connection, id: &str, to: &str, ts: &str, message_type: &str) {
        msg_repo::send(
            conn,
            NewMessage {
                message_id: id,
                sender_id: "sender",
                recipient_id: to,
                message_content: "hello",
                message_type,
                priority: "normal",
                timestamp: ts,
                delivered: true,
                read: false,
                subject: Some("a real subject"),
                parent_message_id: None,
            },
        )
        .unwrap();
    }

    // -- collect_events_with_cap ------------------------------------------

    #[test]
    fn collect_events_with_cap_classifies_direct_message_vs_broadcast() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        send_message(&conn, "m1", "alice", "2026-01-01T00:00:01Z", "text");
        send_message(&conn, "m2", "alice", "2026-01-01T00:00:02Z", "broadcast");

        let result =
            collect_events_with_cap(&conn, "alice", None, "2026-01-01T00:01:00Z", no_env).unwrap();
        assert_eq!(result.events.len(), 2);
        assert_eq!(result.events[0]["type"], "message");
        assert_eq!(result.events[1]["type"], "broadcast");
        assert!(result.msg_cap_ts.is_none());
    }

    #[test]
    fn collect_events_with_cap_excludes_messages_at_or_before_since() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        send_message(&conn, "m1", "alice", "2026-01-01T00:00:01Z", "text");

        let result = collect_events_with_cap(
            &conn,
            "alice",
            Some("2026-01-01T00:00:01Z"),
            "2026-01-01T00:01:00Z",
            no_env,
        )
        .unwrap();
        assert!(result.events.is_empty());
    }

    #[test]
    fn collect_events_with_cap_classifies_task_assigned_vs_changed() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        task_repository::create(
            &conn,
            NewTask {
                task_id: Some("task_1"),
                title: "fresh",
                description: None,
                assigned_to: Some("alice"),
                created_by: "bob",
                status: "pending",
                priority: "medium",
                parent_task: None,
                child_tasks: None,
                depends_on_tasks: None,
                notes: None,
                now: "2026-01-01T00:00:05Z", // created AFTER since -> assigned
            },
        )
        .unwrap();
        task_repository::create(
            &conn,
            NewTask {
                task_id: Some("task_2"),
                title: "older",
                description: None,
                assigned_to: Some("alice"),
                created_by: "bob",
                status: "pending",
                priority: "medium",
                parent_task: Some("task_1"),
                child_tasks: None,
                depends_on_tasks: None,
                notes: None,
                now: "2025-01-01T00:00:00Z", // created BEFORE since -> changed
            },
        )
        .unwrap();
        conn.execute(
            "UPDATE tasks SET updated_at = '2026-01-01T00:00:06Z' WHERE task_id = 'task_2'",
            [],
        )
        .unwrap();

        let result = collect_events_with_cap(
            &conn,
            "alice",
            Some("2026-01-01T00:00:00Z"),
            "2026-01-01T00:01:00Z",
            no_env,
        )
        .unwrap();
        let types: Vec<_> = result
            .events
            .iter()
            .map(|e| e["type"].as_str().unwrap())
            .collect();
        assert!(types.contains(&"task_assigned"));
        assert!(types.contains(&"task_changed"));
    }

    #[test]
    fn collect_events_with_cap_events_are_timestamp_ascending() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        send_message(&conn, "m1", "alice", "2026-01-01T00:00:03Z", "text");
        send_message(&conn, "m2", "alice", "2026-01-01T00:00:01Z", "text");

        let result =
            collect_events_with_cap(&conn, "alice", None, "2026-01-01T00:01:00Z", no_env).unwrap();
        assert_eq!(result.events[0]["timestamp"], "2026-01-01T00:00:01Z");
        assert_eq!(result.events[1]["timestamp"], "2026-01-01T00:00:03Z");
    }

    #[test]
    fn collect_events_with_cap_untitled_root_is_held_only_when_subject_gen_is_on() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        // NULL subject -> placeholder preview -> held while within the
        // title-hold window, IF subject-gen is on.
        msg_repo::send(
            &conn,
            NewMessage {
                message_id: "m1",
                sender_id: "sender",
                recipient_id: "alice",
                message_content: "no real subject yet",
                message_type: "text",
                priority: "normal",
                timestamp: "2026-01-01T00:00:01Z",
                delivered: true,
                read: false,
                subject: None,
                parent_message_id: None,
            },
        )
        .unwrap();

        // subject-gen OFF: fires immediately with the preview.
        let off =
            collect_events_with_cap(&conn, "alice", None, "2026-01-01T00:00:02Z", no_env).unwrap();
        assert_eq!(off.events.len(), 1);

        // subject-gen ON, still within the 120s hold window: held.
        let get_env_on =
            |k: &str| (k == "AGENT_MCP_SUBJECT_MODEL").then(|| "some-model".to_string());
        let held =
            collect_events_with_cap(&conn, "alice", None, "2026-01-01T00:00:02Z", get_env_on)
                .unwrap();
        assert!(held.events.is_empty());
        assert!(held.msg_cap_ts.is_some());

        // subject-gen ON, past the hold window: fires anyway (never
        // stranded by a stalled backfill).
        let expired =
            collect_events_with_cap(&conn, "alice", None, "2026-01-01T00:02:30Z", get_env_on)
                .unwrap();
        assert_eq!(expired.events.len(), 1);
    }

    #[test]
    fn collect_events_with_cap_caps_at_the_message_query_cap() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        for i in 0..(MESSAGE_EVENT_QUERY_CAP + 5) {
            send_message(
                &conn,
                &format!("m{i}"),
                "alice",
                &format!(
                    "2026-01-01T{:02}:{:02}:{:02}Z",
                    i / 3600,
                    (i / 60) % 60,
                    i % 60
                ),
                "text",
            );
        }
        let result =
            collect_events_with_cap(&conn, "alice", None, "2026-01-02T00:00:00Z", no_env).unwrap();
        assert_eq!(result.events.len() as i64, MESSAGE_EVENT_QUERY_CAP);
        assert!(result.msg_cap_ts.is_some());
    }

    // -- collect_unassigned_task_events_for --------------------------------

    #[test]
    fn collect_unassigned_task_events_for_unknown_agent_is_empty() {
        let conn = test_conn();
        assert!(collect_unassigned_task_events_for(&conn, "nobody", None)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn collect_unassigned_task_events_for_returns_claimable_and_excludes_terminal() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        let claimable = NewTask {
            task_id: Some("task_1"),
            title: "up for grabs",
            description: None,
            assigned_to: None,
            created_by: "bob",
            status: "pending",
            priority: "medium",
            parent_task: None,
            child_tasks: None,
            depends_on_tasks: None,
            notes: None,
            now: "2026-01-01T00:00:00Z",
        };
        task_repository::create(&conn, claimable).unwrap();
        let done = NewTask {
            task_id: Some("task_2"),
            title: "finished",
            description: None,
            assigned_to: None,
            created_by: "bob",
            status: "completed",
            priority: "medium",
            parent_task: Some("task_1"),
            child_tasks: None,
            depends_on_tasks: None,
            notes: None,
            now: "2026-01-01T00:00:00Z",
        };
        task_repository::create(&conn, done).unwrap();

        let events = collect_unassigned_task_events_for(&conn, "alice", None).unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["payload"]["task_id"], "task_1");
    }

    // -- collect_agent_profile_events_for -----------------------------------

    #[test]
    fn collect_agent_profile_events_for_projects_the_repo_row() {
        let conn = test_conn();
        seed_agent(&conn, "manager");
        seed_agent(&conn, "worker");
        AgentRepository::review_profile(
            &conn,
            "worker",
            Some("curated"),
            Some("manager"),
            "2026-01-01T00:00:01Z",
        )
        .unwrap();

        let events = collect_agent_profile_events_for(&conn, "someone-else", None).unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["type"], "agent_profile_updated");
        assert_eq!(events[0]["ref_id"], "worker");
        assert_eq!(events[0]["data"]["profile"], "curated");
    }

    // -- check_auto_event_loop_flags ----------------------------------------

    #[test]
    fn check_auto_event_loop_flags_healthy_agent_is_enabled() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        assert_eq!(check_auto_event_loop_flags(&conn, "alice"), (true, None));
    }

    #[test]
    fn check_auto_event_loop_flags_global_off_disables_everyone() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        project_settings_repository::upsert(
            &conn,
            "config_auto_event_loop_global",
            "false",
            None,
            false,
            "operator",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        let (enabled, reason) = check_auto_event_loop_flags(&conn, "alice");
        assert!(!enabled);
        assert_eq!(
            reason.as_deref(),
            Some("config_auto_event_loop_global is OFF")
        );
    }

    #[test]
    fn check_auto_event_loop_flags_unknown_agent_is_disabled_with_a_not_found_reason() {
        let conn = test_conn();
        let (enabled, reason) = check_auto_event_loop_flags(&conn, "nobody");
        assert!(!enabled);
        assert!(reason.unwrap().contains("not found"));
    }

    #[test]
    fn check_auto_event_loop_flags_terminated_agent_is_disabled() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        AgentRepository::update_field(
            &conn,
            "alice",
            AgentField::Status,
            FieldValue::Text("terminated".to_string()),
            "2026-01-01T00:00:01Z",
        )
        .unwrap();
        let (enabled, reason) = check_auto_event_loop_flags(&conn, "alice");
        assert!(!enabled);
        assert!(reason.unwrap().contains("terminated"));
    }

    #[test]
    fn check_auto_event_loop_flags_per_agent_off_is_disabled_with_operator_pause_reason() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        AgentRepository::update_field(
            &conn,
            "alice",
            AgentField::AutoEventLoop,
            FieldValue::Bool(false),
            "2026-01-01T00:00:01Z",
        )
        .unwrap();
        let (enabled, reason) = check_auto_event_loop_flags(&conn, "alice");
        assert!(!enabled);
        assert!(reason.unwrap().contains("paused by operator"));
    }

    // -- idle_stop_seconds_remaining ------------------------------------

    #[test]
    fn idle_stop_seconds_remaining_disabled_when_window_is_zero() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        project_settings_repository::upsert(
            &conn,
            "config_event_idle_stop_seconds",
            "0",
            None,
            false,
            "operator",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        assert_eq!(
            idle_stop_seconds_remaining(&conn, "alice", "2026-01-01T00:00:00Z"),
            None
        );
    }

    #[test]
    fn idle_stop_seconds_remaining_seeds_and_grants_a_full_window_on_first_use() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        project_settings_repository::upsert(
            &conn,
            "config_event_idle_stop_seconds",
            "3600",
            None,
            false,
            "operator",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        let remaining = idle_stop_seconds_remaining(&conn, "alice", "2026-01-01T00:00:00Z");
        assert_eq!(remaining, Some(3600.0));
        // Seeded the marker -- confirmed by a second call computing a
        // real elapsed-time delta instead of re-seeding to a fresh 3600.
        let later = idle_stop_seconds_remaining(&conn, "alice", "2026-01-01T00:00:10Z");
        assert_eq!(later, Some(3590.0));
    }

    #[test]
    fn idle_stop_seconds_remaining_goes_negative_once_the_window_is_exceeded() {
        let conn = test_conn();
        seed_agent(&conn, "alice");
        project_settings_repository::upsert(
            &conn,
            "config_event_idle_stop_seconds",
            "60",
            None,
            false,
            "operator",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        idle_stop_seconds_remaining(&conn, "alice", "2026-01-01T00:00:00Z"); // seed
        let remaining = idle_stop_seconds_remaining(&conn, "alice", "2026-01-01T00:02:00Z");
        assert!(remaining.unwrap() <= 0.0);
    }
}
