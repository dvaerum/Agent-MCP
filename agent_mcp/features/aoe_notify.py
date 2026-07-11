# Agent-MCP/agent_mcp/features/aoe_notify.py
"""Agents-of-Empires (AoE) notification side-channel.

When a message is delivered via `send_agent_message`, we ALSO want the
recipient's tmux pane to be poked out-of-band so the running claude
session notices the new message even if the worker hasn't polled
`get_agent_messages` recently. AoE (``njbrake/agent-of-empires``) is a
Rust tmux wrapper that already exposes an HTTP API for typing keys
into managed sessions; this module piggy-backs on it.

Design notes:

* Best-effort. Every call is fire-and-forget; failures are logged and
  swallowed. The message is already persisted to SQLite by the time
  we get here — the MCP tool MUST still return success even if AoE
  is down, unreachable, in read-only mode, etc.

* Cache-on-first-send. AoE generates 16-hex session ids that we have
  to discover by listing ``/api/sessions`` and matching by
  ``title == recipient_id``. We cache the mapping in process. On a
  404 from ``/sessions/<id>/send`` (session was restarted) we drop the
  cached id and re-resolve once.

* Privacy. We NEVER pass the message body to AoE — admin tokens
  occasionally appear in message text, and AoE types its payload
  verbatim into the recipient's pane (and from there into the wider
  world via the agent's logs). Templates are validated at use-time:
  ``{content}``, ``{body}``, ``{message}`` and any case-variant is
  rejected, leaving only ``{sender}``, ``{recipient}``, ``{message_id}``.

* Per-project config (project_context keys):

  - ``config_aoe_notify_enabled``  (bool, default ``false``)
  - ``config_aoe_base_url``        (str, default ``http://127.0.0.1:8181``)
  - ``config_aoe_bearer_token``    (str, **secret** — matches the
    ``_SECRET_KEY_RE`` redaction filter so workers can't read it)
  - ``config_aoe_notify_template`` (str, default below)
  - ``config_aoe_timeout_ms``      (int, default 2000)

The integration site is ``send_agent_message_tool_impl`` in
``tools/agent_communication_tools.py`` — see that file for the
``asyncio.create_task(notify_aoe(...))`` call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import httpx

from ..core.config import logger
from ..db.connection import get_db_connection


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "http://127.0.0.1:8181"
DEFAULT_TEMPLATE = (
    "[agent-mcp] New message from {sender}. "
    "Call get_agent_messages to read."
)
DEFAULT_TIMEOUT_MS = 2000

# Placeholders that MUST NOT appear in the template. Matched
# case-insensitively because AoE just types whatever we give it; we
# don't want a sloppy admin to accidentally write ``{Content}`` and
# leak admin tokens that appear in message bodies.
_FORBIDDEN_PLACEHOLDERS = ("content", "body", "message")

# Test hook: when set to an ``httpx.MockTransport`` instance, the
# notifier uses it instead of the real network. Production code does
# not touch this attribute.
_TRANSPORT_FOR_TESTS: Optional[httpx.AsyncBaseTransport] = None

# Recipient agent_id → AoE session id (16-hex string). Cleared on
# backend restart; refreshed on miss; invalidated on 404.
_SESSION_ID_CACHE: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Public test/ops helpers
# ---------------------------------------------------------------------------

def clear_session_cache() -> None:
    """Drop every cached recipient→AoE-id mapping.

    Tests call this between cases to avoid bleed; ops can call it from
    a REPL if AoE was restarted out from under us.
    """
    _SESSION_ID_CACHE.clear()


def validate_template(template: str) -> None:
    """Raise ``ValueError`` if the template references a forbidden placeholder.

    Forbidden: ``{content}``, ``{body}``, ``{message}`` and any case
    variant — they would let admin tokens or other sensitive message
    content escape into AoE-typed text. Allowed: ``{sender}``,
    ``{recipient}``, ``{message_id}``, or no placeholders at all.
    """
    # Find every ``{xxx}`` (no nested braces, no formatting spec).
    for raw in re.findall(r"\{([^{}]+)\}", template):
        name = raw.strip().lower()
        if name in _FORBIDDEN_PLACEHOLDERS:
            raise ValueError(
                f"AoE notification template references forbidden "
                f"placeholder {{{raw}}}; allowed: "
                f"{{sender}}, {{recipient}}, {{message_id}}"
            )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AoeConfig:
    enabled: bool
    base_url: str
    bearer_token: Optional[str]
    template: str
    timeout_ms: int
    bearer_token_file: Optional[str] = None


def _resolve_bearer_token(cfg: AoeConfig) -> Optional[str]:
    """Pick the active bearer token.

    Priority (highest first):
      1. ``config_aoe_bearer_token``  (inline, "explicit wins")
      2. ``config_aoe_bearer_token_file`` → read first line of file

    AoE rotates ``~/.config/agent-of-empires/serve.token`` on a
    schedule, so admins who want zero-touch operation point the
    file-key at that path; admins who want pinned credentials use the
    inline key. Reads happen on every send — cheap (single open of a
    64-byte file) and means rotations are picked up live.
    """
    if cfg.bearer_token:
        return cfg.bearer_token
    if cfg.bearer_token_file:
        try:
            with open(cfg.bearer_token_file, "r") as f:
                # AoE writes a single token with a trailing newline;
                # be lenient about either.
                first = f.readline().strip()
            if not first:
                logger.warning(
                    "aoe_notify: token file %s is empty",
                    cfg.bearer_token_file,
                )
                return None
            return first
        except FileNotFoundError:
            logger.warning(
                "aoe_notify: token file %s not found",
                cfg.bearer_token_file,
            )
            return None
        except PermissionError:
            logger.warning(
                "aoe_notify: token file %s not readable (check 0600 + ownership)",
                cfg.bearer_token_file,
            )
            return None
        except OSError as e:
            logger.warning(
                "aoe_notify: token file %s read failed: %s",
                cfg.bearer_token_file, e,
            )
            return None
    return None


def _get_stored_aoe_session_id(agent_id: str) -> Optional[str]:
    """Return ``agents.aoe_session_id`` for ``agent_id`` (None if unset/missing).

    Tolerates the column being absent (legacy DB pre-migration) by
    returning None — the caller will then fall back to title-match
    resolution against /api/sessions.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT aoe_session_id FROM agents WHERE agent_id = ?",
            (agent_id,),
        )
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        logger.debug("aoe_notify: agents.aoe_session_id lookup failed: %s", e)
        return None
    if not row:
        return None
    val = row["aoe_session_id"]
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _read_ctx(key: str) -> Optional[str]:
    """Return the raw project_context value for ``key`` (or None).

    Strips one outer pair of double quotes (project_context stores
    everything as JSON-encoded strings, so a bare string ``"foo"``
    becomes the literal ``"foo"`` here).
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT value FROM project_context WHERE context_key = ?", (key,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:  # pragma: no cover — DB outage unlikely in tests
        logger.warning("aoe_notify: failed to read %s: %s", key, e)
        return None
    if not row:
        return None
    raw = row["value"]
    if isinstance(raw, str):
        return raw.strip().strip('"')
    return str(raw)


def _read_bool(key: str, default: bool) -> bool:
    # Route through the canonical config-read seam in tools.access so the
    # bool-coercion table lives in exactly one place (was duplicated here).
    from ..tools.access import _get_config_bool

    return _get_config_bool(key, default)


def _read_int(key: str, default: int) -> int:
    raw = _read_ctx(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def load_config() -> AoeConfig:
    return AoeConfig(
        enabled=_read_bool("config_aoe_notify_enabled", False),
        base_url=(_read_ctx("config_aoe_base_url") or DEFAULT_BASE_URL).rstrip("/"),
        bearer_token=_read_ctx("config_aoe_bearer_token"),
        bearer_token_file=_read_ctx("config_aoe_bearer_token_file"),
        template=_read_ctx("config_aoe_notify_template") or DEFAULT_TEMPLATE,
        timeout_ms=_read_int("config_aoe_timeout_ms", DEFAULT_TIMEOUT_MS),
    )


# ---------------------------------------------------------------------------
# AoE HTTP client
# ---------------------------------------------------------------------------

def _build_client(cfg: AoeConfig) -> httpx.AsyncClient:
    """Construct an httpx.AsyncClient for AoE.

    Tests set ``_TRANSPORT_FOR_TESTS`` so this returns a client backed
    by an ``httpx.MockTransport``; production goes over the network.

    The bearer token is resolved at client-build time via
    ``_resolve_bearer_token`` so a file-sourced token rotation is
    picked up on the next ``_build_client`` call without a server
    restart.
    """
    timeout = httpx.Timeout(cfg.timeout_ms / 1000.0)
    kwargs: dict = {"base_url": cfg.base_url, "timeout": timeout}
    if _TRANSPORT_FOR_TESTS is not None:
        kwargs["transport"] = _TRANSPORT_FOR_TESTS
    headers: dict[str, str] = {}
    token = _resolve_bearer_token(cfg)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if headers:
        kwargs["headers"] = headers
    return httpx.AsyncClient(**kwargs)


async def _resolve_aoe_id(
    client: httpx.AsyncClient, recipient_id: str
) -> Optional[str]:
    """Look up the AoE session id whose ``title`` equals ``recipient_id``.

    Returns ``None`` if the recipient has no AoE session — that's a
    common case (the recipient may be a worker running outside AoE)
    and is logged at WARNING but not raised.
    """
    try:
        resp = await client.get("/api/sessions")
    except Exception as e:
        logger.warning("aoe_notify: GET /api/sessions failed: %s", e)
        return None
    if resp.status_code != 200:
        logger.warning(
            "aoe_notify: GET /api/sessions returned %s", resp.status_code
        )
        return None
    try:
        sessions = resp.json().get("sessions") or []
    except Exception as e:
        logger.warning("aoe_notify: /api/sessions body unparseable: %s", e)
        return None
    for s in sessions:
        if s.get("title") == recipient_id:
            return s.get("id")
    logger.warning(
        "aoe_notify: no AoE session with title=%r among %d sessions",
        recipient_id, len(sessions),
    )
    return None


async def _post_send(
    client: httpx.AsyncClient, aoe_id: str, message: str
) -> int:
    """POST /api/sessions/<id>/send. Returns HTTP status (0 on transport error)."""
    try:
        resp = await client.post(
            f"/api/sessions/{aoe_id}/send",
            json={"message": message, "revive": True},
        )
        return resp.status_code
    except Exception as e:
        logger.warning("aoe_notify: POST send failed: %s", e)
        return 0


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

async def notify_aoe(recipient_id: str, sender_id: str, message_id: str) -> None:
    """Best-effort: ping the recipient's tmux pane via AoE.

    Never raises. Schedule with ``asyncio.create_task`` from the
    message-send path; safe to await directly in tests.
    """
    try:
        cfg = load_config()
        if not cfg.enabled:
            return
        try:
            validate_template(cfg.template)
        except ValueError as e:
            logger.warning("aoe_notify: invalid template, skipping: %s", e)
            return
        message = cfg.template.format(
            sender=sender_id,
            recipient=recipient_id,
            message_id=message_id,
        )

        # Token sanity: if the admin configured a token-file path but
        # the file isn't usable, give up early — there's no point in
        # talking to AoE without auth, and most AoE deployments require
        # the bearer (read-only mode is the exception).
        if (
            cfg.bearer_token_file
            and not cfg.bearer_token
            and _resolve_bearer_token(cfg) is None
        ):
            return

        async with _build_client(cfg) as client:
            # Per-agent stored binding takes precedence over title-match
            # resolution. Cache holds the most-recent successful id;
            # stored value is consulted on cache miss before falling
            # back to /api/sessions.
            aoe_id = _SESSION_ID_CACHE.get(recipient_id)
            if aoe_id is None:
                aoe_id = _get_stored_aoe_session_id(recipient_id)
                if aoe_id is None:
                    aoe_id = await _resolve_aoe_id(client, recipient_id)
                if aoe_id is None:
                    return
                _SESSION_ID_CACHE[recipient_id] = aoe_id

            status = await _post_send(client, aoe_id, message)
            if status == 404:
                # Cached id is stale (AoE session was restarted). Drop
                # it, re-resolve, and try once more.
                logger.info(
                    "aoe_notify: AoE 404 for %s; invalidating cache and retrying",
                    recipient_id,
                )
                _SESSION_ID_CACHE.pop(recipient_id, None)
                aoe_id = await _resolve_aoe_id(client, recipient_id)
                if aoe_id is None:
                    return
                _SESSION_ID_CACHE[recipient_id] = aoe_id
                status = await _post_send(client, aoe_id, message)
                if status != 200:
                    logger.warning(
                        "aoe_notify: retry POST returned %s for %s",
                        status, recipient_id,
                    )
            elif status != 200:
                logger.warning(
                    "aoe_notify: POST returned %s for %s",
                    status, recipient_id,
                )
    except Exception as e:
        # Belt-and-suspenders. notify_aoe is fire-and-forget; an
        # uncaught exception inside an ``asyncio.create_task`` would
        # spam the logs with "Task exception was never retrieved".
        logger.warning("aoe_notify: unexpected error: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# Health check (admin-facing)
# ---------------------------------------------------------------------------

async def check_health() -> dict:
    """Probe AoE with the current credentials and report status.

    Returns a small dict shaped for the dashboard:

      {"status": "ok",            "session_count": N, ...}
      {"status": "disabled",      "message": "..."}
      {"status": "unauthorized",  "message": "AoE returned 401 ..."}
      {"status": "unreachable",   "message": "..."}
      {"status": "misconfigured", "message": "no bearer token resolved"}

    The endpoint (`/api/aoe/health`) is admin-only — see routes.py.
    """
    cfg = load_config()
    if not cfg.enabled:
        return {
            "status": "disabled",
            "message": "config_aoe_notify_enabled is off",
        }

    # If a token source is configured, make sure we can actually use
    # it before we pester AoE.
    if cfg.bearer_token or cfg.bearer_token_file:
        token = _resolve_bearer_token(cfg)
        if token is None:
            return {
                "status": "misconfigured",
                "message": (
                    "config_aoe_bearer_token_file is set but the file "
                    "could not be read (see server log)"
                ),
            }

    try:
        async with _build_client(cfg) as client:
            try:
                resp = await client.get("/api/sessions")
            except httpx.TimeoutException:
                return {
                    "status": "unreachable",
                    "message": f"AoE timed out at {cfg.base_url}",
                }
            except Exception as e:
                return {
                    "status": "unreachable",
                    "message": f"AoE at {cfg.base_url} unreachable: {e}",
                }

            if resp.status_code == 401 or resp.status_code == 403:
                return {
                    "status": "unauthorized",
                    "message": (
                        f"AoE returned {resp.status_code} — bearer token "
                        "is missing, stale, or rejected"
                    ),
                }
            if resp.status_code != 200:
                return {
                    "status": "unreachable",
                    "message": f"AoE returned {resp.status_code}",
                }
            try:
                sessions = resp.json().get("sessions") or []
            except Exception as e:
                return {
                    "status": "unreachable",
                    "message": f"AoE /api/sessions body unparseable: {e}",
                }
            return {
                "status": "ok",
                "session_count": len(sessions),
                "base_url": cfg.base_url,
            }
    except Exception as e:
        return {
            "status": "unreachable",
            "message": f"AoE health check failed: {e}",
        }
