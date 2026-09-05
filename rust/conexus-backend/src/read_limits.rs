//! Shared bounded-read clamp for `/api` list endpoints. Port of
//! `agent_mcp/app/routers/_read_limits.py` -- single source of truth
//! for the `?limit` query param so every list-style REST read
//! (`/api/tasks`, `/api/agents`, `/api/context-data`, `/api/all-data`)
//! shares one default + upper bound and can't drift (pentest R3-F3).

/// Default row count when `?limit` is absent or unparseable.
pub const ALL_DATA_DEFAULT_LIMIT: i64 = 500;
/// Hard ceiling `?limit` can never exceed, regardless of what the
/// caller requests.
pub const ALL_DATA_MAX_LIMIT: i64 = 5000;

/// Parse the optional `?limit` query param and clamp it to
/// `[1, ALL_DATA_MAX_LIMIT]`, defaulting to `ALL_DATA_DEFAULT_LIMIT`
/// when absent or unparseable. Port of `_clamp_section_limit`.
pub fn clamp_section_limit(raw: Option<&str>) -> i64 {
    let requested = raw
        .and_then(|s| s.parse::<i64>().ok())
        .unwrap_or(ALL_DATA_DEFAULT_LIMIT);
    requested.clamp(1, ALL_DATA_MAX_LIMIT)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn absent_limit_defaults() {
        assert_eq!(clamp_section_limit(None), ALL_DATA_DEFAULT_LIMIT);
    }

    #[test]
    fn unparseable_limit_defaults() {
        assert_eq!(
            clamp_section_limit(Some("not-a-number")),
            ALL_DATA_DEFAULT_LIMIT
        );
    }

    #[test]
    fn a_limit_within_range_passes_through() {
        assert_eq!(clamp_section_limit(Some("42")), 42);
    }

    #[test]
    fn a_limit_over_the_ceiling_is_clamped() {
        assert_eq!(clamp_section_limit(Some("999999")), ALL_DATA_MAX_LIMIT);
    }

    #[test]
    fn a_zero_or_negative_limit_is_clamped_to_one() {
        assert_eq!(clamp_section_limit(Some("0")), 1);
        assert_eq!(clamp_section_limit(Some("-5")), 1);
    }
}
