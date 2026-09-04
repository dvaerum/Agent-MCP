//! MCP Prompt Book catalogue (Phase E1 PR B1).
//!
//! Port of `agent_mcp/prompts/*` -- the data + rendering half of the
//! `prompts/list`/`prompts/get` MCP surfaces (`main_app.py`'s
//! `mcp_list_prompts_handler`/`mcp_get_prompt_handler` wire this into
//! `rmcp`; that wiring lives in `conexus-backend::server`, not here,
//! matching this crate's own "tool-catalogue logic, not transport"
//! role).
//!
//! Source of truth stays the real `agent_mcp/prompts/catalog.json`,
//! embedded via `include_str!` at compile time -- a real, deliberate
//! cross-language coupling for the duration of the migration (avoids
//! a second, independently-maintained copy that could drift; this
//! file is retired only in Phase F, when the Python tree it lives in
//! is deleted). Parsed fields: `id`/`title`/`description`/`template`/
//! `variables`/`visibility`. `categories` is dashboard-only (read by
//! a separate `GET /api/prompts/catalog` REST endpoint, out of scope
//! here) and deliberately not parsed.
//!
//! **Deliberately NOT ported**: the generic `core.registry.
//! Registry[T]` callable-visibility branch. Every catalog.json prompt
//! visibility sentinel is a plain `"any"`/`"admin"` string (confirmed
//! by reading the shipped catalog) -- the callable-policy branch
//! exists in Python only for TOOLS' `worker-if-toggled:<key>`
//! predicate, which Rust's tool-authorization already covers via
//! `Requirement::Predicate`, a completely different mechanism.
//! Building the generic `Registry[T]` engine here for a two-variant
//! enum would be over-engineering -- see this migration's own
//! "promote a shared primitive once two call sites need it"
//! precedent (Phase D4 decision 1).
//!
//! The `event-loop` prompt is the one entry whose RENDERED template is
//! NOT the catalog's own serialized copy -- Python swaps in
//! `WAKE_LOOP_INSTRUCTIONS` (leading whitespace stripped) so the
//! prompt-book entry and the `initialize.instructions` injection
//! (Phase E1 PR A) can never drift even if only the Python constant
//! is edited. Ported identically here, sourced from
//! `conexus_core::WAKE_LOOP_INSTRUCTIONS` -- the same reason that
//! constant was promoted to `conexus-core` rather than left in
//! `conexus-backend`.

use crate::python_compat::python_str;
use conexus_core::principal::CatalogRole;
use serde::Deserialize;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::LazyLock;

const CATALOG_JSON: &str = include_str!("../../../agent_mcp/prompts/catalog.json");
const EVENT_LOOP_PROMPT_ID: &str = "event-loop";

#[derive(Deserialize)]
struct RawCatalog {
    #[serde(default)]
    prompts: Vec<RawPrompt>,
}

#[derive(Deserialize)]
struct RawPrompt {
    id: String,
    #[serde(default)]
    title: Option<String>,
    #[serde(default)]
    description: String,
    #[serde(default)]
    template: String,
    #[serde(default)]
    variables: Vec<RawVariable>,
    #[serde(default = "default_visibility_str")]
    visibility: String,
}

fn default_visibility_str() -> String {
    "any".to_string()
}

#[derive(Deserialize)]
struct RawVariable {
    name: String,
    #[serde(default)]
    description: String,
    #[serde(default)]
    required: bool,
}

/// One declared template variable. Port of a catalog entry's
/// `variables[]` array element.
#[derive(Debug, Clone, PartialEq)]
pub struct PromptVariable {
    pub name: String,
    pub description: String,
    pub required: bool,
}

/// A catalog prompt's visibility sentinel. Narrower than Python's
/// `Visibility` union -- see module doc for why the callable-policy
/// branch has no Rust port here.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PromptVisibility {
    Any,
    Admin,
}

/// One rendered-ready Prompt Book entry.
#[derive(Debug, Clone, PartialEq)]
pub struct PromptEntry {
    pub id: String,
    pub title: String,
    pub description: String,
    pub template: String,
    pub variables: Vec<PromptVariable>,
    pub visibility: PromptVisibility,
}

/// Unknown sentinel -> `Admin` (conservative: hide from workers),
/// matching `_build_registry_from_catalog`'s own fallback ("be
/// conservative on an unknown sentinel").
fn parse_visibility(raw: &str) -> PromptVisibility {
    match raw {
        "any" => PromptVisibility::Any,
        _ => PromptVisibility::Admin,
    }
}

/// True iff `role` may see an entry with the given `visibility`. Port
/// of `resolve_visibility`, narrowed to the two sentinels this crate
/// represents (admin always sees everything; `"any"` is visible to
/// every role; anything else is admin-only).
fn is_visible(visibility: PromptVisibility, role: CatalogRole) -> bool {
    role == CatalogRole::Admin || visibility == PromptVisibility::Any
}

static CATALOG: LazyLock<Vec<PromptEntry>> = LazyLock::new(|| {
    let raw: RawCatalog = serde_json::from_str(CATALOG_JSON)
        .expect("embedded agent_mcp/prompts/catalog.json must be valid JSON");
    raw.prompts
        .into_iter()
        .map(|p| {
            let id = p.id;
            let template = if id == EVENT_LOOP_PROMPT_ID {
                conexus_core::WAKE_LOOP_INSTRUCTIONS
                    .trim_start()
                    .to_string()
            } else {
                p.template
            };
            PromptEntry {
                title: p.title.unwrap_or_else(|| id.clone()),
                description: p.description,
                template,
                variables: p
                    .variables
                    .into_iter()
                    .map(|v| PromptVariable {
                        name: v.name,
                        description: v.description,
                        required: v.required,
                    })
                    .collect(),
                visibility: parse_visibility(&p.visibility),
                id,
            }
        })
        .collect()
});

/// Every catalog entry visible to `role`, in registration (catalog
/// file) order. Port of `PromptRegistry.list_visible` via
/// `Registry.list_visible`.
pub fn list_visible(role: CatalogRole) -> Vec<&'static PromptEntry> {
    CATALOG
        .iter()
        .filter(|entry| is_visible(entry.visibility, role))
        .collect()
}

/// The catalog entry for `id`, regardless of visibility -- callers
/// that need the visibility gate enforced call [`render`] afterward.
pub fn get(id: &str) -> Option<&'static PromptEntry> {
    CATALOG.iter().find(|entry| entry.id == id)
}

/// Why [`render`] refused to render `entry`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NotVisible;

/// [`python_str`], widened to never panic on an object/array --
/// `python_str`'s contract assumes its caller already excluded those
/// (true of its other call site, `project_context_tools.rs`'s
/// already-scalar-filtered `value_parsed`), but a `prompts/get`
/// argument comes straight off the wire, unvalidated: a malformed or
/// adversarial client could send an object/array where a scalar is
/// expected, and that must degrade to a plain rendering, never crash
/// the request. Falls back to the value's own JSON text -- a
/// deliberate, documented divergence from Python's `str(list)`/
/// `str(dict)` repr formatting for this one edge case, not a
/// byte-for-byte port (repr-style stringification of an object/array
/// isn't worth hand-replicating for a template-variable substitution
/// path).
fn stringify_argument(value: &Value) -> String {
    match value {
        Value::Object(_) | Value::Array(_) => value.to_string(),
        scalar => python_str(scalar),
    }
}

/// Render `entry`'s template with `arguments` substituted into its
/// `{{VARIABLE}}` placeholders. Port of `PromptRegistry.render`:
///
/// * Re-checks visibility (defense in depth -- `prompts/list`
///   filtering alone is not enough if a worker guesses an admin-only
///   id; the caller is expected to have already resolved `entry` via
///   [`get`], which does NOT gate on visibility).
/// * A variable name absent from `arguments` substitutes as the empty
///   string (never leaves a stray `{{VAR}}` in the rendered text).
/// * `arguments`' values are stringified with Python `str()`
///   semantics (`true` -> `"True"`, `null` -> `"None"`), matching
///   `str(arguments.get(var_name, ""))` -- a JSON-RPC `prompts/get`
///   call is not schema-validated to `Record<string, string>` here
///   any more than Python's dispatcher validates it.
pub fn render(
    entry: &PromptEntry,
    arguments: &HashMap<String, Value>,
    role: CatalogRole,
) -> Result<String, NotVisible> {
    if !is_visible(entry.visibility, role) {
        return Err(NotVisible);
    }
    let mut declared: Vec<&str> = entry.variables.iter().map(|v| v.name.as_str()).collect();
    declared.sort_unstable();
    let mut rendered = entry.template.clone();
    for name in declared {
        let value = arguments
            .get(name)
            .map(stringify_argument)
            .unwrap_or_default();
        rendered = rendered.replace(&format!("{{{{{name}}}}}"), &value);
    }
    Ok(rendered)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn the_shipped_catalog_parses_and_is_non_empty() {
        assert!(!CATALOG.is_empty());
    }

    #[test]
    fn every_shipped_prompt_is_visible_to_admin() {
        // Every entry in the real catalog is visibility="any" today
        // (confirmed by reading catalog.json) -- admin sees literally
        // everything regardless, so this also holds trivially if that
        // ever changes.
        let visible = list_visible(CatalogRole::Admin);
        assert_eq!(visible.len(), CATALOG.len());
    }

    #[test]
    fn every_shipped_prompt_is_visible_to_a_worker_today() {
        // Confirms the real catalog has no admin-only entries right
        // now (a real fact about the shipped data, not the mechanism)
        // -- if this ever fails, a new admin-only prompt was added and
        // this assertion is the signal to update it deliberately.
        let visible = list_visible(CatalogRole::Worker);
        assert_eq!(visible.len(), CATALOG.len());
    }

    #[test]
    fn get_returns_none_for_an_unknown_id() {
        assert!(get("does-not-exist").is_none());
    }

    #[test]
    fn get_returns_the_entry_for_a_known_id() {
        let entry = get("rag-query").expect("rag-query is in the shipped catalog");
        assert_eq!(entry.id, "rag-query");
    }

    #[test]
    fn render_substitutes_a_supplied_variable() {
        let entry = get("rag-query").unwrap();
        let mut args = HashMap::new();
        args.insert("QUESTION".to_string(), json!("what is the deploy story?"));
        let rendered = render(entry, &args, CatalogRole::Worker).unwrap();
        assert!(rendered.contains("what is the deploy story?"));
        assert!(!rendered.contains("{{QUESTION}}"));
    }

    #[test]
    fn render_substitutes_a_missing_variable_as_empty_string_not_a_stray_placeholder() {
        let entry = get("rag-query").unwrap();
        let rendered = render(entry, &HashMap::new(), CatalogRole::Worker).unwrap();
        assert!(!rendered.contains("{{QUESTION}}"));
    }

    #[test]
    fn render_stringifies_a_non_string_argument_with_python_str_semantics() {
        let entry = get("manager-assign-task").expect("manager-assign-task is in the catalog");
        let mut args = HashMap::new();
        args.insert("AGENT_ID".to_string(), json!("worker-1"));
        args.insert("TASK_TITLE".to_string(), json!("t"));
        args.insert("TASK_DESCRIPTION".to_string(), json!("d"));
        // PRIORITY is declared but optional -- pass a JSON number to
        // confirm it renders as "5", not a JSON-encoded "5" with no
        // difference here, but a bool proves the True/False semantics.
        args.insert("ADDITIONAL_CONTEXT".to_string(), json!(true));
        let rendered = render(entry, &args, CatalogRole::Worker).unwrap();
        assert!(rendered.contains("True"), "rendered: {rendered}");
    }

    #[test]
    fn render_never_panics_on_an_object_or_array_shaped_argument() {
        let entry = get("rag-query").unwrap();
        let mut args = HashMap::new();
        args.insert("QUESTION".to_string(), json!({"nested": "value"}));
        let rendered = render(entry, &args, CatalogRole::Worker).unwrap();
        assert!(!rendered.contains("{{QUESTION}}"));

        let mut args = HashMap::new();
        args.insert("QUESTION".to_string(), json!([1, 2, 3]));
        let rendered = render(entry, &args, CatalogRole::Worker).unwrap();
        assert!(!rendered.contains("{{QUESTION}}"));
    }

    #[test]
    fn a_worker_cannot_render_a_hypothetical_admin_only_entry() {
        // The shipped catalog has no admin-only entry to exercise this
        // against directly, so this pins the MECHANISM against a
        // hand-built entry rather than real catalog data.
        let entry = PromptEntry {
            id: "admin-only-test".to_string(),
            title: "Admin Only".to_string(),
            description: String::new(),
            template: "secret".to_string(),
            variables: vec![],
            visibility: PromptVisibility::Admin,
        };
        assert_eq!(
            render(&entry, &HashMap::new(), CatalogRole::Worker),
            Err(NotVisible)
        );
        assert!(render(&entry, &HashMap::new(), CatalogRole::Admin).is_ok());
    }

    #[test]
    fn the_event_loop_prompts_template_is_the_shared_wake_loop_constant() {
        let entry = get(EVENT_LOOP_PROMPT_ID).expect("event-loop is in the shipped catalog");
        assert_eq!(
            entry.template,
            conexus_core::WAKE_LOOP_INSTRUCTIONS.trim_start()
        );
    }

    #[test]
    fn an_unrecognized_visibility_sentinel_is_conservative_admin_only() {
        assert_eq!(parse_visibility("bogus"), PromptVisibility::Admin);
        assert_eq!(parse_visibility("any"), PromptVisibility::Any);
        assert_eq!(parse_visibility("admin"), PromptVisibility::Admin);
    }
}
