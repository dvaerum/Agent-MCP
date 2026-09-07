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
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Backup {
            project_dir,
            output_path,
            force,
        } => backup::run(&project_dir, &output_path, force),
        Command::Router {
            command:
                RouterCommand::CreateOperator {
                    username,
                    email,
                    password_stdin,
                },
        } => create_operator::run(&username, email.as_deref(), password_stdin),
    }
}
