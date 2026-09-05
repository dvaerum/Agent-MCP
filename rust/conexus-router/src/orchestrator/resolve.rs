//! Port of `ProjectOrchestrator.resolve` -- confirmed by the Phase E2
//! PR 6 research as the ONE method of that Python class with a real
//! production caller (the MCP/API proxy handlers, PR 9). Everything
//! else `ProjectOrchestrator` offered in Python is superseded here by
//! the free functions in `runtime`/`primitives`/`ensure`/`reaper`.

use chrono::{DateTime, Utc};

use crate::project_registry::{ProjectRegistry, RegistryError};

#[derive(Debug)]
pub enum ResolveError {
    /// Neither a real project nor a live alias -- port of the fixed
    /// `reason="unknown project"` `HTTPNotFound` (never reflects the
    /// caller-supplied name).
    UnknownProject,
    Registry(RegistryError),
}

impl From<RegistryError> for ResolveError {
    fn from(e: RegistryError) -> Self {
        ResolveError::Registry(e)
    }
}

impl std::fmt::Display for ResolveError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ResolveError::UnknownProject => write!(f, "unknown project"),
            ResolveError::Registry(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for ResolveError {}

/// `(real_name, alias)` for the URL segment `name` -- `alias` is
/// `None` when `name` IS the real project, or `Some((alias_name,
/// expires_at))` when `name` is a live grace-period alias of the
/// returned real project (the `alias_name` half is always just `name`
/// itself, echoed back so callers building `AliasInfo` don't need to
/// hang onto the original string separately).
pub fn resolve(
    registry: &ProjectRegistry,
    name: &str,
    now: DateTime<Utc>,
) -> Result<(String, Option<(String, String)>), ResolveError> {
    if registry.get(name)?.is_some() {
        return Ok((name.to_string(), None));
    }
    match registry.resolve_alias_entry(name, now)? {
        Some((real_name, expires_at)) => Ok((real_name, Some((name.to_string(), expires_at)))),
        None => Err(ResolveError::UnknownProject),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn registry_with(dir: &std::path::Path, name: &str) -> ProjectRegistry {
        let registry = ProjectRegistry::new(dir.join("projects.local.json"));
        let now: DateTime<Utc> = "2026-01-01T00:00:00Z".parse().unwrap();
        registry
            .register(name, "/ws/proj-a", "python", now)
            .unwrap();
        registry
    }

    #[test]
    fn resolve_a_real_project_returns_itself_with_no_alias() {
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a");
        let now: DateTime<Utc> = "2026-01-01T00:00:00Z".parse().unwrap();

        let (real_name, alias) = resolve(&registry, "proj-a", now).unwrap();
        assert_eq!(real_name, "proj-a");
        assert!(alias.is_none());
    }

    #[test]
    fn resolve_a_live_alias_returns_the_real_project_and_the_alias_entry() {
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a");
        let now: DateTime<Utc> = "2026-01-01T00:00:00Z".parse().unwrap();
        registry
            .add_alias("proj-a", "old-name", None, Some(30), now)
            .unwrap();

        let (real_name, alias) = resolve(&registry, "old-name", now).unwrap();
        assert_eq!(real_name, "proj-a");
        let (alias_name, expires_at) = alias.unwrap();
        assert_eq!(alias_name, "old-name");
        assert!(!expires_at.is_empty());
    }

    #[test]
    fn resolve_an_unknown_name_is_unknown_project() {
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let now: DateTime<Utc> = "2026-01-01T00:00:00Z".parse().unwrap();
        assert!(matches!(
            resolve(&registry, "nope", now).unwrap_err(),
            ResolveError::UnknownProject
        ));
    }
}
