//! CoNexus wake-loop support primitives (Phase D3). One module per
//! `agent_mcp/core/{hold_ladder,client_hold_strategy,idle_reminder,
//! stream_gates}.py`, plus the per-agent waiter registry -- the pieces
//! `wait_for_events` (a `conexus-tools` `Tool`) needs that don't belong
//! in `conexus-core` (they hold real mutable process state, not pure
//! domain types) and don't belong in `conexus-tools` itself (multiple
//! tools/subsystems will eventually share them). Sits between
//! `conexus-auth` and `conexus-tools` in the workspace dependency
//! direction: `conexus-tools` depends on this crate, this crate does
//! not know tool implementations exist.
//!
//! See `/home/dennis/.claude/plans/prancy-napping-pie.md` (Phase D3) for
//! the full wake-loop design and the research report this crate's first
//! modules are ported from.

pub mod client_hold_strategy;
pub mod hold_ladder;
