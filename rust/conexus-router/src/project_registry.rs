//! Port of `agent_mcp/router/project_registry.py` (892 LOC, Phase E2
//! PR 5, `conexus-router-project-registry`) -- the locking JSON store
//! backing `projects.local.json`, the file both the router and
//! `agent-mcp-launcher` read on every request/boot.
//!
//! **Locking strategy**: `flock` (via the `fd-lock` crate) on a
//! SIDECAR lockfile (`<path>.lock`), never the registry file itself
//! -- ported verbatim from Python's own rationale: the write path
//! publishes atomically via a same-directory temp-file rename, so
//! locking the real path would race (two writers opening before the
//! first rename each lock a DIFFERENT inode once the rename lands).
//! The sidecar's inode is stable across every writer's rename, so
//! every lock is on the same kernel lock object. Reads take a shared
//! lock (`RwLock::read`), writes take an exclusive one
//! (`RwLock::write`) held across the whole read-modify-write cycle --
//! [`ProjectRegistry::with_write_lock`] is the one seam every mutating
//! method funnels through, matching Python's `_WriteCtx`.
//!
//! **`now` is always an explicit `DateTime<Utc>` parameter**, never a
//! live clock read inside a mutating/comparison path -- this crate's
//! own convention (see `identity.rs`), and a genuine improvement over
//! Python's `datetime.now(timezone.utc)` calls scattered through
//! `register`/`add_alias`/`resolve_alias`/`rename`: a typed value is
//! used for BOTH formatting new `expires_at` stamps and comparing
//! existing ones, sidestepping the string-reparse Python's own design
//! doesn't need to worry about but a literal string-threading port
//! would have reintroduced. The ONE exception, matching this crate's
//! own established precedent for genuinely rare/non-business-logic
//! internal timestamps, is [`ProjectRegistry::handle_corrupt`]'s
//! backup-filename suffix -- a real wall-clock read, justified inline
//! at that one call site.
//!
//! **Two on-disk shapes accepted on read** (legacy flat string,
//! nested object) are both handled by [`coerce_to_record`]/
//! [`materialise`]; every write normalises every entry to the nested
//! shape via [`coerce_to_record`], matching Python's
//! `_normalise_for_write`.

// No caller yet -- main.rs's `--projects-file` flag stays accepted-
// but-unused until PR6 (orchestrator) wires a live consumer, same
// helpers-ahead-of-their-first-consumer precedent as mount.rs/
// path_policy.rs/identity.rs.
#![allow(dead_code)]

use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::Write as _;
use std::path::{Path, PathBuf};

use std::sync::LazyLock;

use chrono::{DateTime, Duration, Utc};
use regex::Regex;
use serde_json::{json, Map, Value};

use conexus_db::scheduled_directive_repository::parse_flexible;

/// Default grace period for [`ProjectRegistry::add_alias`] when the
/// caller passes neither `expires_at` nor `grace_days`.
pub const DEFAULT_ALIAS_GRACE_DAYS: i64 = 30;

/// Every project defaults to this (today's only Python implementation)
/// until a canary cutover explicitly flips it.
pub const DEFAULT_BACKEND_IMPL: &str = "python";
const VALID_BACKEND_IMPLS: [&str; 2] = ["python", "rust"];

/// Kept in sync with `path_policy.rs`'s own project-name-segment
/// extraction and the router-side `_SLUG_RE` in `agent_mcp/router/
/// app.py`; duplicated (not shared) the same way Python's own copy in
/// this module is duplicated to avoid a circular import. Uses the
/// `regex` crate directly (a real dependency, not a hand-rolled
/// byte-scanner) -- this workspace's own established precedent, set
/// explicitly during Phase B's `AgentRepository` port after an early
/// draft hand-rolled agent-id validation instead of leaning on the
/// well-maintained library already in the dependency tree.
static SLUG_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z](?:[a-z0-9-]*[a-z0-9])?$").unwrap());

fn is_valid_slug(s: &str) -> bool {
    SLUG_RE.is_match(s)
}

/// One alias entry -- a name that resolves to the parent project until
/// `expires_at` passes.
#[derive(Debug, Clone, PartialEq)]
pub struct Alias {
    pub name: String,
    pub expires_at: String,
}

/// One materialised project record -- always carries `name`,
/// `workspace`, `aliases`, and `backend_impl`, regardless of which
/// on-disk shape (legacy flat string vs. nested object) produced it.
/// Opaque extra fields on a nested-shape on-disk record are NOT
/// exposed here (no current Rust consumer needs them) but ARE
/// preserved byte-for-byte through any read-modify-write cycle --
/// every mutating method operates on the raw [`serde_json::Value`]
/// map, never round-tripping through this struct.
#[derive(Debug, Clone, PartialEq)]
pub struct ProjectRow {
    pub name: String,
    pub workspace: String,
    pub aliases: Vec<Alias>,
    pub backend_impl: String,
}

/// Port of Python's `RegistryError` hierarchy
/// (`UnknownProject`/`InvalidName`/`ProjectNameTaken`/`AliasCollision`),
/// collapsed into one closed enum -- this migration's own precedent
/// over Python's exception-subclass ladder (see `IdentityError`,
/// `GroupMembershipError`). `InvalidArgument` covers the one bare
/// `ValueError` Python itself doesn't bother subclassing (a
/// `backend_impl` value outside `VALID_BACKEND_IMPLS`).
#[derive(Debug)]
pub enum RegistryError {
    /// The named project isn't registered.
    UnknownProject(String),
    /// A project/alias name failed slug validation.
    InvalidName(String),
    /// `backend_impl` isn't a recognized value.
    InvalidArgument(String),
    /// The target name is already a REGISTERED project.
    ProjectNameTaken(String),
    /// The target name is a currently-ACTIVE alias of another project.
    AliasCollision(String),
    Io(std::io::Error),
    Json(serde_json::Error),
}

impl From<std::io::Error> for RegistryError {
    fn from(e: std::io::Error) -> Self {
        RegistryError::Io(e)
    }
}

impl From<serde_json::Error> for RegistryError {
    fn from(e: serde_json::Error) -> Self {
        RegistryError::Json(e)
    }
}

impl std::fmt::Display for RegistryError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RegistryError::UnknownProject(name) => write!(f, "unknown project {name:?}"),
            RegistryError::InvalidName(msg) => write!(f, "{msg}"),
            RegistryError::InvalidArgument(msg) => write!(f, "{msg}"),
            RegistryError::ProjectNameTaken(msg) => write!(f, "{msg}"),
            RegistryError::AliasCollision(msg) => write!(f, "{msg}"),
            RegistryError::Io(e) => write!(f, "registry I/O error: {e}"),
            RegistryError::Json(e) => write!(f, "registry JSON error: {e}"),
        }
    }
}

impl std::error::Error for RegistryError {}

fn validate_backend_impl(v: &str) -> Result<(), RegistryError> {
    if VALID_BACKEND_IMPLS.contains(&v) {
        Ok(())
    } else {
        Err(RegistryError::InvalidArgument(format!(
            "backend_impl must be one of {VALID_BACKEND_IMPLS:?}, got {v:?}"
        )))
    }
}

fn validate_slug(name: &str, field_label: &str) -> Result<(), RegistryError> {
    if is_valid_slug(name) {
        Ok(())
    } else {
        Err(RegistryError::InvalidName(format!(
            "{field_label} {name:?} is not a valid slug -- must start with a lowercase letter and contain only lowercase letters, digits, and hyphens"
        )))
    }
}

/// `AGENT_MCP_PROJECTS_FILE` env var if set, else
/// `<HOME>/.config/agent-mcp/projects.local.json`. `get_env` is an
/// explicit lookup (not a direct `std::env::var` read) matching the
/// Phase D2 RAG-clients convention -- sidesteps `cargo test`'s
/// parallel-thread env-var-race hazard, the same bug class already
/// hit twice in this workspace.
pub fn default_registry_path(get_env: impl Fn(&str) -> Option<String>) -> PathBuf {
    if let Some(p) = get_env("AGENT_MCP_PROJECTS_FILE") {
        return PathBuf::from(p);
    }
    let home = get_env("HOME").unwrap_or_else(|| "/".to_string());
    Path::new(&home)
        .join(".config")
        .join("agent-mcp")
        .join("projects.local.json")
}

fn stamp(now: DateTime<Utc>) -> String {
    now.format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

/// Alias entries for DISPLAY (`list`/`get`) -- keeps every dict entry,
/// defaulting a missing `name`/`expires_at` to `""` (matches Python's
/// own `a.get("name", "")` rendering in `_materialise`, never
/// dropping a row the operator might need to see and manually fix).
fn display_aliases(payload: &Value) -> Vec<Alias> {
    let Some(arr) = payload.get("aliases").and_then(Value::as_array) else {
        return vec![];
    };
    arr.iter()
        .filter_map(Value::as_object)
        .map(|a| Alias {
            name: a
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            expires_at: a
                .get("expires_at")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        })
        .collect()
}

/// Alias entries for SCANNING (collision checks, `resolve_alias`) --
/// SKIPS an entry missing a valid `expires_at` string, matching every
/// Python collision-scan call site's own `entry["expires_at"]`
/// `KeyError`-catching try/except (a malformed entry can never win a
/// collision, but it's not silently coerced into one either).
fn scannable_aliases(payload: &Value) -> Vec<Alias> {
    let Some(arr) = payload.get("aliases").and_then(Value::as_array) else {
        return vec![];
    };
    arr.iter()
        .filter_map(|a| {
            let obj = a.as_object()?;
            Some(Alias {
                name: obj.get("name").and_then(Value::as_str)?.to_string(),
                expires_at: obj.get("expires_at").and_then(Value::as_str)?.to_string(),
            })
        })
        .collect()
}

/// Return a mutable nested-shape record for `payload` -- accepts
/// either a legacy string payload or an existing nested object;
/// returns a copy with `workspace`/`aliases` guaranteed present,
/// preserving every OTHER key verbatim (the opaque `extra` passthrough
/// -- a read-modify-write on one field must never drop another).
fn coerce_to_record(payload: &Value) -> Map<String, Value> {
    match payload {
        Value::String(s) => {
            let mut m = Map::new();
            m.insert("workspace".into(), Value::String(s.clone()));
            m.insert("aliases".into(), Value::Array(vec![]));
            m
        }
        Value::Object(obj) => {
            let mut out = obj.clone();
            let workspace = out
                .get("workspace")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let aliases = out
                .get("aliases")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            out.remove("workspace");
            out.remove("aliases");
            out.insert("workspace".into(), Value::String(workspace));
            out.insert("aliases".into(), Value::Array(aliases));
            out
        }
        _ => {
            let mut m = Map::new();
            m.insert("workspace".into(), Value::String(String::new()));
            m.insert("aliases".into(), Value::Array(vec![]));
            m
        }
    }
}

/// Coerce a raw row into the [`ProjectRow`] shape. Accepts both the
/// nested shape and the legacy flat-string shape; always populates
/// `backend_impl` (defaulting to [`DEFAULT_BACKEND_IMPL`] when absent
/// from the stored payload), same "read synthesises the default, a
/// write upgrades the shape it touches" idiom the legacy-shape upgrade
/// itself uses.
fn materialise(name: &str, payload: &Value) -> ProjectRow {
    match payload {
        Value::String(s) => ProjectRow {
            name: name.to_string(),
            workspace: s.clone(),
            aliases: vec![],
            backend_impl: DEFAULT_BACKEND_IMPL.to_string(),
        },
        Value::Object(_) => ProjectRow {
            name: name.to_string(),
            workspace: payload
                .get("workspace")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            aliases: display_aliases(payload),
            backend_impl: payload
                .get("backend_impl")
                .and_then(Value::as_str)
                .unwrap_or(DEFAULT_BACKEND_IMPL)
                .to_string(),
        },
        // Defensive: unexpected payload type -> treat as empty
        // workspace, matching Python's own comment ("the registry is
        // read on hot paths and we'd rather the operator see a
        // broken-looking row than a 500").
        _ => ProjectRow {
            name: name.to_string(),
            workspace: String::new(),
            aliases: vec![],
            backend_impl: DEFAULT_BACKEND_IMPL.to_string(),
        },
    }
}

/// Whether an outcome from [`ProjectRegistry::with_write_lock`]'s
/// closure should be persisted to disk -- `register`'s idempotent
/// re-register path must return the existing row WITHOUT rewriting
/// the file (Python's own explicit choice: "a router that
/// boots-and-idles" must not mutate the operator's file out from
/// under them), so the closure decides per-call, not the wrapper.
enum WriteOutcome<R> {
    Persist(R),
    Skip(R),
}

/// Thread- and process-safe accessor for the projects JSON file.
/// Cheap to construct (no I/O in `new`); each method opens the
/// sidecar lockfile, takes the appropriate lock, does its work, and
/// releases -- fine to construct fresh per call, matching Python's
/// own "no cached instance" design.
pub struct ProjectRegistry {
    path: PathBuf,
}

impl ProjectRegistry {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn lock_path(&self) -> PathBuf {
        let mut name = self
            .path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        name.push_str(".lock");
        self.path.with_file_name(name)
    }

    fn open_lock_file(&self) -> std::io::Result<File> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            // Must NOT truncate -- this is the sidecar LOCK file, not
            // the registry data; matches `os.open(..., O_RDWR |
            // O_CREAT, 0o644)`'s own lack of O_TRUNC.
            .truncate(false)
            .open(self.lock_path())
    }

    fn read_and_parse(&self) -> Result<Map<String, Value>, RegistryError> {
        if !self.path.is_file() {
            return Ok(Map::new());
        }
        let raw = match fs::read(&self.path) {
            Ok(bytes) => bytes,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Map::new()),
            Err(e) => return Err(e.into()),
        };
        Ok(self.parse_or_recover(&raw))
    }

    fn parse_or_recover(&self, raw: &[u8]) -> Map<String, Value> {
        if raw.iter().all(u8::is_ascii_whitespace) {
            return Map::new();
        }
        match serde_json::from_slice::<Value>(raw) {
            Ok(Value::Object(map)) => map,
            Ok(other) => {
                let type_name = match other {
                    Value::Null => "null",
                    Value::Bool(_) => "bool",
                    Value::Number(_) => "number",
                    Value::String(_) => "string",
                    Value::Array(_) => "array",
                    Value::Object(_) => unreachable!(),
                };
                self.handle_corrupt(&format!(
                    "top-level JSON is {type_name}, expected an object"
                ));
                Map::new()
            }
            Err(e) => {
                self.handle_corrupt(&e.to_string());
                Map::new()
            }
        }
    }

    /// Rename the corrupt file to a timestamped backup, loud-warn.
    /// Uses a REAL wall-clock read (the one deliberate exception to
    /// this module's "explicit `now` parameter" rule -- see the
    /// module doc): this only fires on a genuinely exceptional,
    /// off-hot-path event, and the timestamp is purely for filename
    /// disambiguation, not a business decision under test.
    fn handle_corrupt(&self, reason: &str) {
        if !self.path.exists() {
            return;
        }
        let ts = Utc::now().format("%Y-%m-%dT%H-%M-%S").to_string();
        let file_name = self
            .path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        let backup = self
            .path
            .with_file_name(format!("{file_name}.corrupt-{ts}"));
        match fs::rename(&self.path, &backup) {
            Ok(()) => {
                eprintln!(
                    "project registry: {} was unparseable ({reason}); moved to {} and starting fresh. Hand-merge entries back in if you can salvage.",
                    self.path.display(),
                    backup.display()
                );
            }
            Err(e) => {
                eprintln!(
                    "project registry: could not back up corrupt {}: {e}; leaving original in place. Reason for recovery: {reason}",
                    self.path.display()
                );
            }
        }
    }

    fn persist(&self, data: &Map<String, Value>) -> Result<(), RegistryError> {
        let mut normalised: BTreeMap<String, Value> = BTreeMap::new();
        for (name, payload) in data {
            normalised.insert(name.clone(), Value::Object(coerce_to_record(payload)));
        }
        // `serde_json::Map` is BTreeMap-backed workspace-wide (no
        // crate here enables the `preserve_order` feature), so
        // serializing through a `Value::Object` built from a
        // `BTreeMap` sorts every key alphabetically -- the exact
        // `sort_keys=True` guarantee Python's `json.dumps` states
        // explicitly, for free.
        let value = serde_json::to_value(normalised)?;
        let mut bytes = serde_json::to_vec_pretty(&value)?;
        bytes.push(b'\n');

        let dir = self
            .path
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
        fs::create_dir_all(&dir)?;
        let file_name = self
            .path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        let mut tmp = tempfile::Builder::new()
            .prefix(&format!("{file_name}."))
            .suffix(".tmp")
            .tempfile_in(&dir)?;
        tmp.write_all(&bytes)?;
        tmp.as_file().sync_all()?;
        tmp.persist(&self.path)
            .map_err(|e| RegistryError::Io(e.error))?;
        Ok(())
    }

    /// Read the registry under a shared (`LOCK_SH`) lock.
    fn read_locked(&self) -> Result<Map<String, Value>, RegistryError> {
        let lock_file = self.open_lock_file()?;
        let rw = fd_lock::RwLock::new(lock_file);
        let _guard = rw.read()?;
        self.read_and_parse()
    }

    /// Take an exclusive (`LOCK_EX`) lock, read the registry fresh,
    /// hand it to `f`, persist iff `f` says to -- THE one seam every
    /// mutating method funnels through, matching Python's `_WriteCtx`.
    fn with_write_lock<R>(
        &self,
        f: impl FnOnce(&mut Map<String, Value>) -> Result<WriteOutcome<R>, RegistryError>,
    ) -> Result<R, RegistryError> {
        let lock_file = self.open_lock_file()?;
        let mut rw = fd_lock::RwLock::new(lock_file);
        let _guard = rw.write()?;
        let mut data = self.read_and_parse()?;
        match f(&mut data)? {
            WriteOutcome::Persist(r) => {
                self.persist(&data)?;
                Ok(r)
            }
            WriteOutcome::Skip(r) => Ok(r),
        }
    }

    // ── Public API ─────────────────────────────────────────────────

    /// Every registered project, sorted by name.
    pub fn list(&self) -> Result<Vec<ProjectRow>, RegistryError> {
        let data = self.read_locked()?;
        let mut rows: Vec<ProjectRow> = data.iter().map(|(name, v)| materialise(name, v)).collect();
        rows.sort_by(|a, b| a.name.cmp(&b.name));
        Ok(rows)
    }

    /// The project record for `name`, or `None` if unknown.
    pub fn get(&self, name: &str) -> Result<Option<ProjectRow>, RegistryError> {
        let data = self.read_locked()?;
        Ok(data.get(name).map(|v| materialise(name, v)))
    }

    /// Register (or re-confirm) a project. Idempotent: re-registering
    /// with the SAME `workspace` returns the existing row untouched
    /// (including `backend_impl` -- flipping that is
    /// [`Self::set_backend_impl`]'s job, not this method's). Re-
    /// registering with a DIFFERENT workspace is
    /// [`RegistryError::ProjectNameTaken`].
    pub fn register(
        &self,
        name: &str,
        workspace: &str,
        backend_impl: &str,
        now: DateTime<Utc>,
    ) -> Result<ProjectRow, RegistryError> {
        validate_backend_impl(backend_impl)?;
        self.with_write_lock(|data| {
            if let Some(existing) = data.get(name) {
                let existing_row = materialise(name, existing);
                if existing_row.workspace != workspace {
                    return Err(RegistryError::ProjectNameTaken(format!(
                        "project {name:?} is already registered at {:?}; refusing to re-point at {workspace:?}",
                        existing_row.workspace
                    )));
                }
                return Ok(WriteOutcome::Skip(existing_row));
            }

            // BL-R33-1: refuse to claim a name that is a currently-
            // active grace-period alias of ANOTHER project.
            for (other_name, payload) in data.iter() {
                for entry in scannable_aliases(payload) {
                    if entry.name != name {
                        continue;
                    }
                    if let Ok(exp) = parse_flexible(&entry.expires_at) {
                        if exp > now {
                            return Err(RegistryError::AliasCollision(format!(
                                "name {name:?} is already an active alias for project {other_name:?}"
                            )));
                        }
                    }
                }
            }

            let mut record = Map::new();
            record.insert("workspace".into(), Value::String(workspace.to_string()));
            record.insert("aliases".into(), Value::Array(vec![]));
            record.insert(
                "backend_impl".into(),
                Value::String(backend_impl.to_string()),
            );
            data.insert(name.to_string(), Value::Object(record));
            let row = materialise(name, data.get(name).unwrap());
            Ok(WriteOutcome::Persist(row))
        })
    }

    /// Drop `name` from the registry. No-op if not present.
    pub fn unregister(&self, name: &str) -> Result<(), RegistryError> {
        self.with_write_lock(|data| {
            if !data.contains_key(name) {
                return Ok(WriteOutcome::Skip(()));
            }
            data.remove(name);
            Ok(WriteOutcome::Persist(()))
        })
    }

    /// Flip an EXISTING project's `backend_impl` -- the Phase D1
    /// canary-cutover primitive. Deliberately separate from
    /// [`Self::register`] (same "register() is create/reconfirm,
    /// mutation gets its own method" split as `add_alias`/`rename`).
    /// Does NOT touch systemd/sockets -- only rewrites the registry
    /// file; that's the caller's job.
    pub fn set_backend_impl(
        &self,
        name: &str,
        backend_impl: &str,
    ) -> Result<ProjectRow, RegistryError> {
        validate_backend_impl(backend_impl)?;
        self.with_write_lock(|data| {
            let Some(existing) = data.get(name) else {
                return Err(RegistryError::UnknownProject(name.to_string()));
            };
            let mut record = coerce_to_record(existing);
            record.insert(
                "backend_impl".into(),
                Value::String(backend_impl.to_string()),
            );
            data.insert(name.to_string(), Value::Object(record));
            let row = materialise(name, data.get(name).unwrap());
            Ok(WriteOutcome::Persist(row))
        })
    }

    /// Append `alias` to `name`'s alias list. `grace_days` (if given)
    /// overrides `expires_at` outright, matching Python's own
    /// precedence (`grace_days` mirrors `rename`'s knob so a caller
    /// can express "dead on arrival" via `grace_days: Some(0)`);
    /// otherwise `expires_at` is used if given, else the default
    /// [`DEFAULT_ALIAS_GRACE_DAYS`]-day grace period.
    pub fn add_alias(
        &self,
        name: &str,
        alias: &str,
        expires_at: Option<DateTime<Utc>>,
        grace_days: Option<i64>,
        now: DateTime<Utc>,
    ) -> Result<(), RegistryError> {
        validate_slug(alias, "alias")?;

        let expires_at = if let Some(days) = grace_days {
            now + Duration::days(days)
        } else if let Some(exp) = expires_at {
            exp
        } else {
            now + Duration::days(DEFAULT_ALIAS_GRACE_DAYS)
        };
        let already_expired = expires_at <= now;

        self.with_write_lock(|data| {
            if !data.contains_key(name) {
                return Err(RegistryError::UnknownProject(name.to_string()));
            }
            if data.contains_key(alias) && !already_expired {
                return Err(RegistryError::ProjectNameTaken(format!(
                    "alias {alias:?} collides with a real project name"
                )));
            }

            for (other_name, payload) in data.iter() {
                if other_name == name {
                    continue;
                }
                for entry in scannable_aliases(payload) {
                    if entry.name != alias {
                        continue;
                    }
                    if let Ok(exp) = parse_flexible(&entry.expires_at) {
                        if exp > now {
                            return Err(RegistryError::AliasCollision(format!(
                                "alias {alias:?} is already an active alias for project {other_name:?}"
                            )));
                        }
                    }
                }
            }

            let mut record = coerce_to_record(data.get(name).unwrap());
            let aliases = record
                .get_mut("aliases")
                .and_then(Value::as_array_mut)
                .expect("coerce_to_record always sets an array");
            aliases.push(json!({"name": alias, "expires_at": stamp(expires_at)}));
            data.insert(name.to_string(), Value::Object(record));
            Ok(WriteOutcome::Persist(()))
        })
    }

    /// Remove `alias` from `name`'s alias list. No-op if `name` or
    /// the alias entry is absent.
    pub fn expire_alias(&self, name: &str, alias: &str) -> Result<(), RegistryError> {
        self.with_write_lock(|data| {
            let Some(existing) = data.get(name) else {
                return Ok(WriteOutcome::Skip(()));
            };
            let mut record = coerce_to_record(existing);
            if let Some(Value::Array(aliases)) = record.get_mut("aliases") {
                aliases.retain(|a| a.get("name").and_then(Value::as_str) != Some(alias));
            }
            data.insert(name.to_string(), Value::Object(record));
            Ok(WriteOutcome::Persist(()))
        })
    }

    /// If `maybe_alias` matches a non-expired alias of some project,
    /// return that project's real name. `O(N)` over registered
    /// projects, matching Python's own documented cost tradeoff (`N`
    /// is small; the read happens behind the router's already-existing
    /// per-request snapshot).
    pub fn resolve_alias(
        &self,
        maybe_alias: &str,
        now: DateTime<Utc>,
    ) -> Result<Option<String>, RegistryError> {
        Ok(self
            .resolve_alias_entry(maybe_alias, now)?
            .map(|(real_name, _expires_at)| real_name))
    }

    /// Same as [`Self::resolve_alias`], but also returns the alias
    /// entry's `expires_at` -- `proxy_core.rs` (Phase E2 PR 8) needs
    /// both halves to reconstruct Python's `alias_info` tuple
    /// (`"<alias_name>,<expires_at>"`, the `X-Agent-MCP-Alias` header
    /// value), which `resolve_alias` alone can't provide.
    pub fn resolve_alias_entry(
        &self,
        maybe_alias: &str,
        now: DateTime<Utc>,
    ) -> Result<Option<(String, String)>, RegistryError> {
        let data = self.read_locked()?;
        for (real_name, payload) in data.iter() {
            for entry in scannable_aliases(payload) {
                if entry.name != maybe_alias {
                    continue;
                }
                if let Ok(exp) = parse_flexible(&entry.expires_at) {
                    if exp > now {
                        return Ok(Some((real_name.clone(), entry.expires_at.clone())));
                    }
                }
            }
        }
        Ok(None)
    }

    /// Atomically rename `old_name` to `new_name`, parking `old_name`
    /// as a grace-period alias on the new record. Does NOT move the
    /// workspace directory on disk nor restart any systemd unit --
    /// that's the caller's job.
    ///
    /// R9-F5: DOES keep the record's `workspace` field tracking the
    /// rename whenever it still follows the `<parent>/<name>` naming
    /// convention every project created via [`Self::register`]
    /// satisfies (`Path(workspace).file_name() == old_name`) -- the
    /// same check the calling endpoint uses to decide whether to
    /// physically move the directory. Composes correctly across an
    /// arbitrary number of renames since each rename re-derives from
    /// the CURRENT value using the same test, rather than freezing at
    /// creation time.
    pub fn rename(
        &self,
        old_name: &str,
        new_name: &str,
        grace_days: i64,
        now: DateTime<Utc>,
    ) -> Result<(), RegistryError> {
        validate_slug(new_name, "new name")?;
        let expires_at = now + Duration::days(grace_days);

        self.with_write_lock(|data| {
            if !data.contains_key(old_name) {
                return Err(RegistryError::UnknownProject(old_name.to_string()));
            }
            if data.contains_key(new_name) {
                return Err(RegistryError::ProjectNameTaken(format!(
                    "project {new_name:?} is already registered"
                )));
            }

            for (other_name, payload) in data.iter() {
                if other_name == old_name {
                    continue;
                }
                for entry in scannable_aliases(payload) {
                    if entry.name != new_name {
                        continue;
                    }
                    if let Ok(exp) = parse_flexible(&entry.expires_at) {
                        if exp > now {
                            return Err(RegistryError::AliasCollision(format!(
                                "name {new_name:?} is already an active alias for project {other_name:?}"
                            )));
                        }
                    }
                }
            }

            let mut record = coerce_to_record(data.get(old_name).unwrap());
            let old_workspace = record
                .get("workspace")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let follows_convention = !old_workspace.is_empty()
                && Path::new(&old_workspace)
                    .file_name()
                    .map(|f| f.to_string_lossy() == old_name)
                    .unwrap_or(false);
            if follows_convention {
                let new_workspace = Path::new(&old_workspace).with_file_name(new_name);
                record.insert(
                    "workspace".into(),
                    Value::String(new_workspace.to_string_lossy().into_owned()),
                );
            }
            let aliases = record
                .get_mut("aliases")
                .and_then(Value::as_array_mut)
                .expect("coerce_to_record always sets an array");
            aliases.push(json!({"name": old_name, "expires_at": stamp(expires_at)}));
            data.insert(new_name.to_string(), Value::Object(record));
            data.remove(old_name);
            Ok(WriteOutcome::Persist(()))
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn registry_at(dir: &tempfile::TempDir) -> ProjectRegistry {
        ProjectRegistry::new(dir.path().join("projects.local.json"))
    }

    fn now() -> DateTime<Utc> {
        "2026-01-01T00:00:00Z".parse().unwrap()
    }

    fn later(days: i64) -> DateTime<Utc> {
        now() + Duration::days(days)
    }

    #[test]
    fn list_on_a_missing_file_is_empty() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        assert_eq!(reg.list().unwrap(), vec![]);
    }

    #[test]
    fn register_creates_and_returns_the_row() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        let row = reg
            .register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        assert_eq!(row.name, "proj-a");
        assert_eq!(row.workspace, "/ws/proj-a");
        assert_eq!(row.backend_impl, "python");
        assert_eq!(reg.get("proj-a").unwrap().unwrap(), row);
    }

    #[test]
    fn register_is_idempotent_on_the_same_workspace_and_preserves_backend_impl() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.set_backend_impl("proj-a", "rust").unwrap();

        let row = reg
            .register("proj-a", "/ws/proj-a", "python", later(1))
            .unwrap();
        assert_eq!(
            row.backend_impl, "rust",
            "a re-register must not reset an already-flipped backend_impl"
        );
    }

    #[test]
    fn register_rejects_repointing_an_existing_project_to_a_new_workspace() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        let err = reg
            .register("proj-a", "/ws/somewhere-else", "python", now())
            .unwrap_err();
        assert!(matches!(err, RegistryError::ProjectNameTaken(_)));
    }

    #[test]
    fn register_rejects_an_invalid_backend_impl() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        let err = reg
            .register("proj-a", "/ws/proj-a", "golang", now())
            .unwrap_err();
        assert!(matches!(err, RegistryError::InvalidArgument(_)));
    }

    #[test]
    fn register_refuses_a_name_that_is_a_currently_active_alias() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.add_alias("proj-a", "old-name", None, None, now())
            .unwrap();

        let err = reg
            .register("old-name", "/ws/whatever", "python", now())
            .unwrap_err();
        assert!(matches!(err, RegistryError::AliasCollision(_)));
    }

    #[test]
    fn register_allows_a_name_that_is_an_expired_alias() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.add_alias("proj-a", "old-name", None, Some(0), now())
            .unwrap();

        // the alias is dead-on-arrival (grace_days: 0) by `later(1)`.
        reg.register("old-name", "/ws/whatever", "python", later(1))
            .unwrap();
    }

    #[test]
    fn unregister_removes_and_is_a_noop_when_missing() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.unregister("proj-a").unwrap();
        assert_eq!(reg.get("proj-a").unwrap(), None);
        reg.unregister("proj-a").unwrap(); // no-op, must not error
    }

    #[test]
    fn set_backend_impl_flips_an_existing_project() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        let row = reg.set_backend_impl("proj-a", "rust").unwrap();
        assert_eq!(row.backend_impl, "rust");
    }

    #[test]
    fn set_backend_impl_on_an_unknown_project_is_unknown_project() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        let err = reg.set_backend_impl("nope", "rust").unwrap_err();
        assert!(matches!(err, RegistryError::UnknownProject(_)));
    }

    #[test]
    fn add_alias_rejects_an_invalid_slug() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        let err = reg
            .add_alias("proj-a", "Not_A_Slug", None, None, now())
            .unwrap_err();
        assert!(matches!(err, RegistryError::InvalidName(_)));
    }

    #[test]
    fn add_alias_on_an_unknown_project_is_unknown_project() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        let err = reg
            .add_alias("nope", "alias-a", None, None, now())
            .unwrap_err();
        assert!(matches!(err, RegistryError::UnknownProject(_)));
    }

    #[test]
    fn add_alias_defaults_to_the_default_grace_period() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.add_alias("proj-a", "alias-a", None, None, now())
            .unwrap();

        let row = reg.get("proj-a").unwrap().unwrap();
        assert_eq!(row.aliases.len(), 1);
        assert_eq!(row.aliases[0].name, "alias-a");
        assert_eq!(
            row.aliases[0].expires_at,
            stamp(later(DEFAULT_ALIAS_GRACE_DAYS))
        );
    }

    #[test]
    fn add_alias_grace_days_overrides_an_explicit_expires_at() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.add_alias("proj-a", "alias-a", Some(later(5)), Some(1), now())
            .unwrap();

        let row = reg.get("proj-a").unwrap().unwrap();
        assert_eq!(row.aliases[0].expires_at, stamp(later(1)));
    }

    #[test]
    fn add_alias_rejects_a_name_colliding_with_a_real_project() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.register("proj-b", "/ws/proj-b", "python", now())
            .unwrap();

        let err = reg
            .add_alias("proj-a", "proj-b", None, None, now())
            .unwrap_err();
        assert!(matches!(err, RegistryError::ProjectNameTaken(_)));
    }

    #[test]
    fn add_alias_allows_a_dead_on_arrival_name_colliding_with_a_real_project() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.register("proj-b", "/ws/proj-b", "python", now())
            .unwrap();

        // grace_days: 0 makes it dead-on-arrival -- allowed even
        // though "proj-b" is a real project name.
        reg.add_alias("proj-a", "proj-b", None, Some(0), now())
            .unwrap();
    }

    #[test]
    fn add_alias_rejects_colliding_with_another_projects_active_alias() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.register("proj-b", "/ws/proj-b", "python", now())
            .unwrap();
        reg.add_alias("proj-a", "shared-alias", None, None, now())
            .unwrap();

        let err = reg
            .add_alias("proj-b", "shared-alias", None, None, now())
            .unwrap_err();
        assert!(matches!(err, RegistryError::AliasCollision(_)));
    }

    #[test]
    fn expire_alias_removes_the_entry_and_is_a_noop_when_absent() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.add_alias("proj-a", "alias-a", None, None, now())
            .unwrap();
        reg.expire_alias("proj-a", "alias-a").unwrap();
        assert_eq!(reg.get("proj-a").unwrap().unwrap().aliases, vec![]);
        reg.expire_alias("proj-a", "alias-a").unwrap(); // no-op
        reg.expire_alias("nope", "alias-a").unwrap(); // no-op, unknown project
    }

    #[test]
    fn resolve_alias_returns_the_real_project_name_for_a_live_alias() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.add_alias("proj-a", "alias-a", None, None, now())
            .unwrap();

        assert_eq!(
            reg.resolve_alias("alias-a", now()).unwrap(),
            Some("proj-a".to_string())
        );
    }

    #[test]
    fn resolve_alias_entry_also_returns_the_expires_at() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.add_alias("proj-a", "alias-a", None, Some(30), now())
            .unwrap();

        let (real_name, expires_at) = reg.resolve_alias_entry("alias-a", now()).unwrap().unwrap();
        assert_eq!(real_name, "proj-a");
        assert_eq!(expires_at, stamp(later(30)));
    }

    #[test]
    fn resolve_alias_is_none_for_an_expired_alias() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.add_alias("proj-a", "alias-a", None, Some(1), now())
            .unwrap();

        assert_eq!(reg.resolve_alias("alias-a", later(2)).unwrap(), None);
    }

    #[test]
    fn resolve_alias_is_none_for_an_unknown_name() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        assert_eq!(reg.resolve_alias("nope", now()).unwrap(), None);
    }

    #[test]
    fn rename_moves_the_record_and_parks_old_name_as_an_alias() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.rename("proj-a", "proj-a-renamed", DEFAULT_ALIAS_GRACE_DAYS, now())
            .unwrap();

        assert_eq!(reg.get("proj-a").unwrap(), None);
        let row = reg.get("proj-a-renamed").unwrap().unwrap();
        assert_eq!(
            row.workspace, "/ws/proj-a-renamed",
            "R9-F5: workspace must track the rename"
        );
        assert_eq!(row.aliases.len(), 1);
        assert_eq!(row.aliases[0].name, "proj-a");

        assert_eq!(
            reg.resolve_alias("proj-a", now()).unwrap(),
            Some("proj-a-renamed".to_string())
        );
    }

    #[test]
    fn rename_leaves_a_non_conventional_workspace_untouched() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/custom/path/not-matching-name", "python", now())
            .unwrap();
        reg.rename("proj-a", "proj-a-renamed", DEFAULT_ALIAS_GRACE_DAYS, now())
            .unwrap();

        let row = reg.get("proj-a-renamed").unwrap().unwrap();
        assert_eq!(row.workspace, "/custom/path/not-matching-name");
    }

    #[test]
    fn rename_composes_across_multiple_renames() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.rename("proj-a", "proj-b", 30, now()).unwrap();
        reg.rename("proj-b", "proj-c", 30, now()).unwrap();

        let row = reg.get("proj-c").unwrap().unwrap();
        assert_eq!(
            row.workspace, "/ws/proj-c",
            "R9-F5: workspace must keep tracking the name across a SECOND rename, not freeze after the first"
        );
        assert_eq!(row.aliases.len(), 2, "both old names become aliases");
    }

    #[test]
    fn rename_rejects_an_unknown_old_name() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        let err = reg.rename("nope", "proj-b", 30, now()).unwrap_err();
        assert!(matches!(err, RegistryError::UnknownProject(_)));
    }

    #[test]
    fn rename_rejects_a_new_name_that_is_already_registered() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        reg.register("proj-b", "/ws/proj-b", "python", now())
            .unwrap();
        let err = reg.rename("proj-a", "proj-b", 30, now()).unwrap_err();
        assert!(matches!(err, RegistryError::ProjectNameTaken(_)));
    }

    #[test]
    fn rename_rejects_an_invalid_new_name() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("proj-a", "/ws/proj-a", "python", now())
            .unwrap();
        let err = reg.rename("proj-a", "Not_Valid", 30, now()).unwrap_err();
        assert!(matches!(err, RegistryError::InvalidName(_)));
    }

    #[test]
    fn legacy_flat_string_shape_is_read_and_upgraded_on_write() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("projects.local.json");
        fs::write(&path, r#"{"proj-a": "/ws/proj-a"}"#).unwrap();
        let reg = ProjectRegistry::new(path.clone());

        let row = reg.get("proj-a").unwrap().unwrap();
        assert_eq!(row.workspace, "/ws/proj-a");
        assert_eq!(row.backend_impl, DEFAULT_BACKEND_IMPL);

        // Any write upgrades the on-disk shape.
        reg.set_backend_impl("proj-a", "rust").unwrap();
        let on_disk = fs::read_to_string(&path).unwrap();
        let parsed: Value = serde_json::from_str(&on_disk).unwrap();
        assert!(
            parsed["proj-a"].is_object(),
            "the legacy shape must be upgraded on write"
        );
    }

    #[test]
    fn corrupt_json_is_recovered_with_a_backup_and_starts_fresh() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("projects.local.json");
        fs::write(&path, b"{not valid json").unwrap();
        let reg = ProjectRegistry::new(path.clone());

        assert_eq!(reg.list().unwrap(), vec![]);
        assert!(
            !path.exists(),
            "the corrupt file must be moved out of the way"
        );
        let backups: Vec<_> = fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().contains(".corrupt-"))
            .collect();
        assert_eq!(backups.len(), 1, "exactly one backup file must be created");
    }

    #[test]
    fn opaque_extra_fields_survive_a_read_modify_write_cycle() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("projects.local.json");
        fs::write(
            &path,
            r#"{"proj-a": {"workspace": "/ws/proj-a", "aliases": [], "created_at": "2026-01-01T00:00:00Z"}}"#,
        )
        .unwrap();
        let reg = ProjectRegistry::new(path.clone());

        reg.set_backend_impl("proj-a", "rust").unwrap();

        let on_disk = fs::read_to_string(&path).unwrap();
        let parsed: Value = serde_json::from_str(&on_disk).unwrap();
        assert_eq!(
            parsed["proj-a"]["created_at"], "2026-01-01T00:00:00Z",
            "an opaque extra field must survive a write that only touches backend_impl"
        );
    }

    #[test]
    fn list_is_sorted_by_name() {
        let dir = tempfile::tempdir().unwrap();
        let reg = registry_at(&dir);
        reg.register("zebra", "/ws/zebra", "python", now()).unwrap();
        reg.register("apple", "/ws/apple", "python", now()).unwrap();

        let names: Vec<String> = reg.list().unwrap().into_iter().map(|p| p.name).collect();
        assert_eq!(names, vec!["apple".to_string(), "zebra".to_string()]);
    }

    #[test]
    fn default_registry_path_honours_the_env_override() {
        let path = default_registry_path(|k| {
            if k == "AGENT_MCP_PROJECTS_FILE" {
                Some("/custom/projects.json".to_string())
            } else {
                None
            }
        });
        assert_eq!(path, PathBuf::from("/custom/projects.json"));
    }

    #[test]
    fn default_registry_path_falls_back_to_home_config() {
        let path = default_registry_path(|k| {
            if k == "HOME" {
                Some("/home/op".to_string())
            } else {
                None
            }
        });
        assert_eq!(
            path,
            PathBuf::from("/home/op/.config/agent-mcp/projects.local.json")
        );
    }

    #[test]
    fn is_valid_slug_matches_the_expected_shapes() {
        assert!(is_valid_slug("a"));
        assert!(is_valid_slug("proj-a"));
        assert!(is_valid_slug("proj-123"));
        assert!(!is_valid_slug(""));
        assert!(!is_valid_slug("Proj-A"));
        assert!(!is_valid_slug("-proj"));
        assert!(!is_valid_slug("proj-"));
        assert!(!is_valid_slug("proj_a"));
    }

    #[test]
    fn concurrent_writers_serialize_without_losing_either_registration() {
        // Real cross-thread contention against the SAME on-disk file
        // and sidecar lockfile -- proves the fd-lock-based LOCK_EX
        // actually serializes read-modify-write cycles rather than
        // merely compiling against the right types.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("projects.local.json");
        let path_a = path.clone();
        let path_b = path.clone();
        let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
        let barrier_a = barrier.clone();
        let barrier_b = barrier.clone();

        let handle_a = std::thread::spawn(move || {
            let reg = ProjectRegistry::new(path_a);
            barrier_a.wait();
            reg.register("proj-a", "/ws/proj-a", "python", now())
        });
        let handle_b = std::thread::spawn(move || {
            let reg = ProjectRegistry::new(path_b);
            barrier_b.wait();
            reg.register("proj-b", "/ws/proj-b", "python", now())
        });

        handle_a.join().unwrap().unwrap();
        handle_b.join().unwrap().unwrap();

        let reg = ProjectRegistry::new(path);
        let names: Vec<String> = reg.list().unwrap().into_iter().map(|p| p.name).collect();
        assert_eq!(names, vec!["proj-a".to_string(), "proj-b".to_string()]);
    }
}
