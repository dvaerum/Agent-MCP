"""MCP resources subsystem (plan Phase 3).

This package exposes per-agent "ambient state" via the MCP resources
surface — clients (Claude Code, custom MCP consumers, etc.) can read
the calling agent's inbox and status counters via the spec-standard
`resources/list` + `resources/read` requests.

Two resources are exposed per caller, both scoped to the caller's
agent_id (derived server-side from the bearer):

* ``agent-mcp://inbox/<agent_id>`` — JSON envelope identical to
  what `wait_for_events` returns: ``{"events": [...],
  "next_cursor": "..."}``. Backed by the shared
  ``_collect_events_for(agent_id, since)`` helper from Phase 2.

* ``agent-mcp://status/<agent_id>`` — JSON counters:
  ``{"unread_messages": N, "unfinished_tasks": M, ...}``. Reflects
  the agent's current state at query time.

The two URIs co-exist because the consumer needs are different:
inbox is the event timeline (cleared by reading/processing); status
is the ambient counter snapshot (always current).

Candidate B (architecture review 2026-06-02): the per-resource
metadata (URI prefix, short name, reader callable, MIME type) lives
in `resource_registry: ResourceRegistry` — a subclass of the shared
`Registry[T]`. The `agent-mcp://...` routing in
`mcp_read_resource_handler` walks `resource_registry`'s entries
instead of an if/elif chain, so future resources just register and
appear in both `resources/list` and `resources/read`.

Notification emission (`notifications/resources/updated` on open
`GET /mcp` streams) is intentionally NOT implemented in this PR.
Stateless StreamableHTTP mode (the project's chosen transport per
PR #61) does not expose an enumeration API for in-flight GET
sessions, so cross-request fan-out requires a custom session
registry that's out of scope for this Phase. The resources are
fully polled-readable; long-poll wake-on-event continues to flow
through `wait_for_events`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from ..core.auth import get_agent_id
from ..core.registry import Registry, RegistryEntry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.principal import Principal

# Two URI prefixes scoped per-agent.
INBOX_URI_PREFIX = "agent-mcp://inbox/"
STATUS_URI_PREFIX = "agent-mcp://status/"


@dataclass
class ResourceReader:
    """Per-resource payload: URI scheme + reader callable.

    `uri_prefix` is the URI the spec sees (``agent-mcp://inbox/`` etc.);
    `description` populates the `resources/list` response;
    `mime_type` advertises the body shape; `render` is the callable
    that turns an agent_id into the rendered body text.
    """

    uri_prefix: str
    description: str
    mime_type: str
    render: Callable[[str], str]


class ResourceRegistry(Registry[ResourceReader]):
    """Resource subsystem adapter for the shared Registry.

    Adds `read(uri, caller_token)` as the resources' verb. Auth +
    URI→agent_id resolution happen in the shared
    `resolve_agent_id_for_uri` helper; the registry routes to the
    correct reader based on URI prefix matching.
    """

    def find_by_uri(self, uri: str) -> Optional[RegistryEntry[ResourceReader]]:
        """Return the entry whose `uri_prefix` matches the given URI,
        or None for an unknown URI. Caller is expected to surface
        the None case as a JSON-RPC error.
        """
        for entry in self._entries.values():
            if uri.startswith(entry.meta.uri_prefix):
                return entry
        return None

    def read(
        self,
        uri: str,
        caller_token: Optional[str],
        *,
        principal: Optional["Principal"] = None,
    ) -> str:
        """Resolve agent_id from the URI + bearer, then invoke the
        matching reader.

        Raises ValueError on auth mismatch, unknown URI, or absent
        bearer — the MCP framework surfaces the message verbatim
        as a JSON-RPC error.
        """
        entry = self.find_by_uri(uri)
        if entry is None:
            raise ValueError(f"Unknown resource URI: {uri}")
        agent_id = resolve_agent_id_for_uri(
            uri, caller_token, principal=principal
        )
        return entry.meta.render(agent_id)


def resolve_agent_id_for_uri(
    uri: str,
    caller_token: Optional[str],
    *,
    principal: Optional["Principal"] = None,
) -> str:
    """Resolve which agent_id a `resources/read` URI is addressing,
    given the calling bearer.

    The URI carries the agent_id in its path — but the bearer always
    wins. If the URI's agent_id mismatches the bearer's agent_id, we
    raise ValueError so the caller cannot peek into another agent's
    inbox or status by guessing the URI. The framework converts the
    exception into a JSON-RPC error.

    Admin can read any agent's resource (operational visibility);
    workers may only read their own. arch-r3 #1+5 PR-B: "admin" is now
    the shared :func:`agent_mcp.core.principal_builder.catalog_role`
    decision (which folds in the legacy ``agent_id == "admin"`` label
    via :func:`is_operator_tier`), not a bare string test — the same
    admin determination every other MCP catalog surface uses. When the
    handler didn't thread a Principal (in-process / test callers with
    only a bearer), one is built from the token so the decision is
    identical.
    """
    from ..core.principal_builder import build_agent_bearer_principal, catalog_role

    if principal is None and caller_token:
        principal = build_agent_bearer_principal(caller_token)

    # The bearer's own agent_id scopes "read your own"; fall back to the
    # token resolver when the Principal carries no agent_id (it's the
    # same lookup get_agent_id would do).
    bearer_agent_id = principal.agent_id if principal is not None else None
    if not bearer_agent_id and caller_token:
        bearer_agent_id = get_agent_id(caller_token)
    if not bearer_agent_id:
        raise ValueError("Unauthorized: token does not resolve to an agent")

    # Match the URI against any registered resource's prefix to extract
    # the agent_id segment. Walking the registry keeps the helper open
    # to additional resource types without re-touching this function.
    uri_agent_id: Optional[str] = None
    for entry in resource_registry._entries.values():  # type: ignore[attr-defined]
        prefix = entry.meta.uri_prefix
        if uri.startswith(prefix):
            uri_agent_id = uri[len(prefix):].rstrip("/")
            break
    if uri_agent_id is None:
        raise ValueError(f"Unknown resource URI: {uri}")

    # Admin (per the shared catalog_role) can read any agent's resource
    # (operational visibility). Other callers may only read their own.
    if catalog_role(principal) == "admin":
        return uri_agent_id
    if uri_agent_id != bearer_agent_id:
        raise ValueError(
            "Unauthorized: callers may only read their own inbox / "
            "status resources"
        )
    return uri_agent_id


#: Singleton ResourceRegistry consumed by the MCP handlers in
#: `app/main_app.py`. Populated below at import time.
resource_registry: ResourceRegistry = ResourceRegistry()


def _register_default_resources() -> None:
    """Register the two built-in inbox+status resources.

    Both are visible only to authenticated callers (`any` here means
    "any registered role"; the handler in `app/main_app.py` already
    filters out anonymous via the bearer→agent_id resolver before
    `resources/list` even sees these — but classifying them as
    "any" keeps the Registry[T] semantics consistent with how tools
    work).
    """
    from .inbox import render_inbox
    from .status import render_status

    resource_registry.register(
        RegistryEntry(
            name="inbox",
            visibility="any",
            meta=ResourceReader(
                uri_prefix=INBOX_URI_PREFIX,
                description=(
                    "Event timeline for this agent — pending messages, "
                    "broadcasts, and task assignments / changes. JSON "
                    "envelope: {events: [...], next_cursor: \"<iso-ts>\"}."
                ),
                mime_type="application/json",
                render=render_inbox,
            ),
        )
    )
    resource_registry.register(
        RegistryEntry(
            name="status",
            visibility="any",
            meta=ResourceReader(
                uri_prefix=STATUS_URI_PREFIX,
                description=(
                    "Ambient counters for this agent: "
                    "{unread_messages, unfinished_tasks, ...}."
                ),
                mime_type="application/json",
                render=render_status,
            ),
        )
    )


_register_default_resources()
