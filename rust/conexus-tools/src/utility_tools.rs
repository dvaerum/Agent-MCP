//! Port of `agent_mcp/tools/utility_tools.py` (Phase D5, PR 1). The
//! smallest tool module in the codebase: one `PUBLIC` tool with no
//! arguments and no side effects, used to verify the tool-calling
//! mechanism itself works. The FIRST `Requirement::Public` tool this
//! migration ports -- registering it in `registry.rs`'s
//! `PUBLIC_TOOL_ALLOWLIST` IS the security review that allowlist
//! exists to force (see that test's own doc).

use conexus_auth::{Requirement, Tool};
use conexus_core::principal::Principal;
use conexus_core::tool_result::ToolResult;
use rusqlite::Connection;
use serde_json::Value;
use tokio::sync::Mutex as AsyncMutex;

pub struct TestTool;

impl Tool for TestTool {
    const NAME: &'static str = "test";
    const REQUIRED: Requirement = Requirement::Public;
    const DESCRIPTION: &'static str =
        "A simple test tool to verify the tool calling mechanism is working.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {},
        "additionalProperties": false
    }"#;

    fn call<'a>(
        _principal: Option<&'a Principal>,
        _arguments: &'a Value,
        _conn: &'a AsyncMutex<Connection>,
        _now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            ToolResult::Ok {
                data: None,
                message: Some("Tool is working!".to_string()),
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::waiter_registry::WaiterRegistry;

    #[tokio::test]
    async fn test_tool_always_succeeds() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        let conn = AsyncMutex::new(conn);
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result = TestTool::call(None, &Value::Null, &conn, "2026-06-01T00:00:00Z", &ctx).await;
        assert_eq!(
            result,
            ToolResult::Ok {
                data: None,
                message: Some("Tool is working!".to_string()),
            }
        );
    }
}
