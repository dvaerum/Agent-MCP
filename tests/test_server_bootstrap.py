"""ServerBootstrap contract tests (PR E — round-2 architecture review).

Before this PR, the boot path was scattered across three files:

* ``agent_mcp/cli.py`` — argparse adapter that *also* walked ``.env``,
  branched on transport, owned the TUI display loop, and called the
  Starlette runners.
* ``agent_mcp/app/main_app.py`` — Starlette wiring.
* ``agent_mcp/app/server_lifecycle.py`` — lifespan handlers.

"Where does the server start?" required reading three files. This PR
consolidates the boot ordering into ``agent_mcp/server_bootstrap.py``
(a ``ServerConfig`` dataclass + ``bootstrap_server`` factory).
Lifespan handlers STAY in ``server_lifecycle.py`` — they're called by
Starlette during request handling, not by the CLI. The CLI shrinks to
a thin click adapter that builds a ``ServerConfig`` from parsed
options and hands it to ``bootstrap_server``.

These tests pin the boundary:

* ``ServerConfig`` is the single source of truth for boot settings.
* ``ServerConfig.from_cli_args`` translates the click-decoded options
  exactly the same way ``cli.py`` did before — env-var promotion,
  type validation, ``.env`` discovery — so a careless refactor that
  drops a side effect is caught here rather than in production.
* ``bootstrap_server`` returns a Starlette app + a teardown callable.
  The teardown is idempotent (calling it twice is safe — important
  for SystemExit paths in the CLI runner).
* The transport branch (stdio vs sse) selects the right runner.
* Embedding-mode (simple vs advanced) and auto-indexing flags propagate
  to ``core.config`` *before* any module-level imports that read them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest


# --- ServerConfig contract --------------------------------------------


def test_server_config_is_a_frozen_dataclass() -> None:
    """``ServerConfig`` must be frozen so a stray ``cfg.port = 9000``
    after the bootstrap runs gets caught at write time, not deep in a
    background task that captured the value at boot."""
    from dataclasses import fields, is_dataclass
    from agent_mcp.server_bootstrap import ServerConfig

    assert is_dataclass(ServerConfig)
    cfg = _default_config()
    with pytest.raises((AttributeError, Exception)):
        cfg.port = 9999  # type: ignore[misc]

    # Field set covers everything the legacy CLI threaded through to
    # the runners — we lose a flag if this assertion drops.
    field_names = {f.name for f in fields(ServerConfig)}
    for required in (
        "transport",
        "port",
        "uds",
        "project_dir",
        "debug",
        "no_tui",
        "advanced",
        "no_index",
    ):
        assert required in field_names, f"ServerConfig missing field: {required}"


def test_server_config_from_cli_args_builds_from_click_decoded_kwargs(
    tmp_path: Path,
) -> None:
    """``from_cli_args`` translates the dict click hands to the
    subcommand callback into a ``ServerConfig`` with the same values.

    The legacy ``server_cmd`` callback signature is the canonical
    shape; ``ServerConfig.from_cli_args`` MUST accept that exact
    keyword set so swapping the call site is a one-liner.
    """
    from agent_mcp.server_bootstrap import ServerConfig

    project_dir = tmp_path / "p"
    project_dir.mkdir()
    cfg = ServerConfig.from_cli_args(
        port=8080,
        uds=None,
        transport="sse",
        project_dir=str(project_dir),
        debug=False,
        no_tui=True,
        advanced=False,
        no_index=False,
    )
    assert cfg.transport == "sse"
    assert cfg.port == 8080
    assert cfg.uds is None
    assert cfg.project_dir == str(project_dir)
    assert cfg.no_tui is True


def test_server_config_validates_transport_value(tmp_path: Path) -> None:
    """Unknown transport values fail at config-build time, not after
    the DB is initialised — surface bad input early."""
    from agent_mcp.server_bootstrap import ServerConfig

    project_dir = tmp_path / "p"
    project_dir.mkdir()
    with pytest.raises((ValueError, TypeError, AssertionError)):
        ServerConfig.from_cli_args(
            port=8080,
            uds=None,
            transport="bogus",  # neither 'sse' nor 'stdio'
            project_dir=str(project_dir),
            debug=False,
            no_tui=False,
            advanced=False,
            no_index=False,
        )


def test_server_config_normalizes_project_dir_to_absolute(tmp_path: Path) -> None:
    """The bootstrap must resolve the project dir before passing it on
    — ``application_startup`` reads ``MCP_PROJECT_DIR`` and a relative
    path here would resolve against whatever cwd happens to be active
    when a background task fires, not the user's intent."""
    from agent_mcp.server_bootstrap import ServerConfig

    project_dir = tmp_path / "p"
    project_dir.mkdir()
    cfg = ServerConfig.from_cli_args(
        port=8080,
        uds=None,
        transport="sse",
        project_dir=str(project_dir),
        debug=False,
        no_tui=True,
        advanced=False,
        no_index=False,
    )
    assert Path(cfg.project_dir).is_absolute()


# --- .env discovery ---------------------------------------------------
#
# ``test_load_dotenv_walks_parent_chain`` and
# ``test_load_dotenv_is_safe_when_no_env_file_exists`` were deleted
# (arch-r3 #6b): both exercised only
# ``server_bootstrap.load_project_dotenv``, which was itself removed
# as dead code — it was a dead duplicate of the .env walk already
# inlined at the top of ``cli.py``; nothing called the
# ``server_bootstrap`` copy.


# --- Embedding-mode + indexing flag propagation -----------------------


def test_apply_runtime_flags_sets_advanced_embeddings(tmp_path: Path) -> None:
    """``apply_runtime_flags`` flips ``core.config.ADVANCED_EMBEDDINGS``
    when ``advanced=True`` so downstream callers (RAG indexer) see the
    larger-dim model via ``embedding_settings()``."""
    from agent_mcp import server_bootstrap
    from agent_mcp.core import config as core_config

    project_dir = tmp_path / "p"
    project_dir.mkdir()
    cfg = _default_config(project_dir=str(project_dir), advanced=True)

    original = core_config.ADVANCED_EMBEDDINGS
    try:
        server_bootstrap.apply_runtime_flags(cfg)
        assert core_config.ADVANCED_EMBEDDINGS is True
        # embedding_settings() also resolves to advanced.
        assert (
            core_config.embedding_settings().model
            == core_config.ADVANCED_EMBEDDING_MODEL
        )
    finally:
        core_config.ADVANCED_EMBEDDINGS = original


def test_apply_runtime_flags_disables_auto_indexing(tmp_path: Path) -> None:
    """``--no-index`` flips ``DISABLE_AUTO_INDEXING`` so the RAG indexer
    skips its periodic scan."""
    from agent_mcp import server_bootstrap
    from agent_mcp.core import config as core_config

    project_dir = tmp_path / "p"
    project_dir.mkdir()
    cfg = _default_config(project_dir=str(project_dir), no_index=True)

    original = core_config.DISABLE_AUTO_INDEXING
    try:
        server_bootstrap.apply_runtime_flags(cfg)
        assert core_config.DISABLE_AUTO_INDEXING is True
    finally:
        core_config.DISABLE_AUTO_INDEXING = original


def test_apply_runtime_flags_promotes_debug_to_env_var(tmp_path: Path) -> None:
    """``--debug`` MUST end up in ``os.environ['MCP_DEBUG']`` before
    ``create_app`` runs — Starlette's debug mode is built from the env
    var at app construction time."""
    from agent_mcp import server_bootstrap

    project_dir = tmp_path / "p"
    project_dir.mkdir()
    cfg = _default_config(project_dir=str(project_dir), debug=True)
    server_bootstrap.apply_runtime_flags(cfg)
    assert os.environ.get("MCP_DEBUG") == "true"

    # And the inverse: debug=False must NOT leave a stale "true" behind.
    cfg2 = _default_config(project_dir=str(project_dir), debug=False)
    server_bootstrap.apply_runtime_flags(cfg2)
    assert os.environ.get("MCP_DEBUG") == "false"


# --- bootstrap_server: app + teardown ---------------------------------


def test_bootstrap_server_returns_starlette_app_and_teardown(
    tmp_path: Path,
) -> None:
    """``bootstrap_server`` returns a fully-wired ``Starlette`` instance
    plus a teardown callable (idempotent — the SystemExit/KeyboardInterrupt
    paths in the CLI runner may invoke it more than once)."""
    from starlette.applications import Starlette
    from agent_mcp import server_bootstrap

    project_dir = tmp_path / "p"
    project_dir.mkdir()
    cfg = _default_config(project_dir=str(project_dir), transport="sse")

    app, teardown = server_bootstrap.bootstrap_server(cfg)
    assert isinstance(app, Starlette)
    assert callable(teardown)

    # Idempotency contract: calling teardown twice doesn't raise.
    teardown()
    teardown()


def test_bootstrap_server_stdio_does_not_build_starlette(
    tmp_path: Path,
) -> None:
    """For stdio transport there's no Starlette app — ``bootstrap_server``
    returns ``None`` for the app so the caller knows to route through
    the stdio runner instead."""
    from agent_mcp import server_bootstrap

    project_dir = tmp_path / "p"
    project_dir.mkdir()
    cfg = _default_config(project_dir=str(project_dir), transport="stdio")

    app, teardown = server_bootstrap.bootstrap_server(cfg)
    assert app is None, "stdio transport should not build a Starlette app"
    assert callable(teardown)


def test_cli_imports_server_bootstrap_module() -> None:
    """The thin-adapter ``cli.py`` MUST delegate to the bootstrap module
    — if a future refactor accidentally inlines the boot logic back
    into ``cli.py``, this guard catches it."""
    import agent_mcp.cli as cli_module

    # The bootstrap module is imported (used) by cli — check at the
    # module attribute level rather than ``inspect.getsource`` so the
    # test is resilient to formatting changes.
    sources = (cli_module.__file__ or "")
    assert sources, "cli.__file__ unavailable"
    text = Path(sources).read_text(encoding="utf-8")
    assert "server_bootstrap" in text, (
        "cli.py no longer references server_bootstrap — boot logic may "
        "have been re-inlined."
    )


# --- startup banner consumer ------------------------------------------


def test_print_startup_banner_does_not_raise(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``_print_startup_banner`` runs cleanly against a default
    ServerConfig. retire-system-token Wave 3 deleted the system-token
    log branch from the banner entirely."""
    from agent_mcp.server_bootstrap import _print_startup_banner

    cfg = _default_config(transport="sse", port=8080)
    _print_startup_banner(cfg)

    captured = capsys.readouterr()
    # Sanity: the banner ran far enough to print the "running on port" line.
    assert "MCP Server" in captured.out


# --- helpers ----------------------------------------------------------


def _default_config(**overrides: Any):
    """Build a ServerConfig with sensible defaults for tests.

    Centralised so adding a new ServerConfig field doesn't require
    editing every test signature.
    """
    from agent_mcp.server_bootstrap import ServerConfig

    project_dir = overrides.pop("project_dir", "/tmp/agent-mcp-test-default")
    return ServerConfig.from_cli_args(
        port=overrides.pop("port", 8080),
        uds=overrides.pop("uds", None),
        transport=overrides.pop("transport", "sse"),
        project_dir=project_dir,
        debug=overrides.pop("debug", False),
        no_tui=overrides.pop("no_tui", True),
        advanced=overrides.pop("advanced", False),
        no_index=overrides.pop("no_index", False),
        **overrides,
    )


# ─── F015 v7 regression: HMAC key loader must not strip raw bytes ───
# Pre-fix `_load_forwarding_hmac_key` called `data.strip()` on a
# binary HMAC key blob written by the systemd unit's ExecStartPre
# (`head -c 32 /dev/urandom > $RUNTIME_DIRECTORY/forwarding_hmac`).
# When /dev/urandom produced a key starting OR ending with bytes
# that happen to be ASCII whitespace (\n=0x0a, \r=0x0d, space=0x20,
# \t=0x09, \v=0x0b, \f=0x0c), the loaded key was shorter than 32
# bytes; the router (which does NOT strip) signed with the full
# 32 bytes; every HMAC verify failed; every cookie-authenticated
# /mcp request 401'd.

def test_load_forwarding_hmac_key_preserves_leading_whitespace_byte(tmp_path):
    """A 32-byte key whose first byte is \\n must load as 32 bytes,
    not 31. Stripping any byte would have shortened the key and broken
    HMAC verify against the unchanged router-side bytes.
    """
    from agent_mcp.server_bootstrap import _load_forwarding_hmac_key
    from agent_mcp.core import globals as g

    key = b"\n" + bytes(range(31))  # 32 bytes, first is \n (0x0a)
    keyfile = tmp_path / "forwarding_hmac"
    keyfile.write_bytes(key)

    g.forwarding_hmac_key = None  # ensure clean slate
    _load_forwarding_hmac_key(str(keyfile))

    assert g.forwarding_hmac_key == key, (
        "Loader must preserve leading whitespace byte; "
        f"got {len(g.forwarding_hmac_key)} bytes "
        f"({g.forwarding_hmac_key[:4]!r}), expected 32 ({key[:4]!r})"
    )


def test_load_forwarding_hmac_key_preserves_trailing_whitespace_byte(tmp_path):
    """Same defence for trailing \\n / \\r etc."""
    from agent_mcp.server_bootstrap import _load_forwarding_hmac_key
    from agent_mcp.core import globals as g

    key = bytes(range(31)) + b"\n"  # 32 bytes, last is \n
    keyfile = tmp_path / "forwarding_hmac"
    keyfile.write_bytes(key)

    g.forwarding_hmac_key = None
    _load_forwarding_hmac_key(str(keyfile))

    assert g.forwarding_hmac_key == key
    assert len(g.forwarding_hmac_key) == 32


def test_load_forwarding_hmac_key_empty_file_is_dormant(tmp_path):
    """Empty file still leaves the key None (forwarding-header auth
    dormant). The strip() removal must not regress this safety net."""
    from agent_mcp.server_bootstrap import _load_forwarding_hmac_key
    from agent_mcp.core import globals as g

    keyfile = tmp_path / "forwarding_hmac"
    keyfile.write_bytes(b"")

    g.forwarding_hmac_key = b"prior-value"
    _load_forwarding_hmac_key(str(keyfile))

    assert g.forwarding_hmac_key is None
