"""Regression guards for Phase 7-UX2: Agents page row-click + popup polish
+ MCP-onboarding tabs.

This PR brings the Agents page View dialog up to parity with the Tasks
page polish (PR #54): clicking anywhere on a row body opens the View
dialog, the dialog is wider + viewport-capped + has a sticky header /
footer with a single scrollable body region, and long tokens / snippets
wrap inside the box instead of overflowing.

In addition, the View dialog grows a new MCP-onboarding section: a
shadcn Tabs primitive with one tab per supported MCP client (Claude
Code, OpenCode, Cursor, Cline, Zed, Continue.dev, Generic JSON). Each
tab shows the copy-paste-ready config snippet for THIS agent, with a
copy button and a localStorage-persisted "preferred client" memory.

Text-parse regression guards (same convention as
``test_dashboard_tasks_popup_polish.py`` and
``test_dashboard_agents_row_icons.py``). No jsdom in this repo;
behaviour is verified by ``npm run build`` plus Firefox MCP e2e.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")
AGENTS_TSX = DASHBOARD / "components/dashboard/agents-dashboard.tsx"


def _read_agents() -> str:
    return AGENTS_TSX.read_text()


# ---------- Imports ------------------------------------------------


def test_agents_dashboard_imports_tabs_primitive() -> None:
    """The shadcn ``<Tabs>`` primitive must be imported so the MCP-
    onboarding section can render one tab per client."""
    src = _read_agents()
    assert "@/components/ui/tabs" in src, (
        "agents-dashboard.tsx must import the shadcn Tabs primitive "
        "(@/components/ui/tabs) for the MCP-onboarding tabbed section"
    )
    # Spot-check the actual named imports we use.
    for name in ("Tabs", "TabsList", "TabsTrigger", "TabsContent"):
        assert name in src, (
            f"agents-dashboard.tsx must import {name} from "
            "@/components/ui/tabs"
        )


def test_agents_dashboard_imports_use_dialog() -> None:
    """The View dialog migration mirrors PR #59's useDialog<T>() pattern
    — the import must already be there (it was added in PR #59), but
    keep the guard so we don't lose it during the polish refactor."""
    src = _read_agents()
    assert "useDialog" in src, (
        "agents-dashboard.tsx must use the useDialog<T>() hook for the "
        "View dialog state (see PR #59)"
    )
    assert "@/hooks/use-dialog" in src, (
        "useDialog import must come from @/hooks/use-dialog"
    )


def test_agents_dashboard_imports_label_primitive() -> None:
    """Labels above values in the View dialog should use the shadcn
    ``Label`` primitive — matches Tasks page polish (PR #54)."""
    src = _read_agents()
    assert "@/components/ui/label" in src, (
        "shadcn Label must be imported for the polished View dialog"
    )


def test_agents_dashboard_imports_copy_icon() -> None:
    """The per-tab copy button uses lucide's ``Copy`` icon."""
    src = _read_agents()
    # Copy is already imported (used on the row token). Guard it
    # stays — the new MCP-onboarding tabs depend on it.
    assert re.search(r"\bCopy\b", src), (
        "agents-dashboard.tsx must import lucide Copy icon for the "
        "per-tab copy button"
    )


def test_agents_dashboard_imports_project_context() -> None:
    """The MCP URL must come from the path-prefix adapter (PR #56)."""
    src = _read_agents()
    assert "@/lib/project-context" in src or "projectContext" in src, (
        "agents-dashboard.tsx must consume projectContext from "
        "@/lib/project-context to derive the MCP URL"
    )


# ---------- Row click opens the View dialog -----------------------


def test_table_row_click_opens_view_dialog() -> None:
    """The ``TableRow`` body click must call ``openView(agent)`` so
    clicking anywhere on the row body opens the View dialog — mirrors
    the Tasks page row-click pattern."""
    src = _read_agents()
    assert re.search(
        r"onClick=\{\(\)\s*=>\s*openView\(agent\)\}",
        src,
    ), (
        "TableRow onClick must call openView(agent) so the row body "
        "opens the View dialog (same as the eye icon)"
    )


def test_table_row_has_cursor_pointer() -> None:
    """A clickable row must visually advertise its clickability."""
    src = _read_agents()
    # We accept either the literal Tailwind class or a cn() include.
    assert "cursor-pointer" in src, (
        "TableRow must declare cursor-pointer so the click affordance "
        "is visible"
    )


def test_row_action_buttons_stop_propagation() -> None:
    """The per-row action icon buttons (View / Edit / Terminate /
    Restore / Purge) MUST call ``e.stopPropagation()`` in their
    onClick — otherwise their click bubbles up to the row body and
    opens the View dialog on top of the destructive action.

    Pattern lifted from tasks-dashboard.tsx:
        onClick={(e) => { e.stopPropagation(); onSelect(agent) }}
    """
    src = _read_agents()
    # Count: there must be at least one stopPropagation call inside the
    # row-action buttons block. A single occurrence is the floor — in
    # practice we expect five (one per action button).
    assert "stopPropagation" in src, (
        "row-action buttons must call e.stopPropagation() to prevent "
        "bleed-through into the TableRow body onClick"
    )
    # Stricter check: there should be multiple stopPropagation calls
    # (one per action button, ≥3 covers the always-rendered ones).
    count = src.count("stopPropagation")
    assert count >= 3, (
        f"expected ≥3 stopPropagation calls (one per action button); "
        f"found {count}"
    )


# ---------- Dialog polish: width, height cap, scrollable body -----


def test_view_dialog_has_max_w_3xl_override() -> None:
    """The View dialog DialogContent must use ``sm:!max-w-3xl`` (the
    Tailwind important variant) to override the base DialogContent's
    ``sm:max-w-lg``, same as PR #54 did for the Tasks page."""
    src = _read_agents()
    assert re.search(
        r"AgentDetailDialog[\s\S]*?DialogContent[^>]*sm:!max-w-3xl",
        src,
    ), (
        "AgentDetailDialog DialogContent must use sm:!max-w-3xl to "
        "override base sm:max-w-lg"
    )


def test_view_dialog_caps_height_at_90vh() -> None:
    """The DialogContent must cap at 90vh so very long snippets don't
    push the modal past the viewport."""
    src = _read_agents()
    assert re.search(
        r"AgentDetailDialog[\s\S]*?DialogContent[^>]*max-h-\[90vh\]",
        src,
    ), (
        "AgentDetailDialog DialogContent must declare max-h-[90vh]"
    )


def test_view_dialog_body_is_single_flex_scroll_region() -> None:
    """The body region of the View dialog must use the
    ``flex-1 min-h-0 overflow-y-auto`` triplet so it's the single
    scroll region inside a flex-column DialogContent (header + footer
    pinned via flex-shrink-0). Same idiom as PR #54."""
    src = _read_agents()
    body_block = re.search(
        r"AgentDetailDialog[\s\S]*?</DialogFooter>",
        src,
    )
    assert body_block, "could not locate AgentDetailDialog body"
    body = body_block.group(0)
    assert "flex-1 min-h-0 overflow-y-auto" in body, (
        "View dialog body must declare `flex-1 min-h-0 overflow-y-auto` "
        "as the single scroll region"
    )
    assert "flex-shrink-0" in body, (
        "View dialog header + footer must be flex-shrink-0 so the body "
        "is the only thing that scrolls"
    )


def test_view_dialog_long_values_wrap_anywhere() -> None:
    """Tokens are 32-hex blobs and snippet bodies can be long URLs —
    they MUST use ``[overflow-wrap:anywhere]`` so they wrap inside the
    box instead of stretching the dialog horizontally."""
    src = _read_agents()
    body_block = re.search(
        r"AgentDetailDialog[\s\S]*?</DialogFooter>",
        src,
    )
    assert body_block, "could not locate AgentDetailDialog body"
    body = body_block.group(0)
    assert "[overflow-wrap:anywhere]" in body, (
        "long values (token, snippets) must use [overflow-wrap:anywhere]"
    )


def test_view_dialog_title_uses_line_clamp_not_truncate() -> None:
    """The dialog title shows the agent_id; long ids should wrap up to
    3 lines (``line-clamp-3 break-words``) rather than being silently
    truncated with ``truncate``."""
    src = _read_agents()
    body_block = re.search(
        r"AgentDetailDialog[\s\S]*?</DialogFooter>",
        src,
    )
    assert body_block, "could not locate AgentDetailDialog body"
    body = body_block.group(0)
    assert "line-clamp-3" in body, (
        "View dialog title must use line-clamp-3 (not truncate) so "
        "long agent_ids wrap instead of being silently truncated"
    )
    assert "break-words" in body, (
        "View dialog title must use break-words alongside line-clamp-3"
    )


def test_view_dialog_renders_label_primitive() -> None:
    """The polished View dialog must use the shadcn ``<Label>`` for
    field labels above values."""
    src = _read_agents()
    body_block = re.search(
        r"AgentDetailDialog[\s\S]*?</DialogFooter>",
        src,
    )
    assert body_block, "could not locate AgentDetailDialog body"
    body = body_block.group(0)
    assert re.search(r"<Label\b", body), (
        "View dialog must render the shadcn <Label> primitive for "
        "labels above values"
    )


# ---------- MCP-onboarding tabbed section --------------------------


def test_view_dialog_contains_tabs_jsx() -> None:
    """The MCP-onboarding section must use the shadcn ``<Tabs>``
    primitive (Radix-backed)."""
    src = _read_agents()
    body_block = re.search(
        r"AgentDetailDialog[\s\S]*?</DialogFooter>",
        src,
    )
    assert body_block, "could not locate AgentDetailDialog body"
    body = body_block.group(0)
    assert "<Tabs" in body, (
        "View dialog body must render a <Tabs> element for the "
        "MCP-onboarding section"
    )


def test_view_dialog_has_all_seven_client_tabs() -> None:
    """One TabsTrigger per supported client."""
    src = _read_agents()
    body_block = re.search(
        r"AgentDetailDialog[\s\S]*?</DialogFooter>",
        src,
    )
    assert body_block, "could not locate AgentDetailDialog body"
    body = body_block.group(0)
    for value in (
        "claude-code",
        "opencode",
        "cursor",
        "cline",
        "zed",
        "continue",
        "generic",
    ):
        assert re.search(
            rf'<TabsTrigger\s+value="{value}"',
            body,
        ), (
            f"View dialog must include a <TabsTrigger value=\"{value}\"> "
            "for the MCP-onboarding section"
        )
        assert re.search(
            rf'<TabsContent\s+value="{value}"',
            body,
        ), (
            f"View dialog must include a <TabsContent value=\"{value}\"> "
            "with the snippet for that client"
        )


def test_view_dialog_has_copy_button() -> None:
    """Each tab needs a copy button — the lucide Copy icon must be
    rendered inside the MCP-onboarding section."""
    src = _read_agents()
    body_block = re.search(
        r"AgentDetailDialog[\s\S]*?</DialogFooter>",
        src,
    )
    assert body_block, "could not locate AgentDetailDialog body"
    body = body_block.group(0)
    # The body already contains a token-copy <Copy /> — make sure the
    # onboarding section also includes one. Heuristic: there must be at
    # least 2 <Copy occurrences in the body (token + ≥1 snippet copy).
    assert body.count("<Copy") >= 2, (
        "View dialog body must include at least 2 <Copy /> icons "
        "(token copy + MCP-onboarding snippet copy)"
    )


def test_view_dialog_persists_active_tab_in_localstorage() -> None:
    """The active tab must persist across reloads — we key it under
    ``agent-mcp-popup-active-client`` so a user's "I always use
    OpenCode" preference is sticky."""
    src = _read_agents()
    assert "agent-mcp-popup-active-client" in src, (
        "active-tab persistence must use the localStorage key "
        "'agent-mcp-popup-active-client'"
    )
    assert "localStorage" in src, (
        "active-tab persistence must reference localStorage"
    )


def test_server_name_is_fixed_agent_mcp() -> None:
    """The snippet server name must be the fixed string ``agent-mcp``
    (NOT namespaced ``agent-mcp-${agent.agent_id}``). This matches the
    user's .claude.json convention so the slash-command prefix is
    ``agent-mcp:``; a single fixed key is fine because .mcp.json entries
    are scoped per cwd/project. The regex guards against a revert to the
    ``agent-mcp-${...}`` interpolated form."""
    src = _read_agents()
    # buildSnippet owns the server-name literal; assert it binds `name`
    # to the fixed 'agent-mcp' string.
    assert re.search(r"const\s+name\s*=\s*'agent-mcp'", src), (
        "buildSnippet must set `const name = 'agent-mcp'` (the fixed "
        "server key), matching the user's .claude.json convention"
    )
    # And guard against a revert to the interpolated per-agent_id form.
    assert not re.search(r"agent-mcp-\$\{[^}]*agent[^}]*\.agent_id", src), (
        "snippet server name must be the fixed `agent-mcp`, not the "
        "namespaced `agent-mcp-${agent.agent_id}` form"
    )


def test_snippet_uses_streamable_http_transport() -> None:
    """The snippets must declare Streamable HTTP transport — the
    backend gates ``/mcp`` to POST/GET/DELETE per MCP spec rev
    2025-03-26 (PR #61). The Claude Code CLI snippet uses
    ``--transport http``; the JSON snippets use ``"type": "http"``."""
    src = _read_agents()
    # File-level check: snippet templates live in a sibling helper
    # (buildSnippet) so we don't constrain them to the dialog body.
    assert ("--transport http" in src) or ('"type": "http"' in src), (
        "MCP snippets must declare http transport (--transport http "
        "for the CLI or \"type\": \"http\" in JSON configs)"
    )


def test_snippet_uses_bearer_authorization() -> None:
    """Every snippet must send the agent's token as
    ``Authorization: Bearer <token>``."""
    src = _read_agents()
    body_block = re.search(
        r"AgentDetailDialog[\s\S]*?</DialogFooter>",
        src,
    )
    assert body_block, "could not locate AgentDetailDialog body"
    body = body_block.group(0)
    # The literal "Bearer " prefix should be present in the snippet
    # template strings.
    assert "Bearer " in body, (
        "snippets must use Authorization: Bearer <token>"
    )


def test_snippet_url_uses_mcp_endpoint() -> None:
    """The snippet URL must point at the ``/mcp`` Streamable HTTP
    endpoint (not the legacy ``/sse``)."""
    src = _read_agents()
    body_block = re.search(
        r"AgentDetailDialog[\s\S]*?</DialogFooter>",
        src,
    )
    assert body_block, "could not locate AgentDetailDialog body"
    body = body_block.group(0)
    assert "/mcp" in body, (
        "snippet URL must point at the /mcp Streamable HTTP endpoint"
    )


def test_register_modal_snippet_container_has_min_w_0() -> None:
    """RegisterAgentModal pane-2 wraps the .mcp.json snippet in a grid/
    flex child. Without ``min-w-0`` that child inherits ``min-width:auto``
    and refuses to shrink below the <pre>'s min-content (the long
    unbreakable URL), so the dialog balloons past ``sm:!max-w-lg`` and
    the snippet bleeds over the agents table. Lock ``min-w-0`` on the
    snippet container so ``overflow-x-auto`` engages instead."""
    src = _read_agents()
    # The pane-2 block renders {result.mcp_snippet} inside a <pre>. Grab
    # the enclosing snippet <div> (the one right before that <pre>) and
    # assert it carries min-w-0.
    block = re.search(
        r"<div className=\"min-w-0\">\s*<div className=\"flex items-center"
        r"[\s\S]*?\{result\.mcp_snippet\}",
        src,
    )
    assert block, (
        "RegisterAgentModal snippet container must be a <div "
        'className="min-w-0"> wrapping the {result.mcp_snippet} <pre> '
        "so the dialog stays at sm:!max-w-lg"
    )
