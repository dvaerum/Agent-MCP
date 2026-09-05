//! `/api` REST handlers (Phase E1, prancy-napping-pie). PR 2/14:
//! `/api/prompts/catalog` and `/api/settings-schema` -- the first two
//! real endpoints, chosen because neither touches the DB (zero
//! mutation risk) and together they exercise both halves of the
//! `/api` mount: `prompts/catalog` is genuinely UNAUTHENTICATED
//! (Python's own docstring: "the router-level gate is deferred to a
//! follow-up PR" -- one of the 3 confirmed no-auth-by-design REST
//! endpoints), `settings-schema` requires the `rest_gate` door.

use axum::extract::Extension;
use axum::response::{IntoResponse, Response};
use axum::Json;

use conexus_core::settings_schema::SETTINGS_SCHEMA;

use crate::rest_gate::ResolvedRestPrincipal;

/// `GET /api/prompts/catalog` -- the raw Prompt Book catalogue, served
/// verbatim with no visibility filtering (unlike `conexus_tools::
/// prompts::list_visible`, which gates on `CatalogRole` for the MCP
/// `prompts/list` surface). Genuinely unauthenticated, matching
/// Python's `prompts_catalog_api_route` exactly -- mounted on the
/// no-auth half of the `/api` router, not behind `rest_gate`.
pub async fn prompts_catalog() -> Response {
    // Serve the embedded text directly rather than round-tripping
    // through `serde_json::Value` -- it's already valid JSON (the
    // same file `conexus_tools::prompts` parses at startup), and this
    // matches Python's `JSONResponse(load_catalog())` byte-for-byte
    // (the raw file content, not a re-serialized copy that could
    // reorder keys or reformat whitespace differently).
    (
        [(axum::http::header::CONTENT_TYPE, "application/json")],
        conexus_tools::prompts::raw_catalog_json(),
    )
        .into_response()
}

/// `GET /api/settings-schema` -- the settings-schema registry plus the
/// caller's own tier flags, for the dashboard's Settings page to
/// render policy toggles and know whether it may show secret values.
/// Behind `rest_gate` (operator-tier only, matching Python's
/// `Depends(require_operator_session)` + inline confirmed-tier gate).
pub async fn settings_schema(
    Extension(resolved): Extension<ResolvedRestPrincipal>,
) -> impl IntoResponse {
    let schema: Vec<_> = SETTINGS_SCHEMA
        .iter()
        .map(|s| {
            serde_json::json!({
                "key": s.key,
                "type": s.r#type,
                "default": s.default,
                "tier": s.tier,
                "group": s.group,
                "title": s.title,
                "description": s.description,
                "widget": s.widget,
            })
        })
        .collect();
    Json(serde_json::json!({
        "schema": schema,
        "caller": {
            // Always `false`: neither remaining REST door (forwarding
            // header, operator-tier bearer) ever resolves a sysadmin
            // identity -- the dropped session-cookie door was the ONLY
            // one that could (via `group_resolver.resolve_user_is_sysadmin`).
            // Represented as a literal, not a stubbed field, since it's
            // the actually-correct value for every caller this backend
            // can now admit.
            "sysadmin": false,
            "confirmed_operator": resolved.confirmed_operator_tier,
        },
    }))
}
