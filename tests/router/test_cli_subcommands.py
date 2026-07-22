"""CLI surface tests for the Phase 1a `agent-mcp` click group.

The pre-Phase-1a CLI was a single `@click.command` whose flags now
live under the `server` subcommand. Phase 1a converts to a
`@click.group` with two subcommands (`server`, `router`) and keeps a
backward-compat shim that rewrites legacy top-level invocations to
the `server` subcommand (with a DeprecationWarning).

These tests pin the new surface AND the back-compat shim, so a
careless refactor that drops either is caught here.
"""

from __future__ import annotations

import warnings

import pytest
from click.testing import CliRunner

from agent_mcp.cli import (
    _looks_like_legacy_top_level_invocation,
    cli,
    router_cmd,
    server_cmd,
)


def test_cli_is_a_group_with_expected_subcommands() -> None:
    # `backup` was added in the 2026-06-02 db-review PR-5 (item 12).
    assert set(cli.commands) == {"server", "router", "backup"}


def test_server_command_exists_and_keeps_legacy_options() -> None:
    # Every option the pre-Phase-1a command had MUST still be on the
    # server subcommand — otherwise the deploy-repo wrapper script
    # (and any user shell aliases) will silently lose flags.
    # retire-system-token Wave 3 dropped the --system-token-* family
    # (and the legacy --admin-token-* aliases) — they're no longer
    # required surface. The dead `--git` worktree flag was likewise
    # removed (coordinator-model cleanup): it fed the deleted spawn/
    # worktree machinery and was never read.
    option_names = {opt.name for opt in server_cmd.params}
    for required in (
        "port", "uds", "transport", "project_dir",
        "debug", "no_tui", "advanced", "no_index",
    ):
        assert required in option_names, f"server is missing --{required.replace('_', '-')}"


def test_router_command_options_match_plan() -> None:
    option_names = {opt.name for opt in router_cmd.params}
    expected = {
        "port", "projects_file", "sock_dir", "dashboard_dir",
        "external_url", "idle_sec", "installer_template", "readme_html",
    }
    assert expected <= option_names, f"router missing: {expected - option_names}"


def test_router_subcommand_requires_essential_paths(monkeypatch) -> None:
    """--sock-dir, --dashboard-dir, --external-url are mandatory (no
    sane default we can guess at). The subcommand must reject the
    invocation with a UsageError, not blow up deep inside the app."""
    for var in (
        "AGENT_MCP_SOCK_DIR",
        "AGENT_MCP_DASHBOARD_DIR",
        "AGENT_MCP_EXTERNAL_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["router"])
    assert result.exit_code != 0
    # Hits our explicit click.UsageError, not a KeyError stack trace.
    combined = (result.output or "") + str(result.exception or "")
    assert (
        "--sock-dir" in combined
        or "AGENT_MCP_SOCK_DIR" in combined
    ), f"unexpected error: {combined!r}"


@pytest.mark.parametrize("argv,expected", [
    # Legacy: top-level flag that used to live on the bare command.
    (["agent-mcp", "--transport", "sse"], True),
    (["agent-mcp", "--port=8080"], True),
    (["agent-mcp", "--uds", "/tmp/x.sock"], True),
    (["agent-mcp", "--no-tui"], True),
    # Subcommand invocations are NOT legacy.
    (["agent-mcp", "server", "--transport", "sse"], False),
    (["agent-mcp", "router", "--port", "1337"], False),
    # Empty / help / unknown flag — leave to click.
    (["agent-mcp"], False),
    (["agent-mcp", "--help"], False),
])
def test_legacy_invocation_sniffer(argv: list[str], expected: bool) -> None:
    assert _looks_like_legacy_top_level_invocation(argv) is expected


def test_backcompat_shim_emits_deprecation_warning(monkeypatch) -> None:
    """`python -m agent_mcp.cli --transport sse …` should still work
    but must warn. We can't actually start the server in a unit test,
    so we monkey-patch the `cli` callable to record what argv it sees."""
    from agent_mcp import cli as cli_module

    seen_argv: list[list[str]] = []

    def fake_cli() -> None:
        seen_argv.append(list(cli_module.sys.argv))

    monkeypatch.setattr(cli_module, "cli", fake_cli)
    monkeypatch.setattr(
        cli_module.sys, "argv",
        ["agent-mcp", "--transport", "sse", "--no-tui"],
    )

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        cli_module.main()

    assert seen_argv == [
        ["agent-mcp", "server", "--transport", "sse", "--no-tui"]
    ]
    deprecation = [w for w in captured if issubclass(w.category, DeprecationWarning)]
    assert deprecation, "expected a DeprecationWarning"
    assert "agent-mcp server" in str(deprecation[0].message)
