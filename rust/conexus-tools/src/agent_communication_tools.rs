//! Port of `agent_mcp/tools/agent_communication_tools.py`'s
//! `wait_for_events_tool_impl` -- the entry section only (PR 2/4 of
//! the Phase D3 wake-loop tool port; the slow-path `while` loop lands
//! in a later PR, the real `Tool` impl wiring both together in the
//! PR after that).
//!
//! ## Connection-holding discipline (read before touching this file)
//!
//! Every OTHER `Tool` impl in this crate locks `conn: &AsyncMutex<
//! Connection>` ONCE as the first line of its body and holds the guard
//! for the whole call -- correct for them, since they all complete in
//! well under a second. `wait_for_events` must NOT follow that
//! convention: its real Python source never holds one DB connection
//! for the call's duration either -- every collector
//! (`_collect_events_with_cap`/`_check_auto_event_loop_flags`/etc.)
//! opens its OWN short-lived connection, closed before the next
//! `await`. This crate's Mutex-guarded single connection is this
//! project's actual single-writer lock; holding it across a bounded
//! wait that can legitimately last up to `CLAUDE_CODE_HOLD_CAP_SECONDS`
//! (24h) would stall every OTHER tool call and every other agent's own
//! poll for the whole hold. Every function here locks `conn`, does its
//! synchronous DB work, and drops the guard BEFORE returning to its
//! caller -- never holds it across an `.await` on anything
//! `wait_for_events`-loop-shaped (a wake-channel receive, a sleep).

use conexus_auth::ToolCallContext;
use conexus_core::principal::Principal;
use conexus_core::tool_result::ToolResult;
use conexus_db::agent_repository::{AgentField, AgentRepository, FieldValue};
use conexus_db::scheduled_directive_repository;
use conexus_wakeloop::waiter_registry::WakeSignal;
use conexus_wakeloop::{client_hold_strategy, event_feed, hold_ladder};
use rusqlite::Connection;
use serde_json::Value;
use tokio::sync::mpsc::{Receiver, Sender};
use tokio::sync::Mutex as AsyncMutex;

/// Cadence at which the slow-path loop re-checks the auto-event-loop
/// flags and re-derives the soonest schedule-due time. Matches
/// Python's `_FLAG_RECHECK_INTERVAL_SECONDS`.
pub const FLAG_RECHECK_INTERVAL_SECONDS: f64 = 2.0;

/// Effective "no cap" ceiling for a heartbeat-capable client with no
/// `HoldStrategy::hold_cap` (OpenCode's case). Matches Python's
/// `_UNCAPPED_HOLD_CEILING_SECONDS` (24h, the same number as
/// `CLAUDE_CODE_HOLD_CAP_SECONDS` -- a coincidence of both landing on
/// "one day," not a shared constant in Python either).
pub const UNCAPPED_HOLD_CEILING_SECONDS: u64 = 24 * 60 * 60;

/// Max consecutive heartbeat-notify failures before the slow-path loop
/// reaps a half-open connection. Matches Python's
/// `_MAX_HEARTBEAT_MISSES`.
pub const MAX_HEARTBEAT_MISSES: u32 = 2;

/// Bundled facts + the registered waiter channel the slow-path loop
/// (a later PR) needs, computed once by [`wait_for_events_entry`].
pub struct SlowPathSetup {
    pub agent_id: String,
    /// The caller's original cursor (possibly resolved from
    /// `agents.last_event_seen_at`) -- rides every envelope this call
    /// returns as the "preserve progress on empty" fallback.
    pub since: Option<String>,
    /// The resolved hold budget in seconds -- `base_hold`, clamped by
    /// the caller's own `timeout_seconds` unless the ladder overrides
    /// it back to `base_hold`.
    pub timeout: f64,
    pub ladder_eligible: bool,
    pub ladder_override: bool,
    pub heartbeat_enabled: bool,
    /// This call's own sender -- kept so the slow path's cleanup can
    /// call [`WaiterRegistry::unregister`] with the SAME sender it
    /// registered (identity-based, per that method's own doc).
    pub sender: Sender<WakeSignal>,
    pub receiver: Receiver<WakeSignal>,
}

/// Either the call is already over ([`Done`]), or the entry gates all
/// passed and the slow-path loop should run next ([`EnterSlowPath`]).
///
/// [`Done`]: EntryOutcome::Done
/// [`EnterSlowPath`]: EntryOutcome::EnterSlowPath
pub enum EntryOutcome {
    Done(ToolResult),
    EnterSlowPath(SlowPathSetup),
}

/// Best-effort env-var lookup for `AGENT_MCP_SUBJECT_MODEL` (the AI
/// subject-gen on/off flag `collect_events_with_cap`'s title-hold gate
/// needs) -- the real process environment, not a test fixture. See
/// `event_feed::collect_events_with_cap`'s own doc for why this is an
/// explicit `get_env` parameter rather than a direct `std::env::var`
/// call inside that function.
fn process_env(key: &str) -> Option<String> {
    std::env::var(key).ok()
}

/// Drain any signals already queued on this waiter's channel. Always
/// returns an empty `Vec`: [`WakeSignal`] is deliberately payload-less
/// (see `waiter_registry`'s module doc), so there is never a synthetic
/// event to convert into a `Value` here -- draining exists purely to
/// clear stale signals before a fresh DB re-query, not to collect
/// content.
fn drain(receiver: &mut Receiver<WakeSignal>) -> Vec<Value> {
    while receiver.try_recv().is_ok() {}
    Vec::new()
}

/// Run the entry section of `wait_for_events_tool_impl`: `since`
/// resolution, hold-strategy/ladder computation, waiter registration,
/// the auto-event-loop flag gate, the fast path (immediate return if
/// events are already pending), and the pre-loop idle-stop check.
/// Everything BEFORE the slow-path `while` loop.
///
/// `principal.agent_id` must be `Some` -- the caller enforces this via
/// `Requirement::Predicate` before this function is ever reached
/// (mirrors Python's `@requires_predicate(_is_identified_agent, ...)`).
pub async fn wait_for_events_entry(
    principal: &Principal,
    arguments: &Value,
    conn: &AsyncMutex<Connection>,
    now_iso: &str,
    ctx: &ToolCallContext<'_>,
) -> EntryOutcome {
    let agent_id = principal
        .agent_id
        .clone()
        .expect("Requirement::Predicate already enforced principal.agent_id is set");

    let since_arg = match arguments.get("since") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.clone()),
        Some(_) => {
            return EntryOutcome::Done(ToolResult::Invalid {
                field: Some("since".to_string()),
                message: "since must be an ISO-UTC timestamp string".to_string(),
            });
        }
    };
    // No explicit cursor from the caller -> resume from the agent's
    // persisted high-water cursor. Without this, a no-arg reconnect
    // (which the wake-loop recovery guidance tells agents to do)
    // resolves `since` to the epoch and re-dumps the entire backlog.
    let since = match since_arg {
        Some(s) => Some(s),
        None => {
            let guard = conn.lock().await;
            AgentRepository::get_by_id(&guard, &agent_id)
                .ok()
                .flatten()
                .and_then(|a| a.last_event_seen_at)
        }
    };

    // Event-loop long-hold: resolve the per-connection hold strategy
    // from the client's identity (with a progressToken feature-detect
    // fallback), then derive how long THIS connection may hold.
    let strategy =
        client_hold_strategy::resolve_hold_strategy(ctx.client_name, ctx.progress_token_present);
    let strategy_cap = strategy.hold_cap.unwrap_or(UNCAPPED_HOLD_CEILING_SECONDS) as f64;
    let base_hold = if strategy.heartbeat {
        strategy_cap
    } else {
        (client_hold_strategy::NO_HEARTBEAT_HOLD_SECONDS as f64).min(strategy_cap)
    };
    let requested = arguments
        .get("timeout_seconds")
        .and_then(Value::as_f64)
        .filter(|v| *v > 0.0);
    let mut timeout = requested.map(|r| base_hold.min(r)).unwrap_or(base_hold);

    // Adaptive hold ladder -- only for heartbeat-capable clients that
    // sent a progressToken AND are self-capping below the base hold.
    let ladder_eligible = strategy.heartbeat
        && ctx.progress_token_present
        && requested.is_some_and(|r| r < base_hold);
    let mut ladder_override = false;
    if ladder_eligible {
        let decision = hold_ladder::decide(hold_ladder::get_count(&agent_id));
        ladder_override = decision.override_hold;
        if ladder_override {
            timeout = base_hold; // ignore the agent's short cap; park it
        }
    } else {
        hold_ladder::reset(&agent_id);
    }

    // PR-B fan-out (now newest-wins, not fan-out -- see
    // `waiter_registry`'s module doc): register on entry. Any prior
    // waiter for this agent is superseded as a side effect of
    // `register()` itself, so there is no separate "supersede" call to
    // make the way Python's `supersede_prior_waiters` needed.
    let (sender, mut receiver) = ctx.waiter_registry.register(&agent_id);

    // Flag gate: if either toggle is OFF, return stop_listening now.
    let (enabled, reason) = {
        let guard = conn.lock().await;
        event_feed::check_auto_event_loop_flags(&guard, &agent_id)
    };
    if !enabled {
        ctx.waiter_registry.unregister(&agent_id, &sender);
        drain(&mut receiver);
        let stop_evt = event_feed::stop_listening_event(&reason.unwrap_or_default(), now_iso);
        return EntryOutcome::Done(event_feed::envelope(vec![stop_evt], since.as_deref(), None));
    }

    // Fast path -- combine DB backlog with anything already queued for
    // us between register() and here.
    let drained = drain(&mut receiver);
    let fast_feed = {
        let guard = conn.lock().await;
        event_feed::assemble_event_feed(
            &guard,
            &agent_id,
            since.as_deref(),
            now_iso,
            drained,
            true,
            process_env,
        )
    };
    if let Ok(assembled) = fast_feed {
        if !assembled.events.is_empty() {
            hold_ladder::reset(&agent_id); // a real event resets the ladder
            {
                let guard = conn.lock().await;
                let _ = AgentRepository::advance_event_cursor(
                    &guard,
                    &agent_id,
                    &assembled.next_cursor,
                    now_iso,
                );
                let _ = AgentRepository::update_field(
                    &guard,
                    &agent_id,
                    AgentField::LastActivityAt,
                    FieldValue::Text(now_iso.to_string()),
                    now_iso,
                );
            }
            ctx.waiter_registry.unregister(&agent_id, &sender);
            return EntryOutcome::Done(event_feed::envelope(
                assembled.events,
                since.as_deref(),
                None,
            ));
        }
    }

    // Idle-stop (event-loop wind-down), checked once before the loop.
    // An enabled schedule suppresses idle-stop -- the agent must stay
    // present to receive its fires.
    let idle_remaining = {
        let guard = conn.lock().await;
        event_feed::idle_stop_seconds_remaining(&guard, &agent_id, now_iso)
    };
    let has_schedule = {
        let guard = conn.lock().await;
        scheduled_directive_repository::has_active(&guard, &agent_id, now_iso).unwrap_or(false)
    };
    if let Some(remaining) = idle_remaining {
        if remaining <= 0.0 && !has_schedule {
            ctx.waiter_registry.unregister(&agent_id, &sender);
            drain(&mut receiver);
            let stop_evt = event_feed::stop_listening_event(
                "event-loop idle-stop window exceeded (no events)",
                now_iso,
            );
            return EntryOutcome::Done(event_feed::envelope(
                vec![stop_evt],
                since.as_deref(),
                None,
            ));
        }
    }

    let heartbeat_enabled = strategy.heartbeat && ctx.progress_token_present;
    EntryOutcome::EnterSlowPath(SlowPathSetup {
        agent_id,
        since,
        timeout,
        ladder_eligible,
        ladder_override,
        heartbeat_enabled,
        sender,
        receiver,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_core::capability::Capabilities;
    use conexus_core::principal::PrincipalKind;
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::waiter_registry::WaiterRegistry;
    use serde_json::json;

    fn test_conn() -> AsyncMutex<Connection> {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        AsyncMutex::new(conn)
    }

    async fn seed_agent(conn: &AsyncMutex<Connection>, agent_id: &str) {
        let guard = conn.lock().await;
        AgentRepository::create(
            &guard,
            conexus_db::agent_repository::NewAgent {
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

    fn agent_bearer(agent_id: &str) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some(agent_id.to_string()),
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: true,
            source_token: None,
            capabilities: Capabilities::from_iter([]),
        }
    }

    const NOW: &str = "2026-01-01T00:01:00Z";

    #[tokio::test]
    async fn fast_path_returns_pending_events_immediately() {
        let conn = test_conn();
        seed_agent(&conn, "alice").await;
        {
            let guard = conn.lock().await;
            conexus_db::message_repository::send(
                &guard,
                conexus_db::message_repository::NewMessage {
                    message_id: "m1",
                    sender_id: "bob",
                    recipient_id: "alice",
                    message_content: "hi",
                    message_type: "text",
                    priority: "normal",
                    timestamp: "2026-01-01T00:00:01Z",
                    delivered: true,
                    read: false,
                    subject: Some("hello"),
                    parent_message_id: None,
                },
            )
            .unwrap();
        }

        let registry = WaiterRegistry::new();
        let ctx = ToolCallContext::off_wire(&registry);
        let principal = agent_bearer("alice");
        let outcome = wait_for_events_entry(&principal, &json!({}), &conn, NOW, &ctx).await;
        let EntryOutcome::Done(result) = outcome else {
            panic!("expected the fast path to end the call");
        };
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got a non-Ok ToolResult");
        };
        let events = data.unwrap()["events"].as_array().unwrap().len();
        assert_eq!(events, 1);
        // Fast path unregisters -- no waiter left parked.
        assert_eq!(registry.waiter_count("alice"), 0);
    }

    #[tokio::test]
    async fn flag_gate_off_returns_stop_listening_and_unregisters() {
        let conn = test_conn();
        seed_agent(&conn, "alice").await;
        {
            let guard = conn.lock().await;
            conexus_db::project_settings_repository::upsert(
                &guard,
                "config_auto_event_loop_global",
                "false",
                None,
                false,
                "operator",
                "2026-01-01T00:00:00Z",
            )
            .unwrap();
        }

        let registry = WaiterRegistry::new();
        let ctx = ToolCallContext::off_wire(&registry);
        let principal = agent_bearer("alice");
        let outcome = wait_for_events_entry(&principal, &json!({}), &conn, NOW, &ctx).await;
        let EntryOutcome::Done(result) = outcome else {
            panic!("expected the flag gate to end the call");
        };
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got a non-Ok ToolResult");
        };
        let events = data.unwrap();
        let events = events["events"].as_array().unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["type"], "stop_listening");
        assert_eq!(registry.waiter_count("alice"), 0);
    }

    #[tokio::test]
    async fn pre_loop_idle_stop_fires_when_window_already_exceeded() {
        let conn = test_conn();
        seed_agent(&conn, "alice").await;
        {
            let guard = conn.lock().await;
            conexus_db::project_settings_repository::upsert(
                &guard,
                "config_event_idle_stop_seconds",
                "60",
                None,
                false,
                "operator",
                "2026-01-01T00:00:00Z",
            )
            .unwrap();
            // Seed last_activity_at far enough in the past that the
            // 60s window is already exceeded by NOW.
            AgentRepository::update_field(
                &guard,
                "alice",
                AgentField::LastActivityAt,
                FieldValue::Text("2025-01-01T00:00:00Z".to_string()),
                "2025-01-01T00:00:00Z",
            )
            .unwrap();
        }

        let registry = WaiterRegistry::new();
        let ctx = ToolCallContext::off_wire(&registry);
        let principal = agent_bearer("alice");
        let outcome = wait_for_events_entry(&principal, &json!({}), &conn, NOW, &ctx).await;
        let EntryOutcome::Done(result) = outcome else {
            panic!("expected pre-loop idle-stop to end the call");
        };
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got a non-Ok ToolResult");
        };
        let events = data.unwrap();
        let events = events["events"].as_array().unwrap();
        assert_eq!(events[0]["type"], "stop_listening");
    }

    #[tokio::test]
    async fn an_active_schedule_suppresses_idle_stop() {
        let conn = test_conn();
        seed_agent(&conn, "alice").await;
        {
            let guard = conn.lock().await;
            conexus_db::project_settings_repository::upsert(
                &guard,
                "config_event_idle_stop_seconds",
                "60",
                None,
                false,
                "operator",
                "2026-01-01T00:00:00Z",
            )
            .unwrap();
            AgentRepository::update_field(
                &guard,
                "alice",
                AgentField::LastActivityAt,
                FieldValue::Text("2025-01-01T00:00:00Z".to_string()),
                "2025-01-01T00:00:00Z",
            )
            .unwrap();
            scheduled_directive_repository::create(
                &guard,
                "sched_1",
                "alice",
                "ping",
                3600,
                "2027-01-01T00:00:00Z", // far in the future -- not due, just active
                None,
                None,
                Some("operator"),
                "2025-12-31T00:00:00Z",
            )
            .unwrap();
        }

        let registry = WaiterRegistry::new();
        let ctx = ToolCallContext::off_wire(&registry);
        let principal = agent_bearer("alice");
        let outcome = wait_for_events_entry(&principal, &json!({}), &conn, NOW, &ctx).await;
        assert!(
            matches!(outcome, EntryOutcome::EnterSlowPath(_)),
            "an active schedule must suppress idle-stop and reach the slow path"
        );
    }

    #[tokio::test]
    async fn no_events_and_no_idle_stop_configured_enters_the_slow_path() {
        let conn = test_conn();
        seed_agent(&conn, "alice").await;

        let registry = WaiterRegistry::new();
        let ctx = ToolCallContext::off_wire(&registry);
        let principal = agent_bearer("alice");
        let outcome = wait_for_events_entry(&principal, &json!({}), &conn, NOW, &ctx).await;
        let EntryOutcome::EnterSlowPath(setup) = outcome else {
            panic!("expected to reach the slow path");
        };
        assert_eq!(setup.agent_id, "alice");
        assert_eq!(registry.waiter_count("alice"), 1);
    }

    #[tokio::test]
    async fn registering_a_second_call_supersedes_the_first_waiter() {
        let conn = test_conn();
        seed_agent(&conn, "alice").await;
        let registry = WaiterRegistry::new();
        let ctx = ToolCallContext::off_wire(&registry);
        let principal = agent_bearer("alice");

        let first = wait_for_events_entry(&principal, &json!({}), &conn, NOW, &ctx).await;
        let EntryOutcome::EnterSlowPath(mut first_setup) = first else {
            panic!("expected the first call to reach the slow path");
        };

        let second = wait_for_events_entry(&principal, &json!({}), &conn, NOW, &ctx).await;
        assert!(matches!(second, EntryOutcome::EnterSlowPath(_)));

        // The FIRST call's receiver must observe a Superseded signal.
        assert_eq!(first_setup.receiver.try_recv(), Ok(WakeSignal::Superseded));
    }

    #[tokio::test]
    async fn invalid_since_type_is_rejected_before_any_registration() {
        let conn = test_conn();
        seed_agent(&conn, "alice").await;
        let registry = WaiterRegistry::new();
        let ctx = ToolCallContext::off_wire(&registry);
        let principal = agent_bearer("alice");
        let outcome =
            wait_for_events_entry(&principal, &json!({"since": 12345}), &conn, NOW, &ctx).await;
        let EntryOutcome::Done(result) = outcome else {
            panic!("expected validation to end the call");
        };
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("since"))
        );
        assert_eq!(registry.waiter_count("alice"), 0);
    }
}
