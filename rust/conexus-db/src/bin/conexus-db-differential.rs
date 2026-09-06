//! Differential-testing harness binary (Phase B).
//!
//! Reads a JSON request from stdin describing a sequence of
//! repository operations, applies them to an EXISTING SQLite file,
//! then dumps the resulting `agents`/`project_context` table contents
//! as JSON to stdout. Never creates or migrates schema itself — the
//! file it's pointed at must already have Python's real ORM/Alembic
//! schema, matching the plan's "Rust reads/writes the schema Alembic
//! produces, never races it" rule.
//!
//! A Python test runs the SAME operation sequence through the real
//! Python repositories against a copy of the same starting file, then
//! diffs its own dump against this binary's, proving Rust and Python
//! agree on behavior for the sequences the harness covers — see
//! `tests/test_rust_differential_repositories.py`.
//!
//! Request shape:
//! ```json
//! {"db_path": "/path/to/mcp_state.db", "operations": [
//!   {"op": "agent_create", "token": "t1", "agent_id": "alice", ...},
//!   {"op": "agent_update_field", "agent_id": "alice", "field": "status",
//!    "value": {"kind": "text", "value": "active"}, "now": "..."}
//! ]}
//! ```
//!
//! A second, unrelated CLI mode (`--dump-router-tables <db_path>`)
//! closes the Phase F research finding that `router.db` schema
//! compatibility had only ever been verified SOURCE-vs-SOURCE
//! (Alembic migrations vs. `schema::init_router_schema`), never by
//! actually reading a real, populated `router.db` copy through Rust's
//! own `rusqlite` bindings. Deliberately RAW column dumps (no typed
//! repository layer) since the point is proving the on-disk schema
//! parses correctly under Rust at all -- a typed accessor could
//! silently coerce or skip a column a raw dump wouldn't. Lives here
//! rather than as a `conexus-router` binary because `conexus-router`
//! has no library target (ADR-0020's dependency direction runs
//! conexus-db -> ... -> conexus-router, never the reverse) and this
//! tool only needs `rusqlite`, which conexus-db already depends on.

use conexus_db::{
    project_context_repository, AgentField, AgentRepository, AgentRow, FieldValue, NewAgent,
    ProjectContextRow,
};
use rusqlite::types::ValueRef;
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;
use std::io::Read;
use std::process::ExitCode;

/// The router.db tables this dump mode covers -- every table Phase E2
/// PR3/PR4's `conexus-router::identity`/`conexus_db::
/// group_membership_repository` ports touch. Ordered by primary key
/// (or, for `group_membership`/`project_membership`'s composite/no
/// single PK, by every column) for a deterministic dump.
const ROUTER_TABLES: &[(&str, &str)] = &[
    ("users", "user_id"),
    ("sessions", "session_id"),
    ("project_membership", "project_name, user_id, group_id"),
    ("groups", "group_id"),
    ("group_capability", "group_id, capability"),
    (
        "group_membership",
        "group_id, member_user_id, member_group_id",
    ),
];

fn value_ref_to_json(v: ValueRef<'_>) -> JsonValue {
    match v {
        ValueRef::Null => JsonValue::Null,
        ValueRef::Integer(i) => JsonValue::from(i),
        ValueRef::Real(f) => {
            serde_json::Number::from_f64(f).map_or(JsonValue::Null, JsonValue::Number)
        }
        ValueRef::Text(t) => JsonValue::String(String::from_utf8_lossy(t).into_owned()),
        ValueRef::Blob(b) => JsonValue::String(format!("<blob {} bytes>", b.len())),
    }
}

/// Dumps every row of every table in [`ROUTER_TABLES`] as
/// `{table_name: [{column: value, ...}, ...]}`, skipping a table that
/// doesn't exist in this particular `router.db` (an older schema
/// snapshot, or a test fixture that never ran every migration) rather
/// than failing the whole dump.
fn dump_router_tables(conn: &Connection) -> Result<JsonValue, String> {
    let mut out = serde_json::Map::new();
    for (table, order_by) in ROUTER_TABLES {
        let exists: bool = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?1",
                [table],
                |r| r.get::<_, i64>(0),
            )
            .map_err(|e| format!("checking table {table}: {e}"))?
            > 0;
        if !exists {
            out.insert(table.to_string(), JsonValue::Array(vec![]));
            continue;
        }
        let sql = format!("SELECT * FROM {table} ORDER BY {order_by}");
        let mut stmt = conn
            .prepare(&sql)
            .map_err(|e| format!("preparing dump of {table}: {e}"))?;
        let col_names: Vec<String> = stmt.column_names().iter().map(|s| s.to_string()).collect();
        let rows = stmt
            .query_map([], |row| {
                let mut obj = serde_json::Map::new();
                for (i, name) in col_names.iter().enumerate() {
                    let v = row.get_ref(i)?;
                    obj.insert(name.clone(), value_ref_to_json(v));
                }
                Ok(JsonValue::Object(obj))
            })
            .map_err(|e| format!("querying {table}: {e}"))?;
        let mut collected = Vec::new();
        for row in rows {
            collected.push(row.map_err(|e| format!("reading a row of {table}: {e}"))?);
        }
        out.insert(table.to_string(), JsonValue::Array(collected));
    }
    Ok(JsonValue::Object(out))
}

#[derive(Deserialize)]
struct Request {
    db_path: String,
    operations: Vec<Operation>,
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum Operation {
    AgentCreate {
        token: String,
        agent_id: String,
        created_at: String,
        status: String,
        current_task: Option<String>,
        working_directory: String,
        color: Option<String>,
        agent_role: String,
    },
    AgentUpdateField {
        agent_id: String,
        field: String,
        value: OpFieldValue,
        now: String,
    },
    AgentTerminate {
        agent_id: String,
        now: String,
    },
    AgentDelete {
        agent_id: String,
    },
    AgentRotateToken {
        agent_id: String,
        new_token: String,
        now: String,
    },
    AgentAdvanceEventCursor {
        agent_id: String,
        cursor_value: String,
        now: String,
    },
    ContextUpsert {
        context_key: String,
        value: String,
        description: Option<String>,
        description_provided: bool,
        actor: String,
        now: String,
    },
    ContextCreateNew {
        context_key: String,
        value: String,
        description: Option<String>,
        actor: String,
        now: String,
    },
    ContextDeleteMany {
        context_keys: Vec<String>,
    },
}

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum OpFieldValue {
    Text { value: String },
    OptionalText { value: Option<String> },
    Bool { value: bool },
}

impl From<OpFieldValue> for FieldValue {
    fn from(v: OpFieldValue) -> Self {
        match v {
            OpFieldValue::Text { value } => FieldValue::Text(value),
            OpFieldValue::OptionalText { value } => FieldValue::OptionalText(value),
            OpFieldValue::Bool { value } => FieldValue::Bool(value),
        }
    }
}

/// Maps the JSON field name to the closed [`AgentField`] enum. Kept
/// here rather than a `FromStr` on the type itself — that mapping is
/// this binary's own wire format, not part of `conexus-db`'s public
/// API contract.
fn agent_field(name: &str) -> Option<AgentField> {
    Some(match name {
        "status" => AgentField::Status,
        "current_task" => AgentField::CurrentTask,
        "working_directory" => AgentField::WorkingDirectory,
        "color" => AgentField::Color,
        "terminated_at" => AgentField::TerminatedAt,
        "auto_event_loop" => AgentField::AutoEventLoop,
        "last_activity_at" => AgentField::LastActivityAt,
        "last_event_seen_at" => AgentField::LastEventSeenAt,
        "agent_role" => AgentField::AgentRole,
        "aoe_session_id" => AgentField::AoeSessionId,
        _ => return None,
    })
}

#[derive(Serialize)]
struct Dump {
    agents: Vec<AgentRow>,
    project_context: Vec<ProjectContextRow>,
}

fn apply(conn: &Connection, op: Operation) -> Result<(), String> {
    match op {
        Operation::AgentCreate {
            token,
            agent_id,
            created_at,
            status,
            current_task,
            working_directory,
            color,
            agent_role,
        } => AgentRepository::create(
            conn,
            NewAgent {
                token: &token,
                agent_id: &agent_id,
                created_at: &created_at,
                status: &status,
                current_task: current_task.as_deref(),
                working_directory: &working_directory,
                color: color.as_deref(),
                agent_role: &agent_role,
            },
        )
        .map(|_| ())
        .map_err(|e| e.to_string()),

        Operation::AgentUpdateField {
            agent_id,
            field,
            value,
            now,
        } => {
            let field =
                agent_field(&field).ok_or_else(|| format!("unknown agent field: {field}"))?;
            AgentRepository::update_field(conn, &agent_id, field, value.into(), &now)
                .map(|_| ())
                .map_err(|e| e.to_string())
        }

        Operation::AgentTerminate { agent_id, now } => {
            AgentRepository::terminate(conn, &agent_id, &now)
                .map(|_| ())
                .map_err(|e| e.to_string())
        }

        Operation::AgentDelete { agent_id } => AgentRepository::delete(conn, &agent_id)
            .map(|_| ())
            .map_err(|e| e.to_string()),

        Operation::AgentRotateToken {
            agent_id,
            new_token,
            now,
        } => AgentRepository::rotate_token(conn, &agent_id, &new_token, &now)
            .map(|_| ())
            .map_err(|e| e.to_string()),

        Operation::AgentAdvanceEventCursor {
            agent_id,
            cursor_value,
            now,
        } => AgentRepository::advance_event_cursor(conn, &agent_id, &cursor_value, &now)
            .map(|_| ())
            .map_err(|e| e.to_string()),

        Operation::ContextUpsert {
            context_key,
            value,
            description,
            description_provided,
            actor,
            now,
        } => project_context_repository::upsert(
            conn,
            &context_key,
            &value,
            description.as_deref(),
            description_provided,
            &actor,
            &now,
        )
        .map(|_| ())
        .map_err(|e| e.to_string()),

        Operation::ContextCreateNew {
            context_key,
            value,
            description,
            actor,
            now,
        } => project_context_repository::create_new(
            conn,
            &context_key,
            &value,
            description.as_deref(),
            &actor,
            &now,
        )
        .map(|_| ())
        .map_err(|e| e.to_string()),

        Operation::ContextDeleteMany { context_keys } => {
            let refs: Vec<&str> = context_keys.iter().map(String::as_str).collect();
            project_context_repository::delete_many(conn, &refs)
                .map(|_| ())
                .map_err(|e| e.to_string())
        }
    }
}

fn run() -> Result<String, String> {
    let mut input = String::new();
    std::io::stdin()
        .read_to_string(&mut input)
        .map_err(|e| format!("failed to read stdin: {e}"))?;

    let request: Request =
        serde_json::from_str(&input).map_err(|e| format!("failed to parse request JSON: {e}"))?;

    let conn = Connection::open(&request.db_path)
        .map_err(|e| format!("failed to open {}: {e}", request.db_path))?;

    for op in request.operations {
        apply(&conn, op).map_err(|e| format!("operation failed: {e}"))?;
    }

    let agents =
        AgentRepository::dump_all(&conn).map_err(|e| format!("failed to dump agents: {e}"))?;
    let project_context = project_context_repository::list_all(&conn)
        .map_err(|e| format!("failed to dump project_context: {e}"))?;

    serde_json::to_string(&Dump {
        agents,
        project_context,
    })
    .map_err(|e| format!("failed to serialize dump: {e}"))
}

fn run_dump_router_tables(db_path: &str) -> Result<String, String> {
    let conn = Connection::open(db_path).map_err(|e| format!("failed to open {db_path}: {e}"))?;
    let dump = dump_router_tables(&conn)?;
    serde_json::to_string(&dump).map_err(|e| format!("failed to serialize dump: {e}"))
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let result = match args.get(1).map(String::as_str) {
        Some("--dump-router-tables") => match args.get(2) {
            Some(db_path) => run_dump_router_tables(db_path),
            None => Err("--dump-router-tables requires a <db_path> argument".to_string()),
        },
        _ => run(),
    };
    match result {
        Ok(json) => {
            println!("{json}");
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("{e}");
            ExitCode::FAILURE
        }
    }
}
