//! The `openidconnect` crate-integration layer: discovery fetch +
//! OBS-R17-SSO origin-pin + `CoreClient` construction. Port target:
//! `agent_mcp/router/sso.py`'s `_fetch_oidc_metadata`/
//! `_assert_discovery_same_origin`/`_origin_tuple` (Phase E2 PR22
//! step 5/8, `conexus-router-oidc-http-client`).
//!
//! Operator decision 2026-09-06: the full `openidconnect` crate over
//! a hand-rolled auth-code flow -- see `Cargo.toml`'s own comment.
//!
//! **Operator decision 2026-09-06 (typed-vs-raw ID-token claims)**:
//! `openidconnect`'s spec-typed `CoreIdTokenClaims` is used AS-IS for
//! `sub`/`email`/`preferred_username` -- no raw-JSON re-parse
//! fallback layer. This is a DELIBERATE divergence from Python's real
//! fail-open behavior (R16-F1->R20-F1, ~40 tests degrading a
//! malformed claim gracefully rather than crashing): a non-spec-
//! compliant claim now hard-fails that ID token's verification
//! (`IdToken::claims` returns `Err`) rather than degrading. See the
//! plan file's own "Operator decision (2026-09-06): PR22 typed-claims
//! question resolved" entry for the full rationale/trade-off. A real,
//! practical consequence: `SsoSubject`'s `Int`/`Float`/`Bool`
//! variants (PR1, still fully general and tested) are never
//! constructed from this real call site -- `CoreIdTokenClaims::
//! subject()` always yields a `SubjectIdentifier` (a string), or the
//! token fails verification before a caller ever sees it.
//!
//! **OBS-R17-SSO origin-pin kept explicit**, not delegated to the
//! crate: `authorization_endpoint`/`token_endpoint`/`jwks_uri` MUST
//! each share the configured issuer's scheme+host+port, closing an
//! SSRF-ish vector where a hostile/compromised IdP (or a MITM against
//! an `http://` issuer) could point these at an internal host. Ported
//! as a real post-discovery check on the parsed `CoreProviderMetadata`
//! rather than trusting `openidconnect`'s own issuer-matching alone
//! (unconfirmed whether it enforces the identical rule) -- matches
//! this migration's own "keep the explicit pentest-derived check"
//! precedent (PR12's session-gate research made the same call for a
//! different origin-pin).

use openidconnect::core::{CoreClient, CoreProviderMetadata};
use openidconnect::{
    ClientId, ClientSecret, EndpointMaybeSet, EndpointNotSet, EndpointSet, IssuerUrl, RedirectUrl,
};
use url::Url;

/// The exact typestate `build_oidc_client`'s `CoreClient` settles
/// into: `HasAuthUrl`/`HasTokenUrl` both `EndpointSet` (auth URL from
/// discovery; token URL promoted explicitly via `set_token_uri` below
/// -- `exchange_code`'s own `impl` block requires `EndpointSet`, not
/// the `EndpointMaybeSet` `from_provider_metadata` alone produces,
/// confirmed against the crate's real source, not assumed from the
/// OIDC spec's "technically optional" framing). `HasUserInfoUrl`
/// stays `EndpointMaybeSet` (never promoted -- this router never
/// calls the userinfo endpoint, matching Python's own OIDC flow,
/// which reads every claim off the id_token alone). Named here so
/// `build_oidc_client`'s signature doesn't need to spell out all 6
/// typestate parameters at every call site.
pub type BuiltOidcClient = CoreClient<
    EndpointSet,
    EndpointNotSet,
    EndpointNotSet,
    EndpointNotSet,
    EndpointSet,
    EndpointMaybeSet,
>;

/// Port of Python's `_ORIGIN_PINNED_ENDPOINTS` decision, without a
/// bare-tuple representation -- each variant names which check failed
/// for a precise error message (Python's own f-string does the same).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OidcHttpError {
    /// The discovery fetch itself failed (network, TLS, non-2xx,
    /// malformed document). Message text only -- never echoed to an
    /// unauthenticated caller (SD-R10-1 error-hygiene, PR7's job).
    DiscoveryFailed(String),
    /// A pinned endpoint is missing from the discovery document.
    MissingEndpoint { endpoint: &'static str },
    /// A pinned endpoint's origin doesn't match the configured
    /// issuer's origin (OBS-R17-SSO).
    OriginMismatch { endpoint: &'static str },
}

impl std::fmt::Display for OidcHttpError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::DiscoveryFailed(msg) => write!(f, "OIDC discovery failed: {msg}"),
            Self::MissingEndpoint { endpoint } => {
                write!(
                    f,
                    "OIDC discovery document is missing a usable {endpoint:?}"
                )
            }
            Self::OriginMismatch { endpoint } => write!(
                f,
                "OIDC discovery {endpoint:?} origin does not match the configured issuer \
                 origin; refusing (OBS-R17-SSO origin-pin)"
            ),
        }
    }
}
impl std::error::Error for OidcHttpError {}

/// Port of `_origin_tuple`'s same-origin comparison, via `url::Url`'s
/// own scheme/host/port accessors rather than hand-rolled URL
/// parsing -- `port_or_known_default()` already normalises an absent
/// port to its scheme default (`https`->443, `http`->80), the exact
/// behavior Python's own `_DEFAULT_SCHEME_PORTS` table hand-rolls.
fn same_origin(a: &Url, b: &Url) -> bool {
    a.scheme() == b.scheme()
        && a.host_str() == b.host_str()
        && a.port_or_known_default() == b.port_or_known_default()
}

/// Port of `_assert_discovery_same_origin`. Pure -- takes the already-
/// fetched metadata, no I/O -- so it's directly unit-testable against
/// synthetic `CoreProviderMetadata` fixtures without a mock server.
pub fn assert_discovery_same_origin(
    issuer: &IssuerUrl,
    metadata: &CoreProviderMetadata,
) -> Result<(), OidcHttpError> {
    let issuer_origin = issuer.url();

    let auth_url = metadata.authorization_endpoint().url();
    if !same_origin(issuer_origin, auth_url) {
        return Err(OidcHttpError::OriginMismatch {
            endpoint: "authorization_endpoint",
        });
    }

    // Python's real check treats a missing `token_endpoint` as a hard
    // failure (its dict-based discovery doc has no typed "optional"
    // distinction) -- `openidconnect` models it as genuinely
    // `Option<&TokenUrl>` per the OIDC spec's own optionality, but
    // this router's auth-code flow always needs it, so a missing one
    // is ported as the identical hard failure, not silently accepted.
    match metadata.token_endpoint() {
        Some(token_url) => {
            if !same_origin(issuer_origin, token_url.url()) {
                return Err(OidcHttpError::OriginMismatch {
                    endpoint: "token_endpoint",
                });
            }
        }
        None => {
            return Err(OidcHttpError::MissingEndpoint {
                endpoint: "token_endpoint",
            })
        }
    }

    let jwks_url = metadata.jwks_uri().url();
    if !same_origin(issuer_origin, jwks_url) {
        return Err(OidcHttpError::OriginMismatch {
            endpoint: "jwks_uri",
        });
    }

    Ok(())
}

/// Port of `_fetch_oidc_metadata`. Real async discovery fetch (the
/// crate-integration spike this PR's own name refers to) -- confirmed
/// live that `discover_async` performs TWO real HTTP fetches (the
/// `.well-known/openid-configuration` document AND the `jwks_uri`
/// JWKS document) before returning, not assumed from the crate's own
/// docs -- followed by the origin-pin check so both real callers
/// (login-init, callback) inherit it at the single trust boundary,
/// same as Python's own docstring states.
pub async fn fetch_oidc_metadata(
    issuer: &IssuerUrl,
    http_client: &reqwest::Client,
) -> Result<CoreProviderMetadata, OidcHttpError> {
    let metadata = CoreProviderMetadata::discover_async(issuer.clone(), http_client)
        .await
        .map_err(|e| {
            // Flatten the full `source()` chain into the stored
            // message -- SD-R10-1 (this is server-log detail only,
            // never echoed to an unauthenticated caller, per PR7's
            // job) benefits from the real cause (a bare "Request
            // failed" alone doesn't say whether DNS, TLS, or a
            // downstream fetch like the JWKS document actually
            // failed).
            use std::error::Error as _;
            let mut msg = e.to_string();
            let mut src = e.source();
            while let Some(s) = src {
                msg.push_str(&format!(" | caused by: {s}"));
                src = s.source();
            }
            OidcHttpError::DiscoveryFailed(msg)
        })?;
    assert_discovery_same_origin(issuer, &metadata)?;
    Ok(metadata)
}

/// Port of `init_oidc_login_handler`/`handle_oidc_callback`'s shared
/// `CoreClient` construction (`sess = OAuth2Session(...)` in Python's
/// Authlib-based code). Building this once here means both real
/// handlers (PR7) compose it identically rather than two independent
/// construction call sites drifting apart.
pub fn build_oidc_client(
    metadata: CoreProviderMetadata,
    client_id: &str,
    client_secret: &str,
    redirect_uri: &str,
) -> Result<BuiltOidcClient, OidcHttpError> {
    let redirect_uri = RedirectUrl::new(redirect_uri.to_string())
        .map_err(|e| OidcHttpError::DiscoveryFailed(format!("invalid redirect_uri: {e}")))?;
    // Grabbed before `metadata` moves into `from_provider_metadata` --
    // guaranteed `Some` by `assert_discovery_same_origin`'s own hard
    // failure on a missing `token_endpoint` (already run inside
    // `fetch_oidc_metadata`, the only real caller of this function).
    let token_uri = metadata
        .token_endpoint()
        .cloned()
        .ok_or(OidcHttpError::MissingEndpoint {
            endpoint: "token_endpoint",
        })?;
    Ok(CoreClient::from_provider_metadata(
        metadata,
        ClientId::new(client_id.to_string()),
        Some(ClientSecret::new(client_secret.to_string())),
    )
    .set_redirect_uri(redirect_uri)
    .set_token_uri(token_uri))
}

#[cfg(test)]
mod tests {
    use super::*;
    use openidconnect::core::{
        CoreJwsSigningAlgorithm, CoreResponseType, CoreSubjectIdentifierType,
    };
    use openidconnect::{AuthUrl, JsonWebKeySetUrl, ResponseTypes, TokenUrl};

    fn metadata_with(
        issuer: &str,
        auth_endpoint: &str,
        token_endpoint: Option<&str>,
        jwks_uri: &str,
    ) -> CoreProviderMetadata {
        let m = CoreProviderMetadata::new(
            IssuerUrl::new(issuer.to_string()).unwrap(),
            AuthUrl::new(auth_endpoint.to_string()).unwrap(),
            JsonWebKeySetUrl::new(jwks_uri.to_string()).unwrap(),
            vec![ResponseTypes::new(vec![CoreResponseType::Code])],
            vec![CoreSubjectIdentifierType::Public],
            vec![CoreJwsSigningAlgorithm::RsaSsaPkcs1V15Sha256],
            Default::default(),
        );
        match token_endpoint {
            Some(t) => m.set_token_endpoint(Some(TokenUrl::new(t.to_string()).unwrap())),
            None => m,
        }
    }

    const ISSUER: &str = "https://idp.example.test";

    #[test]
    fn same_origin_endpoints_pass() {
        let metadata = metadata_with(
            ISSUER,
            "https://idp.example.test/authorize",
            Some("https://idp.example.test/token"),
            "https://idp.example.test/jwks",
        );
        let issuer = IssuerUrl::new(ISSUER.to_string()).unwrap();
        assert_eq!(assert_discovery_same_origin(&issuer, &metadata), Ok(()));
    }

    #[test]
    fn a_default_port_and_an_explicit_matching_port_are_the_same_origin() {
        let metadata = metadata_with(
            ISSUER,
            "https://idp.example.test:443/authorize",
            Some("https://idp.example.test/token"),
            "https://idp.example.test/jwks",
        );
        let issuer = IssuerUrl::new(ISSUER.to_string()).unwrap();
        assert_eq!(assert_discovery_same_origin(&issuer, &metadata), Ok(()));
    }

    #[test]
    fn a_foreign_authorization_endpoint_host_is_rejected() {
        let metadata = metadata_with(
            ISSUER,
            "https://evil.example.test/authorize",
            Some("https://idp.example.test/token"),
            "https://idp.example.test/jwks",
        );
        let issuer = IssuerUrl::new(ISSUER.to_string()).unwrap();
        assert_eq!(
            assert_discovery_same_origin(&issuer, &metadata),
            Err(OidcHttpError::OriginMismatch {
                endpoint: "authorization_endpoint"
            })
        );
    }

    #[test]
    fn a_foreign_token_endpoint_host_is_rejected() {
        let metadata = metadata_with(
            ISSUER,
            "https://idp.example.test/authorize",
            Some("https://evil.example.test/token"),
            "https://idp.example.test/jwks",
        );
        let issuer = IssuerUrl::new(ISSUER.to_string()).unwrap();
        assert_eq!(
            assert_discovery_same_origin(&issuer, &metadata),
            Err(OidcHttpError::OriginMismatch {
                endpoint: "token_endpoint"
            })
        );
    }

    #[test]
    fn a_foreign_jwks_uri_host_is_rejected() {
        let metadata = metadata_with(
            ISSUER,
            "https://idp.example.test/authorize",
            Some("https://idp.example.test/token"),
            "https://evil.example.test/jwks",
        );
        let issuer = IssuerUrl::new(ISSUER.to_string()).unwrap();
        assert_eq!(
            assert_discovery_same_origin(&issuer, &metadata),
            Err(OidcHttpError::OriginMismatch {
                endpoint: "jwks_uri"
            })
        );
    }

    #[test]
    fn a_foreign_port_is_rejected_even_with_the_same_host() {
        let metadata = metadata_with(
            ISSUER,
            "https://idp.example.test:8443/authorize",
            Some("https://idp.example.test/token"),
            "https://idp.example.test/jwks",
        );
        let issuer = IssuerUrl::new(ISSUER.to_string()).unwrap();
        assert_eq!(
            assert_discovery_same_origin(&issuer, &metadata),
            Err(OidcHttpError::OriginMismatch {
                endpoint: "authorization_endpoint"
            })
        );
    }

    #[test]
    fn a_missing_token_endpoint_is_a_hard_failure() {
        let metadata = metadata_with(
            ISSUER,
            "https://idp.example.test/authorize",
            None,
            "https://idp.example.test/jwks",
        );
        let issuer = IssuerUrl::new(ISSUER.to_string()).unwrap();
        assert_eq!(
            assert_discovery_same_origin(&issuer, &metadata),
            Err(OidcHttpError::MissingEndpoint {
                endpoint: "token_endpoint"
            })
        );
    }

    #[tokio::test]
    async fn fetch_oidc_metadata_rejects_a_discovery_document_whose_endpoints_leave_the_issuer_origin(
    ) {
        // A REAL local HTTP server (no mocking framework) serving a
        // discovery document whose endpoints point at a foreign host
        // -- proves the origin-pin check runs on the real
        // discover_async() output, not just the synthetic fixtures
        // above.
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let issuer_url = format!("http://{addr}");

        let doc = serde_json::json!({
            "issuer": issuer_url,
            "authorization_endpoint": "http://evil.example.test/authorize",
            "token_endpoint": format!("{issuer_url}/token"),
            "jwks_uri": format!("{issuer_url}/jwks"),
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
        });
        let body = doc.to_string();
        // `discover_async` fetches BOTH the discovery document AND
        // the JWKS at `jwks_uri` as part of one call (confirmed by a
        // real Connection-refused failure before this fix, not
        // assumed from the crate's docs) -- the mock server must
        // answer both, on two separate connections (a bare hand-
        // rolled response with no `Connection: keep-alive` framing
        // makes the client open a fresh one for the second request).
        let jwks_body = serde_json::json!({"keys": []}).to_string();

        fn serve_one(stream: &mut std::net::TcpStream, body: &str) {
            use std::io::{Read, Write};
            let mut buf = [0u8; 1024];
            let _ = stream.read(&mut buf);
            let response = format!(
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(response.as_bytes()).unwrap();
        }

        let server = std::thread::spawn(move || {
            let (mut discovery_stream, _) = listener.accept().unwrap();
            serve_one(&mut discovery_stream, &body);
            let (mut jwks_stream, _) = listener.accept().unwrap();
            serve_one(&mut jwks_stream, &jwks_body);
        });

        let issuer = IssuerUrl::new(issuer_url).unwrap();
        let http_client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .unwrap();
        let result = fetch_oidc_metadata(&issuer, &http_client).await;

        server.join().unwrap();

        assert_eq!(
            result,
            Err(OidcHttpError::OriginMismatch {
                endpoint: "authorization_endpoint"
            })
        );
    }

    #[tokio::test]
    async fn fetch_oidc_metadata_succeeds_end_to_end_against_a_real_local_idp() {
        // The success-path mirror of the test above -- a real HTTP
        // round-trip (discovery doc + JWKS) whose endpoints genuinely
        // share the issuer's origin, proving the whole
        // discover_async -> origin-pin pipeline works, not just its
        // rejection branch.
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let issuer_url = format!("http://{addr}");

        let doc = serde_json::json!({
            "issuer": issuer_url,
            "authorization_endpoint": format!("{issuer_url}/authorize"),
            "token_endpoint": format!("{issuer_url}/token"),
            "jwks_uri": format!("{issuer_url}/jwks"),
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
        });
        let body = doc.to_string();
        let jwks_body = serde_json::json!({"keys": []}).to_string();

        fn serve_one(stream: &mut std::net::TcpStream, body: &str) {
            use std::io::{Read, Write};
            let mut buf = [0u8; 1024];
            let _ = stream.read(&mut buf);
            let response = format!(
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(response.as_bytes()).unwrap();
        }

        let server = std::thread::spawn(move || {
            let (mut discovery_stream, _) = listener.accept().unwrap();
            serve_one(&mut discovery_stream, &body);
            let (mut jwks_stream, _) = listener.accept().unwrap();
            serve_one(&mut jwks_stream, &jwks_body);
        });

        let issuer = IssuerUrl::new(issuer_url.clone()).unwrap();
        let http_client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .unwrap();
        let result = fetch_oidc_metadata(&issuer, &http_client).await;

        server.join().unwrap();

        let metadata =
            result.expect("discovery + origin-pin must succeed for a genuine same-origin IdP");
        assert_eq!(metadata.issuer().as_str(), issuer_url);
        assert_eq!(
            metadata.authorization_endpoint().url().as_str(),
            format!("{issuer_url}/authorize")
        );

        // The full pipeline: client construction on the real fetched
        // metadata succeeds too.
        assert!(build_oidc_client(
            metadata,
            "client-id",
            "secret",
            "https://router.example.test/agent-mcp/sso/callback"
        )
        .is_ok());
    }

    #[test]
    fn build_oidc_client_rejects_a_malformed_redirect_uri() {
        let metadata = metadata_with(
            ISSUER,
            "https://idp.example.test/authorize",
            Some("https://idp.example.test/token"),
            "https://idp.example.test/jwks",
        );
        let result = build_oidc_client(metadata, "client-id", "secret", "not a url");
        assert!(result.is_err());
    }

    #[test]
    fn build_oidc_client_succeeds_with_valid_inputs() {
        let metadata = metadata_with(
            ISSUER,
            "https://idp.example.test/authorize",
            Some("https://idp.example.test/token"),
            "https://idp.example.test/jwks",
        );
        let result = build_oidc_client(
            metadata,
            "client-id",
            "secret",
            "https://router.example.test/agent-mcp/sso/callback",
        );
        assert!(result.is_ok());
    }
}
