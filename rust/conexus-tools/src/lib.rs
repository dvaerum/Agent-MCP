//! CoNexus MCP tool catalogue (Phase D). One module per
//! `agent_mcp/tools/*.py`; [`registry::all_tools`] is the single,
//! hand-maintained registration list (see `conexus_auth::tool`'s doc
//! for why it lives HERE and not in `conexus-auth` — `conexus-tools`
//! is the crate that actually knows tool implementations exist).
//! See `/home/dennis/.claude/plans/prancy-napping-pie.md`.

pub mod agent_communication_tools;
pub mod agent_messaging;
pub mod agent_roster_tools;
pub mod agent_tools;
pub mod assign_task_tools;
pub mod completion_client;
pub mod context_window;
pub mod embedding_client;
pub mod project_settings_tools;
pub mod rag_tools;
pub mod registry;
pub mod task_mutation_engine;
pub mod task_query_engine;
pub mod task_tools;
pub mod utility_tools;
pub mod wake_notify;

pub use registry::all_tools;
