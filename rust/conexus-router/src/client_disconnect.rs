//! Peer-disconnect vocabulary shared by the router's request paths.
//! Port of `agent_mcp/router/client_disconnect.py` (60 LOC, part of
//! Phase E2 PR 15, `conexus-router-headers-misc`).
//!
//! **`client_is_gone` has no function to port** -- it reads
//! `request.transport.is_closing()`, a live fact only a real
//! `hyper`/`axum` connection can supply (PR 23, app-wiring); this
//! framework-agnostic layer has no transport type yet, so a caller
//! there already HAS the bool this Python function would compute and
//! has no need to call back into a pure function for it. Only the
//! constant + response-shape half is genuinely portable now.
#![allow(dead_code)]

use crate::mcp_handler::{HandlerBody, HandlerResponse};

/// nginx's non-standard "Client Closed Request" -- never reaches a
/// wire (by definition the peer is gone), but keeps the access log
/// honest about how the request ended.
pub const CLIENT_GONE_STATUS: u16 = 499;

/// The response to return once the peer has provably gone away.
pub fn client_gone_response() -> HandlerResponse {
    HandlerResponse {
        status: CLIENT_GONE_STATUS,
        headers: vec![],
        body: HandlerBody::Empty,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn client_gone_response_is_499_with_an_empty_body() {
        let resp = client_gone_response();
        assert_eq!(resp.status, 499);
        assert!(matches!(resp.body, HandlerBody::Empty));
    }
}
