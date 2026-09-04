//! Provider-agnostic text-embedding HTTP client. Port of
//! `agent_mcp/external/embedding_service.py`.
//!
//! Branches OpenAI-vs-Ollama on `OPENAI_API_KEY`, exactly like Python
//! — the same switch [`crate::completion_client`] uses, so the two
//! seams never disagree about which provider is live.
//!
//! Two deliberate departures from a literal port:
//!
//! - **No env-var-mutation bootstrap.** Python's `core/config.py`
//!   `setdefault()`s `OPENAI_API_KEY=ollama` / `OPENAI_BASE_URL=...`
//!   etc at process-import time when the operator hasn't set
//!   `OPENAI_API_KEY`, so that BY THE TIME `embedding_client()` runs,
//!   its own `OPENAI_API_KEY` check always sees a value (either the
//!   operator's, or the "ollama" sentinel) — a `conexus-backend`
//!   process has no equivalent bootstrap step to port. [`resolve`]
//!   folds that same "no key ⇒ local Ollama defaults" decision
//!   directly into ONE function instead, computed fresh from whatever
//!   the caller hands it — no process env mutation, no import-order
//!   dependency to get right.
//! - **Explicit env lookup, not a hidden `std::env::var` read inside
//!   the resolver.** [`resolve`] takes a `get_env: impl Fn(&str) ->
//!   Option<String>` instead of reading the process environment
//!   itself — this crate's established "explicit input over hidden
//!   state" convention (`conexus_auth::capabilities::
//!   resolve_capabilities`'s `router_conn: Option<&Connection>`), and
//!   it sidesteps a real hazard a literal `std::env::var` read would
//!   create: `cargo test`'s default parallel test execution runs many
//!   tests as threads in ONE process, and `std::env::set_var`/
//!   `remove_var` mutate PROCESS-GLOBAL state — the exact "shared
//!   global racing under parallel tests" bug class this workspace has
//!   already hit twice (`conexus-vec`'s extension-registration lock,
//!   `conexus-auth::tool`'s `CALLED` static). [`resolve_from_process_env`]
//!   is the one real call site that reads the actual environment;
//!   every test below drives [`resolve`] directly with an in-memory
//!   map, so there is nothing to race.
//!
//! No client-instance cache (unlike Python's `_client_cache`, added
//! there to fix a real connection-leak bug, R12-F3): a `reqwest::Client`
//! isn't bound to one target host the way the `openai` SDK's client
//! is, so ONE process-wide [`HTTP_CLIENT`] already serves every
//! (base_url, model, dimension) combination via its own internal
//! connection pool — there is no per-config object to leak in the
//! first place.

use std::sync::LazyLock;
use std::time::Duration;

use serde::Deserialize;
use serde_json::json;

const OLLAMA_DEFAULT_BASE_URL: &str = "http://localhost:11434/v1";
const OPENAI_DEFAULT_BASE_URL: &str = "https://api.openai.com/v1";

/// `AGENT_MCP_LLM_CLIENT_TIMEOUT_SECONDS` default -- R12-F2 defense in
/// depth (see Python's identically-named constant): an unreachable
/// provider must degrade in a bounded number of seconds, not the HTTP
/// stack's own much longer default.
const DEFAULT_CLIENT_TIMEOUT_SECS: u64 = 30;

/// One process-wide client. See module doc for why this replaces
/// Python's per-config-tuple `_client_cache` rather than porting it.
static HTTP_CLIENT: LazyLock<reqwest::Client> = LazyLock::new(|| {
    reqwest::Client::builder()
        .timeout(Duration::from_secs(DEFAULT_CLIENT_TIMEOUT_SECS))
        .build()
        .expect("reqwest client with a plain timeout always builds")
});

fn env_nonempty(get_env: &impl Fn(&str) -> Option<String>, key: &str) -> Option<String> {
    get_env(key)
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

/// A resolved embedding endpoint, ready to call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EmbeddingClient {
    pub base_url: String,
    pub api_key: String,
    pub model: String,
    pub dimension: u32,
}

/// Resolve an [`EmbeddingClient`] from an env-lookup function. See the
/// module doc for why this takes a function rather than reading
/// `std::env` directly.
///
/// `OPENAI_API_KEY` set ⇒ OpenAI-shaped resolution: `base_url` from
/// `OPENAI_BASE_URL` (cloud default otherwise), model/dimension
/// default to `text-embedding-3-large`/1536 (Python's
/// `SIMPLE_EMBEDDING_MODEL`/`SIMPLE_EMBEDDING_DIMENSION` OpenAI
/// fallback -- `conexus-backend` has no `--advanced` flag yet, so the
/// advanced-mode dimension/model never applies here; see
/// `conexus-backend`'s own "deliberately not ported" list).
///
/// `OPENAI_API_KEY` unset/empty ⇒ local Ollama: `base_url` from
/// `AGENT_MCP_LLM_BASE_URL` (verified against Python's actual
/// `OllamaEmbeddingClient.__init__` -- NOT `OPENAI_BASE_URL`, despite
/// this module's own Python docstring claiming the embedding seam
/// "deliberately does NOT consult `AGENT_MCP_LLM_BASE_URL`"; that
/// claim only holds for the OpenAI branch), else the bundled-Ollama
/// default; `api_key` is the `"ollama"` sentinel; model/dimension
/// default to `qwen3-embedding:0.6b`/1024 (Python's own
/// zero-config-VM defaults).
///
/// `AGENT_MCP_EMBEDDING_MODEL`/`AGENT_MCP_EMBEDDING_DIMENSION`, when
/// present, override the model/dimension in EITHER branch (matches
/// Python: an explicit setting always wins over either provider's
/// default).
pub fn resolve(get_env: impl Fn(&str) -> Option<String>) -> EmbeddingClient {
    let model_override = env_nonempty(&get_env, "AGENT_MCP_EMBEDDING_MODEL");
    let dimension_override =
        env_nonempty(&get_env, "AGENT_MCP_EMBEDDING_DIMENSION").and_then(|v| v.parse::<u32>().ok());

    match env_nonempty(&get_env, "OPENAI_API_KEY") {
        Some(api_key) => EmbeddingClient {
            base_url: env_nonempty(&get_env, "OPENAI_BASE_URL")
                .unwrap_or_else(|| OPENAI_DEFAULT_BASE_URL.to_string()),
            api_key,
            model: model_override.unwrap_or_else(|| "text-embedding-3-large".to_string()),
            dimension: dimension_override.unwrap_or(1536),
        },
        None => EmbeddingClient {
            base_url: env_nonempty(&get_env, "AGENT_MCP_LLM_BASE_URL")
                .unwrap_or_else(|| OLLAMA_DEFAULT_BASE_URL.to_string()),
            api_key: "ollama".to_string(),
            model: model_override.unwrap_or_else(|| "qwen3-embedding:0.6b".to_string()),
            dimension: dimension_override.unwrap_or(1024),
        },
    }
}

/// The one real call site: resolve from the actual process
/// environment. Every test drives [`resolve`] directly instead (see
/// module doc).
pub fn resolve_from_process_env() -> EmbeddingClient {
    resolve(|key| std::env::var(key).ok())
}

#[derive(Deserialize)]
struct EmbeddingItem {
    embedding: Vec<f32>,
}

#[derive(Deserialize)]
struct EmbeddingResponse {
    data: Vec<EmbeddingItem>,
}

/// Error embedding `texts`. Deliberately opaque (no reqwest internals
/// leaked) -- the one caller (Phase D2's `ask_project_rag`) maps any
/// `Err` here to the same `RAG_ERR_PROVIDER_UNAVAILABLE` sentinel
/// Python's `openai.APIError` arm produces, per SD-R9-1 (never echo
/// provider/transport detail to a worker).
#[derive(Debug)]
pub struct EmbedError(String);

impl std::fmt::Display for EmbedError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "embedding request failed: {}", self.0)
    }
}
impl std::error::Error for EmbedError {}

impl EmbeddingClient {
    /// Embed `texts`, one vector per input, in order. Mirrors Python's
    /// async `aembed` (the ONLY variant `ask_project_rag` may call --
    /// see Python's own `embed()` doc on why a sync/blocking embed
    /// call must never run on a shared server event loop, R12-F2).
    pub async fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>, EmbedError> {
        let url = format!("{}/embeddings", self.base_url.trim_end_matches('/'));
        let resp = HTTP_CLIENT
            .post(&url)
            .bearer_auth(&self.api_key)
            .json(&json!({
                "model": self.model,
                "input": texts,
                "dimensions": self.dimension,
            }))
            .send()
            .await
            .map_err(|e| EmbedError(e.to_string()))?;

        if !resp.status().is_success() {
            return Err(EmbedError(format!("HTTP {}", resp.status())));
        }
        let parsed: EmbeddingResponse = resp.json().await.map_err(|e| EmbedError(e.to_string()))?;
        Ok(parsed.data.into_iter().map(|item| item.embedding).collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |key| map.get(key).cloned()
    }

    #[test]
    fn no_openai_key_resolves_to_local_ollama_defaults() {
        let client = resolve(env(&[]));
        assert_eq!(
            client,
            EmbeddingClient {
                base_url: OLLAMA_DEFAULT_BASE_URL.to_string(),
                api_key: "ollama".to_string(),
                model: "qwen3-embedding:0.6b".to_string(),
                dimension: 1024,
            }
        );
    }

    #[test]
    fn ollama_branch_honours_agent_mcp_llm_base_url_override() {
        let client = resolve(env(&[(
            "AGENT_MCP_LLM_BASE_URL",
            "http://gpu-box:11434/v1",
        )]));
        assert_eq!(client.base_url, "http://gpu-box:11434/v1");
    }

    #[test]
    fn openai_key_set_resolves_to_openai_cloud_defaults() {
        let client = resolve(env(&[("OPENAI_API_KEY", "sk-real")]));
        assert_eq!(
            client,
            EmbeddingClient {
                base_url: OPENAI_DEFAULT_BASE_URL.to_string(),
                api_key: "sk-real".to_string(),
                model: "text-embedding-3-large".to_string(),
                dimension: 1536,
            }
        );
    }

    #[test]
    fn openai_branch_honours_openai_base_url_override() {
        let client = resolve(env(&[
            ("OPENAI_API_KEY", "sk-real"),
            ("OPENAI_BASE_URL", "https://my-gateway.example/v1"),
        ]));
        assert_eq!(client.base_url, "https://my-gateway.example/v1");
    }

    #[test]
    fn explicit_model_and_dimension_override_win_in_either_branch() {
        let ollama = resolve(env(&[
            ("AGENT_MCP_EMBEDDING_MODEL", "custom-model"),
            ("AGENT_MCP_EMBEDDING_DIMENSION", "768"),
        ]));
        assert_eq!(ollama.model, "custom-model");
        assert_eq!(ollama.dimension, 768);

        let openai = resolve(env(&[
            ("OPENAI_API_KEY", "sk-real"),
            ("AGENT_MCP_EMBEDDING_MODEL", "custom-model"),
            ("AGENT_MCP_EMBEDDING_DIMENSION", "768"),
        ]));
        assert_eq!(openai.model, "custom-model");
        assert_eq!(openai.dimension, 768);
    }

    #[test]
    fn whitespace_only_openai_api_key_is_treated_as_unset() {
        let client = resolve(env(&[("OPENAI_API_KEY", "   ")]));
        assert_eq!(
            client.api_key, "ollama",
            "must fall through to the Ollama branch"
        );
    }

    #[test]
    fn an_unparseable_dimension_override_falls_back_to_the_provider_default() {
        let client = resolve(env(&[("AGENT_MCP_EMBEDDING_DIMENSION", "not-a-number")]));
        assert_eq!(client.dimension, 1024);
    }

    // ── embed() against a real local HTTP server ────────────────────

    async fn spawn_fake_embeddings_server(
        status: u16,
        body: &'static str,
    ) -> (String, tokio::task::JoinHandle<()>) {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut buf = [0u8; 4096];
            let _ = socket.read(&mut buf).await;
            let response = format!(
                "HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = socket.write_all(response.as_bytes()).await;
            let _ = socket.shutdown().await;
        });
        (format!("http://{addr}"), handle)
    }

    #[tokio::test]
    async fn embed_parses_the_openai_compatible_response_shape() {
        let (base_url, handle) = spawn_fake_embeddings_server(
            200,
            r#"{"data":[{"embedding":[0.1,0.2,0.3]},{"embedding":[0.4,0.5,0.6]}]}"#,
        )
        .await;
        let client = EmbeddingClient {
            base_url,
            api_key: "test".to_string(),
            model: "test-model".to_string(),
            dimension: 3,
        };
        let vectors = client
            .embed(&["a".to_string(), "b".to_string()])
            .await
            .unwrap();
        assert_eq!(vectors, vec![vec![0.1, 0.2, 0.3], vec![0.4, 0.5, 0.6]]);
        handle.await.unwrap();
    }

    #[tokio::test]
    async fn embed_returns_an_error_on_a_non_success_status() {
        let (base_url, handle) = spawn_fake_embeddings_server(500, r#"{"error":"boom"}"#).await;
        let client = EmbeddingClient {
            base_url,
            api_key: "test".to_string(),
            model: "test-model".to_string(),
            dimension: 3,
        };
        let err = client.embed(&["a".to_string()]).await.unwrap_err();
        assert!(err.to_string().contains("500"));
        handle.await.unwrap();
    }

    #[tokio::test]
    async fn embed_returns_an_error_on_an_unexpected_response_shape() {
        let (base_url, handle) = spawn_fake_embeddings_server(200, r#"{"unexpected":true}"#).await;
        let client = EmbeddingClient {
            base_url,
            api_key: "test".to_string(),
            model: "test-model".to_string(),
            dimension: 3,
        };
        let err = client.embed(&["a".to_string()]).await.unwrap_err();
        assert!(!err.to_string().is_empty());
        handle.await.unwrap();
    }
}
