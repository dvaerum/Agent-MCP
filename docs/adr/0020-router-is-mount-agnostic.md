# ADR 0020: Router is mount-agnostic; the URL prefix is a reverse-proxy concern

**Status**: Accepted + implemented (v5.64.0, 2026-07-31). The router now
serves every route at the host **root** as well as under `/agent-mcp`
(additive aliases), derives the external prefix/origin per request
(`agent_mcp/router/mount.py`), gates root-aliased routes on the
canonicalised path (no auth bypass), and scopes the session cookie to
the client's mount. The tailnet path is byte-identical.
**Frontend (v5.67.0):** the dashboard derives its mount from
`window.location` at runtime (`lib/urls.ts deriveMount()`), so nav, API
calls, login, and the path regexes are all mount-relative — clean at the
root, `/agent-mcp` on the tailnet.
**Cleanup (v5.68.0):** the dashboard `assetPrefix` is now per-request
(`_serve_dashboard_file` takes the mount-derived prefix; cache keyed by
prefix), the `.mcp.json` snippet honours the mount (`_build_mcp_config_snippet`
`mount_prefix`, sent by the dashboard's `deriveMount()`), and the
operator-SSE 500-on-disconnect is silenced (`security_headers` catches
`ConnectionResetError`). Verified live at `mm.best.aau.dk`: zero
`/agent-mcp` URLs in the served page, connected, no console errors.
**Date**: 2026-07-31
**Amends / supersedes-in-part**:
  - **ADR-0008** (single-tenant URL parity) — its `/agent-mcp/...` URL
    *examples* and the assumption that the router owns its mount prefix.
    ADR-0008's actual decision (single-tenant reaches URL parity with
    multi-tenant; a build artefact "works at any prefix", lines 52/65)
    is *upheld and completed* here — the prefix flexibility it aspired to
    becomes real and per-request.
  - **ADR-0014** (REST admin API) — only the leading `/agent-mcp` in its
    URL table. ADR-0014's real decisions stand unchanged: retire the
    `__` namespace, REST resources, the **reserved top-level segments**
    (`api` / `app` / `assets` / `mcp` / `router`), and Accept-header
    versioning. Those segments are legitimate *app-internal* namespacing
    (they separate router-admin routes from project routes) and are
    orthogonal to the external mount prefix this ADR removes.

## Context

The always-on router serves every route under a hardcoded `/agent-mcp`
prefix — `GET /agent-mcp/api/router/health`, `/agent-mcp/app/<project>/`,
`/agent-mcp/<project>/mcp`, `/agent-mcp/assets`, and so on. The literal
`/agent-mcp` is baked into ~20 source files (25 occurrences in
`router/app.py` alone, plus `login.py`, `sso.py`, `identity.py`,
`admin_api.py`, `asset_prefix.py`, `project_orchestrator.py`,
`tools/admin_tools.py`, `app/routers/events.py`, …). The app reads **no**
`X-Forwarded-Prefix` — it is entirely mount-*unaware*.

A path prefix that namespaces one service among many on a shared domain
is a **reverse-proxy concern**, not an application concern. Baking it into
the app couples the app to a single external mount and forces *every*
proxy in front to forward that exact prefix. This is a real defect, not a
style nit — it surfaced concretely when standing up a second front door:

> A Traefik reverse proxy at `https://mm.best.aau.dk` (public IP, over a
> WireGuard tunnel to the router) could reach the router at the TCP level
> (`nc` succeeded) but every request 404'd, because Traefik mounted the
> service at the **root** of that host while the router only answers under
> `/agent-mcp/`. The operator's correct mental model — "the `/agent-mcp`
> prefix should live in the proxy, since that's what hosts multiple
> services on one domain" — is not expressible today.

**The driving requirement**: the *same* running backend must serve, at the
same time, **both**

  - `https://mm.best.aau.dk/…` (Traefik, mounted at the host **root**), and
  - `https://nixos-developer-system.<tailnet>/agent-mcp/…`
    (tailscale-serve, mounted under **`/agent-mcp`**).

The absolute URLs the app must emit (dashboard `assetPrefix`, `Location`
redirects, the pasteable `.mcp.json` snippet) differ between those two
entry points in both host *and* prefix. A single static config
(`AGENT_MCP_EXTERNAL_URL` today) can encode only one of them, so it
structurally cannot serve both. The external identity must be derived
**per request**.

## Decision

**The router serves at the root of whatever it's given, and the external
mount prefix is owned entirely by the reverse proxy.**

1. **Serve at root.** Route registration drops the `/agent-mcp` prefix:
   `/api/router/health`, `/app/<project>/`, `/<project>/mcp`,
   `/assets/...`. ADR-0014's reserved segments (`api`/`app`/`assets`/
   `mcp`/`router`) are retained — they still carve router-admin from
   project routes; only the outer `/agent-mcp` is removed.

2. **The proxy owns the prefix.** A proxy that co-hosts services mounts
   the router at any prefix (or none), **strips** that prefix before
   forwarding, and advertises it via `X-Forwarded-Prefix`. Standard
   one-liners:
   - Traefik at root: forward as-is, `X-Forwarded-Prefix:` empty.
   - tailscale-serve / nginx at `/agent-mcp`: strip `/agent-mcp`, send
     `X-Forwarded-Prefix: /agent-mcp`.

3. **The app derives its external identity per request** from the
   forwarded headers — `X-Forwarded-Proto`, `X-Forwarded-Host`,
   `X-Forwarded-Prefix` (the ASGI `root_path` / WSGI `SCRIPT_NAME`
   pattern). This is what lets one process answer both front doors
   correctly and concurrently.

4. **Prefer relative URLs; use the derived prefix only where an absolute
   URL is unavoidable:**
   - *Redirects* (`Location`) → relative where possible (a relative
     redirect resolves correctly under any mount); otherwise prepend the
     derived prefix.
   - *Dashboard `assetPrefix`* → the existing `__AGENT_MCP_ASSET_PREFIX__`
     sentinel substitution (`router/asset_prefix.py`) is driven by the
     **per-request** `X-Forwarded-Prefix` instead of a static env, so the
     HTML served over each host references the right asset base.
   - *`.mcp.json` snippet* (`tools/admin_tools._build_mcp_config_snippet`)
     → built from the per-request proto + host + prefix, so a snippet
     minted via `mm.best.aau.dk` reads `https://mm.best.aau.dk/mcp/<p>`
     and one minted via the tailnet reads
     `https://…ts.net/agent-mcp/mcp/<p>`.

5. **`AGENT_MCP_EXTERNAL_URL` becomes a fallback, not the source of
   truth.** It is used only when there are no forwarded headers to derive
   from — e.g. a snippet generated outside any HTTP request (a daemon /
   CLI path). In-request generation always prefers the forwarded headers.

6. **Trusting the prefix is gated by the trusted-proxy allow-list.**
   `X-Forwarded-Prefix` / `-Host` are honored **only** from
   `AGENT_MCP_RATELIMIT_TRUSTED_PROXIES` (loopback + the configured proxy
   source IPs — e.g. Traefik's WireGuard peer). From an untrusted source
   they are ignored and the app falls back to root + `AGENT_MCP_EXTERNAL_URL`.
   This closes the spoofing surface a header-derived absolute URL would
   otherwise open (a forged prefix could poison a generated redirect or a
   pasteable snippet). The gate already exists for `X-Forwarded-Proto` /
   `-For`; this ADR makes it load-bearing for prefix/host too.

## Consequences

**Positive**
- One backend serves any number of front doors at any mounts,
  concurrently — the two-front-door requirement above is met, and
  "deploy at a different prefix" (ADR-0008's aspiration) is finally real.
- The app stops owning a deployment detail; the proxy — the thing that
  actually knows the domain layout — owns it.
- Direct/loopback access to the router is simpler: `curl
  http://127.0.0.1:1337/api/router/health` with no prefix.
- Aligns with the platform-standard reverse-proxy contract
  (`root_path` / `SCRIPT_NAME` / `X-Forwarded-Prefix`), so operators can
  reason about it with existing knowledge.

**Negative**
- **Large blast radius**: ~20 files hardcode `/agent-mcp`; every route
  registration, redirect, snippet, and asset path is touched. Warrants a
  phased implementation with a compatibility bridge (see *Migration*).
- The app now trusts one more forwarded header — mitigated by the
  existing trusted-proxy gate (decision item 6), but it is a real
  trust-surface addition that the security suite must cover.
- Relative-URL correctness is subtle (a missing trailing slash changes
  what a relative redirect resolves to); needs explicit tests per
  generated URL.

## Migration

Forward-only, backward-compatible for existing external URLs, phased so
CI stays green after each step:

1. **Derive-and-thread**: add per-request external-identity derivation
   (proto/host/prefix from forwarded headers, trusted-proxy-gated) and a
   single helper the app uses to build absolute URLs + the asset prefix.
   No route changes yet; the helper returns `/agent-mcp` by default so
   behavior is unchanged.
2. **Serve at root + strip at the proxy**: drop the `/agent-mcp` prefix
   from route registration; update the two production proxies
   (tailscale-serve TLS config + the nginx module) to **strip
   `/agent-mcp` and send `X-Forwarded-Prefix: /agent-mcp`**. External URLs
   (`…/agent-mcp/…`), existing bookmarks, and already-distributed
   `.mcp.json` snippets keep working unchanged — only the internal mount
   moved. Traefik at `mm.best.aau.dk` needs no prefix (root) and works
   the moment step 2 lands.
3. **Delete the hardcoded literals** + the static-`ASSET_PREFIX` path once
   nothing reads them. Pure subtraction.

## Verification

- One backend, two proxies, at once: `…/agent-mcp/api/router/health` via
  the tailnet **and** `https://mm.best.aau.dk/api/router/health` via
  Traefik both return `200`; a `.mcp.json` snippet minted through each
  front door carries that front door's host+prefix; the dashboard's
  assets load under each mount.
- Security: a direct (untrusted) request with a forged
  `X-Forwarded-Prefix` does **not** change any generated URL (falls back
  to root + `AGENT_MCP_EXTERNAL_URL`); only the configured trusted proxy
  is honored.
- Regression: the ADR-0014 reserved-segment collision guards and
  single-tenant URL-parity (ADR-0008) tests still pass with the prefix
  removed.
