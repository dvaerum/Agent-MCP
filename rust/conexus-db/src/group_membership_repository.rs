//! Port of `agent_mcp/router/group_resolver.py` (Phase E2 PR 4,
//! `conexus-router-group-graph`) -- the graph layer on top of
//! `group_membership`: transitive resolution (up AND down), insert-time
//! cycle detection, sysadmin inheritance, project-role resolution, and
//! the group-CRUD functions `sso.py`'s reconcile path needs.
//!
//! `resolve_user_groups` was ported earlier (Phase C) as the one read
//! path `conexus-auth::resolve_capabilities` needed; everything else
//! here was deliberately deferred until "whichever phase eventually
//! ports the router's group-management REST surface" -- that's this
//! phase.
//!
//! Connection ownership: unlike Python's `conn: sqlite3.Connection |
//! None = None` self-opening default, every function here takes an
//! explicit `&Connection` -- this crate's own convention (matching
//! every other repository), not a new decision for this module.
//!
//! `ROLE_TIER`/`role_rank` stay `pub` but local to this module for
//! now (Python's own docstring calls them "the ONE home... other
//! modules read", but `admin_users_api.py`/`router_store.py` aren't
//! ported yet) -- promote to `conexus_core` only once a second real
//! call site needs it, matching this migration's own established
//! "promote a shared primitive once two call sites need it" rule.
//!
//! Lives on the ROUTER DB (`router.db`), same as `group_capability_repository`.

use rusqlite::{Connection, OptionalExtension, Result};
use std::collections::{HashMap, HashSet};

/// Project-role ranking -- port of `ROLE_TIER`/`role_rank`. `viewer` <
/// `operator`; an unrecognized role ranks 0 (below every known role)
/// so a malformed stored value can never out-rank a real membership.
pub fn role_rank(role: &str) -> i32 {
    match role {
        "viewer" => 1,
        "operator" => 2,
        _ => 0,
    }
}

/// Errors from [`add_group_member`] -- port of Python's
/// `ValueError`/`CycleDetected` pair, collapsed into one closed enum
/// (this migration's own precedent over Python's exception-subclass
/// ladder; see `IdentityError`).
#[derive(Debug)]
pub enum GroupMembershipError {
    /// Port of the `ValueError` raised when neither or both of
    /// `member_user_id`/`member_group_id` are given.
    InvalidArgs,
    /// Port of `CycleDetected` -- adding `member_group_id` as a member
    /// of `group_id` would close a cycle in the membership DAG.
    CycleDetected {
        group_id: String,
        member_group_id: String,
    },
    Db(rusqlite::Error),
}

impl From<rusqlite::Error> for GroupMembershipError {
    fn from(e: rusqlite::Error) -> Self {
        GroupMembershipError::Db(e)
    }
}

impl std::fmt::Display for GroupMembershipError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            GroupMembershipError::InvalidArgs => write!(
                f,
                "add_group_member requires exactly one of member_user_id or member_group_id"
            ),
            GroupMembershipError::CycleDetected {
                group_id,
                member_group_id,
            } => write!(
                f,
                "adding group {member_group_id:?} as a member of {group_id:?} would close a cycle in the membership DAG"
            ),
            GroupMembershipError::Db(e) => write!(f, "database error: {e}"),
        }
    }
}

impl std::error::Error for GroupMembershipError {}

/// The transitive set of `group_id`s `user_id` belongs to: every group
/// it's a direct member of, plus every ancestor of those groups (a
/// group nested inside another group makes the parent's membership
/// transitive). Empty when the user has no memberships.
///
/// Ported from `group_resolver._resolve_user_groups_on` +
/// `_ancestors_on`: seed with direct `member_user_id` edges, then walk
/// upward via `member_group_id` level-by-level, batching each level in
/// one `IN (...)` query (O(depth) round-trips, not one query per row).
pub fn resolve_user_groups(conn: &Connection, user_id: &str) -> Result<HashSet<String>> {
    let mut stmt =
        conn.prepare("SELECT group_id FROM group_membership WHERE member_user_id = ?1")?;
    let rows = stmt.query_map([user_id], |row| row.get::<_, String>(0))?;
    let direct: HashSet<String> = rows.collect::<Result<_>>()?;
    drop(stmt);

    if direct.is_empty() {
        return Ok(HashSet::new());
    }
    ancestors(conn, direct)
}

/// Upward closure over `group_membership.member_group_id`: `seed` plus
/// every group reachable by walking upward from it. Shared kernel
/// between [`resolve_user_groups`] and [`resolve_group_ancestors`].
fn ancestors(conn: &Connection, seed: HashSet<String>) -> Result<HashSet<String>> {
    let mut result = seed.clone();
    let mut frontier: Vec<String> = seed.into_iter().collect();

    while !frontier.is_empty() {
        let placeholders = crate::sql_util::in_placeholders(frontier.len());
        let sql = format!(
            "SELECT DISTINCT group_id FROM group_membership WHERE member_group_id IN ({placeholders})"
        );
        let mut stmt = conn.prepare(&sql)?;
        let params = crate::sql_util::to_sql_refs(&frontier);
        let rows = stmt.query_map(params.as_slice(), |row| row.get::<_, String>(0))?;
        let level: Vec<String> = rows.collect::<Result<_>>()?;
        drop(stmt);

        let mut next_frontier = Vec::new();
        for gid in level {
            if result.insert(gid.clone()) {
                next_frontier.push(gid);
            }
        }
        frontier = next_frontier;
    }

    Ok(result)
}

/// `group_id` plus every ancestor group (upward closure) -- the
/// group-rooted mirror of [`resolve_user_groups`]: what a FRESH member
/// of `group_id` would resolve into.
pub fn resolve_group_ancestors(conn: &Connection, group_id: &str) -> Result<HashSet<String>> {
    ancestors(conn, HashSet::from([group_id.to_string()]))
}

/// `(member_user_id, member_group_id)` edges directly out of
/// `group_id` -- the downward-walk primitive shared by
/// [`group_has_transitive_user_member`] and [`would_create_cycle`].
fn children_of_group(
    conn: &Connection,
    group_id: &str,
) -> Result<Vec<(Option<String>, Option<String>)>> {
    let mut stmt = conn.prepare(
        "SELECT member_user_id, member_group_id FROM group_membership WHERE group_id = ?1",
    )?;
    let rows = stmt.query_map([group_id], |row| Ok((row.get(0)?, row.get(1)?)))?;
    rows.collect()
}

/// `true` iff any of `groups` is itself flagged `is_sysadmin = 1`.
pub fn any_group_is_sysadmin(conn: &Connection, groups: &HashSet<String>) -> Result<bool> {
    if groups.is_empty() {
        return Ok(false);
    }
    let ids: Vec<String> = groups.iter().cloned().collect();
    let placeholders = crate::sql_util::in_placeholders(ids.len());
    let sql =
        format!("SELECT 1 FROM groups WHERE is_sysadmin = 1 AND group_id IN ({placeholders})");
    let mut stmt = conn.prepare(&sql)?;
    stmt.exists(crate::sql_util::to_sql_refs(&ids).as_slice())
}

/// `true` iff `user_id` is a sysadmin directly (`users.is_sysadmin`) OR
/// via any group in their transitive membership.
///
/// `groups`: pass an already-resolved transitive group set (e.g. from
/// [`resolve_user_groups`]) to skip the internal walk -- the same
/// request-scoped reuse hook Python's own docstring documents
/// (`router/auth_middleware.py` resolves the graph once per request
/// and threads it through every consumer). `None` self-resolves.
pub fn resolve_user_is_sysadmin(
    conn: &Connection,
    user_id: &str,
    groups: Option<&HashSet<String>>,
) -> Result<bool> {
    let direct: Option<bool> = conn
        .query_row(
            "SELECT is_sysadmin FROM users WHERE user_id = ?1",
            [user_id],
            |r| r.get(0),
        )
        .optional()?;
    if direct == Some(true) {
        return Ok(true);
    }
    match groups {
        Some(g) => any_group_is_sysadmin(conn, g),
        None => {
            let resolved = resolve_user_groups(conn, user_id)?;
            any_group_is_sysadmin(conn, &resolved)
        }
    }
}

/// `true` iff a FRESH member of `group_id` would inherit sysadmin --
/// i.e. `group_id` itself OR any ancestor group is sysadmin-flagged.
pub fn group_is_transitively_sysadmin(conn: &Connection, group_id: &str) -> Result<bool> {
    let ancestors = resolve_group_ancestors(conn, group_id)?;
    any_group_is_sysadmin(conn, &ancestors)
}

/// `true` iff at least one user is reachable under `group_id` --
/// directly, or via nested subgroups, transitively (the downward
/// mirror of [`resolve_user_groups`]'s upward walk). Iterative DFS +
/// visited-set, same shape as [`would_create_cycle`], so a
/// (theoretically impossible post cycle-detection, but defensive)
/// cycle in the membership DAG can't loop forever.
pub fn group_has_transitive_user_member(conn: &Connection, group_id: &str) -> Result<bool> {
    let mut visited: HashSet<String> = HashSet::new();
    let mut stack: Vec<String> = vec![group_id.to_string()];
    while let Some(current) = stack.pop() {
        if !visited.insert(current.clone()) {
            continue;
        }
        for (member_user_id, child_group) in children_of_group(conn, &current)? {
            if member_user_id.is_some() {
                return Ok(true);
            }
            if let Some(child) = child_group {
                if !visited.contains(&child) {
                    stack.push(child);
                }
            }
        }
    }
    Ok(false)
}

/// Highest role per project across a set of groups (group rows only).
pub fn project_roles_for_groups(
    conn: &Connection,
    groups: &HashSet<String>,
) -> Result<HashMap<String, String>> {
    if groups.is_empty() {
        return Ok(HashMap::new());
    }
    let ids: Vec<String> = groups.iter().cloned().collect();
    let placeholders = crate::sql_util::in_placeholders(ids.len());
    let sql = format!(
        "SELECT project_name, role FROM project_membership WHERE group_id IN ({placeholders})"
    );
    let mut stmt = conn.prepare(&sql)?;
    let params = crate::sql_util::to_sql_refs(&ids);
    let rows = stmt.query_map(params.as_slice(), |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
    })?;
    let mut best: HashMap<String, String> = HashMap::new();
    for row in rows {
        let (project, role) = row?;
        let better = best
            .get(&project)
            .is_none_or(|existing| role_rank(&role) > role_rank(existing));
        if better {
            best.insert(project, role);
        }
    }
    Ok(best)
}

/// Every project role a FRESH member of `group_id` would inherit --
/// the highest tier per project across `group_id` and its ancestors.
pub fn group_resolved_project_roles(
    conn: &Connection,
    group_id: &str,
) -> Result<HashMap<String, String>> {
    let ancestors = resolve_group_ancestors(conn, group_id)?;
    project_roles_for_groups(conn, &ancestors)
}

/// The user's effective role for `project_name` -- the highest tier
/// (`operator` > `viewer`) across the user's direct rows and any of
/// their groups' rows, or `None` when no row covers them.
///
/// `groups`: same reuse hook as [`resolve_user_is_sysadmin`] -- pass an
/// already-resolved transitive group set to skip the internal walk.
pub fn resolve_user_project_role(
    conn: &Connection,
    user_id: &str,
    project_name: &str,
    groups: Option<&HashSet<String>>,
) -> Result<Option<String>> {
    let mut candidates: Vec<String> = Vec::new();
    {
        let mut stmt = conn.prepare(
            "SELECT role FROM project_membership WHERE project_name = ?1 AND user_id = ?2",
        )?;
        let rows = stmt.query_map([project_name, user_id], |r| r.get::<_, String>(0))?;
        for row in rows {
            candidates.push(row?);
        }
    }

    let resolved_groups;
    let groups_ref: &HashSet<String> = match groups {
        Some(g) => g,
        None => {
            resolved_groups = resolve_user_groups(conn, user_id)?;
            &resolved_groups
        }
    };
    if !groups_ref.is_empty() {
        let ids: Vec<String> = groups_ref.iter().cloned().collect();
        let placeholders = crate::sql_util::in_placeholders(ids.len());
        let sql = format!(
            "SELECT role FROM project_membership WHERE project_name = ? AND group_id IN ({placeholders})"
        );
        let mut stmt = conn.prepare(&sql)?;
        let mut params: Vec<&dyn rusqlite::ToSql> = vec![&project_name];
        params.extend(crate::sql_util::to_sql_refs(&ids));
        let rows = stmt.query_map(params.as_slice(), |r| r.get::<_, String>(0))?;
        for row in rows {
            candidates.push(row?);
        }
    }

    Ok(candidates.into_iter().max_by_key(|r| role_rank(r)))
}

/// `true` iff adding `new_child_group_id` as a member of
/// `parent_group_id` would close a cycle in the membership DAG. A
/// cycle exists iff `parent_group_id` is reachable from
/// `new_child_group_id` via the existing membership edges (self-loop
/// is the trivial 1-cycle). Iterative DFS + visited-set.
pub fn would_create_cycle(
    conn: &Connection,
    parent_group_id: &str,
    new_child_group_id: &str,
) -> Result<bool> {
    if parent_group_id == new_child_group_id {
        return Ok(true);
    }
    let mut visited: HashSet<String> = HashSet::new();
    let mut stack: Vec<String> = vec![new_child_group_id.to_string()];
    while let Some(current) = stack.pop() {
        if !visited.insert(current.clone()) {
            continue;
        }
        for (_user, child_group) in children_of_group(conn, &current)? {
            let Some(child) = child_group else { continue };
            if child == parent_group_id {
                return Ok(true);
            }
            if !visited.contains(&child) {
                stack.push(child);
            }
        }
    }
    Ok(false)
}

/// Insert a `group_membership` row after validation -- the canonical
/// writer. Exactly one of `member_user_id`/`member_group_id` must be
/// `Some` (the table's own CHECK constraint enforces this at the
/// storage layer too; this surfaces a clean, typed error instead of a
/// caller having to translate a raw constraint-violation). For
/// group-into-group edges, runs cycle detection first -- on
/// [`GroupMembershipError::CycleDetected`] the table is left untouched.
pub fn add_group_member(
    conn: &Connection,
    group_id: &str,
    member_user_id: Option<&str>,
    member_group_id: Option<&str>,
    added_at: &str,
) -> std::result::Result<(), GroupMembershipError> {
    if member_user_id.is_none() == member_group_id.is_none() {
        return Err(GroupMembershipError::InvalidArgs);
    }
    if let Some(child) = member_group_id {
        if would_create_cycle(conn, group_id, child)? {
            return Err(GroupMembershipError::CycleDetected {
                group_id: group_id.to_string(),
                member_group_id: child.to_string(),
            });
        }
    }
    conn.execute(
        "INSERT INTO group_membership (group_id, member_user_id, member_group_id, added_at) VALUES (?1, ?2, ?3, ?4)",
        (group_id, member_user_id, member_group_id, added_at),
    )?;
    Ok(())
}

/// Promote the earliest-by-`created_at` user to sysadmin. No-op when
/// the `users` table is empty, or when any user already has
/// `is_sysadmin = 1` (idempotent -- never demotes an existing
/// sysadmin, never crowns a second one). Tiebreak `user_id ASC` so two
/// users created in the same millisecond resolve deterministically.
///
/// A repair-workflow helper, distinct from [`crate`]-adjacent
/// `conexus-router::identity`'s at-creation-time
/// `bootstrap_first_operator` (which runs inside `create_user`'s own
/// `BEGIN IMMEDIATE` transaction) -- this one is for an operator who
/// somehow ended up sysadmin-less on an existing deployment.
pub fn bootstrap_first_operator_as_sysadmin(conn: &Connection) -> Result<()> {
    let existing: Option<i64> = conn
        .query_row(
            "SELECT 1 FROM users WHERE is_sysadmin = 1 LIMIT 1",
            [],
            |r| r.get(0),
        )
        .optional()?;
    if existing.is_some() {
        return Ok(());
    }
    let earliest: Option<String> = conn
        .query_row(
            "SELECT user_id FROM users ORDER BY created_at ASC, user_id ASC LIMIT 1",
            [],
            |r| r.get(0),
        )
        .optional()?;
    let Some(user_id) = earliest else {
        return Ok(());
    };
    conn.execute(
        "UPDATE users SET is_sysadmin = 1 WHERE user_id = ?1",
        [&user_id],
    )?;
    Ok(())
}

/// Return the `group_id` for `name`, JIT-creating it if missing.
/// `created_at` uses SQLite's own `datetime('now')` -- a real,
/// preserved Python inconsistency (every OTHER `created_at` column in
/// this schema is stamped with the ISO-8601-with-milliseconds format
/// `identity::_now_iso`/this crate's own callers use; this one alone
/// uses SQLite's coarser `'YYYY-MM-DD HH:MM:SS'`), ported as-is per
/// this migration's "re-derive documented behavior, don't smuggle in
/// a fix" discipline -- not reconciled here.
pub fn ensure_group(conn: &Connection, name: &str) -> Result<String> {
    if let Some(group_id) = conn
        .query_row("SELECT group_id FROM groups WHERE name = ?1", [name], |r| {
            r.get(0)
        })
        .optional()?
    {
        return Ok(group_id);
    }
    let mut buf = [0u8; 8];
    getrandom::fill(&mut buf).expect("OS RNG must be available to mint a group_id");
    let group_id: String = buf.iter().map(|b| format!("{b:02x}")).collect();
    conn.execute(
        "INSERT INTO groups (group_id, name, is_sysadmin, created_at) VALUES (?1, ?2, 0, datetime('now'))",
        (&group_id, name),
    )?;
    Ok(group_id)
}

/// Delete a user->group edge; `true` iff a row was removed.
pub fn remove_group_member(conn: &Connection, group_id: &str, user_id: &str) -> Result<bool> {
    let n = conn.execute(
        "DELETE FROM group_membership WHERE group_id = ?1 AND member_user_id = ?2",
        [group_id, user_id],
    )?;
    Ok(n > 0)
}

/// Port of `remove_group_member_handler`'s own `DELETE` -- `member_id`
/// is an opaque surrogate matched against BOTH `member_user_id` AND
/// `member_group_id` (the caller doesn't need to know which kind it
/// is). Deliberately a SEPARATE function from [`remove_group_member`]
/// above (that one is user-only, used by the SSO reconcile path,
/// which always knows it's removing a user edge).
pub fn remove_group_member_by_id(
    conn: &Connection,
    group_id: &str,
    member_id: &str,
) -> Result<bool> {
    let n = conn.execute(
        "DELETE FROM group_membership WHERE group_id = ?1 AND (member_user_id = ?2 OR member_group_id = ?2)",
        [group_id, member_id],
    )?;
    Ok(n > 0)
}

/// One row of [`list_group_members`] -- either a user member or a
/// nested group member, matching `list_group_members_handler`'s own
/// "either shape, never the union" JSON projection (Python strips the
/// None-valued half of the LEFT JOIN rather than emitting nulls).
#[derive(Debug, Clone, PartialEq)]
pub enum GroupMemberRow {
    User {
        user_id: String,
        username: String,
        added_at: String,
    },
    Group {
        group_id: String,
        name: String,
        is_sysadmin: bool,
        added_at: String,
    },
}

/// Port of `list_group_members_handler`'s own query: every direct
/// member of `group_id` (user or nested group), each carrying a
/// renderable label via a `LEFT JOIN` against `users`/`groups` so the
/// dashboard needs no follow-up fetch. Ordered by the member's own
/// display label (`COALESCE(username, group_name)`).
pub fn list_group_members(conn: &Connection, group_id: &str) -> Result<Vec<GroupMemberRow>> {
    let mut stmt = conn.prepare(
        "SELECT gm.member_user_id, gm.member_group_id, gm.added_at, \
                u.username, g.name, g.is_sysadmin \
         FROM group_membership gm \
         LEFT JOIN users u ON gm.member_user_id = u.user_id \
         LEFT JOIN groups g ON gm.member_group_id = g.group_id \
         WHERE gm.group_id = ?1 \
         ORDER BY COALESCE(u.username, g.name)",
    )?;
    let rows = stmt.query_map([group_id], |row| {
        let member_user_id: Option<String> = row.get(0)?;
        let member_group_id: Option<String> = row.get(1)?;
        let added_at: String = row.get(2)?;
        let username: Option<String> = row.get(3)?;
        let name: Option<String> = row.get(4)?;
        let is_sysadmin: Option<bool> = row.get(5)?;
        Ok(if let Some(user_id) = member_user_id {
            GroupMemberRow::User {
                user_id,
                username: username.unwrap_or_default(),
                added_at,
            }
        } else {
            GroupMemberRow::Group {
                group_id: member_group_id.unwrap_or_default(),
                name: name.unwrap_or_default(),
                is_sysadmin: is_sysadmin.unwrap_or(false),
                added_at,
            }
        })
    })?;
    rows.collect()
}

/// `{group_name: group_id}` for `user_id`'s DIRECT memberships in
/// groups whose `name` starts with `name_prefix` -- the SSO OIDC
/// `oidc:`-namespaced group reconcile scope. `name_prefix` is treated
/// literally (the `LIKE` pattern is `name_prefix + '%'` with backslash
/// as the escape char), matching Python's inline query exactly.
pub fn user_group_memberships_by_name_prefix(
    conn: &Connection,
    user_id: &str,
    name_prefix: &str,
) -> Result<HashMap<String, String>> {
    let pattern = format!("{name_prefix}%");
    let mut stmt = conn.prepare(
        "SELECT g.group_id, g.name FROM group_membership gm \
         JOIN groups g ON g.group_id = gm.group_id \
         WHERE gm.member_user_id = ?1 AND g.name LIKE ?2 ESCAPE '\\'",
    )?;
    let rows = stmt.query_map([user_id, pattern.as_str()], |r| {
        Ok((r.get::<_, String>(1)?, r.get::<_, String>(0)?))
    })?;
    let mut out = HashMap::new();
    for row in rows {
        let (name, group_id) = row?;
        out.insert(name, group_id);
    }
    Ok(out)
}

/// Public projection of a `groups` row (Phase E2, `admin_users_api.py`
/// groups-CRUD PR). Every field is safe to return to a caller --
/// `groups` carries no secret column, unlike `users`.
#[derive(Debug, Clone, PartialEq)]
pub struct GroupRow {
    pub group_id: String,
    pub name: String,
    pub is_sysadmin: bool,
    pub created_at: String,
}

const GROUP_COLUMNS: &str = "group_id, name, is_sysadmin, created_at";

fn row_to_group(row: &rusqlite::Row) -> rusqlite::Result<GroupRow> {
    Ok(GroupRow {
        group_id: row.get(0)?,
        name: row.get(1)?,
        is_sysadmin: row.get(2)?,
        created_at: row.get(3)?,
    })
}

/// Port of `_group_member_count`.
pub fn group_member_count(conn: &Connection, group_id: &str) -> Result<i64> {
    conn.query_row(
        "SELECT COUNT(*) FROM group_membership WHERE group_id = ?1",
        [group_id],
        |r| r.get(0),
    )
}

/// Port of `list_groups_handler`: every group with its denormalised
/// member count, ordered by name.
pub fn list_groups_with_member_counts(conn: &Connection) -> Result<Vec<(GroupRow, i64)>> {
    let mut stmt = conn.prepare(&format!("SELECT {GROUP_COLUMNS} FROM groups ORDER BY name"))?;
    let rows = stmt.query_map([], row_to_group)?;
    let mut out = Vec::new();
    for row in rows {
        let group = row?;
        let count = group_member_count(conn, &group.group_id)?;
        out.push((group, count));
    }
    Ok(out)
}

pub fn get_group(conn: &Connection, group_id: &str) -> Result<Option<GroupRow>> {
    conn.query_row(
        &format!("SELECT {GROUP_COLUMNS} FROM groups WHERE group_id = ?1"),
        [group_id],
        row_to_group,
    )
    .optional()
}

/// Errors from [`create_group`]/[`update_group_fields`] -- a distinct
/// enum from [`GroupMembershipError`] since these are plain-CRUD
/// failures (a UNIQUE(name) collision), not membership-graph
/// failures.
#[derive(Debug)]
pub enum GroupCrudError {
    NameConflict,
    Db(rusqlite::Error),
}

impl From<rusqlite::Error> for GroupCrudError {
    fn from(e: rusqlite::Error) -> Self {
        GroupCrudError::Db(e)
    }
}

/// Port of `create_group_handler`'s own raw INSERT -- deliberately
/// NOT [`ensure_group`] above: that function is an idempotent
/// get-or-create with a fixed `is_sysadmin=0` and no explicit `now`,
/// used only by the SSO-group-reconcile path; this one always
/// INSERTs fresh, accepts an explicit `is_sysadmin`, takes an
/// explicit `now`, and surfaces a UNIQUE(name) collision as a real
/// error rather than silently returning the existing row.
pub fn create_group(
    conn: &Connection,
    name: &str,
    is_sysadmin: bool,
    now: &str,
) -> std::result::Result<GroupRow, GroupCrudError> {
    let mut buf = [0u8; 8];
    getrandom::fill(&mut buf).expect("OS RNG must be available to mint a group_id");
    let group_id: String = buf.iter().map(|b| format!("{b:02x}")).collect();
    let insert_result = conn.execute(
        "INSERT INTO groups (group_id, name, is_sysadmin, created_at) VALUES (?1, ?2, ?3, ?4)",
        (&group_id, name, is_sysadmin, now),
    );
    if let Err(e) = insert_result {
        if matches!(&e, rusqlite::Error::SqliteFailure(err, _) if err.code == rusqlite::ErrorCode::ConstraintViolation)
        {
            return Err(GroupCrudError::NameConflict);
        }
        return Err(GroupCrudError::Db(e));
    }
    get_group(conn, &group_id)?
        .ok_or_else(|| GroupCrudError::Db(rusqlite::Error::QueryReturnedNoRows))
}

/// Partial update for [`update_group_fields`] -- `None` means "leave
/// this field untouched", mirroring `edit_group_handler`'s
/// presence-in-body check (never conflated with "explicitly clear",
/// since neither field is nullable).
#[derive(Debug, Clone, Default)]
pub struct GroupFieldUpdate<'a> {
    pub name: Option<&'a str>,
    pub is_sysadmin: Option<bool>,
}

/// Port of `edit_group_handler`'s own `UPDATE` -- applied as one
/// single-column `UPDATE` per supplied field (rather than one
/// dynamically-built multi-column statement) inside the CALLER's own
/// transaction, matching this migration's `decide_edit_user`
/// precedent (`admin_users_users.rs`) for the identical reason.
/// Returns `Ok(false)` if `group_id` doesn't exist (caller decides
/// the 404); a UNIQUE(name) collision surfaces as
/// `GroupCrudError::NameConflict`.
pub fn update_group_fields(
    conn: &Connection,
    group_id: &str,
    update: &GroupFieldUpdate,
) -> std::result::Result<bool, GroupCrudError> {
    if get_group(conn, group_id)?.is_none() {
        return Ok(false);
    }
    if let Some(name) = update.name {
        let result = conn.execute(
            "UPDATE groups SET name = ?1 WHERE group_id = ?2",
            (name, group_id),
        );
        if let Err(e) = result {
            if matches!(&e, rusqlite::Error::SqliteFailure(err, _) if err.code == rusqlite::ErrorCode::ConstraintViolation)
            {
                return Err(GroupCrudError::NameConflict);
            }
            return Err(GroupCrudError::Db(e));
        }
    }
    if let Some(is_sysadmin) = update.is_sysadmin {
        conn.execute(
            "UPDATE groups SET is_sysadmin = ?1 WHERE group_id = ?2",
            (is_sysadmin, group_id),
        )?;
    }
    Ok(true)
}

/// Port of `delete_group_handler`'s own `DELETE` -- `group_membership`
/// rows where this group is either the parent OR a member cascade via
/// `ON DELETE CASCADE`, matching Python's own cascade. Returns
/// `false` if `group_id` doesn't exist (caller decides the 404).
pub fn delete_group(conn: &Connection, group_id: &str) -> Result<bool> {
    let n = conn.execute("DELETE FROM groups WHERE group_id = ?1", [group_id])?;
    Ok(n > 0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_router_schema;

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&conn).unwrap();
        conn
    }

    fn seed_group(conn: &Connection, group_id: &str) {
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) VALUES (?1, ?1, 0, '2026-01-01T00:00:00Z')",
            [group_id],
        )
        .unwrap();
    }

    fn seed_sysadmin_group(conn: &Connection, group_id: &str) {
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) VALUES (?1, ?1, 1, '2026-01-01T00:00:00Z')",
            [group_id],
        )
        .unwrap();
    }

    fn seed_user(conn: &Connection, user_id: &str) {
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?1, ?1, '2026-01-01T00:00:00Z')",
            [user_id],
        )
        .unwrap();
    }

    fn add_user_member(conn: &Connection, group_id: &str, user_id: &str) {
        // `member_user_id` carries a real FK to `users(user_id)` since
        // Phase E2 PR 3 backfilled it -- seed a placeholder row (test
        // callers reuse ids like "alice" across several tests, so
        // `OR IGNORE` keeps this idempotent per test function).
        seed_user(conn, user_id);
        conn.execute(
            "INSERT INTO group_membership (group_id, member_user_id, added_at) VALUES (?1, ?2, '2026-01-01T00:00:00Z')",
            [group_id, user_id],
        )
        .unwrap();
    }

    /// Nests `child_group_id` as a member of `parent_group_id` --
    /// direct table insert, bypassing [`add_group_member`]'s own cycle
    /// check, for tests that want to set up a graph shape without
    /// exercising that validation path.
    fn nest_group_under(conn: &Connection, parent_group_id: &str, child_group_id: &str) {
        conn.execute(
            "INSERT INTO group_membership (group_id, member_group_id, added_at) VALUES (?1, ?2, '2026-01-01T00:00:00Z')",
            [parent_group_id, child_group_id],
        )
        .unwrap();
    }

    #[test]
    fn user_with_no_memberships_resolves_to_empty_set() {
        let conn = test_conn();
        assert_eq!(
            resolve_user_groups(&conn, "nobody").unwrap(),
            HashSet::new()
        );
    }

    #[test]
    fn direct_membership_resolves_to_that_one_group() {
        let conn = test_conn();
        seed_group(&conn, "engineers");
        add_user_member(&conn, "engineers", "alice");

        assert_eq!(
            resolve_user_groups(&conn, "alice").unwrap(),
            HashSet::from(["engineers".to_string()])
        );
    }

    #[test]
    fn nested_group_membership_resolves_transitively() {
        // alice is directly in "backend", which is nested inside
        // "engineers", which is nested inside "all-staff" -- resolving
        // alice's groups must walk the whole chain upward.
        let conn = test_conn();
        seed_group(&conn, "backend");
        seed_group(&conn, "engineers");
        seed_group(&conn, "all-staff");
        add_user_member(&conn, "backend", "alice");
        nest_group_under(&conn, "engineers", "backend");
        nest_group_under(&conn, "all-staff", "engineers");

        assert_eq!(
            resolve_user_groups(&conn, "alice").unwrap(),
            HashSet::from([
                "backend".to_string(),
                "engineers".to_string(),
                "all-staff".to_string(),
            ])
        );
    }

    #[test]
    fn sibling_groups_dont_leak_into_each_others_resolution() {
        let conn = test_conn();
        seed_group(&conn, "engineers");
        seed_group(&conn, "sales");
        add_user_member(&conn, "engineers", "alice");
        add_user_member(&conn, "sales", "bob");

        assert_eq!(
            resolve_user_groups(&conn, "alice").unwrap(),
            HashSet::from(["engineers".to_string()])
        );
    }

    #[test]
    fn diamond_shaped_membership_graph_resolves_without_duplication_or_hang() {
        // alice is in both "backend" and "frontend", which both nest
        // inside "engineers" -- a naive walk that doesn't dedup the
        // frontier could loop or double-count.
        let conn = test_conn();
        seed_group(&conn, "backend");
        seed_group(&conn, "frontend");
        seed_group(&conn, "engineers");
        add_user_member(&conn, "backend", "alice");
        add_user_member(&conn, "frontend", "alice");
        nest_group_under(&conn, "engineers", "backend");
        nest_group_under(&conn, "engineers", "frontend");

        assert_eq!(
            resolve_user_groups(&conn, "alice").unwrap(),
            HashSet::from([
                "backend".to_string(),
                "frontend".to_string(),
                "engineers".to_string(),
            ])
        );
    }

    #[test]
    fn membership_in_an_unrelated_group_does_not_expand_to_the_whole_graph() {
        let conn = test_conn();
        seed_group(&conn, "engineers");
        seed_group(&conn, "island");
        add_user_member(&conn, "island", "alice");

        assert_eq!(
            resolve_user_groups(&conn, "alice").unwrap(),
            HashSet::from(["island".to_string()])
        );
    }

    #[test]
    fn resolve_group_ancestors_includes_the_group_itself_and_its_parents() {
        let conn = test_conn();
        seed_group(&conn, "backend");
        seed_group(&conn, "engineers");
        seed_group(&conn, "all-staff");
        nest_group_under(&conn, "engineers", "backend");
        nest_group_under(&conn, "all-staff", "engineers");

        assert_eq!(
            resolve_group_ancestors(&conn, "backend").unwrap(),
            HashSet::from([
                "backend".to_string(),
                "engineers".to_string(),
                "all-staff".to_string(),
            ])
        );
    }

    #[test]
    fn resolve_group_ancestors_of_a_root_group_is_just_itself() {
        let conn = test_conn();
        seed_group(&conn, "engineers");
        assert_eq!(
            resolve_group_ancestors(&conn, "engineers").unwrap(),
            HashSet::from(["engineers".to_string()])
        );
    }

    #[test]
    fn resolve_user_is_sysadmin_true_from_a_direct_flag() {
        let conn = test_conn();
        seed_user(&conn, "alice");
        conn.execute(
            "UPDATE users SET is_sysadmin = 1 WHERE user_id = 'alice'",
            [],
        )
        .unwrap();
        assert!(resolve_user_is_sysadmin(&conn, "alice", None).unwrap());
    }

    #[test]
    fn resolve_user_is_sysadmin_true_via_a_transitively_sysadmin_group() {
        let conn = test_conn();
        seed_group(&conn, "backend");
        seed_sysadmin_group(&conn, "root-admins");
        nest_group_under(&conn, "root-admins", "backend");
        add_user_member(&conn, "backend", "alice");

        assert!(resolve_user_is_sysadmin(&conn, "alice", None).unwrap());
    }

    #[test]
    fn resolve_user_is_sysadmin_false_for_an_ordinary_user_and_group() {
        let conn = test_conn();
        seed_group(&conn, "engineers");
        add_user_member(&conn, "engineers", "alice");
        assert!(!resolve_user_is_sysadmin(&conn, "alice", None).unwrap());
    }

    #[test]
    fn resolve_user_is_sysadmin_honours_a_precomputed_group_set() {
        let conn = test_conn();
        seed_sysadmin_group(&conn, "root-admins");
        seed_user(&conn, "alice");
        // alice has NO real membership row -- only the precomputed set
        // says she's in root-admins, proving the passed-in set is used
        // instead of a fresh internal walk.
        let precomputed = HashSet::from(["root-admins".to_string()]);
        assert!(resolve_user_is_sysadmin(&conn, "alice", Some(&precomputed)).unwrap());
    }

    #[test]
    fn group_is_transitively_sysadmin_true_via_an_ancestor() {
        let conn = test_conn();
        seed_group(&conn, "backend");
        seed_sysadmin_group(&conn, "root-admins");
        nest_group_under(&conn, "root-admins", "backend");
        assert!(group_is_transitively_sysadmin(&conn, "backend").unwrap());
    }

    #[test]
    fn group_is_transitively_sysadmin_false_for_an_unrelated_group() {
        let conn = test_conn();
        seed_group(&conn, "engineers");
        assert!(!group_is_transitively_sysadmin(&conn, "engineers").unwrap());
    }

    #[test]
    fn group_has_transitive_user_member_true_for_a_direct_member() {
        let conn = test_conn();
        seed_group(&conn, "engineers");
        add_user_member(&conn, "engineers", "alice");
        assert!(group_has_transitive_user_member(&conn, "engineers").unwrap());
    }

    #[test]
    fn group_has_transitive_user_member_true_via_a_nested_subgroup() {
        let conn = test_conn();
        seed_group(&conn, "backend");
        seed_group(&conn, "engineers");
        add_user_member(&conn, "backend", "alice");
        nest_group_under(&conn, "engineers", "backend");
        assert!(group_has_transitive_user_member(&conn, "engineers").unwrap());
    }

    #[test]
    fn group_has_transitive_user_member_false_for_an_empty_group() {
        let conn = test_conn();
        seed_group(&conn, "empty-group");
        assert!(!group_has_transitive_user_member(&conn, "empty-group").unwrap());
    }

    #[test]
    fn group_resolved_project_roles_takes_the_highest_tier_across_ancestors() {
        let conn = test_conn();
        seed_group(&conn, "backend");
        seed_group(&conn, "engineers");
        nest_group_under(&conn, "engineers", "backend");
        conn.execute(
            "INSERT INTO project_membership (project_name, group_id, role) VALUES ('proj-a', 'backend', 'viewer')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO project_membership (project_name, group_id, role) VALUES ('proj-a', 'engineers', 'operator')",
            [],
        )
        .unwrap();

        let roles = group_resolved_project_roles(&conn, "backend").unwrap();
        assert_eq!(roles.get("proj-a"), Some(&"operator".to_string()));
    }

    #[test]
    fn resolve_user_project_role_unions_direct_and_group_rows() {
        let conn = test_conn();
        seed_user(&conn, "alice");
        seed_group(&conn, "engineers");
        add_user_member(&conn, "engineers", "alice");
        conn.execute(
            "INSERT INTO project_membership (project_name, group_id, role) VALUES ('proj-a', 'engineers', 'viewer')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO project_membership (project_name, user_id, role) VALUES ('proj-a', 'alice', 'operator')",
            [],
        )
        .unwrap();

        assert_eq!(
            resolve_user_project_role(&conn, "alice", "proj-a", None).unwrap(),
            Some("operator".to_string())
        );
    }

    #[test]
    fn resolve_user_project_role_is_none_when_no_row_covers_the_user() {
        let conn = test_conn();
        seed_user(&conn, "alice");
        assert_eq!(
            resolve_user_project_role(&conn, "alice", "proj-a", None).unwrap(),
            None
        );
    }

    #[test]
    fn would_create_cycle_detects_a_self_loop() {
        let conn = test_conn();
        seed_group(&conn, "engineers");
        assert!(would_create_cycle(&conn, "engineers", "engineers").unwrap());
    }

    #[test]
    fn would_create_cycle_detects_a_transitive_cycle() {
        // engineers -> backend already exists; adding backend as a
        // member of... wait, adding engineers as a member of backend
        // would close backend -> engineers -> backend.
        let conn = test_conn();
        seed_group(&conn, "backend");
        seed_group(&conn, "engineers");
        nest_group_under(&conn, "engineers", "backend");
        assert!(would_create_cycle(&conn, "backend", "engineers").unwrap());
    }

    #[test]
    fn would_create_cycle_is_false_for_an_unrelated_pair() {
        let conn = test_conn();
        seed_group(&conn, "backend");
        seed_group(&conn, "sales");
        assert!(!would_create_cycle(&conn, "backend", "sales").unwrap());
    }

    #[test]
    fn add_group_member_rejects_neither_field_set() {
        let conn = test_conn();
        seed_group(&conn, "engineers");
        let err =
            add_group_member(&conn, "engineers", None, None, "2026-01-01T00:00:00Z").unwrap_err();
        assert!(matches!(err, GroupMembershipError::InvalidArgs));
    }

    #[test]
    fn add_group_member_rejects_both_fields_set() {
        let conn = test_conn();
        seed_group(&conn, "engineers");
        seed_user(&conn, "alice");
        let err = add_group_member(
            &conn,
            "engineers",
            Some("alice"),
            Some("engineers"),
            "2026-01-01T00:00:00Z",
        )
        .unwrap_err();
        assert!(matches!(err, GroupMembershipError::InvalidArgs));
    }

    #[test]
    fn add_group_member_inserts_a_user_edge() {
        let conn = test_conn();
        seed_group(&conn, "engineers");
        seed_user(&conn, "alice");
        add_group_member(
            &conn,
            "engineers",
            Some("alice"),
            None,
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        assert_eq!(
            resolve_user_groups(&conn, "alice").unwrap(),
            HashSet::from(["engineers".to_string()])
        );
    }

    #[test]
    fn add_group_member_refuses_a_cycle_and_leaves_the_table_untouched() {
        let conn = test_conn();
        seed_group(&conn, "backend");
        seed_group(&conn, "engineers");
        nest_group_under(&conn, "engineers", "backend");

        let err = add_group_member(
            &conn,
            "backend",
            None,
            Some("engineers"),
            "2026-01-01T00:00:00Z",
        )
        .unwrap_err();
        assert!(matches!(err, GroupMembershipError::CycleDetected { .. }));

        // the table must be exactly as before -- one row (engineers -> backend).
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM group_membership", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn add_group_member_allows_a_non_cyclic_group_edge() {
        let conn = test_conn();
        seed_group(&conn, "backend");
        seed_group(&conn, "engineers");
        add_group_member(
            &conn,
            "engineers",
            None,
            Some("backend"),
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        assert_eq!(
            resolve_group_ancestors(&conn, "backend").unwrap(),
            HashSet::from(["backend".to_string(), "engineers".to_string()])
        );
    }

    #[test]
    fn bootstrap_first_operator_as_sysadmin_promotes_the_earliest_user() {
        let conn = test_conn();
        conn.execute(
            "INSERT INTO users (user_id, username, created_at) VALUES ('bob', 'bob', '2026-01-02T00:00:00Z')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO users (user_id, username, created_at) VALUES ('alice', 'alice', '2026-01-01T00:00:00Z')",
            [],
        )
        .unwrap();

        bootstrap_first_operator_as_sysadmin(&conn).unwrap();

        let alice_admin: bool = conn
            .query_row(
                "SELECT is_sysadmin FROM users WHERE user_id = 'alice'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let bob_admin: bool = conn
            .query_row(
                "SELECT is_sysadmin FROM users WHERE user_id = 'bob'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(alice_admin, "the earliest-created user must be promoted");
        assert!(!bob_admin);
    }

    #[test]
    fn bootstrap_first_operator_as_sysadmin_is_a_noop_when_a_sysadmin_already_exists() {
        let conn = test_conn();
        conn.execute(
            "INSERT INTO users (user_id, username, created_at, is_sysadmin) VALUES ('bob', 'bob', '2026-01-02T00:00:00Z', 1)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO users (user_id, username, created_at) VALUES ('alice', 'alice', '2026-01-01T00:00:00Z')",
            [],
        )
        .unwrap();

        bootstrap_first_operator_as_sysadmin(&conn).unwrap();

        let alice_admin: bool = conn
            .query_row(
                "SELECT is_sysadmin FROM users WHERE user_id = 'alice'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(
            !alice_admin,
            "an existing sysadmin must never be joined by a second one"
        );
    }

    #[test]
    fn bootstrap_first_operator_as_sysadmin_is_a_noop_on_an_empty_table() {
        let conn = test_conn();
        // Must not panic/error when there is nobody to promote.
        bootstrap_first_operator_as_sysadmin(&conn).unwrap();
    }

    #[test]
    fn ensure_group_creates_on_first_call_and_is_idempotent() {
        let conn = test_conn();
        let id1 = ensure_group(&conn, "engineers").unwrap();
        let id2 = ensure_group(&conn, "engineers").unwrap();
        assert_eq!(id1, id2);
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM groups", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn remove_group_member_deletes_and_reports_whether_a_row_existed() {
        let conn = test_conn();
        seed_group(&conn, "engineers");
        add_user_member(&conn, "engineers", "alice");

        assert!(remove_group_member(&conn, "engineers", "alice").unwrap());
        assert!(!remove_group_member(&conn, "engineers", "alice").unwrap());
        assert_eq!(resolve_user_groups(&conn, "alice").unwrap(), HashSet::new());
    }

    #[test]
    fn user_group_memberships_by_name_prefix_matches_only_the_prefix() {
        let conn = test_conn();
        seed_group(&conn, "oidc:engineers");
        seed_group(&conn, "oidc:sales");
        seed_group(&conn, "manual:engineers");
        add_user_member(&conn, "oidc:engineers", "alice");
        add_user_member(&conn, "manual:engineers", "alice");

        let matches = user_group_memberships_by_name_prefix(&conn, "alice", "oidc:").unwrap();
        assert_eq!(
            matches,
            HashMap::from([("oidc:engineers".to_string(), "oidc:engineers".to_string())])
        );
    }

    const NOW: &str = "2026-01-01T00:00:00.000+00:00";

    #[test]
    fn create_group_inserts_a_fresh_row() {
        let conn = test_conn();
        let group = create_group(&conn, "engineers", false, NOW).unwrap();
        assert_eq!(group.name, "engineers");
        assert!(!group.is_sysadmin);
        assert_eq!(group.created_at, NOW);
    }

    #[test]
    fn create_group_rejects_a_duplicate_name() {
        let conn = test_conn();
        create_group(&conn, "engineers", false, NOW).unwrap();
        let err = create_group(&conn, "engineers", false, NOW).unwrap_err();
        assert!(matches!(err, GroupCrudError::NameConflict));
    }

    #[test]
    fn create_group_is_a_separate_contract_from_ensure_group() {
        // ensure_group is idempotent get-or-create; create_group always
        // inserts fresh and errors on a collision -- confirm they don't
        // silently converge.
        let conn = test_conn();
        let via_ensure = ensure_group(&conn, "shared-name").unwrap();
        let err = create_group(&conn, "shared-name", false, NOW).unwrap_err();
        assert!(matches!(err, GroupCrudError::NameConflict));
        assert!(get_group(&conn, &via_ensure).unwrap().is_some());
    }

    #[test]
    fn get_group_returns_none_for_an_unknown_id() {
        let conn = test_conn();
        assert!(get_group(&conn, "nope").unwrap().is_none());
    }

    #[test]
    fn group_member_count_reflects_real_membership() {
        let conn = test_conn();
        let group = create_group(&conn, "engineers", false, NOW).unwrap();
        assert_eq!(group_member_count(&conn, &group.group_id).unwrap(), 0);
        add_user_member(&conn, &group.group_id, "alice");
        assert_eq!(group_member_count(&conn, &group.group_id).unwrap(), 1);
    }

    #[test]
    fn list_groups_with_member_counts_is_ordered_by_name() {
        let conn = test_conn();
        create_group(&conn, "zebra", false, NOW).unwrap();
        let engineers = create_group(&conn, "engineers", false, NOW).unwrap();
        add_user_member(&conn, &engineers.group_id, "alice");
        let groups = list_groups_with_member_counts(&conn).unwrap();
        let names: Vec<&str> = groups.iter().map(|(g, _)| g.name.as_str()).collect();
        assert_eq!(names, vec!["engineers", "zebra"]);
        assert_eq!(groups[0].1, 1);
    }

    #[test]
    fn update_group_fields_renames_a_group() {
        let conn = test_conn();
        let group = create_group(&conn, "old-name", false, NOW).unwrap();
        let ok = update_group_fields(
            &conn,
            &group.group_id,
            &GroupFieldUpdate {
                name: Some("new-name"),
                is_sysadmin: None,
            },
        )
        .unwrap();
        assert!(ok);
        assert_eq!(
            get_group(&conn, &group.group_id).unwrap().unwrap().name,
            "new-name"
        );
    }

    #[test]
    fn update_group_fields_flips_is_sysadmin() {
        let conn = test_conn();
        let group = create_group(&conn, "engineers", false, NOW).unwrap();
        update_group_fields(
            &conn,
            &group.group_id,
            &GroupFieldUpdate {
                name: None,
                is_sysadmin: Some(true),
            },
        )
        .unwrap();
        assert!(
            get_group(&conn, &group.group_id)
                .unwrap()
                .unwrap()
                .is_sysadmin
        );
    }

    #[test]
    fn update_group_fields_rejects_a_name_collision() {
        let conn = test_conn();
        create_group(&conn, "taken", false, NOW).unwrap();
        let group = create_group(&conn, "other", false, NOW).unwrap();
        let err = update_group_fields(
            &conn,
            &group.group_id,
            &GroupFieldUpdate {
                name: Some("taken"),
                is_sysadmin: None,
            },
        )
        .unwrap_err();
        assert!(matches!(err, GroupCrudError::NameConflict));
    }

    #[test]
    fn update_group_fields_returns_false_for_an_unknown_group() {
        let conn = test_conn();
        let ok = update_group_fields(
            &conn,
            "nope",
            &GroupFieldUpdate {
                name: Some("x"),
                is_sysadmin: None,
            },
        )
        .unwrap();
        assert!(!ok);
    }

    #[test]
    fn delete_group_removes_the_row_and_reports_whether_one_existed() {
        let conn = test_conn();
        let group = create_group(&conn, "engineers", false, NOW).unwrap();
        assert!(delete_group(&conn, &group.group_id).unwrap());
        assert!(get_group(&conn, &group.group_id).unwrap().is_none());
        assert!(!delete_group(&conn, &group.group_id).unwrap());
    }

    #[test]
    fn delete_group_cascades_to_group_membership_rows() {
        let conn = test_conn();
        let group = create_group(&conn, "engineers", false, NOW).unwrap();
        add_user_member(&conn, &group.group_id, "alice");
        assert!(delete_group(&conn, &group.group_id).unwrap());
        assert_eq!(group_member_count(&conn, &group.group_id).unwrap(), 0);
    }

    #[test]
    fn remove_group_member_by_id_matches_a_user_member() {
        let conn = test_conn();
        let group = create_group(&conn, "engineers", false, NOW).unwrap();
        add_user_member(&conn, &group.group_id, "alice");
        assert!(remove_group_member_by_id(&conn, &group.group_id, "alice").unwrap());
        assert_eq!(group_member_count(&conn, &group.group_id).unwrap(), 0);
    }

    #[test]
    fn remove_group_member_by_id_matches_a_nested_group_member() {
        let conn = test_conn();
        let parent = create_group(&conn, "parent", false, NOW).unwrap();
        let child = create_group(&conn, "child", false, NOW).unwrap();
        add_group_member(&conn, &parent.group_id, None, Some(&child.group_id), NOW).unwrap();
        assert!(remove_group_member_by_id(&conn, &parent.group_id, &child.group_id).unwrap());
        assert_eq!(group_member_count(&conn, &parent.group_id).unwrap(), 0);
    }

    #[test]
    fn remove_group_member_by_id_returns_false_when_no_row_matches() {
        let conn = test_conn();
        let group = create_group(&conn, "engineers", false, NOW).unwrap();
        assert!(!remove_group_member_by_id(&conn, &group.group_id, "nobody").unwrap());
    }

    #[test]
    fn list_group_members_projects_a_user_member() {
        let conn = test_conn();
        let group = create_group(&conn, "engineers", false, NOW).unwrap();
        add_user_member(&conn, &group.group_id, "alice");
        let members = list_group_members(&conn, &group.group_id).unwrap();
        assert_eq!(members.len(), 1);
        assert_eq!(
            members[0],
            GroupMemberRow::User {
                user_id: "alice".to_string(),
                username: "alice".to_string(),
                added_at: "2026-01-01T00:00:00Z".to_string(),
            }
        );
    }

    #[test]
    fn list_group_members_projects_a_nested_group_member() {
        let conn = test_conn();
        let parent = create_group(&conn, "parent", false, NOW).unwrap();
        let child = create_group(&conn, "child", true, NOW).unwrap();
        add_group_member(&conn, &parent.group_id, None, Some(&child.group_id), NOW).unwrap();
        let members = list_group_members(&conn, &parent.group_id).unwrap();
        assert_eq!(members.len(), 1);
        assert_eq!(
            members[0],
            GroupMemberRow::Group {
                group_id: child.group_id,
                name: "child".to_string(),
                is_sysadmin: true,
                added_at: NOW.to_string(),
            }
        );
    }

    #[test]
    fn list_group_members_orders_by_display_label() {
        let conn = test_conn();
        let group = create_group(&conn, "engineers", false, NOW).unwrap();
        add_user_member(&conn, &group.group_id, "zeb");
        add_user_member(&conn, &group.group_id, "alice");
        let members = list_group_members(&conn, &group.group_id).unwrap();
        let usernames: Vec<&str> = members
            .iter()
            .map(|m| match m {
                GroupMemberRow::User { username, .. } => username.as_str(),
                GroupMemberRow::Group { .. } => "",
            })
            .collect();
        assert_eq!(usernames, vec!["alice", "zeb"]);
    }
}
