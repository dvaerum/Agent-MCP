"""Regression guards for the dashboard PathPrefix adapter refactor
(Candidate C, architecture review 2026-06-01).

The original bootstrap was a 3-useEffect dance inside
`components/providers/api-client-initializer.tsx`:
  1. Auto-seed a synthetic server-store entry from
     `window.location.pathname` (gated on zustand-persist hydration).
  2. Sync `activeServerId` → `apiClient.setServer(...)` and override
     baseUrl when the URL pattern matches.
  3. Cold-start retry: poll `setActiveServer` every 1.5s up to 30
     times (45s budget) so the dashboard reconnects once the lazily-
     spawned backend's socket finally appears.

That dance was three-effect ordering theater for two facts known
synchronously at page load:
  - the project name + API root (derived from `window.location.pathname`)
  - the connect attempt has to be retried until the backend is up
    (a fetch concern, not a React-effect concern)

Candidate C collapses it into:
  - A **module-level singleton** in `agent_mcp/dashboard/lib/project-context.ts`
    that runs at module import and exposes `projectContext` plus a
    React `ProjectContext`.
  - A **Provider in `app/layout.tsx`** that propagates the resolved
    values to all children that need them.
  - **Transparent retry inside `ApiClient.request()`**: on 502/503/504
    responses, retry with exponential backoff (200ms, 400ms — 3
    attempts total). Callers see success or a hard failure; the
    boundary-level retry useEffect disappears.

These guards are text-level (the fork has no jsdom/RTL infrastructure
for behavioural dashboard tests). Build + manual click-through verify
behaviour on the PR.
"""

from __future__ import annotations

from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")
PROJECT_CONTEXT = DASHBOARD / "lib" / "project-context.ts"
LAYOUT = DASHBOARD / "app" / "layout.tsx"
PROVIDER = DASHBOARD / "components" / "providers" / "project-context-provider.tsx"
API = DASHBOARD / "lib" / "api.ts"
OLD_INIT = DASHBOARD / "components" / "providers" / "api-client-initializer.tsx"


def _read(path: Path) -> str:
    return path.read_text()


# -- project-context.ts ----------------------------------------------------


def test_project_context_module_exists() -> None:
    """The module-level singleton lives in `lib/project-context.ts`."""
    assert PROJECT_CONTEXT.exists(), (
        f"expected {PROJECT_CONTEXT} to exist — Candidate C makes the "
        "PathPrefix derivation a module-level singleton instead of a "
        "useEffect"
    )


def test_project_context_exports_singleton_and_react_context() -> None:
    """Module must export `projectContext` (the resolved values) and
    `ProjectContext` (the React context for provider/consumer wiring)."""
    src = _read(PROJECT_CONTEXT)
    assert "export const projectContext" in src, (
        "expected `export const projectContext` — the module-level "
        "singleton holding {projectName, baseUrl, apiPrefix}"
    )
    assert "export const ProjectContext" in src, (
        "expected `export const ProjectContext` — the React context "
        "used by the Provider in app/layout.tsx"
    )
    assert "createContext" in src, (
        "expected `createContext` import from React for ProjectContext"
    )


def test_project_context_derives_from_pathname_synchronously() -> None:
    """Derivation happens at module import. The singleton inspects
    `window.location.pathname` (SSR fallback: typeof window check)
    and matches against the path-prefix regex."""
    src = _read(PROJECT_CONTEXT)
    assert "window.location.pathname" in src, (
        "expected the singleton to read `window.location.pathname` "
        "directly (no useEffect)"
    )
    assert "typeof window" in src, (
        "expected `typeof window !== 'undefined'` SSR guard so the "
        "module imports cleanly during Next.js prerender"
    )
    assert "/agent-mcp/__dashboard" in src, (
        "expected the path-prefix regex literal `/agent-mcp/__dashboard` "
        "in project-context.ts"
    )
    assert "/agent-mcp/__api/" in src, (
        "expected the derived API root literal `/agent-mcp/__api/` "
        "in project-context.ts"
    )


# -- app/layout.tsx Provider wiring ---------------------------------------


def test_layout_wraps_children_in_project_context_provider() -> None:
    """The Provider is wired into `app/layout.tsx` so every dashboard
    route sees the resolved values.

    Next.js Server/Client boundary requires the actual
    `<ProjectContext.Provider value={projectContext}>` to live in a
    "use client" module — `app/layout.tsx` is a server component
    (exports `metadata` + `viewport`). The Provider is therefore
    extracted into a thin client wrapper
    (`components/providers/project-context-provider.tsx`) that
    layout.tsx renders. We accept either pattern: the Provider
    rendered directly in layout, or via the wrapper component.
    """
    layout_src = _read(LAYOUT)
    direct = (
        "ProjectContext.Provider" in layout_src
        and "projectContext" in layout_src
    )
    wrapper_in_layout = "ProjectContextProvider" in layout_src
    assert direct or wrapper_in_layout, (
        "expected `<ProjectContext.Provider value={projectContext}>` "
        "in app/layout.tsx OR a `<ProjectContextProvider>` client "
        "wrapper rendered from app/layout.tsx"
    )
    if wrapper_in_layout:
        assert PROVIDER.exists(), (
            "layout.tsx references ProjectContextProvider but the "
            "wrapper module is missing"
        )
        provider_src = _read(PROVIDER)
        assert "ProjectContext.Provider" in provider_src, (
            "expected `<ProjectContext.Provider>` inside the client "
            f"wrapper at {PROVIDER}"
        )
        assert "projectContext" in provider_src, (
            "expected the singleton `projectContext` to be passed as "
            "the Provider value in the wrapper"
        )


# -- ApiClient transparent retry ------------------------------------------


def test_api_client_retries_on_5xx_with_exponential_backoff() -> None:
    """`ApiClient.request()` must retry on 502/503/504 with
    exponential backoff (cold-start case: backend systemd unit takes
    10–15s to come up after a request lands on the router)."""
    src = _read(API)
    # Retry loop signature: bounded attempts + 5xx detection + setTimeout.
    assert "attempt" in src and "< 3" in src, (
        "expected a bounded `for (let attempt = 0; attempt < 3; ...)` "
        "retry loop in ApiClient.request()"
    )
    # Status 5xx detection. Accept either an explicit code list or a
    # `>= 500 && < 600` range check.
    has_explicit = "502" in src and "503" in src
    has_range = ">= 500" in src and "< 600" in src
    assert has_explicit or has_range, (
        "expected ApiClient.request() to detect 502/503/504 (either "
        "explicit codes or `>= 500 && < 600` range)"
    )
    # Exponential backoff via setTimeout. Match `* 2 **` (the doubling
    # multiplier) — robust to whitespace and base value.
    assert "setTimeout" in src and "* 2 **" in src, (
        "expected exponential backoff `setTimeout(..., base * 2 ** attempt)` "
        "in the retry loop"
    )


# -- old bootstrap useEffects removed -------------------------------------


def test_old_api_client_initializer_useeffects_removed() -> None:
    """The 3-useEffect bootstrap dance in
    `api-client-initializer.tsx` is the regression we are killing.
    Either the file is deleted entirely, or it no longer contains
    the cold-start `setInterval` retry loop nor the
    `onFinishHydration`-gated auto-seed.

    A module-level singleton + ApiClient.request() retry replace both.
    """
    if not OLD_INIT.exists():
        # File deleted — best case, the whole bootstrap module is gone.
        return
    src = _read(OLD_INIT)
    assert "setInterval" not in src, (
        "the cold-start retry useEffect (setInterval-based polling) "
        "must be removed — retry now lives transparently inside "
        "ApiClient.request()"
    )
    assert "onFinishHydration" not in src and "hasHydrated" not in src, (
        "the persist-hydration-gated auto-seed useEffect must be "
        "removed — derivation is now synchronous at module import in "
        "lib/project-context.ts"
    )


def test_layout_does_not_render_old_api_client_initializer() -> None:
    """`<ApiClientInitializer />` was the boundary that ran the
    useEffect dance. After the refactor, layout.tsx must not render
    it (the Provider replaces it)."""
    src = _read(LAYOUT)
    assert "<ApiClientInitializer" not in src, (
        "expected `<ApiClientInitializer />` to be removed from "
        "app/layout.tsx — replaced by `<ProjectContext.Provider>`"
    )
