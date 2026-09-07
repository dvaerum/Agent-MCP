//! Ollama-backed subject-suggestion helper for `agent_messages`. Port
//! of `agent_mcp/features/message_suggestions.py`.
//!
//! When a sender doesn't supply a `subject` for a root message, this
//! module asks a local Ollama (or any OpenAI `/v1`-compatible)
//! endpoint to produce a one-line summary of the body.
//!
//! Deliberately does NOT reuse [`crate::completion_client::resolve`]
//! (this crate's RAG-facing completion resolver) even though both end
//! up talking to an OpenAI-shaped `/chat/completions` endpoint --
//! Python's own module doc explains why: this module always wants a
//! LOCAL Ollama endpoint regardless of whatever provider RAG
//! embeddings/completions are configured with for this deploy, so the
//! two config surfaces (`AGENT_MCP_SUBJECT_MODEL`/`AGENT_MCP_LLM_BASE_URL`
//! here vs. `OPENAI_API_KEY`/`OLLAMA_MODEL` there) are kept genuinely
//! separate. [`crate::completion_client::CompletionClient`] itself
//! (the struct + its `chat()` method) IS reused directly -- only the
//! *resolution* is bespoke.
//!
//! Same "explicit `get_env` lookup, not a hidden `std::env::var` read"
//! discipline as every other external-service module in this crate
//! (parallel-test-safety).

use crate::completion_client::CompletionClient;
use crate::context_window::resolve_subject_input_chars;

const DEFAULT_BASE_URL: &str = "http://localhost:11434/v1";
const SYSTEM_PROMPT: &str = "Summarize the user's message in 6 words or fewer as an email-style \
     subject line. Return only the subject text, no quotes, no prefix, no punctuation at the \
     end.";
/// Hard ceiling regardless of what the model returns.
const MAX_SUBJECT_LEN: usize = 80;
const MAX_COMPLETION_TOKENS: u32 = 32;
const TEMPERATURE: f64 = 0.2;

fn env_nonempty(get_env: &impl Fn(&str) -> Option<String>, key: &str) -> Option<String> {
    get_env(key)
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

/// True when `AGENT_MCP_SUBJECT_MODEL` is set (non-empty) -- no model
/// configured means nothing to generate with.
pub fn subject_model_configured(get_env: &impl Fn(&str) -> Option<String>) -> bool {
    env_nonempty(get_env, "AGENT_MCP_SUBJECT_MODEL").is_some()
}

/// Trim whitespace, strip enclosing quotes, collapse internal
/// newlines to spaces, and cap at [`MAX_SUBJECT_LEN`].
fn truncate(subject: &str) -> String {
    let mut out = subject.trim().to_string();
    let bytes_len = out.len();
    if bytes_len >= 2 {
        let starts_quote = out.starts_with('"') || out.starts_with('\'');
        let ends_quote = out.ends_with('"') || out.ends_with('\'');
        if starts_quote && ends_quote {
            out = out[1..out.len() - 1].trim().to_string();
        }
    }
    // Collapse internal whitespace runs (incl. newlines) to a single
    // space -- a subject is a one-liner.
    out = out.split_whitespace().collect::<Vec<_>>().join(" ");
    if out.chars().count() > MAX_SUBJECT_LEN {
        let truncated: String = out.chars().take(MAX_SUBJECT_LEN - 3).collect();
        out = format!("{}...", truncated.trim_end());
    }
    out
}

/// Ask the configured Ollama model for a one-line subject.
///
/// Returns `None` when: `AGENT_MCP_SUBJECT_MODEL` is unset; the HTTP
/// call fails for any reason; or the model returns an empty /
/// whitespace-only completion. Returns the (trimmed, length-capped)
/// subject string otherwise.
pub async fn suggest_subject(
    get_env: impl Fn(&str) -> Option<String>,
    content: &str,
) -> Option<String> {
    let model = env_nonempty(&get_env, "AGENT_MCP_SUBJECT_MODEL")?;
    let base_url = env_nonempty(&get_env, "AGENT_MCP_LLM_BASE_URL")
        .unwrap_or_else(|| DEFAULT_BASE_URL.to_string());

    // Head-truncate so the input can never overflow the model's
    // context window -- see context_window's own module doc.
    let max_input_chars = resolve_subject_input_chars(&get_env, Some(&base_url)).await as usize;
    let content: String = if content.chars().count() > max_input_chars {
        content.chars().take(max_input_chars).collect()
    } else {
        content.to_string()
    };

    let client = CompletionClient {
        base_url,
        api_key: "ollama".to_string(),
        model,
    };

    let raw = client
        .chat(
            &[("system", SYSTEM_PROMPT), ("user", &content)],
            TEMPERATURE,
            Some(MAX_COMPLETION_TOKENS),
        )
        .await
        .ok()?;

    let out = truncate(&raw);
    if out.is_empty() {
        None
    } else {
        Some(out)
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
    fn model_unconfigured_by_default() {
        assert!(!subject_model_configured(&env(&[])));
    }

    #[test]
    fn model_configured_when_set_nonempty() {
        assert!(subject_model_configured(&env(&[(
            "AGENT_MCP_SUBJECT_MODEL",
            "qwen2.5:3b-instruct"
        )])));
    }

    #[test]
    fn whitespace_only_model_env_counts_as_unconfigured() {
        assert!(!subject_model_configured(&env(&[(
            "AGENT_MCP_SUBJECT_MODEL",
            "   "
        )])));
    }

    #[test]
    fn truncate_trims_and_strips_matching_quotes() {
        assert_eq!(truncate("  \"Deploy failed\"  "), "Deploy failed");
    }

    #[test]
    fn truncate_leaves_a_lone_quote_character_alone() {
        // Not a matching pair -- must not eat a real leading quote.
        assert_eq!(truncate("\"unbalanced"), "\"unbalanced");
    }

    #[test]
    fn truncate_collapses_internal_newlines_to_spaces() {
        assert_eq!(truncate("line one\nline two"), "line one line two");
    }

    #[test]
    fn truncate_caps_at_the_max_length_with_an_ellipsis() {
        let long = "a".repeat(200);
        let out = truncate(&long);
        assert_eq!(out.chars().count(), MAX_SUBJECT_LEN);
        assert!(out.ends_with("..."));
    }

    #[tokio::test]
    async fn suggest_subject_is_none_when_the_model_is_unconfigured() {
        let out = suggest_subject(env(&[]), "hello world").await;
        assert_eq!(out, None);
    }

    #[tokio::test]
    async fn suggest_subject_is_none_when_the_endpoint_is_unreachable() {
        // Nothing bound on this port -- connection refused, must
        // degrade to None rather than propagate an error.
        let out = suggest_subject(
            env(&[
                ("AGENT_MCP_SUBJECT_MODEL", "qwen2.5:3b-instruct"),
                ("AGENT_MCP_LLM_BASE_URL", "http://127.0.0.1:1/v1"),
            ]),
            "hello world",
        )
        .await;
        assert_eq!(out, None);
    }

    #[tokio::test]
    async fn suggest_subject_calls_the_real_endpoint_and_returns_a_titled_subject() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut buf = [0u8; 8192];
            let _ = socket.read(&mut buf).await;
            let body =
                r#"{"choices":[{"message":{"content":"  \"Deploy failed on staging\"  "}}]}"#;
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = socket.write_all(resp.as_bytes()).await;
            let _ = socket.shutdown().await;
        });

        let base_url = format!("http://{addr}/v1");
        let out = suggest_subject(
            env(&[
                ("AGENT_MCP_SUBJECT_MODEL", "qwen2.5:3b-instruct"),
                ("AGENT_MCP_LLM_BASE_URL", &base_url),
                // Pins the context window so resolve_subject_input_chars
                // skips its own `/props` HTTP probe entirely -- this
                // fake server only ever accepts ONE connection (for
                // the real /chat/completions call this test is
                // actually exercising), and an unguarded probe would
                // consume that single connection first.
                ("AGENT_MCP_MODEL_CONTEXT_WINDOW", "4096"),
            ]),
            "the deploy to staging just failed with a timeout",
        )
        .await;
        assert_eq!(out, Some("Deploy failed on staging".to_string()));
        handle.await.unwrap();
    }

    #[tokio::test]
    async fn suggest_subject_is_none_on_an_empty_completion() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut buf = [0u8; 8192];
            let _ = socket.read(&mut buf).await;
            let body = r#"{"choices":[{"message":{"content":"   "}}]}"#;
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = socket.write_all(resp.as_bytes()).await;
            let _ = socket.shutdown().await;
        });

        let base_url = format!("http://{addr}/v1");
        let out = suggest_subject(
            env(&[
                ("AGENT_MCP_SUBJECT_MODEL", "qwen2.5:3b-instruct"),
                ("AGENT_MCP_LLM_BASE_URL", &base_url),
                // See the sibling test above for why this is pinned.
                ("AGENT_MCP_MODEL_CONTEXT_WINDOW", "4096"),
            ]),
            "hello",
        )
        .await;
        assert_eq!(out, None);
        handle.await.unwrap();
    }
}
