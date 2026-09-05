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

use crate::python_compat::python_str;
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

/// Port of `utils/string_utils.py::has_unsafe_unicode_for_identifier`
/// (F005 verify-all-v6 MUTATING #3) — a narrower denylist than
/// [`is_valid_memory_key`]'s ASCII allowlist above (every character
/// this rejects is ALSO rejected by that allowlist), kept as its own
/// check purely so the REST create/update handlers can give a more
/// specific spoofing-aware message for this subset before falling
/// back to the generic allowlist message for everything else --
/// `tests/test_memories_unsafe_unicode_key.py` pins the distinct
/// wording, so this is a real, tested contract, not a redundant
/// no-op. Written as explicit codepoint-range matches rather than a
/// regex over literal invisible/bidi characters embedded in source
/// (which the Python module deliberately avoids too, spelling out the
/// exact ranges in a comment for the same reason: an invisible
/// character sitting in source code is itself a spoofing risk for
/// whoever next edits the file).
pub fn has_unsafe_unicode_for_identifier(value: &str) -> bool {
    value.chars().any(|ch| {
        let cp = ch as u32;
        matches!(cp,
            0x00..=0x1F | 0x7F                     // C0 controls + DEL
            | 0x200B..=0x200F                       // ZWSP/ZWNJ/ZWJ/LRM/RLM
            | 0x2028..=0x2029                       // line/paragraph separator
            | 0x202A..=0x202E                       // PDF/LRE/RLE/LRO/RLO bidi overrides
            | 0x2060..=0x2064                       // word joiner, function application, ...
            | 0x2066..=0x2069                       // LRI/RLI/FSI/PDI bidi isolates
            | 0x206A..=0x206F                       // deprecated bidi controls
            | 0xFEFF                                // BOM / zero-width no-break space
        )
    })
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

/// Port of `_analyze_context_health`'s full return shape. `Serialize`
/// (Phase D5, `backup_project_context`) embeds this directly as the
/// backup JSON's `health_report` field -- field names already match
/// Python's dict keys verbatim, so the derive needs no renaming.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
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

/// Python's `str.title()`: uppercase the first letter of every
/// maximal alphabetic run, lowercase every other letter in that run;
/// a non-alphabetic character (here, always `_`) is left untouched
/// and starts a new word. Every health-status string this crate
/// renders is closed-set lowercase-with-underscores
/// (`needs_attention`, `no_data`, ...), so a naive "capitalize only
/// the very first character" port is wrong for any status past the
/// first word: Python gives `"Needs_Attention"`, a first-char-only
/// port gives `"Needs_attention"`.
/// The emoji Python's `health_icon` ternary chain picks for a health
/// status -- shared by `render_view_message` and
/// `backup_project_context`'s response text, which both render it.
fn health_status_icon(status: &str) -> &'static str {
    match status {
        "excellent" => "\u{1f7e2}",
        "good" => "\u{1f7e1}",
        "needs_attention" => "\u{1f7e0}",
        _ => "\u{1f534}",
    }
}

fn python_title_case(s: &str) -> String {
    let mut result = String::with_capacity(s.len());
    let mut at_word_start = true;
    for c in s.chars() {
        if c.is_alphabetic() {
            if at_word_start {
                result.extend(c.to_uppercase());
            } else {
                result.extend(c.to_lowercase());
            }
            at_word_start = false;
        } else {
            result.push(c);
            at_word_start = true;
        }
    }
    result
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
        let health_icon = health_status_icon(&health.status);
        let status_title = python_title_case(&health.status);
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

/// Port of `_single_update_inline` -- upsert one key, gated by
/// [`check_write_authorization`]. `Ok(true)` if the key was newly
/// created (matches `upsert`'s own `created` flag; unused by the
/// caller's response shape today but kept for parity/future use).
fn single_update_project_context(
    conn: &Connection,
    requesting_agent_id: &str,
    context_key: &str,
    value_json_str: &str,
    description: Option<&str>,
    is_admin: bool,
    now: &str,
) -> Result<bool, ToolResult> {
    if let Some(denial) =
        check_write_authorization(conn, requesting_agent_id, context_key, is_admin)
    {
        return Err(denial);
    }
    let (_, created) = project_context_repository::upsert(
        conn,
        context_key,
        value_json_str,
        description,
        description.is_some(),
        requesting_agent_id,
        now,
    )
    .map_err(|_e| ToolResult::Failed {
        message: "A database error occurred; it has been logged. Retry, or ask an operator to \
            check logs."
            .to_string(),
    })?;
    let _ = agent_action_repository::log_agent_action(
        conn,
        requesting_agent_id,
        "updated_context",
        None,
        Some(&serde_json::json!({"context_key": context_key, "action": "set/update"})),
        now,
    );
    Ok(created)
}

/// One item's outcome in a bulk update -- Python's `results`/
/// `failed_updates` string-accumulation, made structural instead of a
/// pair of parallel `Vec<String>`s (matches this migration's own
/// closed-enum-over-substring-formatting precedent, e.g.
/// `UpdateSingleTaskOutcome`).
enum BulkUpdateOutcome {
    Updated { context_key: String },
    Failed { context_key: String, reason: String },
}

/// Port of `_bulk_update_inline`. Phase 1 authorizes every key up
/// front -- ANY denial aborts the WHOLE batch (returns `Err`), no
/// writes land. Phase 2 applies each update; a per-item failure (only
/// a genuine DB error is possible here -- context_value serialization
/// is infallible for an already-parsed JSON `Value`, unlike Python's
/// `json.dumps`) is recorded and does NOT abort the batch, matching
/// Python's "atomic on authorization, not on per-item success"
/// design.
fn bulk_update_project_context_entries(
    conn: &Connection,
    requesting_agent_id: &str,
    updates: &[Value],
    is_admin: bool,
    now: &str,
) -> Result<Vec<BulkUpdateOutcome>, ToolResult> {
    // Phase 1: authorize every key up front.
    for update in updates {
        let Some(key) = update.get("context_key").and_then(Value::as_str) else {
            continue;
        };
        if let Some(denial) = check_write_authorization(conn, requesting_agent_id, key, is_admin) {
            return Err(denial);
        }
    }

    // Phase 2: apply each update.
    let mut outcomes = Vec::with_capacity(updates.len());
    for (i, update) in updates.iter().enumerate() {
        let context_key = update
            .get("context_key")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        let Some(context_value) = update.get("context_value") else {
            outcomes.push(BulkUpdateOutcome::Failed {
                context_key: context_key.to_string(),
                reason: "context_value is required".to_string(),
            });
            continue;
        };
        // BL-R22-1: `description_provided` is true iff the caller's
        // item literally HAS a `description` key (Python: `"description"
        // in update`), regardless of its value -- an explicit
        // `"description": null` counts as provided (clears the field)
        // and must NOT fall through to the junk default text below,
        // which exists only to seed a brand-new key's first CREATE.
        let description_provided = update.get("description").is_some();
        let description: Option<String> = match update.get("description") {
            None => Some(format!("Bulk update operation {}", i + 1)),
            Some(Value::Null) => None,
            Some(Value::String(s)) => Some(s.clone()),
            Some(other) => Some(other.to_string()),
        };

        let value_json_str = context_value.to_string();
        let result = project_context_repository::upsert(
            conn,
            context_key,
            &value_json_str,
            description.as_deref(),
            description_provided,
            requesting_agent_id,
            now,
        );
        match result {
            Ok(_) => {
                outcomes.push(BulkUpdateOutcome::Updated {
                    context_key: context_key.to_string(),
                });
                let _ = agent_action_repository::log_agent_action(
                    conn,
                    requesting_agent_id,
                    "bulk_updated_context",
                    None,
                    Some(&serde_json::json!({
                        "context_key": context_key,
                        "operation": format!("bulk_update_{}", i + 1),
                    })),
                    now,
                );
            }
            Err(_e) => outcomes.push(BulkUpdateOutcome::Failed {
                context_key: context_key.to_string(),
                reason: "database error".to_string(),
            }),
        }
    }
    Ok(outcomes)
}

fn render_bulk_summary(outcomes: &[BulkUpdateOutcome]) -> Vec<String> {
    let successful: Vec<&str> = outcomes
        .iter()
        .filter_map(|o| match o {
            BulkUpdateOutcome::Updated { context_key } => Some(context_key.as_str()),
            BulkUpdateOutcome::Failed { .. } => None,
        })
        .collect();
    let failed: Vec<(&str, &str)> = outcomes
        .iter()
        .filter_map(|o| match o {
            BulkUpdateOutcome::Failed {
                context_key,
                reason,
            } => Some((context_key.as_str(), reason.as_str())),
            BulkUpdateOutcome::Updated { .. } => None,
        })
        .collect();

    let mut parts = vec![format!(
        "Bulk update completed: {} successful, {} failed",
        successful.len(),
        failed.len()
    )];
    if !successful.is_empty() {
        parts.push("\nSuccessful updates:".to_string());
        parts.extend(successful.iter().map(|k| format!("\u{2713} Updated '{k}'")));
    }
    if !failed.is_empty() {
        parts.push("\nFailed updates:".to_string());
        parts.extend(
            failed
                .iter()
                .map(|(k, reason)| format!("\u{2717} Failed '{k}': {reason}")),
        );
    }
    parts
}

pub struct UpdateProjectContextTool;

impl Tool for UpdateProjectContextTool {
    const NAME: &'static str = "update_project_context";
    const REQUIRED: Requirement = Requirement::Predicate {
        check: can_update_project_context,
        reason: WRITE_DENIED_REASON,
    };
    const DESCRIPTION: &'static str = "Add or update a project context entry with a specific \
        key. The value can be any JSON-serializable type.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "context_key": {
                "type": "string",
                "description": "The exact key for the context entry (e.g., 'api.service_x.url')."
            },
            "context_value": {
                "description": "The JSON-serializable value to set (e.g., string, number, list, dict).",
                "anyOf": [
                    {"type": "string"},
                    {"type": "number"},
                    {"type": "boolean"},
                    {"type": "null"},
                    {"type": "object", "additionalProperties": true},
                    {"type": "array"}
                ]
            },
            "description": {
                "type": "string",
                "description": "Optional description of this context entry."
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
            let requesting_agent_id = principal.map(Principal::actor_label).unwrap_or("unknown");
            let is_admin = principal.is_some_and(conexus_core::principal::is_operator_tier);

            // Support both single and bulk operations, matching
            // Python's own dispatch-by-argument-shape (the JSON schema
            // above doesn't declare `updates` at all -- this crate's
            // dispatcher doesn't enforce JSON-schema validation on
            // `tools/call` arguments either, matching Python's own
            // real behavior, so this branch is genuinely reachable).
            if let Some(updates_value) = arguments.get("updates") {
                let Some(updates) = updates_value.as_array().filter(|a| !a.is_empty()) else {
                    return ToolResult::Invalid {
                        field: Some("updates".to_string()),
                        message: "updates must be a non-empty list for bulk operations."
                            .to_string(),
                    };
                };

                let guard = conn.lock().await;
                let outcomes = match bulk_update_project_context_entries(
                    &guard,
                    requesting_agent_id,
                    updates,
                    is_admin,
                    now,
                ) {
                    Ok(o) => o,
                    Err(denial) => return denial,
                };
                drop(guard);

                let keys: Vec<&str> = updates
                    .iter()
                    .filter_map(|u| u.get("context_key").and_then(Value::as_str))
                    .collect();
                let wakes: Vec<&str> = {
                    let guard = conn.lock().await;
                    wake_notify::deliver_bulk(&guard, ctx.waiter_registry, keys)
                }
                .into_iter()
                .map(|w| w.as_str())
                .collect();

                let summary = render_bulk_summary(&outcomes);
                return ToolResult::Ok {
                    data: Some(serde_json::json!({
                        "updates_attempted": updates.len(),
                        "summary_lines": summary,
                        "wakes": wakes,
                    })),
                    message: Some(summary.join("\n")),
                };
            }

            let Some(context_key) = arguments
                .get("context_key")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            else {
                return ToolResult::Invalid {
                    field: Some("context_key".to_string()),
                    message: "context_key and context_value are required for single updates."
                        .to_string(),
                };
            };
            let Some(context_value) = arguments.get("context_value") else {
                return ToolResult::Invalid {
                    field: Some("context_value".to_string()),
                    message: "context_key and context_value are required for single updates."
                        .to_string(),
                };
            };
            let description = arguments.get("description").and_then(Value::as_str);
            let value_json_str = context_value.to_string();

            let guard = conn.lock().await;
            if let Err(denial) = single_update_project_context(
                &guard,
                requesting_agent_id,
                context_key,
                &value_json_str,
                description,
                is_admin,
                now,
            ) {
                return denial;
            }
            drop(guard);

            let wakes: Vec<&str> = {
                let guard = conn.lock().await;
                wake_notify::deliver(&guard, ctx.waiter_registry, context_key)
            }
            .into_iter()
            .map(|w| w.as_str())
            .collect();

            ToolResult::Ok {
                data: Some(serde_json::json!({"context_key": context_key, "wakes": wakes})),
                message: Some(format!(
                    "Project context updated successfully for key '{context_key}'."
                )),
            }
        })
    }
}

pub struct BulkUpdateProjectContextTool;

impl Tool for BulkUpdateProjectContextTool {
    const NAME: &'static str = "bulk_update_project_context";
    const REQUIRED: Requirement = Requirement::Predicate {
        check: can_update_project_context,
        reason: WRITE_DENIED_REASON,
    };
    const DESCRIPTION: &'static str = "Update multiple project context entries atomically. \
        Essential for large-scale context corrections.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "updates": {
                "type": "array",
                "description": "Array of update operations",
                "items": {
                    "type": "object",
                    "properties": {
                        "context_key": {
                            "type": "string",
                            "description": "The context key to update"
                        },
                        "context_value": {
                            "description": "The new value (any JSON-serializable type)",
                            "anyOf": [
                                {"type": "string"},
                                {"type": "number"},
                                {"type": "boolean"},
                                {"type": "null"},
                                {"type": "object"},
                                {"type": "array"}
                            ]
                        },
                        "description": {
                            "type": "string",
                            "description": "Optional description for this update"
                        }
                    },
                    "required": ["context_key", "context_value"],
                    "additionalProperties": false
                }
            }
        },
        "required": ["updates"],
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
            let Some(updates) = arguments.get("updates").and_then(Value::as_array) else {
                return ToolResult::Invalid {
                    field: Some("updates".to_string()),
                    message: "updates array is required.".to_string(),
                };
            };
            if updates.is_empty() {
                return ToolResult::Invalid {
                    field: Some("updates".to_string()),
                    message: "updates array is required.".to_string(),
                };
            }
            // This standalone tool validates every item's SHAPE up
            // front (a genuine, documented difference from
            // update_project_context's bulk arm, which tolerates a
            // malformed item by failing just that item during Phase 2
            // -- ported as-is, not reconciled, per this migration's
            // "re-derive the documented behavior" discipline).
            for (i, update) in updates.iter().enumerate() {
                if !update.is_object() {
                    return ToolResult::Invalid {
                        field: Some(format!("updates[{i}]")),
                        message: format!("Update {i} must be an object."),
                    };
                }
                if update.get("context_key").is_none() {
                    return ToolResult::Invalid {
                        field: Some(format!("updates[{i}].context_key")),
                        message: format!("Update {i} missing required 'context_key'."),
                    };
                }
                if update.get("context_value").is_none() {
                    return ToolResult::Invalid {
                        field: Some(format!("updates[{i}].context_value")),
                        message: format!("Update {i} missing required 'context_value'."),
                    };
                }
            }

            let requesting_agent_id = principal.map(Principal::actor_label).unwrap_or("unknown");
            let is_admin = principal.is_some_and(conexus_core::principal::is_operator_tier);

            let guard = conn.lock().await;
            let outcomes = match bulk_update_project_context_entries(
                &guard,
                requesting_agent_id,
                updates,
                is_admin,
                now,
            ) {
                Ok(o) => o,
                Err(denial) => return denial,
            };
            drop(guard);

            let keys: Vec<&str> = updates
                .iter()
                .filter_map(|u| u.get("context_key").and_then(Value::as_str))
                .collect();
            let wakes: Vec<&str> = {
                let guard = conn.lock().await;
                wake_notify::deliver_bulk(&guard, ctx.waiter_registry, keys)
            }
            .into_iter()
            .map(|w| w.as_str())
            .collect();

            let summary = render_bulk_summary(&outcomes);
            ToolResult::Ok {
                data: Some(serde_json::json!({
                    "updates_attempted": updates.len(),
                    "summary_lines": summary,
                    "wakes": wakes,
                })),
                message: Some(summary.join("\n")),
            }
        })
    }
}

/// Critical system keys that require `force_delete=true`. Matches
/// Python's own loose prefix-or-exact match (`key.startswith(pattern.
/// split("_")[0] + "_") or key == pattern`) verbatim -- e.g.
/// `"server_anything"` matches the `"server_startup"` pattern via its
/// `"server_"` prefix, not just the literal string.
const CRITICAL_KEY_PATTERNS: &[&str] = &[
    "server_startup",
    "database_version",
    "system_config",
    "mcp_server_url",
];

fn critical_key_match(key: &str, pattern: &str) -> bool {
    let prefix = format!("{}_", pattern.split('_').next().unwrap_or(pattern));
    key.starts_with(&prefix) || key == pattern
}

pub struct DeleteProjectContextTool;

impl Tool for DeleteProjectContextTool {
    const NAME: &'static str = "delete_project_context";
    const REQUIRED: Requirement = Requirement::Predicate {
        check: can_delete_project_context,
        reason: WRITE_DENIED_REASON,
    };
    const DESCRIPTION: &'static str = "Delete project context entries permanently. Admin-only \
        operation with safety checks for critical system keys.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "context_key": {
                "type": "string",
                "description": "Single context key to delete (alternative to context_keys)"
            },
            "context_keys": {
                "type": "array",
                "description": "List of context keys to delete",
                "items": {"type": "string"},
                "minItems": 1
            },
            "force_delete": {
                "type": "boolean",
                "description": "Force deletion even for critical system keys (default: false)",
                "default": false
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
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let mut keys_to_delete: Vec<String> = Vec::new();
            if let Some(k) = arguments.get("context_key").and_then(Value::as_str) {
                if !k.is_empty() {
                    keys_to_delete.push(k.to_string());
                }
            }
            if let Some(arr) = arguments.get("context_keys").and_then(Value::as_array) {
                keys_to_delete.extend(arr.iter().filter_map(Value::as_str).map(str::to_string));
            }
            if keys_to_delete.is_empty() {
                return ToolResult::Invalid {
                    field: Some("context_key".to_string()),
                    message: "No context keys specified for deletion".to_string(),
                };
            }
            let force_delete = arguments
                .get("force_delete")
                .and_then(Value::as_bool)
                .unwrap_or(false);

            // ADR-0016: reject config_* up front, checked BEFORE the
            // critical-key guard, so the caller gets the category
            // error rather than a misleading force_delete hint.
            if keys_to_delete.iter().any(|k| CONFIG_KEY_RE.is_match(k)) {
                return config_key_error();
            }

            let critical_keys_found: Vec<&String> = keys_to_delete
                .iter()
                .filter(|k| {
                    CRITICAL_KEY_PATTERNS
                        .iter()
                        .any(|p| critical_key_match(k, p))
                })
                .collect();
            if !critical_keys_found.is_empty() && !force_delete {
                return ToolResult::Invalid {
                    field: Some("force_delete".to_string()),
                    message: format!(
                        "Cannot delete critical system keys without force_delete=true: {:?}",
                        critical_keys_found
                    ),
                };
            }

            let requesting_agent_id = principal.map(Principal::actor_label).unwrap_or("unknown");
            let is_admin = principal.is_some_and(conexus_core::principal::is_operator_tier);

            let guard = conn.lock().await;

            for key in &keys_to_delete {
                if let Some(denial) =
                    check_write_authorization(&guard, requesting_agent_id, key, is_admin)
                {
                    return denial;
                }
            }

            let key_refs: Vec<&str> = keys_to_delete.iter().map(String::as_str).collect();
            let deleted_rows = match project_context_repository::delete_many(&guard, &key_refs) {
                Ok(rows) => rows,
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "A database error occurred; it has been logged. Retry, or ask \
                            an operator to check logs."
                            .to_string(),
                    }
                }
            };

            if deleted_rows.is_empty() {
                return ToolResult::NotFound {
                    resource: "project_context".to_string(),
                    identifier: keys_to_delete.join(", "),
                    hint: None,
                };
            }

            let deletion_details: Vec<(String, String, bool)> = deleted_rows
                .iter()
                .map(|row| {
                    let was_critical = critical_keys_found.iter().any(|k| **k == row.context_key);
                    (
                        row.context_key.clone(),
                        row.description.clone().unwrap_or_default(),
                        was_critical,
                    )
                })
                .collect();
            let deleted_keys: Vec<&str> = deletion_details
                .iter()
                .map(|(k, _, _)| k.as_str())
                .collect();

            let _ = agent_action_repository::log_agent_action(
                &guard,
                requesting_agent_id,
                "deleted_context",
                None,
                Some(&serde_json::json!({
                    "deleted_keys": deleted_keys,
                    "critical_keys_deleted": critical_keys_found,
                    "force_delete": force_delete,
                    "total_deleted": deleted_rows.len(),
                })),
                now,
            );

            // BL-R4-1: prune each deleted key's RAG chunk + hash
            // watermark in the SAME transaction/connection as the row
            // delete -- the incremental indexer never sweeps orphans,
            // so a deleted context row's chunk would otherwise stay
            // queryable via ask_project_rag forever.
            for key in &deleted_keys {
                let _ = conexus_db::rag_repository::purge_source(&guard, "context", key);
            }

            drop(guard);

            // SEC-C/F5: fire the same wake seam every other write
            // surface in this file uses -- this delete path was the
            // last one that bypassed it entirely.
            let wakes: Vec<&str> = {
                let guard = conn.lock().await;
                wake_notify::deliver_bulk(&guard, ctx.waiter_registry, deleted_keys.clone())
            }
            .into_iter()
            .map(|w| w.as_str())
            .collect();

            let mut response_parts = vec![format!(
                "Deleted {} project context entries successfully:",
                deletion_details.len()
            )];
            for (key, description, was_critical) in &deletion_details {
                let mut line = format!("  \u{2022} {key}");
                if !description.is_empty() {
                    line.push_str(&format!(" ({description})"));
                }
                if *was_critical {
                    line.push_str(" [CRITICAL]");
                }
                response_parts.push(line);
            }
            if !critical_keys_found.is_empty() {
                response_parts.push(format!(
                    "\n\u{26a0}\u{fe0f}  WARNING: {} critical system keys were deleted!",
                    critical_keys_found.len()
                ));
                response_parts.push(
                    "System functionality may be affected. Consider backing up before restart."
                        .to_string(),
                );
            }
            response_parts.push(format!("\nDeletion completed at: {now}"));

            ToolResult::Ok {
                data: Some(serde_json::json!({
                    "deleted_count": deletion_details.len(),
                    "deleted_keys": deleted_keys,
                    "critical_keys_deleted": critical_keys_found,
                    "force_delete": force_delete,
                    "wakes": wakes,
                })),
                message: Some(response_parts.join("\n")),
            }
        })
    }
}

/// VULN-003, primary gate: in every other Python tool in this file,
/// this crate's dispatcher skipping `tools/call` argument validation
/// against `SCHEMA` is safe because nothing in the schema was ever
/// load-bearing for security. HERE it is NOT safe to skip -- Python's
/// own comment on this exact schema `pattern` calls it the primary
/// path-traversal defense, with the `resolve()`/`relative_to()` check
/// in the impl as only a SECOND, belt-and-suspenders layer for
/// in-process callers that bypass schema validation. Since this
/// crate's dispatcher never runs schema validation for ANY tool, the
/// "second layer" is the ONLY layer here unless this check is made
/// explicit in the tool body -- so it is, checked before anything
/// else runs.
static BACKUP_NAME_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9._-]{1,128}$").unwrap());

/// Port of `_create_context_backup`. `now` doubles as both the
/// auto-generated name's timestamp component and the `created_at`
/// field -- Python re-reads the wall clock for each
/// (`datetime.datetime.now()` twice); this crate's "one injected `now`
/// per call" convention collapses that to one value, which can only
/// ever matter at a microsecond level neither call site's output is
/// sensitive to.
fn create_context_backup(
    entries: Vec<ProjectContextRow>,
    backup_name: Option<&str>,
    now: DateTime<Utc>,
) -> Value {
    let backup_name = backup_name
        .map(str::to_string)
        .unwrap_or_else(|| format!("context_backup_{}", now.format("%Y%m%d_%H%M%S")));
    serde_json::json!({
        "backup_name": backup_name,
        "created_at": now.to_rfc3339(),
        "total_entries": entries.len(),
        "entries": entries,
    })
}

pub struct BackupProjectContextTool;

impl Tool for BackupProjectContextTool {
    const NAME: &'static str = "backup_project_context";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::SystemConfigWrite,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Create comprehensive backup of all project context with \
        health analysis. Admin-only operation for data safety and recovery.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "backup_name": {
                "type": "string",
                "pattern": "^[A-Za-z0-9._-]{1,128}$",
                "description": "Optional custom backup name (auto-generated if not provided). Slug — alphanumeric plus . _ - only, max 128 chars."
            },
            "include_health_report": {
                "type": "boolean",
                "description": "Include health analysis in backup (default: true)",
                "default": true
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
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let backup_name_arg = arguments.get("backup_name").and_then(Value::as_str);
            if let Some(name) = backup_name_arg {
                if !BACKUP_NAME_RE.is_match(name) {
                    return ToolResult::Invalid {
                        field: Some("backup_name".to_string()),
                        message: "backup_name may contain only letters, digits, and . _ - \
                            (A-Z a-z 0-9 . _ -), max 128 chars."
                            .to_string(),
                    };
                }
            }
            let include_health_report = arguments
                .get("include_health_report")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let requesting_agent_id = principal.map(Principal::actor_label).unwrap_or("unknown");

            let now_dt: DateTime<Utc> = match now.parse() {
                Ok(dt) => dt,
                Err(_) => Utc::now(),
            };

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
            let health_report = if include_health_report {
                Some(analyze_context_health(&entries, now_dt))
            } else {
                None
            };
            let total_entries = entries.len();
            let mut backup_data = create_context_backup(entries, backup_name_arg, now_dt);
            if let Some(health) = &health_report {
                backup_data["health_report"] =
                    serde_json::to_value(health).expect("ContextHealthReport always serializes");
            }
            let backup_name = backup_data["backup_name"].as_str().unwrap().to_string();

            // Save to <project_dir>/.agent/backups/context/<name>.json.
            let backup_dir = ctx
                .project_dir
                .join(".agent")
                .join("backups")
                .join("context");
            if let Err(_e) = std::fs::create_dir_all(&backup_dir) {
                return ToolResult::Failed {
                    message: "A database error occurred; it has been logged. Retry, or ask an \
                        operator to check logs."
                        .to_string(),
                };
            }
            let backup_filename = format!("{backup_name}.json");

            // VULN-003 defense-in-depth (see BACKUP_NAME_RE's doc for
            // why the primary gate above is load-bearing here, unlike
            // every other tool in this file): resolve the candidate
            // path and verify it stays inside the backup directory
            // before writing. Since backup_filename is entirely
            // derived from a BACKUP_NAME_RE-validated slug (no `/`,
            // no `..`, no NUL), this can never actually fail today --
            // kept as belt-and-suspenders against a future change to
            // how backup_filename is derived, matching Python's own
            // stated rationale for keeping this check at all.
            let backup_dir_resolved = match std::fs::canonicalize(&backup_dir) {
                Ok(p) => p,
                Err(_e) => {
                    return ToolResult::Invalid {
                        field: Some("backup_name".to_string()),
                        message: "backup_name resolves outside the backup directory".to_string(),
                    }
                }
            };
            let backup_path_resolved = backup_dir_resolved.join(&backup_filename);
            if !backup_path_resolved.starts_with(&backup_dir_resolved) {
                return ToolResult::Invalid {
                    field: Some("backup_name".to_string()),
                    message: "backup_name resolves outside the backup directory".to_string(),
                };
            }

            let json_text = match serde_json::to_string_pretty(&backup_data) {
                Ok(s) => s,
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "A database error occurred; it has been logged. Retry, or ask \
                            an operator to check logs."
                            .to_string(),
                    }
                }
            };
            if let Err(_e) = std::fs::write(&backup_path_resolved, json_text) {
                return ToolResult::Failed {
                    message: "A database error occurred; it has been logged. Retry, or ask an \
                        operator to check logs."
                        .to_string(),
                };
            }
            let backup_path = backup_path_resolved.display().to_string();

            let _ = agent_action_repository::log_agent_action(
                &guard,
                requesting_agent_id,
                "backup_project_context",
                Some(&backup_name),
                Some(&serde_json::json!({
                    "total_entries": total_entries,
                    "backup_path": backup_path,
                })),
                now,
            );
            drop(guard);

            let created_at = backup_data["created_at"].as_str().unwrap_or(now);
            let mut response_parts = vec![
                "\u{2705} **Context Backup Created**".to_string(),
                format!("   Name: {backup_name}"),
                format!("   Entries: {total_entries}"),
                format!("   File: {backup_path}"),
                format!("   Created: {created_at}"),
            ];
            if let Some(health) = &health_report {
                let icon = health_status_icon(&health.status);
                let status_title = python_title_case(&health.status);
                response_parts.push(String::new());
                response_parts.push(format!(
                    "\u{1f4ca} **Health Report:** {icon} {status_title} ({}/100)",
                    health.health_score
                ));
                response_parts.push(format!(
                    "   Issues: {} JSON errors, {} stale entries",
                    health.json_errors, health.stale_entries
                ));
                response_parts.push(format!(
                    "   Recommendations: {} items",
                    health.recommendations.len()
                ));
            }
            response_parts.push(String::new());
            response_parts.push("\u{1f4a1} **Backup Usage:**".to_string());
            response_parts.push(
                "\u{2022} Use this backup to restore context in case of corruption".to_string(),
            );
            response_parts.push(
                "\u{2022} Store backup files securely - they contain sensitive project data"
                    .to_string(),
            );
            response_parts.push(
                "\u{2022} Regular backups recommended before major context changes".to_string(),
            );

            ToolResult::Ok {
                data: Some(serde_json::json!({
                    "backup_name": backup_name,
                    "backup_path": backup_path,
                    "total_entries": total_entries,
                    "created_at": created_at,
                    "health_report": health_report,
                })),
                message: Some(response_parts.join("\n")),
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

    /// A genuinely `is_operator_tier` caller (agent_id literally
    /// `"admin"`) -- bypasses `check_write_authorization`'s
    /// per-key creator-ownership matrix entirely, unlike
    /// `write_operator()` above (which only carries the memories.*
    /// capabilities, not `system.config.write`/the `"admin"`
    /// agent_id, so it does NOT trip `is_operator_tier`).
    fn admin_agent() -> Principal {
        Principal {
            agent_id: Some("admin".to_string()),
            ..worker()
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
    fn unsafe_unicode_denylist_catches_the_canonical_spoofing_example() {
        // config<RLO>drowssap renders as "configpassword" in a UI --
        // the exact F005 verify-all-v6 MUTATING #3 exploit string.
        assert!(has_unsafe_unicode_for_identifier("config\u{202e}drowssap"));
        assert!(has_unsafe_unicode_for_identifier("a\u{0000}b"));
        assert!(has_unsafe_unicode_for_identifier("a\u{feff}b"));
        assert!(!has_unsafe_unicode_for_identifier("normal_key"));
        assert!(!has_unsafe_unicode_for_identifier("café.config"));
        assert!(!has_unsafe_unicode_for_identifier("emoji.🚀.key"));
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
        ToolCallContext::off_wire(registry, file_map, std::path::Path::new("/tmp"))
    }

    /// For `BackupProjectContextTool` tests only -- a real isolated
    /// tempdir, since this tool is the first in the crate to actually
    /// write a file to `project_dir`.
    fn ctx_with_project_dir<'a>(
        registry: &'a WaiterRegistry,
        file_map: &'a FileMap,
        project_dir: &'a std::path::Path,
    ) -> ToolCallContext<'a> {
        ToolCallContext::off_wire(registry, file_map, project_dir)
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
    fn python_title_case_matches_pythons_str_title_for_underscored_words() {
        // Python: "needs_attention".title() == "Needs_Attention" --
        // NOT "Needs_attention" (a naive first-char-only port's wrong
        // answer, the actual bug this test pins).
        assert_eq!(python_title_case("needs_attention"), "Needs_Attention");
        assert_eq!(python_title_case("no_data"), "No_Data");
        assert_eq!(python_title_case("excellent"), "Excellent");
        assert_eq!(python_title_case(""), "");
    }

    #[tokio::test]
    async fn view_health_analysis_title_cases_a_multi_word_status_correctly() {
        // Regression for the found-and-fixed bug: a naive first-char-
        // only title-case renders "Needs_attention" for the
        // "needs_attention" status; Python's real `.title()` gives
        // "Needs_Attention". One healthy + one stale-and-invalid-JSON
        // entry (out of 2 total) lands the health score at 55, inside
        // the needs_attention band (50-70).
        let conn = setup().await;
        seed_context(&conn, "healthy", "\"ok\"", "2026-06-01T00:00:00Z").await;
        {
            let guard = conn.lock().await;
            project_context_repository::create_new(
                &guard,
                "bad",
                "not valid json",
                Some("d"),
                "alice",
                "2026-01-01T00:00:00Z",
            )
            .unwrap();
        }
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
        let message = message.unwrap();
        assert!(
            message.contains("Needs_Attention"),
            "message was: {message}"
        );
        assert!(
            !message.contains("Needs_attention"),
            "message was: {message}"
        );
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

    // ── UpdateProjectContextTool ─────────────────────────────────────

    #[tokio::test]
    async fn single_update_creates_a_new_key_when_it_does_not_exist() {
        let conn = setup().await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = UpdateProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"context_key": "fresh.key", "context_value": "v1"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["context_key"], "fresh.key");
    }

    #[tokio::test]
    async fn single_update_without_description_preserves_the_existing_one() {
        let conn = setup().await;
        {
            let guard = conn.lock().await;
            project_context_repository::create_new(
                &guard,
                "k",
                "\"v1\"",
                Some("original description"),
                "alice",
                "2026-06-01T00:00:00Z",
            )
            .unwrap();
        }
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = UpdateProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"context_key": "k", "context_value": "v2"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let row = project_context_repository::get(&guard, "k")
            .unwrap()
            .unwrap();
        assert_eq!(row.value, "\"v2\"");
        assert_eq!(row.description.as_deref(), Some("original description"));
    }

    #[tokio::test]
    async fn single_update_denies_a_foreign_key_for_a_non_admin() {
        let conn = setup().await;
        seed_context(&conn, "k", "\"v1\"", "2026-06-01T00:00:00Z").await;
        let bob = Principal {
            agent_id: Some("bob".to_string()),
            ..worker()
        };
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = UpdateProjectContextTool::call(
            Some(&bob),
            &serde_json::json!({"context_key": "k", "context_value": "v2"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::PermissionDenied { .. }));
    }

    #[tokio::test]
    async fn update_tool_schema_does_not_declare_updates_matching_pythons_own_inconsistency() {
        let parsed: Value = serde_json::from_str(UpdateProjectContextTool::SCHEMA).unwrap();
        assert!(parsed["properties"].get("updates").is_none());
    }

    #[tokio::test]
    async fn update_tools_bulk_arm_dispatches_on_updates_presence_even_though_schema_omits_it() {
        let conn = setup().await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = UpdateProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"updates": [
                {"context_key": "a", "context_value": "1"},
                {"context_key": "b", "context_value": "2"},
            ]}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["updates_attempted"], 2);
    }

    #[tokio::test]
    async fn update_tools_bulk_arm_tolerates_a_malformed_item_matching_pythons_lax_validation() {
        // A genuine, documented Python behavioral asymmetry: this
        // surface's bulk arm does NOT validate item shape up front --
        // a malformed item fails only that item during Phase 2, the
        // batch is not aborted. Contrast with
        // `bulk_tools_own_strict_validation_rejects_the_whole_batch_
        // upfront` below.
        let conn = setup().await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = UpdateProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"updates": [
                {"context_key": "good"},
                {"context_key": "also_good", "context_value": "1"},
            ]}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok (batch not aborted), got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(data["updates_attempted"], 2);
        let lines: Vec<&str> = data["summary_lines"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert!(lines.iter().any(|l| l.contains("1 successful")));
        assert!(lines.iter().any(|l| l.contains("1 failed")));
    }

    #[tokio::test]
    async fn bulk_update_authorizes_every_key_up_front_aborting_the_whole_batch_on_denial() {
        let conn = setup().await;
        {
            let guard = conn.lock().await;
            project_context_repository::create_new(
                &guard,
                "owned-by-bob",
                "\"v\"",
                Some("d"),
                "bob",
                "2026-06-01T00:00:00Z",
            )
            .unwrap();
        }
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = UpdateProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"updates": [
                {"context_key": "alice-owned-new", "context_value": "1"},
                {"context_key": "owned-by-bob", "context_value": "2"},
            ]}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::PermissionDenied { .. }));
        // Zero writes landed -- the whole batch aborted, including the
        // key that would otherwise have been perfectly fine.
        let guard = conn.lock().await;
        assert!(project_context_repository::get(&guard, "alice-owned-new")
            .unwrap()
            .is_none());
    }

    #[tokio::test]
    async fn bulk_update_delivers_wake_all_for_flag_recheck_only_once_per_batch() {
        // config_* keys are always rejected by check_write_authorization
        // (same real finding as CreateProjectContextTool's own test) --
        // this proves deliver_bulk's dedup logic directly via a
        // non-namespaced pseudo-key is not reachable through THIS
        // tool either, matching the sibling test's documented finding.
        let conn = setup().await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let (_sender, mut receiver) = registry.register("alice");
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = UpdateProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"updates": [
                {"context_key": "config_auto_event_loop_global", "context_value": true},
            ]}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        // config_* rejection is Invalid, not PermissionDenied --
        // config_key_error()'s own documented worker-message-clarity
        // choice (see this file's check_write_authorization).
        assert!(matches!(result, ToolResult::Invalid { .. }));
        assert!(receiver.try_recv().is_err());
    }

    // ── BulkUpdateProjectContextTool ─────────────────────────────────

    #[tokio::test]
    async fn bulk_tools_own_strict_validation_rejects_the_whole_batch_upfront() {
        // The genuine, documented Python asymmetry from the other
        // direction: THIS standalone tool validates every item's shape
        // BEFORE any write, so a malformed item hard-fails the call
        // instead of just failing that one item.
        let conn = setup().await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = BulkUpdateProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"updates": [
                {"context_key": "good", "context_value": "1"},
                {"context_key": "missing_value"},
            ]}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
        // Zero writes landed -- the shape check happens before Phase 1
        // authorization even starts.
        let guard = conn.lock().await;
        assert!(project_context_repository::get(&guard, "good")
            .unwrap()
            .is_none());
    }

    #[tokio::test]
    async fn bulk_tool_happy_path_updates_every_item() {
        let conn = setup().await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = BulkUpdateProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"updates": [
                {"context_key": "a", "context_value": "1"},
                {"context_key": "b", "context_value": "2", "description": "desc-b"},
            ]}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["updates_attempted"], 2);
        let guard = conn.lock().await;
        let a = project_context_repository::get(&guard, "a")
            .unwrap()
            .unwrap();
        assert_eq!(a.value, "\"1\"");
        let b = project_context_repository::get(&guard, "b")
            .unwrap()
            .unwrap();
        assert_eq!(b.description.as_deref(), Some("desc-b"));
    }

    #[tokio::test]
    async fn bulk_tool_an_explicit_null_description_clears_an_existing_one() {
        let conn = setup().await;
        {
            let guard = conn.lock().await;
            project_context_repository::create_new(
                &guard,
                "k",
                "\"v1\"",
                Some("original"),
                "alice",
                "2026-06-01T00:00:00Z",
            )
            .unwrap();
        }
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = BulkUpdateProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"updates": [
                {"context_key": "k", "context_value": "v2", "description": null},
            ]}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let row = project_context_repository::get(&guard, "k")
            .unwrap()
            .unwrap();
        assert_eq!(row.description, None);
    }

    #[tokio::test]
    async fn bulk_tool_applies_every_authorized_item_in_the_batch() {
        // Phase 2 applies each authorized item independently -- with
        // both items passing Phase 1 authorization (new, non-config_*
        // keys), both must land, including a non-string JSON value.
        let conn = setup().await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = BulkUpdateProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"updates": [
                {"context_key": "x", "context_value": 1},
                {"context_key": "y", "context_value": {"nested": true}},
            ]}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        assert!(project_context_repository::get(&guard, "x")
            .unwrap()
            .is_some());
        assert!(project_context_repository::get(&guard, "y")
            .unwrap()
            .is_some());
    }

    #[tokio::test]
    async fn bulk_tool_empty_updates_array_is_invalid() {
        let conn = setup().await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = BulkUpdateProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"updates": []}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn bulk_tool_denies_a_viewer_tier_operator() {
        let viewer = viewer_operator();
        let denied = BulkUpdateProjectContextTool::REQUIRED
            .check(Some(&viewer), &conexus_auth::NoPolicyOverrides);
        assert!(denied.is_err());
    }

    // ── DeleteProjectContextTool ─────────────────────────────────────

    #[test]
    fn critical_key_match_covers_prefix_and_exact_forms() {
        assert!(critical_key_match("server_startup", "server_startup"));
        assert!(critical_key_match("server_anything_else", "server_startup"));
        assert!(!critical_key_match("serverish", "server_startup"));
        assert!(critical_key_match("mcp_server_url", "mcp_server_url"));
        assert!(critical_key_match("mcp_whatever", "mcp_server_url"));
    }

    #[tokio::test]
    async fn deletes_a_single_owned_key() {
        let conn = setup().await;
        seed_context(&conn, "k", "\"v\"", "2026-06-01T00:00:00Z").await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = DeleteProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"context_key": "k"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["deleted_count"], 1);
        let guard = conn.lock().await;
        assert!(project_context_repository::get(&guard, "k")
            .unwrap()
            .is_none());
    }

    #[tokio::test]
    async fn deletes_multiple_keys_via_context_keys_array() {
        let conn = setup().await;
        seed_context(&conn, "a", "\"1\"", "2026-06-01T00:00:00Z").await;
        seed_context(&conn, "b", "\"2\"", "2026-06-01T00:00:00Z").await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = DeleteProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"context_keys": ["a", "b"]}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["deleted_count"], 2);
    }

    #[tokio::test]
    async fn no_keys_specified_is_invalid() {
        let conn = setup().await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = DeleteProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn deleting_a_nonexistent_key_is_not_found() {
        let conn = setup().await;
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = DeleteProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"context_key": "ghost"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn a_config_namespaced_delete_is_rejected_before_the_critical_key_guard() {
        let conn = setup().await;
        let op = write_operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = DeleteProjectContextTool::call(
            Some(&op),
            &serde_json::json!({"context_key": "config_server_startup"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        // The config_* category error (Invalid on `context_key`), NOT
        // the critical-key force_delete hint (Invalid on
        // `force_delete`) -- confirmed via the field, since both
        // branches return the same variant.
        let ToolResult::Invalid { field, .. } = result else {
            panic!("expected Invalid, got {result:?}");
        };
        assert_eq!(field.as_deref(), Some("context_key"));
    }

    #[tokio::test]
    async fn a_critical_key_without_force_delete_is_refused() {
        let conn = setup().await;
        seed_context(&conn, "server_startup", "\"v\"", "2026-06-01T00:00:00Z").await;
        let admin = admin_agent();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = DeleteProjectContextTool::call(
            Some(&admin),
            &serde_json::json!({"context_key": "server_startup"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
        let guard = conn.lock().await;
        assert!(project_context_repository::get(&guard, "server_startup")
            .unwrap()
            .is_some());
    }

    #[tokio::test]
    async fn a_critical_key_with_force_delete_succeeds() {
        let conn = setup().await;
        seed_context(&conn, "server_startup", "\"v\"", "2026-06-01T00:00:00Z").await;
        let admin = admin_agent();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = DeleteProjectContextTool::call(
            Some(&admin),
            &serde_json::json!({"context_key": "server_startup", "force_delete": true}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, message } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        assert_eq!(
            data["critical_keys_deleted"],
            serde_json::json!(["server_startup"])
        );
        assert!(message.unwrap().contains("CRITICAL"));
    }

    #[tokio::test]
    async fn a_non_owner_worker_is_denied_before_any_deletion() {
        let conn = setup().await;
        seed_context(&conn, "k", "\"v\"", "2026-06-01T00:00:00Z").await;
        let bob = Principal {
            agent_id: Some("bob".to_string()),
            ..worker()
        };
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = DeleteProjectContextTool::call(
            Some(&bob),
            &serde_json::json!({"context_key": "k"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::PermissionDenied { .. }));
        let guard = conn.lock().await;
        assert!(project_context_repository::get(&guard, "k")
            .unwrap()
            .is_some());
    }

    #[tokio::test]
    async fn a_multi_key_delete_aborts_entirely_if_any_key_fails_authorization() {
        let conn = setup().await;
        seed_context(&conn, "owned-by-alice", "\"v\"", "2026-06-01T00:00:00Z").await;
        {
            let guard = conn.lock().await;
            project_context_repository::create_new(
                &guard,
                "owned-by-bob",
                "\"v\"",
                Some("d"),
                "bob",
                "2026-06-01T00:00:00Z",
            )
            .unwrap();
        }
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = DeleteProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"context_keys": ["owned-by-alice", "owned-by-bob"]}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::PermissionDenied { .. }));
        let guard = conn.lock().await;
        assert!(project_context_repository::get(&guard, "owned-by-alice")
            .unwrap()
            .is_some());
    }

    #[tokio::test]
    async fn delete_purges_the_rag_chunk_for_the_deleted_key() {
        let conn = setup().await;
        seed_context(&conn, "k", "\"v\"", "2026-06-01T00:00:00Z").await;
        {
            let guard = conn.lock().await;
            let chunks = [conexus_db::rag_repository::NewChunk {
                chunk_text: "some indexed text",
                metadata: None,
                embedding: None,
            }];
            conexus_db::rag_repository::bulk_index_chunks(
                &guard,
                "context",
                "k",
                &chunks,
                "2026-06-01T00:00:00Z",
            )
            .unwrap();
        }
        let alice = worker();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = DeleteProjectContextTool::call(
            Some(&alice),
            &serde_json::json!({"context_key": "k"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let remaining: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM rag_chunks WHERE source_type = 'context' AND source_ref = 'k'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(remaining, 0);
    }

    #[tokio::test]
    async fn delete_schema_has_no_key_length_cap_matching_pythons_own_note() {
        let parsed: Value = serde_json::from_str(DeleteProjectContextTool::SCHEMA).unwrap();
        assert!(parsed["properties"]["context_key"]
            .get("maxLength")
            .is_none());
    }

    // ── BackupProjectContextTool ─────────────────────────────────────

    #[tokio::test]
    async fn backup_writes_a_json_file_with_the_auto_generated_name() {
        let conn = setup().await;
        seed_context(&conn, "a", "\"one\"", "2026-06-01T00:00:00Z").await;
        let admin = admin_agent();
        let tmp = tempfile::tempdir().unwrap();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx_with_project_dir(&registry, &file_map, tmp.path());
        let result = BackupProjectContextTool::call(
            Some(&admin),
            &serde_json::json!({}),
            &conn,
            "2026-06-01T12:34:56Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, message } = result else {
            panic!("expected Ok, got {result:?}");
        };
        let data = data.unwrap();
        let backup_name = data["backup_name"].as_str().unwrap();
        assert_eq!(backup_name, "context_backup_20260601_123456");
        assert_eq!(data["total_entries"], 1);

        let backup_path = data["backup_path"].as_str().unwrap();
        let contents = std::fs::read_to_string(backup_path).unwrap();
        let parsed: Value = serde_json::from_str(&contents).unwrap();
        assert_eq!(parsed["backup_name"], backup_name);
        assert_eq!(parsed["entries"][0]["context_key"], "a");
        assert!(message.unwrap().contains("Context Backup Created"));
    }

    #[tokio::test]
    async fn backup_uses_a_caller_supplied_name() {
        let conn = setup().await;
        let admin = admin_agent();
        let tmp = tempfile::tempdir().unwrap();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx_with_project_dir(&registry, &file_map, tmp.path());
        let result = BackupProjectContextTool::call(
            Some(&admin),
            &serde_json::json!({"backup_name": "before-migration.v2"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert_eq!(data.unwrap()["backup_name"], "before-migration.v2");
        assert!(tmp
            .path()
            .join(".agent/backups/context/before-migration.v2.json")
            .is_file());
    }

    #[tokio::test]
    async fn backup_rejects_a_backup_name_outside_the_allowed_charset() {
        // VULN-003 primary gate: this crate's dispatcher never runs
        // JSON-schema validation, so this check in the tool body is
        // the ONLY thing standing between a caller and a path-
        // traversal-shaped backup_name (see BACKUP_NAME_RE's doc).
        let conn = setup().await;
        let admin = admin_agent();
        let tmp = tempfile::tempdir().unwrap();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx_with_project_dir(&registry, &file_map, tmp.path());
        for bad_name in ["../../etc/passwd", "a/b", "with spaces", ""] {
            let result = BackupProjectContextTool::call(
                Some(&admin),
                &serde_json::json!({"backup_name": bad_name}),
                &conn,
                "2026-06-01T00:00:00Z",
                &c,
            )
            .await;
            assert!(
                matches!(result, ToolResult::Invalid { .. }),
                "expected Invalid for {bad_name:?}, got {result:?}"
            );
        }
    }

    #[tokio::test]
    async fn backup_includes_health_report_by_default() {
        let conn = setup().await;
        seed_context(&conn, "a", "\"ok\"", "2026-06-01T00:00:00Z").await;
        let admin = admin_agent();
        let tmp = tempfile::tempdir().unwrap();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx_with_project_dir(&registry, &file_map, tmp.path());
        let result = BackupProjectContextTool::call(
            Some(&admin),
            &serde_json::json!({}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, message } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert!(data.unwrap()["health_report"].is_object());
        assert!(message.unwrap().contains("Health Report"));
    }

    #[tokio::test]
    async fn backup_omits_health_report_when_disabled() {
        let conn = setup().await;
        let admin = admin_agent();
        let tmp = tempfile::tempdir().unwrap();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx_with_project_dir(&registry, &file_map, tmp.path());
        let result = BackupProjectContextTool::call(
            Some(&admin),
            &serde_json::json!({"include_health_report": false}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, message } = result else {
            panic!("expected Ok, got {result:?}");
        };
        assert!(data.unwrap()["health_report"].is_null());
        assert!(!message.unwrap().contains("Health Report"));
    }

    #[tokio::test]
    async fn backup_denies_a_worker_without_system_config_write() {
        let conn = setup().await;
        let alice = worker();
        let denied = BackupProjectContextTool::REQUIRED
            .check(Some(&alice), &conexus_auth::NoPolicyOverrides);
        assert!(denied.is_err());
        let _ = conn;
    }

    #[tokio::test]
    async fn backup_writes_a_durable_audit_row() {
        let conn = setup().await;
        let admin = admin_agent();
        let tmp = tempfile::tempdir().unwrap();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx_with_project_dir(&registry, &file_map, tmp.path());
        let result = BackupProjectContextTool::call(
            Some(&admin),
            &serde_json::json!({"backup_name": "audited"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let count: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM agent_actions WHERE action_type = 'backup_project_context'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }
}
