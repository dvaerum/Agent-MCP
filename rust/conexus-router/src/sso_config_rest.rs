//! `GET /agent-mcp/api/router/sso/config` -- port of
//! `agent_mcp/router/admin_sso_api.py` (120 LOC, Phase E2 PR23
//! step 10/10, `conexus-router-sso-admin-config`).
//!
//! Read-only: reports the SSO mode + operator-visible knobs so a
//! sysadmin can verify the deploy is configured correctly without
//! grepping the systemd environment. WRITES are deliberately not
//! shipped (matches Python's own module doc -- the config is
//! env-var-sourced, and letting the dashboard mutate it would need
//! either a home-manager config write-back or a parallel config file
//! the nix module wouldn't know about). The client secret is never
//! serialised, only its presence as a bool.

use std::sync::Arc;

use axum::extract::State;
use axum::response::{IntoResponse, Response};
use axum::Extension;
use conexus_core::capability::Capability;
use serde_json::json;

use crate::mcp_handler::{HandlerBody, HandlerResponse};
use crate::project_gate;
use crate::session_gate::GateIdentity;
use crate::sso::{self, SsoSettings};
use crate::state::RouterState;

/// Pure: the JSON payload for a successfully loaded config. Port of
/// `get_sso_config_handler`'s payload assembly.
pub fn sso_config_payload(settings: &SsoSettings) -> serde_json::Value {
    let mut payload = serde_json::Map::new();
    payload.insert("mode".to_string(), json!(settings.mode.as_str()));
    if let Some(oidc) = &settings.oidc {
        payload.insert(
            "oidc".to_string(),
            json!({
                "issuer": oidc.issuer,
                "client_id": oidc.client_id,
                "client_secret_present": !oidc.client_secret.is_empty(),
                "provider_name": oidc.provider_name,
                "group_mapping": oidc.group_mapping,
                "redirect_url": oidc.redirect_url,
                "scopes": oidc.scopes,
            }),
        );
    }
    if let Some(proxy) = &settings.proxy {
        let mut trusted_ips: Vec<String> =
            proxy.trusted_ips.iter().map(ToString::to_string).collect();
        trusted_ips.sort();
        payload.insert(
            "proxy".to_string(),
            json!({
                "trust_header": proxy.trust_header,
                "trusted_ips": trusted_ips,
                "default_is_sysadmin": proxy.default_is_sysadmin,
            }),
        );
    }
    serde_json::Value::Object(payload)
}

/// Pure: the full response for either outcome of `load_sso_config`.
/// Port of `get_sso_config_handler`'s success/error branches.
pub fn sso_config_response(result: &Result<SsoSettings, sso::SsoConfigError>) -> HandlerResponse {
    match result {
        Ok(settings) => HandlerResponse {
            status: 200,
            headers: Vec::new(),
            body: HandlerBody::Json(json!({
                "success": true,
                "config": sso_config_payload(settings),
            })),
        },
        Err(e) => HandlerResponse {
            status: 500,
            headers: Vec::new(),
            body: HandlerBody::Json(json!({
                "success": false,
                "error": "sso_config_error",
                "message": e.to_string(),
            })),
        },
    }
}

/// `GET /agent-mcp/api/router/sso/config` -- gated on
/// `system.sso.configure` (sysadmins admit unconditionally via the
/// wildcard cap set; a sysadmin can ALSO delegate SSO configuration
/// to a group without promoting members to sysadmin, matching
/// Python's own Wave 9 PR 4 rationale for this exact capability
/// shape over a blanket `require_sysadmin`).
pub async fn get_sso_config_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
) -> Response {
    let single_tenant_name = state.mcp_handler_config.single_tenant_name.as_deref();
    if let Err(resp) = project_gate::require_capability(
        &identity,
        single_tenant_name,
        Capability::SystemSsoConfigure,
    ) {
        return resp.into_response();
    }
    let result = sso::load_sso_config(
        |key| std::env::var(key).ok(),
        |path| std::fs::read_to_string(path),
    );
    sso_config_response(&result).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sso::{OidcSettings, ProxyHeaderSettings, SsoMode};
    use std::collections::{HashMap, HashSet};

    #[test]
    fn payload_reports_builtin_mode_with_no_extra_sections() {
        let settings = SsoSettings {
            mode: SsoMode::Builtin,
            oidc: None,
            proxy: None,
        };
        let payload = sso_config_payload(&settings);
        assert_eq!(payload["mode"], "builtin");
        assert!(payload.get("oidc").is_none());
        assert!(payload.get("proxy").is_none());
    }

    #[test]
    fn payload_reports_oidc_config_with_secret_presence_only() {
        let mut group_mapping = HashMap::new();
        group_mapping.insert("engineers".to_string(), "operator".to_string());
        let settings = SsoSettings {
            mode: SsoMode::Oidc,
            oidc: Some(OidcSettings {
                issuer: "https://idp.example.test".to_string(),
                client_id: "abc123".to_string(),
                client_secret: "super-secret-value".to_string(),
                provider_name: "Example IdP".to_string(),
                group_mapping,
                redirect_url: Some(
                    "https://router.example.test/agent-mcp/sso/callback".to_string(),
                ),
                scopes: vec!["openid".to_string(), "email".to_string()],
                default_is_sysadmin: false,
            }),
            proxy: None,
        };
        let payload = sso_config_payload(&settings);
        assert_eq!(payload["mode"], "oidc");
        let oidc = &payload["oidc"];
        assert_eq!(oidc["issuer"], "https://idp.example.test");
        assert_eq!(oidc["client_id"], "abc123");
        assert_eq!(oidc["client_secret_present"], true);
        // The secret value itself must never appear anywhere in the payload.
        assert!(!payload.to_string().contains("super-secret-value"));
        assert_eq!(oidc["provider_name"], "Example IdP");
        assert_eq!(oidc["group_mapping"]["engineers"], "operator");
        assert_eq!(
            oidc["redirect_url"],
            "https://router.example.test/agent-mcp/sso/callback"
        );
        assert_eq!(oidc["scopes"], json!(["openid", "email"]));
    }

    #[test]
    fn payload_reports_an_empty_secret_as_not_present() {
        let settings = SsoSettings {
            mode: SsoMode::Oidc,
            oidc: Some(OidcSettings {
                issuer: "https://idp.example.test".to_string(),
                client_id: "abc123".to_string(),
                client_secret: String::new(),
                provider_name: "Example IdP".to_string(),
                group_mapping: HashMap::new(),
                redirect_url: None,
                scopes: Vec::new(),
                default_is_sysadmin: false,
            }),
            proxy: None,
        };
        let payload = sso_config_payload(&settings);
        assert_eq!(payload["oidc"]["client_secret_present"], false);
    }

    #[test]
    fn payload_reports_proxy_config_with_sorted_trusted_ips() {
        let mut trusted_ips = HashSet::new();
        trusted_ips.insert("10.0.0.5".parse().unwrap());
        trusted_ips.insert("10.0.0.1".parse().unwrap());
        let settings = SsoSettings {
            mode: SsoMode::ProxyHeader,
            oidc: None,
            proxy: Some(ProxyHeaderSettings {
                trust_header: "X-Agent-MCP-SSO-User".to_string(),
                trusted_ips,
                default_is_sysadmin: true,
            }),
        };
        let payload = sso_config_payload(&settings);
        assert_eq!(payload["mode"], "proxy_header");
        let proxy = &payload["proxy"];
        assert_eq!(proxy["trust_header"], "X-Agent-MCP-SSO-User");
        assert_eq!(proxy["trusted_ips"], json!(["10.0.0.1", "10.0.0.5"]));
        assert_eq!(proxy["default_is_sysadmin"], true);
    }

    #[test]
    fn response_reports_a_config_load_error_as_a_500() {
        let result = Err(sso::SsoConfigError(
            "both AGENT_MCP_SSO_OIDC_ISSUER and AGENT_MCP_SSO_PROXY_HEADER are set".to_string(),
        ));
        let resp = sso_config_response(&result);
        assert_eq!(resp.status, 500);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected a JSON body");
        };
        assert_eq!(body["success"], false);
        assert_eq!(body["error"], "sso_config_error");
        assert_eq!(
            body["message"],
            "both AGENT_MCP_SSO_OIDC_ISSUER and AGENT_MCP_SSO_PROXY_HEADER are set"
        );
    }

    #[test]
    fn response_reports_success_with_the_real_config_shape() {
        let result = Ok(SsoSettings {
            mode: SsoMode::Builtin,
            oidc: None,
            proxy: None,
        });
        let resp = sso_config_response(&result);
        assert_eq!(resp.status, 200);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected a JSON body");
        };
        assert_eq!(body["success"], true);
        assert_eq!(body["config"]["mode"], "builtin");
    }
}
