# Agent-MCP documentation

The `docs/` tree is organised by audience. Pick the folder that
matches what you're trying to do.

## Categories

### [`operator/`](./operator/) — running Agent-MCP

For operators standing up the router, the dashboard, or a
per-project backend.

- [`getting-started.md`](./operator/getting-started.md) — install, first-boot bootstrap (wizard / env vars / CLI), env-var reference, your first multi-agent project walkthrough.
- [`local-embeddings-guide.md`](./operator/local-embeddings-guide.md) — running the embeddings stack against a local Ollama instead of OpenAI.
- [`vm.md`](./operator/vm.md) — the NixOS VM the project ships for end-to-end testing against a production-shaped deployment.

### [`integrations/`](./integrations/) — connecting external clients

For wiring Claude Code, IDE plugins, ad-hoc scripts, or the REST
admin surface to a running Agent-MCP project.

- [`external-mcp-client.md`](./integrations/external-mcp-client.md) — per-agent bearer tokens; required after the `system_token` retirement (PRs #208–#211).
- [`api-versioning.md`](./integrations/api-versioning.md) — the strict `Accept: application/vnd.agent-mcp.v1+json` gate on `/agent-mcp/api/<name>/…`.

### [`adr/`](./adr/) — architecture decision records

Numbered ADRs (0008+). Each captures a decision, its context,
and the alternatives weighed. Most recent decisions:

- [ADR-0013](./adr/0013-operator-login.md) — operator login on the dashboard surface.
- [ADR-0014](./adr/0014-rest-admin-api.md) — REST admin API; retire the `__` URL namespace.
- [ADR-0015](./adr/0015-sso-oidc-and-proxy-header.md) — SSO via OIDC + proxy-header trust.

### [`proposals/`](./proposals/) — parked future improvements

Designs that are proposed but not accepted — captured so the idea
outlives the session that raised it. Each states what it is, its
current state in the tree, and the concrete need that would justify
finishing it.

- [`capability-based-authz.md`](./proposals/capability-based-authz.md) — replace coarse role tiers with fine-grained `resource.verb` capabilities sourced from SSO groups + bundles. Foundation partially shipped; feature parked pending a per-group-permissions need.

### [`audit/`](./audit/) — dated investigations

Capture-what-we-learned reports from one-off investigations.
Each file is dated and frozen — don't update in place, write a
new one if the topic resurfaces.

### [`mcd-example/`](./mcd-example/) — Main Context Document

The MCD authoring guide plus a worked example.

- [`mcd-guide.md`](./mcd-example/mcd-guide.md) — how to write an effective MCD.
- [`README.md`](./mcd-example/README.md) — entry point to the example MCD (React + Supabase).

### [`theory/`](./theory/) — background reading

Chapters on the cognitive model behind Agent-MCP: empathy,
context, tools, intelligent judgement. Source PDFs live under
[`theory/chapters/`](./theory/chapters/).

### [`historical/`](./historical/) — frozen comparison analyses

Pre-restructure comparison notes (Python vs. proposed Node/TS
port). Kept for reference; do not treat as current behaviour
specifications — they reference the retired `admin_token` model
because they predate the `system_token` retirement.

## Conventions

- Lower-case, kebab-case filenames (`getting-started.md`, not
  `Getting-Started.md`).
- Cross-link with relative paths; every fact has one canonical
  home and other docs reference it rather than duplicate.
- When a file moves, leave a redirect stub at the old path only
  if the old path is referenced from source code or shipped
  product output. Otherwise just update the links.
