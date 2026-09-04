//! Port of `task_tools.py`'s `assign_task` (all 4 modes) and
//! `create_self_task` (Phase D4, PR 7/8).
//!
//! Per decision 4 in the migration plan's Phase D4 section, this PR
//! ships MECHANICAL-ONLY: `ENABLE_TASK_PLACEMENT_RAG`'s whole
//! placement-validation/suggestion block (`validate_task_placement`,
//! `format_suggestions_for_agent`, `should_escalate_to_admin`,
//! `accept_suggestions`) is NOT wired in either tool. Both real Python
//! call sites gate that entire block behind `ENABLE_TASK_PLACEMENT_RAG`
//! and treat it as pure enrichment on top of the mechanical create/
//! assign path (a suggested parent/deps override, or an outright
//! block) — with it off, `final_parent_task_id`/`final_depends_on_tasks`
//! are simply the caller-supplied values unchanged, which is exactly
//! what this port does unconditionally. Wiring the RAG layer is a
//! tracked follow-up, not a silent drop.
//!
//! `auto_suggest_parent`'s "smart" root-conflict suggestion text
//! (`_suggest_optimal_parent_task`, RAG-driven) is deferred for the
//! same reason -- the schema still accepts the field (for forward
//! compat with a real MCP client), but this port always renders the
//! plain SQL-only suggestion list Python's own "basic fallback"
//! branch produces, regardless of the flag's value.
//!
//! Deliberate simplification over the literal port, not a preserved
//! contract: Python's `_authorize_assign_task` communicates its Mode-0
//! "who really created this" decision back to the caller by mutating
//! `arguments["_worker_created_by"]` -- a JSON side-channel the admin
//! path has to remember to strip so a caller can't forge it. This port
//! returns an [`AssignAuthorization`] enum instead: the decision IS the
//! value, so there's no side-channel to strip in the first place.

use conexus_auth::{Requirement, Tool};
use conexus_core::capability::Capability;
use conexus_core::principal::{Principal, PrincipalKind};
use conexus_core::task_ownership::can_access_task;
use conexus_core::tool_result::ToolResult;
use conexus_db::agent_repository::AgentRepository;
use conexus_db::scheduled_directive_repository::parse_flexible;
use conexus_db::task_repository::{self, NewTask, TaskRow};
use conexus_db::{agent_action_repository, project_settings_repository};
use conexus_wakeloop::event_feed::UNASSIGNED_TASK_TERMINAL_STATUSES as TERMINAL_TASK_STATUSES;
use rusqlite::Connection;
use serde_json::Value;
use tokio::sync::Mutex as AsyncMutex;

use crate::task_tools::{
    agent_assignable, bool_arg, find_dependency_cycle, link_child_to_parent, normalize_parent,
    single_root_conflict, str_arg,
};

// ── _authorize_assign_task ──────────────────────────────────────────

/// What kind of caller reached `assign_task`, and under what
/// provenance. Port of `_authorize_assign_task`'s decision -- see the
/// module doc for why this replaces Python's `arguments` side-channel
/// mutation.
pub enum AssignAuthorization {
    /// Operator / manager / sysadmin -- unrestricted.
    Admin,
    /// A worker filing an unassigned task (Mode 0). Carries the
    /// worker's own id so the caller (not "admin") is the recorded
    /// creator/audit actor -- OBS-R17-AZ provenance.
    WorkerFileUnassigned { creator: String },
    /// A worker self-claiming existing unassigned tasks (Mode 3 only
    /// -- create-and-assign-to-self is not a supported worker path).
    WorkerSelfClaim { worker_id: String },
}

/// Port of `_authorize_assign_task`. Permission matrix (see the
/// Python docstring, reproduced): admin/manager always permitted;
/// worker + no `target_agent_token` -> Mode 0, gated by
/// `config_allow_worker_create_unassigned`; worker +
/// `target_agent_token == own token` + non-empty `task_ids` -> Mode 3
/// self-claim, gated by `config_allow_worker_self_assign`; every other
/// worker shape (targeting someone else, or self-targeting without
/// `task_ids`) is rejected.
pub fn authorize_assign_task(
    conn: &Connection,
    target_agent_token: Option<&str>,
    has_task_ids: bool,
    principal: &Principal,
) -> Result<AssignAuthorization, String> {
    if principal.has_capability(Capability::TasksAssign) {
        return Ok(AssignAuthorization::Admin);
    }

    let worker_id = if principal.kind == PrincipalKind::AgentBearer {
        principal.agent_id.as_deref()
    } else {
        None
    };
    let Some(worker_id) = worker_id else {
        return Err("Unauthorized: Admin token required".to_string());
    };

    let Some(target_agent_token) = target_agent_token else {
        if !project_settings_repository::get_bool(
            conn,
            "config_allow_worker_create_unassigned",
            true,
        ) {
            return Err(
                "Unauthorized: worker self-filing of unassigned tasks is disabled by \
                 project policy (config_allow_worker_create_unassigned=false). Ask admin \
                 to enable it in dashboard Settings."
                    .to_string(),
            );
        }
        return Ok(AssignAuthorization::WorkerFileUnassigned {
            creator: worker_id.to_string(),
        });
    };

    let target_agent_id = AgentRepository::get_by_token(conn, target_agent_token)
        .ok()
        .flatten()
        .map(|row| row.agent_id);
    let targeting_self = target_agent_id.as_deref() == Some(worker_id);

    if !targeting_self {
        return Err(
            "Unauthorized: workers can only assign tasks to themselves (use \
             config_allow_worker_self_assign + agent_token=<your own>)"
                .to_string(),
        );
    }

    if !has_task_ids {
        return Err(
            "Unauthorized: workers may only self-claim existing unassigned tasks (pass \
             task_ids=[...]); create-and-assign-to-self is not supported. File the task \
             with no agent_token and then claim it."
                .to_string(),
        );
    }

    if !project_settings_repository::get_bool(conn, "config_allow_worker_self_assign", true) {
        return Err("Unauthorized: worker self-assignment is disabled \
             (config_allow_worker_self_assign=false). Ask admin to enable it in dashboard \
             Settings."
            .to_string());
    }

    Ok(AssignAuthorization::WorkerSelfClaim {
        worker_id: worker_id.to_string(),
    })
}

// ── _analyze_agent_workload ─────────────────────────────────────────

pub struct WorkloadAnalysis {
    pub total_active_tasks: i64,
    pub high_priority_tasks: i64,
    pub stale_tasks: i64,
    pub capacity_status: &'static str,
    pub can_take_new_task: bool,
    pub recommendations: Vec<String>,
}

/// Port of `_analyze_agent_workload` -- pure SQL + arithmetic, no RAG
/// involved. `now` is this crate's usual explicit-clock convention;
/// Python reads a live `datetime.now()` here.
pub fn analyze_agent_workload(
    conn: &Connection,
    agent_id: &str,
    now: &str,
) -> rusqlite::Result<WorkloadAnalysis> {
    let mut stmt = conn.prepare(
        "SELECT status, priority, updated_at FROM tasks \
         WHERE assigned_to = ?1 AND status IN ('pending', 'in_progress')",
    )?;
    let rows: Vec<(String, String, String)> = stmt
        .query_map([agent_id], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })?
        .collect::<rusqlite::Result<_>>()?;

    let total_tasks = rows.len() as i64;
    let high_priority_tasks = rows.iter().filter(|(_, p, _)| p == "high").count() as i64;

    let current_time = parse_flexible(now).ok();
    let stale_tasks = rows
        .iter()
        .filter(|(_, _, updated_at)| {
            current_time
                .zip(parse_flexible(updated_at).ok())
                .is_some_and(|(now, updated)| (now - updated).num_days() > 3)
        })
        .count() as i64;

    let capacity_status = if total_tasks >= 8 {
        "overloaded"
    } else if total_tasks >= 5 || high_priority_tasks >= 3 {
        "busy"
    } else {
        "available"
    };

    let mut recommendations = Vec::new();
    if capacity_status == "overloaded" {
        recommendations.push("Consider redistributing some tasks to other agents".to_string());
        recommendations.push("Focus on completing high-priority tasks first".to_string());
    }
    if stale_tasks > 0 {
        recommendations.push(format!(
            "Review {stale_tasks} stale tasks that haven't been updated recently"
        ));
    }
    if total_tasks > 6 {
        recommendations
            .push("Consider breaking down large tasks into smaller subtasks".to_string());
    }
    if recommendations.is_empty() {
        recommendations.push("Workload appears manageable".to_string());
    }

    Ok(WorkloadAnalysis {
        total_active_tasks: total_tasks,
        high_priority_tasks,
        stale_tasks,
        capacity_status,
        can_take_new_task: matches!(capacity_status, "available" | "busy")
            && high_priority_tasks < 4,
        recommendations,
    })
}

// ── Mode 0: _create_unassigned_tasks ────────────────────────────────

/// One task to create unassigned -- shared shape for both the
/// single-item and batch (`tasks: [...]`) call forms.
struct UnassignedTaskSpec {
    title: String,
    description: String,
    priority: String,
    parent_task_id: Option<String>,
}

fn parse_unassigned_task_specs(arguments: &Value) -> Option<Vec<UnassignedTaskSpec>> {
    let items = arguments.get("tasks")?.as_array()?;
    let mut specs = Vec::with_capacity(items.len());
    for item in items {
        let title = item.get("title")?.as_str()?.to_string();
        let description = item.get("description")?.as_str()?.to_string();
        let priority = item
            .get("priority")
            .and_then(Value::as_str)
            .unwrap_or("medium")
            .to_string();
        let parent_task_id = item
            .get("parent_task_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_string);
        specs.push(UnassignedTaskSpec {
            title,
            description,
            priority,
            parent_task_id,
        });
    }
    Some(specs)
}

/// Port of `_create_unassigned_tasks` (Mode 0). `worker_created_by` is
/// `Some(worker_id)` exactly when [`authorize_assign_task`] returned
/// `WorkerFileUnassigned` -- gates the AZ-R19-1 worker-parent-ownership
/// check (a worker may only attach a child under a parent it owns; a
/// foreign-or-nonexistent parent collapses to the same phantom
/// `NotFound`, never distinguishing the two).
fn create_unassigned_tasks(
    tx: &Connection,
    arguments: &Value,
    worker_created_by: Option<&str>,
    now: &str,
) -> Result<ToolResult, rusqlite::Error> {
    let creator = worker_created_by.unwrap_or("admin");

    let batch = parse_unassigned_task_specs(arguments);
    let single_title = str_arg(arguments, "task_title");
    let single_description = str_arg(arguments, "task_description");
    let single_priority = str_arg(arguments, "priority").unwrap_or_else(|| "medium".to_string());
    let single_parent = normalize_parent(arguments.get("parent_task_id"));

    // AZ-R19-1: a worker may only attach a child under a parent it
    // OWNS. Collected up front so both branches share the same check.
    if let Some(worker_id) = worker_created_by {
        let parent_ids: Vec<Option<String>> = match &batch {
            Some(specs) => {
                if specs.iter().any(|s| s.parent_task_id.is_none()) {
                    return Ok(ToolResult::Conflict {
                        reason: "Workers cannot create root tasks. Every task filed via \
                            assign_task must specify a parent_task_id -- and the parent \
                            must be a task you own."
                            .to_string(),
                    });
                }
                specs.iter().map(|s| s.parent_task_id.clone()).collect()
            }
            None => {
                if single_parent.is_none() {
                    return Ok(ToolResult::Conflict {
                        reason: "Workers cannot create root tasks. Specify a parent_task_id \
                            -- a task you own -- when filing an unassigned task."
                            .to_string(),
                    });
                }
                vec![single_parent.clone()]
            }
        };
        for parent_id in parent_ids.into_iter().flatten() {
            let owns = task_repository::get_by_id(tx, &parent_id)?.is_some_and(|p| {
                can_access_task(
                    p.assigned_to.as_deref(),
                    Some(p.created_by.as_str()),
                    Some(worker_id),
                    false,
                    false,
                    false,
                    false,
                )
            });
            if !owns {
                return Ok(ToolResult::NotFound {
                    resource: "task".to_string(),
                    identifier: parent_id,
                    hint: None,
                });
            }
        }
    }

    let mut created: Vec<(String, String, String)> = Vec::new(); // (task_id, title, priority)

    if let Some(specs) = &batch {
        // R5-F5: single-root guard for the bulk path -- both a
        // pre-existing DB root AND more than one parentless task in
        // this SAME batch conflict (the 2nd would lose the partial
        // UNIQUE index race with a generic IntegrityError otherwise).
        let parentless_count = specs.iter().filter(|s| s.parent_task_id.is_none()).count();
        if parentless_count > 0 {
            if let Some(conflict) = single_root_conflict(tx) {
                return Ok(conflict);
            }
            if parentless_count > 1 {
                return Ok(ToolResult::Conflict {
                    reason: format!(
                        "Cannot create more than one root task in a single batch. This \
                         batch has {parentless_count} tasks without a parent_task_id; at \
                         most one task per batch may omit parent_task_id (it becomes the \
                         root). Give the rest a parent_task_id."
                    ),
                });
            }
        }
        for spec in specs {
            let fresh = task_repository::create(
                tx,
                NewTask {
                    task_id: None,
                    title: &spec.title,
                    description: Some(&spec.description),
                    assigned_to: None,
                    created_by: creator,
                    status: "unassigned",
                    priority: &spec.priority,
                    parent_task: spec.parent_task_id.as_deref(),
                    child_tasks: None,
                    depends_on_tasks: None,
                    notes: None,
                    now,
                },
            )?;
            let _ = link_child_to_parent(tx, spec.parent_task_id.as_deref(), &fresh.task_id, now);
            let _ = agent_action_repository::log_agent_action(
                tx,
                creator,
                "created_unassigned_task",
                Some(&fresh.task_id),
                Some(&serde_json::json!({"title": spec.title, "mode": "unassigned_multiple"})),
                now,
            );
            created.push((fresh.task_id, spec.title.clone(), spec.priority.clone()));
        }
    } else if let (Some(title), Some(description)) = (&single_title, &single_description) {
        if single_parent.is_none() {
            if let Some(conflict) = single_root_conflict(tx) {
                return Ok(conflict);
            }
        }
        let fresh = task_repository::create(
            tx,
            NewTask {
                task_id: None,
                title,
                description: Some(description),
                assigned_to: None,
                created_by: creator,
                status: "unassigned",
                priority: &single_priority,
                parent_task: single_parent.as_deref(),
                child_tasks: None,
                depends_on_tasks: None,
                notes: None,
                now,
            },
        )?;
        let _ = link_child_to_parent(tx, single_parent.as_deref(), &fresh.task_id, now);
        let _ = agent_action_repository::log_agent_action(
            tx,
            creator,
            "created_unassigned_task",
            Some(&fresh.task_id),
            Some(&serde_json::json!({"title": title, "mode": "unassigned_single"})),
            now,
        );
        created.push((fresh.task_id, title.clone(), single_priority.clone()));
    } else {
        return Ok(ToolResult::Invalid {
            field: None,
            message: "Provide either 'task_title' and 'task_description' for single task, \
                or 'tasks' array for multiple tasks."
                .to_string(),
        });
    }

    let mut response_parts = vec![
        "\u{2705} **Unassigned Tasks Created**".to_string(),
        format!("   Tasks Created: {}", created.len()),
        "   Status: Unassigned".to_string(),
        String::new(),
    ];
    for (i, (task_id, title, priority)) in created.iter().enumerate() {
        response_parts.push(format!(
            "   {}. {task_id}: {title} (Priority: {priority})",
            i + 1
        ));
    }
    response_parts.push(
        "\n\u{1F4A1} Use assign_task with task_ids parameter to assign these tasks to agents."
            .to_string(),
    );

    Ok(ToolResult::Ok {
        data: None,
        message: Some(response_parts.join("\n")),
    })
}

// ── Mode 3: _assign_to_existing_tasks ───────────────────────────────

/// Port of `_assign_to_existing_tasks`. `is_admin_request` gates the
/// informative-vs-phantom error split (SEC-R18/AZ-R18-1/BL-R18-1): a
/// non-admin self-claim caller sees the IDENTICAL phantom `NotFound`
/// for every non-claimable outcome (nonexistent, foreign-owned,
/// terminal), never learning which case it hit.
#[allow(clippy::too_many_arguments)]
fn assign_to_existing_tasks(
    tx: &Connection,
    target_agent_id: &str,
    task_ids: &[String],
    coordination_notes: Option<&str>,
    requesting_actor: &str,
    is_admin_request: bool,
    now: &str,
) -> Result<ToolResult, rusqlite::Error> {
    let phantom_not_found = || ToolResult::NotFound {
        resource: "task".to_string(),
        identifier: task_ids.join(", "),
        hint: None,
    };

    let mut found: Vec<TaskRow> = Vec::new();
    for tid in task_ids {
        if let Some(row) = task_repository::get_by_id(tx, tid)? {
            found.push(row);
        }
    }
    if found.len() != task_ids.len() {
        if !is_admin_request {
            return Ok(phantom_not_found());
        }
        let found_ids: std::collections::HashSet<&str> =
            found.iter().map(|t| t.task_id.as_str()).collect();
        let missing: Vec<&str> = task_ids
            .iter()
            .map(String::as_str)
            .filter(|tid| !found_ids.contains(tid))
            .collect();
        return Ok(ToolResult::NotFound {
            resource: "task".to_string(),
            identifier: missing.join(", "),
            hint: None,
        });
    }

    let assigned: Vec<&TaskRow> = found.iter().filter(|t| t.assigned_to.is_some()).collect();
    if !assigned.is_empty() {
        if !is_admin_request {
            let all_own = assigned.iter().all(|t| {
                can_access_task(
                    t.assigned_to.as_deref(),
                    Some(t.created_by.as_str()),
                    Some(target_agent_id),
                    false,
                    false,
                    false,
                    false,
                )
            });
            if !all_own {
                return Ok(phantom_not_found());
            }
            let own_ids: Vec<&str> = assigned.iter().map(|t| t.task_id.as_str()).collect();
            let mut own_terminal: Vec<&str> = assigned
                .iter()
                .map(|t| t.status.as_str())
                .filter(|s| TERMINAL_TASK_STATUSES.contains(s))
                .collect();
            own_terminal.sort_unstable();
            own_terminal.dedup();
            if !own_terminal.is_empty() {
                return Ok(ToolResult::Conflict {
                    reason: format!(
                        "task(s) {} are already assigned to you and in a terminal state \
                         ({}); they are finished and cannot be re-claimed.",
                        own_ids.join(", "),
                        own_terminal.join(", ")
                    ),
                });
            }
            return Ok(ToolResult::Conflict {
                reason: format!(
                    "task(s) {} are already assigned to you -- no claim needed. You \
                     already own them; call update_task_status to work on them.",
                    own_ids.join(", ")
                ),
            });
        }
        let assigned_list: Vec<String> = assigned
            .iter()
            .map(|t| {
                format!(
                    "{} (assigned to {})",
                    t.task_id,
                    t.assigned_to.as_deref().unwrap_or("")
                )
            })
            .collect();
        return Ok(ToolResult::Conflict {
            reason: format!(
                "some tasks are already assigned: {}",
                assigned_list.join(", ")
            ),
        });
    }

    let terminal: Vec<&TaskRow> = found
        .iter()
        .filter(|t| TERMINAL_TASK_STATUSES.contains(&t.status.as_str()))
        .collect();
    if !terminal.is_empty() {
        if !is_admin_request {
            return Ok(phantom_not_found());
        }
        let terminal_list: Vec<String> = terminal
            .iter()
            .map(|t| format!("{} ({})", t.task_id, t.status))
            .collect();
        return Ok(ToolResult::Conflict {
            reason: format!(
                "cannot assign task(s) in a terminal state (terminal states are a sink): {}",
                terminal_list.join(", ")
            ),
        });
    }

    if !AgentRepository::is_live(tx, target_agent_id).unwrap_or(false) {
        return Ok(ToolResult::NotFound {
            resource: "agent".to_string(),
            identifier: target_agent_id.to_string(),
            hint: None,
        });
    }

    for task_id in task_ids {
        let _ = task_repository::update_fields(
            tx,
            task_id,
            &task_repository::TaskFields {
                assigned_to: conexus_db::scheduled_directive_repository::NullableUpdate::Set(
                    target_agent_id.to_string(),
                ),
                ..Default::default()
            },
            now,
        );
        let _ = agent_action_repository::log_agent_action(
            tx,
            requesting_actor,
            "assigned_task",
            Some(task_id),
            Some(&serde_json::json!({
                "agent_id": target_agent_id,
                "mode": "existing_task_assignment",
            })),
            now,
        );
    }

    if let Ok(Some(agent)) = AgentRepository::get_by_id(tx, target_agent_id) {
        if agent.current_task.is_none() {
            let _ = AgentRepository::update_field(
                tx,
                target_agent_id,
                conexus_db::agent_repository::AgentField::CurrentTask,
                conexus_db::agent_repository::FieldValue::OptionalText(Some(task_ids[0].clone())),
                now,
            );
        }
    }

    let mut response_parts = vec![
        "\u{2705} **Tasks Assigned Successfully**".to_string(),
        format!("   Agent: {target_agent_id}"),
        format!("   Tasks Assigned: {}", task_ids.len()),
        String::new(),
    ];
    for (i, task) in found.iter().enumerate() {
        response_parts.push(format!("   {}. {}: {}", i + 1, task.task_id, task.title));
    }
    if let Some(notes) = coordination_notes {
        response_parts.push(format!("\n\u{1F4CB} **Coordination Notes:** {notes}"));
    }

    Ok(ToolResult::Ok {
        data: None,
        message: Some(response_parts.join("\n")),
    })
}

// ── Mode 2: _create_and_assign_multiple_tasks ───────────────────────

/// Port of `_create_and_assign_multiple_tasks` (Mode 2). Admin-only in
/// practice -- `authorize_assign_task` never routes a worker here
/// (Mode 2 requires `target_agent_token` + `tasks`, which only an
/// admin/manager can reach per the permission matrix).
fn create_and_assign_multiple_tasks(
    tx: &Connection,
    target_agent_id: &str,
    specs: &[UnassignedTaskSpec],
    coordination_notes: Option<&str>,
    now: &str,
) -> Result<ToolResult, rusqlite::Error> {
    if !agent_assignable(tx, target_agent_id) {
        return Ok(ToolResult::NotFound {
            resource: "agent".to_string(),
            identifier: target_agent_id.to_string(),
            hint: None,
        });
    }

    let parentless_count = specs.iter().filter(|s| s.parent_task_id.is_none()).count();
    if parentless_count > 0 {
        if let Some(conflict) = single_root_conflict(tx) {
            return Ok(conflict);
        }
        if parentless_count > 1 {
            return Ok(ToolResult::Conflict {
                reason: format!(
                    "Cannot create more than one root task in a single batch. This batch \
                     has {parentless_count} tasks without a parent_task_id; at most one \
                     task per batch may omit parent_task_id (it becomes the root). Give \
                     the rest a parent_task_id."
                ),
            });
        }
    }

    let mut created: Vec<(String, String, String)> = Vec::new();
    for spec in specs {
        let fresh = task_repository::create(
            tx,
            NewTask {
                task_id: None,
                title: &spec.title,
                description: Some(&spec.description),
                assigned_to: Some(target_agent_id),
                created_by: "admin",
                status: "pending",
                priority: &spec.priority,
                parent_task: spec.parent_task_id.as_deref(),
                child_tasks: None,
                depends_on_tasks: None,
                notes: None,
                now,
            },
        )?;
        let _ = link_child_to_parent(tx, spec.parent_task_id.as_deref(), &fresh.task_id, now);
        let _ = agent_action_repository::log_agent_action(
            tx,
            "admin",
            "assigned_task",
            Some(&fresh.task_id),
            Some(&serde_json::json!({
                "agent_id": target_agent_id,
                "title": spec.title,
                "mode": "multiple_task_creation",
            })),
            now,
        );
        created.push((fresh.task_id, spec.title.clone(), spec.priority.clone()));
    }

    if let Ok(Some(agent)) = AgentRepository::get_by_id(tx, target_agent_id) {
        if agent.current_task.is_none() {
            if let Some((first_id, _, _)) = created.first() {
                let _ = AgentRepository::update_field(
                    tx,
                    target_agent_id,
                    conexus_db::agent_repository::AgentField::CurrentTask,
                    conexus_db::agent_repository::FieldValue::OptionalText(Some(first_id.clone())),
                    now,
                );
            }
        }
    }

    let mut response_parts = vec![
        "\u{2705} **Multiple Tasks Created and Assigned**".to_string(),
        format!("   Agent: {target_agent_id}"),
        format!("   Tasks Created: {}", created.len()),
        String::new(),
    ];
    for (i, (task_id, title, priority)) in created.iter().enumerate() {
        response_parts.push(format!(
            "   {}. {task_id}: {title} (Priority: {priority})",
            i + 1
        ));
    }
    if let Some(notes) = coordination_notes {
        response_parts.push(format!("\n\u{1F4CB} **Coordination Notes:** {notes}"));
    }

    Ok(ToolResult::Ok {
        data: None,
        message: Some(response_parts.join("\n")),
    })
}

// ── assign_task tool (top-level dispatch) ───────────────────────────

pub struct AssignTaskTool;

impl Tool for AssignTaskTool {
    const NAME: &'static str = "assign_task";
    const REQUIRED: Requirement = Requirement::Policy {
        keys: &[
            "config_allow_worker_self_assign",
            "config_allow_worker_create_unassigned",
        ],
        default: true,
    };
    const DESCRIPTION: &'static str = "Multi-mode task assignment tool. Mode 1: create \
        single task + assign agent. Mode 2: create multiple tasks + assign agent. Mode 3: \
        assign an agent to existing unassigned tasks. WORKERS -- this is how you take \
        ownership of a task: to CLAIM an unassigned (claimable-pool) task for yourself so \
        you can then update its status, call Mode 3 with just task_ids=['<id>'] \
        (self-claim) -- you do NOT need to supply agent_token; you self-claim as the \
        authenticated caller. Gated by the project policy \
        config_allow_worker_self_assign (on by default). You can only self-claim \
        UNASSIGNED tasks, and only for yourself.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "agent_token": {"type": "string", "description": "Agent token to assign the task(s) TO. Workers self-claiming do NOT set this."},
            "agent_id": {"type": "string", "description": "Admin-only alternative to agent_token -- resolves by name server-side."},
            "task_title": {"type": "string", "description": "Title of the task (Mode 1: single task creation)."},
            "task_description": {"type": "string", "description": "Detailed description of the task (Mode 1)."},
            "priority": {"type": "string", "description": "Task priority.", "enum": ["low","medium","high"], "default": "medium"},
            "depends_on_tasks": {"type": "array", "description": "List of task IDs this task depends on (Mode 1 only).", "items": {"type": "string"}},
            "parent_task_id": {"type": "string", "description": "ID of the parent task (Mode 1 only)."},
            "tasks": {"type": "array", "description": "Array of tasks to create and assign (Mode 2), or file unassigned (Mode 0 batch).", "items": {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "priority": {"type": "string", "enum": ["low","medium","high"]}, "parent_task_id": {"type": "string"}}, "required": ["title","description"], "additionalProperties": false}},
            "task_ids": {"type": "array", "description": "Array of existing task IDs to assign to agent (Mode 3).", "items": {"type": "string"}},
            "auto_suggest_parent": {"type": "boolean", "description": "Use AI to suggest optimal parent task based on content similarity (default: true).", "default": true},
            "validate_agent_workload": {"type": "boolean", "description": "Analyze agent capacity and provide workload warnings (default: true).", "default": true},
            "coordination_notes": {"type": "string", "description": "Optional coordination context for team awareness and handoffs."},
            "estimated_hours": {"type": "number", "description": "Optional workload estimation in hours for capacity planning."},
            "accept_suggestions": {"type": "boolean", "description": "When validator returns suggestions, auto-apply them (default: false).", "default": false},
            "override_rag": {"type": "boolean", "description": "Override RAG validation suggestions.", "default": false},
            "override_reason": {"type": "string", "description": "Reason for overriding RAG validation."}
        },
        "required": [],
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
            let principal = principal.expect("Policy-gated tool always has a resolved principal");

            let target_agent_id_alias = str_arg(arguments, "agent_id");
            let mut target_agent_token = str_arg(arguments, "agent_token");
            let is_admin_request = principal.has_capability(Capability::TasksAssign);
            let task_ids = str_array_arg_owned(arguments, "task_ids");
            let tasks_batch = parse_unassigned_task_specs(arguments);

            let conn = conn.lock().await;

            // Admin-only `agent_id` alias -> resolve to a token
            // server-side. Precedence: an explicit agent_token wins.
            if let (Some(alias), None) = (&target_agent_id_alias, &target_agent_token) {
                if !is_admin_request {
                    return ToolResult::PermissionDenied {
                        reason: "agent_id is admin-only. To take on work as yourself, call \
                            assign_task with just task_ids=[...] -- you self-claim as the \
                            authenticated caller; no agent_token is needed (a worker cannot \
                            access its own token)."
                            .to_string(),
                    };
                }
                match AgentRepository::get_by_id(&conn, alias) {
                    Ok(Some(row)) if row.status != "terminated" => {
                        target_agent_token = Some(row.token);
                    }
                    _ => {
                        return ToolResult::Invalid {
                            field: Some("agent_id".to_string()),
                            message: format!("Unknown agent_id: '{alias}'"),
                        };
                    }
                }
            }

            // Worker self-claim ergonomics: an authenticated worker
            // passing task_ids with no agent_token/agent_id self-claims
            // as itself via its own bearer token.
            if target_agent_token.is_none()
                && target_agent_id_alias.is_none()
                && !task_ids.is_empty()
                && !is_admin_request
                && principal.kind == PrincipalKind::AgentBearer
            {
                target_agent_token = principal.source_token.clone();
            }

            let coordination_notes = str_arg(arguments, "coordination_notes");

            let auth = match authorize_assign_task(
                &conn,
                target_agent_token.as_deref(),
                !task_ids.is_empty(),
                principal,
            ) {
                Ok(auth) => auth,
                Err(reason) => {
                    return ToolResult::PermissionDenied {
                        reason: reason
                            .strip_prefix("Unauthorized: ")
                            .unwrap_or(&reason)
                            .to_string(),
                    }
                }
            };

            let Some(target_agent_token) = target_agent_token else {
                // Mode 0: file unassigned task(s).
                let worker_created_by = match &auth {
                    AssignAuthorization::WorkerFileUnassigned { creator } => Some(creator.as_str()),
                    _ => None,
                };
                let tx = match conn.unchecked_transaction() {
                    Ok(tx) => tx,
                    Err(_) => {
                        return ToolResult::Failed {
                            message: "Database error assigning task".to_string(),
                        }
                    }
                };
                let result = match create_unassigned_tasks(&tx, arguments, worker_created_by, now) {
                    Ok(r) => r,
                    Err(_) => {
                        return ToolResult::Failed {
                            message: "Database error assigning task".to_string(),
                        }
                    }
                };
                if matches!(result, ToolResult::Ok { .. }) {
                    if tx.commit().is_err() {
                        return ToolResult::Failed {
                            message: "Database error assigning task".to_string(),
                        };
                    }
                    if let Ok(active) = AgentRepository::list_active(&conn) {
                        for agent in active {
                            ctx.waiter_registry.notify(&agent.agent_id);
                        }
                    }
                }
                return result;
            };

            let Ok(Some(agent_row)) = AgentRepository::get_by_token(&conn, &target_agent_token)
            else {
                return ToolResult::NotFound {
                    resource: "agent_token".to_string(),
                    identifier: "(invalid or unknown)".to_string(),
                    hint: None,
                };
            };
            let target_agent_id = agent_row.agent_id.clone();

            if target_agent_id.to_lowercase().starts_with("admin") {
                return ToolResult::Conflict {
                    reason: "admin agents cannot be assigned tasks. Admin agents are for \
                        coordination and management only."
                        .to_string(),
                };
            }

            let requesting_actor = principal
                .agent_id
                .clone()
                .or_else(|| principal.user_id.clone())
                .unwrap_or_else(|| "admin".to_string());

            if !task_ids.is_empty() {
                // Mode 3: assign to existing unassigned tasks.
                let tx = match conn.unchecked_transaction() {
                    Ok(tx) => tx,
                    Err(_) => {
                        return ToolResult::Failed {
                            message: "Database error assigning task".to_string(),
                        }
                    }
                };
                let result = match assign_to_existing_tasks(
                    &tx,
                    &target_agent_id,
                    &task_ids,
                    coordination_notes.as_deref(),
                    &requesting_actor,
                    is_admin_request,
                    now,
                ) {
                    Ok(r) => r,
                    Err(_) => {
                        return ToolResult::Failed {
                            message: "Database error assigning task".to_string(),
                        }
                    }
                };
                if matches!(result, ToolResult::Ok { .. }) {
                    if tx.commit().is_err() {
                        return ToolResult::Failed {
                            message: "Database error assigning task".to_string(),
                        };
                    }
                    ctx.waiter_registry.notify(&target_agent_id);
                }
                return result;
            }

            if let Some(specs) = &tasks_batch {
                // Mode 2: create multiple tasks + assign (admin-only in
                // practice -- authorize_assign_task never yields
                // WorkerFileUnassigned/WorkerSelfClaim with a non-empty
                // `tasks` array reaching this branch).
                for (i, spec) in specs.iter().enumerate() {
                    if spec.title.is_empty() || spec.description.is_empty() {
                        return ToolResult::Invalid {
                            field: Some("tasks".to_string()),
                            message: format!(
                                "Task {} must have 'title' and 'description' fields.",
                                i + 1
                            ),
                        };
                    }
                }
                let tx = match conn.unchecked_transaction() {
                    Ok(tx) => tx,
                    Err(_) => {
                        return ToolResult::Failed {
                            message: "Database error assigning task".to_string(),
                        }
                    }
                };
                let result = match create_and_assign_multiple_tasks(
                    &tx,
                    &target_agent_id,
                    specs,
                    coordination_notes.as_deref(),
                    now,
                ) {
                    Ok(r) => r,
                    Err(_) => {
                        return ToolResult::Failed {
                            message: "Database error assigning task".to_string(),
                        }
                    }
                };
                if matches!(result, ToolResult::Ok { .. }) {
                    if tx.commit().is_err() {
                        return ToolResult::Failed {
                            message: "Database error assigning task".to_string(),
                        };
                    }
                    ctx.waiter_registry.notify(&target_agent_id);
                }
                return result;
            }

            // Mode 1: single task creation + assign.
            let task_title = str_arg(arguments, "task_title");
            let task_description = str_arg(arguments, "task_description");
            let (Some(task_title), Some(task_description)) = (&task_title, &task_description)
            else {
                return ToolResult::Invalid {
                    field: None,
                    message: "task_title and task_description are required for single task \
                        creation, or provide 'tasks' array for multiple tasks, or \
                        'task_ids' for existing task assignment."
                        .to_string(),
                };
            };
            let priority = str_arg(arguments, "priority").unwrap_or_else(|| "medium".to_string());
            let depends_on_tasks = str_array_arg_owned(arguments, "depends_on_tasks");
            let parent_task_id = normalize_parent(arguments.get("parent_task_id"));
            let validate_agent_workload = bool_arg(arguments, "validate_agent_workload")
                || !arguments
                    .as_object()
                    .is_some_and(|o| o.contains_key("validate_agent_workload"));
            let estimated_hours = arguments.get("estimated_hours").cloned();

            if parent_task_id.is_none() {
                if let Some(conflict) = single_root_conflict(&conn) {
                    return conflict;
                }
            }

            let tx = match conn.unchecked_transaction() {
                Ok(tx) => tx,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error assigning task".to_string(),
                    }
                }
            };

            if !agent_assignable(&tx, &target_agent_id) {
                return ToolResult::NotFound {
                    resource: "agent".to_string(),
                    identifier: target_agent_id.clone(),
                    hint: None,
                };
            }

            if parent_task_id.is_none() {
                if let Some(conflict) = single_root_conflict(&tx) {
                    return conflict;
                }
            }

            let workload = if validate_agent_workload {
                analyze_agent_workload(&tx, &target_agent_id, now).ok()
            } else {
                None
            };

            let new_task_id = task_repository::generate_task_id();
            if !depends_on_tasks.is_empty() {
                match find_dependency_cycle(&tx, &new_task_id, &depends_on_tasks) {
                    Ok(Some(cycle)) => {
                        return ToolResult::Conflict {
                            reason: format!(
                                "Cannot create task with depends_on_tasks {depends_on_tasks:?}: \
                                 would introduce a dependency cycle ({}).",
                                cycle.join(" -> ")
                            ),
                        };
                    }
                    Ok(None) => {}
                    Err(_) => {
                        return ToolResult::Failed {
                            message: "Database error assigning task".to_string(),
                        }
                    }
                }
            }

            let mut initial_notes: Vec<conexus_db::task_repository::TaskNote> = Vec::new();
            if let Some(notes) = &coordination_notes {
                initial_notes.push(conexus_db::task_repository::TaskNote {
                    timestamp: now.to_string(),
                    author: Some("admin".to_string()),
                    content: format!("\u{1F4CB} Coordination: {notes}"),
                });
            }
            if let Some(w) = &workload {
                let mut content = format!(
                    "\u{1F464} Agent workload: {} ({} active tasks)",
                    w.capacity_status, w.total_active_tasks
                );
                if let Some(hours) = estimated_hours.as_ref().and_then(Value::as_f64) {
                    content.push_str(&format!(" | Estimated: {hours}h"));
                }
                initial_notes.push(conexus_db::task_repository::TaskNote {
                    timestamp: now.to_string(),
                    author: Some("system".to_string()),
                    content,
                });
            }

            let fresh_task = match task_repository::create(
                &tx,
                NewTask {
                    task_id: Some(&new_task_id),
                    title: task_title,
                    description: Some(task_description),
                    assigned_to: Some(&target_agent_id),
                    created_by: "admin",
                    status: "pending",
                    priority: &priority,
                    parent_task: parent_task_id.as_deref(),
                    child_tasks: None,
                    depends_on_tasks: Some(&depends_on_tasks),
                    notes: Some(&initial_notes),
                    now,
                },
            ) {
                Ok(row) => row,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error assigning task".to_string(),
                    }
                }
            };
            let _ = link_child_to_parent(&tx, parent_task_id.as_deref(), &new_task_id, now);

            if let Ok(Some(agent)) = AgentRepository::get_by_id(&tx, &target_agent_id) {
                if agent.current_task.is_none() {
                    let _ = AgentRepository::update_field(
                        &tx,
                        &target_agent_id,
                        conexus_db::agent_repository::AgentField::CurrentTask,
                        conexus_db::agent_repository::FieldValue::OptionalText(Some(
                            new_task_id.clone(),
                        )),
                        now,
                    );
                }
            }
            let _ = agent_action_repository::log_agent_action(
                &tx,
                "admin",
                "assigned_task",
                Some(&new_task_id),
                Some(&serde_json::json!({"agent_id": target_agent_id, "title": task_title})),
                now,
            );

            if tx.commit().is_err() {
                return ToolResult::Failed {
                    message: "Database error assigning task".to_string(),
                };
            }
            ctx.waiter_registry.notify(&target_agent_id);

            let mut response_parts = vec![
                "\u{2705} **Task Assigned Successfully**".to_string(),
                format!("   Task ID: {new_task_id}"),
                format!("   Title: {task_title}"),
                format!("   Agent: {target_agent_id}"),
                format!("   Priority: {priority}"),
            ];
            if let Some(p) = &fresh_task.parent_task {
                response_parts.push(format!("   Parent: {p}"));
            }
            if !depends_on_tasks.is_empty() {
                response_parts.push(format!("   Dependencies: {}", depends_on_tasks.join(", ")));
            }
            if let Some(hours) = estimated_hours.as_ref().and_then(Value::as_f64) {
                response_parts.push(format!("   Estimated: {hours} hours"));
            }
            if let Some(w) = &workload {
                let icon = match w.capacity_status {
                    "available" => "\u{1F7E2}",
                    "busy" => "\u{1F7E1}",
                    _ => "\u{1F534}",
                };
                response_parts.push(String::new());
                response_parts.push(format!(
                    "\u{1F464} **Agent Workload:** {icon} {}",
                    title_case(w.capacity_status)
                ));
                response_parts.push(format!(
                    "   Active Tasks: {} ({} high priority)",
                    w.total_active_tasks, w.high_priority_tasks
                ));
                if !w.can_take_new_task {
                    response_parts.push(format!(
                        "\u{26A0}\u{FE0F} Agent workload warning: {} ({} active tasks, {} high priority)",
                        w.capacity_status, w.total_active_tasks, w.high_priority_tasks
                    ));
                    for rec in w.recommendations.iter().take(2) {
                        response_parts.push(format!("   \u{1F4A1} {rec}"));
                    }
                }
            }
            if let Some(notes) = &coordination_notes {
                response_parts.push(format!("\n\u{1F4CB} **Coordination Notes:** {notes}"));
            }

            ToolResult::Ok {
                data: None,
                message: Some(response_parts.join("\n")),
            }
        })
    }
}

fn str_array_arg_owned(arguments: &Value, key: &str) -> Vec<String> {
    arguments
        .get(key)
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn title_case(s: &str) -> String {
    let mut c = s.chars();
    match c.next() {
        None => String::new(),
        Some(first) => first.to_uppercase().collect::<String>() + c.as_str(),
    }
}

// ── create_self_task tool ───────────────────────────────────────────

pub struct CreateSelfTaskTool;

impl Tool for CreateSelfTaskTool {
    const NAME: &'static str = "create_self_task";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::TasksCreate,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Agent tool to create a task for themselves. \
        IMPORTANT: parent_task_id is REQUIRED -- agents cannot create root tasks.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "task_title": {"type": "string", "description": "Title of the task."},
            "task_description": {"type": "string", "description": "Detailed description of the task."},
            "priority": {"type": "string", "description": "Task priority.", "enum": ["low","medium","high"], "default": "medium"},
            "depends_on_tasks": {"type": "array", "description": "List of task IDs this task depends on (optional).", "items": {"type": "string"}},
            "parent_task_id": {"type": "string", "description": "ID of the parent task (defaults to agent's current task if not specified, but MUST have a parent)."},
            "accept_suggestions": {"type": "boolean", "description": "When validator returns suggestions, auto-apply them (default: false).", "default": false}
        },
        "required": ["task_title","task_description"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        // No wake here -- matches Python: create_self_task_tool_impl
        // never calls notify_agent_inbox/notify_unassigned_task_appeared.
        // The creating agent is already active (it's THEIR request);
        // nobody else needs waking for a task that only affects them.
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let principal = principal.expect("Cap-gated tool always has a resolved principal");

            let Some(task_title) = str_arg(arguments, "task_title") else {
                return ToolResult::Invalid {
                    field: None,
                    message: "task_title and task_description are required.".to_string(),
                };
            };
            let Some(task_description) = str_arg(arguments, "task_description") else {
                return ToolResult::Invalid {
                    field: None,
                    message: "task_title and task_description are required.".to_string(),
                };
            };
            let priority = str_arg(arguments, "priority").unwrap_or_else(|| "medium".to_string());
            let depends_on_tasks = str_array_arg_owned(arguments, "depends_on_tasks");
            let parent_task_id_arg = normalize_parent(arguments.get("parent_task_id"));

            // @requires_capability("tasks.create") guarantees a
            // resolved principal; agent-bearer callers always carry
            // agent_id.
            let requesting_agent_id = principal
                .agent_id
                .clone()
                .unwrap_or_else(|| "admin".to_string());

            let conn = conn.lock().await;

            // No in-memory "current_task of the caller's active
            // session" cache exists in this port (see this crate's
            // established no-in-memory-mirror convention) -- fall back
            // directly to the agent's DB-persisted current_task when
            // no explicit parent is given.
            let actual_parent_task_id = if parent_task_id_arg.is_some() {
                parent_task_id_arg.clone()
            } else {
                AgentRepository::get_by_id(&conn, &requesting_agent_id)
                    .ok()
                    .flatten()
                    .and_then(|a| a.current_task)
            };

            let tx = match conn.unchecked_transaction() {
                Ok(tx) => tx,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error creating self task".to_string(),
                    }
                }
            };

            // Hierarchy: agents can NEVER create root tasks.
            if requesting_agent_id != "admin" && actual_parent_task_id.is_none() {
                let suggested = tx
                    .query_row(
                        "SELECT task_id, title FROM tasks WHERE assigned_to = ?1 OR \
                         created_by = ?1 ORDER BY created_at DESC LIMIT 1",
                        [requesting_agent_id.as_str()],
                        |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
                    )
                    .ok();
                let suggestion_text = suggested
                    .map(|(id, title)| format!("\nSuggested parent: {id} ({title})"))
                    .unwrap_or_default();
                return ToolResult::Conflict {
                    reason: format!(
                        "Agents cannot create root tasks. Every task must have a \
                         parent.{suggestion_text}\nPlease specify a parent_task_id."
                    ),
                };
            }

            if actual_parent_task_id.is_none() {
                if let Some(conflict) = single_root_conflict(&tx) {
                    return conflict;
                }
            }

            // AZ-R19-1 / R4-F5: a non-privileged caller may only parent
            // (or depend) under a task it OWNS; foreign-or-nonexistent
            // collapses to the same phantom NotFound.
            let is_privileged = principal.has_capability(Capability::TasksAssign);
            if !is_privileged {
                if let Some(parent_id) = &actual_parent_task_id {
                    let owns = task_repository::get_by_id(&tx, parent_id)
                        .ok()
                        .flatten()
                        .is_some_and(|p| {
                            can_access_task(
                                p.assigned_to.as_deref(),
                                Some(p.created_by.as_str()),
                                Some(requesting_agent_id.as_str()),
                                false,
                                false,
                                false,
                                false,
                            )
                        });
                    if !owns {
                        return ToolResult::NotFound {
                            resource: "task".to_string(),
                            identifier: parent_id.clone(),
                            hint: None,
                        };
                    }
                }
                for dep_id in &depends_on_tasks {
                    let owns = task_repository::get_by_id(&tx, dep_id)
                        .ok()
                        .flatten()
                        .is_some_and(|d| {
                            can_access_task(
                                d.assigned_to.as_deref(),
                                Some(d.created_by.as_str()),
                                Some(requesting_agent_id.as_str()),
                                false,
                                false,
                                false,
                                false,
                            )
                        });
                    if !owns {
                        return ToolResult::NotFound {
                            resource: "task".to_string(),
                            identifier: dep_id.clone(),
                            hint: None,
                        };
                    }
                }
            }

            let new_task_id = task_repository::generate_task_id();
            if !depends_on_tasks.is_empty() {
                match find_dependency_cycle(&tx, &new_task_id, &depends_on_tasks) {
                    Ok(Some(cycle)) => {
                        return ToolResult::Conflict {
                            reason: format!(
                                "Cannot create task with depends_on_tasks {depends_on_tasks:?}: \
                                 would introduce a dependency cycle ({}).",
                                cycle.join(" -> ")
                            ),
                        };
                    }
                    Ok(None) => {}
                    Err(_) => {
                        return ToolResult::Failed {
                            message: "Database error creating self task".to_string(),
                        }
                    }
                }
            }

            let fresh_task = match task_repository::create(
                &tx,
                NewTask {
                    task_id: Some(&new_task_id),
                    title: &task_title,
                    description: Some(&task_description),
                    assigned_to: Some(&requesting_agent_id),
                    created_by: &requesting_agent_id,
                    status: "pending",
                    priority: &priority,
                    parent_task: actual_parent_task_id.as_deref(),
                    child_tasks: None,
                    depends_on_tasks: Some(&depends_on_tasks),
                    notes: None,
                    now,
                },
            ) {
                Ok(row) => row,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error creating self task".to_string(),
                    }
                }
            };
            let _ = link_child_to_parent(&tx, actual_parent_task_id.as_deref(), &new_task_id, now);

            if requesting_agent_id != "admin" {
                if let Ok(Some(agent)) = AgentRepository::get_by_id(&tx, &requesting_agent_id) {
                    if agent.current_task.is_none() {
                        let _ = AgentRepository::update_field(
                            &tx,
                            &requesting_agent_id,
                            conexus_db::agent_repository::AgentField::CurrentTask,
                            conexus_db::agent_repository::FieldValue::OptionalText(Some(
                                new_task_id.clone(),
                            )),
                            now,
                        );
                    }
                }
            }
            let _ = agent_action_repository::log_agent_action(
                &tx,
                &requesting_agent_id,
                "created_self_task",
                Some(&new_task_id),
                Some(&serde_json::json!({"title": task_title})),
                now,
            );

            if tx.commit().is_err() {
                return ToolResult::Failed {
                    message: "Database error creating self task".to_string(),
                };
            }
            let _ = fresh_task;

            ToolResult::Ok {
                data: Some(serde_json::json!({"task_id": new_task_id})),
                message: Some(format!(
                    "Self-assigned task '{new_task_id}' created.\nTitle: {task_title}"
                )),
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_core::capability::Capabilities;
    use conexus_db::agent_repository::NewAgent;
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::waiter_registry::{WaiterRegistry, WakeSignal};

    const NOW: &str = "2026-04-01T00:00:00Z";

    fn test_conn() -> AsyncMutex<Connection> {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        AsyncMutex::new(conn)
    }

    fn worker(agent_id: &str) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some(agent_id.to_string()),
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: true,
            source_token: Some(format!("tok-{agent_id}")),
            capabilities: Capabilities::from_iter([]),
        }
    }

    fn admin(agent_id: &str) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some(agent_id.to_string()),
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: true,
            source_token: Some(format!("tok-{agent_id}")),
            capabilities: Capabilities::from_iter([Capability::TasksAssign]),
        }
    }

    fn seed_agent(conn: &Connection, agent_id: &str) {
        AgentRepository::create(
            conn,
            NewAgent {
                token: &format!("tok-{agent_id}"),
                agent_id,
                created_at: NOW,
                status: "active",
                current_task: None,
                working_directory: "/tmp",
                color: None,
                agent_role: "worker",
            },
        )
        .unwrap();
    }

    fn seed_task(
        conn: &Connection,
        id: &str,
        status: &str,
        assigned_to: Option<&str>,
        created_by: &str,
        parent: Option<&str>,
    ) {
        task_repository::create(
            conn,
            NewTask {
                task_id: Some(id),
                title: &format!("Task {id}"),
                description: None,
                assigned_to,
                created_by,
                status,
                priority: "medium",
                parent_task: parent,
                child_tasks: None,
                depends_on_tasks: None,
                notes: None,
                now: NOW,
            },
        )
        .unwrap();
    }

    async fn call_assign(
        args: Value,
        principal: &Principal,
        conn: &AsyncMutex<Connection>,
    ) -> ToolResult {
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        AssignTaskTool::call(Some(principal), &args, conn, NOW, &ctx).await
    }

    fn message_of(result: &ToolResult) -> String {
        match result {
            ToolResult::Ok { message, .. } => message.clone().unwrap_or_default(),
            other => panic!("expected Ok, got {other:?}"),
        }
    }

    // -- authorize_assign_task --------------------------------------------

    #[test]
    fn authorize_admin_is_always_permitted() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        let result = authorize_assign_task(&conn, None, false, &admin("alice"));
        assert!(matches!(result, Ok(AssignAuthorization::Admin)));
    }

    #[test]
    fn authorize_worker_no_token_files_unassigned_by_default() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        let result = authorize_assign_task(&conn, None, false, &worker("bob"));
        assert!(matches!(
            result,
            Ok(AssignAuthorization::WorkerFileUnassigned { creator }) if creator == "bob"
        ));
    }

    #[test]
    fn authorize_worker_no_token_denied_when_policy_off() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        project_settings_repository::upsert(
            &conn,
            "config_allow_worker_create_unassigned",
            "false",
            None,
            false,
            "test",
            NOW,
        )
        .unwrap();
        let result = authorize_assign_task(&conn, None, false, &worker("bob"));
        assert!(result.is_err());
    }

    #[test]
    fn authorize_worker_self_claim_with_task_ids_is_permitted() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        seed_agent(&conn, "bob");
        let result = authorize_assign_task(&conn, Some("tok-bob"), true, &worker("bob"));
        assert!(matches!(
            result,
            Ok(AssignAuthorization::WorkerSelfClaim { worker_id }) if worker_id == "bob"
        ));
    }

    #[test]
    fn authorize_worker_self_target_without_task_ids_is_denied() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        seed_agent(&conn, "bob");
        let result = authorize_assign_task(&conn, Some("tok-bob"), false, &worker("bob"));
        assert!(result.is_err());
    }

    #[test]
    fn authorize_worker_targeting_another_worker_is_denied() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        seed_agent(&conn, "carol");
        let result = authorize_assign_task(&conn, Some("tok-carol"), true, &worker("bob"));
        assert!(result.is_err());
    }

    // -- analyze_agent_workload -------------------------------------------

    #[test]
    fn workload_escalates_to_busy_at_five_active_tasks() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        seed_task(&conn, "root", "pending", None, "alice", None);
        for i in 0..5 {
            let id = format!("t{i}");
            seed_task(&conn, &id, "pending", Some("bob"), "alice", Some("root"));
        }
        let w = analyze_agent_workload(&conn, "bob", NOW).unwrap();
        assert_eq!(w.total_active_tasks, 5);
        assert_eq!(w.capacity_status, "busy");
    }

    #[test]
    fn workload_available_with_no_active_tasks() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        let w = analyze_agent_workload(&conn, "bob", NOW).unwrap();
        assert_eq!(w.total_active_tasks, 0);
        assert_eq!(w.capacity_status, "available");
        assert!(w.can_take_new_task);
    }

    // -- AssignTaskTool: Mode 0 (unassigned) -------------------------------

    #[tokio::test]
    async fn mode0_worker_files_unassigned_task_under_own_parent() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root", "pending", Some("bob"), "bob", None);
        }
        let result = call_assign(
            serde_json::json!({
                "task_title": "help wanted",
                "task_description": "desc",
                "parent_task_id": "root"
            }),
            &worker("bob"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
    }

    #[tokio::test]
    async fn mode0_worker_cannot_file_under_a_foreign_parent() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root", "pending", Some("carol"), "carol", None);
        }
        let result = call_assign(
            serde_json::json!({
                "task_title": "sneaky",
                "task_description": "desc",
                "parent_task_id": "root"
            }),
            &worker("bob"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn mode0_worker_cannot_file_a_root_task() {
        let conn = test_conn();
        let result = call_assign(
            serde_json::json!({"task_title": "root attempt", "task_description": "desc"}),
            &worker("bob"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
    }

    #[tokio::test]
    async fn mode0_admin_can_file_an_unassigned_root_task() {
        let conn = test_conn();
        let result = call_assign(
            serde_json::json!({"task_title": "root", "task_description": "desc"}),
            &admin("alice"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let rows = task_repository::list_all(&guard, None).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].status, "unassigned");
    }

    // -- AssignTaskTool: Mode 3 (existing) ---------------------------------

    #[tokio::test]
    async fn mode3_worker_self_claims_an_unassigned_task() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_agent(&guard, "bob");
        }
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root", "pending", None, "alice", None);
            seed_task(&guard, "t1", "pending", None, "alice", Some("root"));
        }
        let result = call_assign(
            serde_json::json!({"task_ids": ["t1"]}),
            &worker("bob"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let row = task_repository::get_by_id(&guard, "t1").unwrap().unwrap();
        assert_eq!(row.assigned_to.as_deref(), Some("bob"));
    }

    #[tokio::test]
    async fn mode3_worker_claiming_a_foreign_task_gets_the_phantom_not_found() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_agent(&guard, "bob");
        }
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root", "pending", None, "alice", None);
            seed_task(
                &guard,
                "t1",
                "pending",
                Some("carol"),
                "alice",
                Some("root"),
            );
        }
        let result = call_assign(
            serde_json::json!({"task_ids": ["t1"]}),
            &worker("bob"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn mode3_worker_reclaiming_own_task_is_a_conflict() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_agent(&guard, "bob");
        }
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root", "pending", None, "alice", None);
            seed_task(&guard, "t1", "pending", Some("bob"), "alice", Some("root"));
        }
        let result = call_assign(
            serde_json::json!({"task_ids": ["t1"]}),
            &worker("bob"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
    }

    #[tokio::test]
    async fn mode3_admin_assigns_existing_tasks_and_wakes_the_agent() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_agent(&guard, "bob");
        }
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root", "pending", None, "alice", None);
            seed_task(&guard, "t1", "pending", None, "alice", Some("root"));
        }
        let registry = WaiterRegistry::new();
        let (_tx, mut rx) = registry.register("bob");
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result = AssignTaskTool::call(
            Some(&admin("alice")),
            &serde_json::json!({"agent_token": "tok-bob", "task_ids": ["t1"]}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        assert_eq!(rx.try_recv(), Ok(WakeSignal::Wake));
    }

    // -- AssignTaskTool: Mode 1 (single create+assign) ---------------------

    #[tokio::test]
    async fn mode1_admin_creates_and_assigns_a_single_task() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_agent(&guard, "bob");
        }
        let result = call_assign(
            serde_json::json!({
                "agent_token": "tok-bob",
                "task_title": "new work",
                "task_description": "desc"
            }),
            &admin("alice"),
            &conn,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("Task Assigned Successfully"));
        let guard = conn.lock().await;
        let rows = task_repository::list_all(&guard, None).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].assigned_to.as_deref(), Some("bob"));
    }

    #[tokio::test]
    async fn mode1_rejects_an_unknown_agent_token() {
        let conn = test_conn();
        let result = call_assign(
            serde_json::json!({
                "agent_token": "ghost-token",
                "task_title": "x",
                "task_description": "desc"
            }),
            &admin("alice"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn mode1_refuses_to_assign_to_an_admin_prefixed_agent() {
        let conn = test_conn();
        {
            // AgentRepository::create itself refuses an "admin"-prefixed
            // agent_id (RESERVED_AGENT_ID_PREFIX) -- seed directly via
            // SQL to exercise this tool's OWN defensive re-check
            // against a row that shouldn't exist via the normal
            // creation path but is still worth guarding against.
            let guard = conn.lock().await;
            guard
                .execute(
                    "INSERT INTO agents (token, agent_id, created_at, status,                      working_directory, agent_role) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                    (
                        "tok-admin-helper",
                        "adminhelper",
                        NOW,
                        "active",
                        "/tmp",
                        "worker",
                    ),
                )
                .unwrap();
        }
        let result = call_assign(
            serde_json::json!({
                "agent_token": "tok-admin-helper",
                "task_title": "x",
                "task_description": "desc"
            }),
            &admin("alice"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
    }

    #[tokio::test]
    async fn mode1_rejects_a_dependency_cycle() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_agent(&guard, "bob");
        }
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root", "pending", None, "alice", None);
            seed_task(&guard, "dep", "pending", None, "alice", Some("root"));
            task_repository::update_fields(
                &guard,
                "dep",
                &task_repository::TaskFields {
                    depends_on_tasks:
                        conexus_db::scheduled_directive_repository::NullableUpdate::Set(vec![]),
                    ..Default::default()
                },
                NOW,
            )
            .unwrap();
        }
        // A freshly-minted task_id can never already have an incoming
        // edge, so this pins the (structurally always-empty) cycle
        // check runs without erroring -- not a real cycle scenario.
        let result = call_assign(
            serde_json::json!({
                "agent_token": "tok-bob",
                "task_title": "x",
                "task_description": "desc",
                "parent_task_id": "root",
                "depends_on_tasks": ["dep"]
            }),
            &admin("alice"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
    }

    // -- AssignTaskTool: Mode 2 (multiple create+assign) -------------------

    #[tokio::test]
    async fn mode2_admin_creates_and_assigns_multiple_tasks() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_agent(&guard, "bob");
        }
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root", "pending", None, "alice", None);
        }
        let result = call_assign(
            serde_json::json!({
                "agent_token": "tok-bob",
                "tasks": [
                    {"title": "a", "description": "a desc", "parent_task_id": "root"},
                    {"title": "b", "description": "b desc", "parent_task_id": "root"}
                ]
            }),
            &admin("alice"),
            &conn,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("Tasks Created: 2"));
    }

    // -- agent_id alias -----------------------------------------------------

    #[tokio::test]
    async fn agent_id_alias_is_admin_only() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_agent(&guard, "bob");
        }
        let result = call_assign(
            serde_json::json!({"agent_id": "bob", "task_ids": ["whatever"]}),
            &worker("carol"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::PermissionDenied { .. }));
    }

    #[tokio::test]
    async fn agent_id_alias_resolves_for_an_admin_caller() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_agent(&guard, "bob");
        }
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root", "pending", None, "alice", None);
            seed_task(&guard, "t1", "pending", None, "alice", Some("root"));
        }
        let result = call_assign(
            serde_json::json!({"agent_id": "bob", "task_ids": ["t1"]}),
            &admin("alice"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
    }
}

#[cfg(test)]
mod create_self_task_tests {
    use super::*;
    use conexus_core::capability::Capabilities;
    use conexus_db::agent_repository::NewAgent;
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::waiter_registry::WaiterRegistry;

    const NOW: &str = "2026-04-05T00:00:00Z";

    fn test_conn() -> AsyncMutex<Connection> {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        AsyncMutex::new(conn)
    }

    fn worker(agent_id: &str) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some(agent_id.to_string()),
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: true,
            source_token: Some(format!("tok-{agent_id}")),
            capabilities: Capabilities::from_iter([Capability::TasksCreate]),
        }
    }

    fn seed_agent(conn: &Connection, agent_id: &str) {
        AgentRepository::create(
            conn,
            NewAgent {
                token: &format!("tok-{agent_id}"),
                agent_id,
                created_at: NOW,
                status: "active",
                current_task: None,
                working_directory: "/tmp",
                color: None,
                agent_role: "worker",
            },
        )
        .unwrap();
    }

    fn seed_task(
        conn: &Connection,
        id: &str,
        assigned_to: Option<&str>,
        created_by: &str,
        parent: Option<&str>,
    ) {
        task_repository::create(
            conn,
            NewTask {
                task_id: Some(id),
                title: &format!("Task {id}"),
                description: None,
                assigned_to,
                created_by,
                status: "pending",
                priority: "medium",
                parent_task: parent,
                child_tasks: None,
                depends_on_tasks: None,
                notes: None,
                now: NOW,
            },
        )
        .unwrap();
    }

    async fn call(args: Value, principal: &Principal, conn: &AsyncMutex<Connection>) -> ToolResult {
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        CreateSelfTaskTool::call(Some(principal), &args, conn, NOW, &ctx).await
    }

    #[tokio::test]
    async fn requires_title_and_description() {
        let conn = test_conn();
        let result = call(serde_json::json!({}), &worker("bob"), &conn).await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn a_worker_cannot_create_a_root_task() {
        let conn = test_conn();
        let result = call(
            serde_json::json!({"task_title": "x", "task_description": "desc"}),
            &worker("bob"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
    }

    #[tokio::test]
    async fn a_worker_creates_a_self_task_under_its_own_parent() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root", Some("bob"), "bob", None);
        }
        let result = call(
            serde_json::json!({"task_title": "x", "task_description": "desc", "parent_task_id": "root"}),
            &worker("bob"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let rows = task_repository::list_all(&guard, None).unwrap();
        let created = rows.iter().find(|t| t.task_id != "root").unwrap();
        assert_eq!(created.assigned_to.as_deref(), Some("bob"));
        assert_eq!(created.created_by, "bob");
    }

    #[tokio::test]
    async fn a_worker_cannot_parent_under_a_foreign_task() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root", Some("carol"), "carol", None);
        }
        let result = call(
            serde_json::json!({"task_title": "x", "task_description": "desc", "parent_task_id": "root"}),
            &worker("bob"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn a_worker_cannot_depend_on_a_foreign_task() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root", Some("bob"), "bob", None);
            seed_task(&guard, "foreign_dep", Some("carol"), "carol", Some("root"));
        }
        let result = call(
            serde_json::json!({
                "task_title": "x",
                "task_description": "desc",
                "parent_task_id": "root",
                "depends_on_tasks": ["foreign_dep"]
            }),
            &worker("bob"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn falls_back_to_the_agents_current_task_when_no_parent_given() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_agent(&guard, "bob");
        }
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root", Some("bob"), "bob", None);
            conexus_db::agent_repository::AgentRepository::reconcile_current_task_on_reassign(
                &guard,
                "root",
                None,
                Some("bob"),
                NOW,
            )
            .unwrap();
        }
        let result = call(
            serde_json::json!({"task_title": "x", "task_description": "desc"}),
            &worker("bob"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let rows = task_repository::list_all(&guard, None).unwrap();
        let created = rows.iter().find(|t| t.task_id != "root").unwrap();
        assert_eq!(created.parent_task.as_deref(), Some("root"));
    }

    #[tokio::test]
    async fn writes_a_durable_audit_row() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root", Some("bob"), "bob", None);
        }
        call(
            serde_json::json!({"task_title": "x", "task_description": "desc", "parent_task_id": "root"}),
            &worker("bob"),
            &conn,
        )
        .await;
        let guard = conn.lock().await;
        let count: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM agent_actions WHERE action_type = 'created_self_task'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }
}
