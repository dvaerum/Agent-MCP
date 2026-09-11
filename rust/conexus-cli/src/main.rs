//! `conexus-cli` -- the operator CLI Python's `agent_mcp.cli` module
//! carries alongside the `server`/`router` leaf commands (both of
//! which stay their own binaries, `conexus-backend`/`conexus-router`,
//! per this migration's Target Architecture; this crate exists for
//! the two remaining standalone operator utilities Python's `cli.py`
//! bundles into the same entry point: `backup` and
//! `router create-operator`).
//!
//! Deliberately NOT a general-purpose wrapper: no `server`/`router`
//! subcommands are re-exposed here (those are `conexus-backend`/
//! `conexus-router`'s own binaries -- duplicating their invocation
//! surface here would just be a second, driftable entry point).

mod backup;
mod create_operator;
mod migrate;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "conexus-cli", about = "CoNexus operator CLI")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Back up a project's SQLite database to OUTPUT_PATH.
    Backup {
        /// Directory containing `.agent/mcp_state.db`.
        project_dir: std::path::PathBuf,
        /// Where to write the backup.
        output_path: std::path::PathBuf,
        /// Overwrite OUTPUT_PATH if it already exists.
        #[arg(long)]
        force: bool,
    },
    /// Bring a project's database up to date against the sea-orm-
    /// migration schema authority (idempotent -- safe on both a
    /// fresh and an already-migrated database; see
    /// `conexus_db::migration`'s own module doc).
    Migrate {
        /// Directory containing `.agent/mcp_state.db`.
        project_dir: std::path::PathBuf,
    },
    /// Router-scoped operator-management subcommands.
    Router {
        #[command(subcommand)]
        command: RouterCommand,
    },
}

#[derive(Subcommand)]
enum RouterCommand {
    /// Create the first operator (or an additional one).
    CreateOperator {
        /// Username for the new operator account.
        #[arg(long)]
        username: String,
        /// Optional email address (used by SSO linking).
        #[arg(long)]
        email: Option<String>,
        /// Read the password from the first line of stdin instead of
        /// an interactive hidden prompt.
        #[arg(long)]
        password_stdin: bool,
    },
    /// Bring `router.db` up to date against the sea-orm-migration
    /// schema authority (idempotent, matching the per-project
    /// `migrate` command). Path resolution matches
    /// `router create-operator`'s own `AGENT_MCP_ROUTER_DB`-or-
    /// production-default rule.
    Migrate,
}

// `#[tokio::main]`, matching `conexus-router`'s/`conexus-backend`'s own
// binary-entry-point convention (both `#[tokio::main] async fn main()`)
// rather than a scoped `tokio::runtime::Runtime::new()?.block_on(...)`
// wrapped only around the one now-async call site
// (`create_operator::run`, which needs a `sea_orm::DatabaseConnection`
// now that `identity::create_user` is sea-orm-backed) -- this crate is
// small enough (two leaf subcommands) that matching the workspace's
// established idiom costs nothing, and `backup::run` (still fully
// sync, plain rusqlite file I/O) runs unchanged inside the async `main`
// with no `.await` needed. `flavor = "current_thread"` (not
// conexus-router's/conexus-backend's own default multi-thread
// runtime): a one-shot CLI command has no concurrent connections to
// schedule across worker threads, so a single-threaded runtime is the
// right-sized choice here (matches the crate's own `tokio = { features
// = ["rt", "macros"] }`, not `rt-multi-thread`).
#[tokio::main(flavor = "current_thread")]
async fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Backup {
            project_dir,
            output_path,
            force,
        } => backup::run(&project_dir, &output_path, force),
        Command::Migrate { project_dir } => migrate::run_project(&project_dir).await,
        Command::Router {
            command:
                RouterCommand::CreateOperator {
                    username,
                    email,
                    password_stdin,
                },
        } => create_operator::run(&username, email.as_deref(), password_stdin).await,
        Command::Router {
            command: RouterCommand::Migrate,
        } => {
            let db_path = create_operator::router_db_path(|k| std::env::var(k).ok());
            migrate::run_router(&db_path).await
        }
    }
}
