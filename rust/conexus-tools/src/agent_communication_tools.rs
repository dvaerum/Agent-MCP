//! Port of `agent_mcp/tools/agent_communication_tools.py`'s
//! `wait_for_events_tool_impl`: the entry section
//! ([`wait_for_events_entry`], PR 2/4) and the slow-path loop
//! ([`wait_for_events_slow_path`], PR 3/4) -- the real `Tool` impl
//! wiring both together (plus registration in `all_tools()`) lands in
//! a later PR.
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
        let ctx = ToolCallContext::off_wire(&registry);
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
        let ctx = ToolCallContext::off_wire(&registry);
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
        let ctx = ToolCallContext::off_wire(&registry);
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
        let ctx = ToolCallContext {
            progress_token_present: true,
            client_name: Some("claude-code"),
            progress_sink: Some(&sink),
            waiter_registry: &registry,
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
        let ctx = ToolCallContext {
            progress_token_present: true,
            client_name: Some("claude-code"),
            progress_sink: Some(&sink),
            waiter_registry: &registry,
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
        let ctx = ToolCallContext {
            progress_token_present: true,
            client_name: Some("claude-code"),
            progress_sink: None,
            waiter_registry: &registry,
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
}
