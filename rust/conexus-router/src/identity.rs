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
//! SSO-subject reconciliation (`find_user_by_sso_subject`/
//! `find_linkable_user_by_email`/`stamp_sso_subject_if_absent`/
//! `upgrade_sso_subject`) landed once the SSO PRs did; `sessions`
//! remains rusqlite-based (see Phase G paragraph below).
//!
//! **Phase G (sea-orm migration, router step 4)**: PR C rewrote the 7
//! `project_membership`-table functions (`add_project_membership`/
//! `grant_project_membership`/`project_membership_role`/
//! `update_project_membership_role`/`remove_project_membership`/
//! `remove_project_membership_by_project`/
//! `rename_project_membership_project`) onto `sea_orm::DatabaseConnection`
//! against `conexus_db::entity::project_membership`, chosen to go first
//! for its small, low-fan-out real call-site surface. PR D rewrote the
//! 14 `users`-table functions (`users_table_is_empty`/
//! `get_user_by_username`/`get_user_by_id`/`find_user_by_sso_subject`/
//! `find_linkable_user_by_email`/`stamp_sso_subject_if_absent`/
//! `upgrade_sso_subject`/`touch_last_login`/`list_users`/
//! `get_user_public_by_id`/`admin_create_user`/`bootstrap_first_operator`/
//! `create_user`/`create_sso_user`) onto `sea_orm::DatabaseConnection`
//! against `conexus_db::entity::users`, EXCEPT for the eight functions
//! `sso.rs`'s `find_or_create_sso_user`/`extract_proxy_header_user`
//! call (`users_table_is_empty`/`get_user_by_username`/
//! `get_user_by_id`/`find_user_by_sso_subject`/
//! `find_linkable_user_by_email`/`stamp_sso_subject_if_absent`/
//! `touch_last_login`/`create_sso_user`), which keep a
//! deliberately-still-sync `_sync`-suffixed rusqlite twin (see each
//! twin's own doc) -- `sso.rs` is called synchronously from
//! `session_gate.rs::evaluate_session_gate`, this migration's own
//! declared highest-risk hot path, and forcing it async is explicitly
//! out of scope for PR D (reserved for whichever PR converts
//! `sessions`, deliberately last in this sequence). `sessions`/
//! `groups`/`group_membership` (this same module's OTHER functions)
//! remain rusqlite::Connection-based; `sessions` last since it's the
//! highest-risk piece (`session_gate.rs`'s hot path).
//!
//! **`create_user`/`create_sso_user`'s `BEGIN IMMEDIATE` under
//! sea-orm**: sea-orm 2.0.2 exposes SQLite's transaction-mode keyword
//! natively via `TransactionTrait::begin_with_options` +
//! `TransactionOptions.sqlite_transaction_mode` (`SqliteTransactionMode::
//! Immediate` maps directly to `BEGIN IMMEDIATE`) -- no raw-SQL escape
//! hatch or `Statement::from_string("BEGIN IMMEDIATE")` workaround
//! needed, verified directly against `sea-orm-2.0.2`'s own
//! `driver/sqlx_sqlite.rs`/`driver/rusqlite.rs`. [`create_user_row`]'s
//! own real concurrent-racing test
//! (`create_user_bootstrap_is_atomic_under_real_concurrent_racing`)
//! proves the SAME dual-sysadmin race this file's rusqlite-era version
//! already guarded against stays closed under sea-orm.
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
use conexus_db::entity::project_membership::{ActiveModel, Column, Entity};
use conexus_db::entity::users::{
    ActiveModel as UserActiveModel, Column as UserColumn, Entity as UserEntity, Model as UserModel,
};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior};
use sea_orm::ActiveValue::{self, Set};
use sea_orm::{
    ColumnTrait, ConnectionTrait, DatabaseConnection, DatabaseTransaction, EntityTrait,
    QueryFilter, QueryOrder, QuerySelect, SqliteTransactionMode, Statement, TransactionOptions,
    TransactionTrait,
};

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
    /// Phase G (sea-orm migration, router step 4 PR C): every
    /// `project_membership` function below is converted onto
    /// `sea_orm::DatabaseConnection` -- their errors surface here
    /// rather than through [`IdentityError::Db`], which stays
    /// `rusqlite::Error`-shaped for the (still-sync) users/sessions
    /// functions this same enum covers.
    SeaOrm(sea_orm::DbErr),
}

impl From<rusqlite::Error> for IdentityError {
    fn from(e: rusqlite::Error) -> Self {
        IdentityError::Db(e)
    }
}

impl From<sea_orm::DbErr> for IdentityError {
    fn from(e: sea_orm::DbErr) -> Self {
        IdentityError::SeaOrm(e)
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
            IdentityError::SeaOrm(e) => write!(f, "database error: {e}"),
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

/// `true` iff `sql_err`'s portable classification identifies `e` as a
/// UNIQUE-constraint violation -- the SAME check `admin_project_
/// membership::finish_add_project_membership` already established for
/// this migration (`DbErr::sql_err`'s own doc: portable across MySQL/
/// Postgres/SQLite, unlike sniffing a backend-specific error code).
fn is_unique_violation(e: &sea_orm::DbErr) -> bool {
    matches!(
        e.sql_err(),
        Some(sea_orm::SqlErr::UniqueConstraintViolation(_))
    )
}

fn user_row_from_model(m: UserModel) -> UserRow {
    UserRow {
        user_id: m.user_id,
        username: m.username,
        email: m.email,
        password_hash: m.password_hash,
        created_at: m.created_at,
        last_login_at: m.last_login_at,
        is_sysadmin: m.is_sysadmin,
        sso_subject: m.sso_subject,
    }
}

/// Row-extraction twin of [`user_row_from_model`] for the raw-SQL
/// escape hatch [`find_linkable_user_by_email`] needs -- same reason
/// `list_project_memberships` above has its own manual `try_get`
/// extraction: a query shape sea-query's typed builder can't express
/// has no `Entity`/`Model` to decode through.
fn user_row_from_query_result(row: &sea_orm::QueryResult) -> Result<UserRow, sea_orm::DbErr> {
    Ok(UserRow {
        user_id: row.try_get("", "user_id")?,
        username: row.try_get("", "username")?,
        email: row.try_get("", "email")?,
        password_hash: row.try_get("", "password_hash")?,
        created_at: row.try_get("", "created_at")?,
        last_login_at: row.try_get("", "last_login_at")?,
        is_sysadmin: row.try_get("", "is_sysadmin")?,
        sso_subject: row.try_get("", "sso_subject")?,
    })
}

/// `true` iff the `users` table has zero rows. Port of
/// `users_table_is_empty`. Generic over [`ConnectionTrait`] so
/// [`create_user_row`]'s own `BEGIN IMMEDIATE` transaction can reuse
/// the SAME check against its `&DatabaseTransaction` (see that
/// function's own doc) rather than duplicating this query.
async fn users_table_is_empty_impl<C: ConnectionTrait>(conn: &C) -> Result<bool, sea_orm::DbErr> {
    let found = UserEntity::find()
        .select_only()
        .column(UserColumn::UserId)
        .limit(1)
        .into_tuple::<String>()
        .one(conn)
        .await?;
    Ok(found.is_none())
}

pub async fn users_table_is_empty(db: &DatabaseConnection) -> Result<bool, IdentityError> {
    Ok(users_table_is_empty_impl(db).await?)
}

/// Sync rusqlite duplicate of [`users_table_is_empty`] -- kept ONLY
/// for `sso.rs`'s `extract_proxy_header_user` (see this module's own
/// doc: called synchronously from `session_gate.rs::
/// evaluate_session_gate`, out of scope to force async here).
pub(crate) fn users_table_is_empty_sync(conn: &Connection) -> Result<bool, IdentityError> {
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

pub async fn get_user_by_username(
    db: &DatabaseConnection,
    username: &str,
) -> Result<Option<UserRow>, IdentityError> {
    Ok(UserEntity::find()
        .filter(UserColumn::Username.eq(username))
        .one(db)
        .await?
        .map(user_row_from_model))
}

/// Sync rusqlite duplicate of [`get_user_by_username`] -- kept ONLY
/// for `sso.rs`'s `find_or_create_sso_user` (see this module's own
/// doc).
pub(crate) fn get_user_by_username_sync(
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

pub async fn get_user_by_id(
    db: &DatabaseConnection,
    user_id: &str,
) -> Result<Option<UserRow>, IdentityError> {
    Ok(UserEntity::find_by_id(user_id)
        .one(db)
        .await?
        .map(user_row_from_model))
}

/// Sync rusqlite duplicate of [`get_user_by_id`] -- kept for `sso.rs`'s
/// `find_or_create_sso_user` (see this module's own doc) AND
/// `login.rs::resolve_current_user`, which reads the session's owning
/// user on the SAME `evaluate_session_gate` hot path.
pub(crate) fn get_user_by_id_sync(
    conn: &Connection,
    user_id: &str,
) -> Result<Option<UserRow>, IdentityError> {
    Ok(conn
        .query_row(
            &format!("SELECT {USER_COLUMNS} FROM users WHERE user_id = ?1"),
            [user_id],
            row_to_user,
        )
        .optional()?)
}

/// Port of `_find_user_by_subject` (SSO). Direct subject-reconciliation
/// lookup -- the FIRST step of `find_or_create_sso_user`'s 3-step
/// algorithm, and what makes repeated calls with the SAME subject
/// resolve to the SAME row (no session cookie exists in proxy-header
/// mode, so this runs on every request).
pub async fn find_user_by_sso_subject(
    db: &DatabaseConnection,
    subject: &str,
) -> Result<Option<UserRow>, IdentityError> {
    Ok(UserEntity::find()
        .filter(UserColumn::SsoSubject.eq(subject))
        .one(db)
        .await?
        .map(user_row_from_model))
}

/// Sync rusqlite duplicate of [`find_user_by_sso_subject`] -- kept
/// ONLY for `sso.rs`'s `find_or_create_sso_user` and `oidc_reconcile.rs`'s
/// legacy-subject self-heal path stays on the fully-async version (see
/// this module's own doc for why only `sso.rs`'s call sites need a
/// twin).
pub(crate) fn find_user_by_sso_subject_sync(
    conn: &Connection,
    subject: &str,
) -> Result<Option<UserRow>, IdentityError> {
    Ok(conn
        .query_row(
            &format!("SELECT {USER_COLUMNS} FROM users WHERE sso_subject = ?1"),
            [subject],
            row_to_user,
        )
        .optional()?)
}

/// Port of `_find_linkable_user_by_email` (SSO). A verified-email
/// claim links to an EXISTING password-authenticated account in
/// preference to a legacy passwordless-SSO row sharing the same
/// address (`ORDER BY (password_hash IS NULL) ASC` -- a row WITH a
/// password sorts first). Case-insensitive per the real column
/// comparison Python's own query uses. Raw-SQL escape hatch (matching
/// `list_project_memberships`'s own precedent above): the `ORDER BY
/// (password_hash IS NULL) ASC` expression-ordering has no sea-query
/// typed-builder combinator.
pub async fn find_linkable_user_by_email(
    db: &DatabaseConnection,
    email: &str,
) -> Result<Option<UserRow>, IdentityError> {
    let stmt = Statement::from_sql_and_values(
        sea_orm::DatabaseBackend::Sqlite,
        format!(
            "SELECT {USER_COLUMNS} FROM users \
             WHERE LOWER(email) = LOWER(?1) AND (password_hash IS NOT NULL OR sso_subject IS NULL) \
             ORDER BY (password_hash IS NULL) ASC \
             LIMIT 1"
        ),
        [email.into()],
    );
    db.query_one_raw(stmt)
        .await?
        .map(|row| user_row_from_query_result(&row))
        .transpose()
        .map_err(IdentityError::from)
}

/// Sync rusqlite duplicate of [`find_linkable_user_by_email`] -- kept
/// ONLY for `sso.rs`'s `find_or_create_sso_user` (see this module's
/// own doc).
pub(crate) fn find_linkable_user_by_email_sync(
    conn: &Connection,
    email: &str,
) -> Result<Option<UserRow>, IdentityError> {
    Ok(conn
        .query_row(
            &format!(
                "SELECT {USER_COLUMNS} FROM users \
                 WHERE LOWER(email) = LOWER(?1) AND (password_hash IS NOT NULL OR sso_subject IS NULL) \
                 ORDER BY (password_hash IS NULL) ASC \
                 LIMIT 1"
            ),
            [email],
            row_to_user,
        )
        .optional()?)
}

/// Port of `_stamp_subject_if_absent` (SSO). The `sso_subject IS NULL`
/// guard is load-bearing, not defensive: it's what makes this
/// race-safe under a concurrent double-bind attempt together with
/// `idx_users_sso_subject` (the partial UNIQUE index in `schema.rs`)
/// -- never simplify this to an unconditional UPDATE/upsert.
pub async fn stamp_sso_subject_if_absent(
    db: &DatabaseConnection,
    user_id: &str,
    subject: &str,
) -> Result<(), IdentityError> {
    UserEntity::update_many()
        .col_expr(
            UserColumn::SsoSubject,
            sea_orm::sea_query::Expr::value(subject),
        )
        .filter(UserColumn::UserId.eq(user_id))
        .filter(UserColumn::SsoSubject.is_null())
        .exec(db)
        .await?;
    Ok(())
}

/// Sync rusqlite duplicate of [`stamp_sso_subject_if_absent`] -- kept
/// ONLY for `sso.rs`'s `find_or_create_sso_user` (see this module's
/// own doc).
pub(crate) fn stamp_sso_subject_if_absent_sync(
    conn: &Connection,
    user_id: &str,
    subject: &str,
) -> Result<(), IdentityError> {
    conn.execute(
        "UPDATE users SET sso_subject = ?1 WHERE user_id = ?2 AND sso_subject IS NULL",
        (subject, user_id),
    )?;
    Ok(())
}

/// Port of `upgrade_sso_subject` -- R19-F1's self-heal, re-stamping a
/// row matched via the pre-R18-F1 UNTAGGED reconciliation key to the
/// current tagged format. The `WHERE sso_subject = old_subject` guard
/// (exact match, not `IS NULL`, unlike [`stamp_sso_subject_if_absent`])
/// means this only ever advances the SAME row that was just matched by
/// that exact legacy string -- it can't clobber a row that has
/// concurrently already moved on to a different subject. No `_sync`
/// twin: only `oidc_reconcile.rs` (already-async) calls this.
pub async fn upgrade_sso_subject(
    db: &DatabaseConnection,
    user_id: &str,
    old_subject: &str,
    new_subject: &str,
) -> Result<(), IdentityError> {
    UserEntity::update_many()
        .col_expr(
            UserColumn::SsoSubject,
            sea_orm::sea_query::Expr::value(new_subject),
        )
        .filter(UserColumn::UserId.eq(user_id))
        .filter(UserColumn::SsoSubject.eq(old_subject))
        .exec(db)
        .await?;
    Ok(())
}

/// Port of `touch_last_login`.
pub async fn touch_last_login(
    db: &DatabaseConnection,
    user_id: &str,
    now: &str,
) -> Result<(), IdentityError> {
    UserEntity::update_many()
        .col_expr(
            UserColumn::LastLoginAt,
            sea_orm::sea_query::Expr::value(now),
        )
        .filter(UserColumn::UserId.eq(user_id))
        .exec(db)
        .await?;
    Ok(())
}

/// Sync rusqlite duplicate of [`touch_last_login`] -- kept ONLY for
/// `sso.rs`'s `find_or_create_sso_user` (see this module's own doc).
pub(crate) fn touch_last_login_sync(
    conn: &Connection,
    user_id: &str,
    now: &str,
) -> Result<(), IdentityError> {
    conn.execute(
        "UPDATE users SET last_login_at = ?1 WHERE user_id = ?2",
        (now, user_id),
    )?;
    Ok(())
}

/// A `users` row with every SENSITIVE column excluded (`password_hash`,
/// `sso_subject`) -- port of the exact column list
/// `admin_users_api.py`'s `list_users_handler`/`create_user_handler`/
/// `edit_user_handler` SELECT. Deliberately a SEPARATE struct/query
/// from [`UserRow`], never fetching the hash at all, rather than
/// fetching the full row and dropping the field at the JSON-response
/// layer -- the same "don't even pull the secret across the boundary"
/// posture `admin_tools.rs`'s `redact_agent_row` established for
/// agent rows in the tool-catalogue layer. [`list_users`]/
/// [`get_user_public_by_id`] preserve this via `select_only()` +
/// `into_tuple()` -- the SQL text itself never names `password_hash`/
/// `sso_subject`, not merely "fetched then dropped in Rust".
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

type UserPublicTuple = (String, String, Option<String>, bool, String, Option<String>);

fn user_public_row_from_tuple(t: UserPublicTuple) -> UserPublicRow {
    UserPublicRow {
        user_id: t.0,
        username: t.1,
        email: t.2,
        is_sysadmin: t.3,
        created_at: t.4,
        last_login_at: t.5,
    }
}

fn user_public_select() -> sea_orm::Select<UserEntity> {
    UserEntity::find()
        .select_only()
        .column(UserColumn::UserId)
        .column(UserColumn::Username)
        .column(UserColumn::Email)
        .column(UserColumn::IsSysadmin)
        .column(UserColumn::CreatedAt)
        .column(UserColumn::LastLoginAt)
}

/// Port of `list_users_handler`: every user, public projection,
/// ordered by username.
pub async fn list_users(db: &DatabaseConnection) -> Result<Vec<UserPublicRow>, IdentityError> {
    let rows: Vec<UserPublicTuple> = user_public_select()
        .order_by_asc(UserColumn::Username)
        .into_tuple()
        .all(db)
        .await?;
    Ok(rows.into_iter().map(user_public_row_from_tuple).collect())
}

pub async fn get_user_public_by_id(
    db: &DatabaseConnection,
    user_id: &str,
) -> Result<Option<UserPublicRow>, IdentityError> {
    let row: Option<UserPublicTuple> = user_public_select()
        .filter(UserColumn::UserId.eq(user_id))
        .into_tuple()
        .one(db)
        .await?;
    Ok(row.map(user_public_row_from_tuple))
}

/// Deliberately-still-sync twin of [`get_user_public_by_id`] for
/// `admin_users_users.rs::decide_edit_user`, which reads inside an
/// in-flight, uncommitted `rusqlite::Transaction` shared with its own
/// `is_sysadmin`/`email` `UPDATE`s (its own `BEGIN IMMEDIATE`
/// last-sysadmin-demotion race guard) -- same "genuinely SEPARATE
/// connection pool would see stale, pre-commit data" rationale as
/// `task_repository::get_by_id_in_transaction`'s own precedent. Not
/// deleted when `decide_edit_user` itself is eventually converted --
/// delete this helper THEN, once nothing calls it.
pub(crate) fn get_user_public_by_id_in_transaction(
    tx: &rusqlite::Transaction,
    user_id: &str,
) -> Result<Option<UserPublicRow>, IdentityError> {
    Ok(tx
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
pub async fn admin_create_user(
    db: &DatabaseConnection,
    username: &str,
    password: &str,
    email: Option<&str>,
    is_sysadmin: bool,
    now: &str,
) -> Result<UserPublicRow, IdentityError> {
    let user_id = random_id(8);
    let password_hash = hash_password(password);
    let am = UserActiveModel {
        user_id: Set(user_id.clone()),
        username: Set(username.to_string()),
        email: Set(email.map(str::to_string)),
        password_hash: Set(Some(password_hash)),
        created_at: Set(now.to_string()),
        last_login_at: Set(None),
        is_sysadmin: Set(is_sysadmin),
        sso_subject: Set(None),
    };
    if let Err(e) = UserEntity::insert(am).exec(db).await {
        if is_unique_violation(&e) {
            return Err(IdentityError::UsernameAlreadyExists(username.to_string()));
        }
        return Err(e.into());
    }
    get_user_public_by_id(db, &user_id).await?.ok_or_else(|| {
        IdentityError::SeaOrm(sea_orm::DbErr::RecordNotFound(
            "user just inserted is missing".to_string(),
        ))
    })
}

/// Apply the first-operator bootstrap invariant to `user_id` -- port
/// of `bootstrap_first_operator`. THE single routine for the
/// security-critical rule "the first user on an otherwise-empty users
/// table becomes sysadmin and gets membership in every registered
/// project." Runs on the CALLER's transaction (`tx`) so it's atomic
/// with the INSERT that created `user_id` -- see [`create_user_row`]'s
/// own doc for why that atomicity matters (a real historical dual-
/// sysadmin race). Takes a `&DatabaseTransaction` rather than the
/// bare `&DatabaseConnection` every other function here uses -- this
/// is intentionally NOT ALSO generic over [`ConnectionTrait`] like
/// [`users_table_is_empty_impl`]: it has no standalone caller outside
/// [`create_user_row`]'s own transaction, so there is no second
/// concrete type it would ever need to serve.
///
/// `registered_projects` is the pre-Phase-1-deployment migration story
/// (existing single-tenant deploys upgrade smoothly because the first
/// operator inherits access to every project they already had) --
/// threaded explicitly since the project-registry reader doesn't exist
/// in this crate yet (PR 5). An empty slice is the correct, safe
/// input until then, not a stub -- a fresh deployment has no
/// pre-existing projects to inherit either.
pub async fn bootstrap_first_operator(
    tx: &DatabaseTransaction,
    user_id: &str,
    grant_sysadmin: bool,
    registered_projects: &[String],
    now: &str,
) -> Result<(), IdentityError> {
    if grant_sysadmin {
        UserEntity::update_many()
            .col_expr(
                UserColumn::IsSysadmin,
                sea_orm::sea_query::Expr::value(true),
            )
            .filter(UserColumn::UserId.eq(user_id))
            .exec(tx)
            .await?;
    }
    for project_name in registered_projects {
        let am = ActiveModel {
            rowid: ActiveValue::NotSet,
            project_name: Set(project_name.clone()),
            user_id: Set(Some(user_id.to_string())),
            group_id: Set(None),
            role: ActiveValue::NotSet, // let the schema DEFAULT 'operator' apply
        };
        // Same `try_insert`/`OnConflict::do_nothing` idiom as
        // `add_project_membership` above (INSERT OR IGNORE semantics
        // -- see that function's own doc for why `.try_insert()` is
        // load-bearing, not decorative).
        match Entity::insert(am)
            .on_conflict(
                sea_orm::sea_query::OnConflict::new()
                    .do_nothing()
                    .to_owned(),
            )
            .try_insert()
            .exec(tx)
            .await?
        {
            sea_orm::TryInsertResult::Inserted(_) | sea_orm::TryInsertResult::Conflicted => {}
            sea_orm::TryInsertResult::Empty => {
                unreachable!("Insert::one always produces exactly one row to try")
            }
        }
    }
    let _ = now; // reserved: Python's routine only logs with it, no column write.
    Ok(())
}

/// Sync rusqlite duplicate of [`bootstrap_first_operator`] -- kept
/// ONLY for [`create_user_row_sync`] (`sso.rs`'s `create_sso_user_sync`
/// call path; see this module's own doc). Unchanged from this file's
/// pre-Phase-G implementation.
fn bootstrap_first_operator_sync(
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
/// are [`create_sso_user`]'s job). See [`create_user_row`] for the
/// full `BEGIN IMMEDIATE`/sanitization rationale both entry points
/// share.
#[allow(clippy::too_many_arguments)]
pub async fn create_user(
    db: &DatabaseConnection,
    username: &str,
    password: &str,
    email: Option<&str>,
    is_sysadmin: bool,
    bootstrap_sysadmin: bool,
    registered_projects: &[String],
    now: &str,
) -> Result<String, IdentityError> {
    let password_hash = hash_password(password);
    create_user_row(
        db,
        username,
        Some(&password_hash),
        None,
        email,
        is_sysadmin,
        bootstrap_sysadmin,
        registered_projects,
        now,
    )
    .await
}

/// Create a passwordless SSO-JIT user row -- port of `find_or_create_
/// sso_user`'s own create step (`identity.py`'s unified `create_user`,
/// scoped here to the `sso_subject`-bearing half [`create_user`]
/// above deliberately doesn't cover). A genuinely separate PUBLIC
/// function rather than widening [`create_user`]'s own signature: the
/// latter already has 34+ call sites across this crate, and Rust has
/// no default-argument mechanism to add an optional `sso_subject`
/// without touching every one of them for zero behavioral gain --
/// both functions instead share the SAME atomicity-critical
/// [`create_user_row`] inner helper, so the `BEGIN IMMEDIATE`/
/// dual-sysadmin-race protection is written exactly once regardless
/// of which public entry point a caller uses (Python's own real
/// motivation for unifying `create_user`/`_create_passwordless_user`
/// in the first place -- see `identity.py`'s own comment on that
/// unification).
pub async fn create_sso_user(
    db: &DatabaseConnection,
    username: &str,
    sso_subject: &str,
    email: Option<&str>,
    is_sysadmin: bool,
    bootstrap_sysadmin: bool,
    now: &str,
) -> Result<String, IdentityError> {
    create_user_row(
        db,
        username,
        None,
        Some(sso_subject),
        email,
        is_sysadmin,
        bootstrap_sysadmin,
        &[],
        now,
    )
    .await
}

/// Sync rusqlite duplicate of [`create_sso_user`] -- kept ONLY for
/// `sso.rs`'s `find_or_create_sso_user`/`extract_proxy_header_user`
/// (see this module's own doc: called synchronously from
/// `session_gate.rs::evaluate_session_gate`). Shares
/// [`create_user_row_sync`]/[`bootstrap_first_operator_sync`] rather
/// than [`create_user_row`], so the `BEGIN IMMEDIATE` dual-sysadmin
/// race protection stays written twice total across this file (once
/// sea-orm, once rusqlite) rather than N times.
pub(crate) fn create_sso_user_sync(
    conn: &mut Connection,
    username: &str,
    sso_subject: &str,
    email: Option<&str>,
    is_sysadmin: bool,
    bootstrap_sysadmin: bool,
    now: &str,
) -> Result<String, IdentityError> {
    create_user_row_sync(
        conn,
        username,
        None,
        Some(sso_subject),
        email,
        is_sysadmin,
        bootstrap_sysadmin,
        &[],
        now,
    )
}

/// Shared inner implementation for [`create_user`]/[`create_sso_user`]
/// -- exactly one of `password_hash`/`sso_subject` is ever set by a
/// real caller (never enforced here since both current callers are
/// this module's own trusted code, not untrusted input).
///
/// **The BEGIN IMMEDIATE transaction is load-bearing, not incidental**:
/// this file's own historical rusqlite version (still alive as
/// [`create_user_row_sync`]) already documented a real race --
/// SQLite's default deferred-transaction mode lets the empty-table
/// PROBE, the INSERT, and the sysadmin/membership bootstrap grant
/// interleave with a concurrent `create_user` call, so two racing
/// callers on an empty table could BOTH read `was_empty=true` and
/// BOTH bootstrap a sysadmin (dual-sysadmin). `SqliteTransactionMode::
/// Immediate` (via `TransactionTrait::begin_with_options`) takes the
/// write-lock up front (matching SQLite's own `BEGIN IMMEDIATE`), so a
/// concurrent second creator blocks, then re-reads `was_empty=false`
/// once it acquires the lock and is neither crowned nor bootstrapped.
/// Proven, not just documented, by
/// `create_user_bootstrap_is_atomic_under_real_concurrent_racing`
/// below (20 real-OS-thread reps).
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
async fn create_user_row(
    db: &DatabaseConnection,
    username: &str,
    password_hash: Option<&str>,
    sso_subject: Option<&str>,
    email: Option<&str>,
    is_sysadmin: bool,
    bootstrap_sysadmin: bool,
    registered_projects: &[String],
    now: &str,
) -> Result<String, IdentityError> {
    let username = conexus_core::string_sanitize::sanitize_string_leaf(username);
    let email = email.map(conexus_core::string_sanitize::sanitize_string_leaf);
    let user_id = random_id(8); // 16 hex chars, matches Python's secrets.token_hex(8)

    let tx = db
        .begin_with_options(TransactionOptions {
            sqlite_transaction_mode: Some(SqliteTransactionMode::Immediate),
            ..Default::default()
        })
        .await?;
    let was_empty = users_table_is_empty_impl(&tx).await?;

    let am = UserActiveModel {
        user_id: Set(user_id.clone()),
        username: Set(username.clone()),
        email: Set(email),
        password_hash: Set(password_hash.map(str::to_string)),
        created_at: Set(now.to_string()),
        last_login_at: Set(None),
        is_sysadmin: Set(is_sysadmin),
        sso_subject: Set(sso_subject.map(str::to_string)),
    };
    // The transaction rolls back on Drop without an explicit
    // ROLLBACK when this early-returns -- `DatabaseTransaction`'s own
    // `Drop` impl does this automatically, matching rusqlite's
    // `Transaction` behavior this file's sync twins rely on.
    if let Err(e) = UserEntity::insert(am).exec(&tx).await {
        // UNIQUE(username) is the only constraint that can fail here.
        if is_unique_violation(&e) {
            return Err(IdentityError::UsernameAlreadyExists(username));
        }
        return Err(e.into());
    }

    if was_empty {
        bootstrap_first_operator(&tx, &user_id, bootstrap_sysadmin, registered_projects, now)
            .await?;
    }

    tx.commit().await?;
    Ok(user_id)
}

/// Sync rusqlite duplicate of [`create_user_row`] -- kept ONLY for
/// [`create_sso_user_sync`] (`sso.rs`'s call path; see this module's
/// own doc). Unchanged from this file's pre-Phase-G implementation.
#[allow(clippy::too_many_arguments)]
fn create_user_row_sync(
    conn: &mut Connection,
    username: &str,
    password_hash: Option<&str>,
    sso_subject: Option<&str>,
    email: Option<&str>,
    is_sysadmin: bool,
    bootstrap_sysadmin: bool,
    registered_projects: &[String],
    now: &str,
) -> Result<String, IdentityError> {
    let username = conexus_core::string_sanitize::sanitize_string_leaf(username);
    let email = email.map(conexus_core::string_sanitize::sanitize_string_leaf);
    let user_id = random_id(8); // 16 hex chars, matches Python's secrets.token_hex(8)

    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let was_empty = users_table_is_empty_sync(&tx)?;

    let insert_result = tx.execute(
        "INSERT INTO users \
             (user_id, username, email, password_hash, created_at, last_login_at, is_sysadmin, sso_subject) \
             VALUES (?1, ?2, ?3, ?4, ?5, NULL, ?6, ?7)",
        (
            &user_id,
            &username,
            &email,
            &password_hash,
            now,
            is_sysadmin,
            &sso_subject,
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
        bootstrap_first_operator_sync(&tx, &user_id, bootstrap_sysadmin, registered_projects, now)?;
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
pub async fn add_project_membership(
    db: &DatabaseConnection,
    user_id: &str,
    project_name: &str,
) -> Result<(), IdentityError> {
    let am = ActiveModel {
        rowid: ActiveValue::NotSet,
        project_name: Set(project_name.to_string()),
        user_id: Set(Some(user_id.to_string())),
        group_id: Set(None),
        role: ActiveValue::NotSet, // let the schema DEFAULT 'operator' apply
    };
    // `ON CONFLICT DO NOTHING` with NO target column list -- SQLite
    // accepts this ambiguous-target form and it matches "ignore ANY
    // constraint violation on this row" (the real `uq_project_
    // membership_user` index this INSERT can hit is a PARTIAL unique
    // index; sea-query's typed `OnConflict::columns(...)` target
    // list has no vocabulary for a partial index's own `WHERE`
    // clause, but the target-less form sidesteps that entirely and is
    // exactly what rusqlite's verb-level `INSERT OR IGNORE` compiled
    // to before this rewrite).
    //
    // `.try_insert()` (not plain `.insert()`) is load-bearing, found
    // by a real failing test: sea-orm's own `Insert::exec` surfaces a
    // skipped `DO NOTHING` conflict as `Err(DbErr::RecordNotInserted)`
    // -- a real error, not a quiet no-op -- which broke this
    // function's idempotency contract (a second call errored instead
    // of silently succeeding). `TryInsert::exec` is sea-orm's own
    // purpose-built wrapper for exactly this: it maps that SAME
    // `RecordNotInserted` to `TryInsertResult::Conflicted` instead of
    // an `Err`, matching `INSERT OR IGNORE`'s real semantics.
    match Entity::insert(am)
        .on_conflict(
            sea_orm::sea_query::OnConflict::new()
                .do_nothing()
                .to_owned(),
        )
        .try_insert()
        .exec(db)
        .await?
    {
        sea_orm::TryInsertResult::Inserted(_) | sea_orm::TryInsertResult::Conflicted => Ok(()),
        sea_orm::TryInsertResult::Empty => {
            unreachable!("Insert::one always produces exactly one row to try")
        }
    }
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
pub async fn grant_project_membership(
    db: &DatabaseConnection,
    project_name: &str,
    user_id: Option<&str>,
    group_id: Option<&str>,
    role: &str,
) -> Result<(), IdentityError> {
    debug_assert!(
        user_id.is_some() != group_id.is_some(),
        "grant_project_membership requires exactly one of user_id or group_id"
    );
    let am = ActiveModel {
        rowid: ActiveValue::NotSet,
        project_name: Set(project_name.to_string()),
        user_id: Set(user_id.map(str::to_string)),
        group_id: Set(group_id.map(str::to_string)),
        role: Set(role.to_string()),
    };
    Entity::insert(am).exec(db).await?;
    Ok(())
}

/// The current role for exactly one of `user_id`/`group_id` on
/// `project_name`, or `None` if no such row exists -- port of
/// `change_project_membership_role_handler`'s existing-role lookup
/// (AZ-R12-1's revoke-mirror guard needs the PRE-change role to apply
/// the grant guard symmetrically).
pub async fn project_membership_role(
    db: &DatabaseConnection,
    project_name: &str,
    user_id: Option<&str>,
    group_id: Option<&str>,
) -> Result<Option<String>, IdentityError> {
    let id = user_id
        .or(group_id)
        .expect("project_membership_role requires exactly one of user_id or group_id");
    let query = Entity::find().filter(Column::ProjectName.eq(project_name));
    let query = if user_id.is_some() {
        query.filter(Column::UserId.eq(id))
    } else {
        query.filter(Column::GroupId.eq(id))
    };
    let row = query.one(db).await?;
    Ok(row.map(|m| m.role))
}

/// Change the role for exactly one of `user_id`/`group_id` on
/// `project_name` -- port of `change_project_membership_role_handler`'s
/// `UPDATE`. A no-op (no error) if the row doesn't exist -- the
/// caller re-checks existence via [`project_membership_role`] first
/// and 404s before ever calling this.
pub async fn update_project_membership_role(
    db: &DatabaseConnection,
    project_name: &str,
    user_id: Option<&str>,
    group_id: Option<&str>,
    role: &str,
) -> Result<(), IdentityError> {
    let id = user_id
        .or(group_id)
        .expect("update_project_membership_role requires exactly one of user_id or group_id");
    let query = Entity::update_many()
        .col_expr(Column::Role, sea_orm::sea_query::Expr::value(role))
        .filter(Column::ProjectName.eq(project_name));
    let query = if user_id.is_some() {
        query.filter(Column::UserId.eq(id))
    } else {
        query.filter(Column::GroupId.eq(id))
    };
    query.exec(db).await?;
    Ok(())
}

/// Remove exactly one of `user_id`/`group_id`'s membership row on
/// `project_name` -- port of `delete_project_membership_handler`'s
/// `DELETE`. `true` iff a row was removed (deliberately distinct from
/// [`remove_project_membership_by_project`] below, which drops EVERY
/// row for a project during project deletion -- a different contract
/// for a different caller).
pub async fn remove_project_membership(
    db: &DatabaseConnection,
    project_name: &str,
    user_id: Option<&str>,
    group_id: Option<&str>,
) -> Result<bool, IdentityError> {
    let id = user_id
        .or(group_id)
        .expect("remove_project_membership requires exactly one of user_id or group_id");
    let query = Entity::delete_many().filter(Column::ProjectName.eq(project_name));
    let query = if user_id.is_some() {
        query.filter(Column::UserId.eq(id))
    } else {
        query.filter(Column::GroupId.eq(id))
    };
    let result = query.exec(db).await?;
    Ok(result.rows_affected > 0)
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
/// Raw-SQL escape hatch (matching `rag_repository::search_similar`/
/// `task_comments_repository::task_status`'s own precedent), not a
/// typed sea-orm query-builder chain: this needs a two-way `LEFT
/// JOIN` (against `users` AND `groups` simultaneously) ordered by
/// `COALESCE(u.username, g.name)`, which sea-query's typed builder
/// has no combinator for -- the SQL text is otherwise identical to
/// the rusqlite version this replaces.
pub async fn list_project_memberships(
    db: &DatabaseConnection,
    project_name: &str,
) -> Result<Vec<ProjectMembershipRow>, IdentityError> {
    let stmt = Statement::from_sql_and_values(
        sea_orm::DatabaseBackend::Sqlite,
        "SELECT pm.user_id, pm.group_id, pm.role, u.username, g.name \
         FROM project_membership pm \
         LEFT JOIN users u ON pm.user_id = u.user_id \
         LEFT JOIN groups g ON pm.group_id = g.group_id \
         WHERE pm.project_name = ? \
         ORDER BY COALESCE(u.username, g.name)",
        [project_name.into()],
    );
    let rows = db.query_all_raw(stmt).await?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let user_id: Option<String> = row.try_get("", "user_id")?;
        let group_id: Option<String> = row.try_get("", "group_id")?;
        let role: String = row.try_get("", "role")?;
        let username: Option<String> = row.try_get("", "username")?;
        let name: Option<String> = row.try_get("", "name")?;
        out.push(if let Some(user_id) = user_id {
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
        });
    }
    Ok(out)
}

/// Re-key every `project_membership` row from `old_name` to
/// `new_name` -- port of `rename_project_handler`'s inline
/// best-effort `UPDATE` (AZ-R13-1). A project rename is registry-
/// primary; a membership-repoint failure here is the caller's own
/// best-effort-and-log concern, not this function's.
pub async fn rename_project_membership_project(
    db: &DatabaseConnection,
    old_name: &str,
    new_name: &str,
) -> Result<(), IdentityError> {
    Entity::update_many()
        .col_expr(
            Column::ProjectName,
            sea_orm::sea_query::Expr::value(new_name),
        )
        .filter(Column::ProjectName.eq(old_name))
        .exec(db)
        .await?;
    Ok(())
}

/// Drop every `project_membership` row for `project_name` -- port of
/// `delete_project_handler`'s inline best-effort `DELETE`.
pub async fn remove_project_membership_by_project(
    db: &DatabaseConnection,
    project_name: &str,
) -> Result<(), IdentityError> {
    Entity::delete_many()
        .filter(Column::ProjectName.eq(project_name))
        .exec(db)
        .await?;
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

    /// A file-backed router DB opened as BOTH a `rusqlite::Connection`
    /// (to seed fixture rows through the still-sync `create_user`/
    /// `group_membership_repository::create_group`) and a sea-orm
    /// `DatabaseConnection` (to exercise the now-converted
    /// `project_membership` functions) -- the same dual-connection
    /// recipe `task_repository`'s own tests use, since an in-memory
    /// `:memory:` DB can't be shared across two separate connection
    /// handles the way a real file can.
    async fn conn_with_sea_orm() -> (tempfile::TempDir, Connection, sea_orm::DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("identity_test.db");
        let c = Connection::open(&path).unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        let db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, c, db)
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

    #[tokio::test]
    async fn create_user_persists_and_is_retrievable() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            Some("alice@example.test"),
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        let row = get_user_by_id(&db, &uid).await.unwrap().unwrap();
        assert_eq!(row.username, "alice");
        assert_eq!(row.email.as_deref(), Some("alice@example.test"));
        assert!(verify_password(
            &row.password_hash.unwrap(),
            "correct horse battery staple"
        ));
        let by_username = get_user_by_username(&db, "alice").await.unwrap().unwrap();
        assert_eq!(by_username.user_id, uid);
    }

    #[tokio::test]
    async fn create_sso_user_persists_a_passwordless_row() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        let uid = create_sso_user(&db, "alice", "proxy:alice", None, false, true, NOW)
            .await
            .unwrap();
        let row = get_user_by_id(&db, &uid).await.unwrap().unwrap();
        assert_eq!(row.username, "alice");
        assert!(row.password_hash.is_none());
        assert_eq!(row.sso_subject.as_deref(), Some("proxy:alice"));
    }

    #[tokio::test]
    async fn create_sso_user_bootstraps_the_first_operator_as_sysadmin() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        let uid = create_sso_user(&db, "alice", "proxy:alice", None, false, true, NOW)
            .await
            .unwrap();
        assert!(
            get_user_by_id(&db, &uid)
                .await
                .unwrap()
                .unwrap()
                .is_sysadmin
        );
    }

    #[tokio::test]
    async fn create_sso_user_rejects_a_duplicate_username() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        create_sso_user(&db, "alice", "proxy:alice", None, false, true, NOW)
            .await
            .unwrap();
        let err = create_sso_user(&db, "alice", "proxy:alice2", None, false, false, NOW)
            .await
            .unwrap_err();
        assert!(matches!(err, IdentityError::UsernameAlreadyExists(u) if u == "alice"));
    }

    #[tokio::test]
    async fn find_user_by_sso_subject_reconciles_to_the_same_row_on_every_call() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        let uid = create_sso_user(&db, "alice", "proxy:alice", None, false, true, NOW)
            .await
            .unwrap();
        let first = find_user_by_sso_subject(&db, "proxy:alice")
            .await
            .unwrap()
            .unwrap();
        let second = find_user_by_sso_subject(&db, "proxy:alice")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(first.user_id, uid);
        assert_eq!(second.user_id, uid);
    }

    #[tokio::test]
    async fn find_user_by_sso_subject_is_none_for_an_unknown_subject() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        assert!(find_user_by_sso_subject(&db, "proxy:nobody")
            .await
            .unwrap()
            .is_none());
    }

    #[tokio::test]
    async fn find_linkable_user_by_email_prefers_a_password_authenticated_row() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        // A legacy passwordless SSO row shares the email...
        create_sso_user(
            &db,
            "alice-sso",
            "proxy:alice-legacy",
            Some("alice@example.test"),
            false,
            true,
            NOW,
        )
        .await
        .unwrap();
        // ...but a real password-authenticated account takes priority.
        let password_uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            Some("alice@example.test"),
            false,
            false,
            &[],
            NOW,
        )
        .await
        .unwrap();
        let linked = find_linkable_user_by_email(&db, "alice@example.test")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(linked.user_id, password_uid);
    }

    #[tokio::test]
    async fn find_linkable_user_by_email_is_case_insensitive() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            Some("Alice@Example.Test"),
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        let linked = find_linkable_user_by_email(&db, "alice@example.test")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(linked.user_id, uid);
    }

    #[tokio::test]
    async fn stamp_sso_subject_if_absent_binds_once_and_never_overwrites() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        stamp_sso_subject_if_absent(&db, &uid, "proxy:alice")
            .await
            .unwrap();
        assert_eq!(
            get_user_by_id(&db, &uid)
                .await
                .unwrap()
                .unwrap()
                .sso_subject
                .as_deref(),
            Some("proxy:alice")
        );
        // A second stamp attempt with a DIFFERENT subject must not
        // overwrite the already-bound one.
        stamp_sso_subject_if_absent(&db, &uid, "proxy:someone-else")
            .await
            .unwrap();
        assert_eq!(
            get_user_by_id(&db, &uid)
                .await
                .unwrap()
                .unwrap()
                .sso_subject
                .as_deref(),
            Some("proxy:alice")
        );
    }

    #[tokio::test]
    async fn upgrade_sso_subject_advances_a_row_matched_by_the_exact_old_key() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        stamp_sso_subject_if_absent(&db, &uid, "oidc:https://idp.example.test:alice-1")
            .await
            .unwrap();

        upgrade_sso_subject(
            &db,
            &uid,
            "oidc:https://idp.example.test:alice-1",
            "oidc:https://idp.example.test:str:alice-1",
        )
        .await
        .unwrap();

        assert_eq!(
            get_user_by_id(&db, &uid)
                .await
                .unwrap()
                .unwrap()
                .sso_subject
                .as_deref(),
            Some("oidc:https://idp.example.test:str:alice-1")
        );
    }

    #[tokio::test]
    async fn upgrade_sso_subject_is_a_noop_when_the_row_has_already_moved_on() {
        // The exact-old-value WHERE guard means a row that concurrently
        // already advanced to a DIFFERENT subject must not be clobbered
        // by a stale caller still holding the old legacy key.
        let (_dir, _c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        stamp_sso_subject_if_absent(&db, &uid, "oidc:https://idp.example.test:already-moved-on")
            .await
            .unwrap();

        upgrade_sso_subject(
            &db,
            &uid,
            "oidc:https://idp.example.test:alice-1",
            "oidc:https://idp.example.test:str:alice-1",
        )
        .await
        .unwrap();

        assert_eq!(
            get_user_by_id(&db, &uid)
                .await
                .unwrap()
                .unwrap()
                .sso_subject
                .as_deref(),
            Some("oidc:https://idp.example.test:already-moved-on")
        );
    }

    #[tokio::test]
    async fn touch_last_login_updates_the_timestamp() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        assert!(get_user_by_id(&db, &uid)
            .await
            .unwrap()
            .unwrap()
            .last_login_at
            .is_none());
        touch_last_login(&db, &uid, "2026-06-01T00:00:00.000+00:00")
            .await
            .unwrap();
        assert_eq!(
            get_user_by_id(&db, &uid)
                .await
                .unwrap()
                .unwrap()
                .last_login_at
                .as_deref(),
            Some("2026-06-01T00:00:00.000+00:00")
        );
    }

    #[tokio::test]
    async fn create_user_sanitizes_username_and_email_on_write_only() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        // U+200B ZERO WIDTH SPACE embedded in both fields.
        let uid = create_user(
            &db,
            "ad\u{200B}min",
            "correct horse battery staple",
            Some("a\u{200B}dmin@example.test"),
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        let row = get_user_by_id(&db, &uid).await.unwrap().unwrap();
        assert_eq!(row.username, "admin");
        assert_eq!(row.email.as_deref(), Some("admin@example.test"));
        // The lookup side does NOT sanitize -- the unsanitized
        // spoofing variant must NOT resolve to the stored "admin" row.
        assert!(get_user_by_username(&db, "ad\u{200B}min")
            .await
            .unwrap()
            .is_none());
        assert!(get_user_by_username(&db, "admin").await.unwrap().is_some());
    }

    #[tokio::test]
    async fn create_user_rejects_a_duplicate_username() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        let err = create_user(
            &db,
            "alice",
            "another password entirely",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap_err();
        assert!(matches!(err, IdentityError::UsernameAlreadyExists(u) if u == "alice"));
    }

    #[tokio::test]
    async fn create_user_bootstraps_the_first_operator_as_sysadmin_with_project_membership() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        let projects = vec!["proj-a".to_string(), "proj-b".to_string()];
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &projects,
            NOW,
        )
        .await
        .unwrap();
        let row = get_user_by_id(&db, &uid).await.unwrap().unwrap();
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

    #[tokio::test]
    async fn create_user_does_not_bootstrap_a_second_user() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        let uid2 = create_user(
            &db,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &["proj-a".to_string()],
            NOW,
        )
        .await
        .unwrap();
        let row2 = get_user_by_id(&db, &uid2).await.unwrap().unwrap();
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

    #[tokio::test]
    async fn create_user_sso_opt_out_skips_sysadmin_promotion_but_still_creates_the_first_user() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            false,
            &[],
            NOW,
        )
        .await
        .unwrap();
        let row = get_user_by_id(&db, &uid).await.unwrap().unwrap();
        assert!(
            !row.is_sysadmin,
            "bootstrap_sysadmin=false must not crown the first user"
        );
    }

    #[tokio::test]
    async fn session_lifecycle_create_get_slides_last_used_and_delete() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
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

    #[tokio::test]
    async fn get_session_refuses_an_expired_session_without_deleting_it() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
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

    #[tokio::test]
    async fn prune_expired_sessions_removes_only_past_expiry_rows() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
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

    #[tokio::test]
    async fn add_project_membership_grants_and_is_idempotent() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        add_project_membership(&db, &uid, "proj-a").await.unwrap();
        add_project_membership(&db, &uid, "proj-a").await.unwrap(); // idempotent, no error
        assert_eq!(
            membership_projects_for(&c, &uid),
            vec!["proj-a".to_string()]
        );
    }

    #[tokio::test]
    async fn add_project_membership_defaults_to_the_operator_role() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        add_project_membership(&db, &uid, "proj-a").await.unwrap();
        let role: String = c
            .query_row(
                "SELECT role FROM project_membership WHERE user_id = ?1 AND project_name = ?2",
                (&uid, "proj-a"),
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(role, "operator");
    }

    #[tokio::test]
    async fn rename_project_membership_project_rekeys_every_matching_row() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        add_project_membership(&db, &uid, "old-name").await.unwrap();
        rename_project_membership_project(&db, "old-name", "new-name")
            .await
            .unwrap();
        assert_eq!(
            membership_projects_for(&c, &uid),
            vec!["new-name".to_string()]
        );
    }

    #[tokio::test]
    async fn rename_project_membership_project_rekeys_a_group_grant_too() {
        // AZ-R13-1 (test_sec_r13_project_rename_membership.py, exploit
        // 1): a GROUP-conferred grant must follow the rename exactly
        // like a user grant -- the UPDATE is keyed on project_name
        // alone, but confirm it directly since the Python regression
        // was specifically about group rows being missed by an
        // earlier, narrower fix attempt.
        let (_dir, c, db) = conn_with_sea_orm().await;
        c.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) \
             VALUES ('g-admins', 'Proj Admins', 0, ?1)",
            [NOW],
        )
        .unwrap();
        grant_project_membership(&db, "old-name", None, Some("g-admins"), "viewer")
            .await
            .unwrap();
        rename_project_membership_project(&db, "old-name", "new-name")
            .await
            .unwrap();
        assert_eq!(
            project_membership_role(&db, "new-name", None, Some("g-admins"))
                .await
                .unwrap(),
            Some("viewer".to_string()),
            "the group grant must follow the rename"
        );
        assert!(
            project_membership_role(&db, "old-name", None, Some("g-admins"))
                .await
                .unwrap()
                .is_none(),
            "nothing may be left orphaned under the old name"
        );
    }

    #[tokio::test]
    async fn rename_project_membership_project_leaves_an_unrelated_project_untouched() {
        // test_sec_r13_project_rename_membership.py's
        // `test_rename_leaves_unrelated_projects_membership_untouched`:
        // renaming one project's membership rows must not touch a
        // different project's rows, even for the SAME user.
        let (_dir, c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "carol",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        add_project_membership(&db, &uid, "moving").await.unwrap();
        add_project_membership(&db, &uid, "bystander")
            .await
            .unwrap();
        rename_project_membership_project(&db, "moving", "moved")
            .await
            .unwrap();
        assert_eq!(
            membership_projects_for(&c, &uid),
            vec!["bystander".to_string(), "moved".to_string()]
        );
    }

    #[tokio::test]
    async fn remove_project_membership_by_project_drops_every_matching_row() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        add_project_membership(&db, &uid, "proj-a").await.unwrap();
        add_project_membership(&db, &uid, "proj-b").await.unwrap();
        remove_project_membership_by_project(&db, "proj-a")
            .await
            .unwrap();
        assert_eq!(
            membership_projects_for(&c, &uid),
            vec!["proj-b".to_string()]
        );
    }

    #[tokio::test]
    async fn remove_project_membership_by_project_on_an_unknown_project_is_a_noop() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        remove_project_membership_by_project(&db, "never-existed")
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn grant_project_membership_grants_a_user_an_explicit_role() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        grant_project_membership(&db, "proj-a", Some(&uid), None, "viewer")
            .await
            .unwrap();
        let role: String = c
            .query_row(
                "SELECT role FROM project_membership WHERE project_name = 'proj-a' AND user_id = ?1",
                [&uid],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(role, "viewer");
    }

    #[tokio::test]
    async fn grant_project_membership_grants_a_group_an_explicit_role() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        c.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) VALUES ('g1', 'g1', 0, ?1)",
            [NOW],
        )
        .unwrap();
        grant_project_membership(&db, "proj-a", None, Some("g1"), "operator")
            .await
            .unwrap();
        let role: String = c
            .query_row(
                "SELECT role FROM project_membership WHERE project_name = 'proj-a' AND group_id = 'g1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(role, "operator");
    }

    #[tokio::test]
    async fn grant_project_membership_rejects_a_duplicate() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        grant_project_membership(&db, "proj-a", Some(&uid), None, "operator")
            .await
            .unwrap();
        let err = grant_project_membership(&db, "proj-a", Some(&uid), None, "operator")
            .await
            .unwrap_err();
        assert!(matches!(err, IdentityError::SeaOrm(_)));
    }

    #[tokio::test]
    async fn project_membership_role_reads_back_the_granted_role() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        grant_project_membership(&db, "proj-a", Some(&uid), None, "viewer")
            .await
            .unwrap();
        let role = project_membership_role(&db, "proj-a", Some(&uid), None)
            .await
            .unwrap();
        assert_eq!(role.as_deref(), Some("viewer"));
    }

    #[tokio::test]
    async fn project_membership_role_is_none_for_a_missing_row() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        assert!(project_membership_role(&db, "proj-a", Some("nobody"), None)
            .await
            .unwrap()
            .is_none());
    }

    #[tokio::test]
    async fn update_project_membership_role_changes_an_existing_grant() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        grant_project_membership(&db, "proj-a", Some(&uid), None, "viewer")
            .await
            .unwrap();
        update_project_membership_role(&db, "proj-a", Some(&uid), None, "operator")
            .await
            .unwrap();
        let role = project_membership_role(&db, "proj-a", Some(&uid), None)
            .await
            .unwrap();
        assert_eq!(role.as_deref(), Some("operator"));
    }

    #[tokio::test]
    async fn update_project_membership_role_on_a_missing_row_is_a_noop() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        update_project_membership_role(&db, "proj-a", Some("nobody"), None, "operator")
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn remove_project_membership_deletes_a_user_row_and_reports_whether_one_existed() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        grant_project_membership(&db, "proj-a", Some(&uid), None, "operator")
            .await
            .unwrap();
        assert!(remove_project_membership(&db, "proj-a", Some(&uid), None)
            .await
            .unwrap());
        assert!(project_membership_role(&db, "proj-a", Some(&uid), None)
            .await
            .unwrap()
            .is_none());
        assert!(!remove_project_membership(&db, "proj-a", Some(&uid), None)
            .await
            .unwrap());
    }

    #[tokio::test]
    async fn remove_project_membership_deletes_a_group_row() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        let group_id =
            conexus_db::group_membership_repository::create_group(&c, "engineers", false, NOW)
                .unwrap()
                .group_id;
        grant_project_membership(&db, "proj-a", None, Some(&group_id), "viewer")
            .await
            .unwrap();
        assert!(
            remove_project_membership(&db, "proj-a", None, Some(&group_id))
                .await
                .unwrap()
        );
    }

    #[tokio::test]
    async fn list_project_memberships_projects_both_kinds() {
        let (_dir, c, db) = conn_with_sea_orm().await;
        let uid = create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        let group_id =
            conexus_db::group_membership_repository::create_group(&c, "engineers", false, NOW)
                .unwrap()
                .group_id;
        grant_project_membership(&db, "proj-a", Some(&uid), None, "operator")
            .await
            .unwrap();
        grant_project_membership(&db, "proj-a", None, Some(&group_id), "viewer")
            .await
            .unwrap();
        let rows = list_project_memberships(&db, "proj-a").await.unwrap();
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

    #[tokio::test]
    async fn list_project_memberships_is_empty_for_an_unmembered_project() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        assert!(list_project_memberships(&db, "proj-a")
            .await
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn users_table_is_empty_reflects_real_state() {
        let (_dir, _c, db) = conn_with_sea_orm().await;
        assert!(users_table_is_empty(&db).await.unwrap());
        create_user(
            &db,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        assert!(!users_table_is_empty(&db).await.unwrap());
    }

    /// Proves the `BEGIN IMMEDIATE` locking documented on
    /// [`create_user_row`] for real, not just by inspection: two
    /// genuinely racing OS-level SQLite connections (an in-memory
    /// `:memory:` connection can't be shared across threads, so this
    /// needs a real tempfile-backed DB) both call `create_user(...,
    /// bootstrap_sysadmin: true)` against an initially-empty `users`
    /// table at the same instant. Without `SqliteTransactionMode::
    /// Immediate`, SQLite's default deferred mode lets both connections
    /// read `was_empty=true` before either commits, crowning BOTH
    /// callers sysadmin -- the exact historical dual-sysadmin race this
    /// module's own doc names. Each racing caller is a genuine
    /// `std::thread::spawn` OS thread (not merely a tokio task, which
    /// COULD be scheduled onto the same worker thread even under a
    /// multi-thread runtime) running its own minimal single-threaded
    /// tokio runtime to drive the async `create_user` call -- the same
    /// "real OS thread per racing connection" shape this test used
    /// pre-sea-orm. Repeated 20x (a race is a timing-dependent bug, one
    /// clean run proves nothing) to make a flake in either direction
    /// visible.
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
                let rt = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .unwrap();
                rt.block_on(async move {
                    // sqlx-sqlite's own default `busy_timeout` is
                    // already 5s (`SqliteConnectOptions::default()`),
                    // matching this test's pre-sea-orm explicit
                    // `conn.busy_timeout(Duration::from_secs(5))` call
                    // -- no extra config needed to get the same grace
                    // period for the loser to acquire the write lock.
                    let db =
                        sea_orm::Database::connect(format!("sqlite://{}", db_path_a.display()))
                            .await
                            .unwrap();
                    barrier_a.wait();
                    create_user(
                        &db,
                        "alice",
                        "correct horse battery staple",
                        None,
                        false,
                        true,
                        &[],
                        NOW,
                    )
                    .await
                })
            });
            let handle_b = std::thread::spawn(move || {
                let rt = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .unwrap();
                rt.block_on(async move {
                    let db =
                        sea_orm::Database::connect(format!("sqlite://{}", db_path_b.display()))
                            .await
                            .unwrap();
                    barrier_b.wait();
                    create_user(
                        &db,
                        "bob",
                        "correct horse battery staple",
                        None,
                        false,
                        true,
                        &[],
                        NOW,
                    )
                    .await
                })
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
