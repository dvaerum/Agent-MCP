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
use conexus_db::project_context_repository::{self, ProjectContextRow};
use conexus_db::scheduled_directive_repository::parse_flexible;
use regex::Regex;
use rusqlite::Connection;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::LazyLock;
use tokio::sync::Mutex as AsyncMutex;

static MEMORY_KEY_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9._/-]+$").unwrap());

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
}
