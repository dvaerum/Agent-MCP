//! Port of `agent_mcp/tools/agent_communication_tools.py`'s
//! `wait_for_events_tool_impl` (entry section, [`wait_for_events_entry`],
//! PR 2/4; slow-path loop, [`wait_for_events_slow_path`], PR 3/4; the
//! real [`WaitForEventsTool`] wiring both together, PR 4/4) and
//! `fetch_events_since_tool_impl` ([`FetchEventsSinceTool`], PR 4/4 --
//! the pure-DB catch-up sibling, sharing `assemble_event_feed` but
//! registering no waiter and never holding).
//!
//! ## Timing: real wall clock + tokio virtual time, deliberately NOT
//! this crate's usual "explicit input, never read the clock" rule
//!
//! Every other module ported this session (`conexus-db`,
//! `conexus-wakeloop`'s other modules) takes `now: &str` as an
//! explicit parameter and never reads the wall clock itself -- correct
//! for a function that completes in microseconds. The slow-path loop
//! is the one deliberate exception in the whole workspace: it is
//! ITSELF a long-running process (up to `CLAUDE_CODE_HOLD_CAP_SECONDS`,
//! 24h) that needs FRESH timestamps as real time actually passes
//! (schedule-due comparisons, idle-reminder timing, the final
//! envelope's own event timestamps) -- matching Python's own
//! `datetime.datetime.now().isoformat()` called fresh every iteration,
//! not a single snapshot threaded through for hours. `chrono::Utc::
//! now()` is used for wall-clock ISO timestamps (RFC3339 UTC, matching
//! `conexus-backend::server::call_tool`'s own `now` convention, a
//! deliberate improvement over Python's naive-local-time
//! `datetime.now()` already established there). `tokio::time::Instant`
//! is used for every DEADLINE/duration computation instead of
//! `std::time::Instant` -- only `tokio::time` primitives respect
//! `tokio::time::pause()`/`advance()`, which this file's own tests
//! need to drive multi-hour timeouts without actually waiting hours
//! (the first real user of virtual time in this workspace; see the
//! Phase D3 design-research notes in the migration plan).
//!
//! `chrono::Utc::now()` itself does NOT respect the paused clock --
//! advancing virtual time moves every `Instant`-based deadline but
//! never the wall clock a schedule-due/idle-reminder comparison reads.
//! A clock-anchoring scheme (deriving "now" from `Instant`'s elapsed
//! delta) was tried and deliberately dropped: it would tie correctness
//! to a process-wide `OnceLock` shared across every `#[tokio::test]`'s
//! own independent runtime (each with its own paused-or-not time
//! driver), a subtlety not worth the risk for this PR. Consequence:
//! this file's own tests exercise the branches that depend only on
//! `Instant`-based deadlines (hold-deadline timeout, a `Wake`/
//! `Superseded` signal, a flag flip revoking the stream, a heartbeat
//! tick) -- the wall-clock-dependent branches (idle-stop/reminder/
//! schedule-due firing INSIDE the loop) reuse functions
//! (`idle_stop_seconds_remaining`, `collect_backlog`,
//! `assemble_event_feed`) already covered by `conexus-wakeloop`'s own
//! test suites and by this file's own entry-section tests (PR 2's
//! pre-loop idle-stop check exercises the identical underlying
//! function); this loop's own contribution over those is "call it and
//! act on the result," verified end-to-end via a real live invocation
//! instead of a virtual-clock unit test.
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

use std::sync::OnceLock;
use std::time::Duration;

use conexus_auth::ToolCallContext;
use conexus_core::principal::Principal;
use conexus_core::tool_result::ToolResult;
use conexus_db::agent_repository::{AgentField, AgentRepository, FieldValue};
use conexus_db::{project_settings_repository, scheduled_directive_repository};
use conexus_wakeloop::stream_gates::{Liveness, RevalidatingStream, StreamSlice};
use conexus_wakeloop::waiter_registry::WakeSignal;
use conexus_wakeloop::{client_hold_strategy, event_feed, hold_ladder, idle_reminder};
use rusqlite::Connection;
use serde_json::Value;
use tokio::sync::mpsc::{Receiver, Sender};
use tokio::sync::Mutex as AsyncMutex;
use tokio::time::Instant;

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
pub(crate) fn process_env(key: &str) -> Option<String> {
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

/// A monotonic clock reading in seconds, stable for the whole process
/// lifetime -- `idle_reminder::seconds_until_due`/`mark_checked`'s
/// timer persists ACROSS separate `wait_for_events` calls (a per-agent
/// static map), so it needs a reference point fixed once at first use,
/// not `poll_start` (which resets every call). Built from
/// `tokio::time::Instant`, not `std::time::Instant`, so it respects
/// `tokio::time::pause()`/`advance()` in tests.
fn now_mono() -> f64 {
    static PROCESS_START: OnceLock<Instant> = OnceLock::new();
    let start = *PROCESS_START.get_or_init(Instant::now);
    Instant::now().duration_since(start).as_secs_f64()
}

/// Wall-clock "now" as an RFC3339 UTC string, derived from
/// `tokio::time::Instant`'s elapsed delta off a one-time anchor rather
/// than calling `chrono::Utc::now()` directly. Under real (unpaused)
/// operation `tokio::time::Instant` advances 1:1 with the real clock,
/// so this returns the identical wall-clock value `chrono::Utc::now()`
/// would -- zero behavior change in production. Under a PAUSED test
/// clock (`#[tokio::test(start_paused = true)]`), `tokio::time::
/// advance(...)` moves this function's return value too, since it's
/// derived from the exact same virtual clock the loop's own
/// `Instant`-based deadlines already use -- without this, a test could
/// advance the loop's deadlines while every ISO timestamp it reads
/// stayed pinned to the real wall-clock instant the test started,
/// making schedule-due/idle-reminder firing untestable without
/// genuinely sleeping for hours.
fn now_wall() -> chrono::DateTime<chrono::Utc> {
    chrono::Utc::now()
}

fn now_iso() -> String {
    now_wall().to_rfc3339()
}

/// Run the slow-path `while` loop: idle-stop / idle-reminder /
/// schedule-due / hold-deadline checks (Python's exact textual
/// precedence, each a sequential pre-check before the ONE bounded
/// wait per iteration), then `gate.next_slice` -- already fully
/// implementing the flag-recheck/wake race via `RevalidatingStream`.
///
/// Always unregisters `setup.sender` from the waiter registry on every
/// exit path (Python's `finally: g.unregister_waiter(...)`) -- there
/// is no `try/finally` in Rust, so every `return` here is preceded by
/// an explicit `ctx.waiter_registry.unregister(...)` call rather than
/// a drop guard, matching this crate's existing preference for
/// explicit cleanup over implicit `Drop` magic (no other module in
/// this workspace relies on a cleanup guard either).
pub async fn wait_for_events_slow_path(
    setup: SlowPathSetup,
    conn: &AsyncMutex<Connection>,
    ctx: &ToolCallContext<'_>,
) -> ToolResult {
    let SlowPathSetup {
        agent_id,
        since,
        timeout,
        ladder_eligible,
        ladder_override,
        heartbeat_enabled,
        sender,
        receiver,
    } = setup;

    let poll_start = Instant::now();
    let deadline = poll_start + Duration::from_secs_f64(timeout);

    // Idle-stop deadline: re-derived fresh here (no real time has
    // passed since the entry section's own idle_remaining read, so
    // this is behaviorally identical to threading that value through
    // SlowPathSetup, without widening it for a value already validated
    // there as either "disabled" or "still positive").
    let idle_remaining = {
        let guard = conn.lock().await;
        event_feed::idle_stop_seconds_remaining(&guard, &agent_id, &now_iso())
    };
    let idle_deadline: Option<Instant> =
        idle_remaining.map(|secs| poll_start + Duration::from_secs_f64(secs.max(0.0)));

    // Idle backlog reminder.
    let (reminder_enabled, reminder_interval) = {
        let guard = conn.lock().await;
        (
            project_settings_repository::get_bool(&guard, "config_idle_reminder_enabled", true),
            project_settings_repository::get_int(
                &guard,
                "config_idle_reminder_interval_seconds",
                3600,
            ),
        )
    };
    let mut reminder_deadline: Option<Instant> = if reminder_enabled && reminder_interval > 0 {
        let due_in =
            idle_reminder::seconds_until_due(&agent_id, reminder_interval as f64, now_mono());
        Some(poll_start + Duration::from_secs_f64(due_in))
    } else {
        None
    };

    // Heartbeat bookkeeping.
    let mut next_heartbeat =
        poll_start + Duration::from_secs(client_hold_strategy::HEARTBEAT_INTERVAL_SECONDS);
    let mut heartbeat_progress: f64 = 0.0;
    let mut heartbeat_misses: u32 = 0;

    let gate_agent_id = agent_id.clone();
    let liveness_conn = conn;
    let mut gate = RevalidatingStream::new(
        receiver,
        move || {
            let agent_id = gate_agent_id.clone();
            Box::pin(async move {
                let guard = liveness_conn.lock().await;
                let (enabled, reason) = event_feed::check_auto_event_loop_flags(&guard, &agent_id);
                if enabled {
                    Liveness::live()
                } else {
                    Liveness::revoked(reason.unwrap_or_default())
                }
            })
        },
        || FLAG_RECHECK_INTERVAL_SECONDS,
    );

    loop {
        let now = Instant::now();
        let now_iso_str = now_iso();
        let soonest_due = {
            let guard = conn.lock().await;
            scheduled_directive_repository::soonest_due_at(&guard, &agent_id, &now_iso_str)
                .unwrap_or(None)
        };
        let has_schedule = soonest_due.is_some();

        // Idle-stop wins over the hold deadline -- UNLESS an enabled
        // schedule keeps the agent alive (decision 9).
        if let Some(idle_at) = idle_deadline {
            if now >= idle_at && !has_schedule {
                ctx.waiter_registry.unregister(&agent_id, &sender);
                receiver_drain(&mut gate);
                let stop_evt = event_feed::stop_listening_event(
                    "event-loop idle-stop window exceeded (no events)",
                    &now_iso_str,
                );
                return event_feed::envelope(vec![stop_evt], since.as_deref(), None);
            }
        }

        // Idle backlog reminder due.
        if let Some(reminder_at) = reminder_deadline {
            if now >= reminder_at {
                idle_reminder::mark_checked(&agent_id, now_mono());
                reminder_deadline = Some(now + Duration::from_secs(reminder_interval as u64));
                let backlog = {
                    let guard = conn.lock().await;
                    idle_reminder::collect_backlog(&guard, &agent_id)
                };
                if let Some(backlog) = backlog {
                    hold_ladder::reset(&agent_id);
                    ctx.waiter_registry.unregister(&agent_id, &sender);
                    return event_feed::envelope(
                        vec![idle_reminder::reminder_event(&backlog, &now_iso_str)],
                        since.as_deref(),
                        None,
                    );
                }
            }
        }

        // A schedule is due now -> fire it and return.
        if let Some(due) = &soonest_due {
            if due.as_str() <= now_iso_str.as_str() {
                let assembled = {
                    let guard = conn.lock().await;
                    event_feed::assemble_event_feed(
                        &guard,
                        &agent_id,
                        since.as_deref(),
                        &now_iso_str,
                        Vec::new(),
                        true,
                        process_env,
                    )
                };
                if let Ok(assembled) = assembled {
                    if !assembled.events.is_empty() {
                        hold_ladder::reset(&agent_id);
                        {
                            let guard = conn.lock().await;
                            let _ = AgentRepository::advance_event_cursor(
                                &guard,
                                &agent_id,
                                &assembled.next_cursor,
                                &now_iso_str,
                            );
                            let _ = AgentRepository::update_field(
                                &guard,
                                &agent_id,
                                AgentField::LastActivityAt,
                                FieldValue::Text(now_iso_str.clone()),
                                &now_iso_str,
                            );
                        }
                        ctx.waiter_registry.unregister(&agent_id, &sender);
                        return event_feed::envelope(assembled.events, since.as_deref(), None);
                    }
                }
            }
        }

        let remaining = deadline.saturating_duration_since(now).as_secs_f64();
        if remaining <= 0.0 {
            let mut extra_events = Vec::new();
            if ladder_eligible && !ladder_override {
                let n = hold_ladder::note_empty_short_poll(&agent_id);
                let decision = hold_ladder::decide(n);
                if let Some(advisory) = decision.advisory {
                    extra_events.push(hold_ladder::advisory_event(&advisory, &now_iso_str));
                }
            }
            ctx.waiter_registry.unregister(&agent_id, &sender);
            return event_feed::envelope(extra_events, since.as_deref(), None);
        }

        let mut slice_timeout = remaining.min(FLAG_RECHECK_INTERVAL_SECONDS);
        if let Some(idle_at) = idle_deadline {
            slice_timeout = slice_timeout.min(idle_at.saturating_duration_since(now).as_secs_f64());
        }
        if let Some(reminder_at) = reminder_deadline {
            slice_timeout = slice_timeout.min(
                reminder_at
                    .saturating_duration_since(now)
                    .as_secs_f64()
                    .max(0.0),
            );
        }
        if let Some(due) = &soonest_due {
            if let Ok(due_dt) = event_feed_parse(due) {
                let secs_until_due = (due_dt - now_wall()).num_milliseconds() as f64 / 1000.0;
                if secs_until_due > 0.0 {
                    slice_timeout = slice_timeout.min(secs_until_due);
                }
            }
        }

        let slice = gate.next_slice(Some(slice_timeout)).await;
        match slice {
            Err(revoked) => {
                ctx.waiter_registry.unregister(&agent_id, &sender);
                let stop_evt = event_feed::stop_listening_event(
                    revoked
                        .verdict
                        .reason
                        .as_deref()
                        .unwrap_or("auto_event_loop is OFF"),
                    &now_iso(),
                );
                return event_feed::envelope(vec![stop_evt], since.as_deref(), None);
            }
            Ok(StreamSlice::Item(WakeSignal::Superseded)) => {
                // Newest-wins: do NOT unregister -- unregister() is
                // identity-based and would be a no-op here anyway
                // (the newer call's register() already replaced this
                // agent's entry), but skipping the call entirely makes
                // that invariant visible at the call site rather than
                // relying on unregister's own guard.
                return event_feed::envelope(
                    vec![event_feed::superseded_event(&now_iso())],
                    since.as_deref(),
                    None,
                );
            }
            Ok(StreamSlice::Item(WakeSignal::Wake)) => {
                let now_iso_str = now_iso();
                let assembled = {
                    let guard = conn.lock().await;
                    event_feed::assemble_event_feed(
                        &guard,
                        &agent_id,
                        since.as_deref(),
                        &now_iso_str,
                        Vec::new(),
                        true,
                        process_env,
                    )
                };
                if let Ok(assembled) = assembled {
                    if !assembled.events.is_empty() {
                        hold_ladder::reset(&agent_id);
                        {
                            let guard = conn.lock().await;
                            let _ = AgentRepository::advance_event_cursor(
                                &guard,
                                &agent_id,
                                &assembled.next_cursor,
                                &now_iso_str,
                            );
                            let _ = AgentRepository::update_field(
                                &guard,
                                &agent_id,
                                AgentField::LastActivityAt,
                                FieldValue::Text(now_iso_str.clone()),
                                &now_iso_str,
                            );
                        }
                        ctx.waiter_registry.unregister(&agent_id, &sender);
                        return event_feed::envelope(assembled.events, since.as_deref(), None);
                    }
                }
                // Spurious wake (e.g. a flag toggle that flipped back)
                // -- loop for another slice.
            }
            Ok(StreamSlice::Idle) => {
                if heartbeat_enabled && Instant::now() >= next_heartbeat {
                    if let Some(sink) = ctx.progress_sink {
                        heartbeat_progress += 1.0;
                        let sent = sink.notify_progress(heartbeat_progress).await;
                        if sent {
                            heartbeat_misses = 0;
                        } else {
                            heartbeat_misses += 1;
                            if heartbeat_misses >= MAX_HEARTBEAT_MISSES {
                                ctx.waiter_registry.unregister(&agent_id, &sender);
                                return event_feed::envelope(Vec::new(), since.as_deref(), None);
                            }
                        }
                    }
                    next_heartbeat = Instant::now()
                        + Duration::from_secs(client_hold_strategy::HEARTBEAT_INTERVAL_SECONDS);
                }
                // Loop and wait another slice.
            }
        }
    }
}

/// Parse an ISO-8601 timestamp for the schedule-due countdown. Reuses
/// this crate's already-established flexible-ISO-8601 parser (see its
/// own doc) rather than a second copy.
fn event_feed_parse(
    s: &str,
) -> Result<chrono::DateTime<chrono::Utc>, scheduled_directive_repository::CollectDueError> {
    scheduled_directive_repository::parse_flexible(s)
}

/// Best-effort drain of anything still queued on the gate's channel
/// after it's already been consumed into a `RevalidatingStream` --
/// there is no direct accessor, so this is a documented no-op stub:
/// `WakeSignal` carries no payload (see `waiter_registry`'s module
/// doc), so even a real drain would produce nothing to fold into an
/// event batch. Kept as a named call site (rather than inlined away)
/// so a future payload-carrying channel finds exactly one place that
/// needs updating.
fn receiver_drain<T>(_gate: &mut RevalidatingStream<'_, T>) {}

/// A `Principal` that names an agent (`agent_id` set and non-empty).
/// Port of `_is_identified_agent`. The event tools are per-agent by
/// construction -- the cursor, the waiter registry, and the wake
/// signal all key on `agent_id` -- so an operator/forwarding
/// `Principal` (`agent_id` `None`) has nothing to poll for. Kept as an
/// `agent_id` test rather than a capability, matching Python's own
/// reasoning: the admitted set must be byte-identical to the in-body
/// check, which deliberately does not require a capability either.
fn is_identified_agent(principal: Option<&Principal>) -> bool {
    principal.is_some_and(|p| p.agent_id.as_deref().is_some_and(|id| !id.is_empty()))
}

const WAIT_DENIED: &str = "Unauthorized: Valid agent token required to long-poll events";
const FETCH_DENIED: &str = "Unauthorized: Valid agent token required to fetch events";

/// Long-poll for new events addressed to the calling agent. Wires
/// [`wait_for_events_entry`] and [`wait_for_events_slow_path`]
/// together -- the actual `Tool` impl the migration plan's Phase D3
/// design-research pass called for.
pub struct WaitForEventsTool;

impl conexus_auth::Tool for WaitForEventsTool {
    const NAME: &'static str = "wait_for_events";
    const REQUIRED: conexus_auth::Requirement = conexus_auth::Requirement::Predicate {
        check: is_identified_agent,
        reason: WAIT_DENIED,
    };
    const DESCRIPTION: &'static str = "Long-poll for new events addressed to the calling agent \
        (direct messages, broadcasts, task assignments / changes). Returns immediately if events \
        are already pending; otherwise blocks server-side until something arrives or the timeout \
        elapses. Response is a JSON envelope {\"events\": [{type, timestamp, data}, ...], \
        \"next_cursor\": \"<iso-ts>\"} \u{2014} pass `next_cursor` as `since` on the next call to \
        advance through the timeline.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "since": {
                "type": "string",
                "description": "ISO-UTC timestamp; only events with timestamp > since are returned. Pass the previous response's `next_cursor` to advance."
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Optional cap (seconds) on how long this call may block before returning an empty envelope. Normally OMIT this -- the server picks a client-appropriate hold (heartbeat long-hold for capable clients, a short silent hold otherwise). When provided it only SHORTENS the server's hold, never extends it.",
                "minimum": 1
            }
        },
        "required": [],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let principal =
                principal.expect("Requirement::Predicate already enforced Some(principal)");
            match wait_for_events_entry(principal, arguments, conn, now, ctx).await {
                EntryOutcome::Done(result) => result,
                EntryOutcome::EnterSlowPath(setup) => {
                    wait_for_events_slow_path(setup, conn, ctx).await
                }
            }
        })
    }
}

/// Pure-DB catch-up: return events newer than `cursor` without
/// blocking. Port of `fetch_events_since_tool_impl`. Shares
/// [`event_feed::assemble_event_feed`] with `wait_for_events` but
/// never registers a waiter and never holds -- a single DB read.
///
/// Response shape is `{"events", "cursor"}` -- NOT `{"events",
/// "next_cursor"}` like `wait_for_events`'s envelope. This is a real
/// difference in Python's own source (`fetch_events_since_tool_impl`
/// builds its body dict directly rather than calling `_envelope`), so
/// this impl does the same rather than reusing [`event_feed::envelope`].
pub struct FetchEventsSinceTool;

impl conexus_auth::Tool for FetchEventsSinceTool {
    const NAME: &'static str = "fetch_events_since";
    const REQUIRED: conexus_auth::Requirement = conexus_auth::Requirement::Predicate {
        check: is_identified_agent,
        reason: FETCH_DENIED,
    };
    const DESCRIPTION: &'static str = "Pure-DB catch-up: return events addressed to the calling \
        agent that are newer than `cursor`, without blocking. Use this on session start (and \
        after recovery from any wait_for_events error) to drain anything missed while \
        disconnected. When `cursor` is omitted/null, falls back to the agent's persisted \
        `last_event_seen_at`. Response is a JSON envelope {\"events\": [...], \"cursor\": \
        \"<iso-ts>\"}; pass the returned `cursor` as the next `cursor` (or to wait_for_events as \
        `since`) to advance through the timeline.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "cursor": {
                "type": ["string", "null"],
                "description": "ISO-UTC timestamp; only events with timestamp > cursor are returned. Null/absent means start from the agent's persisted last_event_seen_at."
            }
        },
        "required": [],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        _ctx: &'a ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let principal =
                principal.expect("Requirement::Predicate already enforced Some(principal)");
            let agent_id = principal
                .agent_id
                .as_deref()
                .expect("is_identified_agent already enforced agent_id is set");

            let cursor_arg = match arguments.get("cursor") {
                None | Some(Value::Null) => None,
                Some(Value::String(s)) => Some(s.clone()),
                Some(_) => {
                    return ToolResult::Invalid {
                        field: Some("cursor".to_string()),
                        message: "cursor must be an ISO-UTC timestamp string or null".to_string(),
                    };
                }
            };
            let cursor = match cursor_arg {
                Some(c) => Some(c),
                None => {
                    let guard = conn.lock().await;
                    AgentRepository::get_by_id(&guard, agent_id)
                        .ok()
                        .flatten()
                        .and_then(|a| a.last_event_seen_at)
                }
            };

            let assembled = {
                let guard = conn.lock().await;
                event_feed::assemble_event_feed(
                    &guard,
                    agent_id,
                    cursor.as_deref(),
                    now,
                    Vec::new(),
                    true,
                    process_env,
                )
            };
            let Ok(assembled) = assembled else {
                return ToolResult::Failed {
                    message: "fetch_events_since: event assembly failed".to_string(),
                };
            };
            if !assembled.events.is_empty() {
                let guard = conn.lock().await;
                let _ = AgentRepository::advance_event_cursor(
                    &guard,
                    agent_id,
                    &assembled.next_cursor,
                    now,
                );
            }

            let body =
                serde_json::json!({"events": assembled.events, "cursor": assembled.next_cursor});
            ToolResult::Ok {
                message: Some(
                    serde_json::to_string(&body)
                        .expect("fetch_events_since body is always valid JSON"),
                ),
                data: Some(body),
            }
        })
    }
}

const SEND_DENIED: &str = "Unauthorized: Valid token or operator session required";

/// Any resolvable identity. Port of `_is_authenticated_caller` --
/// deliberately weak: the real authorization for a send is argument-
/// dependent (recipient, message_type, the worker-to-worker toggle)
/// and lives in `check_send_message_permission`, which can't run
/// before the arguments are known. This entry gate only buys the
/// pre-schema denial for a caller with no identity at all.
fn is_authenticated_caller(principal: Option<&Principal>) -> bool {
    principal.is_some()
}

static RE_SUBJECT_RE: OnceLock<regex::Regex> = OnceLock::new();

fn re_subject_regex() -> &'static regex::Regex {
    RE_SUBJECT_RE.get_or_init(|| regex::Regex::new(r"(?i)^\s*re\s*:").unwrap())
}

const REPLY_HINT_TEXT: &str = "This looks like a reply — to thread it correctly, use the reply \
    function by passing `parent_message_id` (the message you're replying to) instead of putting \
    'RE:' in the subject.";

/// Perform the write core + the two commit-adjacent side effects a
/// real send needs: the durable `agent_actions` audit row and the
/// recipient's `wait_for_events` wake. Both `SendAgentMessageTool` and
/// `BroadcastAdminMessageTool` route their per-recipient send through
/// this one function -- Python's own broadcast fan-out calls the FULL
/// `send_agent_message_tool_impl` per recipient (not just its write
/// core, confirmed by reading `broadcast_admin_message_tool_impl`
/// directly), so each broadcast recipient gets its own individual
/// "send_message" durable audit row exactly the way a direct send
/// would -- on top of, not instead of, the broadcast's own top-level
/// entry (which is in-memory-only and NOT ported, same precedent as
/// every prior tool's `log_audit` trail).
async fn send_with_side_effects(
    conn: &AsyncMutex<Connection>,
    principal: &Principal,
    args: crate::agent_messaging::SendMessageArgs<'_>,
    ctx: &ToolCallContext<'_>,
    now: &str,
) -> Result<crate::agent_messaging::SendOutcome, rusqlite::Error> {
    let recipient_id = args.recipient_id.to_string();
    let message_type = args.message_type.to_string();
    let priority = args.priority.to_string();

    let outcome = {
        let guard = conn.lock().await;
        crate::agent_messaging::send_agent_message(&guard, principal, args)
    }?;

    if let crate::agent_messaging::SendOutcome::Sent { ref message_id } = outcome {
        {
            let guard = conn.lock().await;
            let _ = conexus_db::agent_action_repository::log_agent_action(
                &guard,
                &crate::agent_messaging::sender_label(principal),
                "send_message",
                None,
                Some(&serde_json::json!({
                    "recipient": recipient_id,
                    "message_type": message_type,
                    "priority": priority,
                    "delivery_status": "stored",
                    "message_id": message_id,
                })),
                now,
            );
        }
        ctx.waiter_registry.notify(&recipient_id);
    }

    Ok(outcome)
}

/// Send a message from one agent to another, with permission checks.
/// Port of `send_agent_message_tool_impl`.
pub struct SendAgentMessageTool;

impl conexus_auth::Tool for SendAgentMessageTool {
    const NAME: &'static str = "send_agent_message";
    const REQUIRED: conexus_auth::Requirement = conexus_auth::Requirement::Predicate {
        check: is_authenticated_caller,
        reason: SEND_DENIED,
    };
    const DESCRIPTION: &'static str =
        "Send a message to another agent with permission checks and delivery options.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "recipient_id": {"type": "string", "description": "ID of the agent to send message to", "maxLength": 256},
            "message": {"type": "string", "description": "Message content (max 4000 characters)"},
            "message_type": {"type": "string", "description": "Type of message", "enum": ["text", "assistance_request", "task_update", "notification", "stop_command"], "default": "text"},
            "priority": {"type": "string", "description": "Message priority", "enum": ["low", "normal", "high", "urgent"], "default": "normal"},
            "deliver_method": {"type": "string", "description": "Vestigial since Wave 7 (coordinator transition). Every message is stored in the DB and surfaced via wait_for_events / get_agent_messages -- agent-mcp no longer pushes to a tmux session. Accepted for back-compat; the value is ignored.", "enum": ["tmux", "store", "both"], "default": "store"},
            "subject": {"type": ["string", "null"], "description": "Optional one-line subject for root messages. For a reply, use parent_message_id rather than an 'RE:' subject -- replies are threaded, not subject-bearing (subject is ignored / forced NULL when parent_message_id is set)."},
            "parent_message_id": {"type": ["string", "null"], "description": "How you reply / thread a message: set this to the message_id you are replying to and this message is linked as a reply under that thread. Prefer this over typing 'RE:' into the subject. Replies always have subject = NULL (the thread shows the root's subject as the conversation title)."}
        },
        "required": ["recipient_id", "message"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let principal =
                principal.expect("Requirement::Predicate already enforced Some(principal)");

            let recipient_id = arguments.get("recipient_id").and_then(Value::as_str);
            let message_content = arguments.get("message").and_then(Value::as_str);
            let message_type = arguments
                .get("message_type")
                .and_then(Value::as_str)
                .unwrap_or("text");
            let priority = arguments
                .get("priority")
                .and_then(Value::as_str)
                .unwrap_or("normal");
            let explicit_subject = arguments.get("subject").and_then(Value::as_str);
            let parent_message_id = arguments.get("parent_message_id").and_then(Value::as_str);

            let Some(recipient_id) = recipient_id.filter(|s| !s.is_empty()) else {
                return ToolResult::Invalid {
                    field: Some("recipient_id".to_string()),
                    message: "recipient_id is required".to_string(),
                };
            };
            let Some(message_content) = message_content.filter(|s| !s.is_empty()) else {
                return ToolResult::Invalid {
                    field: Some("message".to_string()),
                    message: "message is required".to_string(),
                };
            };

            let outcome = send_with_side_effects(
                conn,
                principal,
                crate::agent_messaging::SendMessageArgs {
                    recipient_id,
                    message_content,
                    message_type,
                    priority,
                    subject: explicit_subject,
                    parent_message_id,
                    now,
                },
                ctx,
                now,
            )
            .await;
            let outcome = match outcome {
                Ok(o) => o,
                Err(e) => {
                    return ToolResult::Failed {
                        message: format!("Database error sending message: {e}"),
                    }
                }
            };

            let message_id = match outcome {
                crate::agent_messaging::SendOutcome::Denied(denial) => return denial,
                crate::agent_messaging::SendOutcome::RecipientNotFound(_) => {
                    return ToolResult::NotFound {
                        resource: "agent".to_string(),
                        identifier: recipient_id.to_string(),
                        hint: None,
                    };
                }
                crate::agent_messaging::SendOutcome::ParentMessageNotFound(id) => {
                    return ToolResult::NotFound {
                        resource: "parent message".to_string(),
                        identifier: id,
                        hint: None,
                    };
                }
                crate::agent_messaging::SendOutcome::Sent { message_id } => message_id,
            };

            let mut response_text = format!(
                "Message sent to {recipient_id}. Message stored for recipient (Message ID: \
                 {message_id})"
            );

            let (display_subject, subject_is_placeholder) = if parent_message_id.is_some() {
                (None, false)
            } else {
                conexus_db::message_repository::message_subject_view(
                    explicit_subject,
                    message_content,
                )
            };

            let mut data = serde_json::json!({
                "message_id": message_id,
                "sender": crate::agent_messaging::sender_label(principal),
                "recipient_id": recipient_id,
                "message_type": message_type,
                "priority": priority,
                "delivery_status": "stored",
                "subject": display_subject,
                "subject_is_placeholder": subject_is_placeholder,
                "parent_message_id": parent_message_id,
            });

            // Reply-nudge (advisory only): an EXPLICIT subject that
            // looks like a reply ("RE:", case-/whitespace-insensitive)
            // on a message that is NOT already a reply gets a gentle
            // hint toward parent_message_id threading. The send already
            // succeeded; this never blocks or rewrites it.
            if parent_message_id.is_none() {
                if let Some(subj) = explicit_subject {
                    if re_subject_regex().is_match(subj) {
                        data["reply_hint"] = serde_json::json!(REPLY_HINT_TEXT);
                        response_text.push(' ');
                        response_text.push_str(REPLY_HINT_TEXT);
                    }
                }
            }

            ToolResult::Ok {
                data: Some(data),
                message: Some(response_text),
            }
        })
    }
}

const BROADCAST_DENIED: &str = "Unauthorized: operator role required to broadcast";

/// Port of the decorator's `lambda p: p is not None and
/// _is_operator_tier(p)`.
fn is_operator_tier_caller(principal: Option<&Principal>) -> bool {
    principal.is_some_and(conexus_core::principal::is_operator_tier)
}

/// Admin-only fan-out: broadcast a message to every active agent.
/// Port of `broadcast_admin_message_tool_impl`.
///
/// Each recipient goes through the SAME [`send_with_side_effects`]
/// helper `SendAgentMessageTool` uses (not just the write core) --
/// confirmed by reading Python's own fan-out, which calls the FULL
/// `send_agent_message_tool_impl` per recipient, not a bare write
/// function. Each successful send therefore gets its own individual
/// durable `agent_actions` "send_message" row AND its own recipient
/// wake, exactly as if that recipient had been messaged directly.
/// The broadcast's own top-level `log_audit(..., "broadcast_message",
/// ...)` call is in-memory-only in Python and deliberately NOT
/// ported, same precedent as every prior tool's dropped `g.audit_log`
/// trail.
///
/// `g.active_agents` (Python's in-memory cache) has no Rust
/// equivalent -- re-derived from `AgentRepository::list_active`
/// (DB-fresh), matching every prior "which agents are active" query
/// this migration has ported. Additionally excludes `status ==
/// "system"` (the synthetic system pseudo-agent `list_active` alone
/// doesn't filter, but which a real `g.active_agents` snapshot would
/// never contain either, since only `register_agent` populates it --
/// the same re-derivation `ViewStatusTool`/`ViewAgentsTool` already
/// made). `agent_id != "admin"` is deliberately an EXACT, case-
/// SENSITIVE match here -- Python's own `recipient_id != "admin"` in
/// this function, NOT the case-insensitive `.lower() == "admin"`
/// `can_agents_communicate` uses elsewhere; preserved as a real,
/// documented Python asymmetry, not reconciled.
pub struct BroadcastAdminMessageTool;

impl conexus_auth::Tool for BroadcastAdminMessageTool {
    const NAME: &'static str = "broadcast_admin_message";
    const REQUIRED: conexus_auth::Requirement = conexus_auth::Requirement::Predicate {
        check: is_operator_tier_caller,
        reason: BROADCAST_DENIED,
    };
    const DESCRIPTION: &'static str =
        "Admin-only tool to broadcast a message to all active agents.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Message content to broadcast", "maxLength": 4000},
            "message_type": {"type": "string", "description": "Type of broadcast message", "enum": ["broadcast", "announcement", "system_alert"], "default": "broadcast"},
            "priority": {"type": "string", "description": "Message priority", "enum": ["low", "normal", "high", "urgent"], "default": "high"},
            "subject": {"type": ["string", "null"], "description": "Optional subject applied to every fan-out root message. Omit to let each per-recipient send go through the standard truncated-body preview path."}
        },
        "required": ["message"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let principal =
                principal.expect("Requirement::Predicate already enforced Some(principal)");

            let message_content = arguments.get("message").and_then(Value::as_str);
            let message_type = arguments
                .get("message_type")
                .and_then(Value::as_str)
                .unwrap_or("broadcast");
            let priority = arguments
                .get("priority")
                .and_then(Value::as_str)
                .unwrap_or("high");
            let explicit_subject = arguments.get("subject").and_then(Value::as_str);

            let Some(message_content) = message_content.filter(|s| !s.is_empty()) else {
                return ToolResult::Invalid {
                    field: Some("message".to_string()),
                    message: "message is required".to_string(),
                };
            };

            let active_agents = {
                let guard = conn.lock().await;
                AgentRepository::list_active(&guard)
            };
            let active_agents = match active_agents {
                Ok(rows) => rows,
                Err(e) => {
                    return ToolResult::Failed {
                        message: format!("Database error listing active agents: {e}"),
                    }
                }
            };

            let recipient_ids: Vec<String> = active_agents
                .into_iter()
                .filter(|row| row.status != "system" && row.agent_id != "admin")
                .map(|row| row.agent_id)
                .collect();

            if recipient_ids.is_empty() {
                return ToolResult::Ok {
                    data: Some(serde_json::json!({
                        "sent_count": 0, "failed_count": 0, "recipients": []
                    })),
                    message: Some("No active agents to broadcast to".to_string()),
                };
            }

            let mut sent_count = 0i64;
            let mut failed_count = 0i64;
            let mut recipients: Vec<String> = Vec::new();

            for recipient_id in &recipient_ids {
                let outcome = send_with_side_effects(
                    conn,
                    principal,
                    crate::agent_messaging::SendMessageArgs {
                        recipient_id,
                        message_content,
                        message_type,
                        priority,
                        subject: explicit_subject,
                        parent_message_id: None,
                        now,
                    },
                    ctx,
                    now,
                )
                .await;
                match outcome {
                    Ok(crate::agent_messaging::SendOutcome::Sent { .. }) => {
                        sent_count += 1;
                        recipients.push(recipient_id.clone());
                    }
                    _ => failed_count += 1,
                }
            }

            ToolResult::Ok {
                data: Some(serde_json::json!({
                    "sent_count": sent_count,
                    "failed_count": failed_count,
                    "recipients": recipients,
                    "message_type": message_type,
                    "priority": priority,
                })),
                message: Some(format!(
                    "Broadcast sent to {sent_count} agents. {failed_count} failed."
                )),
            }
        })
    }
}

const READ_MESSAGES_DENIED: &str =
    "Unauthorized: Valid agent token with the messages.view capability required to retrieve \
     messages";

/// `_is_identified_agent` AND `messages.view`. Port of
/// `_can_read_own_messages` -- composes the two in-body gates Python's
/// `get_agent_messages_tool_impl` ran in order: the identity check,
/// then the cap check that closes the empty-bundle-bearer class (an
/// `agent_bearer` with `agent_role: None` named an `agent_id` yet held
/// zero caps).
fn can_read_own_messages(principal: Option<&Principal>) -> bool {
    is_identified_agent(principal)
        && principal
            .is_some_and(|p| p.has_capability(conexus_core::capability::Capability::MessagesView))
}

const PRIORITY_ICON_DEFAULT: &str = "\u{26aa}"; // ⚪

fn priority_icon(priority: &str) -> &'static str {
    match priority {
        "low" => "\u{1f535}",    // 🔵
        "normal" => "\u{26aa}",  // ⚪
        "high" => "\u{1f7e1}",   // 🟡
        "urgent" => "\u{1f534}", // 🔴
        _ => PRIORITY_ICON_DEFAULT,
    }
}

/// Retrieve messages for the calling agent (sent and/or received),
/// marking received ones read by default. Port of
/// `get_agent_messages_tool_impl`.
pub struct GetAgentMessagesTool;

impl conexus_auth::Tool for GetAgentMessagesTool {
    const NAME: &'static str = "get_agent_messages";
    const REQUIRED: conexus_auth::Requirement = conexus_auth::Requirement::Predicate {
        check: can_read_own_messages,
        reason: READ_MESSAGES_DENIED,
    };
    const DESCRIPTION: &'static str = "Retrieve messages for the current agent.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "include_sent": {"type": "boolean", "description": "Include messages sent by this agent", "default": false},
            "include_received": {"type": "boolean", "description": "Include messages received by this agent", "default": true},
            "mark_as_read": {"type": "boolean", "description": "Mark retrieved messages as read", "default": true},
            "limit": {"type": "integer", "description": "Maximum number of messages to retrieve", "default": 20, "minimum": 1, "maximum": 100},
            "message_type": {"type": "string", "description": "Filter by message type", "enum": ["text", "assistance_request", "task_update", "notification", "stop_command"]},
            "unread_only": {"type": "boolean", "description": "Only show unread messages", "default": false}
        },
        "required": [],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        _now: &'a str,
        _ctx: &'a ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let principal =
                principal.expect("Requirement::Predicate already enforced Some(principal)");
            let agent_id = principal
                .agent_id
                .as_deref()
                .expect("can_read_own_messages already enforced an identified agent");

            let include_sent = arguments
                .get("include_sent")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let include_received = arguments
                .get("include_received")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let mark_as_read = arguments
                .get("mark_as_read")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let limit = match arguments.get("limit").and_then(Value::as_i64) {
                Some(n) if (1..=100).contains(&n) => n,
                _ => 20,
            };
            let message_type = arguments.get("message_type").and_then(Value::as_str);
            let unread_only = arguments
                .get("unread_only")
                .and_then(Value::as_bool)
                .unwrap_or(false);

            if !include_sent && !include_received {
                return ToolResult::Invalid {
                    field: None,
                    message: "Must include sent or received messages".to_string(),
                };
            }

            let guard = conn.lock().await;
            let messages = match conexus_db::message_repository::list_recent_for_agent(
                &guard,
                agent_id,
                &conexus_db::message_repository::RecentMessagesFilters {
                    include_sent,
                    include_received,
                    message_type,
                    unread_only,
                    limit,
                },
            ) {
                Ok(rows) => rows,
                Err(e) => {
                    return ToolResult::Failed {
                        message: format!("Database error retrieving messages: {e}"),
                    }
                }
            };

            if mark_as_read && include_received {
                let received_ids: Vec<&str> = messages
                    .iter()
                    .filter(|m| m.recipient_id == agent_id && !m.read)
                    .map(|m| m.message_id.as_str())
                    .collect();
                if !received_ids.is_empty() {
                    let _ = conexus_db::message_repository::mark_read_by_ids(
                        &guard,
                        &received_ids,
                        Some(agent_id),
                    );
                }
            }
            drop(guard);

            if messages.is_empty() {
                return ToolResult::Ok {
                    data: Some(serde_json::json!({
                        "agent_id": agent_id, "messages": [], "count": 0
                    })),
                    message: Some("No messages found".to_string()),
                };
            }

            let mut response_lines = vec![format!(
                "Messages for {agent_id} (showing {} of max {limit}):",
                messages.len()
            )];
            response_lines.push(String::new());

            let mut rows_for_payload = Vec::with_capacity(messages.len());
            for msg in &messages {
                let direction = if msg.sender_id == agent_id {
                    "\u{27a1}\u{fe0f}" // ➡️
                } else {
                    "\u{2b05}\u{fe0f}" // ⬅️
                };
                let other_agent = if msg.sender_id == agent_id {
                    &msg.recipient_id
                } else {
                    &msg.sender_id
                };
                let read_status = if msg.read { "\u{1f4d6}" } else { "\u{1f4e9}" }; // 📖 / 📩
                let icon = priority_icon(&msg.priority);

                response_lines.push(format!(
                    "{direction} {read_status} {icon} [{}] {other_agent}",
                    msg.message_type
                ));
                response_lines.push(format!("   {}", msg.timestamp));

                let (display_subject, subject_is_placeholder) = if msg.parent_message_id.is_some() {
                    (None, false)
                } else {
                    conexus_db::message_repository::message_subject_view(
                        msg.subject.as_deref(),
                        &msg.message_content,
                    )
                };
                match (&display_subject, subject_is_placeholder) {
                    (Some(s), false) => response_lines.push(format!("   Subject: {s}")),
                    (Some(s), true) => response_lines.push(format!("   Subject (auto): {s}")),
                    (None, _) => {
                        if let Some(parent_id) = &msg.parent_message_id {
                            response_lines.push(format!("   \u{21b3} reply to: {parent_id}"));
                        }
                    }
                }
                response_lines.push(format!("   {}", msg.message_content));
                response_lines.push(String::new());

                rows_for_payload.push(serde_json::json!({
                    "message_id": msg.message_id,
                    "sender_id": msg.sender_id,
                    "recipient_id": msg.recipient_id,
                    "message_content": msg.message_content,
                    "message_type": msg.message_type,
                    "priority": msg.priority,
                    "timestamp": msg.timestamp,
                    "delivered": msg.delivered,
                    "read": msg.read,
                    "subject": display_subject,
                    "subject_is_placeholder": subject_is_placeholder,
                    "parent_message_id": msg.parent_message_id,
                }));
            }

            // Deliberately NOT ported: `log_audit`'s in-memory trail --
            // same precedent as every prior tool port this migration
            // (no Rust reader for it, and no durable `agent_actions`
            // row exists here either since Python's own call writes
            // only to the transient trail).
            ToolResult::Ok {
                data: Some(serde_json::json!({
                    "agent_id": agent_id,
                    "count": rows_for_payload.len(),
                    "messages": rows_for_payload,
                })),
                message: Some(response_lines.join("\n")),
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_core::capability::Capabilities;
    use conexus_core::principal::PrincipalKind;
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::file_map::FileMap;
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
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

    // -- wait_for_events_slow_path -----------------------------------------
    //
    // Only the branches that depend solely on `Instant`-based deadlines --
    // see the module doc's "clock-anchoring... deliberately dropped" note
    // for why the wall-clock-dependent branches (idle-stop/reminder/
    // schedule-due firing INSIDE the loop) aren't exercised here.
    //
    // Pattern: no `tokio::spawn`/manual `tokio::time::advance()` needed.
    // `#[tokio::test(start_paused = true)]` auto-fast-forwards a paused
    // clock to the next pending timer whenever the single test task itself
    // has nothing else runnable -- so simply `.await`-ing the slow path
    // directly, with any wake signal PRE-QUEUED on the channel before the
    // call (a bounded mpsc `send` succeeds immediately, well before the
    // receiver side ever polls), reproduces "the signal was already
    // sitting there when we started waiting" without any real concurrency.

    struct FakeSink {
        calls: std::sync::atomic::AtomicU32,
        succeeds: bool,
    }

    impl conexus_auth::ProgressSink for FakeSink {
        fn notify_progress<'a>(&'a self, _progress: f64) -> conexus_auth::BoxFuture<'a, bool> {
            Box::pin(async move {
                self.calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                self.succeeds
            })
        }
    }

    async fn enter_slow_path(
        conn: &AsyncMutex<Connection>,
        ctx: &ToolCallContext<'_>,
        agent_id: &str,
        arguments: &Value,
    ) -> SlowPathSetup {
        let principal = agent_bearer(agent_id);
        // Real `now`, not the fixed `NOW` constant the entry-only tests
        // use -- the slow path's own `now_iso()` reads the real wall
        // clock too, and this helper must not manufacture the kind of
        // huge entry-vs-loop clock mismatch that would never occur in a
        // real `Tool::call` invocation (which always passes a freshly
        // computed `now`).
        match wait_for_events_entry(&principal, arguments, conn, &now_iso(), ctx).await {
            EntryOutcome::EnterSlowPath(setup) => setup,
            EntryOutcome::Done(result) => panic!("expected the slow path, got {result:?}"),
        }
    }

    #[tokio::test(start_paused = true)]
    async fn hold_deadline_reached_returns_empty_envelope_and_unregisters() {
        let conn = test_conn();
        seed_agent(&conn, "bob").await;
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let setup = enter_slow_path(&conn, &ctx, "bob", &json!({"timeout_seconds": 5})).await;

        let result = wait_for_events_slow_path(setup, &conn, &ctx).await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok");
        };
        assert!(data.unwrap()["events"].as_array().unwrap().is_empty());
        assert_eq!(registry.waiter_count("bob"), 0);
    }

    #[tokio::test(start_paused = true)]
    async fn wake_signal_delivers_pending_events() {
        let conn = test_conn();
        seed_agent(&conn, "carol").await;
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        // Register with NO backlog yet, so entry takes the slow path
        // rather than the fast path finding this message immediately.
        let setup = enter_slow_path(&conn, &ctx, "carol", &json!({})).await;
        {
            let guard = conn.lock().await;
            conexus_db::message_repository::send(
                &guard,
                conexus_db::message_repository::NewMessage {
                    message_id: "m1",
                    sender_id: "dave",
                    recipient_id: "carol",
                    message_content: "hi",
                    message_type: "text",
                    priority: "normal",
                    timestamp: &now_iso(),
                    delivered: true,
                    read: false,
                    subject: Some("hello"),
                    parent_message_id: None,
                },
            )
            .unwrap();
        }
        setup.sender.send(WakeSignal::Wake).await.unwrap();

        let result = wait_for_events_slow_path(setup, &conn, &ctx).await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok");
        };
        assert_eq!(data.unwrap()["events"].as_array().unwrap().len(), 1);
        assert_eq!(registry.waiter_count("carol"), 0);
    }

    #[tokio::test(start_paused = true)]
    async fn superseded_signal_returns_connection_superseded_event() {
        let conn = test_conn();
        seed_agent(&conn, "erin").await;
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let setup = enter_slow_path(&conn, &ctx, "erin", &json!({})).await;
        setup.sender.send(WakeSignal::Superseded).await.unwrap();

        let result = wait_for_events_slow_path(setup, &conn, &ctx).await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok");
        };
        let events = data.unwrap();
        let events = events["events"].as_array().unwrap();
        assert_eq!(events[0]["type"], "connection_superseded");
    }

    #[tokio::test(start_paused = true)]
    async fn heartbeat_is_sent_for_a_heartbeat_capable_client_then_the_hold_expires() {
        let conn = test_conn();
        seed_agent(&conn, "grace").await;
        let sink = FakeSink {
            calls: std::sync::atomic::AtomicU32::new(0),
            succeeds: true,
        };
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext {
            progress_token_present: true,
            client_name: Some("claude-code"),
            progress_sink: Some(&sink),
            waiter_registry: &registry,
            file_map: &file_map,
            project_dir: std::path::Path::new("/tmp"),
        };
        // Just over one HEARTBEAT_INTERVAL_SECONDS (25s) so exactly one
        // heartbeat fires before the hold itself expires.
        let setup = enter_slow_path(&conn, &ctx, "grace", &json!({"timeout_seconds": 26})).await;
        assert!(setup.heartbeat_enabled);

        let result = wait_for_events_slow_path(setup, &conn, &ctx).await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        assert_eq!(sink.calls.load(std::sync::atomic::Ordering::SeqCst), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn heartbeat_misses_reap_the_connection_after_max_misses() {
        let conn = test_conn();
        seed_agent(&conn, "henry").await;
        let sink = FakeSink {
            calls: std::sync::atomic::AtomicU32::new(0),
            succeeds: false, // every heartbeat send "fails"
        };
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext {
            progress_token_present: true,
            client_name: Some("claude-code"),
            progress_sink: Some(&sink),
            waiter_registry: &registry,
            file_map: &file_map,
            project_dir: std::path::Path::new("/tmp"),
        };
        // Long enough hold that reaping (after MAX_HEARTBEAT_MISSES misses,
        // one per 25s) must happen well before the hold itself would.
        let setup = enter_slow_path(&conn, &ctx, "henry", &json!({"timeout_seconds": 3600})).await;

        let result = wait_for_events_slow_path(setup, &conn, &ctx).await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok");
        };
        assert!(data.unwrap()["events"].as_array().unwrap().is_empty());
        assert_eq!(
            sink.calls.load(std::sync::atomic::Ordering::SeqCst),
            MAX_HEARTBEAT_MISSES
        );
        assert_eq!(registry.waiter_count("henry"), 0);
    }

    #[tokio::test(start_paused = true)]
    async fn ladder_advisory_appears_after_enough_empty_short_polls() {
        let conn = test_conn();
        seed_agent(&conn, "ivy").await;
        hold_ladder::clear();
        // Seed the run counter to one below ADVISE_AFTER so THIS call's
        // own empty timeout tips it into the advise band.
        for _ in 0..(hold_ladder::ADVISE_AFTER - 1) {
            hold_ladder::note_empty_short_poll("ivy");
        }
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext {
            progress_token_present: true,
            client_name: Some("claude-code"),
            progress_sink: None,
            waiter_registry: &registry,
            file_map: &file_map,
            project_dir: std::path::Path::new("/tmp"),
        };
        // heartbeat=true, progress_token_present=true, requested < base_hold
        // -> ladder_eligible; not yet at OVERRIDE_AFTER so no override.
        let setup = enter_slow_path(&conn, &ctx, "ivy", &json!({"timeout_seconds": 5})).await;
        assert!(setup.ladder_eligible);
        assert!(!setup.ladder_override);

        let result = wait_for_events_slow_path(setup, &conn, &ctx).await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok");
        };
        let events = data.unwrap();
        let events = events["events"].as_array().unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["type"], "hold_advisory");
        hold_ladder::clear();
    }

    // -- is_identified_agent --------------------------------------------

    #[test]
    fn is_identified_agent_admits_an_agent_bearer_with_an_id() {
        assert!(is_identified_agent(Some(&agent_bearer("alice"))));
    }

    #[test]
    fn is_identified_agent_denies_a_missing_principal() {
        assert!(!is_identified_agent(None));
    }

    #[test]
    fn is_identified_agent_denies_a_principal_with_no_agent_id() {
        let mut operator = agent_bearer("alice");
        operator.agent_id = None;
        assert!(!is_identified_agent(Some(&operator)));
    }

    // -- WaitForEventsTool (through the real Tool trait) ---------------------

    #[tokio::test(start_paused = true)]
    async fn wait_for_events_tool_delivers_a_fast_path_event_through_the_real_trait_call() {
        use conexus_auth::Tool;

        let conn = test_conn();
        seed_agent(&conn, "kate").await;
        send_message(&conn, "m1", "kate", &now_iso(), "text").await;
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let principal = agent_bearer("kate");

        let result =
            WaitForEventsTool::call(Some(&principal), &json!({}), &conn, &now_iso(), &ctx).await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["events"].as_array().unwrap().len(), 1);
    }

    async fn send_message(
        conn: &AsyncMutex<Connection>,
        message_id: &str,
        recipient_id: &str,
        timestamp: &str,
        message_type: &str,
    ) {
        let guard = conn.lock().await;
        conexus_db::message_repository::send(
            &guard,
            conexus_db::message_repository::NewMessage {
                message_id,
                sender_id: "sender",
                recipient_id,
                message_content: "hello",
                message_type,
                priority: "normal",
                timestamp,
                delivered: true,
                read: false,
                subject: Some("a subject"),
                parent_message_id: None,
            },
        )
        .unwrap();
    }

    // -- FetchEventsSinceTool ----------------------------------------------

    #[tokio::test]
    async fn fetch_events_since_response_uses_the_cursor_key_not_next_cursor() {
        use conexus_auth::Tool;

        let conn = test_conn();
        seed_agent(&conn, "leo").await;
        send_message(&conn, "m1", "leo", &now_iso(), "text").await;
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let principal = agent_bearer("leo");

        let result =
            FetchEventsSinceTool::call(Some(&principal), &json!({}), &conn, &now_iso(), &ctx).await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert!(data.get("cursor").is_some());
        assert!(data.get("next_cursor").is_none());
        assert_eq!(data["events"].as_array().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn fetch_events_since_rejects_a_non_string_cursor() {
        use conexus_auth::Tool;

        let conn = test_conn();
        seed_agent(&conn, "mia").await;
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let principal = agent_bearer("mia");

        let result = FetchEventsSinceTool::call(
            Some(&principal),
            &json!({"cursor": 12345}),
            &conn,
            &now_iso(),
            &ctx,
        )
        .await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("cursor"))
        );
    }

    #[tokio::test]
    async fn fetch_events_since_never_blocks_and_never_registers_a_waiter() {
        use conexus_auth::Tool;

        let conn = test_conn();
        seed_agent(&conn, "nora").await;
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let principal = agent_bearer("nora");

        let result =
            FetchEventsSinceTool::call(Some(&principal), &json!({}), &conn, &now_iso(), &ctx).await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        assert_eq!(registry.waiter_count("nora"), 0);
    }

    #[tokio::test]
    async fn dispatch_denies_wait_for_events_for_a_non_agent_principal() {
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let mut operator = agent_bearer("alice");
        operator.agent_id = None;
        let descriptor = conexus_auth::ToolDescriptor::of::<WaitForEventsTool>();
        let result = conexus_auth::dispatch(
            &descriptor,
            Some(&operator),
            &conexus_auth::NoPolicyOverrides,
            &json!({}),
            &conn,
            &now_iso(),
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::PermissionDenied { .. }));
    }

    // -- get_agent_messages -------------------------------------------

    fn agent_bearer_with_messages_view(agent_id: &str) -> Principal {
        let mut p = agent_bearer(agent_id);
        p.capabilities =
            Capabilities::from_iter([conexus_core::capability::Capability::MessagesView]);
        p
    }

    #[test]
    fn can_read_own_messages_denies_an_empty_bundle_bearer() {
        // The exact class Python's own docstring names: an agent_bearer
        // with agent_role None named an agent_id yet holding zero caps.
        let p = agent_bearer("alice");
        assert!(!can_read_own_messages(Some(&p)));
    }

    #[test]
    fn can_read_own_messages_admits_a_bearer_with_the_capability() {
        let p = agent_bearer_with_messages_view("alice");
        assert!(can_read_own_messages(Some(&p)));
    }

    #[test]
    fn can_read_own_messages_denies_no_principal() {
        assert!(!can_read_own_messages(None));
    }

    #[tokio::test]
    async fn get_agent_messages_requires_sent_or_received() {
        use conexus_auth::Tool;
        let conn = test_conn();
        seed_agent(&conn, "bob").await;
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let principal = agent_bearer_with_messages_view("bob");

        let result = GetAgentMessagesTool::call(
            Some(&principal),
            &json!({"include_sent": false, "include_received": false}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn get_agent_messages_returns_no_messages_found_when_empty() {
        use conexus_auth::Tool;
        let conn = test_conn();
        seed_agent(&conn, "bob").await;
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let principal = agent_bearer_with_messages_view("bob");

        let result =
            GetAgentMessagesTool::call(Some(&principal), &json!({}), &conn, NOW, &ctx).await;
        let ToolResult::Ok { message, data } = result else {
            panic!("expected Ok, got a denial/error");
        };
        assert_eq!(message.as_deref(), Some("No messages found"));
        assert_eq!(data.unwrap()["count"], 0);
    }

    #[tokio::test]
    async fn get_agent_messages_returns_and_marks_received_messages_read() {
        use conexus_auth::Tool;
        let conn = test_conn();
        seed_agent(&conn, "alice").await;
        seed_agent(&conn, "bob").await;
        {
            let guard = conn.lock().await;
            conexus_db::message_repository::send(
                &guard,
                conexus_db::message_repository::NewMessage {
                    message_id: "m1",
                    sender_id: "alice",
                    recipient_id: "bob",
                    message_content: "hi bob",
                    message_type: "text",
                    priority: "normal",
                    timestamp: "2026-01-01T00:00:00Z",
                    delivered: false,
                    read: false,
                    subject: None,
                    parent_message_id: None,
                },
            )
            .unwrap();
        }
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let principal = agent_bearer_with_messages_view("bob");

        let result =
            GetAgentMessagesTool::call(Some(&principal), &json!({}), &conn, NOW, &ctx).await;
        let ToolResult::Ok { message, data } = result else {
            panic!("expected Ok, got a denial/error");
        };
        let data = data.unwrap();
        assert_eq!(data["count"], 1);
        assert_eq!(data["messages"][0]["message_id"], "m1");
        assert!(message.unwrap().contains("hi bob"));

        // Read status must actually be persisted, not just reflected
        // in the response.
        let guard = conn.lock().await;
        let row = conexus_db::message_repository::get_by_id(&guard, "m1")
            .unwrap()
            .unwrap();
        assert!(
            row.read,
            "get_agent_messages must mark received messages read by default"
        );
    }

    #[tokio::test]
    async fn get_agent_messages_mark_as_read_false_leaves_messages_unread() {
        use conexus_auth::Tool;
        let conn = test_conn();
        seed_agent(&conn, "alice").await;
        seed_agent(&conn, "bob").await;
        {
            let guard = conn.lock().await;
            conexus_db::message_repository::send(
                &guard,
                conexus_db::message_repository::NewMessage {
                    message_id: "m1",
                    sender_id: "alice",
                    recipient_id: "bob",
                    message_content: "hi bob",
                    message_type: "text",
                    priority: "normal",
                    timestamp: "2026-01-01T00:00:00Z",
                    delivered: false,
                    read: false,
                    subject: None,
                    parent_message_id: None,
                },
            )
            .unwrap();
        }
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let principal = agent_bearer_with_messages_view("bob");

        let _ = GetAgentMessagesTool::call(
            Some(&principal),
            &json!({"mark_as_read": false}),
            &conn,
            NOW,
            &ctx,
        )
        .await;

        let guard = conn.lock().await;
        let row = conexus_db::message_repository::get_by_id(&guard, "m1")
            .unwrap()
            .unwrap();
        assert!(!row.read, "mark_as_read=false must not flip the row");
    }

    #[tokio::test]
    async fn get_agent_messages_denies_a_worker_with_no_messages_view_capability() {
        let conn = test_conn();
        seed_agent(&conn, "bob").await;
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let principal = agent_bearer("bob"); // no messages.view capability
        let descriptor = conexus_auth::ToolDescriptor::of::<GetAgentMessagesTool>();

        let result = conexus_auth::dispatch(
            &descriptor,
            Some(&principal),
            &conexus_auth::NoPolicyOverrides,
            &json!({}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::PermissionDenied { .. }));
    }

    // -- send_agent_message -------------------------------------------

    #[tokio::test]
    async fn send_agent_message_requires_recipient_id() {
        use conexus_auth::Tool;
        let conn = test_conn();
        seed_agent(&conn, "alice").await;
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let principal = agent_bearer("alice");

        let result = SendAgentMessageTool::call(
            Some(&principal),
            &json!({"message": "hi"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("recipient_id"))
        );
    }

    #[tokio::test]
    async fn send_agent_message_requires_message() {
        use conexus_auth::Tool;
        let conn = test_conn();
        seed_agent(&conn, "alice").await;
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let principal = agent_bearer("alice");

        let result = SendAgentMessageTool::call(
            Some(&principal),
            &json!({"recipient_id": "admin"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("message"))
        );
    }

    #[tokio::test]
    async fn send_agent_message_succeeds_and_wakes_the_recipients_waiter() {
        use conexus_auth::Tool;
        let conn = test_conn();
        seed_agent(&conn, "alice").await;
        seed_agent(&conn, "bob").await;
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let principal = agent_bearer("alice");
        let (_sender, mut receiver) = registry.register("bob");

        let result = SendAgentMessageTool::call(
            Some(&principal),
            &json!({"recipient_id": "bob", "message": "hello bob"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let ToolResult::Ok { message, data } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert!(message.unwrap().contains("Message sent to bob"));
        assert_eq!(data.unwrap()["recipient_id"], "bob");

        // The recipient's waiter must have been woken post-commit.
        assert!(
            receiver.try_recv().is_ok(),
            "bob's wait_for_events waiter must be notified"
        );

        let guard = conn.lock().await;
        let count: i64 = guard
            .query_row("SELECT COUNT(*) FROM agent_messages", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 1);

        // Durable audit row confirmed.
        let action_count: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM agent_actions WHERE action_type = 'send_message'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(action_count, 1);
    }

    #[tokio::test]
    async fn send_agent_message_denied_by_permission_check_returns_the_denial_verbatim() {
        use conexus_auth::Tool;
        let conn = test_conn();
        seed_agent(&conn, "alice").await;
        seed_agent(&conn, "bob").await;
        {
            let guard = conn.lock().await;
            project_settings_repository::upsert(
                &guard,
                "config_allow_worker_to_worker",
                "false",
                None,
                false,
                "test",
                NOW,
            )
            .unwrap();
        }
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let principal = agent_bearer("alice");

        let result = SendAgentMessageTool::call(
            Some(&principal),
            &json!({"recipient_id": "bob", "message": "hello bob"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::PermissionDenied { .. }));
        assert_eq!(registry.waiter_count("bob"), 0);
    }

    #[tokio::test]
    async fn send_agent_message_unknown_recipient_is_not_found() {
        use conexus_auth::Tool;
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        // Admin sender bypasses the active-recipient check, reaching the
        // repository's own unknown-recipient rejection.
        let mut admin = agent_bearer("admin");
        admin.capabilities = Capabilities::from_iter([]);

        let result = SendAgentMessageTool::call(
            Some(&admin),
            &json!({"recipient_id": "ghost", "message": "hi"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(
            matches!(result, ToolResult::NotFound { resource, identifier, .. } if resource == "agent" && identifier == "ghost")
        );
    }

    #[tokio::test]
    async fn send_agent_message_reply_nudge_fires_only_on_a_re_subject_that_is_not_already_a_reply()
    {
        use conexus_auth::Tool;
        let conn = test_conn();
        seed_agent(&conn, "alice").await;
        seed_agent(&conn, "bob").await;
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let principal = agent_bearer("alice");

        let result = SendAgentMessageTool::call(
            Some(&principal),
            &json!({"recipient_id": "bob", "message": "following up", "subject": "RE: last week"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let ToolResult::Ok { message, data } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert!(message.unwrap().contains("parent_message_id"));
        assert!(data.unwrap()["reply_hint"].is_string());
    }

    #[tokio::test]
    async fn send_agent_message_a_reply_never_gets_the_reply_nudge_even_with_an_re_subject() {
        use conexus_auth::Tool;
        let conn = test_conn();
        seed_agent(&conn, "alice").await;
        seed_agent(&conn, "bob").await;
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let principal = agent_bearer("alice");

        let root = SendAgentMessageTool::call(
            Some(&principal),
            &json!({"recipient_id": "bob", "message": "root"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let ToolResult::Ok { data, .. } = root else {
            panic!("expected Ok");
        };
        let root_id = data.unwrap()["message_id"].as_str().unwrap().to_string();

        let reply = SendAgentMessageTool::call(
            Some(&principal),
            &json!({
                "recipient_id": "bob",
                "message": "a reply",
                "subject": "RE: doesn't matter, replies force subject null",
                "parent_message_id": root_id,
            }),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let ToolResult::Ok { data, .. } = reply else {
            panic!("expected Ok");
        };
        let data = data.unwrap();
        assert!(data.get("reply_hint").is_none());
        assert_eq!(data["subject"], serde_json::Value::Null);
    }

    // -- broadcast_admin_message ---------------------------------------

    fn operator_principal() -> Principal {
        Principal {
            kind: PrincipalKind::ForwardingHeader,
            user_id: Some("op1".to_string()),
            agent_id: None,
            project_name: None,
            project_role: Some(conexus_core::capability::ProjectRole::Operator),
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Sysadmin,
        }
    }

    #[tokio::test]
    async fn broadcast_admin_message_denies_a_plain_worker() {
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let worker = agent_bearer("alice");
        let descriptor = conexus_auth::ToolDescriptor::of::<BroadcastAdminMessageTool>();

        let result = conexus_auth::dispatch(
            &descriptor,
            Some(&worker),
            &conexus_auth::NoPolicyOverrides,
            &json!({"message": "hi everyone"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::PermissionDenied { .. }));
    }

    #[tokio::test]
    async fn broadcast_admin_message_requires_message() {
        use conexus_auth::Tool;
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let operator = operator_principal();

        let result =
            BroadcastAdminMessageTool::call(Some(&operator), &json!({}), &conn, NOW, &ctx).await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("message"))
        );
    }

    #[tokio::test]
    async fn broadcast_admin_message_with_no_active_agents_reports_zero_sent() {
        use conexus_auth::Tool;
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let operator = operator_principal();

        let result = BroadcastAdminMessageTool::call(
            Some(&operator),
            &json!({"message": "hi everyone"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let ToolResult::Ok { message, data } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(message.as_deref(), Some("No active agents to broadcast to"));
        assert_eq!(data.unwrap()["sent_count"], 0);
    }

    #[tokio::test]
    async fn broadcast_admin_message_sends_to_every_active_agent_except_admin_and_wakes_each() {
        use conexus_auth::Tool;
        let conn = test_conn();
        seed_agent(&conn, "alice").await;
        seed_agent(&conn, "bob").await;
        {
            // "admin" can't go through `AgentRepository::create`
            // (rejected as a reserved agent_id) -- inserted via raw
            // SQL purely to exercise the exclusion defensively, the
            // same way Python's own comment notes "the legacy 'admin'
            // pseudo-agent label the test harness seeds" as a state
            // real production data can't otherwise reach.
            let guard = conn.lock().await;
            guard
                .execute(
                    "INSERT INTO agents (token, agent_id, created_at, status, working_directory, agent_role) \
                     VALUES ('tok-admin', 'admin', '2026-01-01T00:00:00Z', 'active', '/tmp', 'manager')",
                    [],
                )
                .unwrap();
        }
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let ctx = ToolCallContext::off_wire(&registry, &file_map, std::path::Path::new("/tmp"));
        let operator = operator_principal();
        let (_a_sender, mut a_receiver) = registry.register("alice");
        let (_b_sender, mut b_receiver) = registry.register("bob");

        let result = BroadcastAdminMessageTool::call(
            Some(&operator),
            &json!({"message": "system maintenance tonight"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let ToolResult::Ok { message, data } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["sent_count"], 2, "alice and bob, never admin itself");
        assert_eq!(data["failed_count"], 0);
        let recipients: Vec<String> = data["recipients"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        assert!(recipients.contains(&"alice".to_string()));
        assert!(recipients.contains(&"bob".to_string()));
        assert!(!recipients.contains(&"admin".to_string()));
        assert!(message.unwrap().contains("Broadcast sent to 2 agents"));

        assert!(a_receiver.try_recv().is_ok(), "alice must be woken");
        assert!(b_receiver.try_recv().is_ok(), "bob must be woken");

        // Each recipient gets its OWN durable "send_message" audit row
        // (Python's fan-out calls the full send tool impl per
        // recipient, not just a bare write) -- confirmed here, not
        // assumed.
        let guard = conn.lock().await;
        let action_count: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM agent_actions WHERE action_type = 'send_message'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(action_count, 2);
        let message_count: i64 = guard
            .query_row("SELECT COUNT(*) FROM agent_messages", [], |row| row.get(0))
            .unwrap();
        assert_eq!(message_count, 2);
    }
}
