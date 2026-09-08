//! Port of `agent_mcp/repositories/pending_directive_repository.py`.
//!
//! `pending_directive` is the one-shot, human-triggered "poke" queue:
//! an operator pushes a single ad-hoc directive to one agent
//! out-of-band. It is NOT recurring — no `next_due_at`/interval —
//! just one row, delivered once (stamped `delivered_at`) or sitting
//! undelivered until the next check-in. Its sibling
//! `scheduled_directive_repository` (not yet ported) is the same
//! delivery concept at a different lifecycle stage: a recurring,
//! self-scheduling directive. Both converge on the identical
//! `directive` event JSON shape ([`DirectiveEvent`]), distinguished
//! only by `data.source` (`"poke"` vs `"schedule"`) and
//! `data.schedule_id` (`None` vs the schedule's id) — per ADR-0026,
//! both feed the same unified delivery-scheduler push mechanism.
//!
//! A module of plain functions, matching Python's own design (no
//! cache). Every function that touches the DB takes the
//! `&DatabaseConnection` it should run against — this crate has no
//! separate "opens its own connection" path, matching every other
//! repository here. Unlike `group_capability_repository`, this table
//! lives on the per-project AGENT database (confirmed via the ORM
//! model, `agent_mcp/db/models/pending_directive.py`), not the router
//! DB — don't assume from a sibling repository's placement.
//!
//! `agent_id` has NO database-level foreign key to `agents.agent_id`
//! — Python's model/migration comments call it a "logical FK" only,
//! so this port doesn't assume referential-integrity enforcement at
//! the DB layer either.
//!
//! Phase G (sea-orm migration): the sixth repository converted, and
//! the first whose own collector inside `assemble_event_feed`'s
//! wake-loop pipeline is now sea-orm-backed too — the prerequisite PR
//! (#975) threaded a `&sea_orm::DatabaseConnection` through that
//! function's async plumbing specifically so this conversion could
//! land without reshaping its signature again. [`poke_event`] stays a
//! pure, connection-less function throughout — it builds the wire
//! shape from already-fetched fields, nothing else in this module
//! changes that.

use sea_orm::{
    ActiveValue::Set, ColumnTrait, DatabaseConnection, DbErr, EntityTrait, PaginatorTrait,
    QueryFilter, QueryOrder,
};

pub use crate::entity::pending_directive::Model as PendingDirectiveRow;
use crate::entity::pending_directive::{ActiveModel, Column, Entity};

/// The wire shape both poke and scheduled directives converge on.
/// `event_type` serializes as `"type"` to match the JSON key Python's
/// `_poke_event`/`_schedule_event` produce.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct DirectiveEvent {
    #[serde(rename = "type")]
    pub event_type: String,
    pub ref_id: String,
    pub timestamp: String,
    pub priority: String,
    pub data: DirectiveEventData,
}

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct DirectiveEventData {
    pub prompt: String,
    pub source: String,
    pub schedule_id: Option<String>,
}

/// Ported from Python's `_poke_event`: `priority or "urgent"` — a
/// defensive fallback for a stored empty-string priority (the DB
/// column is `NOT NULL DEFAULT 'urgent'`, but this guards a row that
/// somehow got an empty string written directly, bypassing
/// `create_poke`'s own default). `pub` (not `pub(crate)`): Phase E1 PR
/// 14 (`conexus-rest-delivery-transport`) needs the identical wire
/// shape to build the `delivery` frame `POST /api/agents/{id}/
/// directive` pushes onto an already-connected worker's stream, the
/// same event this repository's own INSERT path constructs.
pub fn poke_event(poke_id: &str, prompt: &str, priority: &str, timestamp: &str) -> DirectiveEvent {
    let priority = if priority.is_empty() {
        "urgent"
    } else {
        priority
    };
    DirectiveEvent {
        event_type: "directive".to_string(),
        ref_id: poke_id.to_string(),
        timestamp: timestamp.to_string(),
        priority: priority.to_string(),
        data: DirectiveEventData {
            prompt: prompt.to_string(),
            source: "poke".to_string(),
            schedule_id: None,
        },
    }
}

/// INSERT an undelivered poke row. Returns the row as constructed
/// from the INSERT's own parameters — matching Python exactly, this
/// does NOT re-`SELECT` afterward. A duplicate `poke_id` surfaces as
/// a real `DbErr` (PK violation), not a special variant — matching
/// Python, which lets the underlying `sqlite3.IntegrityError`
/// propagate uncaught, and matching the rusqlite version's own
/// uncaught-`rusqlite::Error` behavior before this port.
#[allow(clippy::too_many_arguments)]
pub async fn create_poke(
    db: &DatabaseConnection,
    poke_id: &str,
    agent_id: &str,
    prompt: &str,
    priority: Option<&str>,
    created_by: Option<&str>,
    now_iso: &str,
) -> Result<PendingDirectiveRow, DbErr> {
    let priority = priority.unwrap_or("urgent");
    let am = ActiveModel {
        poke_id: Set(poke_id.to_string()),
        agent_id: Set(agent_id.to_string()),
        prompt: Set(prompt.to_string()),
        priority: Set(priority.to_string()),
        created_at: Set(now_iso.to_string()),
        created_by: Set(created_by.map(str::to_string)),
        delivered_at: Set(None),
    };
    Entity::insert(am).exec(db).await?;
    Ok(PendingDirectiveRow {
        poke_id: poke_id.to_string(),
        agent_id: agent_id.to_string(),
        prompt: prompt.to_string(),
        priority: priority.to_string(),
        created_at: now_iso.to_string(),
        created_by: created_by.map(String::from),
        delivered_at: None,
    })
}

/// Collect + mark-delivered every undelivered poke for `agent_id`.
/// Each row's `delivered_at` is stamped inside this same pass, so a
/// poke can never be double-delivered even if the caller re-invokes
/// this before committing (the caller owns the transaction/commit,
/// matching Python). Returns events in `created_at ASC` order —
/// "urgent sorts to the front" is a caller-side concern (Python's
/// `_sort_events_priority_then_time`), NOT this repository's job;
/// don't conflate SQL ordering with delivery-order guarantees.
/// Empty (never an error) when nothing is undelivered.
pub async fn collect_undelivered(
    db: &DatabaseConnection,
    agent_id: &str,
    now_iso: &str,
) -> Result<Vec<DirectiveEvent>, DbErr> {
    let rows = Entity::find()
        .filter(Column::AgentId.eq(agent_id))
        .filter(Column::DeliveredAt.is_null())
        .order_by_asc(Column::CreatedAt)
        .all(db)
        .await?;

    let mut events = Vec::with_capacity(rows.len());
    for row in rows {
        Entity::update_many()
            .col_expr(
                Column::DeliveredAt,
                sea_orm::sea_query::Expr::value(now_iso),
            )
            .filter(Column::PokeId.eq(row.poke_id.clone()))
            .exec(db)
            .await?;
        events.push(poke_event(
            &row.poke_id,
            &row.prompt,
            &row.priority,
            now_iso,
        ));
    }
    Ok(events)
}

/// Count of undelivered pokes for `agent_id`. `0`, never an error,
/// when there are none (`COUNT(*)` always returns exactly one row).
pub async fn count_undelivered(db: &DatabaseConnection, agent_id: &str) -> Result<i64, DbErr> {
    let count = Entity::find()
        .filter(Column::AgentId.eq(agent_id))
        .filter(Column::DeliveredAt.is_null())
        .count(db)
        .await?;
    Ok(count as i64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_schema;
    use sea_orm::Database;

    async fn test_conn() -> (tempfile::TempDir, DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.db");
        {
            let c = rusqlite::Connection::open(&path).unwrap();
            init_schema(&c).unwrap();
        }
        let db = Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, db)
    }

    #[tokio::test]
    async fn create_poke_returns_the_row_it_just_inserted() {
        let (_dir, db) = test_conn().await;
        let row = create_poke(
            &db,
            "poke-1",
            "alice",
            "check in",
            None,
            Some("admin"),
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();

        assert_eq!(row.poke_id, "poke-1");
        assert_eq!(row.agent_id, "alice");
        assert_eq!(row.prompt, "check in");
        assert_eq!(
            row.priority, "urgent",
            "default priority when None is passed"
        );
        assert_eq!(row.created_by.as_deref(), Some("admin"));
        assert_eq!(row.delivered_at, None);
    }

    #[tokio::test]
    async fn create_poke_duplicate_poke_id_is_a_real_error() {
        let (_dir, db) = test_conn().await;
        create_poke(
            &db,
            "poke-1",
            "alice",
            "first",
            None,
            None,
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        let err = create_poke(
            &db,
            "poke-1",
            "alice",
            "second",
            None,
            None,
            "2026-01-01T00:00:01Z",
        )
        .await;
        assert!(err.is_err());
    }

    #[tokio::test]
    async fn count_undelivered_reflects_only_undelivered_rows() {
        let (_dir, db) = test_conn().await;
        assert_eq!(count_undelivered(&db, "alice").await.unwrap(), 0);
        create_poke(
            &db,
            "poke-1",
            "alice",
            "p1",
            None,
            None,
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        create_poke(
            &db,
            "poke-2",
            "alice",
            "p2",
            None,
            None,
            "2026-01-01T00:00:01Z",
        )
        .await
        .unwrap();
        assert_eq!(count_undelivered(&db, "alice").await.unwrap(), 2);
    }

    #[tokio::test]
    async fn count_undelivered_is_scoped_per_agent() {
        let (_dir, db) = test_conn().await;
        create_poke(
            &db,
            "poke-1",
            "alice",
            "p1",
            None,
            None,
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        create_poke(
            &db,
            "poke-2",
            "bob",
            "p2",
            None,
            None,
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        assert_eq!(count_undelivered(&db, "alice").await.unwrap(), 1);
        assert_eq!(count_undelivered(&db, "bob").await.unwrap(), 1);
    }

    #[tokio::test]
    async fn collect_undelivered_marks_delivered_exactly_once() {
        let (_dir, db) = test_conn().await;
        create_poke(
            &db,
            "poke-1",
            "alice",
            "check in",
            Some("high"),
            None,
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();

        let events = collect_undelivered(&db, "alice", "2026-01-01T00:00:05Z")
            .await
            .unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event_type, "directive");
        assert_eq!(events[0].ref_id, "poke-1");
        assert_eq!(events[0].priority, "high");
        assert_eq!(events[0].data.prompt, "check in");
        assert_eq!(events[0].data.source, "poke");
        assert_eq!(events[0].data.schedule_id, None);
        assert_eq!(count_undelivered(&db, "alice").await.unwrap(), 0);

        // Second call: nothing left to collect -- delivered exactly once.
        let second = collect_undelivered(&db, "alice", "2026-01-01T00:00:06Z")
            .await
            .unwrap();
        assert_eq!(second, Vec::new());
    }

    #[tokio::test]
    async fn collect_undelivered_orders_by_created_at_ascending() {
        let (_dir, db) = test_conn().await;
        create_poke(
            &db,
            "poke-2",
            "alice",
            "second",
            None,
            None,
            "2026-01-01T00:00:02Z",
        )
        .await
        .unwrap();
        create_poke(
            &db,
            "poke-1",
            "alice",
            "first",
            None,
            None,
            "2026-01-01T00:00:01Z",
        )
        .await
        .unwrap();

        let events = collect_undelivered(&db, "alice", "2026-01-01T00:00:05Z")
            .await
            .unwrap();
        let ids: Vec<&str> = events.iter().map(|e| e.ref_id.as_str()).collect();
        assert_eq!(ids, vec!["poke-1", "poke-2"]);
    }

    #[tokio::test]
    async fn collect_undelivered_is_scoped_per_agent() {
        let (_dir, db) = test_conn().await;
        create_poke(
            &db,
            "poke-1",
            "alice",
            "for alice",
            None,
            None,
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        create_poke(
            &db,
            "poke-2",
            "bob",
            "for bob",
            None,
            None,
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();

        let events = collect_undelivered(&db, "alice", "2026-01-01T00:00:05Z")
            .await
            .unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].ref_id, "poke-1");
        // bob's poke must still be sitting undelivered.
        assert_eq!(count_undelivered(&db, "bob").await.unwrap(), 1);
    }

    #[tokio::test]
    async fn collect_undelivered_empty_is_not_an_error() {
        let (_dir, db) = test_conn().await;
        assert_eq!(
            collect_undelivered(&db, "alice", "2026-01-01T00:00:00Z")
                .await
                .unwrap(),
            Vec::new()
        );
    }
}
