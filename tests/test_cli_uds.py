"""CLI surface for `--uds` Unix domain socket option.

Note on test scope: this verifies the CLI exposes `--uds`. The actual
dispatch (uvicorn receives `uds=<path>` instead of `host`/`port` when
the option is set) is exercised end-to-end by the deployment tests in
`nixos-developer-system/users/dennis/agent-mcp/tests/` (where the
deployed binary really binds a UDS). Adding an in-process dispatch
test would require refactoring `main_cli` to extract a pure
`_build_uvicorn_kwargs(port, uds)` helper — out of scope for this PR.
"""

from __future__ import annotations

from click.testing import CliRunner


def test_cli_help_mentions_uds_option() -> None:
    """`agent_mcp --help` lists --uds as an available flag."""
    from agent_mcp.cli import main_cli

    runner = CliRunner()
    result = runner.invoke(main_cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "--uds" in result.output, (
        "expected `--uds` option in CLI help output; got:\n" + result.output
    )


def test_cli_uds_help_text_mentions_unix_socket() -> None:
    """The help text for --uds explains what it does."""
    from agent_mcp.cli import main_cli

    runner = CliRunner()
    result = runner.invoke(main_cli, ["--help"])

    assert result.exit_code == 0, result.output
    # Find the --uds line + its description.
    lines = result.output.splitlines()
    uds_idx = next((i for i, line in enumerate(lines) if "--uds" in line), -1)
    assert uds_idx >= 0
    # The description either lives on the same line (wide terminal) or
    # the following lines (wrapped). Concatenate a small window.
    window = " ".join(lines[uds_idx : uds_idx + 3]).lower()
    assert "unix" in window or "uds" in window or "socket" in window, (
        "expected --uds help text to mention Unix domain socket; got:\n"
        + result.output
    )
