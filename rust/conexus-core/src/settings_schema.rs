//! Settings-schema registry — the single source of truth for every
//! per-project `config_*` setting (ADR-0018). Faithful, verbatim port
//! of `agent_mcp/core/settings_schema.py`'s `SETTINGS_SCHEMA` tuple.
//!
//! Every registered setting is operator-tier: the `config_*` namespace
//! is writable by any confirmed operator (`Capability::SystemConfigWrite`
//! is the enforcer, ported separately in `conexus-tools`). Per
//! Guiding Principle 4 of the migration plan (`prancy-napping-pie.md`),
//! this stays a hand-written registry consumed by `GET
//! /api/settings-schema`, NOT a `specta`/`ts-rs` derive target — the
//! `specta` pattern this migration uses elsewhere is for domain
//! structs, and extending it here would create a second generated-
//! types pipeline settings could drift from, which ADR-0018 exists
//! specifically to prevent.
//!
//! `default` is [`SettingDefault`], a small const-representable enum,
//! rather than `serde_json::Value` directly (Python's
//! `SettingSpec.default: object` is genuinely heterogeneous across
//! entries -- bool for switches, int for durations) -- `Value` isn't
//! const-constructible, so it can't appear inside the `static` array
//! below; `SettingDefault` serializes to the identical wire shape.

use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum SettingType {
    Bool,
    Int,
    String,
    Secret,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum SettingTier {
    Operator,
    Sysadmin,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SettingGroup {
    WorkerPermissions,
    EventLoop,
    Retention,
    AgentProfiles,
    Scheduling,
    Delivery,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SettingWidget {
    Switch,
    IntDays,
    IntMs,
    IntDuration,
    Url,
    Secret,
    SecretPath,
    Template,
}

/// A setting's default value. Every real entry today is `Bool` or
/// `Int` (confirmed against the actual Python schema -- no
/// `SettingType::String`/`Secret` entry has shipped a default yet); a
/// small const-representable enum, rather than `serde_json::Value`
/// directly, because `Value::Number(N.into())` isn't a `const fn` and
/// so can't appear inside the `static` array below.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SettingDefault {
    Bool(bool),
    Int(i64),
}

impl From<SettingDefault> for serde_json::Value {
    fn from(d: SettingDefault) -> Self {
        match d {
            SettingDefault::Bool(b) => serde_json::Value::Bool(b),
            SettingDefault::Int(n) => serde_json::Value::Number(n.into()),
        }
    }
}

impl Serialize for SettingDefault {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            SettingDefault::Bool(b) => serializer.serialize_bool(*b),
            SettingDefault::Int(n) => serializer.serialize_i64(*n),
        }
    }
}

/// Schema for one per-project `config_*` setting. Port of Python's
/// `SettingSpec` dataclass, field-for-field.
#[derive(Debug, Clone)]
pub struct SettingSpec {
    pub key: &'static str,
    pub r#type: SettingType,
    pub default: SettingDefault,
    pub tier: SettingTier,
    pub group: SettingGroup,
    pub title: &'static str,
    pub description: &'static str,
    pub widget: Option<SettingWidget>,
}

/// The full settings-schema registry, in the exact order Python
/// declares it (wire-order is part of the observable contract — the
/// dashboard renders groups in this order).
pub static SETTINGS_SCHEMA: &[SettingSpec] = &[
    SettingSpec {
        key: "config_allow_worker_to_worker",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::WorkerPermissions,
        title: "Allow worker-to-worker messaging",
        description: "When on (default), workers and managers may use send_agent_message to message any agent. When off, direct agent-to-agent messaging is disabled for them entirely — the send_agent_message tool is hidden and they must use request_assistance to escalate to an admin.",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_allow_worker_self_assign",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::WorkerPermissions,
        title: "Allow workers to self-assign tasks",
        description: "When on (default), workers may call assign_task using their own agent_token. When off, only the admin may assign tasks.",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_allow_worker_create_unassigned",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::WorkerPermissions,
        title: "Allow workers to file unassigned tasks",
        description: "When on (default), workers may call assign_task with no agent_token to file work into the unassigned pool for any peer to claim. When off, only the admin may create tasks.",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_allow_worker_update_own_status",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::WorkerPermissions,
        title: "Allow workers to update their own task status",
        description: "When on (default), workers may call update_task_status on tasks they are assigned to. When off, only the admin may transition task status.",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_allow_worker_view_foreign_tasks",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::WorkerPermissions,
        title: "Allow workers to view tasks assigned to other agents",
        description: "When on (default), a worker's view_tasks / search_tasks / ask_project_rag calls are no longer scoped to just their own tasks plus the unassigned pool — they can also see and search tasks assigned to a DIFFERENT agent. When off, cross-agent task visibility is denied (a foreign-owned task_id resolves to the same phantom 'not found' a nonexistent task_id does, so a worker cannot enumerate foreign tasks). Does not affect who may edit or reassign a task — only who can see it.",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_allow_worker_comment_foreign_tasks",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::WorkerPermissions,
        title: "Allow workers to comment on tasks assigned to other agents",
        description: "When on (default, implies view), a worker may call add_task_comment on a task assigned to a DIFFERENT agent — the comment is authored and timestamped as normal, and editing/deleting someone else's comment stays author-only regardless of this setting. When off, add_task_comment on a foreign-owned task is denied (same phantom 'not found' as an unassigned/nonexistent task). Never affects task status, reassignment, subtask creation, or bulk operations — those stay owner-only.",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_auto_event_loop_global",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::EventLoop,
        title: "Agent event-loop (wake on inbox / task events)",
        description: "When on (default), worker agents are instructed to call wait_for_events on session start and after each event, so they wake automatically when messages or tasks arrive. When off, the wake-loop bootstrap text is omitted from serverInfo.instructions for every agent — workers fall back to human-prompted polling. Per-agent overrides live on the Agents tab (disabled here also disables every per-agent toggle).",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_event_idle_stop_seconds",
        r#type: SettingType::Int,
        default: SettingDefault::Int(604800), // 7 days
        tier: SettingTier::Operator,
        group: SettingGroup::EventLoop,
        title: "Stop the event-loop after idle",
        description: "How long an agent may sit in the wake-loop with NO real events (messages / task changes) before the server tells it to stop listening and go dormant. Measured across reconnects and reset by every real event (heartbeats and the profile-review greet do NOT count). When the window is exceeded, wait_for_events returns a stop_listening event and the agent exits its loop. Default 7 days; set to 0 to never stop (hold indefinitely). Re-waking a dormant agent is a manual/operator action.",
        widget: Some(SettingWidget::IntDuration),
    },
    SettingSpec {
        key: "config_debug_eventloop",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(false),
        tier: SettingTier::Operator,
        group: SettingGroup::EventLoop,
        title: "Event-loop debug logging",
        description: "When on, the backend logs a detailed trace of the wait_for_events wake loop (which hold strategy each client gets, whether a connection parks vs re-polls, heartbeats sent, the adaptive hold-ladder phase, and events in/out) at a level the systemd journal captures — grep for \"EVENTLOOP\". Off by default; when unset it falls back to the AGENT_MCP_EVENTLOOP_DEBUG environment variable (the deploy default). Diagnostic only — leave off in normal operation.",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_idle_reminder_enabled",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::EventLoop,
        title: "Idle backlog reminders",
        description: "When on (default), an agent sitting idle in the event loop that still has unaddressed work — unread messages and/or OPEN tasks assigned to it (not completed/cancelled/failed) — is periodically reminded with a listed summary and told to go handle it. An agent with no backlog is never reminded (it stays parked for free).",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_idle_reminder_interval_seconds",
        r#type: SettingType::Int,
        default: SettingDefault::Int(3600), // 1 hour
        tier: SettingTier::Operator,
        group: SettingGroup::EventLoop,
        title: "Idle reminder interval",
        description: "How often to re-remind an idle agent that still has an unaddressed backlog. Default 1 hour. The reminder only fires when a backlog is actually present at the interval boundary.",
        widget: Some(SettingWidget::IntDuration),
    },
    SettingSpec {
        key: "config_message_retention_days",
        r#type: SettingType::Int,
        default: SettingDefault::Int(0),
        tier: SettingTier::Operator,
        group: SettingGroup::Retention,
        title: "Auto-delete read messages older than",
        description: "The background pruner runs once every 24 hours and deletes rows from agent_messages where read=1 and timestamp is older than the configured window. Unread messages are never pruned. Set to 0 to disable (keep forever).",
        widget: Some(SettingWidget::IntDays),
    },
    SettingSpec {
        key: "config_allow_worker_update_own_profile",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::AgentProfiles,
        title: "Allow workers to edit their own profile",
        description: "When on (default), a worker may call update_agent_profile to edit or confirm its own self-authored profile. When off, a worker can still review (confirm) but not change its profile. Editing a profile is routing-neutral — this toggle is a governance preference, not a safety gate.",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_allow_manager_update_own_profile",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::AgentProfiles,
        title: "Allow managers to edit their own profile",
        description: "When on (default), a manager may call update_agent_profile to edit or confirm its own profile (the charter seeded at registration). When off, a manager can still review but not change its own profile.",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_allow_manager_curate_profiles",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::AgentProfiles,
        title: "Allow managers to curate worker profiles",
        description: "When on (default), a manager may edit any worker's profile in the project (curation). Managers may never edit another manager's profile regardless of this toggle. When off, managers may only edit their own profile (subject to the manager self-edit toggle).",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_profile_review_interval_days",
        r#type: SettingType::Int,
        default: SettingDefault::Int(7),
        tier: SettingTier::Operator,
        group: SettingGroup::AgentProfiles,
        title: "Remind agents to review their profile every",
        description: "How often an agent is nudged (on its event loop) to confirm or refresh its profile. The nudge fires when the profile has not been reviewed within this window, and always once on the first event-loop call of a new session. Set to 0 to disable the staleness nudge (the first-connect greet still fires once).",
        widget: Some(SettingWidget::IntDays),
    },
    SettingSpec {
        key: "config_allow_worker_self_schedule",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::Scheduling,
        title: "Allow agents to self-register scheduled directives",
        description: "When on (default), an agent may call create_scheduled_directive to register its own recurring directives (imperative 'do X' commands that fire when the agent next checks in at-or-after the interval). When off, only a manager (for its workers) or an operator/admin may create schedules on an agent's behalf. Guardrails (min-interval floor + max active loops per agent) always apply.",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_allow_manager_curate_schedules",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::Scheduling,
        title: "Allow managers to curate worker schedules",
        description: "When on (default), a manager may create/update/delete scheduled directives on any WORKER in the project. Managers may never curate another manager's schedules regardless of this toggle. When off, managers may only manage their own schedules (subject to the self-schedule toggle).",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_min_schedule_interval_seconds",
        r#type: SettingType::Int,
        default: SettingDefault::Int(60),
        tier: SettingTier::Operator,
        group: SettingGroup::Scheduling,
        title: "Minimum schedule interval",
        description: "The floor (in seconds) on a scheduled directive's interval. create/update reject any interval below this value with a clear error. Since self-scheduling is on by default, this keeps an agent from registering a hot loop. Default 60s.",
        widget: Some(SettingWidget::IntDuration),
    },
    SettingSpec {
        key: "config_max_schedules_per_agent",
        r#type: SettingType::Int,
        default: SettingDefault::Int(10),
        tier: SettingTier::Operator,
        group: SettingGroup::Scheduling,
        title: "Maximum active schedules per agent",
        description: "The cap on how many active (enabled) scheduled directives a single agent may hold at once. create/update reject a new schedule that would exceed this count. Completed/paused schedules do not count. Default 10.",
        // Matches Python's literal spec verbatim, including its own
        // apparent copy/paste choice: an int-COUNT setting (not a
        // duration) still carries the `int_duration` widget hint, not
        // `int_days`/a plain numeric widget. Preserved as-is per this
        // migration's re-derive discipline -- a UI-hint quirk, not a
        // behavioral bug worth silently fixing mid-port.
        widget: Some(SettingWidget::IntDuration),
    },
    // -- Delivery transport / fallback push (ADR-0021) -----------------
    // The tunable per-project policy for when agent-mcp pushes a skinny
    // notification down a worker's registered delivery transport (the
    // fallback for sessions that don't poll wait_for_events). All
    // operator-tier.
    SettingSpec {
        key: "config_delivery_enabled",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(false),
        tier: SettingTier::Operator,
        group: SettingGroup::Delivery,
        title: "Fallback delivery channel",
        description: "When on, agent-mcp pushes skinny notifications (message/task id, title, status — never the body) to a worker's registered delivery transport when the agent falls behind (see the triggers below), so a session that isn't polling still gets poked. Off by default; a runtime (e.g. the AoE bridge) must also register the transport for a worker.",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_delivery_on_unread_messages",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::Delivery,
        title: "Deliver on unread messages",
        description: "Arm the fallback while the agent has unread inbox messages.",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_delivery_on_unfinished_tasks",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::Delivery,
        title: "Deliver on unfinished tasks",
        description: "Arm the fallback while the agent has OPEN tasks assigned to it (not completed/cancelled/failed).",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_delivery_on_unassigned_tasks",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(false),
        tier: SettingTier::Operator,
        group: SettingGroup::Delivery,
        title: "Deliver on unassigned tasks",
        description: "Arm the fallback while there are unassigned tasks in the pool the agent could claim. Off by default — this can be noisy.",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_delivery_on_due_directives",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(true),
        tier: SettingTier::Operator,
        group: SettingGroup::Delivery,
        title: "Deliver on due scheduled directives",
        description: "Arm the fallback while the agent has a scheduled directive that is due now. On by default — a due directive is an explicit, deliberate obligation, not an ambient signal.",
        widget: Some(SettingWidget::Switch),
    },
    SettingSpec {
        key: "config_delivery_backoff_initial_seconds",
        r#type: SettingType::Int,
        default: SettingDefault::Int(30),
        tier: SettingTier::Operator,
        group: SettingGroup::Delivery,
        title: "Initial re-ping delay",
        description: "First delay before re-pinging while a condition stays unmet. The delay widens on each re-ping (escalating backoff) up to the max, and resets the moment the condition clears.",
        widget: Some(SettingWidget::IntDuration),
    },
    SettingSpec {
        key: "config_delivery_backoff_max_seconds",
        r#type: SettingType::Int,
        default: SettingDefault::Int(3600), // 1 hour
        tier: SettingTier::Operator,
        group: SettingGroup::Delivery,
        title: "Max re-ping delay",
        description: "The ceiling the escalating re-ping delay backs off to. Default 1 hour.",
        widget: Some(SettingWidget::IntDuration),
    },
    SettingSpec {
        key: "config_delivery_cooldown_seconds",
        r#type: SettingType::Int,
        default: SettingDefault::Int(60),
        tier: SettingTier::Operator,
        group: SettingGroup::Delivery,
        title: "Post-ping cooldown",
        description: "Minimum quiet window after a ping before another may fire for the same worker (also the window a just-active session is left alone).",
        widget: Some(SettingWidget::IntDuration),
    },
    SettingSpec {
        key: "config_delivery_wake_dormant",
        r#type: SettingType::Bool,
        default: SettingDefault::Bool(false),
        tier: SettingTier::Operator,
        group: SettingGroup::Delivery,
        title: "Wake dormant sessions",
        description: "When on, a fallback ping may wake a dormant (stopped-but-revivable) session. When off (default), dormant sessions are left asleep — only idle sessions are pinged.",
        widget: Some(SettingWidget::Switch),
    },
];

/// Return the [`SettingSpec`] for `key`, or `None` if unknown. Port of
/// `spec_for`.
pub fn spec_for(key: &str) -> Option<&'static SettingSpec> {
    SETTINGS_SCHEMA.iter().find(|s| s.key == key)
}

/// Every setting key the backend knows about. Port of
/// `KNOWN_SETTING_KEYS`.
pub fn known_setting_keys() -> std::collections::HashSet<&'static str> {
    SETTINGS_SCHEMA.iter().map(|s| s.key).collect()
}

/// The genuinely-secret keys, derived from the schema (single source).
/// Port of `SECRET_SETTING_KEYS` -- empty today (no `SettingType::Secret`
/// entry has shipped yet), kept derived rather than hand-maintained so
/// a future secret setting is picked up automatically, matching
/// Python's own single-source rationale.
pub fn secret_setting_keys() -> std::collections::HashSet<&'static str> {
    SETTINGS_SCHEMA
        .iter()
        .filter(|s| s.r#type == SettingType::Secret)
        .map(|s| s.key)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn schema_has_exactly_29_entries() {
        // Pinned count (matches agent_mcp/core/settings_schema.py's
        // real SETTINGS_SCHEMA tuple) -- a future addition/removal
        // should update this deliberately, not silently drift.
        assert_eq!(SETTINGS_SCHEMA.len(), 29);
    }

    #[test]
    fn every_key_is_unique() {
        let mut seen = std::collections::HashSet::new();
        for s in SETTINGS_SCHEMA {
            assert!(seen.insert(s.key), "duplicate key: {}", s.key);
        }
    }

    #[test]
    fn every_key_starts_with_config_prefix() {
        for s in SETTINGS_SCHEMA {
            assert!(
                s.key.starts_with("config_"),
                "{} does not start with config_",
                s.key
            );
        }
    }

    #[test]
    fn spec_for_finds_a_known_key() {
        let spec = spec_for("config_auto_event_loop_global").unwrap();
        assert_eq!(spec.group, SettingGroup::EventLoop);
        assert_eq!(spec.default, SettingDefault::Bool(true));
    }

    #[test]
    fn spec_for_returns_none_for_an_unknown_key() {
        assert!(spec_for("config_does_not_exist").is_none());
    }

    #[test]
    fn known_setting_keys_matches_schema_length() {
        assert_eq!(known_setting_keys().len(), SETTINGS_SCHEMA.len());
    }

    #[test]
    fn secret_setting_keys_is_currently_empty() {
        // No SettingType::Secret entry has shipped yet -- matches
        // Python's real, current SECRET_SETTING_KEYS.
        assert!(secret_setting_keys().is_empty());
    }

    #[test]
    fn every_setting_is_operator_tier() {
        // Matches the module's own documented invariant: "Every
        // registered setting is operator-tier" (the former
        // sysadmin-only AoE keys were retired).
        for s in SETTINGS_SCHEMA {
            assert_eq!(
                s.tier,
                SettingTier::Operator,
                "{} is not operator-tier",
                s.key
            );
        }
    }

    #[test]
    fn schema_serializes_to_the_expected_wire_shape() {
        // Spot-check one entry's JSON shape -- the real contract
        // GET /api/settings-schema exposes.
        let spec = spec_for("config_event_idle_stop_seconds").unwrap();
        let json = serde_json::json!({
            "key": spec.key,
            "type": spec.r#type,
            "default": spec.default,
            "tier": spec.tier,
            "group": spec.group,
            "title": spec.title,
            "widget": spec.widget,
        });
        assert_eq!(json["type"], "int");
        assert_eq!(json["tier"], "operator");
        assert_eq!(json["group"], "event_loop");
        assert_eq!(json["default"], 604800);
        assert_eq!(json["widget"], "int_duration");
    }
}
