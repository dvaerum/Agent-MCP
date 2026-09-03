//! Tiny shared helpers for building `IN (...)` clauses over a
//! caller-supplied slice of `rusqlite`-bindable values. Used by any
//! repository that needs a dynamic-width `IN` list (`AgentRepository`,
//! `project_context_repository`, and future repositories that need
//! the same shape) — factored out once two call sites needed it
//! identically, rather than duplicated per module.

use rusqlite::ToSql;

pub(crate) fn in_placeholders(n: usize) -> String {
    std::iter::repeat_n("?", n).collect::<Vec<_>>().join(", ")
}

pub(crate) fn to_sql_refs<S: ToSql>(items: &[S]) -> Vec<&dyn ToSql> {
    items.iter().map(|v| v as &dyn ToSql).collect()
}
