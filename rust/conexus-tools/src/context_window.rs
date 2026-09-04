//! Runtime discovery of the chat model's context window + budget
//! derivation. Port of `agent_mcp/external/context_window.py`.
//!
//! Local inference servers (llama-cpp, Ollama) run a FIXED per-slot
//! context window; overflowing it makes the completion call fail.
//! Rather than hardcode that window per deploy, this module discovers
//! it at runtime (`GET {base_url}/props`, llama-cpp's own introspection
//! endpoint) and derives the RAG retrieved-context budget from it, so
//! budgets adapt to whatever host `conexus-backend` runs on.
//!
//! Same "explicit `get_env` lookup, not a hidden `std::env::var` read"
//! discipline as [`crate::embedding_client`]/[`crate::completion_client`]
//! for the override paths -- see that module's doc for why.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Duration;

use serde::Deserialize;

const DEFAULT_WINDOW: u64 = 4096;
/// When the window is UNDISCOVERABLE (no base_url, endpoint
/// unreachable, or a non-llama-cpp endpoint such as cloud OpenAI or
/// Ollama), the budget must stay exactly as it was before discovery
/// existed: unbounded. Matches Python's `_LEGACY_UNBOUNDED_BUDGET`
/// (GPT-4.1's context window) so cloud deploys never regress.
const LEGACY_UNBOUNDED_BUDGET: u64 = 1_000_000;

const ANSWER_RESERVE_TOKENS: u64 = 1024;
const PROMPT_OVERHEAD_TOKENS: u64 = 512;
const MAX_TOKENS_PER_WORD: u64 = 2;
const MIN_BUDGET: u64 = 256;

const PROPS_TIMEOUT_SECS: u64 = 3;

fn env_nonempty(get_env: &impl Fn(&str) -> Option<String>, key: &str) -> Option<String> {
    get_env(key)
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

#[derive(Deserialize)]
struct GenerationSettings {
    n_ctx: Option<u64>,
}
#[derive(Deserialize)]
struct PropsResponse {
    default_generation_settings: Option<GenerationSettings>,
}

/// GET `{base_url}/props` and read
/// `default_generation_settings.n_ctx`. Any failure (endpoint down,
/// non-llama-cpp server, unexpected shape) returns `None` so the
/// caller falls back to the default -- matches Python's blanket
/// `except Exception` around this probe (a probe failure is never a
/// hard error, only a signal to use the fallback).
async fn probe_props(base_url: &str) -> Option<u64> {
    // base_url is an OpenAI-style ".../v1"; /props sits at the server
    // root, matching Python's own strip.
    let root = base_url
        .trim_end_matches('/')
        .strip_suffix("/v1")
        .unwrap_or(base_url.trim_end_matches('/'));
    let url = format!("{root}/props");
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(PROPS_TIMEOUT_SECS))
        .build()
        .ok()?;
    let resp = client.get(&url).send().await.ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let parsed: PropsResponse = resp.json().await.ok()?;
    parsed.default_generation_settings?.n_ctx.filter(|&n| n > 0)
}

/// Per-endpoint cache: the window is constant for a server's lifetime.
/// Caches BOTH outcomes (a successful probe and a failed one) so a
/// cloud endpoint without `/props` isn't re-probed (and made to eat
/// the timeout) on every single query -- matches Python's
/// `_WINDOW_CACHE`.
static WINDOW_CACHE: Mutex<Option<HashMap<String, Option<u64>>>> = Mutex::new(None);

fn cache_get(base_url: &str) -> Option<Option<u64>> {
    WINDOW_CACHE
        .lock()
        .unwrap()
        .as_ref()?
        .get(base_url)
        .copied()
}

fn cache_set(base_url: &str, value: Option<u64>) {
    let mut guard = WINDOW_CACHE.lock().unwrap();
    guard
        .get_or_insert_with(HashMap::new)
        .insert(base_url.to_string(), value);
}

/// The chat model's usable per-slot context window in tokens, or
/// `None` when it can't be determined.
///
/// Resolution order: (1) `AGENT_MCP_MODEL_CONTEXT_WINDOW` explicit
/// override; (2) a cached probe of `{base_url}/props`; (3) `None` when
/// no `base_url` is given.
pub async fn resolve_context_window(
    get_env: impl Fn(&str) -> Option<String>,
    base_url: Option<&str>,
) -> Option<u64> {
    if let Some(v) = env_nonempty(&get_env, "AGENT_MCP_MODEL_CONTEXT_WINDOW") {
        return Some(v.parse::<u64>().unwrap_or(DEFAULT_WINDOW));
    }
    let base_url = base_url?;
    if let Some(cached) = cache_get(base_url) {
        return cached;
    }
    let probed = probe_props(base_url).await;
    cache_set(base_url, probed);
    probed
}

/// RAG retrieved-context budget, in WORDS (the assembler counts
/// context in words, not tokens -- see Python's own module doc on the
/// word-vs-token unit mismatch this derivation reconciles by dividing
/// by the worst-case tokens/word ratio).
///
/// Override-on-top: an explicit `AGENT_MCP_MAX_CONTEXT_TOKENS` always
/// wins; otherwise derived from the discovered window, or unbounded
/// (today's Python behaviour) when the window is undiscoverable.
pub async fn resolve_max_context_tokens(
    get_env: impl Fn(&str) -> Option<String>,
    base_url: Option<&str>,
) -> u64 {
    if let Some(v) = env_nonempty(&get_env, "AGENT_MCP_MAX_CONTEXT_TOKENS") {
        return v.parse::<u64>().unwrap_or(LEGACY_UNBOUNDED_BUDGET);
    }
    let Some(window) = resolve_context_window(get_env, base_url).await else {
        return LEGACY_UNBOUNDED_BUDGET;
    };
    let context_tokens = window.saturating_sub(ANSWER_RESERVE_TOKENS + PROMPT_OVERHEAD_TOKENS);
    (context_tokens / MAX_TOKENS_PER_WORD).max(MIN_BUDGET)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |key| map.get(key).cloned()
    }

    #[tokio::test]
    async fn no_base_url_and_no_override_is_undiscoverable() {
        assert_eq!(resolve_context_window(env(&[]), None).await, None);
    }

    #[tokio::test]
    async fn explicit_window_override_wins_without_needing_a_base_url() {
        let value =
            resolve_context_window(env(&[("AGENT_MCP_MODEL_CONTEXT_WINDOW", "8192")]), None).await;
        assert_eq!(value, Some(8192));
    }

    #[tokio::test]
    async fn a_malformed_window_override_falls_back_to_the_default() {
        let value = resolve_context_window(
            env(&[("AGENT_MCP_MODEL_CONTEXT_WINDOW", "not-a-number")]),
            None,
        )
        .await;
        assert_eq!(value, Some(DEFAULT_WINDOW));
    }

    #[tokio::test]
    async fn undiscoverable_window_yields_the_legacy_unbounded_budget() {
        let budget = resolve_max_context_tokens(env(&[]), None).await;
        assert_eq!(budget, LEGACY_UNBOUNDED_BUDGET);
    }

    #[tokio::test]
    async fn explicit_max_context_tokens_override_wins_regardless_of_window() {
        let budget =
            resolve_max_context_tokens(env(&[("AGENT_MCP_MAX_CONTEXT_TOKENS", "12345")]), None)
                .await;
        assert_eq!(budget, 12345);
    }

    #[tokio::test]
    async fn budget_is_derived_from_an_explicit_window_override() {
        // (8192 - 1024 - 512) / 2 = 3328
        let budget =
            resolve_max_context_tokens(env(&[("AGENT_MCP_MODEL_CONTEXT_WINDOW", "8192")]), None)
                .await;
        assert_eq!(budget, 3328);
    }

    #[tokio::test]
    async fn a_degenerate_window_floors_at_the_minimum_budget() {
        let budget =
            resolve_max_context_tokens(env(&[("AGENT_MCP_MODEL_CONTEXT_WINDOW", "1000")]), None)
                .await;
        assert_eq!(budget, MIN_BUDGET);
    }

    // ── probe_props / resolve_context_window against a real server ──

    async fn spawn_fake_props_server(
        path_and_body: &'static str,
    ) -> (String, tokio::task::JoinHandle<()>) {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut buf = [0u8; 4096];
            let _ = socket.read(&mut buf).await;
            let _ = socket.write_all(path_and_body.as_bytes()).await;
            let _ = socket.shutdown().await;
        });
        (format!("http://{addr}/v1"), handle)
    }

    fn http_ok_json(body: &str) -> String {
        format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
    }

    #[tokio::test]
    async fn resolve_context_window_probes_props_and_strips_the_v1_suffix() {
        let body = http_ok_json(r#"{"default_generation_settings":{"n_ctx":8192}}"#);
        let leaked: &'static str = Box::leak(body.into_boxed_str());
        let (base_url, handle) = spawn_fake_props_server(leaked).await;
        let window = resolve_context_window(env(&[]), Some(&base_url)).await;
        assert_eq!(window, Some(8192));
        handle.await.unwrap();
    }

    #[tokio::test]
    async fn resolve_context_window_returns_none_on_an_unreachable_endpoint() {
        // Nothing bound on this port -- connection refused.
        let window = resolve_context_window(env(&[]), Some("http://127.0.0.1:1")).await;
        assert_eq!(window, None);
    }
}
