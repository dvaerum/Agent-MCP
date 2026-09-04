//! Provider-agnostic chat-completion HTTP client. Port of
//! `agent_mcp/external/completion_service.py`.
//!
//! Same design departures as [`crate::embedding_client`] (see that
//! module's doc for the full rationale): no env-mutation bootstrap to
//! port, [`resolve`] takes an explicit `get_env` lookup rather than
//! reading the process environment (parallel-test-safety), and no
//! per-config client cache (`HTTP_CLIENT` is one process-wide
//! `reqwest::Client`, not bound to a single target the way the
//! `openai` SDK's client is).
//!
//! Branches on `OPENAI_API_KEY`:
//! - **Set + `OPENAI_MODEL` set** → OpenAI-shaped resolution.
//! - **Set + `OPENAI_MODEL` unset** → [`CompletionConfigError`]. No
//!   silent fallback to a default model (Python's own v5.0.43
//!   incident: a hardcoded, nonexistent model name broke every
//!   OpenAI-configured deploy at once).
//! - **Unset/empty** → local Ollama, model from `OLLAMA_MODEL`
//!   (default `qwen3:1.7b`, matching the VM's bundled chat model).

use std::sync::LazyLock;
use std::time::Duration;

use serde::Deserialize;
use serde_json::{json, Value};

const OLLAMA_DEFAULT_BASE_URL: &str = "http://localhost:11434/v1";
const OLLAMA_DEFAULT_MODEL: &str = "qwen3:1.7b";
const DEFAULT_CLIENT_TIMEOUT_SECS: u64 = 30;

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

/// Raised by [`resolve`] when `OPENAI_API_KEY` is set without
/// `OPENAI_MODEL` -- see module doc.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompletionConfigError;

impl std::fmt::Display for CompletionConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "OPENAI_API_KEY is set but OPENAI_MODEL is not; the OpenAI provider requires an \
             explicit model"
        )
    }
}
impl std::error::Error for CompletionConfigError {}

/// A resolved chat-completion endpoint, ready to call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompletionClient {
    pub base_url: String,
    pub api_key: String,
    pub model: String,
}

/// Resolve a [`CompletionClient`] from an env-lookup function. See
/// module doc for the branch table.
pub fn resolve(
    get_env: impl Fn(&str) -> Option<String>,
) -> Result<CompletionClient, CompletionConfigError> {
    match env_nonempty(&get_env, "OPENAI_API_KEY") {
        Some(api_key) => {
            let model = env_nonempty(&get_env, "OPENAI_MODEL").ok_or(CompletionConfigError)?;
            // AGENT_MCP_LLM_BASE_URL overrides the chat endpoint for
            // EITHER provider (see this module's Python source doc);
            // unset falls back to OPENAI_BASE_URL (the SDK's own env
            // pickup this port replicates explicitly), then the cloud
            // default.
            let base_url = env_nonempty(&get_env, "AGENT_MCP_LLM_BASE_URL")
                .or_else(|| env_nonempty(&get_env, "OPENAI_BASE_URL"))
                .unwrap_or_else(|| "https://api.openai.com/v1".to_string());
            Ok(CompletionClient {
                base_url,
                api_key,
                model,
            })
        }
        None => {
            let model = env_nonempty(&get_env, "OLLAMA_MODEL")
                .unwrap_or_else(|| OLLAMA_DEFAULT_MODEL.to_string());
            let base_url = env_nonempty(&get_env, "AGENT_MCP_LLM_BASE_URL")
                .unwrap_or_else(|| OLLAMA_DEFAULT_BASE_URL.to_string());
            Ok(CompletionClient {
                base_url,
                api_key: "ollama".to_string(),
                model,
            })
        }
    }
}

/// The one real call site: resolve from the actual process
/// environment. Every test drives [`resolve`] directly instead.
pub fn resolve_from_process_env() -> Result<CompletionClient, CompletionConfigError> {
    resolve(|key| std::env::var(key).ok())
}

/// The chat/completion endpoint base URL, for introspection (used by
/// `context_window` to discover the chat model's context window).
/// Deliberately its OWN, simpler resolution than [`resolve`]'s
/// per-provider base_url -- Python's `resolve_chat_base_url` doesn't
/// fall back to the Ollama default when nothing is set, matching the
/// original source exactly (a probe with no URL just can't run).
pub fn resolve_chat_base_url(get_env: impl Fn(&str) -> Option<String>) -> Option<String> {
    env_nonempty(&get_env, "AGENT_MCP_LLM_BASE_URL")
        .or_else(|| env_nonempty(&get_env, "OPENAI_BASE_URL"))
}

#[derive(Deserialize)]
struct ChatChoice {
    message: ChatMessage,
}
#[derive(Deserialize)]
struct ChatMessage {
    content: Option<String>,
}
#[derive(Deserialize)]
struct ChatResponse {
    choices: Vec<ChatChoice>,
}

/// Error calling the chat-completion endpoint. Deliberately opaque, no
/// transport internals -- same SD-R9-1 discipline as
/// [`crate::embedding_client::EmbedError`].
#[derive(Debug)]
pub struct ChatError(String);

impl std::fmt::Display for ChatError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "chat completion request failed: {}", self.0)
    }
}
impl std::error::Error for ChatError {}

impl CompletionClient {
    /// Send a chat-completion request, return the assistant text (or
    /// `""` if the provider's `content` field was null, matching
    /// Python's `content or ""`).
    pub async fn chat(
        &self,
        messages: &[(&str, &str)],
        temperature: f64,
    ) -> Result<String, ChatError> {
        let url = format!("{}/chat/completions", self.base_url.trim_end_matches('/'));
        let messages_json: Vec<Value> = messages
            .iter()
            .map(|(role, content)| json!({"role": role, "content": content}))
            .collect();
        let resp = HTTP_CLIENT
            .post(&url)
            .bearer_auth(&self.api_key)
            .json(&json!({
                "model": self.model,
                "messages": messages_json,
                "temperature": temperature,
            }))
            .send()
            .await
            .map_err(|e| ChatError(e.to_string()))?;

        if !resp.status().is_success() {
            return Err(ChatError(format!("HTTP {}", resp.status())));
        }
        let parsed: ChatResponse = resp.json().await.map_err(|e| ChatError(e.to_string()))?;
        let content = parsed
            .choices
            .into_iter()
            .next()
            .ok_or_else(|| ChatError("no choices in response".to_string()))?
            .message
            .content
            .unwrap_or_default();
        Ok(content)
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
    fn no_openai_key_resolves_to_ollama_defaults() {
        let client = resolve(env(&[])).unwrap();
        assert_eq!(
            client,
            CompletionClient {
                base_url: OLLAMA_DEFAULT_BASE_URL.to_string(),
                api_key: "ollama".to_string(),
                model: OLLAMA_DEFAULT_MODEL.to_string(),
            }
        );
    }

    #[test]
    fn ollama_model_env_var_overrides_the_default() {
        let client = resolve(env(&[("OLLAMA_MODEL", "llama3:8b")])).unwrap();
        assert_eq!(client.model, "llama3:8b");
    }

    #[test]
    fn openai_key_without_model_is_a_config_error() {
        let err = resolve(env(&[("OPENAI_API_KEY", "sk-real")])).unwrap_err();
        assert_eq!(err, CompletionConfigError);
    }

    #[test]
    fn openai_key_and_model_resolve_to_the_cloud_default_base_url() {
        let client = resolve(env(&[
            ("OPENAI_API_KEY", "sk-real"),
            ("OPENAI_MODEL", "gpt-4.1"),
        ]))
        .unwrap();
        assert_eq!(
            client,
            CompletionClient {
                base_url: "https://api.openai.com/v1".to_string(),
                api_key: "sk-real".to_string(),
                model: "gpt-4.1".to_string(),
            }
        );
    }

    #[test]
    fn agent_mcp_llm_base_url_overrides_the_chat_endpoint_for_openai_too() {
        let client = resolve(env(&[
            ("OPENAI_API_KEY", "sk-real"),
            ("OPENAI_MODEL", "gpt-4.1"),
            ("AGENT_MCP_LLM_BASE_URL", "http://fast-igpu:11435/v1"),
            ("OPENAI_BASE_URL", "https://ignored.example/v1"),
        ]))
        .unwrap();
        assert_eq!(client.base_url, "http://fast-igpu:11435/v1");
    }

    #[test]
    fn openai_base_url_is_the_fallback_when_agent_mcp_llm_base_url_is_unset() {
        let client = resolve(env(&[
            ("OPENAI_API_KEY", "sk-real"),
            ("OPENAI_MODEL", "gpt-4.1"),
            ("OPENAI_BASE_URL", "https://my-gateway.example/v1"),
        ]))
        .unwrap();
        assert_eq!(client.base_url, "https://my-gateway.example/v1");
    }

    // ── resolve_chat_base_url ────────────────────────────────────────

    #[test]
    fn chat_base_url_is_none_when_nothing_is_set() {
        assert_eq!(resolve_chat_base_url(env(&[])), None);
    }

    #[test]
    fn chat_base_url_prefers_agent_mcp_llm_base_url_over_openai_base_url() {
        assert_eq!(
            resolve_chat_base_url(env(&[
                ("AGENT_MCP_LLM_BASE_URL", "http://a"),
                ("OPENAI_BASE_URL", "http://b"),
            ])),
            Some("http://a".to_string())
        );
    }

    // ── chat() against a real local HTTP server ─────────────────────

    async fn spawn_fake_chat_server(
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
    async fn chat_parses_the_openai_compatible_response_shape() {
        let (base_url, handle) =
            spawn_fake_chat_server(200, r#"{"choices":[{"message":{"content":"the answer"}}]}"#)
                .await;
        let client = CompletionClient {
            base_url,
            api_key: "test".to_string(),
            model: "test-model".to_string(),
        };
        let answer = client.chat(&[("user", "hi")], 0.4).await.unwrap();
        assert_eq!(answer, "the answer");
        handle.await.unwrap();
    }

    #[tokio::test]
    async fn chat_returns_empty_string_for_a_null_content_not_an_error() {
        let (base_url, handle) =
            spawn_fake_chat_server(200, r#"{"choices":[{"message":{"content":null}}]}"#).await;
        let client = CompletionClient {
            base_url,
            api_key: "test".to_string(),
            model: "test-model".to_string(),
        };
        let answer = client.chat(&[("user", "hi")], 0.4).await.unwrap();
        assert_eq!(answer, "");
        handle.await.unwrap();
    }

    #[tokio::test]
    async fn chat_returns_an_error_on_a_non_success_status() {
        let (base_url, handle) = spawn_fake_chat_server(500, r#"{"error":"boom"}"#).await;
        let client = CompletionClient {
            base_url,
            api_key: "test".to_string(),
            model: "test-model".to_string(),
        };
        let err = client.chat(&[("user", "hi")], 0.4).await.unwrap_err();
        assert!(err.to_string().contains("500"));
        handle.await.unwrap();
    }
}
