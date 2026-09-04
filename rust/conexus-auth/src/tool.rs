//! `Tool` trait + type-erased [`ToolDescriptor`] + [`dispatch`].
//!
//! Port of the OTHER half of `agent_mcp/core/authorize.py` +
//! `agent_mcp/tools/registry.py::register_tool`: a `Tool` implementation
//! declares its own name and [`Requirement`] as associated consts —
//! there is no second, decorator-level place for them to drift against
//! (see `crate::requirement`'s module doc).
//!
//! Mechanism note: `const` items aren't dispatchable through `dyn
//! Tool`, so a registry can't return `Vec<&dyn Tool>` the way a more
//! naive design might reach for. [`ToolDescriptor`] is the type-erased
//! shape instead — `name`/`required` copied out of the const, `call` a
//! plain function pointer to the monomorphized impl. `ToolDescriptor::
//! of::<T>()` is `const fn`, so a whole registry can be a `static`
//! array built at compile time, same shape as a hand-written sequence
//! of Python's `register_tool(...)` calls, just collected into a slice
//! instead of executed as side-effecting calls at import time.
//!
//! **The registration list itself (`all_tools()`) does NOT live here.**
//! It moved to `conexus-tools` (the crate one layer up in the
//! workspace dependency direction, see the migration plan's Target
//! Architecture section) the moment a real tool crate existed to hold
//! it — `conexus-auth` sits BELOW `conexus-tools` and must not know
//! tool implementations exist, only the trait/dispatch mechanism they
//! plug into. `all_tools()` briefly lived here as a placeholder before
//! any tool crate existed (Phase C); Phase D1's first real tool port
//! is what surfaced that it was in the wrong crate.

use crate::requirement::{PolicySource, Requirement};
use conexus_core::principal::Principal;
use conexus_core::tool_result::ToolResult;
use conexus_wakeloop::waiter_registry::WaiterRegistry;
use rusqlite::Connection;
use serde_json::Value;
use std::future::Future;
use std::pin::Pin;
use tokio::sync::Mutex as AsyncMutex;

/// A boxed, type-erased future — the return shape [`Tool::call`] uses
/// instead of a bare `async fn` (a trait method can't return `impl
/// Future` and still be storable as a plain `fn` pointer field on
/// [`ToolDescriptor`], the way `Tool::call`'s synchronous predecessor
/// was). `Send` is required because `conexus-backend`'s `call_tool`
/// awaits this from inside an `rmcp` handler future that the runtime
/// may resume on a different worker thread.
///
/// Why `conn: &AsyncMutex<Connection>`, not a bare `&Connection`
/// (tried first, reverted): `rusqlite::Connection` is `Send` but not
/// `Sync`, so a bare `&'a Connection` captured into an `async move`
/// block is itself `!Send` — and that makes the WHOLE generated
/// future non-`Send`, even for a tool with zero internal `.await`
/// points, because the future's pre-first-poll state already holds
/// its captured parameters (the compiler can't assume nothing will
/// ever move the boxed-but-unpolled future to another thread first).
/// `tokio::sync::Mutex<T>` is unconditionally `Sync` when `T: Send`
/// (its whole point is providing its own synchronization), so
/// `&AsyncMutex<Connection>` IS `Send` regardless of `Connection`'s
/// own `Sync`-ness. Every `Tool::call` impl's first line is `let conn
/// = conn.lock().await;` — the resulting `MutexGuard<'_, Connection>`
/// is itself `Send` (only needs `T: Send`), so it stays fine to hold
/// across any FURTHER internal `.await` the tool body needs (e.g.
/// `ask_project_rag`'s embedding/completion calls, once its DB reads
/// are done).
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Sends one MCP `notifications/progress` frame for the in-flight
/// call. `conexus-auth` stays `rmcp`-free (mirrors `conexus-core`'s own
/// "never needs an `rmcp` dependency" design value, see
/// `conexus-core::tool_result`'s module doc) by taking this trait
/// object instead of a raw `rmcp::service::Peer` -- only
/// `conexus-backend` (the one crate that actually depends on `rmcp`)
/// implements it, against a real `Peer`/`ProgressToken` pair.
pub trait ProgressSink: Send + Sync {
    /// Returns `false` on send failure (client transport gone) or when
    /// off-wire (no real MCP peer to notify, e.g. a REST caller or a
    /// test harness) -- callers treat `false` exactly like Python's
    /// `send_progress_heartbeat` returning `False`.
    fn notify_progress<'a>(&'a self, progress: f64) -> BoxFuture<'a, bool>;
}

/// Ambient per-call context beyond the DB connection: MCP-transport
/// facts only `conexus-backend`'s `call_tool` (which alone holds the
/// real `RequestContext`) can supply, plus the one piece of
/// cross-connection shared state `wait_for_events` needs. Every
/// `Tool::call` impl before this parameter existed added no I/O-
/// adjacent input beyond `conn`/`now`; this is the next explicit-input
/// addition in that same lineage -- needed the moment a tool
/// (`wait_for_events`, Phase D3) genuinely depends on facts no prior
/// tool did, per the same "explicit input over hidden state"
/// convention `conn`/`now` themselves were added under. Every OTHER
/// tool simply ignores this parameter.
pub struct ToolCallContext<'a> {
    /// Whether this call's MCP request carried a `_meta.progressToken`
    /// -- Python's `current_progress_token() is not None`. Only
    /// presence matters to tool logic (hold-strategy/ladder
    /// eligibility); the token value itself never leaves the transport
    /// layer (`progress_sink` already carries it internally).
    pub progress_token_present: bool,
    /// The client's `clientInfo.name` from its MCP `initialize`
    /// handshake, if known -- Python's
    /// `client_info_registry.get_client_name`. `None` off-wire or for
    /// an unknown client.
    pub client_name: Option<&'a str>,
    /// Send a progress heartbeat on this call's connection. `None`
    /// off-wire (no real MCP peer to notify).
    pub progress_sink: Option<&'a dyn ProgressSink>,
    /// The per-agent `wait_for_events` waiter registry.
    pub waiter_registry: &'a WaiterRegistry,
}

impl<'a> ToolCallContext<'a> {
    /// An off-wire context: no progress token, no client identity, no
    /// progress sink -- what a REST caller or a test harness gets. A
    /// `WaiterRegistry` is still required (never `Option`d away): every
    /// real boot path constructs exactly one and every tool that
    /// doesn't need it simply ignores this field, matching the
    /// explicit-input convention rather than special-casing "off-wire"
    /// as a reason to skip constructing shared state that costs
    /// nothing to create.
    pub fn off_wire(waiter_registry: &'a WaiterRegistry) -> Self {
        ToolCallContext {
            progress_token_present: false,
            client_name: None,
            progress_sink: None,
            waiter_registry,
        }
    }
}

/// A tool entry point. Real tool modules (Phase D1+, in `conexus-
/// tools` and later crates) each define a zero-sized type implementing
/// this trait; `NAME` and `REQUIRED` are the single declaration site
/// for the tool's identity and authorization story.
pub trait Tool {
    const NAME: &'static str;
    const REQUIRED: Requirement;

    /// The tool's one-line description, verbatim from the Python
    /// `register_tool(..., description=...)` call site — part of the
    /// `tools/list` surface a client sees.
    const DESCRIPTION: &'static str;

    /// The tool's JSON Schema for its `arguments`, as literal JSON
    /// text — mirrors the exact `input_schema` dict shape Python's
    /// `register_tool(...)` call sites pass. A `&'static str` rather
    /// than a `serde_json::Value` because `Value` isn't `const`-
    /// constructible; parse it via [`ToolDescriptor::parsed_schema`]
    /// when a real `Value` is needed (e.g. building a `tools/list`
    /// response).
    const SCHEMA: &'static str;

    /// Run the tool. Enforcement is the DISPATCHER's job (see
    /// [`dispatch`]), not this method's — a direct call here is
    /// intentionally ungated, the same way Python's raw impl function
    /// (before `@requires_*` wraps it) has no gate of its own. Every
    /// real call path must go through [`dispatch`] or an equivalent
    /// that checks `Self::REQUIRED` first.
    ///
    /// `conn` is a lock over the caller's already-open connection to
    /// the project's own database (Python's `unit_of_work()` seam) —
    /// see [`BoxFuture`]'s doc for why it's a `Mutex` reference, not a
    /// bare `&Connection`. `now` is an ISO-8601 timestamp (the shape
    /// every `conexus-db` write function's own `now: &str` parameter
    /// already expects). Both added when Phase D1's first real tool
    /// port (`project_settings_tools`) needed them; `EchoTool` and
    /// friends in this module's own tests ignore both. Explicit
    /// parameters, not a hidden global/thread-local/wall-clock read,
    /// matching this crate's established "explicit input over hidden
    /// state" convention (`router_conn: Option<&Connection>` in
    /// `capabilities::resolve_capabilities`, `now: u64` in
    /// `forwarding_header`).
    ///
    /// Async (returns [`BoxFuture`], not a bare `ToolResult`) since
    /// Phase D2's `ask_project_rag` needs real network I/O (an
    /// embedding call and a chat-completion call) mid-call — the
    /// crate's own `never block the event loop` discipline (mirroring Python's
    /// R12-F2, a live-exploited full-server-freeze bug from exactly
    /// this mistake) rules out a synchronous `call` reaching for
    /// `block_on` internally. A tool with no I/O of its own (every
    /// tool ported before D2) just locks `conn`, does its (fully
    /// synchronous) work, and never actually suspends.
    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a ToolCallContext<'a>,
    ) -> BoxFuture<'a, ToolResult>;
}

/// The type-erased shape of a [`Tool`], suitable for a flat runtime
/// registry. See the module doc for why this exists instead of `dyn
/// Tool`.
/// The monomorphized shape of [`Tool::call`], type-erased to a plain
/// function pointer. Named (rather than written inline on
/// [`ToolDescriptor::call`]) purely to satisfy `clippy::type_complexity`
/// -- the params/return themselves haven't changed shape, just where
/// the type is spelled out.
pub type ToolCallFn = for<'a> fn(
    Option<&'a Principal>,
    &'a Value,
    &'a AsyncMutex<Connection>,
    &'a str,
    &'a ToolCallContext<'a>,
) -> BoxFuture<'a, ToolResult>;

pub struct ToolDescriptor {
    pub name: &'static str,
    pub description: &'static str,
    pub required: Requirement,
    pub schema: &'static str,
    pub call: ToolCallFn,
}

impl ToolDescriptor {
    pub const fn of<T: Tool>() -> Self {
        ToolDescriptor {
            name: T::NAME,
            description: T::DESCRIPTION,
            required: T::REQUIRED,
            schema: T::SCHEMA,
            call: T::call,
        }
    }

    /// Parse `schema` into a `serde_json::Value` — e.g. for a
    /// `tools/list` response. Panics on malformed JSON: a `Tool`
    /// impl's `SCHEMA` is developer-authored literal text, not
    /// untrusted runtime input, so a parse failure is a bug to catch
    /// in CI (`conexus-tools`'s own
    /// `every_registered_tools_schema_is_valid_json` arch test), not
    /// a condition a real caller could ever trigger.
    pub fn parsed_schema(&self) -> serde_json::Value {
        serde_json::from_str(self.schema)
            .unwrap_or_else(|e| panic!("Tool {:?}'s SCHEMA is not valid JSON: {e}", self.name))
    }
}

/// Check `descriptor.required` against `principal`, THEN call the
/// tool. Denial becomes [`ToolResult::PermissionDenied`] rather than a
/// separate error type, matching how a real MCP/REST caller ultimately
/// renders it — the same "trait declares intent, dispatcher enforces
/// before calling" split the migration plan calls for, and the direct
/// analogue of `dispatch_tool_call`'s R20-F4 pre-schema-validation
/// gate (enforcement happens before the tool ever sees `arguments`).
pub async fn dispatch(
    descriptor: &ToolDescriptor,
    principal: Option<&Principal>,
    policy_source: &dyn PolicySource,
    arguments: &Value,
    conn: &AsyncMutex<Connection>,
    now: &str,
    ctx: &ToolCallContext<'_>,
) -> ToolResult {
    if let Err(rejected) = descriptor.required.check(principal, policy_source) {
        return ToolResult::PermissionDenied {
            reason: rejected.reason,
        };
    }
    (descriptor.call)(principal, arguments, conn, now, ctx).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::requirement::NoPolicyOverrides;
    use conexus_core::capability::{Capabilities, Capability};
    use conexus_core::principal::PrincipalKind;
    use std::sync::atomic::{AtomicBool, Ordering};

    static CALLED: AtomicBool = AtomicBool::new(false);

    struct EchoTool;
    impl Tool for EchoTool {
        const NAME: &'static str = "echo";
        const REQUIRED: Requirement = Requirement::Cap {
            cap: Capability::TasksView,
            reason: None,
        };
        const DESCRIPTION: &'static str = "Echoes its arguments back.";
        const SCHEMA: &'static str =
            r#"{"type":"object","properties":{},"additionalProperties":true}"#;
        fn call<'a>(
            _principal: Option<&'a Principal>,
            arguments: &'a Value,
            _conn: &'a AsyncMutex<Connection>,
            _now: &'a str,
            _ctx: &'a ToolCallContext<'a>,
        ) -> BoxFuture<'a, ToolResult> {
            Box::pin(async move {
                CALLED.store(true, Ordering::SeqCst);
                ToolResult::Ok {
                    data: Some(arguments.clone()),
                    message: None,
                }
            })
        }
    }

    fn agent_bearer(caps: Capabilities) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some("a1".to_string()),
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: caps,
        }
    }

    #[test]
    fn tool_descriptor_of_captures_the_impls_const_declaration() {
        let d = ToolDescriptor::of::<EchoTool>();
        assert_eq!(d.name, "echo");
        assert_eq!(d.description, "Echoes its arguments back.");
        assert_eq!(
            d.required,
            Requirement::Cap {
                cap: Capability::TasksView,
                reason: None
            }
        );
        assert_eq!(
            d.schema,
            r#"{"type":"object","properties":{},"additionalProperties":true}"#
        );
    }

    #[test]
    fn parsed_schema_parses_the_literal_schema_text() {
        let d = ToolDescriptor::of::<EchoTool>();
        let parsed = d.parsed_schema();
        assert_eq!(parsed["type"], "object");
        assert_eq!(parsed["additionalProperties"], true);
    }

    #[test]
    #[should_panic(expected = "not valid JSON")]
    fn parsed_schema_panics_on_malformed_json() {
        struct BrokenSchemaTool;
        impl Tool for BrokenSchemaTool {
            const NAME: &'static str = "broken";
            const REQUIRED: Requirement = Requirement::Public;
            const DESCRIPTION: &'static str = "Has a malformed schema.";
            const SCHEMA: &'static str = "{not json";
            fn call<'a>(
                _: Option<&'a Principal>,
                _: &'a Value,
                _: &'a AsyncMutex<Connection>,
                _: &'a str,
                _: &'a ToolCallContext<'a>,
            ) -> BoxFuture<'a, ToolResult> {
                unreachable!("not called by this test")
            }
        }
        ToolDescriptor::of::<BrokenSchemaTool>().parsed_schema();
    }

    // Both cases below share the `CALLED` static and MUST run in one
    // test function, not two -- split into separate `#[test]`s they'd
    // race under Rust's default parallel test execution (the exact
    // class of flake this workspace already fixed once in
    // `conexus-vec`'s `REGISTRATION_LOCK` tests: two tests mutating
    // shared global state can interleave and observe each other's
    // writes). Sequencing both assertions in one test makes the
    // ordering deterministic instead.
    #[tokio::test]
    async fn dispatch_denies_before_calling_and_calls_the_tool_on_admission() {
        CALLED.store(false, Ordering::SeqCst);
        let conn = AsyncMutex::new(Connection::open_in_memory().unwrap());
        let registry = WaiterRegistry::new();
        let ctx = ToolCallContext::off_wire(&registry);
        let d = ToolDescriptor::of::<EchoTool>();
        let denied_principal = agent_bearer(Capabilities::from_iter([])); // missing TasksView
        let denial = dispatch(
            &d,
            Some(&denied_principal),
            &NoPolicyOverrides,
            &Value::Null,
            &conn,
            "2026-01-01T00:00:00Z",
            &ctx,
        )
        .await;

        assert!(
            !CALLED.load(Ordering::SeqCst),
            "tool must not run on denial"
        );
        assert!(matches!(denial, ToolResult::PermissionDenied { .. }));

        let admitted_principal = agent_bearer(Capabilities::from_iter([Capability::TasksView]));
        let args = serde_json::json!({"x": 1});
        let admission = dispatch(
            &d,
            Some(&admitted_principal),
            &NoPolicyOverrides,
            &args,
            &conn,
            "2026-01-01T00:00:00Z",
            &ctx,
        )
        .await;

        assert!(CALLED.load(Ordering::SeqCst));
        assert_eq!(
            admission,
            ToolResult::Ok {
                data: Some(args),
                message: None
            }
        );
    }

    #[test]
    fn off_wire_context_has_no_progress_token_or_client_identity() {
        let registry = WaiterRegistry::new();
        let ctx = ToolCallContext::off_wire(&registry);
        assert!(!ctx.progress_token_present);
        assert_eq!(ctx.client_name, None);
        assert!(ctx.progress_sink.is_none());
    }

    struct FakeSink {
        sent: std::sync::atomic::AtomicU32,
    }

    impl ProgressSink for FakeSink {
        fn notify_progress<'a>(&'a self, _progress: f64) -> BoxFuture<'a, bool> {
            Box::pin(async move {
                self.sent.fetch_add(1, Ordering::SeqCst);
                true
            })
        }
    }

    #[tokio::test]
    async fn a_tool_can_reach_the_progress_sink_through_the_context() {
        // Proves the wiring, not just the type: a Tool::call impl reads
        // ctx.progress_sink and actually invokes it -- the same shape
        // wait_for_events's real heartbeat call will use.
        struct HeartbeatingTool;
        impl Tool for HeartbeatingTool {
            const NAME: &'static str = "heartbeating";
            const REQUIRED: Requirement = Requirement::Public;
            const DESCRIPTION: &'static str = "Sends one heartbeat then returns.";
            const SCHEMA: &'static str = r#"{"type":"object"}"#;
            fn call<'a>(
                _principal: Option<&'a Principal>,
                _arguments: &'a Value,
                _conn: &'a AsyncMutex<Connection>,
                _now: &'a str,
                ctx: &'a ToolCallContext<'a>,
            ) -> BoxFuture<'a, ToolResult> {
                Box::pin(async move {
                    let sent = match ctx.progress_sink {
                        Some(sink) => sink.notify_progress(1.0).await,
                        None => false,
                    };
                    ToolResult::Ok {
                        data: Some(serde_json::json!({"heartbeat_sent": sent})),
                        message: None,
                    }
                })
            }
        }

        let registry = WaiterRegistry::new();
        let sink = FakeSink {
            sent: std::sync::atomic::AtomicU32::new(0),
        };
        let ctx = ToolCallContext {
            progress_token_present: true,
            client_name: Some("claude-code"),
            progress_sink: Some(&sink),
            waiter_registry: &registry,
        };
        let conn = AsyncMutex::new(Connection::open_in_memory().unwrap());
        let result =
            HeartbeatingTool::call(None, &Value::Null, &conn, "2026-01-01T00:00:00Z", &ctx).await;
        assert_eq!(
            result,
            ToolResult::Ok {
                data: Some(serde_json::json!({"heartbeat_sent": true})),
                message: None,
            }
        );
        assert_eq!(sink.sent.load(Ordering::SeqCst), 1);
    }
}
