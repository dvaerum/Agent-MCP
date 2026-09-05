//! Router-level identity store -- users, sessions, project memberships.
//! Port of `agent_mcp/router/identity.py` (976 LOC), grown across
//! several PRs (same discipline as splitting `task_tools.py`/
//! `admin_tools.py` across several PRs): PR 3 shipped schema (see
//! `conexus_db::schema::init_router_schema`), password hashing, the
//! security-critical `create_user` bootstrap cluster, and session
//! lifecycle; PR 16 added the `project_membership` writer slice
//! `create_project_handler`/`rename_project_handler`/
//! `delete_project_handler` actually call
//! (`add_project_membership`/`rename_project_membership_project`/
//! `remove_project_membership_by_project` -- a deliberately tight
//! subset of Python's fuller `insert_project_membership`/
//! `remove_project_membership`/`is_project_member`/`list_user_projects`
//! surface, scoped to real call sites rather than the whole API).
//! Still deferred: SSO-subject reconciliation
//! (`find_user_by_sso_subject`/`find_linkable_user_by_email`/
//! `stamp_sso_subject_if_absent`/`upgrade_sso_subject` -- only needed
//! once the SSO PRs land).
//!
//! **Threading, not a live connection pool**: every function here
//! takes an explicit `&Connection` (this crate's own convention,
//! matching every repository in `conexus-db`) rather than Python's
//! "every public function opens and closes its own connection" shape
//! -- the router's own connection-lifecycle story (pooled? one
//! long-lived connection behind a mutex, like `conexus-backend`?) is
//! an app-wiring decision (PR 23), not this module's to make.
//!
//! **`_list_registered_projects()` threaded explicitly**: Python's
//! `bootstrap_first_operator` reads the live project registry
//! directly; that reader doesn't exist in this crate yet (PR 5,
//! `conexus-router-project-registry`). Matching `mount.rs`'s own
//! "explicit input over hidden dependency" precedent,
//! `bootstrap_first_operator`/`create_user` take the registered-project
//! list as an explicit `&[String]` parameter instead of blocking this
//! PR on PR 5.

// PR3 ships a deliberately tight slice (see module doc); several
// functions here (get_user_by_id, delete_session, ...) have no caller
// yet since main.rs wires nothing REST/session-facing until later PRs
// -- matching mount.rs/path_policy.rs's own precedent for a
// helpers-ahead-of-their-first-consumer module.
#![allow(dead_code)]

use std::sync::LazyLock;

use argon2::password_hash::phc::PasswordHash;
use argon2::password_hash::{PasswordHasher, PasswordVerifier};
use argon2::{Algorithm, Argon2, Params, Version};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior};

/// Base class for router identity errors -- port of Python's
/// `IdentityError` hierarchy, collapsed into one enum (matching this
/// migration's own `SendMessageError`/`CreateAgentError` precedent of
/// a closed Rust enum over Python's exception-subclass ladder).
#[derive(Debug)]
pub enum IdentityError {
    /// Port of `UsernameAlreadyExistsError` -- the `UNIQUE(username)`
    /// constraint fired.
    UsernameAlreadyExists(String),
    /// Port of `WeakPasswordError`. The message is operator-facing
    /// (rendered into the setup form) -- it must never echo the
    /// rejected value, and it never does (see [`validate_password_strength`]).
    WeakPassword(String),
    Db(rusqlite::Error),
}

impl From<rusqlite::Error> for IdentityError {
    fn from(e: rusqlite::Error) -> Self {
        IdentityError::Db(e)
    }
}

impl std::fmt::Display for IdentityError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            IdentityError::UsernameAlreadyExists(u) => {
                write!(f, "username {u:?} already exists")
            }
            IdentityError::WeakPassword(msg) => write!(f, "{msg}"),
            IdentityError::Db(e) => write!(f, "database error: {e}"),
        }
    }
}

impl std::error::Error for IdentityError {}

/// Minimum password length for any NEW operator password -- port of
/// `PASSWORD_MIN_LENGTH`. Gates NEW password-setting only; never
/// re-validates existing stored hashes.
pub const PASSWORD_MIN_LENGTH: usize = 12;

/// Enforce the password-strength policy; `Err` on violation. Call
/// BEFORE [`create_user`] at every path that sets a NEW operator
/// password.
pub fn validate_password_strength(password: &str) -> Result<(), IdentityError> {
    if password.chars().count() < PASSWORD_MIN_LENGTH {
        return Err(IdentityError::WeakPassword(format!(
            "Password must be at least {PASSWORD_MIN_LENGTH} characters."
        )));
    }
    Ok(())
}

/// Argon2id with Python's argon2-cffi library defaults EXPLICITLY
/// pinned (`time_cost=2, memory_cost=65536 KiB, parallelism=4`) --
/// deliberately NOT this crate's own `Argon2::default()` preset (RFC
/// 9106's `m=19456, t=2, p=1`, a genuinely different, lighter profile).
/// This match matters only for NEWLY MINTED hashes: verifying an
/// EXISTING hash (either implementation's) reads its own embedded
/// PHC-string parameters regardless of the verifier's configured
/// defaults, so [`verify_password`] is cross-compatible either way --
/// pinning this is about keeping a Rust-minted hash's cost profile
/// identical to a Python-minted one, not a correctness requirement.
static HASHER: LazyLock<Argon2<'static>> = LazyLock::new(|| {
    let params =
        Params::new(65536, 2, 4, None).expect("argon2 params matching argon2-cffi's defaults");
    Argon2::new(Algorithm::Argon2id, Version::V0x13, params)
});

/// Hash `password` via argon2id with library-default-matching
/// parameters. Returns the full PHC-encoded string (parameters, salt,
/// and hash together) -- callers store one TEXT column and never
/// reason about salt management. `PasswordHasher::hash_password`
/// (password-hash 0.6's own auto-salting entry point, backed by the
/// `getrandom` feature) generates a fresh random salt per call -- no
/// manual `SaltString` plumbing needed, unlike the 0.5-series API
/// this module was first drafted against.
pub fn hash_password(password: &str) -> String {
    HASHER
        .hash_password(password.as_bytes())
        .expect("hashing a well-formed password cannot fail")
        .to_string()
}

/// `true` iff `password` matches `hashed`. Wraps
/// `password_hash::PasswordVerifier` (which returns a typed error on
/// mismatch/malformed hash) to a simple boolean -- callers
/// consistently want "did this pair match?", not the error ladder.
pub fn verify_password(hashed: &str, password: &str) -> bool {
    let Ok(parsed) = PasswordHash::new(hashed) else {
        return false;
    };
    HASHER.verify_password(password.as_bytes(), &parsed).is_ok()
}

fn random_id(byte_len: usize) -> String {
    let mut bytes = vec![0u8; byte_len];
    getrandom::fill(&mut bytes).expect("OS CSPRNG must be available to mint an identity id");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// `true` iff the `users` table has zero rows. Port of
/// `users_table_is_empty`, taking the transaction/connection the
/// caller is already inside (Python's `conn: sqlite3.Connection |
/// None = None` self-opening default has no analogue here -- every
/// function in this crate takes an explicit `&Connection`).
pub fn users_table_is_empty(conn: &Connection) -> Result<bool, IdentityError> {
    Ok(conn
        .query_row("SELECT 1 FROM users LIMIT 1", [], |_| Ok(()))
        .optional()?
        .is_none())
}

/// One `users` row, as read back by [`get_user_by_username`]/
/// [`get_user_by_id`].
#[derive(Debug, Clone, PartialEq)]
pub struct UserRow {
    pub user_id: String,
    pub username: String,
    pub email: Option<String>,
    pub password_hash: Option<String>,
    pub created_at: String,
    pub last_login_at: Option<String>,
    pub is_sysadmin: bool,
    pub sso_subject: Option<String>,
}

const USER_COLUMNS: &str =
    "user_id, username, email, password_hash, created_at, last_login_at, is_sysadmin, sso_subject";

fn row_to_user(row: &rusqlite::Row) -> rusqlite::Result<UserRow> {
    Ok(UserRow {
        user_id: row.get(0)?,
        username: row.get(1)?,
        email: row.get(2)?,
        password_hash: row.get(3)?,
        created_at: row.get(4)?,
        last_login_at: row.get(5)?,
        is_sysadmin: row.get(6)?,
        sso_subject: row.get(7)?,
    })
}

pub fn get_user_by_username(
    conn: &Connection,
    username: &str,
) -> Result<Option<UserRow>, IdentityError> {
    Ok(conn
        .query_row(
            &format!("SELECT {USER_COLUMNS} FROM users WHERE username = ?1"),
            [username],
            row_to_user,
        )
        .optional()?)
}

pub fn get_user_by_id(conn: &Connection, user_id: &str) -> Result<Option<UserRow>, IdentityError> {
    Ok(conn
        .query_row(
            &format!("SELECT {USER_COLUMNS} FROM users WHERE user_id = ?1"),
            [user_id],
            row_to_user,
        )
        .optional()?)
}

/// A `users` row with every SENSITIVE column excluded (`password_hash`,
/// `sso_subject`) -- port of the exact column list
/// `admin_users_api.py`'s `list_users_handler`/`create_user_handler`/
/// `edit_user_handler` SELECT. Deliberately a SEPARATE struct/query
/// from [`UserRow`], never fetching the hash at all, rather than
/// fetching the full row and dropping the field at the JSON-response
/// layer -- the same "don't even pull the secret across the boundary"
/// posture `admin_tools.rs`'s `redact_agent_row` established for
/// agent rows in the tool-catalogue layer.
#[derive(Debug, Clone, PartialEq)]
pub struct UserPublicRow {
    pub user_id: String,
    pub username: String,
    pub email: Option<String>,
    pub is_sysadmin: bool,
    pub created_at: String,
    pub last_login_at: Option<String>,
}

const USER_PUBLIC_COLUMNS: &str =
    "user_id, username, email, is_sysadmin, created_at, last_login_at";

fn row_to_user_public(row: &rusqlite::Row) -> rusqlite::Result<UserPublicRow> {
    Ok(UserPublicRow {
        user_id: row.get(0)?,
        username: row.get(1)?,
        email: row.get(2)?,
        is_sysadmin: row.get(3)?,
        created_at: row.get(4)?,
        last_login_at: row.get(5)?,
    })
}

/// Port of `list_users_handler`: every user, public projection,
/// ordered by username.
pub fn list_users(conn: &Connection) -> Result<Vec<UserPublicRow>, IdentityError> {
    let mut stmt = conn.prepare(&format!(
        "SELECT {USER_PUBLIC_COLUMNS} FROM users ORDER BY username"
    ))?;
    let rows = stmt.query_map([], row_to_user_public)?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

pub fn get_user_public_by_id(
    conn: &Connection,
    user_id: &str,
) -> Result<Option<UserPublicRow>, IdentityError> {
    Ok(conn
        .query_row(
            &format!("SELECT {USER_PUBLIC_COLUMNS} FROM users WHERE user_id = ?1"),
            [user_id],
            row_to_user_public,
        )
        .optional()?)
}

/// Port of `create_user_handler`'s own raw INSERT -- deliberately
/// NOT [`create_user`] below: this is the ADMIN-facing create path
/// (an already-authenticated operator minting another user), which
/// must apply EXACTLY the `is_sysadmin` value the caller requested,
/// with NO first-user-bootstrap side effect. Reaching this endpoint
/// at all requires an existing operator session, so `users` can never
/// be empty here in practice -- but the two INSERTs stay genuinely
/// separate functions (matching Python's own two separate code
/// paths) rather than reusing [`create_user`] and relying on that
/// invariant to keep its bootstrap branch dead. No explicit
/// username/email sanitization here, matching the real Python
/// handler exactly -- it relies solely on the shared JSON-body-decode
/// chokepoint (PR 23's job), not a second local pass.
pub fn admin_create_user(
    conn: &Connection,
    username: &str,
    password: &str,
    email: Option<&str>,
    is_sysadmin: bool,
    now: &str,
) -> Result<UserPublicRow, IdentityError> {
    let user_id = random_id(8);
    let password_hash = hash_password(password);
    let insert_result = conn.execute(
        "INSERT INTO users (user_id, username, email, password_hash, created_at, last_login_at, is_sysadmin) \
         VALUES (?1, ?2, ?3, ?4, ?5, NULL, ?6)",
        (&user_id, username, email, &password_hash, now, is_sysadmin),
    );
    if let Err(e) = insert_result {
        if matches!(
            &e,
            rusqlite::Error::SqliteFailure(err, _)
                if err.code == rusqlite::ErrorCode::ConstraintViolation
        ) {
            return Err(IdentityError::UsernameAlreadyExists(username.to_string()));
        }
        return Err(IdentityError::Db(e));
    }
    get_user_public_by_id(conn, &user_id)?
        .ok_or_else(|| IdentityError::Db(rusqlite::Error::QueryReturnedNoRows))
}

/// Apply the first-operator bootstrap invariant to `user_id` -- port
/// of `bootstrap_first_operator`. THE single routine for the
/// security-critical rule "the first user on an otherwise-empty users
/// table becomes sysadmin and gets membership in every registered
/// project." Runs on the CALLER's transaction (`tx`) so it's atomic
/// with the INSERT that created `user_id` -- see [`create_user`]'s own
/// doc for why that atomicity matters (a real historical dual-
/// sysadmin race).
///
/// `registered_projects` is the pre-Phase-1-deployment migration story
/// (existing single-tenant deploys upgrade smoothly because the first
/// operator inherits access to every project they already had) --
/// threaded explicitly since the project-registry reader doesn't exist
/// in this crate yet (PR 5). An empty slice is the correct, safe
/// input until then, not a stub -- a fresh deployment has no
/// pre-existing projects to inherit either.
pub fn bootstrap_first_operator(
    tx: &rusqlite::Transaction,
    user_id: &str,
    grant_sysadmin: bool,
    registered_projects: &[String],
    now: &str,
) -> Result<(), IdentityError> {
    if grant_sysadmin {
        tx.execute(
            "UPDATE users SET is_sysadmin = 1 WHERE user_id = ?1",
            [user_id],
        )?;
    }
    for project_name in registered_projects {
        tx.execute(
            "INSERT OR IGNORE INTO project_membership (project_name, user_id, role) \
             VALUES (?1, ?2, 'operator')",
            (project_name, user_id),
        )?;
    }
    let _ = now; // reserved: Python's routine only logs with it, no column write.
    Ok(())
}

/// Create a user; return the assigned `user_id`. Port of `create_user`,
/// scoped to the password path (`sso_subject`/passwordless SSO rows
/// are the follow-up SSO PR's job).
///
/// **The BEGIN IMMEDIATE transaction is load-bearing, not incidental**:
/// Python's own docstring documents a real historical race --
/// SQLite's default deferred-transaction mode lets the empty-table
/// PROBE, the INSERT, and the sysadmin/membership bootstrap grant
/// interleave with a concurrent `create_user` call, so two racing
/// callers on an empty table could BOTH read `was_empty=true` and
/// BOTH bootstrap a sysadmin (dual-sysadmin). `TransactionBehavior::
/// Immediate` takes the write-lock up front (matching SQLite's own
/// `BEGIN IMMEDIATE`), so a concurrent second creator blocks, then
/// re-reads `was_empty=false` once it acquires the lock and is
/// neither crowned nor bootstrapped.
///
/// `username`/`email` are sanitized through the SAME hidden-Unicode/
/// control-byte stripper `conexus-backend`'s `/api` body-decode
/// chokepoint uses (`conexus_core::string_sanitize::sanitize_string_leaf`)
/// -- Python's real `create_user` reuses `_strip_control_bytes` for
/// exactly this reason (an IdP-claim-derived `email` never passes
/// through the REST body sanitizer). Sanitizing on the WRITE side
/// only, deliberately: [`get_user_by_username`] (the login lookup)
/// keeps matching EXACTLY, so a submitted `ad\u{200B}min` still fails
/// to authenticate as the stored `admin` rather than being silently
/// folded onto it.
///
/// Python's `InvalidEmailError` (raised on a `UnicodeEncodeError` at
/// the SQLite bind site) has NO Rust equivalent to port: a Rust
/// `&str` is a valid UTF-8 byte sequence by construction, so there is
/// no `email: &str` value that could ever fail to bind -- the whole
/// failure class is structurally impossible here, the same class of
/// finding this migration already made for `json_sanitize`'s own
/// `Cs`/surrogate case.
#[allow(clippy::too_many_arguments)]
pub fn create_user(
    conn: &mut Connection,
    username: &str,
    password: &str,
    email: Option<&str>,
    is_sysadmin: bool,
    bootstrap_sysadmin: bool,
    registered_projects: &[String],
    now: &str,
) -> Result<String, IdentityError> {
    let username = conexus_core::string_sanitize::sanitize_string_leaf(username);
    let email = email.map(conexus_core::string_sanitize::sanitize_string_leaf);
    let user_id = random_id(8); // 16 hex chars, matches Python's secrets.token_hex(8)
    let password_hash = hash_password(password);

    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let was_empty = users_table_is_empty(&tx)?;

    let insert_result = tx.execute(
        "INSERT INTO users \
             (user_id, username, email, password_hash, created_at, last_login_at, is_sysadmin, sso_subject) \
             VALUES (?1, ?2, ?3, ?4, ?5, NULL, ?6, NULL)",
        (
            &user_id,
            &username,
            &email,
            &password_hash,
            now,
            is_sysadmin,
        ),
    );
    if let Err(e) = insert_result {
        // UNIQUE(username) is the only constraint that can fail here.
        // The transaction rolls back on Drop without an explicit
        // ROLLBACK -- rusqlite's Transaction does this automatically,
        // unlike Python's manual isolation_level=None dance.
        if matches!(
            &e,
            rusqlite::Error::SqliteFailure(err, _)
                if err.code == rusqlite::ErrorCode::ConstraintViolation
        ) {
            return Err(IdentityError::UsernameAlreadyExists(username));
        }
        return Err(IdentityError::Db(e));
    }

    if was_empty {
        bootstrap_first_operator(&tx, &user_id, bootstrap_sysadmin, registered_projects, now)?;
    }

    tx.commit()?;
    Ok(user_id)
}

/// Default session lifetime -- port of `DEFAULT_SESSION_LIFETIME_DAYS`.
pub const DEFAULT_SESSION_LIFETIME_DAYS: i64 = 30;

/// One `sessions` row, as read back by [`get_session`].
#[derive(Debug, Clone, PartialEq)]
pub struct SessionRow {
    pub session_id: String,
    pub user_id: String,
    pub created_at: String,
    pub expires_at: String,
    pub last_used_at: String,
}

/// Create a session for `user_id`; return the `session_id`.
/// `lifetime_days` may be negative -- useful for tests that want an
/// already-expired row to assert the prune sweep removes it. `now`/
/// `expires_at` are both explicit (this crate's own "never read a
/// hidden wall clock" convention) rather than computed internally
/// from `lifetime_days` off a live clock read.
pub fn create_session(
    conn: &Connection,
    user_id: &str,
    now: &str,
    expires_at: &str,
) -> Result<String, IdentityError> {
    let session_id = random_id(16); // 32 hex chars, matches Python's secrets.token_hex(16)
    conn.execute(
        "INSERT INTO sessions (session_id, user_id, created_at, expires_at, last_used_at) \
         VALUES (?1, ?2, ?3, ?4, ?3)",
        (&session_id, user_id, now, expires_at),
    )?;
    Ok(session_id)
}

/// Return the session row, or `None` if missing OR expired (compared
/// against `now`, threaded explicitly -- not read from a live clock).
/// Side effect: slides `last_used_at` to `now` on every successful
/// fetch, matching Python's exact "an active operator's session never
/// expires" sliding-window semantics. The expired-but-still-present
/// row is NOT deleted here (the periodic prune sweep owns cleanup);
/// this only refuses to surface it, so a caller can't extend an
/// expired session by mere mention.
pub fn get_session(
    conn: &Connection,
    session_id: &str,
    now: &str,
) -> Result<Option<SessionRow>, IdentityError> {
    let row: Option<(String, String, String, String)> = conn
        .query_row(
            "SELECT user_id, created_at, expires_at, last_used_at FROM sessions WHERE session_id = ?1",
            [session_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .optional()?;
    let Some((user_id, created_at, expires_at, _last_used_at)) = row else {
        return Ok(None);
    };
    // String comparison is correct here because every timestamp this
    // crate writes is RFC3339/ISO-8601 UTC with a fixed-width offset,
    // which sorts lexicographically identically to chronologically.
    if expires_at.as_str() <= now {
        return Ok(None);
    }
    conn.execute(
        "UPDATE sessions SET last_used_at = ?1 WHERE session_id = ?2",
        (now, session_id),
    )?;
    Ok(Some(SessionRow {
        session_id: session_id.to_string(),
        user_id,
        created_at,
        expires_at,
        last_used_at: now.to_string(),
    }))
}

/// Drop a session row. No-op if missing.
pub fn delete_session(conn: &Connection, session_id: &str) -> Result<(), IdentityError> {
    conn.execute("DELETE FROM sessions WHERE session_id = ?1", [session_id])?;
    Ok(())
}

/// Delete every session whose `expires_at` is in the past (compared
/// against `now`). Returns the number of rows deleted. Called
/// periodically by the router's reaper task (wired in a later PR);
/// safe to call ad-hoc.
pub fn prune_expired_sessions(conn: &Connection, now: &str) -> Result<usize, IdentityError> {
    Ok(conn.execute("DELETE FROM sessions WHERE expires_at <= ?1", [now])?)
}

/// Grant `user_id` (`operator`-tier, the schema `DEFAULT`) access to
/// `project_name`. Idempotent -- port of `add_project_membership`,
/// scoped to its own real call site's shape (`(user_id, project_name)`
/// user grants only; Python's `insert_project_membership`'s fuller
/// `group_id`/explicit-`role`/`or_ignore` surface has no other caller
/// in this crate yet, so it isn't ported wholesale -- matches this
/// module's own "PR 3 ships a deliberately tight slice" precedent).
pub fn add_project_membership(
    conn: &Connection,
    user_id: &str,
    project_name: &str,
) -> Result<(), IdentityError> {
    conn.execute(
        "INSERT OR IGNORE INTO project_membership (project_name, user_id) VALUES (?1, ?2)",
        (project_name, user_id),
    )?;
    Ok(())
}

/// Grant EXACTLY ONE of `user_id`/`group_id` `role` on `project_name`
/// -- port of `RouterStore.add_project_membership`'s real call shape
/// from `add_project_membership_handler` (a plain INSERT, never `OR
/// IGNORE` -- that handler wants a duplicate to fail, mapped to a 409
/// by the caller). Distinct from [`add_project_membership`] above
/// (which is the narrower, idempotent, user-only, default-role grant
/// `create_project_handler`'s own bootstrap needs) -- named
/// differently so the two deliberately different contracts (idempotent
/// vs. fail-on-duplicate; user-only vs. user-or-group; DB-default role
/// vs. explicit role) can never be confused at a call site.
pub fn grant_project_membership(
    conn: &Connection,
    project_name: &str,
    user_id: Option<&str>,
    group_id: Option<&str>,
    role: &str,
) -> Result<(), IdentityError> {
    debug_assert!(
        user_id.is_some() != group_id.is_some(),
        "grant_project_membership requires exactly one of user_id or group_id"
    );
    conn.execute(
        "INSERT INTO project_membership (project_name, user_id, group_id, role) VALUES (?1, ?2, ?3, ?4)",
        (project_name, user_id, group_id, role),
    )?;
    Ok(())
}

/// The current role for exactly one of `user_id`/`group_id` on
/// `project_name`, or `None` if no such row exists -- port of
/// `change_project_membership_role_handler`'s existing-role lookup
/// (AZ-R12-1's revoke-mirror guard needs the PRE-change role to apply
/// the grant guard symmetrically).
pub fn project_membership_role(
    conn: &Connection,
    project_name: &str,
    user_id: Option<&str>,
    group_id: Option<&str>,
) -> Result<Option<String>, IdentityError> {
    let id = user_id
        .or(group_id)
        .expect("project_membership_role requires exactly one of user_id or group_id");
    let sql = if user_id.is_some() {
        "SELECT role FROM project_membership WHERE project_name = ?1 AND user_id = ?2"
    } else {
        "SELECT role FROM project_membership WHERE project_name = ?1 AND group_id = ?2"
    };
    Ok(conn
        .query_row(sql, (project_name, id), |r| r.get(0))
        .optional()?)
}

/// Change the role for exactly one of `user_id`/`group_id` on
/// `project_name` -- port of `change_project_membership_role_handler`'s
/// `UPDATE`. A no-op (no error) if the row doesn't exist -- the
/// caller re-checks existence via [`project_membership_role`] first
/// and 404s before ever calling this.
pub fn update_project_membership_role(
    conn: &Connection,
    project_name: &str,
    user_id: Option<&str>,
    group_id: Option<&str>,
    role: &str,
) -> Result<(), IdentityError> {
    let id = user_id
        .or(group_id)
        .expect("update_project_membership_role requires exactly one of user_id or group_id");
    let sql = if user_id.is_some() {
        "UPDATE project_membership SET role = ?1 WHERE project_name = ?2 AND user_id = ?3"
    } else {
        "UPDATE project_membership SET role = ?1 WHERE project_name = ?2 AND group_id = ?3"
    };
    conn.execute(sql, (role, project_name, id))?;
    Ok(())
}

/// Remove exactly one of `user_id`/`group_id`'s membership row on
/// `project_name` -- port of `delete_project_membership_handler`'s
/// `DELETE`. `true` iff a row was removed (deliberately distinct from
/// [`remove_project_membership_by_project`] below, which drops EVERY
/// row for a project during project deletion -- a different contract
/// for a different caller).
pub fn remove_project_membership(
    conn: &Connection,
    project_name: &str,
    user_id: Option<&str>,
    group_id: Option<&str>,
) -> Result<bool, IdentityError> {
    let id = user_id
        .or(group_id)
        .expect("remove_project_membership requires exactly one of user_id or group_id");
    let sql = if user_id.is_some() {
        "DELETE FROM project_membership WHERE project_name = ?1 AND user_id = ?2"
    } else {
        "DELETE FROM project_membership WHERE project_name = ?1 AND group_id = ?2"
    };
    Ok(conn.execute(sql, (project_name, id))? > 0)
}

/// One row of [`list_project_memberships`] -- either a user or group
/// membership, matching `list_project_memberships_handler`'s own
/// "either shape, never the union" JSON projection. `membership_id`
/// is the `u:<id>`/`g:<id>` surrogate PATCH/DELETE addresses.
#[derive(Debug, Clone, PartialEq)]
pub enum ProjectMembershipRow {
    User {
        user_id: String,
        username: String,
        role: String,
    },
    Group {
        group_id: String,
        name: String,
        role: String,
    },
}

/// Port of `list_project_memberships_handler`'s own query: every
/// membership row for `project_name` (user or group), each carrying a
/// renderable label via a `LEFT JOIN` against `users`/`groups`.
/// Ordered by the member's own display label.
pub fn list_project_memberships(
    conn: &Connection,
    project_name: &str,
) -> Result<Vec<ProjectMembershipRow>, IdentityError> {
    let mut stmt = conn.prepare(
        "SELECT pm.user_id, pm.group_id, pm.role, u.username, g.name \
         FROM project_membership pm \
         LEFT JOIN users u ON pm.user_id = u.user_id \
         LEFT JOIN groups g ON pm.group_id = g.group_id \
         WHERE pm.project_name = ?1 \
         ORDER BY COALESCE(u.username, g.name)",
    )?;
    let rows = stmt.query_map([project_name], |row| {
        let user_id: Option<String> = row.get(0)?;
        let group_id: Option<String> = row.get(1)?;
        let role: String = row.get(2)?;
        let username: Option<String> = row.get(3)?;
        let name: Option<String> = row.get(4)?;
        Ok(if let Some(user_id) = user_id {
            ProjectMembershipRow::User {
                user_id,
                username: username.unwrap_or_default(),
                role,
            }
        } else {
            ProjectMembershipRow::Group {
                group_id: group_id.unwrap_or_default(),
                name: name.unwrap_or_default(),
                role,
            }
        })
    })?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

/// Re-key every `project_membership` row from `old_name` to
/// `new_name` -- port of `rename_project_handler`'s inline
/// best-effort `UPDATE` (AZ-R13-1). A project rename is registry-
/// primary; a membership-repoint failure here is the caller's own
/// best-effort-and-log concern, not this function's.
pub fn rename_project_membership_project(
    conn: &Connection,
    old_name: &str,
    new_name: &str,
) -> Result<(), IdentityError> {
    conn.execute(
        "UPDATE project_membership SET project_name = ?1 WHERE project_name = ?2",
        (new_name, old_name),
    )?;
    Ok(())
}

/// Drop every `project_membership` row for `project_name` -- port of
/// `delete_project_handler`'s inline best-effort `DELETE`.
pub fn remove_project_membership_by_project(
    conn: &Connection,
    project_name: &str,
) -> Result<(), IdentityError> {
    conn.execute(
        "DELETE FROM project_membership WHERE project_name = ?1",
        [project_name],
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::schema::init_router_schema;

    fn conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        c
    }

    const NOW: &str = "2026-01-01T00:00:00.000+00:00";

    #[test]
    fn hash_and_verify_round_trip() {
        let hashed = hash_password("correct horse battery staple");
        assert!(verify_password(&hashed, "correct horse battery staple"));
        assert!(!verify_password(&hashed, "wrong password"));
    }

    #[test]
    fn hash_password_uses_argon2id_with_the_pinned_params() {
        let hashed = hash_password("correct horse battery staple");
        assert!(hashed.starts_with("$argon2id$v=19$m=65536,t=2,p=4$"));
    }

    #[test]
    fn verify_password_accepts_a_hash_with_different_embedded_params() {
        // Cross-compatibility check: a hash minted with DIFFERENT
        // parameters than this module's own HASHER preset must still
        // verify -- PasswordHash carries its own params in the PHC
        // string, matching argon2-cffi interop (Python's hash and
        // Rust's verifier, or vice versa, must never depend on both
        // sides agreeing on cost parameters).
        let params = Params::new(19456, 2, 1, None).unwrap();
        let other = Argon2::new(Algorithm::Argon2id, Version::V0x13, params);
        let hashed = other
            .hash_password_with_salt(b"correct horse battery staple", b"0123456789abcdef")
            .unwrap()
            .to_string();
        assert!(verify_password(&hashed, "correct horse battery staple"));
    }

    #[test]
    fn validate_password_strength_rejects_short_passwords() {
        assert!(validate_password_strength("short").is_err());
        assert!(validate_password_strength("exactly-twelve").is_ok());
    }

    #[test]
    fn create_user_persists_and_is_retrievable() {
        let mut c = conn();
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            Some("alice@example.test"),
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let row = get_user_by_id(&c, &uid).unwrap().unwrap();
        assert_eq!(row.username, "alice");
        assert_eq!(row.email.as_deref(), Some("alice@example.test"));
        assert!(verify_password(
            &row.password_hash.unwrap(),
            "correct horse battery staple"
        ));
        let by_username = get_user_by_username(&c, "alice").unwrap().unwrap();
        assert_eq!(by_username.user_id, uid);
    }

    #[test]
    fn create_user_sanitizes_username_and_email_on_write_only() {
        let mut c = conn();
        // U+200B ZERO WIDTH SPACE embedded in both fields.
        let uid = create_user(
            &mut c,
            "ad\u{200B}min",
            "correct horse battery staple",
            Some("a\u{200B}dmin@example.test"),
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let row = get_user_by_id(&c, &uid).unwrap().unwrap();
        assert_eq!(row.username, "admin");
        assert_eq!(row.email.as_deref(), Some("admin@example.test"));
        // The lookup side does NOT sanitize -- the unsanitized
        // spoofing variant must NOT resolve to the stored "admin" row.
        assert!(get_user_by_username(&c, "ad\u{200B}min").unwrap().is_none());
        assert!(get_user_by_username(&c, "admin").unwrap().is_some());
    }

    #[test]
    fn create_user_rejects_a_duplicate_username() {
        let mut c = conn();
        create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let err = create_user(
            &mut c,
            "alice",
            "another password entirely",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap_err();
        assert!(matches!(err, IdentityError::UsernameAlreadyExists(u) if u == "alice"));
    }

    #[test]
    fn create_user_bootstraps_the_first_operator_as_sysadmin_with_project_membership() {
        let mut c = conn();
        let projects = vec!["proj-a".to_string(), "proj-b".to_string()];
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &projects,
            NOW,
        )
        .unwrap();
        let row = get_user_by_id(&c, &uid).unwrap().unwrap();
        assert!(
            row.is_sysadmin,
            "the first user on an empty table must be promoted"
        );

        let memberships: Vec<String> = c
            .prepare("SELECT project_name FROM project_membership WHERE user_id = ?1 ORDER BY project_name")
            .unwrap()
            .query_map([&uid], |r| r.get(0))
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap();
        assert_eq!(
            memberships,
            vec!["proj-a".to_string(), "proj-b".to_string()]
        );
    }

    #[test]
    fn create_user_does_not_bootstrap_a_second_user() {
        let mut c = conn();
        create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let uid2 = create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &["proj-a".to_string()],
            NOW,
        )
        .unwrap();
        let row2 = get_user_by_id(&c, &uid2).unwrap().unwrap();
        assert!(!row2.is_sysadmin, "only the FIRST user is auto-promoted");
        let count: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM project_membership WHERE user_id = ?1",
                [&uid2],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            count, 0,
            "the second user does not inherit pre-existing projects"
        );
    }

    #[test]
    fn create_user_sso_opt_out_skips_sysadmin_promotion_but_still_creates_the_first_user() {
        let mut c = conn();
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            false,
            &[],
            NOW,
        )
        .unwrap();
        let row = get_user_by_id(&c, &uid).unwrap().unwrap();
        assert!(
            !row.is_sysadmin,
            "bootstrap_sysadmin=false must not crown the first user"
        );
    }

    #[test]
    fn session_lifecycle_create_get_slides_last_used_and_delete() {
        let mut c = conn();
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();

        let later = "2026-01-01T00:05:00.000+00:00";
        let expires = "2026-02-01T00:00:00.000+00:00";
        let sid = create_session(&c, &uid, NOW, expires).unwrap();

        let fetched = get_session(&c, &sid, later).unwrap().unwrap();
        assert_eq!(fetched.user_id, uid);
        assert_eq!(
            fetched.last_used_at, later,
            "get_session slides last_used_at to `now`"
        );

        delete_session(&c, &sid).unwrap();
        assert!(get_session(&c, &sid, later).unwrap().is_none());
    }

    #[test]
    fn get_session_refuses_an_expired_session_without_deleting_it() {
        let mut c = conn();
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let expires = "2026-01-01T00:01:00.000+00:00";
        let after_expiry = "2026-01-01T00:02:00.000+00:00";
        let sid = create_session(&c, &uid, NOW, expires).unwrap();

        assert!(get_session(&c, &sid, after_expiry).unwrap().is_none());
        // Still physically present -- only the periodic sweep deletes it.
        let still_there: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM sessions WHERE session_id = ?1",
                [&sid],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(still_there, 1);
    }

    #[test]
    fn prune_expired_sessions_removes_only_past_expiry_rows() {
        let mut c = conn();
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let expired = create_session(&c, &uid, NOW, "2026-01-01T00:01:00.000+00:00").unwrap();
        let live = create_session(&c, &uid, NOW, "2027-01-01T00:00:00.000+00:00").unwrap();

        let removed = prune_expired_sessions(&c, "2026-06-01T00:00:00.000+00:00").unwrap();
        assert_eq!(removed, 1);
        assert!(get_session(&c, &expired, "2026-06-01T00:00:00.000+00:00")
            .unwrap()
            .is_none());
        assert!(get_session(&c, &live, "2026-06-01T00:00:00.000+00:00")
            .unwrap()
            .is_some());
    }

    fn membership_projects_for(c: &Connection, user_id: &str) -> Vec<String> {
        c.prepare(
            "SELECT project_name FROM project_membership WHERE user_id = ?1 ORDER BY project_name",
        )
        .unwrap()
        .query_map([user_id], |r| r.get(0))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap()
    }

    #[test]
    fn add_project_membership_grants_and_is_idempotent() {
        let mut c = conn();
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        add_project_membership(&c, &uid, "proj-a").unwrap();
        add_project_membership(&c, &uid, "proj-a").unwrap(); // idempotent, no error
        assert_eq!(
            membership_projects_for(&c, &uid),
            vec!["proj-a".to_string()]
        );
    }

    #[test]
    fn add_project_membership_defaults_to_the_operator_role() {
        let mut c = conn();
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        add_project_membership(&c, &uid, "proj-a").unwrap();
        let role: String = c
            .query_row(
                "SELECT role FROM project_membership WHERE user_id = ?1 AND project_name = ?2",
                (&uid, "proj-a"),
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(role, "operator");
    }

    #[test]
    fn rename_project_membership_project_rekeys_every_matching_row() {
        let mut c = conn();
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        add_project_membership(&c, &uid, "old-name").unwrap();
        rename_project_membership_project(&c, "old-name", "new-name").unwrap();
        assert_eq!(
            membership_projects_for(&c, &uid),
            vec!["new-name".to_string()]
        );
    }

    #[test]
    fn remove_project_membership_by_project_drops_every_matching_row() {
        let mut c = conn();
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        add_project_membership(&c, &uid, "proj-a").unwrap();
        add_project_membership(&c, &uid, "proj-b").unwrap();
        remove_project_membership_by_project(&c, "proj-a").unwrap();
        assert_eq!(
            membership_projects_for(&c, &uid),
            vec!["proj-b".to_string()]
        );
    }

    #[test]
    fn remove_project_membership_by_project_on_an_unknown_project_is_a_noop() {
        let c = conn();
        remove_project_membership_by_project(&c, "never-existed").unwrap();
    }

    #[test]
    fn grant_project_membership_grants_a_user_an_explicit_role() {
        let mut c = conn();
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        grant_project_membership(&c, "proj-a", Some(&uid), None, "viewer").unwrap();
        let role: String = c
            .query_row(
                "SELECT role FROM project_membership WHERE project_name = 'proj-a' AND user_id = ?1",
                [&uid],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(role, "viewer");
    }

    #[test]
    fn grant_project_membership_grants_a_group_an_explicit_role() {
        let c = conn();
        c.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) VALUES ('g1', 'g1', 0, ?1)",
            [NOW],
        )
        .unwrap();
        grant_project_membership(&c, "proj-a", None, Some("g1"), "operator").unwrap();
        let role: String = c
            .query_row(
                "SELECT role FROM project_membership WHERE project_name = 'proj-a' AND group_id = 'g1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(role, "operator");
    }

    #[test]
    fn grant_project_membership_rejects_a_duplicate() {
        let mut c = conn();
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        grant_project_membership(&c, "proj-a", Some(&uid), None, "operator").unwrap();
        let err = grant_project_membership(&c, "proj-a", Some(&uid), None, "operator").unwrap_err();
        assert!(matches!(err, IdentityError::Db(_)));
    }

    #[test]
    fn project_membership_role_reads_back_the_granted_role() {
        let mut c = conn();
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        grant_project_membership(&c, "proj-a", Some(&uid), None, "viewer").unwrap();
        let role = project_membership_role(&c, "proj-a", Some(&uid), None).unwrap();
        assert_eq!(role.as_deref(), Some("viewer"));
    }

    #[test]
    fn project_membership_role_is_none_for_a_missing_row() {
        let c = conn();
        assert!(project_membership_role(&c, "proj-a", Some("nobody"), None)
            .unwrap()
            .is_none());
    }

    #[test]
    fn update_project_membership_role_changes_an_existing_grant() {
        let mut c = conn();
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        grant_project_membership(&c, "proj-a", Some(&uid), None, "viewer").unwrap();
        update_project_membership_role(&c, "proj-a", Some(&uid), None, "operator").unwrap();
        let role = project_membership_role(&c, "proj-a", Some(&uid), None).unwrap();
        assert_eq!(role.as_deref(), Some("operator"));
    }

    #[test]
    fn update_project_membership_role_on_a_missing_row_is_a_noop() {
        let c = conn();
        update_project_membership_role(&c, "proj-a", Some("nobody"), None, "operator").unwrap();
    }

    #[test]
    fn remove_project_membership_deletes_a_user_row_and_reports_whether_one_existed() {
        let mut c = conn();
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        grant_project_membership(&c, "proj-a", Some(&uid), None, "operator").unwrap();
        assert!(remove_project_membership(&c, "proj-a", Some(&uid), None).unwrap());
        assert!(project_membership_role(&c, "proj-a", Some(&uid), None)
            .unwrap()
            .is_none());
        assert!(!remove_project_membership(&c, "proj-a", Some(&uid), None).unwrap());
    }

    #[test]
    fn remove_project_membership_deletes_a_group_row() {
        let c = conn();
        let group_id =
            conexus_db::group_membership_repository::create_group(&c, "engineers", false, NOW)
                .unwrap()
                .group_id;
        grant_project_membership(&c, "proj-a", None, Some(&group_id), "viewer").unwrap();
        assert!(remove_project_membership(&c, "proj-a", None, Some(&group_id)).unwrap());
    }

    #[test]
    fn list_project_memberships_projects_both_kinds() {
        let mut c = conn();
        let uid = create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let group_id =
            conexus_db::group_membership_repository::create_group(&c, "engineers", false, NOW)
                .unwrap()
                .group_id;
        grant_project_membership(&c, "proj-a", Some(&uid), None, "operator").unwrap();
        grant_project_membership(&c, "proj-a", None, Some(&group_id), "viewer").unwrap();
        let rows = list_project_memberships(&c, "proj-a").unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(
            rows[0],
            ProjectMembershipRow::User {
                user_id: uid,
                username: "alice".to_string(),
                role: "operator".to_string(),
            }
        );
        assert_eq!(
            rows[1],
            ProjectMembershipRow::Group {
                group_id,
                name: "engineers".to_string(),
                role: "viewer".to_string(),
            }
        );
    }

    #[test]
    fn list_project_memberships_is_empty_for_an_unmembered_project() {
        let c = conn();
        assert!(list_project_memberships(&c, "proj-a").unwrap().is_empty());
    }

    #[test]
    fn users_table_is_empty_reflects_real_state() {
        let mut c = conn();
        assert!(users_table_is_empty(&c).unwrap());
        create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        assert!(!users_table_is_empty(&c).unwrap());
    }

    /// Proves the `BEGIN IMMEDIATE` locking documented on [`create_user`]
    /// for real, not just by inspection: two genuinely racing OS-level
    /// SQLite connections (an in-memory `:memory:` connection can't be
    /// shared across threads, so this needs a real tempfile-backed DB)
    /// both call `create_user(..., bootstrap_sysadmin: true)` against an
    /// initially-empty `users` table at the same instant. Without the
    /// explicit `TransactionBehavior::Immediate` lock, SQLite's default
    /// deferred mode lets both connections read `was_empty=true` before
    /// either commits, crowning BOTH callers sysadmin -- the exact
    /// historical dual-sysadmin race Python's own docstring names.
    /// Repeated 20x (a race is a timing-dependent bug, one clean run
    /// proves nothing) to make a flake in either direction visible.
    #[test]
    fn create_user_bootstrap_is_atomic_under_real_concurrent_racing() {
        for _ in 0..20 {
            let dir = tempfile::tempdir().unwrap();
            let db_path = dir.path().join("router.db");

            {
                let setup = Connection::open(&db_path).unwrap();
                init_router_schema(&setup).unwrap();
            }

            let db_path_a = db_path.clone();
            let db_path_b = db_path.clone();
            let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
            let barrier_a = barrier.clone();
            let barrier_b = barrier.clone();

            let handle_a = std::thread::spawn(move || {
                let mut conn = Connection::open(&db_path_a).unwrap();
                conn.busy_timeout(std::time::Duration::from_secs(5))
                    .unwrap();
                barrier_a.wait();
                create_user(
                    &mut conn,
                    "alice",
                    "correct horse battery staple",
                    None,
                    false,
                    true,
                    &[],
                    NOW,
                )
            });
            let handle_b = std::thread::spawn(move || {
                let mut conn = Connection::open(&db_path_b).unwrap();
                conn.busy_timeout(std::time::Duration::from_secs(5))
                    .unwrap();
                barrier_b.wait();
                create_user(
                    &mut conn,
                    "bob",
                    "correct horse battery staple",
                    None,
                    false,
                    true,
                    &[],
                    NOW,
                )
            });

            let result_a = handle_a.join().unwrap();
            let result_b = handle_b.join().unwrap();
            assert!(result_a.is_ok() && result_b.is_ok(), "both inserts must succeed -- only the BOOTSTRAP must be exclusive, not the insert itself");

            let verify = Connection::open(&db_path).unwrap();
            let sysadmin_count: i64 = verify
                .query_row(
                    "SELECT COUNT(*) FROM users WHERE is_sysadmin = 1",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(
                sysadmin_count, 1,
                "exactly one racing caller must be crowned sysadmin, never zero or two"
            );
        }
    }
}
