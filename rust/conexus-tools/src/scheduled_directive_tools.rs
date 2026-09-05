//! Port of `agent_mcp/tools/scheduled_directive_tools.py` (Phase D5,
//! PR 7): the four `scheduled_directive` CRUD tools —
//! `create_scheduled_directive`, `list_scheduled_directives`,
//! `update_scheduled_directive`, `delete_scheduled_directive`. Every
//! DB primitive these tools need was already ported in Phase B
//! (`conexus_db::scheduled_directive_repository`) — this PR is the
//! tool-layer's three-tier authorization + validation + wiring on top
//! of an already-proven data layer.
//!
//! ## Timestamps: injected `now`, RFC3339 UTC — a deliberate
//! improvement over Python's scattered `datetime.now()` reads
//!
//! Python's `_now()` reads the LOCAL wall clock fresh at multiple
//! points within one call (`until` validation, `next_due`
//! computation) and stores naive-local ISO strings compared lexically
//! (its own R17-F1 comment documents the tz-unsafe corners this
//! creates). This port uses the single injected `now: &str` (RFC3339
//! UTC, matching `conexus-backend::server.rs`'s own convention) for
//! every "current time" the call needs, and
//! `scheduled_directive_repository::parse_flexible` (already tolerant
//! of RFC3339/naive-ISO, already used by this same repository's own
//! `collect_due_and_fire`) for comparing a caller-supplied `until`
//! against it as real `DateTime<Utc>` values rather than naive-local
//! strings — strictly safer, never changes the intended outcome when
//! formats agree.
//!
//! ## Three-tier authorization (ported faithfully)
//!
//! [`authorize_target_write`]: operator-tier always bypasses; an
//! agent-bearer writing its OWN schedules needs
//! `config_allow_worker_self_schedule` (default on); writing a
//! DIFFERENT agent's schedules requires the caller to be a manager
//! AND `config_allow_manager_curate_schedules` (default on) AND the
//! target to be a live, non-terminal WORKER (never another manager).
//!
//! [`authorize_existing_or_notfound`] (R17-F2): update/delete must not
//! let a non-owner distinguish "exists but forbidden" from "missing" —
//! collapses both into the SAME opaque `NotFound` for anyone who isn't
//! the owner or operator-tier, while the real owner (or a manager/
//! operator with real access) still gets the real reason.
//!
//! Deliberately NOT ported, with an explicit reason (never a silent
//! drop): the in-memory `log_audit` trail — same precedent as every
//! prior Phase D5 tool (the durable `agent_actions` row IS written).

use chrono::{DateTime, Duration, Utc};

use conexus_auth::{Requirement, Tool};
use conexus_core::capability::Capability;
use conexus_core::principal::{is_operator_tier, Principal, PrincipalKind};
use conexus_core::tool_result::ToolResult;
use conexus_db::agent_repository::{AgentRepository, AgentRow};
use conexus_db::scheduled_directive_repository::{
    self as repo, NullableUpdate, ScheduledDirectiveFields, ScheduledDirectiveRow,
};
use conexus_db::{agent_action_repository, project_settings_repository};
use rusqlite::Connection;
use serde_json::Value;
use tokio::sync::Mutex as AsyncMutex;

use crate::task_tools::{bool_arg, str_arg};

const MAX_INTERVAL_SECONDS: i64 = 315_360_000; // 10 years
const MAX_COUNT: i64 = 1_000_000;

/// Mirrors `agent_repository`'s own private `NOT_TERMINAL_SQL` set --
/// no shared public constant exists to reuse (that set is
/// SQL-embedded, not exported), so this is redeclared here rather
/// than reached into.
const TERMINAL_AGENT_STATUSES: &[&str] = &["terminated", "tombstone"];

fn rand_u64() -> u64 {
    use std::collections::hash_map::RandomState;
    use std::hash::BuildHasher;
    RandomState::new().hash_one(std::time::Instant::now())
}

fn generate_directive_id() -> String {
    format!("sd_{:016x}", rand_u64())
}

fn floor_seconds(conn: &Connection) -> i64 {
    project_settings_repository::get_int(conn, "config_min_schedule_interval_seconds", 60)
}

fn max_per_agent(conn: &Connection) -> i64 {
    project_settings_repository::get_int(conn, "config_max_schedules_per_agent", 10)
}

/// Format a `DateTime<Utc>` the same way
/// `scheduled_directive_repository`'s own internal `add_seconds_iso`
/// does, so a freshly-created row's timestamp shape matches what that
/// repository's own later recomputation (`collect_due_and_fire`)
/// produces on subsequent fires.
fn format_utc(dt: DateTime<Utc>) -> String {
    dt.to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}

/// Three-tier write authorization (see module doc). `None` = proceed.
fn authorize_target_write(
    conn: &Connection,
    principal: &Principal,
    target_agent_id: &str,
) -> Option<ToolResult> {
    if is_operator_tier(principal) {
        return None;
    }
    let (Some(caller_id), PrincipalKind::AgentBearer) =
        (principal.agent_id.as_deref(), principal.kind)
    else {
        return Some(ToolResult::PermissionDenied {
            reason: "Valid agent token or operator session required".to_string(),
        });
    };

    if target_agent_id == caller_id {
        if !project_settings_repository::get_bool(conn, "config_allow_worker_self_schedule", true) {
            return Some(ToolResult::PermissionDenied {
                reason: "Self-scheduling is disabled by the operator \
                    (config_allow_worker_self_schedule). Ask a manager or admin to create the \
                    schedule for you."
                    .to_string(),
            });
        }
        return None;
    }

    // Targeting another agent -> manager curation only.
    let caller_role = principal
        .agent_role
        .map(|r| r == conexus_core::capability::AgentRole::Manager)
        .unwrap_or(false);
    if !caller_role {
        return Some(ToolResult::PermissionDenied {
            reason: "Only a manager may schedule directives for another agent. Workers may \
                only manage their own schedules."
                .to_string(),
        });
    }
    if !project_settings_repository::get_bool(conn, "config_allow_manager_curate_schedules", true) {
        return Some(ToolResult::PermissionDenied {
            reason: "Manager schedule-curation is disabled by the operator \
                (config_allow_manager_curate_schedules)."
                .to_string(),
        });
    }
    let target: Option<AgentRow> = AgentRepository::get_by_id(conn, target_agent_id)
        .ok()
        .flatten();
    let Some(target) = target else {
        return Some(ToolResult::NotFound {
            resource: "agent".to_string(),
            identifier: target_agent_id.to_string(),
            hint: None,
        });
    };
    if TERMINAL_AGENT_STATUSES.contains(&target.status.as_str()) {
        return Some(ToolResult::NotFound {
            resource: "agent".to_string(),
            identifier: target_agent_id.to_string(),
            hint: None,
        });
    }
    if target.agent_role != "worker" {
        return Some(ToolResult::PermissionDenied {
            reason: "Managers may only schedule directives for workers.".to_string(),
        });
    }
    None
}

/// R17-F2: authorize a write against an EXISTING directive without
/// leaking that it exists to a non-owner, non-operator caller.
fn authorize_existing_or_notfound(
    conn: &Connection,
    principal: &Principal,
    existing: &ScheduledDirectiveRow,
    directive_id: &str,
) -> Option<ToolResult> {
    let denial = authorize_target_write(conn, principal, &existing.agent_id)?;
    let caller_id = (principal.kind == PrincipalKind::AgentBearer)
        .then_some(principal.agent_id.as_deref())
        .flatten();
    if caller_id != Some(existing.agent_id.as_str()) {
        return Some(ToolResult::NotFound {
            resource: "scheduled directive".to_string(),
            identifier: directive_id.to_string(),
            hint: None,
        });
    }
    Some(denial)
}

fn validate_interval(raw: Option<&Value>, floor: i64) -> Result<i64, ToolResult> {
    let interval = raw
        .and_then(|v| v.as_i64())
        .ok_or_else(|| ToolResult::Invalid {
            field: Some("interval_seconds".to_string()),
            message: "interval_seconds must be an integer number of seconds".to_string(),
        })?;
    if interval < floor {
        return Err(ToolResult::Invalid {
            field: Some("interval_seconds".to_string()),
            message: format!(
                "interval_seconds must be at least the configured floor of {floor}s \
                 (config_min_schedule_interval_seconds). Got {interval}."
            ),
        });
    }
    if interval > MAX_INTERVAL_SECONDS {
        return Err(ToolResult::Invalid {
            field: Some("interval_seconds".to_string()),
            message: format!(
                "interval_seconds must be at most {MAX_INTERVAL_SECONDS}s (10 years). Got \
                 {interval}."
            ),
        });
    }
    Ok(interval)
}

/// `(None, Ok(()))` = no end-condition. Compares against `now`
/// (already-parsed) as real `DateTime<Utc>` values -- see module doc.
fn validate_until(raw: Option<&Value>, now: DateTime<Utc>) -> Result<Option<String>, ToolResult> {
    let Some(raw) = raw else {
        return Ok(None);
    };
    if raw.is_null() {
        return Ok(None);
    }
    let Some(s) = raw.as_str() else {
        return Err(ToolResult::Invalid {
            field: Some("until".to_string()),
            message: "until must be an ISO-8601 datetime string".to_string(),
        });
    };
    let until_dt = repo::parse_flexible(s).map_err(|_| ToolResult::Invalid {
        field: Some("until".to_string()),
        message: "until must be a valid ISO-8601 datetime string".to_string(),
    })?;
    if until_dt <= now {
        return Err(ToolResult::Invalid {
            field: Some("until".to_string()),
            message: "until must be in the future".to_string(),
        });
    }
    Ok(Some(format_utc(until_dt)))
}

fn validate_count(raw: Option<&Value>) -> Result<Option<i64>, ToolResult> {
    let Some(raw) = raw else {
        return Ok(None);
    };
    if raw.is_null() {
        return Ok(None);
    }
    let count = raw.as_i64().ok_or_else(|| ToolResult::Invalid {
        field: Some("count".to_string()),
        message: "count must be a positive integer".to_string(),
    })?;
    if count < 1 {
        return Err(ToolResult::Invalid {
            field: Some("count".to_string()),
            message: "count must be a positive integer".to_string(),
        });
    }
    if count > MAX_COUNT {
        return Err(ToolResult::Invalid {
            field: Some("count".to_string()),
            message: format!("count must be at most {MAX_COUNT}. Got {count}."),
        });
    }
    Ok(Some(count))
}

/// Public-facing directive shape, shared by every tool's `Ok.data`
/// AND by `GET /api/schedules` (Phase E1) -- that REST endpoint reads
/// `conexus_db::scheduled_directive_repository::list_all` directly
/// (an operator-only, cross-agent, unscoped view; the MCP
/// `list_scheduled_directives` tool below is scoped to the caller's
/// own schedules) rather than dispatching through a tool, but reuses
/// this exact serialization so the two surfaces render identically.
pub fn serialize(row: &ScheduledDirectiveRow) -> Value {
    serde_json::json!({
        "directive_id": row.directive_id,
        "agent_id": row.agent_id,
        "prompt": row.prompt,
        "interval_seconds": row.interval_seconds,
        "next_due_at": row.next_due_at,
        "enabled": row.enabled,
        "status": row.status,
        "until_at": row.until_at,
        "max_runs": row.max_runs,
        "run_count": row.run_count,
        "created_at": row.created_at,
        "created_by": row.created_by,
        "updated_at": row.updated_at,
        "updated_by": row.updated_by,
    })
}

pub struct CreateScheduledDirectiveTool;

impl Tool for CreateScheduledDirectiveTool {
    const NAME: &'static str = "create_scheduled_directive";
    const REQUIRED: Requirement = Requirement::Policy {
        keys: &[
            "config_allow_worker_self_schedule",
            "config_allow_manager_curate_schedules",
        ],
        default: true,
    };
    const DESCRIPTION: &'static str = "Register a recurring directive that fires when the \
        target agent next checks in at-or-after the interval (a durable, server-side, \
        event-coalesced '/loop'). By default an agent schedules for itself; a manager may \
        schedule for its workers. An enabled schedule keeps the agent alive past the idle-stop \
        window so it can receive fires.";
    // The literal 256/"maxLength" mirrors IDENTIFIER_MAX_LEN -- cross-checked
    // by this module's own test (SCHEMA must be `const`-constructible).
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The imperative directive text delivered to the agent when the schedule fires (e.g. 'check the CI status and report')."
            },
            "interval_seconds": {
                "type": "integer",
                "description": "How often the directive fires, in seconds. Must be >= the operator's floor (config_min_schedule_interval_seconds, default 60). The interval resets from each delivery, so a busy agent never piles up fires.",
                "minimum": 1
            },
            "agent_id": {
                "type": "string",
                "description": "Target agent. Omit to schedule for yourself. A manager may target one of its workers; an operator may target anyone.",
                "maxLength": 256
            },
            "until": {
                "type": ["string", "null"],
                "description": "Optional end-condition: an ISO-8601 datetime after which the schedule stops firing and becomes 'completed'."
            },
            "count": {
                "type": ["integer", "null"],
                "description": "Optional end-condition: stop after this many fires (then 'completed').",
                "minimum": 1
            },
            "run_now": {
                "type": "boolean",
                "description": "When true, the first fire is immediate (next check-in) instead of one interval out. Default false.",
                "default": false
            }
        },
        "required": ["prompt", "interval_seconds"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            // Requirement::Policy guarantees `principal` is `Some`.
            let principal = principal.unwrap();

            let Some(prompt) = str_arg(arguments, "prompt").filter(|s| !s.is_empty()) else {
                return ToolResult::Invalid {
                    field: Some("prompt".to_string()),
                    message: "prompt is required".to_string(),
                };
            };
            if prompt.len() > 4000 {
                return ToolResult::Invalid {
                    field: Some("prompt".to_string()),
                    message: "prompt too long (max 4000 characters)".to_string(),
                };
            }

            let guard = conn.lock().await;
            let now_dt = match repo::parse_flexible(now) {
                Ok(dt) => dt,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Internal clock error".to_string(),
                    }
                }
            };

            let interval =
                match validate_interval(arguments.get("interval_seconds"), floor_seconds(&guard)) {
                    Ok(v) => v,
                    Err(e) => return e,
                };
            let until_iso = match validate_until(arguments.get("until"), now_dt) {
                Ok(v) => v,
                Err(e) => return e,
            };
            let max_runs = match validate_count(arguments.get("count")) {
                Ok(v) => v,
                Err(e) => return e,
            };

            let caller_id = (principal.kind == PrincipalKind::AgentBearer)
                .then_some(principal.agent_id.as_deref())
                .flatten();
            let target_raw = arguments.get("agent_id").and_then(Value::as_str);
            let target_agent_id = match target_raw.or(caller_id) {
                Some(id) if !id.is_empty() => id.to_string(),
                _ => {
                    return ToolResult::Invalid {
                        field: Some("agent_id".to_string()),
                        message: "agent_id is required (no calling agent to default to)"
                            .to_string(),
                    }
                }
            };

            if let Some(denial) = authorize_target_write(&guard, principal, &target_agent_id) {
                return denial;
            }

            let run_now = bool_arg(arguments, "run_now");
            let next_due_dt = if run_now {
                now_dt
            } else {
                now_dt + Duration::seconds(interval)
            };
            if let Some(until_iso) = &until_iso {
                if !run_now && format_utc(next_due_dt).as_str() > until_iso.as_str() {
                    return ToolResult::Invalid {
                        field: Some("until".to_string()),
                        message: "until is before the first fire -- nothing would run".to_string(),
                    };
                }
            }

            let active = match repo::count_active_for_agent(&guard, &target_agent_id) {
                Ok(n) => n,
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "Failed to create scheduled directive".to_string(),
                    }
                }
            };
            let cap = max_per_agent(&guard);
            if active >= cap {
                return ToolResult::Invalid {
                    field: Some("interval_seconds".to_string()),
                    message: format!(
                        "agent '{target_agent_id}' already has {active} active schedules (max \
                         {cap}, config_max_schedules_per_agent). Delete or pause one first."
                    ),
                };
            }

            let directive_id = generate_directive_id();
            let created_by = principal.actor_label();
            let created = repo::create(
                &guard,
                &directive_id,
                &target_agent_id,
                &prompt,
                interval,
                &format_utc(next_due_dt),
                until_iso.as_deref(),
                max_runs,
                Some(created_by),
                now,
            );
            let created = match created {
                Ok(row) => row,
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "Failed to create scheduled directive".to_string(),
                    }
                }
            };
            let _ = agent_action_repository::log_agent_action(
                &guard,
                created_by,
                "create_scheduled_directive",
                None,
                Some(&serde_json::json!({
                    "directive_id": directive_id,
                    "agent_id": target_agent_id,
                    "interval_seconds": interval,
                    "run_now": run_now,
                })),
                now,
            );
            drop(guard);
            // Wake a currently-holding wait_for_events so a run_now (or an
            // immediately-due) schedule fires now rather than on the next
            // ~2s flag-recheck slice.
            ctx.waiter_registry.notify(&target_agent_id);

            ToolResult::Ok {
                data: Some(serde_json::json!({"directive": serialize(&created)})),
                message: Some(format!(
                    "Scheduled directive {directive_id} created for {target_agent_id} (every \
                     {interval}s, first fire {}).",
                    if run_now {
                        "now".to_string()
                    } else {
                        format!("in {interval}s")
                    }
                )),
            }
        })
    }
}

pub struct ListScheduledDirectivesTool;

impl Tool for ListScheduledDirectivesTool {
    const NAME: &'static str = "list_scheduled_directives";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::CoordinationWait,
        reason: None,
    };
    const DESCRIPTION: &'static str = "List scheduled directives for the calling agent (or, \
        for a manager/operator, a named target). Returns id, prompt, interval, next_due_at, \
        enabled, status, and run_count.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Target agent whose schedules to list. Omit for your own.",
                "maxLength": 256
            }
        },
        "required": [],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        _now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let principal = principal.unwrap();
            let caller_id = (principal.kind == PrincipalKind::AgentBearer)
                .then_some(principal.agent_id.as_deref())
                .flatten();
            let target_raw = arguments.get("agent_id").and_then(Value::as_str);
            let target_agent_id = match target_raw.or(caller_id) {
                Some(id) if !id.is_empty() => id.to_string(),
                _ => {
                    return ToolResult::Invalid {
                        field: Some("agent_id".to_string()),
                        message: "agent_id is required (no calling agent to default to)"
                            .to_string(),
                    }
                }
            };

            let guard = conn.lock().await;
            if Some(target_agent_id.as_str()) != caller_id {
                if let Some(denial) = authorize_target_write(&guard, principal, &target_agent_id) {
                    return denial;
                }
            }

            let rows = match repo::list_for_agent(&guard, &target_agent_id) {
                Ok(r) => r,
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "Failed to list scheduled directives".to_string(),
                    }
                }
            };
            let directives: Vec<Value> = rows.iter().map(serialize).collect();
            ToolResult::Ok {
                data: Some(serde_json::json!({
                    "agent_id": target_agent_id,
                    "directives": directives,
                    "count": directives.len(),
                })),
                message: Some(format!(
                    "{} scheduled directive(s) for {target_agent_id}.",
                    directives.len()
                )),
            }
        })
    }
}

pub struct UpdateScheduledDirectiveTool;

impl Tool for UpdateScheduledDirectiveTool {
    const NAME: &'static str = "update_scheduled_directive";
    const REQUIRED: Requirement = Requirement::Policy {
        keys: &[
            "config_allow_worker_self_schedule",
            "config_allow_manager_curate_schedules",
        ],
        default: true,
    };
    const DESCRIPTION: &'static str = "Edit, pause, or resume a scheduled directive. Set \
        enabled=false to pause and enabled=true to resume (re-arms the next fire one interval \
        out). Interval changes re-validate the floor.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "directive_id": {"type": "string", "description": "The id of the schedule to update.", "maxLength": 256},
            "prompt": {"type": ["string", "null"], "description": "New directive text."},
            "interval_seconds": {"type": ["integer", "null"], "description": "New interval (seconds); re-checks floor.", "minimum": 1},
            "enabled": {"type": ["boolean", "null"], "description": "true=resume, false=pause."},
            "until": {"type": ["string", "null"], "description": "New until end-condition (ISO datetime), or null to clear it."},
            "count": {"type": ["integer", "null"], "description": "New max-runs end-condition, or null to clear it.", "minimum": 1}
        },
        "required": ["directive_id"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let principal = principal.unwrap();
            let Some(directive_id) = str_arg(arguments, "directive_id").filter(|s| !s.is_empty())
            else {
                return ToolResult::Invalid {
                    field: Some("directive_id".to_string()),
                    message: "directive_id is required".to_string(),
                };
            };

            let guard = conn.lock().await;
            let now_dt = match repo::parse_flexible(now) {
                Ok(dt) => dt,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Internal clock error".to_string(),
                    }
                }
            };

            let existing = match repo::get(&guard, &directive_id) {
                Ok(Some(r)) => r,
                Ok(None) => {
                    return ToolResult::NotFound {
                        resource: "scheduled directive".to_string(),
                        identifier: directive_id,
                        hint: None,
                    }
                }
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "Failed to update scheduled directive".to_string(),
                    }
                }
            };
            if let Some(denial) =
                authorize_existing_or_notfound(&guard, principal, &existing, &directive_id)
            {
                return denial;
            }

            let mut fields = ScheduledDirectiveFields::default();
            let mut touched_any = false;

            if let Some(v) = arguments.get("prompt") {
                if !v.is_null() {
                    let Some(s) = v.as_str().filter(|s| !s.is_empty()) else {
                        return ToolResult::Invalid {
                            field: Some("prompt".to_string()),
                            message: "prompt must be a non-empty string".to_string(),
                        };
                    };
                    if s.len() > 4000 {
                        return ToolResult::Invalid {
                            field: Some("prompt".to_string()),
                            message: "prompt too long (max 4000 characters)".to_string(),
                        };
                    }
                    fields.prompt = Some(s.to_string());
                    touched_any = true;
                }
            }

            let mut new_interval = existing.interval_seconds;
            if let Some(v) = arguments.get("interval_seconds") {
                if !v.is_null() {
                    new_interval = match validate_interval(Some(v), floor_seconds(&guard)) {
                        Ok(v) => v,
                        Err(e) => return e,
                    };
                    fields.interval_seconds = Some(new_interval);
                    touched_any = true;
                }
            }

            if arguments.get("until").is_some() {
                let until_iso = match validate_until(arguments.get("until"), now_dt) {
                    Ok(v) => v,
                    Err(e) => return e,
                };
                fields.until_at = match until_iso {
                    Some(v) => NullableUpdate::Set(v),
                    None => NullableUpdate::Clear,
                };
                touched_any = true;
            }

            if arguments.get("count").is_some() {
                let max_runs = match validate_count(arguments.get("count")) {
                    Ok(v) => v,
                    Err(e) => return e,
                };
                fields.max_runs = match max_runs {
                    Some(v) => NullableUpdate::Set(v),
                    None => NullableUpdate::Clear,
                };
                touched_any = true;
            }

            if let Some(v) = arguments.get("enabled") {
                if !v.is_null() {
                    let enabled = v.as_bool().unwrap_or(false);
                    if enabled {
                        if !existing.enabled {
                            let active =
                                match repo::count_active_for_agent(&guard, &existing.agent_id) {
                                    Ok(n) => n,
                                    Err(_e) => {
                                        return ToolResult::Failed {
                                            message: "Failed to update scheduled directive"
                                                .to_string(),
                                        }
                                    }
                                };
                            let cap = max_per_agent(&guard);
                            if active >= cap {
                                return ToolResult::Invalid {
                                    field: Some("enabled".to_string()),
                                    message: format!(
                                        "agent '{}' already has {active} active schedules (max \
                                         {cap}). Pause or delete one first.",
                                        existing.agent_id
                                    ),
                                };
                            }
                        }
                        fields.enabled = Some(true);
                        fields.status = Some("active".to_string());
                        fields.next_due_at =
                            Some(format_utc(now_dt + Duration::seconds(new_interval)));
                    } else {
                        fields.enabled = Some(false);
                        fields.status = Some("paused".to_string());
                    }
                    touched_any = true;
                }
            }

            if !touched_any {
                return ToolResult::Invalid {
                    field: None,
                    message: "no updatable field provided (prompt, interval_seconds, enabled, \
                        until, count)"
                        .to_string(),
                };
            }

            let updated_by = principal.actor_label();
            let updated = repo::update_fields(&guard, &directive_id, &fields, updated_by, now);
            let updated = match updated {
                Ok(Some(row)) => row,
                Ok(None) => {
                    return ToolResult::NotFound {
                        resource: "scheduled directive".to_string(),
                        identifier: directive_id,
                        hint: None,
                    }
                }
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "Failed to update scheduled directive".to_string(),
                    }
                }
            };
            let _ = agent_action_repository::log_agent_action(
                &guard,
                updated_by,
                "update_scheduled_directive",
                None,
                Some(&serde_json::json!({
                    "directive_id": directive_id,
                    "agent_id": existing.agent_id,
                })),
                now,
            );
            drop(guard);
            ctx.waiter_registry.notify(&existing.agent_id);

            ToolResult::Ok {
                data: Some(serde_json::json!({"directive": serialize(&updated)})),
                message: Some(format!("Scheduled directive {directive_id} updated.")),
            }
        })
    }
}

pub struct DeleteScheduledDirectiveTool;

impl Tool for DeleteScheduledDirectiveTool {
    const NAME: &'static str = "delete_scheduled_directive";
    const REQUIRED: Requirement = Requirement::Policy {
        keys: &[
            "config_allow_worker_self_schedule",
            "config_allow_manager_curate_schedules",
        ],
        default: true,
    };
    const DESCRIPTION: &'static str = "Delete a scheduled directive permanently.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "directive_id": {"type": "string", "description": "The id of the schedule to delete.", "maxLength": 256}
        },
        "required": ["directive_id"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let principal = principal.unwrap();
            let Some(directive_id) = str_arg(arguments, "directive_id").filter(|s| !s.is_empty())
            else {
                return ToolResult::Invalid {
                    field: Some("directive_id".to_string()),
                    message: "directive_id is required".to_string(),
                };
            };

            let guard = conn.lock().await;
            let existing = match repo::get(&guard, &directive_id) {
                Ok(Some(r)) => r,
                Ok(None) => {
                    return ToolResult::NotFound {
                        resource: "scheduled directive".to_string(),
                        identifier: directive_id,
                        hint: None,
                    }
                }
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "Failed to delete scheduled directive".to_string(),
                    }
                }
            };
            if let Some(denial) =
                authorize_existing_or_notfound(&guard, principal, &existing, &directive_id)
            {
                return denial;
            }

            if let Err(_e) = repo::delete(&guard, &directive_id) {
                return ToolResult::Failed {
                    message: "Failed to delete scheduled directive".to_string(),
                };
            }
            let _ = agent_action_repository::log_agent_action(
                &guard,
                principal.actor_label(),
                "delete_scheduled_directive",
                None,
                Some(&serde_json::json!({
                    "directive_id": directive_id,
                    "agent_id": existing.agent_id,
                })),
                now,
            );

            ToolResult::Ok {
                data: Some(serde_json::json!({"deleted": directive_id})),
                message: Some(format!("Scheduled directive {directive_id} deleted.")),
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_auth::ToolCallContext;
    use conexus_core::capability::{AgentRole, Capabilities};
    use conexus_db::agent_repository::NewAgent;
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::file_map::FileMap;
    use conexus_wakeloop::waiter_registry::WaiterRegistry;
    use std::collections::HashSet;

    const NOW: &str = "2026-06-01T00:00:00+00:00";

    fn worker(agent_id: &str) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some(agent_id.to_string()),
            project_name: None,
            project_role: None,
            agent_role: Some(AgentRole::Worker),
            can_wake_loop: true,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::from([Capability::CoordinationWait])),
        }
    }

    fn manager(agent_id: &str) -> Principal {
        let mut p = worker(agent_id);
        p.agent_role = Some(AgentRole::Manager);
        p.capabilities = Capabilities::Set(HashSet::from([
            Capability::CoordinationWait,
            Capability::TasksAssign,
        ]));
        p
    }

    fn operator() -> Principal {
        Principal {
            kind: PrincipalKind::ForwardingHeader,
            user_id: Some("op-1".to_string()),
            agent_id: None,
            project_name: Some("demo".to_string()),
            project_role: Some(conexus_core::capability::ProjectRole::Operator),
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::from([
                Capability::SystemConfigWrite,
                Capability::CoordinationWait,
            ])),
        }
    }

    async fn setup() -> AsyncMutex<Connection> {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        AsyncMutex::new(conn)
    }

    fn seed_agent(conn: &Connection, agent_id: &str, role: &str) {
        AgentRepository::create(
            conn,
            NewAgent {
                token: &format!("{agent_id}-tok"),
                agent_id,
                created_at: NOW,
                status: "active",
                current_task: None,
                working_directory: "/tmp",
                color: None,
                agent_role: role,
            },
        )
        .unwrap();
    }

    fn ctx<'a>(registry: &'a WaiterRegistry, file_map: &'a FileMap) -> ToolCallContext<'a> {
        ToolCallContext::off_wire(registry, file_map, std::path::Path::new("/tmp"))
    }

    #[tokio::test]
    async fn a_worker_can_create_a_self_schedule() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_agent(&c, "alice", "worker");
        }
        let alice = worker("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = CreateScheduledDirectiveTool::call(
            Some(&alice),
            &serde_json::json!({"prompt": "check CI", "interval_seconds": 300}),
            &conn,
            NOW,
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let d = &data.unwrap()["directive"];
        assert_eq!(d["agent_id"], "alice");
        assert_eq!(d["status"], "active");
    }

    #[tokio::test]
    async fn interval_below_the_floor_is_invalid() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_agent(&c, "alice", "worker");
        }
        let alice = worker("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = CreateScheduledDirectiveTool::call(
            Some(&alice),
            &serde_json::json!({"prompt": "x", "interval_seconds": 5}),
            &conn,
            NOW,
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn a_worker_cannot_schedule_for_another_worker() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_agent(&c, "alice", "worker");
            seed_agent(&c, "bob", "worker");
        }
        let alice = worker("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = CreateScheduledDirectiveTool::call(
            Some(&alice),
            &serde_json::json!({"prompt": "x", "interval_seconds": 300, "agent_id": "bob"}),
            &conn,
            NOW,
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::PermissionDenied { .. }));
    }

    #[tokio::test]
    async fn a_manager_can_schedule_for_its_worker_but_not_another_manager() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_agent(&c, "mgr", "manager");
            seed_agent(&c, "worker-1", "worker");
            seed_agent(&c, "mgr-2", "manager");
        }
        let mgr = manager("mgr");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);

        let ok = CreateScheduledDirectiveTool::call(
            Some(&mgr),
            &serde_json::json!({"prompt": "x", "interval_seconds": 300, "agent_id": "worker-1"}),
            &conn,
            NOW,
            &c,
        )
        .await;
        assert!(matches!(ok, ToolResult::Ok { .. }));

        let denied = CreateScheduledDirectiveTool::call(
            Some(&mgr),
            &serde_json::json!({"prompt": "x", "interval_seconds": 300, "agent_id": "mgr-2"}),
            &conn,
            NOW,
            &c,
        )
        .await;
        assert!(matches!(denied, ToolResult::PermissionDenied { .. }));
    }

    #[tokio::test]
    async fn an_operator_can_schedule_for_anyone_bypassing_policy() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_agent(&c, "worker-1", "worker");
        }
        let op = operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = CreateScheduledDirectiveTool::call(
            Some(&op),
            &serde_json::json!({"prompt": "x", "interval_seconds": 300, "agent_id": "worker-1"}),
            &conn,
            NOW,
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
    }

    #[tokio::test]
    async fn the_per_agent_cap_is_enforced() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_agent(&c, "alice", "worker");
            c.execute(
                "INSERT INTO project_settings (context_key, value, updated_at, updated_by) \
                 VALUES ('config_max_schedules_per_agent', '1', ?1, 'test')",
                [NOW],
            )
            .unwrap();
        }
        let alice = worker("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let first = CreateScheduledDirectiveTool::call(
            Some(&alice),
            &serde_json::json!({"prompt": "one", "interval_seconds": 300}),
            &conn,
            NOW,
            &c,
        )
        .await;
        assert!(matches!(first, ToolResult::Ok { .. }));
        let second = CreateScheduledDirectiveTool::call(
            Some(&alice),
            &serde_json::json!({"prompt": "two", "interval_seconds": 300}),
            &conn,
            NOW,
            &c,
        )
        .await;
        assert!(matches!(second, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn list_returns_only_the_targets_directives() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_agent(&c, "alice", "worker");
        }
        let alice = worker("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        CreateScheduledDirectiveTool::call(
            Some(&alice),
            &serde_json::json!({"prompt": "x", "interval_seconds": 300}),
            &conn,
            NOW,
            &c,
        )
        .await;
        let result =
            ListScheduledDirectivesTool::call(Some(&alice), &serde_json::json!({}), &conn, NOW, &c)
                .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["count"], 1);
    }

    #[tokio::test]
    async fn a_foreign_worker_cannot_list_someone_elses_schedules() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_agent(&c, "alice", "worker");
            seed_agent(&c, "bob", "worker");
        }
        let bob = worker("bob");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ListScheduledDirectivesTool::call(
            Some(&bob),
            &serde_json::json!({"agent_id": "alice"}),
            &conn,
            NOW,
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::PermissionDenied { .. }));
    }

    #[tokio::test]
    async fn the_owner_can_pause_and_resume_their_own_schedule() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_agent(&c, "alice", "worker");
        }
        let alice = worker("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let created = CreateScheduledDirectiveTool::call(
            Some(&alice),
            &serde_json::json!({"prompt": "x", "interval_seconds": 300}),
            &conn,
            NOW,
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = created else {
            panic!("expected Ok, got {created:?}");
        };
        let directive_id = data.unwrap()["directive"]["directive_id"]
            .as_str()
            .unwrap()
            .to_string();

        let paused = UpdateScheduledDirectiveTool::call(
            Some(&alice),
            &serde_json::json!({"directive_id": directive_id, "enabled": false}),
            &conn,
            NOW,
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = paused else {
            panic!("expected Ok, got {paused:?}");
        };
        assert_eq!(data.unwrap()["directive"]["status"], "paused");

        let resumed = UpdateScheduledDirectiveTool::call(
            Some(&alice),
            &serde_json::json!({"directive_id": directive_id, "enabled": true}),
            &conn,
            "2026-06-01T00:10:00+00:00",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = resumed else {
            panic!("expected Ok, got {resumed:?}");
        };
        assert_eq!(data.unwrap()["directive"]["status"], "active");
    }

    #[tokio::test]
    async fn a_non_owner_update_and_a_missing_directive_update_are_indistinguishable() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_agent(&c, "alice", "worker");
            seed_agent(&c, "bob", "worker");
        }
        let alice = worker("alice");
        let bob = worker("bob");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let created = CreateScheduledDirectiveTool::call(
            Some(&alice),
            &serde_json::json!({"prompt": "x", "interval_seconds": 300}),
            &conn,
            NOW,
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = created else {
            panic!("expected Ok, got {created:?}");
        };
        let directive_id = data.unwrap()["directive"]["directive_id"]
            .as_str()
            .unwrap()
            .to_string();

        let foreign = UpdateScheduledDirectiveTool::call(
            Some(&bob),
            &serde_json::json!({"directive_id": directive_id, "prompt": "steal"}),
            &conn,
            NOW,
            &c,
        )
        .await;
        let missing = UpdateScheduledDirectiveTool::call(
            Some(&bob),
            &serde_json::json!({"directive_id": "sd_doesnotexist", "prompt": "x"}),
            &conn,
            NOW,
            &c,
        )
        .await;
        assert!(matches!(foreign, ToolResult::NotFound { .. }));
        assert!(matches!(missing, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn the_owner_can_delete_their_own_schedule() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_agent(&c, "alice", "worker");
        }
        let alice = worker("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let created = CreateScheduledDirectiveTool::call(
            Some(&alice),
            &serde_json::json!({"prompt": "x", "interval_seconds": 300}),
            &conn,
            NOW,
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = created else {
            panic!("expected Ok, got {created:?}");
        };
        let directive_id = data.unwrap()["directive"]["directive_id"]
            .as_str()
            .unwrap()
            .to_string();
        let deleted = DeleteScheduledDirectiveTool::call(
            Some(&alice),
            &serde_json::json!({"directive_id": directive_id}),
            &conn,
            NOW,
            &c,
        )
        .await;
        assert!(matches!(deleted, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        assert_eq!(repo::get(&guard, &directive_id).unwrap(), None);
    }

    #[tokio::test]
    async fn a_past_until_is_rejected() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_agent(&c, "alice", "worker");
        }
        let alice = worker("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = CreateScheduledDirectiveTool::call(
            Some(&alice),
            &serde_json::json!({
                "prompt": "x", "interval_seconds": 300, "until": "2020-01-01T00:00:00+00:00"
            }),
            &conn,
            NOW,
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn run_now_fires_immediately() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_agent(&c, "alice", "worker");
        }
        let alice = worker("alice");
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = CreateScheduledDirectiveTool::call(
            Some(&alice),
            &serde_json::json!({"prompt": "x", "interval_seconds": 300, "run_now": true}),
            &conn,
            NOW,
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let d = &data.unwrap()["directive"];
        // next_due_at should equal "now" (well, the parsed+reformatted now).
        assert!(d["next_due_at"]
            .as_str()
            .unwrap()
            .starts_with("2026-06-01T00:00:00"));
    }

    #[test]
    fn schema_max_lengths_match_the_shared_constant() {
        let parsed: Value = serde_json::from_str(CreateScheduledDirectiveTool::SCHEMA).unwrap();
        let max_len = parsed["properties"]["agent_id"]["maxLength"]
            .as_u64()
            .unwrap();
        assert_eq!(
            max_len as usize,
            conexus_core::schema_limits::IDENTIFIER_MAX_LEN
        );
    }
}
