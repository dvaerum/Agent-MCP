"""SSO config read-only REST surface (Phase 3 Wave 3).

The dashboard's System → SSO tab needs to show the current SSO mode
and its operator-visible knobs so a sysadmin can verify the deploy is
configured correctly without grepping the systemd environment. This
module exposes ONE endpoint:

  GET /agent-mcp/api/router/sso/config  →  {
    mode: "builtin" | "oidc" | "proxy_header",
    oidc?: {issuer, client_id, provider_name, group_mapping,
            redirect_url, scopes},
    proxy?: {trust_header, trusted_ips, default_is_sysadmin},
  }

WRITES are deliberately NOT shipped in this PR. The SSO config is
sourced from env vars (mirroring the rest of the router: nix module
populates them via the systemd service unit, sops-nix supplies the
client secret via a chmod-0600 file). Letting the dashboard mutate
them would require the router to either write the env vars back to
the home-manager config (out of scope) or maintain a parallel
config file the nix module wouldn't know about (drift hazard).
Future work: a NixOS-friendly config-write surface that integrates
with the home-manager module — tracked under the ADR-0015 follow-ups.

The CLIENT SECRET is never serialised — only its presence is
reported via a boolean. The file path itself stays out of the
response because the contents (not the path) are sensitive and the
operator already knows the path from the home-manager config.
"""

from __future__ import annotations

import logging

from aiohttp import web

from . import sso


logger = logging.getLogger(__name__)


__all__ = [
    "get_sso_config_handler",
    "register_admin_sso_routes",
]


async def get_sso_config_handler(req: web.Request) -> web.Response:
    """GET /agent-mcp/api/router/sso/config — current SSO surface.

    Returns the operator-visible knobs for whichever SSO mode is
    active (or ``mode: "builtin"`` when nothing is configured). The
    client secret is reported as a presence boolean only.

    Errors during config load (e.g. an unreadable secret file) are
    surfaced as 500 with the SSOConfigError message so the sysadmin
    can debug the deploy without journalctl access. Below the error
    threshold, the response also returns the mode discovered before
    the load failed (e.g. issuer set, secret unreadable → still
    reports ``oidc``) so the UI can render "OIDC misconfigured" copy.
    """
    try:
        settings = sso.get_sso_config(reload=True)
    except sso.SSOConfigError as e:
        return web.json_response(
            {
                "success": False,
                "error": "sso_config_error",
                "message": str(e),
            },
            status=500,
        )

    payload: dict[str, object] = {"mode": settings.mode.value}
    if settings.oidc is not None:
        payload["oidc"] = {
            "issuer": settings.oidc.issuer,
            "client_id": settings.oidc.client_id,
            "client_secret_present": bool(settings.oidc.client_secret),
            "provider_name": settings.oidc.provider_name,
            "group_mapping": dict(settings.oidc.group_mapping),
            "redirect_url": settings.oidc.redirect_url,
            "scopes": list(settings.oidc.scopes),
        }
    if settings.proxy is not None:
        payload["proxy"] = {
            "trust_header": settings.proxy.trust_header,
            "trusted_ips": sorted(settings.proxy.trusted_ips),
            "default_is_sysadmin": settings.proxy.default_is_sysadmin,
        }
    return web.json_response({"success": True, "config": payload})


def register_admin_sso_routes(app: web.Application) -> None:
    """Wire the SSO config endpoint on ``app``.

    Same envelope conventions as ``admin_users_api`` — the
    operator-session middleware handles auth at the path-prefix
    level, and a system-perm gate is applied per handler so a non-
    sysadmin operator gets a clean 403 instead of seeing the IdP
    settings.

    Wave 9 PR 4 (prancy-napping-pie): the gate moved from
    ``require_sysadmin`` to
    ``require_capability("system.sso.configure")``. Sysadmins still
    admit unconditionally (their cap set is the wildcard); the cap
    shape ALSO lets a sysadmin delegate SSO configuration to a group
    without promoting members to sysadmin.
    """
    from . import app as _app
    from .perm_gates import require_capability

    gated = _app._rest_gated
    sso_gate = require_capability("system.sso.configure")

    app.router.add_get(
        "/agent-mcp/api/router/sso/config",
        gated(sso_gate(get_sso_config_handler)),
    )
