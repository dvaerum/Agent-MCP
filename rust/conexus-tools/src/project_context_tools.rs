//! Port of `agent_mcp/tools/project_context_tools.py` (Phase D5, PR 8
//! — 2557 LOC, the second-largest file in the migration after
//! `task_tools.py`, given its own dedicated scoping pass per this
//! migration's established discipline). 7 registered tools:
//! `view_project_context`, `update_project_context`,
//! `create_project_context`, `bulk_update_project_context`,
//! `backup_project_context`, `validate_context_consistency`,
//! `delete_project_context`.
//!
//! `conexus_db::project_context_repository` (Phase B) already covers
//! every DB primitive these tools need 1:1
//! (`get`/`list_all`/`upsert`/`create_new`/`delete_many`) — this
//! module is the tool-layer authorization/validation/wiring on top,
//! landed across 7 PRs (smallest/foundational -> largest/branchy).
//!
//! ## PR 1/7: pure helpers, zero DB, no tool registered yet
//!
//! - [`is_valid_memory_key`] — port of `utils/string_utils.py::
//!   is_valid_memory_key`/`MEMORY_KEY_RE`.
//! - The compound write-gate ([`can_create_project_context`]/
//!   [`can_update_project_context`]/[`can_delete_project_context`]) —
//!   Python's `_can_write_project_context(capability)` is a CLOSURE
//!   FACTORY producing 3 predicate variants; `Requirement::Predicate`'s
//!   `check` field is a bare `fn` pointer with no captured state
//!   (deliberately, so two `Predicate` requirements can be compared by
//!   `reason` text alone — see `conexus-auth::requirement`'s own doc),
//!   so this ports as one generic [`compound_write_gate`] helper plus
//!   3 thin top-level wrapper `fn`s, each hardcoding its capability.
//!   Composes Python's two in-body gates in the same order
//!   (`_requires_authenticated_caller` then `_deny_viewer_tier_write`):
//!   an `AgentBearer` is admitted on identity alone (the per-key
//!   creator-ownership matrix inside each mutating tool governs it
//!   further); an operator-path caller (`OperatorSession`/
//!   `ForwardingHeader`) additionally needs the given memories-write
//!   capability (viewer-tier operators carry `memories.view` only).
//! - [`analyze_context_health`] — port of `_analyze_context_health`/
//!   `_generate_context_recommendations`. Takes `now: DateTime<Utc>`
//!   as an explicit parameter rather than reading the wall clock
//!   inline (Python's `datetime.datetime.now()`) — the same
//!   established convention as every prior tool this migration has
//!   ported.
//! - [`check_context_consistency`] — port of
//!   `validate_context_consistency_tool_impl`'s 5 checks (invalid
//!   JSON, case-insensitive duplicate keys, missing descriptions, old
//!   entries, oversized entries). The "old entries" check is a
//!   DELIBERATE re-derivation, not a literal port: Python computes a
//!   `cutoff_date` string and compares `updated_at < cutoff_date`
//!   LEXICALLY (naive-local ISO strings); this port parses both sides
//!   via `scheduled_directive_repository::parse_flexible` and compares
//!   as real `DateTime<Utc>` values instead — the exact same
//!   deliberate improvement already applied to this migration's
//!   `scheduled_directive_tools.rs` sibling (never changes the
//!   intended outcome when formats agree, strictly safer when they
//!   don't).

use chrono::{DateTime, Utc};
use conexus_auth::{Requirement, Tool};
use conexus_core::capability::Capability;
use conexus_core::principal::{Principal, PrincipalKind};
use conexus_core::tool_result::ToolResult;
use conexus_db::agent_action_repository;
use conexus_db::project_context_repository::{self, ProjectContextRow};
use conexus_db::scheduled_directive_repository::parse_flexible;
use regex::Regex;
use rusqlite::Connection;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::LazyLock;
use tokio::sync::Mutex as AsyncMutex;

use crate::wake_notify;

static MEMORY_KEY_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9._/-]+$").unwrap());

/// ADR-0016: the `config_*` namespace lives in the `project_settings`
/// store, NOT `project_context` -- backs the write/delete rejection
/// only (every caller, admin included).
static CONFIG_KEY_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)^config_").unwrap());

/// One `AuthRejected` message covering both arms of the composed
/// write gate (`Requirement::Predicate`'s `reason` is static) --
/// names both the identity requirement and the viewer-tier carve-out.
pub const WRITE_DENIED_REASON: &str = "Unauthorized: Valid token or operator session required, \
    and viewer-tier operators cannot mutate project context (read-only project membership)";

/// Port of `utils/string_utils.py::is_valid_memory_key` — non-empty,
/// `A-Z a-z 0-9 . _ / -` only.
pub fn is_valid_memory_key(value: &str) -> bool {
    !value.is_empty() && MEMORY_KEY_RE.is_match(value)
}

/// Port of `_requires_authenticated_caller`'s predicate form
/// (`_is_authenticated_caller`): any principal at all (agent-bearer or
/// either operator-path kind).
pub fn is_authenticated_caller(principal: Option<&Principal>) -> bool {
    principal.is_some()
}

/// Composes `_requires_authenticated_caller` + `_deny_viewer_tier_write`
/// in that order -- see this module's doc for the full rationale.
fn compound_write_gate(principal: Option<&Principal>, cap: Capability) -> bool {
    principal.is_some_and(|p| p.kind == PrincipalKind::AgentBearer || p.has_capability(cap))
}

pub fn can_create_project_context(principal: Option<&Principal>) -> bool {
    compound_write_gate(principal, Capability::MemoriesCreate)
}

pub fn can_update_project_context(principal: Option<&Principal>) -> bool {
    compound_write_gate(principal, Capability::MemoriesUpdate)
}

pub fn can_delete_project_context(principal: Option<&Principal>) -> bool {
    compound_write_gate(principal, Capability::MemoriesDelete)
}

/// Port of `_analyze_context_health`'s full return shape.
#[derive(Debug, Clone, PartialEq)]
pub struct ContextHealthReport {
    pub status: String,
    pub health_score: f64,
    pub total: usize,
    pub stale_entries: usize,
    pub json_errors: usize,
    pub large_entries: usize,
    pub issues: Vec<String>,
    pub warnings: Vec<String>,
    pub recommendations: Vec<String>,
}

const STALE_DAYS: i64 = 30;
const VERY_STALE_DAYS: i64 = 90;
const LARGE_ENTRY_CHARS: usize = 10240;

/// Port of `_analyze_context_health`. `now` is the caller's own
/// injected timestamp (see module doc) -- staleness is computed
/// against it, never a fresh wall-clock read.
pub fn analyze_context_health(
    entries: &[ProjectContextRow],
    now: DateTime<Utc>,
) -> ContextHealthReport {
    if entries.is_empty() {
        // Full shape even when empty -- every consumer reads these
        // fields unconditionally (matches Python's own comment on
        // this exact branch).
        return ContextHealthReport {
            status: "no_data".to_string(),
            health_score: 100.0,
            total: 0,
            stale_entries: 0,
            json_errors: 0,
            large_entries: 0,
            issues: Vec::new(),
            warnings: Vec::new(),
            recommendations: vec![
                "No context entries yet - add project context to enable health analysis"
                    .to_string(),
            ],
        };
    }

    let total = entries.len();
    let mut issues = Vec::new();
    let mut warnings = Vec::new();
    let mut stale_count = 0usize;
    let mut json_errors = 0usize;
    let mut large_entries = 0usize;

    for entry in entries {
        let key = &entry.context_key;

        if serde_json::from_str::<serde_json::Value>(&entry.value).is_err() {
            json_errors += 1;
            issues.push(format!("JSON parse error in '{key}'"));
        }

        match parse_flexible(&entry.updated_at) {
            Ok(updated_time) => {
                let days_old = (now - updated_time).num_days();
                if days_old > STALE_DAYS {
                    stale_count += 1;
                    if days_old > VERY_STALE_DAYS {
                        warnings.push(format!("'{key}' is {days_old} days old"));
                    }
                }
            }
            Err(_) => warnings.push(format!("Invalid timestamp for '{key}'")),
        }

        let entry_size = entry.value.chars().count();
        if entry_size > LARGE_ENTRY_CHARS {
            large_entries += 1;
            warnings.push(format!("'{key}' is large ({}KB)", entry_size / 1024));
        }
    }

    let stale_ratio = stale_count as f64 / total as f64;
    let error_ratio = json_errors as f64 / total as f64;
    let large_ratio = large_entries as f64 / total as f64;
    let health_score =
        (100.0 - stale_ratio * 40.0 - error_ratio * 50.0 - large_ratio * 10.0).clamp(0.0, 100.0);

    let status = if health_score >= 90.0 {
        "excellent"
    } else if health_score >= 70.0 {
        "good"
    } else if health_score >= 50.0 {
        "needs_attention"
    } else {
        "critical"
    };

    issues.truncate(5);
    warnings.truncate(5);

    ContextHealthReport {
        status: status.to_string(),
        health_score: (health_score * 10.0).round() / 10.0,
        total,
        stale_entries: stale_count,
        json_errors,
        large_entries,
        issues,
        warnings,
        recommendations: generate_context_recommendations(
            stale_count,
            json_errors,
            large_entries,
            total,
        ),
    }
}

/// Port of `_generate_context_recommendations`.
fn generate_context_recommendations(
    stale_count: usize,
    json_errors: usize,
    large_entries: usize,
    total: usize,
) -> Vec<String> {
    let mut recommendations = Vec::new();

    if json_errors > 0 {
        recommendations.push(format!(
            "Fix {json_errors} JSON parsing errors using validate_context_consistency"
        ));
    }
    if stale_count as f64 > total as f64 * 0.3 {
        recommendations.push(format!(
            "Review and update {stale_count} stale entries (30+ days old)"
        ));
    }
    if large_entries > 0 {
        recommendations.push(format!(
            "Consider breaking down {large_entries} large entries into smaller components"
        ));
    }
    if total > 100 {
        recommendations
            .push("Consider archiving old context entries to improve performance".to_string());
    }
    if recommendations.is_empty() {
        recommendations
            .push("Context health is excellent - no immediate action required".to_string());
    }
    recommendations
}

/// Port of `validate_context_consistency_tool_impl`'s 5 checks --
/// see module doc for the "old entries" re-derivation.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ConsistencyReport {
    pub total_entries: usize,
    pub issues: Vec<String>,
    pub warnings: Vec<String>,
}

const OLD_ENTRY_DAYS: i64 = 30;
const LARGE_VALUE_CHARS: usize = 10000;

pub fn check_context_consistency(
    entries: &[ProjectContextRow],
    now: DateTime<Utc>,
) -> ConsistencyReport {
    let mut issues = Vec::new();
    let mut warnings = Vec::new();

    // Check 1: invalid JSON values.
    for entry in entries {
        if let Err(e) = serde_json::from_str::<serde_json::Value>(&entry.value) {
            issues.push(format!("Invalid JSON in '{}': {e}", entry.context_key));
        }
    }

    // Check 2: duplicate/conflicting keys, case-insensitive.
    let mut key_map: HashMap<String, &str> = HashMap::new();
    for entry in entries {
        let key_lower = entry.context_key.to_lowercase();
        if let Some(&first) = key_map.get(&key_lower) {
            issues.push(format!(
                "Potential duplicate keys: '{first}' and '{}'",
                entry.context_key
            ));
        } else {
            key_map.insert(key_lower, entry.context_key.as_str());
        }
    }

    // Check 3: missing descriptions.
    let missing_desc: Vec<&str> = entries
        .iter()
        .filter(|e| e.description.as_deref().unwrap_or("").is_empty())
        .map(|e| e.context_key.as_str())
        .collect();
    if !missing_desc.is_empty() {
        warnings.extend(
            missing_desc
                .iter()
                .take(10)
                .map(|key| format!("Missing description: '{key}'")),
        );
        if missing_desc.len() > 10 {
            warnings.push(format!(
                "... and {} more missing descriptions",
                missing_desc.len() - 10
            ));
        }
    }

    // Check 4: old entries (>30 days) -- real-datetime compare, a
    // deliberate re-derivation of Python's lexical-string cutoff
    // compare (see module doc).
    let cutoff = now - chrono::Duration::days(OLD_ENTRY_DAYS);
    let old_entries: Vec<&str> = entries
        .iter()
        .filter(|e| {
            parse_flexible(&e.updated_at)
                .map(|t| t < cutoff)
                .unwrap_or(false)
        })
        .map(|e| e.context_key.as_str())
        .collect();
    if !old_entries.is_empty() {
        warnings.extend(
            old_entries
                .iter()
                .take(5)
                .map(|key| format!("Old entry (>30 days): '{key}'")),
        );
        if old_entries.len() > 5 {
            warnings.push(format!(
                "... and {} more old entries",
                old_entries.len() - 5
            ));
        }
    }

    // Check 5: unusually large values.
    let large_entries: Vec<String> = entries
        .iter()
        .filter(|e| e.value.chars().count() > LARGE_VALUE_CHARS)
        .map(|e| format!("{} ({} chars)", e.context_key, e.value.chars().count()))
        .collect();
    if !large_entries.is_empty() {
        warnings.extend(
            large_entries
                .iter()
                .take(5)
                .map(|entry| format!("Large entry: {entry}")),
        );
        if large_entries.len() > 5 {
            warnings.push(format!(
                "... and {} more large entries",
                large_entries.len() - 5
            ));
        }
    }

    ConsistencyReport {
        total_entries: entries.len(),
        issues,
        warnings,
    }
}

const AUTHENTICATED_DENIED_REASON: &str = "Unauthorized: Valid token or operator session required";

/// Renders a [`ConsistencyReport`] as Python's
/// `validate_context_consistency_tool_impl` renders its
/// `response_parts` list -- one message string, `\n`-joined.
fn render_consistency_message(report: &ConsistencyReport) -> String {
    let mut parts = vec![
        "Context Consistency Validation Results".to_string(),
        format!("Total entries: {}", report.total_entries),
    ];

    if report.issues.is_empty() && report.warnings.is_empty() {
        parts.push("\n\u{2705} No issues found! Context appears consistent.".to_string());
    } else {
        if !report.issues.is_empty() {
            parts.push(format!(
                "\n\u{1f6a8} Critical Issues ({}):",
                report.issues.len()
            ));
            parts.extend(report.issues.iter().map(|issue| format!("  {issue}")));
        }
        if !report.warnings.is_empty() {
            parts.push(format!(
                "\n\u{26a0}\u{fe0f}  Warnings ({}):",
                report.warnings.len()
            ));
            parts.extend(report.warnings.iter().map(|warning| format!("  {warning}")));
        }
        parts.push("\nRecommendations:".to_string());
        if !report.issues.is_empty() {
            parts.push("- Fix critical issues immediately".to_string());
            parts.push("- Use bulk_update_project_context for corrections".to_string());
        }
        if !report.warnings.is_empty() {
            parts.push("- Review warnings for potential cleanup".to_string());
            parts.push("- Consider using delete_project_context for unused entries".to_string());
        }
    }

    parts.join("\n")
}

pub struct ValidateContextConsistencyTool;

impl Tool for ValidateContextConsistencyTool {
    const NAME: &'static str = "validate_context_consistency";
    const REQUIRED: Requirement = Requirement::Predicate {
        check: is_authenticated_caller,
        reason: AUTHENTICATED_DENIED_REASON,
    };
    const DESCRIPTION: &'static str = "Check for inconsistencies, conflicts, and quality \
        issues in project context. Critical for preventing context poisoning.";
    const SCHEMA: &'static str =
        r#"{"type":"object","properties":{},"required":[],"additionalProperties":false}"#;

    fn call<'a>(
        _principal: Option<&'a Principal>,
        _arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let guard = conn.lock().await;
            let entries = match project_context_repository::list_all(&guard) {
                Ok(rows) => rows,
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "A database error occurred; it has been logged. Retry, or ask \
                            an operator to check logs."
                            .to_string(),
                    }
                }
            };

            if entries.is_empty() {
                return ToolResult::Ok {
                    data: Some(serde_json::json!({
                        "total_entries": 0,
                        "issues": Vec::<String>::new(),
                        "warnings": Vec::<String>::new(),
                    })),
                    message: Some("No project context entries found.".to_string()),
                };
            }

            let now_dt: DateTime<Utc> = match parse_flexible(now) {
                Ok(dt) => dt,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Internal clock error".to_string(),
                    }
                }
            };
            let report = check_context_consistency(&entries, now_dt);
            let message = render_consistency_message(&report);

            ToolResult::Ok {
                data: Some(serde_json::json!({
                    "total_entries": report.total_entries,
                    "issues": report.issues,
                    "warnings": report.warnings,
                })),
                message: Some(message),
            }
        })
    }
}

const VIEW_STALE_DAYS: i64 = 30;
const VIEW_LARGE_ENTRY_CHARS: usize = 10240;

fn is_older_than_days(updated_at: &str, now: DateTime<Utc>, days: i64) -> bool {
    parse_flexible(updated_at)
        .map(|t| (now - t).num_days() > days)
        .unwrap_or(false)
}

/// Python's `str()` on a JSON-decoded scalar -- NOT the same as
/// re-serializing to JSON text (`str(True)` is `"True"`, `str(None)`
/// is `"None"`, `str("hello")` is `hello` with no quotes). Only
/// called on a non-object/non-array `Value` (the caller branches on
/// that first); an invalid-JSON entry's `value_parsed` is already the
/// raw string (see [`build_view_entries`]), so this returns it
/// unchanged in that case too.
fn python_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => s.clone(),
        Value::Object(_) | Value::Array(_) => {
            unreachable!("caller already branched on object/array")
        }
    }
}

/// One `view_project_context` result entry, with the same `_metadata`
/// block Python's `entry_data` dict attaches.
struct ViewEntry {
    row: ProjectContextRow,
    value_parsed: Value,
    json_valid: bool,
    days_old: Option<i64>,
}

fn build_view_entries(entries: Vec<ProjectContextRow>, now: DateTime<Utc>) -> Vec<ViewEntry> {
    entries
        .into_iter()
        .map(|row| {
            let (value_parsed, json_valid) = match serde_json::from_str::<Value>(&row.value) {
                Ok(v) => (v, true),
                Err(_) => (Value::String(row.value.clone()), false),
            };
            let days_old = parse_flexible(&row.updated_at)
                .ok()
                .map(|t| (now - t).num_days());
            ViewEntry {
                row,
                value_parsed,
                json_valid,
                days_old,
            }
        })
        .collect()
}

fn view_entry_json(entry: &ViewEntry) -> Value {
    let entry_size = entry.row.value.chars().count();
    let is_stale = entry.days_old.is_some_and(|d| d > VIEW_STALE_DAYS);
    serde_json::json!({
        "key": entry.row.context_key,
        "value": entry.value_parsed,
        "description": entry.row.description,
        "updated_by": entry.row.updated_by,
        "updated_at": entry.row.updated_at,
        "created_by": entry.row.created_by,
        "created_at": entry.row.created_at,
        "_metadata": {
            "size_bytes": entry_size,
            "size_kb": (entry_size as f64 / 1024.0 * 100.0).round() / 100.0,
            "json_valid": entry.json_valid,
            "days_old": entry.days_old,
            "is_stale": is_stale,
            "is_large": entry_size > VIEW_LARGE_ENTRY_CHARS,
        },
    })
}

/// Renders the full "Smart Tips"-style message Python's
/// `view_project_context_tool_impl` builds. `all_entries` is the
/// UNFILTERED table (needed only for `show_health_analysis`); `view`
/// is the already-filtered/sorted/limited result set.
#[allow(clippy::too_many_arguments)]
fn render_view_message(
    view: &[ViewEntry],
    context_key_filter: Option<&str>,
    search_query_filter: Option<&str>,
    show_stale_entries: bool,
    show_health_analysis: bool,
    include_backup_info: bool,
    sort_by: &str,
    all_entries: &[ProjectContextRow],
    now: DateTime<Utc>,
) -> String {
    if view.is_empty() {
        return "No project context entries found matching the criteria.".to_string();
    }

    let mut filter_info = Vec::new();
    if let Some(k) = context_key_filter {
        filter_info.push(format!("key='{k}'"));
    }
    if let Some(q) = search_query_filter {
        filter_info.push(format!("search='{q}'"));
    }
    if show_stale_entries {
        filter_info.push("stale_only=true".to_string());
    }

    let mut header = format!("Project Context ({} entries", view.len());
    if !filter_info.is_empty() {
        header.push_str(&format!(", filtered by: {}", filter_info.join(", ")));
    }
    header.push_str(&format!(", sorted by: {sort_by})"));

    let mut parts = vec![format!("{header}\n")];

    if show_health_analysis {
        let health = analyze_context_health(all_entries, now);
        let health_icon = match health.status.as_str() {
            "excellent" => "\u{1f7e2}",
            "good" => "\u{1f7e1}",
            "needs_attention" => "\u{1f7e0}",
            _ => "\u{1f534}",
        };
        let status_title = {
            let mut c = health.status.chars();
            match c.next() {
                Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
                None => String::new(),
            }
        };
        parts.push(format!(
            "\u{1f4ca} **Context Health:** {health_icon} {status_title} ({}/100)",
            health.health_score
        ));
        parts.push(format!("   Total: {} entries", health.total));
        parts.push(format!(
            "   Issues: {} JSON errors, {} stale, {} large",
            health.json_errors, health.stale_entries, health.large_entries
        ));
        if let Some(first) = health.recommendations.first() {
            parts.push(format!("   \u{1f4a1} {first}"));
        }
        parts.push(String::new());
    }

    if include_backup_info {
        parts.push(
            "\u{1f4be} **Backup Info:** Use bulk_update_project_context for backups".to_string(),
        );
        parts.push(String::new());
    }

    for entry in view.iter().take(20) {
        let entry_size = entry.row.value.chars().count();
        let is_stale = entry.days_old.is_some_and(|d| d > VIEW_STALE_DAYS);
        let is_large = entry_size > VIEW_LARGE_ENTRY_CHARS;

        let mut indicators = Vec::new();
        if !entry.json_valid {
            indicators.push("\u{274c} JSON_ERROR".to_string());
        }
        if is_stale {
            indicators.push(format!(
                "\u{23f0} STALE({}d)",
                entry.days_old.unwrap_or_default()
            ));
        }
        if is_large {
            indicators.push(format!(
                "\u{1f4e6} LARGE({:.2}KB)",
                entry_size as f64 / 1024.0
            ));
        }
        let indicator_text = if indicators.is_empty() {
            String::new()
        } else {
            format!(" {}", indicators.join(" "))
        };

        parts.push(format!("**{}**{indicator_text}", entry.row.context_key));
        parts.push(format!(
            "  Description: {}",
            entry.row.description.as_deref().unwrap_or("No description")
        ));
        parts.push(format!(
            "  Updated: {} by {}",
            entry.row.updated_at, entry.row.updated_by
        ));
        if let Some(created_by) = &entry.row.created_by {
            parts.push(format!(
                "  Created: {} by {created_by}",
                entry.row.created_at.as_deref().unwrap_or("Unknown")
            ));
        }

        let mut value_str = if entry.value_parsed.is_object() || entry.value_parsed.is_array() {
            serde_json::to_string_pretty(&entry.value_parsed).unwrap_or_default()
        } else {
            python_str(&entry.value_parsed)
        };
        if value_str.chars().count() > 500 {
            value_str = value_str.chars().take(500).collect::<String>() + "... [TRUNCATED]";
        }
        parts.push(format!("  Value: {value_str}"));
        parts.push(String::new());
    }

    if view.len() > 20 {
        parts.push(format!("... and {} more entries", view.len() - 20));
        parts.push(
            "Use max_results parameter to see more, or add filters to narrow results".to_string(),
        );
    }

    parts.push("\n\u{1f4a1} Smart Tips:".to_string());
    if !show_health_analysis {
        parts.push("\u{2022} Add show_health_analysis=true for context health metrics".to_string());
    }
    if !show_stale_entries {
        parts.push(
            "\u{2022} Add show_stale_entries=true to see entries needing updates".to_string(),
        );
    }
    parts.push("\u{2022} Use sort_by=[key|size|updated_at] for different sorting".to_string());
    parts.push("\u{2022} Use validate_context_consistency to fix JSON errors".to_string());

    parts.join("\n")
}

pub struct ViewProjectContextTool;

impl Tool for ViewProjectContextTool {
    const NAME: &'static str = "view_project_context";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::MemoriesView,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Smart project context viewer with health analysis, \
        stale entry detection, and advanced filtering. Provides comprehensive insights into \
        context quality and usage.";
    // "maxLength": 256 mirrors conexus_core::schema_limits::IDENTIFIER_MAX_LEN
    // -- kept as a literal (SCHEMA must be `const`-constructible) and
    // cross-checked against that constant by this module's own test.
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "context_key": {
                "type": "string",
                "description": "Exact key to view (optional). If provided, search_query is ignored.",
                "maxLength": 256
            },
            "search_query": {
                "type": "string",
                "description": "Keyword search query (optional). Searches keys, descriptions, and values."
            },
            "show_health_analysis": {
                "type": "boolean",
                "description": "Include comprehensive health metrics and analysis (default: false)"
            },
            "show_stale_entries": {
                "type": "boolean",
                "description": "Show only entries older than 30 days needing review (default: false)"
            },
            "include_backup_info": {
                "type": "boolean",
                "description": "Include backup recommendations and info (default: false)"
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of entries to return (default: 50)",
                "minimum": 1,
                "maximum": 200
            },
            "sort_by": {
                "type": "string",
                "description": "Sort entries by specified field (default: updated_at). 'last_updated' is accepted as a deprecated alias.",
                "enum": ["key", "updated_at", "last_updated", "size"],
                "default": "updated_at"
            }
        },
        "required": [],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        _principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let context_key_filter = arguments
                .get("context_key")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty());
            let search_query_filter = arguments
                .get("search_query")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty());
            let show_health_analysis = arguments
                .get("show_health_analysis")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let show_stale_entries = arguments
                .get("show_stale_entries")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let include_backup_info = arguments
                .get("include_backup_info")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            // Coerce + clamp to [1, 200], matching Python's defensive
            // re-clamp (R16-sweep sibling: schema validation alone
            // isn't trusted to have run).
            let max_results = arguments
                .get("max_results")
                .and_then(Value::as_i64)
                .unwrap_or(50)
                .clamp(1, 200) as usize;
            let sort_by_raw = arguments
                .get("sort_by")
                .and_then(Value::as_str)
                .unwrap_or("updated_at");
            let sort_by = if sort_by_raw == "last_updated" {
                "updated_at"
            } else {
                sort_by_raw
            };

            let guard = conn.lock().await;
            let all_rows = match project_context_repository::list_all(&guard) {
                Ok(rows) => rows,
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "A database error occurred; it has been logged. Retry, or ask \
                            an operator to check logs."
                            .to_string(),
                    }
                }
            };
            drop(guard);

            let now_dt: DateTime<Utc> = match parse_flexible(now) {
                Ok(dt) => dt,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Internal clock error".to_string(),
                    }
                }
            };

            let mut filtered: Vec<ProjectContextRow> = all_rows
                .iter()
                .filter(|row| {
                    if let Some(key) = context_key_filter {
                        row.context_key == key
                    } else if let Some(q) = search_query_filter {
                        row.context_key.contains(q)
                            || row.description.as_deref().unwrap_or("").contains(q)
                            || row.value.contains(q)
                    } else {
                        true
                    }
                })
                .filter(|row| {
                    !show_stale_entries
                        || is_older_than_days(&row.updated_at, now_dt, VIEW_STALE_DAYS)
                })
                .cloned()
                .collect();

            match sort_by {
                "size" => filtered.sort_by_key(|row| std::cmp::Reverse(row.value.chars().count())),
                "key" => filtered.sort_by(|a, b| a.context_key.cmp(&b.context_key)),
                _ => filtered.sort_by(|a, b| b.updated_at.cmp(&a.updated_at)),
            }
            filtered.truncate(max_results);

            let view = build_view_entries(filtered, now_dt);
            let entries_json: Vec<Value> = view.iter().map(view_entry_json).collect();
            let message = render_view_message(
                &view,
                context_key_filter,
                search_query_filter,
                show_stale_entries,
                show_health_analysis,
                include_backup_info,
                sort_by,
                &all_rows,
                now_dt,
            );

            ToolResult::Ok {
                data: Some(serde_json::json!({
                    "entries": entries_json,
                    "count": entries_json.len(),
                    "filters": {
                        "context_key": context_key_filter,
                        "search_query": search_query_filter,
                        "show_stale_entries": show_stale_entries,
                        "sort_by": sort_by,
                        "max_results": max_results,
                    },
                })),
                message: Some(message),
            }
        })
    }
}

fn config_key_error() -> ToolResult {
    // ADR-0016: the config_* namespace lives in the dedicated
    // project_settings store -- the knowledge write path rejects it
    // for EVERYONE (admin included). Invalid, not PermissionDenied
    // (worker-message clarity): PermissionDenied reads as an auth
    // failure the caller can't fix; Invalid steers them to pick a
    // different key or escalate.
    ToolResult::Invalid {
        field: Some("context_key".to_string()),
        message: "'config_*' keys are not stored in project memory -- they live in the \
            operator-managed project settings store. Choose a non-config_* key for memory, \
            or ask a project operator to set this via project settings."
            .to_string(),
    }
}

/// Port of `_check_write_authorization` -- the per-key creator-
/// ownership matrix every mutating tool in this module funnels
/// through. `None` = the caller may write/delete `context_key`.
fn check_write_authorization(
    conn: &Connection,
    requesting_agent_id: &str,
    context_key: &str,
    is_admin: bool,
) -> Option<ToolResult> {
    // FLAG-R17-2: length bound, checked before the admin early-return
    // so it applies to every caller.
    if context_key.chars().count() > conexus_core::schema_limits::IDENTIFIER_MAX_LEN {
        return Some(ToolResult::Invalid {
            field: Some("context_key".to_string()),
            message: format!(
                "context_key exceeds the maximum length of {} characters.",
                conexus_core::schema_limits::IDENTIFIER_MAX_LEN
            ),
        });
    }
    // ADR-0016: checked before the admin early-return -- config_* is
    // rejected for every caller on the knowledge write path.
    if CONFIG_KEY_RE.is_match(context_key) {
        return Some(config_key_error());
    }
    if is_admin {
        return None;
    }
    let existing = match project_context_repository::get(conn, context_key) {
        Ok(row) => row,
        Err(_e) => {
            return Some(ToolResult::Failed {
                message: "A database error occurred; it has been logged. Retry, or ask an \
                    operator to check logs."
                    .to_string(),
            })
        }
    };
    let existing = existing?;
    let creator_label = match existing.created_by.as_deref() {
        // Legacy rows where created_by is NULL (pre-migration backfill
        // edge case) cannot be safely attributed -- treat as
        // admin-only so workers can't claim them.
        None => "(unknown -- legacy entry)".to_string(),
        Some(creator) if creator == requesting_agent_id => return None,
        Some(creator) => creator.to_string(),
    };
    Some(ToolResult::PermissionDenied {
        reason: format!(
            "key '{context_key}' was created by '{creator_label}'; only its creator or admin \
             can modify it"
        ),
    })
}

pub struct CreateProjectContextTool;

impl Tool for CreateProjectContextTool {
    const NAME: &'static str = "create_project_context";
    const REQUIRED: Requirement = Requirement::Predicate {
        check: can_create_project_context,
        reason: WRITE_DENIED_REASON,
    };
    const DESCRIPTION: &'static str = "Create a NEW project context entry (insert-only; \
        fails if the key already exists -- use update_project_context to change an existing \
        key's value).";
    // "maxLength": 256 mirrors conexus_core::schema_limits::IDENTIFIER_MAX_LEN
    // -- kept as a literal (SCHEMA must be `const`-constructible) and
    // cross-checked against that constant by this module's own test.
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "context_key": {
                "type": "string",
                "description": "The key for this context entry (letters, digits, and . _ / - only).",
                "maxLength": 256
            },
            "context_value": {
                "description": "The value to store (any JSON type)."
            },
            "description": {
                "type": "string",
                "description": "Optional human-readable description of this entry."
            }
        },
        "required": ["context_key", "context_value"],
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
            let Some(context_key) = arguments
                .get("context_key")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            else {
                return ToolResult::Invalid {
                    field: Some("context_key".to_string()),
                    message: "context_key is required".to_string(),
                };
            };
            if !is_valid_memory_key(context_key) {
                return ToolResult::Invalid {
                    field: Some("context_key".to_string()),
                    message: "context_key may contain only letters, digits, and . _ / - \
                        (A-Z a-z 0-9 . _ / -)."
                        .to_string(),
                };
            }
            let Some(context_value) = arguments.get("context_value") else {
                return ToolResult::Invalid {
                    field: Some("context_value".to_string()),
                    message: "context_value is required".to_string(),
                };
            };
            let description = arguments.get("description").and_then(Value::as_str);

            // Value serialization is infallible for an already-parsed
            // serde_json::Value (unlike Python's json.dumps, which can
            // raise TypeError on a non-JSON-serializable Python object
            // -- not reachable here since context_value only ever
            // arrives as already-decoded JSON).
            let value_json_str = context_value.to_string();

            let requesting_agent_id = principal.map(Principal::actor_label).unwrap_or("unknown");
            let is_admin = principal.is_some_and(conexus_core::principal::is_operator_tier);

            let guard = conn.lock().await;
            if let Some(denial) =
                check_write_authorization(&guard, requesting_agent_id, context_key, is_admin)
            {
                return denial;
            }

            let created = project_context_repository::create_new(
                &guard,
                context_key,
                &value_json_str,
                description,
                requesting_agent_id,
                now,
            );
            match created {
                Ok(Some(_row)) => {}
                Ok(None) => {
                    return ToolResult::Conflict {
                        reason: format!(
                            "Memory key '{context_key}' already exists. \
                             create_project_context is insert-only -- use \
                             update_project_context to change an existing key's value."
                        ),
                    }
                }
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "A database error occurred; it has been logged. Retry, or \
                            ask an operator to check logs."
                            .to_string(),
                    }
                }
            };
            let _ = agent_action_repository::log_agent_action(
                &guard,
                requesting_agent_id,
                "created_memory",
                None,
                Some(&serde_json::json!({"context_key": context_key})),
                now,
            );
            drop(guard);

            // BL-R14-1: fire the full post-write wake set this key
            // requires. The write already committed above (rusqlite
            // has no separate commit step for a non-transaction
            // execute); a fresh short lock here is fine, matching
            // every other tool's own commit-then-notify sequencing.
            let wakes: Vec<&str> = {
                let guard = conn.lock().await;
                wake_notify::deliver(&guard, ctx.waiter_registry, context_key)
            }
            .into_iter()
            .map(|w| w.as_str())
            .collect();

            ToolResult::Ok {
                data: Some(serde_json::json!({"context_key": context_key, "wakes": wakes})),
                message: Some(format!("Memory '{context_key}' created successfully")),
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_core::capability::{AgentRole, Capabilities};
    use std::collections::HashSet;

    fn row(
        key: &str,
        value: &str,
        updated_at: &str,
        description: Option<&str>,
    ) -> ProjectContextRow {
        ProjectContextRow {
            context_key: key.to_string(),
            value: value.to_string(),
            description: description.map(str::to_string),
            created_at: Some(updated_at.to_string()),
            created_by: Some("alice".to_string()),
            updated_at: updated_at.to_string(),
            updated_by: "alice".to_string(),
        }
    }

    fn worker() -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some("alice".to_string()),
            project_name: None,
            project_role: None,
            agent_role: Some(AgentRole::Worker),
            can_wake_loop: true,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::from([Capability::MemoriesView])),
        }
    }

    fn viewer_operator() -> Principal {
        Principal {
            kind: PrincipalKind::ForwardingHeader,
            user_id: Some("op-1".to_string()),
            agent_id: None,
            project_name: Some("demo".to_string()),
            project_role: Some(conexus_core::capability::ProjectRole::Viewer),
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::from([Capability::MemoriesView])),
        }
    }

    fn write_operator() -> Principal {
        Principal {
            kind: PrincipalKind::ForwardingHeader,
            user_id: Some("op-2".to_string()),
            agent_id: None,
            project_name: Some("demo".to_string()),
            project_role: Some(conexus_core::capability::ProjectRole::Operator),
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Set(HashSet::from([
                Capability::MemoriesCreate,
                Capability::MemoriesUpdate,
                Capability::MemoriesDelete,
            ])),
        }
    }

    #[test]
    fn memory_key_validation_matches_python_charset() {
        assert!(is_valid_memory_key("api.endpoint-1/v2_beta"));
        assert!(!is_valid_memory_key(""));
        assert!(!is_valid_memory_key("has spaces"));
        assert!(!is_valid_memory_key("emoji🎉"));
    }

    #[test]
    fn a_worker_always_passes_the_write_gate_regardless_of_capability() {
        assert!(can_create_project_context(Some(&worker())));
        assert!(can_update_project_context(Some(&worker())));
        assert!(can_delete_project_context(Some(&worker())));
    }

    #[test]
    fn a_viewer_tier_operator_is_denied_every_write_capability() {
        let v = viewer_operator();
        assert!(!can_create_project_context(Some(&v)));
        assert!(!can_update_project_context(Some(&v)));
        assert!(!can_delete_project_context(Some(&v)));
    }

    #[test]
    fn a_write_capable_operator_passes() {
        let op = write_operator();
        assert!(can_create_project_context(Some(&op)));
        assert!(can_update_project_context(Some(&op)));
        assert!(can_delete_project_context(Some(&op)));
    }

    #[test]
    fn no_principal_is_denied_everywhere() {
        assert!(!is_authenticated_caller(None));
        assert!(!can_create_project_context(None));
    }

    #[test]
    fn empty_entries_return_the_full_no_data_shape() {
        let report = analyze_context_health(&[], Utc::now());
        assert_eq!(report.status, "no_data");
        assert_eq!(report.health_score, 100.0);
        assert_eq!(report.total, 0);
        assert_eq!(report.recommendations.len(), 1);
    }

    #[test]
    fn a_healthy_fresh_entry_scores_excellent() {
        let now: DateTime<Utc> = "2026-06-01T00:00:00Z".parse().unwrap();
        let entries = vec![row("a", "\"ok\"", "2026-06-01T00:00:00Z", Some("desc"))];
        let report = analyze_context_health(&entries, now);
        assert_eq!(report.status, "excellent");
        assert_eq!(report.json_errors, 0);
        assert_eq!(report.stale_entries, 0);
    }

    #[test]
    fn invalid_json_and_staleness_lower_the_score() {
        let now: DateTime<Utc> = "2026-06-01T00:00:00Z".parse().unwrap();
        let entries = vec![
            row("bad-json", "{not valid", "2026-06-01T00:00:00Z", Some("d")),
            row("stale", "\"ok\"", "2026-01-01T00:00:00Z", Some("d")),
        ];
        let report = analyze_context_health(&entries, now);
        assert_eq!(report.json_errors, 1);
        assert_eq!(report.stale_entries, 1);
        assert!(report.health_score < 100.0);
        assert!(report
            .recommendations
            .iter()
            .any(|r| r.contains("JSON parsing errors")));
    }

    #[test]
    fn a_large_entry_is_flagged() {
        let now: DateTime<Utc> = "2026-06-01T00:00:00Z".parse().unwrap();
        let big_value = "x".repeat(LARGE_ENTRY_CHARS + 1);
        let entries = vec![row("big", &big_value, "2026-06-01T00:00:00Z", Some("d"))];
        let report = analyze_context_health(&entries, now);
        assert_eq!(report.large_entries, 1);
    }

    #[test]
    fn consistency_check_flags_invalid_json_and_case_insensitive_duplicates() {
        let now: DateTime<Utc> = "2026-06-01T00:00:00Z".parse().unwrap();
        let entries = vec![
            row("Key1", "not json", "2026-06-01T00:00:00Z", Some("d")),
            row("key1", "\"ok\"", "2026-06-01T00:00:00Z", Some("d")),
        ];
        let report = check_context_consistency(&entries, now);
        assert_eq!(report.total_entries, 2);
        assert!(report.issues.iter().any(|i| i.contains("Invalid JSON")));
        assert!(report
            .issues
            .iter()
            .any(|i| i.contains("Potential duplicate keys")));
    }

    #[test]
    fn consistency_check_flags_missing_descriptions_and_old_entries() {
        let now: DateTime<Utc> = "2026-06-01T00:00:00Z".parse().unwrap();
        let entries = vec![
            row("no-desc", "\"ok\"", "2026-06-01T00:00:00Z", None),
            row("old", "\"ok\"", "2026-01-01T00:00:00Z", Some("d")),
        ];
        let report = check_context_consistency(&entries, now);
        assert!(report
            .warnings
            .iter()
            .any(|w| w.contains("Missing description")));
        assert!(report.warnings.iter().any(|w| w.contains("Old entry")));
    }

    #[test]
    fn consistency_check_flags_large_values() {
        let now: DateTime<Utc> = "2026-06-01T00:00:00Z".parse().unwrap();
        let big_value = "x".repeat(LARGE_VALUE_CHARS + 1);
        let entries = vec![row("big", &big_value, "2026-06-01T00:00:00Z", Some("d"))];
        let report = check_context_consistency(&entries, now);
        assert!(report.warnings.iter().any(|w| w.contains("Large entry")));
    }

    #[test]
    fn a_clean_context_produces_no_issues_or_warnings() {
        let now: DateTime<Utc> = "2026-06-01T00:00:00Z".parse().unwrap();
        let entries = vec![row("clean", "\"ok\"", "2026-06-01T00:00:00Z", Some("d"))];
        let report = check_context_consistency(&entries, now);
        assert!(report.issues.is_empty());
        assert!(report.warnings.is_empty());
    }

    // ── ValidateContextConsistencyTool ──────────────────────────────

    use conexus_auth::ToolCallContext;
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::file_map::FileMap;
    use conexus_wakeloop::waiter_registry::WaiterRegistry;

    async fn setup() -> AsyncMutex<Connection> {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        AsyncMutex::new(conn)
    }

    fn ctx<'a>(registry: &'a WaiterRegistry, file_map: &'a FileMap) -> ToolCallContext<'a> {
        ToolCallContext::off_wire(registry, file_map)
    }

    #[tokio::test]
    async fn an_empty_context_is_a_benign_ok_not_an_error() {
        let conn = setup().await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ValidateContextConsistencyTool::call(
            Some(&alice),
            &Value::Null,
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, message } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["total_entries"], 0);
        assert_eq!(message.unwrap(), "No project context entries found.");
    }

    #[tokio::test]
    async fn a_clean_populated_context_reports_no_issues() {
        let conn = setup().await;
        {
            let guard = conn.lock().await;
            project_context_repository::create_new(
                &guard,
                "a.key",
                "\"value\"",
                Some("a description"),
                "alice",
                "2026-06-01T00:00:00Z",
            )
            .unwrap();
        }
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ValidateContextConsistencyTool::call(
            Some(&alice),
            &Value::Null,
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, message } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["total_entries"], 1);
        assert!(message.unwrap().contains("No issues found"));
    }

    #[tokio::test]
    async fn an_unauthenticated_caller_is_denied() {
        let conn = setup().await;
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let denied =
            ValidateContextConsistencyTool::REQUIRED.check(None, &conexus_auth::NoPolicyOverrides);
        assert!(denied.is_err());
        // Sanity: the tool itself still runs fine if somehow reached
        // with no principal (dispatch would never allow this in
        // practice -- this is defense-in-depth, matching the tool's
        // own principal-independent body).
        let result = ValidateContextConsistencyTool::call(
            None,
            &Value::Null,
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
    }

    // ── ViewProjectContextTool ───────────────────────────────────────

    async fn seed_context(conn: &AsyncMutex<Connection>, key: &str, value: &str, now: &str) {
        let guard = conn.lock().await;
        project_context_repository::create_new(&guard, key, value, Some("d"), "alice", now)
            .unwrap();
    }

    #[tokio::test]
    async fn view_returns_all_entries_by_default() {
        let conn = setup().await;
        seed_context(&conn, "a", "\"one\"", "2026-06-01T00:00:00Z").await;
        seed_context(&conn, "b", "\"two\"", "2026-06-01T00:01:00Z").await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ViewProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({}),
            &conn,
            "2026-06-01T00:02:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["count"], 2);
    }

    #[tokio::test]
    async fn view_filters_by_exact_context_key() {
        let conn = setup().await;
        seed_context(&conn, "a", "\"one\"", "2026-06-01T00:00:00Z").await;
        seed_context(&conn, "b", "\"two\"", "2026-06-01T00:01:00Z").await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ViewProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"context_key": "a"}),
            &conn,
            "2026-06-01T00:02:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["count"], 1);
        assert_eq!(data["entries"][0]["key"], "a");
    }

    #[tokio::test]
    async fn view_search_query_matches_key_description_and_value() {
        let conn = setup().await;
        seed_context(
            &conn,
            "matching-key",
            "\"irrelevant\"",
            "2026-06-01T00:00:00Z",
        )
        .await;
        seed_context(
            &conn,
            "other",
            "\"has matching text\"",
            "2026-06-01T00:01:00Z",
        )
        .await;
        seed_context(&conn, "unrelated", "\"nothing\"", "2026-06-01T00:02:00Z").await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ViewProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"search_query": "matching"}),
            &conn,
            "2026-06-01T00:03:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["count"], 2);
    }

    #[tokio::test]
    async fn view_show_stale_entries_filters_to_only_stale_rows() {
        let conn = setup().await;
        seed_context(&conn, "fresh", "\"v\"", "2026-06-01T00:00:00Z").await;
        seed_context(&conn, "stale", "\"v\"", "2026-01-01T00:00:00Z").await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ViewProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"show_stale_entries": true}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["count"], 1);
        assert_eq!(data["entries"][0]["key"], "stale");
    }

    #[tokio::test]
    async fn view_sorts_by_key_ascending() {
        let conn = setup().await;
        seed_context(&conn, "zebra", "\"v\"", "2026-06-01T00:00:00Z").await;
        seed_context(&conn, "alpha", "\"v\"", "2026-06-01T00:01:00Z").await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ViewProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"sort_by": "key"}),
            &conn,
            "2026-06-01T00:02:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let entries = data.unwrap()["entries"].as_array().unwrap().clone();
        assert_eq!(entries[0]["key"], "alpha");
        assert_eq!(entries[1]["key"], "zebra");
    }

    #[tokio::test]
    async fn view_last_updated_is_accepted_as_a_deprecated_alias_for_updated_at() {
        let conn = setup().await;
        seed_context(&conn, "older", "\"v\"", "2026-06-01T00:00:00Z").await;
        seed_context(&conn, "newer", "\"v\"", "2026-06-01T00:05:00Z").await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ViewProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"sort_by": "last_updated"}),
            &conn,
            "2026-06-01T00:06:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["filters"]["sort_by"], "updated_at");
        // updated_at descending -> newest first.
        assert_eq!(data["entries"][0]["key"], "newer");
    }

    #[tokio::test]
    async fn view_max_results_is_clamped_into_range() {
        let conn = setup().await;
        for i in 0..3 {
            seed_context(
                &conn,
                &format!("k{i}"),
                "\"v\"",
                &format!("2026-06-01T00:0{i}:00Z"),
            )
            .await;
        }
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ViewProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"max_results": 1}),
            &conn,
            "2026-06-01T00:10:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["count"], 1);
    }

    #[tokio::test]
    async fn view_flags_invalid_json_and_renders_it_unquoted_in_the_preview() {
        let conn = setup().await;
        {
            let guard = conn.lock().await;
            project_context_repository::create_new(
                &guard,
                "raw-string",
                "not valid json",
                Some("d"),
                "alice",
                "2026-06-01T00:00:00Z",
            )
            .unwrap();
        }
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ViewProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, message } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["entries"][0]["_metadata"]["json_valid"], false);
        assert_eq!(data["entries"][0]["value"], "not valid json");
        assert!(message.unwrap().contains("Value: not valid json"));
    }

    #[tokio::test]
    async fn view_health_analysis_is_included_when_requested() {
        let conn = setup().await;
        seed_context(&conn, "a", "\"ok\"", "2026-06-01T00:00:00Z").await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = ViewProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"show_health_analysis": true}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { message, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert!(message.unwrap().contains("Context Health"));
    }

    #[test]
    fn python_str_matches_python_semantics_for_scalars() {
        assert_eq!(python_str(&Value::Null), "None");
        assert_eq!(python_str(&Value::Bool(true)), "True");
        assert_eq!(python_str(&Value::Bool(false)), "False");
        assert_eq!(python_str(&Value::String("hello".to_string())), "hello");
    }

    #[test]
    fn view_schema_max_length_matches_the_shared_constant() {
        let parsed: Value = serde_json::from_str(ViewProjectContextTool::SCHEMA).unwrap();
        let max_len = parsed["properties"]["context_key"]["maxLength"]
            .as_u64()
            .unwrap();
        assert_eq!(
            max_len as usize,
            conexus_core::schema_limits::IDENTIFIER_MAX_LEN
        );
    }

    // ── CreateProjectContextTool ─────────────────────────────────────

    #[tokio::test]
    async fn a_worker_can_create_a_new_key() {
        let conn = setup().await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = CreateProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"context_key": "a.new.key", "context_value": "hello"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["context_key"], "a.new.key");
    }

    #[tokio::test]
    async fn creating_an_existing_key_is_a_conflict_not_an_overwrite() {
        let conn = setup().await;
        seed_context(&conn, "dup", "\"v1\"", "2026-06-01T00:00:00Z").await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = CreateProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"context_key": "dup", "context_value": "v2"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
    }

    #[tokio::test]
    async fn an_invalid_charset_key_is_invalid() {
        let conn = setup().await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = CreateProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"context_key": "has spaces", "context_value": "v"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn a_config_namespaced_key_is_rejected_for_everyone_including_admin() {
        let conn = setup().await;
        let op = write_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = CreateProjectContextTool::call(
            Some(&op),
            &serde_json::json!({"context_key": "config_x", "context_value": "v"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn a_viewer_tier_operator_cannot_create_a_key() {
        let conn = setup().await;
        let viewer = viewer_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let denied = CreateProjectContextTool::REQUIRED
            .check(Some(&viewer), &conexus_auth::NoPolicyOverrides);
        assert!(denied.is_err());
        let _ = (conn, c);
    }

    #[tokio::test]
    async fn a_wake_eligible_key_is_unreachable_here_because_config_rejection_wins_first() {
        // Both of wakes_for()'s classified patterns
        // (`config_allow_worker_*` / `config_auto_event_loop_global`)
        // are themselves config_*-namespaced, and this file's own
        // check_write_authorization unconditionally rejects EVERY
        // config_* key before a write ever reaches wake_notify::deliver
        // -- exactly matching Python's own control flow (`_check_write_
        // authorization` runs, and returns early, before
        // `emit_context_write_wakes` is ever called). Real, deliberate
        // consequence: wake delivery is correctly wired here (per this
        // module's own decision -- see wake_notify::deliver's doc) but
        // is dead code IN PRACTICE for every write tool in THIS file --
        // never actually reachable, not a bug. wake_notify::deliver's
        // own test module exercises the broadcast behavior directly
        // against a non-namespace-restricted caller instead.
        let conn = setup().await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let (_sender, mut receiver) = registry.register("alice");
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = CreateProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({
                "context_key": "config_auto_event_loop_global", "context_value": true
            }),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
        assert!(receiver.try_recv().is_err());
    }

    #[test]
    fn create_schema_max_length_matches_the_shared_constant() {
        let parsed: Value = serde_json::from_str(CreateProjectContextTool::SCHEMA).unwrap();
        let max_len = parsed["properties"]["context_key"]["maxLength"]
            .as_u64()
            .unwrap();
        assert_eq!(
            max_len as usize,
            conexus_core::schema_limits::IDENTIFIER_MAX_LEN
        );
    }
}
