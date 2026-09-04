//! Small, shared Python-semantics helpers reused across tool modules.
//! Extracted from `project_context_tools.rs` once a second call site
//! ([`crate::prompts`]) needed the identical function -- this
//! migration's own "promote a shared primitive once two call sites
//! need it" precedent (Phase D4 decision 1).

use serde_json::Value;

/// Python's `str()` on a JSON-decoded scalar -- NOT the same as
/// re-serializing to JSON text (`str(True)` is `"True"`, `str(None)`
/// is `"None"`, `str("hello")` is `hello` with no quotes). Only
/// called on a non-object/non-array `Value` (the caller branches on
/// that first).
pub(crate) fn python_str(value: &Value) -> String {
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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn python_str_matches_python_semantics_for_scalars() {
        assert_eq!(python_str(&Value::Null), "None");
        assert_eq!(python_str(&json!(true)), "True");
        assert_eq!(python_str(&json!(false)), "False");
        assert_eq!(python_str(&json!(42)), "42");
        assert_eq!(python_str(&json!("hello")), "hello");
    }
}
