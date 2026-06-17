#!/usr/bin/env python3
"""
Agent-MCP CLI: command-line interface for multi-agent collaboration.

Post-PR-E (round-2 architecture review): this module is a thin click
adapter. It owns the user-facing surface — argparse / click
decorators, deprecation shims, the `router` + `backup` subcommands
that don't go through the server boot path — and delegates the
``server`` subcommand's orchestration to
``agent_mcp.server_bootstrap.run_server``.

What used to live here and now lives in dedicated modules:

* ``.env`` discovery, embedding-mode flag translation, debug-flag
  promotion, transport branching, uvicorn config, anyio task-group
  setup → ``agent_mcp.server_bootstrap``.
* TUI display loop → ``agent_mcp.tui.runtime``.
* DB admin-token reader (used by the TUI + startup banner) →
  ``agent_mcp.server_bootstrap.get_admin_token_from_db``.

What stays here on purpose:

* The click ``cli`` group + subcommand registrations — this is the
  user-facing surface.
* The pre-import ``.env`` walk (the same loop the legacy CLI ran).
  ``core.config`` reads ``OPENAI_API_KEY`` at module-import time, so
  the discovery has to run BEFORE any other ``agent_mcp`` import; we
  can't move this block into ``server_bootstrap`` because importing
  that module triggers ``core.config`` as a side effect.
* The ``router`` + ``backup`` subcommands — separate concerns from
  the server boot path (the router is its own aiohttp app; the backup
  command is a one-shot SQLite copy). They keep their callbacks here.
* The legacy-invocation sniffer / deprecation shim — same surface as
  before, no rewrites.

Copyright (C) 2025 Luis Alejandro Rincon (rinadelph)

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
# --- Pre-import .env discovery ---------------------------------------
# Done inline here (rather than via server_bootstrap) because
# ``core.config`` reads ``OPENAI_API_KEY`` from the environment at
# module-import time. Any ``from agent_mcp.*`` import below — even a
# transitive one through server_bootstrap — locks in the env state
# at that point. So we walk the .env discovery as the very first
# thing, then import the rest.
import os
import sys
import sqlite3
import warnings
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

_script_dir = Path(__file__).resolve().parent
for _parent_level in range(3):
    _env_path = (_script_dir / ("../" * _parent_level) / ".env").resolve()
    print(f"Trying to load .env from: {_env_path}")
    if _env_path.exists():
        print(f"Found .env at: {_env_path}")
        _env_vars = dotenv_values(str(_env_path))
        print(f"Loaded variables: {list(_env_vars.keys())}")
        # Avoid printing secrets in plaintext while still confirming
        # they're present.
        _printable_key = _env_vars.get("OPENAI_API_KEY", "NOT FOUND") or "NOT FOUND"
        print(f"OPENAI_API_KEY from file: {_printable_key[:10]}...")
        for _key, _value in _env_vars.items():
            if _value is not None:
                os.environ[_key] = _value
        if os.environ.get("OPENAI_API_KEY"):
            print("OPENAI_API_KEY successfully loaded from environment")
        else:
            print("OPENAI_API_KEY not found in environment")
        break

# Also try cwd-relative load_dotenv in case the deploy repo's
# wrapper script puts the .env somewhere this walk doesn't find.
load_dotenv()

# Cleanup of loop locals so a future reader doesn't mistake them for
# config surface.
del _script_dir, _parent_level
for _local in ("_env_path", "_env_vars", "_printable_key", "_key", "_value", "_local"):
    if _local in dir():
        try:
            del globals()[_local]
        except KeyError:
            pass

# --- Now safe to import core.config (it reads env at import time) ----
from typing import Optional  # noqa: E402

import click  # noqa: E402

from .server_bootstrap import (  # noqa: E402
    ServerConfig,
    get_admin_token_from_db,
    run_server,
)


# --- Click command group --------------------------------------------
# Two subcommands today:
#   * ``agent-mcp server …``  — the MCP backend (Starlette/uvicorn or stdio).
#   * ``agent-mcp router …``  — the always-on URL-keyed HTTP router that
#                                proxies per-project backends.
#   * ``agent-mcp backup …``  — full-DB sqlite3 online backup.
# Phase 1a of the router-upstream plan (prancy-napping-pie) converted
# the pre-existing single ``@click.command`` into the group below;
# the backward-compat shim at the bottom routes legacy top-level
# flag invocations through ``server``.
@click.group(
    context_settings=dict(help_option_names=["-h", "--help"]),
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Agent-MCP command-line interface.

    Run ``agent-mcp server --help`` for the MCP backend, or
    ``agent-mcp router --help`` for the always-on HTTP router.
    """
    if ctx.invoked_subcommand is None:
        # Empty invocation: keep the historic behaviour of starting
        # the server with defaults. Emit a deprecation note so we can
        # remove this in a future release.
        warnings.warn(
            "Invoking agent-mcp with no subcommand is deprecated; "
            "use 'agent-mcp server' (or 'agent-mcp router') instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        ctx.invoke(server_cmd)


# --- ``server`` subcommand ------------------------------------------
# Option set is unchanged from pre-PR-E so the deploy repo's wrapper
# scripts and the legacy-invocation shim keep working bit-for-bit;
# only the callback body is now a thin two-liner that hands off to
# ``server_bootstrap.run_server``.
@cli.command("server", context_settings=dict(help_option_names=["-h", "--help"]))
@click.option(
    "--port",
    type=int,
    default=os.environ.get("PORT", 8080),
    show_default=True,
    help="Port to listen on for SSE and HTTP dashboard.",
)
@click.option(
    "--uds",
    type=str,
    default=None,
    help=(
        "Unix domain socket path. When set, the server listens on this "
        "socket instead of host:port (SSE transport only). Useful for "
        "reverse-proxy deployments that want to avoid exposing TCP."
    ),
)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse"], case_sensitive=False),
    default="sse",
    show_default=True,
    help="Transport type for MCP communication (stdio or sse).",
)
@click.option(
    "--project-dir",
    type=click.Path(file_okay=False, dir_okay=True, resolve_path=True, writable=True),
    default=".",
    show_default=True,
    help="Project directory. The .agent folder will be created/used here. Defaults to current directory.",
)
@click.option(
    "--admin-token",
    "admin_token_cli",
    type=str,
    default=None,
    help="Admin token for authentication. If not provided, one will be loaded from DB or generated.",
)
@click.option(
    "--admin-token-out",
    "admin_token_out_path",
    type=click.Path(dir_okay=False, resolve_path=True),
    default=None,
    help=(
        "Write the resolved admin token to this file on startup "
        "(mode 0600). Use --admin-token-format to pick raw vs "
        "MCP_ADMIN_TOKEN=<token> output."
    ),
)
@click.option(
    "--admin-token-format",
    "admin_token_out_format",
    type=click.Choice(["raw", "env"], case_sensitive=False),
    default="raw",
    show_default=True,
    help=(
        "Format for --admin-token-out: 'raw' writes just the token; "
        "'env' writes MCP_ADMIN_TOKEN=<token>. No effect without "
        "--admin-token-out."
    ),
)
@click.option(
    "--admin-token-in",
    "admin_token_in_path",
    type=click.Path(dir_okay=False, resolve_path=True, exists=True),
    default=None,
    help=(
        "Read the admin token from this file at startup. Overrides "
        "any token stored in the DB and any --admin-token value."
    ),
)
@click.option(
    "--admin-token-log",
    is_flag=True,
    default=False,
    help=(
        "Log the admin token to stdout/log on startup (opt-in; the "
        "default is silent — operators read the token from the TUI, "
        "the dashboard, or via --admin-token-out)."
    ),
)
@click.option(
    "--debug",
    is_flag=True,
    default=os.environ.get("MCP_DEBUG", "false").lower() == "true",
    help="Enable debug mode for the server (more verbose logging, Starlette debug pages).",
)
@click.option(
    "--no-tui",
    is_flag=True,
    default=False,
    help="Disable the terminal UI display (logs will still go to file).",
)
@click.option(
    "--advanced",
    is_flag=True,
    default=False,
    help="Enable advanced embeddings mode with larger dimension (3072) and more sophisticated code analysis.",
)
@click.option(
    "--git",
    is_flag=True,
    default=False,
    help="Enable experimental Git worktree support for parallel agent development (advanced users only).",
)
@click.option(
    "--no-index",
    is_flag=True,
    default=False,
    help="Disable automatic markdown file indexing. Allows selective manual indexing of specific content into the RAG system.",
)
def server_cmd(
    port: int,
    uds: Optional[str],
    transport: str,
    project_dir: str,
    admin_token_cli: Optional[str],
    admin_token_out_path: Optional[str],
    admin_token_out_format: str,
    admin_token_in_path: Optional[str],
    admin_token_log: bool,
    debug: bool,
    no_tui: bool,
    advanced: bool,
    git: bool,
    no_index: bool,
) -> None:
    """
    Start the MCP Server.

    The server supports two embedding modes:
      * Simple (default): text-embedding-3-large (1536 dimensions) —
        indexes markdown files and context.
      * Advanced (``--advanced``): text-embedding-3-large (3072
        dimensions) — includes code analysis, task indexing.

    Indexing options:
      * Default: automatic indexing of all markdown files in the
        project directory.
      * ``--no-index``: disable automatic markdown indexing for
        selective manual control.

    Switching between modes will require re-indexing all content.
    """
    # Validate the admin-token output / input / log flags. At most one
    # of out/in/log may be set; --admin-token-format only makes sense
    # with --admin-token-out. Caught at CLI time so the lifecycle code
    # never has to defend against a contradictory combination.
    sinks = sum(
        1
        for v in (admin_token_out_path, admin_token_in_path, admin_token_log)
        if v
    )
    if sinks > 1:
        raise click.UsageError(
            "--admin-token-out, --admin-token-in, and --admin-token-log "
            "are mutually exclusive — pick at most one."
        )
    # Click defaults --admin-token-format to "raw"; only error if the
    # operator explicitly passed a value AND no -out sink. We detect
    # "explicit" by looking at sys.argv (Click's get_current_context
    # would also work but argv is simpler here).
    if (
        admin_token_out_format
        and admin_token_out_format.lower() != "raw"
        and not admin_token_out_path
    ):
        raise click.UsageError(
            "--admin-token-format requires --admin-token-out."
        )
    if (
        "--admin-token-format" in sys.argv
        and not admin_token_out_path
    ):
        raise click.UsageError(
            "--admin-token-format requires --admin-token-out."
        )

    config = ServerConfig.from_cli_args(
        port=port,
        uds=uds,
        transport=transport,
        project_dir=project_dir,
        admin_token_cli=admin_token_cli,
        admin_token_out_path=admin_token_out_path,
        admin_token_out_format=admin_token_out_format.lower(),
        admin_token_in_path=admin_token_in_path,
        admin_token_log=admin_token_log,
        debug=debug,
        no_tui=no_tui,
        advanced=advanced,
        git=git,
        no_index=no_index,
    )
    run_server(config)


# --- ``router`` subcommand ------------------------------------------
# Thin wrapper around ``agent_mcp.router.app.main``. The underlying
# app reads its config from ``AGENT_MCP_*`` env vars at module import
# time — this subcommand sets defaults for them from CLI flags before
# doing the import, so users get both an ergonomic CLI and the env-
# var escape hatch the deploy repo currently uses.
#
# Phase 1 PR B of prancy-napping-pie promoted ``router`` from a leaf
# command to a click group so a sibling ``create-operator``
# subcommand could live alongside it. The default-invocation shape
# (``agent-mcp router --port … --sock-dir …``) is preserved via
# ``invoke_without_command=True`` plus an ``invoked_subcommand``
# check: passing flags with no subcommand still runs the router.
@cli.group(
    "router",
    context_settings=dict(help_option_names=["-h", "--help"]),
    invoke_without_command=True,
)
@click.option(
    "--port",
    type=int,
    default=lambda: int(os.environ.get("AGENT_MCP_ROUTER_PORT", "1337")),
    show_default="1337 (or $AGENT_MCP_ROUTER_PORT)",
    help="Port to listen on for the URL-keyed router.",
)
@click.option(
    "--projects-file",
    type=click.Path(dir_okay=False, resolve_path=True),
    default=lambda: os.environ.get(
        "AGENT_MCP_PROJECTS_FILE",
        str(
            Path(
                os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
            )
            / "agent-mcp"
            / "projects.local.json"
        ),
    ),
    show_default="~/.config/agent-mcp/projects.local.json",
    help="JSON file mapping project name → workspace path.",
)
@click.option(
    "--sock-dir",
    type=click.Path(file_okay=False, resolve_path=True),
    default=lambda: os.environ.get("AGENT_MCP_SOCK_DIR"),
    show_default="$AGENT_MCP_SOCK_DIR",
    help="Directory containing per-project Unix-domain backend sockets.",
)
@click.option(
    "--dashboard-dir",
    type=click.Path(file_okay=False, resolve_path=True),
    default=lambda: os.environ.get("AGENT_MCP_DASHBOARD_DIR"),
    show_default="$AGENT_MCP_DASHBOARD_DIR",
    help="Directory holding the Next.js static dashboard export.",
)
@click.option(
    "--external-url",
    type=str,
    default=lambda: os.environ.get("AGENT_MCP_EXTERNAL_URL"),
    show_default="$AGENT_MCP_EXTERNAL_URL",
    help="Base URL the router reachable at (used in copy-paste wiring snippets).",
)
@click.option(
    "--idle-sec",
    type=int,
    default=lambda: int(os.environ.get("AGENT_MCP_IDLE_SEC", str(4 * 60 * 60))),
    show_default="14400 (4h)",
    help="Idle seconds before stopping an inactive backend.",
)
@click.option(
    "--installer-template",
    type=click.Path(dir_okay=False, resolve_path=True),
    default=lambda: os.environ.get("AGENT_MCP_INSTALLER_TEMPLATE"),
    show_default="packaged installer.sh.in (or $AGENT_MCP_INSTALLER_TEMPLATE)",
    help="Path to the installer.sh.in template (env override).",
)
@click.option(
    "--readme-html",
    type=click.Path(dir_okay=False, resolve_path=True),
    default=lambda: os.environ.get("AGENT_MCP_README_HTML") or None,
    show_default="(none)",
    help="Optional README rendered to HTML, embedded in the index page.",
)
@click.option(
    "--asset-prefix",
    "asset_prefix",
    type=str,
    default=lambda: os.environ.get("AGENT_MCP_ASSET_PREFIX") or None,
    show_default="$AGENT_MCP_ASSET_PREFIX (default: /agent-mcp/__dashboard)",
    help=(
        "Runtime URL prefix substituted into the dashboard's "
        "sentinel-marked asset URLs on serve (Phase 4). Set this to "
        "match the path the dashboard is mounted at by your reverse "
        "proxy. Default ``/agent-mcp/__dashboard`` matches the "
        "router's own dashboard route table. One build artifact "
        "serves any prefix — no rebuild needed."
    ),
)
@click.option(
    "--single-tenant",
    "single_tenant_name",
    type=str,
    default=lambda: os.environ.get("AGENT_MCP_SINGLE_TENANT_NAME") or None,
    show_default="$AGENT_MCP_SINGLE_TENANT_NAME (else multi-tenant)",
    help=(
        "Run the router in single-tenant mode for the named project. "
        "Disables __create / __unregister / __rename (410); URLs naming "
        "any other project are 302-redirected to the configured one "
        "(W1; ADR-0008). Pair with --single-workspace."
    ),
)
@click.option(
    "--single-workspace",
    "single_tenant_workspace",
    type=click.Path(file_okay=False, resolve_path=True),
    default=lambda: os.environ.get("AGENT_MCP_SINGLE_TENANT_WORKSPACE") or None,
    show_default="$AGENT_MCP_SINGLE_TENANT_WORKSPACE",
    help=(
        "Workspace path for the single-tenant project. The home-manager "
        "module's ExecStartPre seeds projects.local.json with this entry "
        "before the router starts, so the router can still resolve the "
        "single project's UDS via its registry lookup."
    ),
)
@click.pass_context
def router_cmd(
    ctx: click.Context,
    port: int,
    projects_file: str,
    sock_dir: Optional[str],
    dashboard_dir: Optional[str],
    external_url: Optional[str],
    idle_sec: int,
    installer_template: Optional[str],
    readme_html: str,
    asset_prefix: Optional[str],
    single_tenant_name: Optional[str],
    single_tenant_workspace: Optional[str],
) -> None:
    """Run the always-on URL-keyed HTTP router.

    The router proxies ``/agent-mcp/<name>/*`` to the per-project
    backend over Unix-domain sockets and serves the shared Next.js
    static dashboard + index page at ``/agent-mcp/``.

    Sibling subcommands (``router create-operator``, etc.) reuse the
    same group; when one of those is invoked the body below short-
    circuits and the subcommand handler runs instead.
    """
    # Subcommand dispatch — see comment on @cli.group above.
    # Stash the resolved options on the context in case a subcommand
    # wants them (today none do, but it keeps the door open).
    ctx.ensure_object(dict)
    ctx.obj["projects_file"] = projects_file
    if ctx.invoked_subcommand is not None:
        return

    # Promote CLI flags to env vars so the app module's import-time
    # reads pick them up. The deploy repo still sets these env vars
    # directly via systemd; both paths converge here.
    os.environ["AGENT_MCP_ROUTER_PORT"] = str(port)
    os.environ["AGENT_MCP_PROJECTS_FILE"] = projects_file
    if sock_dir:
        os.environ["AGENT_MCP_SOCK_DIR"] = sock_dir
    if dashboard_dir:
        os.environ["AGENT_MCP_DASHBOARD_DIR"] = dashboard_dir
    if external_url:
        os.environ["AGENT_MCP_EXTERNAL_URL"] = external_url
    os.environ["AGENT_MCP_IDLE_SEC"] = str(idle_sec)
    if installer_template:
        os.environ["AGENT_MCP_INSTALLER_TEMPLATE"] = installer_template
    if readme_html:
        os.environ["AGENT_MCP_README_HTML"] = readme_html
    if asset_prefix is not None:
        os.environ["AGENT_MCP_ASSET_PREFIX"] = asset_prefix

    # Required env vars without defaults: surface a clean error
    # rather than a KeyError stack trace deep inside the app.
    required = {
        "AGENT_MCP_SOCK_DIR": "--sock-dir",
        "AGENT_MCP_DASHBOARD_DIR": "--dashboard-dir",
        "AGENT_MCP_EXTERNAL_URL": "--external-url",
    }
    missing = [
        f"{flag} (or ${env})"
        for env, flag in required.items()
        if not os.environ.get(env)
    ]
    if missing:
        raise click.UsageError(
            "router subcommand requires: " + ", ".join(missing)
        )

    # --single-tenant / --single-workspace must come as a pair (or
    # not at all). Catch the lopsided invocation here so the operator
    # gets a clean error rather than a router that's half-toggled.
    if (single_tenant_name is None) != (single_tenant_workspace is None):
        raise click.UsageError(
            "--single-tenant and --single-workspace must be passed together"
        )

    # Lazy import — the router module reads env at top level, so
    # importing it before the os.environ assignments above would
    # bind to stale (likely missing) values.
    from .router.app import make_app
    from aiohttp import web

    app = make_app(
        single_tenant_name=single_tenant_name,
        single_tenant_workspace=single_tenant_workspace,
    )
    # Same env-override-on-bind-host pattern as router.app.main —
    # used by the VM tests' module so qemu hostfwd can route in.
    host = os.environ.get("AGENT_MCP_ROUTER_HOST", "127.0.0.1")
    # ``shutdown_timeout=3.0`` matches ``router.app.main()`` — capping
    # the aiohttp drain window paired with the ``_drain_proxy_tasks``
    # on_shutdown hook keeps the SIGTERM-to-exit window inside
    # systemd's ``TimeoutStopSec=15s``, fixing the 90 s deploy outage
    # caused by long-lived MCP proxy streams.
    web.run_app(app, host=host, port=port, shutdown_timeout=3.0)


# --- ``router create-operator`` subcommand --------------------------
# Phase 1 PR B (operator-login, prancy-napping-pie). Wraps the same
# ``create_user`` code path the env-var bootstrap and the (PR-C)
# setup wizard use, so all three bootstrap routes share argon2
# hashing + retroactive project_membership semantics.
#
# Two password input shapes:
#   * ``--password-stdin``   read the password as the first line of
#                            stdin; designed for ``cat secret | …``
#                            and ``-c '…' <<<password`` invocations
#                            in scripts and systemd ExecStartPre.
#   * no flag                interactive ``getpass()`` prompt with
#                            confirmation; the operator-friendly path.
@router_cmd.command(
    "create-operator", context_settings=dict(help_option_names=["-h", "--help"])
)
@click.option(
    "--username",
    required=True,
    help="Username for the new operator account.",
)
@click.option(
    "--email",
    default=None,
    help="Optional email address (used by Phase 3 SSO linking).",
)
@click.option(
    "--password-stdin",
    "password_stdin",
    is_flag=True,
    default=False,
    help=(
        "Read the password from the first line of stdin instead of "
        "prompting. Use this for non-interactive provisioning "
        "(scripts, systemd ExecStartPre, container entrypoints)."
    ),
)
def router_create_operator_cmd(
    username: str,
    email: Optional[str],
    password_stdin: bool,
) -> None:
    """Create the first operator (or an additional one).

    Same code path as the env-var bootstrap and the (PR-C) setup
    wizard. The first operator created on a fresh router.db gets
    membership in every existing project; subsequent operators get
    no automatic memberships and need explicit grants.
    """
    if password_stdin:
        # First line of stdin only; everything else gets discarded.
        # Strip trailing newline but preserve internal whitespace in
        # case the operator picked a multi-word passphrase.
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            raise click.UsageError(
                "--password-stdin received an empty first line of stdin."
            )
    else:
        # Interactive prompt with confirmation. Click's hide_input
        # routes through getpass so the password doesn't echo.
        password = click.prompt(
            "Password", hide_input=True, confirmation_prompt=True
        )

    # Lazy import: identity pulls argon2-cffi at module load, no need
    # to pay that cost on every ``agent-mcp router …`` invocation
    # (the leaf one that runs the HTTP router, in particular).
    from .router.identity import (
        UsernameAlreadyExistsError,
        create_user,
        run_router_migrations_upgrade,
    )

    # Ensure router.db exists + is migrated before the insert. The
    # leaf ``router`` invocation would do this in its lifespan; the
    # standalone CLI path has to do it here.
    run_router_migrations_upgrade()
    try:
        user_id = create_user(
            username=username, password=password, email=email
        )
    except UsernameAlreadyExistsError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Created operator {username!r} (user_id={user_id}).")


# --- Backward-compatibility shim ------------------------------------
# Pre-Phase-1a invocations looked like:
#   python -m agent_mcp.cli --transport sse --uds /run/.../backend.sock \
#                           --project-dir /path --no-tui
# i.e. top-level flags, no subcommand. The deploy repo's wrapper
# script (``agent-mcp-backend``) used this exact shape. We keep one
# release of compatibility by sniffing argv and rerouting through
# the new ``server`` subcommand with a DeprecationWarning. Remove in
# a future PR once the deploy repo has switched over.
_TOP_LEVEL_FLAGS_THAT_NOW_BELONG_TO_SERVER = {
    "--port",
    "--uds",
    "--transport",
    "--project-dir",
    "--admin-token",
    "--debug",
    "--no-tui",
    "--advanced",
    "--git",
    "--no-index",
}


def _looks_like_legacy_top_level_invocation(argv: list[str]) -> bool:
    """True iff argv[1] is a flag that used to live on the top-level
    command but now lives under ``server``. Subcommands like ``server``
    and ``router`` never start with ``-``, so this is unambiguous."""
    if len(argv) < 2:
        return False
    first = argv[1]
    if not first.startswith("-"):
        return False
    # Accept both ``--flag`` and ``--flag=value``.
    head = first.split("=", 1)[0]
    return head in _TOP_LEVEL_FLAGS_THAT_NOW_BELONG_TO_SERVER


# --- ``backup`` subcommand ------------------------------------------
# Per item 12 of the 2026-06-02 database review. The previous
# project_context-only JSON dump was the only backup surface; this
# is a full-DB online backup via ``sqlite3.Connection.backup()``,
# which is safe under WAL (doesn't block writers).
@cli.command("backup", context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument(
    "project_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
)
@click.argument("output_path", type=click.Path(dir_okay=False))
@click.option(
    "--force/--no-force",
    default=False,
    help=(
        "Overwrite OUTPUT_PATH if it already exists. Without this "
        "flag, the command refuses to clobber an existing file."
    ),
)
def backup_cmd(project_dir: str, output_path: str, force: bool) -> None:
    """Back up a project's SQLite database to OUTPUT_PATH.

    Uses ``sqlite3.Connection.backup()`` — the canonical online backup
    API. Safe to run while the server is live; readers and writers
    keep going.

    PROJECT_DIR is the directory containing ``.agent/mcp_state.db``
    (the same path you'd pass to ``agent-mcp server --project-dir``).
    """
    src_path = Path(project_dir).resolve() / ".agent" / "mcp_state.db"
    dst_path = Path(output_path)

    if not src_path.exists():
        click.echo(f"Error: database not found at {src_path}", err=True)
        sys.exit(1)

    if dst_path.exists() and not force:
        click.echo(
            f"Error: output file {dst_path} already exists; "
            f"pass --force to overwrite",
            err=True,
        )
        sys.exit(1)

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    # If --force, remove the existing target so the backup writes a
    # fresh DB rather than appending to an unrelated sqlite file.
    if dst_path.exists() and force:
        dst_path.unlink()

    src = sqlite3.connect(str(src_path))
    dst = sqlite3.connect(str(dst_path))
    try:
        # progress callback gets (status, remaining, total); we don't
        # need to surface it interactively for now but the API leaves
        # the hook available for a future --progress flag.
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    click.echo(f"Backup complete: {dst_path}")


def main() -> None:
    """Public entry point.

    Handles the backward-compat rewrite (legacy top-level flags →
    ``server`` subcommand) before handing off to the click group.
    """
    if _looks_like_legacy_top_level_invocation(sys.argv):
        warnings.warn(
            "Top-level flags will be removed in a future release; "
            "use 'agent-mcp server …' (e.g. "
            "'agent-mcp server --transport sse …') instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        sys.argv = [sys.argv[0], "server", *sys.argv[1:]]
    cli()


# Public aliases. ``main_cli`` is referenced by ``agent_mcp/__main__.py``
# (the ``python -m agent_mcp`` entry point); pre-Phase-1a code also
# called the inner click command ``main_cli``. Keep the alias so old
# import paths don't break.
main_cli = main


# Re-export ``get_admin_token_from_db`` from this module too so any
# external script that imported it from ``agent_mcp.cli`` pre-PR-E
# keeps working. The canonical home is ``server_bootstrap``.
__all__ = [
    "backup_cmd",
    "cli",
    "get_admin_token_from_db",
    "main",
    "main_cli",
    "router_cmd",
    "server_cmd",
    "_looks_like_legacy_top_level_invocation",
]


# This allows running ``python -m agent_mcp.cli --port …``
if __name__ == "__main__":
    main()
