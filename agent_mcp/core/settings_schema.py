"""Settings-schema registry — the single source of truth for every
per-project ``config_*`` setting (ADR-0018).

Before this module the schema for each setting — its default, its group,
its human title/description, its tier — was declared in TWO places: the
frontend (``settings-dashboard.tsx``'s hardcoded ``POLICIES`` /
``AOE_FIELDS`` arrays) AND, partially, the backend
(``tools/access._TOGGLE_DEFAULTS`` — only 4 of 6 bool keys — plus
scattered ``default=`` literals per tool + ``aoe_notify``'s own
``DEFAULT_*`` constants). Two owners of the same fact drift; the UI's
"(using default: …)" hint could silently lie.

This registry makes the BACKEND the single owner. Every default reader
resolves through :func:`default_for`; the frontend consumes the schema
over ``GET /api/settings-schema``; the DEFAULT_* constants
``features/aoe_notify.py`` used to own now live here and are imported
back.

Tier-enforcement is HYBRID (ADR-0018): the proven ``_CONFIG_AOE_KEY_RE``
sysadmin gate in ``tools/project_settings_tools.py`` stays the ENFORCER
(safe-by-default: any future ``config_aoe_*`` key is sysadmin-gated
automatically). :attr:`SettingSpec.tier` drives the UI + the agreement
guarantee — a CI invariant test asserts ``schema.tier`` agrees with the
regex for every key — but it never relocates the live gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


SettingType = Literal["bool", "int", "string", "secret"]
SettingTier = Literal["operator", "sysadmin"]
SettingGroup = Literal[
    "worker_permissions", "event_loop", "retention", "aoe", "agent_profiles",
    "scheduling", "delivery",
]
SettingWidget = Literal[
    "switch",
    "int_days",
    "int_ms",
    "int_duration",
    "url",
    "secret",
    "secret_path",
    "template",
]


# ---------------------------------------------------------------------------
# AoE default constants — owned HERE (single source), imported back by
# ``features/aoe_notify.py``. Moving them out of that module is what makes
# the registry the single owner of the ``config_aoe_*`` defaults.
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "http://127.0.0.1:8181"
DEFAULT_TEMPLATE = (
    "[agent-mcp] New message from {sender}. "
    "Call get_agent_messages to read."
)
DEFAULT_TIMEOUT_MS = 2000


@dataclass(frozen=True)
class SettingSpec:
    """Schema for one per-project ``config_*`` setting.

    ``default`` MUST equal the value the backend resolved before the
    registry existed (no behaviour change). ``title`` / ``description``
    are the human copy lifted verbatim from the former frontend
    declarations so the backend now owns that copy. ``widget`` is an
    optional render hint the frontend maps to a control.
    """

    key: str
    type: SettingType
    default: object
    tier: SettingTier
    group: SettingGroup
    title: str
    description: str
    widget: Optional[SettingWidget] = None


SETTINGS_SCHEMA: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="config_allow_worker_to_worker",
        type="bool",
        default=True,
        tier="operator",
        group="worker_permissions",
        title="Allow worker-to-worker messaging",
        description=(
            "When on (default), workers and managers may use "
            "send_agent_message to message any agent. When off, direct "
            "agent-to-agent messaging is disabled for them entirely — the "
            "send_agent_message tool is hidden and they must use "
            "request_assistance to escalate to an admin."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_allow_worker_self_assign",
        type="bool",
        default=True,
        tier="operator",
        group="worker_permissions",
        title="Allow workers to self-assign tasks",
        description=(
            "When on (default), workers may call assign_task using their "
            "own agent_token. When off, only the admin may assign tasks."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_allow_worker_create_unassigned",
        type="bool",
        default=True,
        tier="operator",
        group="worker_permissions",
        title="Allow workers to file unassigned tasks",
        description=(
            "When on (default), workers may call assign_task with no "
            "agent_token to file work into the unassigned pool for any "
            "peer to claim. When off, only the admin may create tasks."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_allow_worker_update_own_status",
        type="bool",
        default=True,
        tier="operator",
        group="worker_permissions",
        title="Allow workers to update their own task status",
        description=(
            "When on (default), workers may call update_task_status on "
            "tasks they are assigned to. When off, only the admin may "
            "transition task status."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_auto_event_loop_global",
        type="bool",
        default=True,
        tier="operator",
        group="event_loop",
        title="Agent event-loop (wake on inbox / task events)",
        description=(
            "When on (default), worker agents are instructed to call "
            "wait_for_events on session start and after each event, so "
            "they wake automatically when messages or tasks arrive. When "
            "off, the wake-loop bootstrap text is omitted from "
            "serverInfo.instructions for every agent — workers fall back "
            "to human-prompted polling. Per-agent overrides live on the "
            "Agents tab (disabled here also disables every per-agent "
            "toggle)."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_event_idle_stop_seconds",
        type="int",
        default=604800,  # 7 days
        tier="operator",
        group="event_loop",
        title="Stop the event-loop after idle",
        description=(
            "How long an agent may sit in the wake-loop with NO real "
            "events (messages / task changes) before the server tells it "
            "to stop listening and go dormant. Measured across reconnects "
            "and reset by every real event (heartbeats and the "
            "profile-review greet do NOT count). When the window is "
            "exceeded, wait_for_events returns a stop_listening event and "
            "the agent exits its loop. Default 7 days; set to 0 to never "
            "stop (hold indefinitely). Re-waking a dormant agent is a "
            "manual/operator action."
        ),
        widget="int_duration",
    ),
    SettingSpec(
        key="config_debug_eventloop",
        type="bool",
        default=False,
        tier="operator",
        group="event_loop",
        title="Event-loop debug logging",
        description=(
            "When on, the backend logs a detailed trace of the "
            "wait_for_events wake loop (which hold strategy each client "
            "gets, whether a connection parks vs re-polls, heartbeats sent, "
            "the adaptive hold-ladder phase, and events in/out) at a level "
            "the systemd journal captures — grep for \"EVENTLOOP\". Off by "
            "default; when unset it falls back to the "
            "AGENT_MCP_EVENTLOOP_DEBUG environment variable (the deploy "
            "default). Diagnostic only — leave off in normal operation."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_idle_reminder_enabled",
        type="bool",
        default=True,
        tier="operator",
        group="event_loop",
        title="Idle backlog reminders",
        description=(
            "When on (default), an agent sitting idle in the event loop that "
            "still has unaddressed work — unread messages and/or OPEN tasks "
            "assigned to it (not completed/cancelled/failed) — is periodically "
            "reminded with a listed summary and told to go handle it. An agent "
            "with no backlog is never reminded (it stays parked for free)."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_idle_reminder_interval_seconds",
        type="int",
        default=3600,  # 1 hour
        tier="operator",
        group="event_loop",
        title="Idle reminder interval",
        description=(
            "How often to re-remind an idle agent that still has an "
            "unaddressed backlog. Default 1 hour. The reminder only fires "
            "when a backlog is actually present at the interval boundary."
        ),
        widget="int_duration",
    ),
    SettingSpec(
        key="config_message_retention_days",
        type="int",
        default=0,
        tier="operator",
        group="retention",
        title="Auto-delete read messages older than",
        description=(
            "The background pruner runs once every 24 hours and deletes "
            "rows from agent_messages where read=1 and timestamp is older "
            "than the configured window. Unread messages are never "
            "pruned. Set to 0 to disable (keep forever)."
        ),
        widget="int_days",
    ),
    SettingSpec(
        key="config_allow_worker_update_own_profile",
        type="bool",
        default=True,
        tier="operator",
        group="agent_profiles",
        title="Allow workers to edit their own profile",
        description=(
            "When on (default), a worker may call update_agent_profile to "
            "edit or confirm its own self-authored profile. When off, a "
            "worker can still review (confirm) but not change its profile. "
            "Editing a profile is routing-neutral — this toggle is a "
            "governance preference, not a safety gate."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_allow_manager_update_own_profile",
        type="bool",
        default=True,
        tier="operator",
        group="agent_profiles",
        title="Allow managers to edit their own profile",
        description=(
            "When on (default), a manager may call update_agent_profile to "
            "edit or confirm its own profile (the charter seeded at "
            "registration). When off, a manager can still review but not "
            "change its own profile."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_allow_manager_curate_profiles",
        type="bool",
        default=True,
        tier="operator",
        group="agent_profiles",
        title="Allow managers to curate worker profiles",
        description=(
            "When on (default), a manager may edit any worker's profile in "
            "the project (curation). Managers may never edit another "
            "manager's profile regardless of this toggle. When off, "
            "managers may only edit their own profile (subject to the "
            "manager self-edit toggle)."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_profile_review_interval_days",
        type="int",
        default=7,
        tier="operator",
        group="agent_profiles",
        title="Remind agents to review their profile every",
        description=(
            "How often an agent is nudged (on its event loop) to confirm or "
            "refresh its profile. The nudge fires when the profile has not "
            "been reviewed within this window, and always once on the first "
            "event-loop call of a new session. Set to 0 to disable the "
            "staleness nudge (the first-connect greet still fires once)."
        ),
        widget="int_days",
    ),
    SettingSpec(
        key="config_allow_worker_self_schedule",
        type="bool",
        default=True,
        tier="operator",
        group="scheduling",
        title="Allow agents to self-register scheduled directives",
        description=(
            "When on (default), an agent may call "
            "create_scheduled_directive to register its own recurring "
            "directives (imperative 'do X' commands that fire when the "
            "agent next checks in at-or-after the interval). When off, "
            "only a manager (for its workers) or an operator/admin may "
            "create schedules on an agent's behalf. Guardrails "
            "(min-interval floor + max active loops per agent) always "
            "apply."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_allow_manager_curate_schedules",
        type="bool",
        default=True,
        tier="operator",
        group="scheduling",
        title="Allow managers to curate worker schedules",
        description=(
            "When on (default), a manager may create/update/delete "
            "scheduled directives on any WORKER in the project. Managers "
            "may never curate another manager's schedules regardless of "
            "this toggle. When off, managers may only manage their own "
            "schedules (subject to the self-schedule toggle)."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_min_schedule_interval_seconds",
        type="int",
        default=60,
        tier="operator",
        group="scheduling",
        title="Minimum schedule interval",
        description=(
            "The floor (in seconds) on a scheduled directive's interval. "
            "create/update reject any interval below this value with a "
            "clear error. Since self-scheduling is on by default, this "
            "keeps an agent from registering a hot loop. Default 60s."
        ),
        widget="int_duration",
    ),
    SettingSpec(
        key="config_max_schedules_per_agent",
        type="int",
        default=10,
        tier="operator",
        group="scheduling",
        title="Maximum active schedules per agent",
        description=(
            "The cap on how many active (enabled) scheduled directives a "
            "single agent may hold at once. create/update reject a new "
            "schedule that would exceed this count. Completed/paused "
            "schedules do not count. Default 10."
        ),
        widget="int_duration",
    ),
    SettingSpec(
        key="config_aoe_notify_enabled",
        type="bool",
        default=False,
        tier="sysadmin",
        group="aoe",
        title="Notify Agents-of-Empires on new messages",
        description=(
            "When on, send_agent_message also POSTs a tmux-pane wake-up "
            "to a local Agents-of-Empires (AoE) instance so the recipient "
            "notices the message even between polls. Disabled by default. "
            "Configure config_aoe_base_url, config_aoe_bearer_token "
            "(secret), and config_aoe_notify_template in the AoE "
            "integration card below (sysadmin-only). The message body "
            "itself is never forwarded — only {sender}, {recipient}, "
            "{message_id} are interpolated."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_aoe_base_url",
        type="string",
        default=DEFAULT_BASE_URL,
        tier="sysadmin",
        group="aoe",
        title="Base URL",
        description="",
        widget="url",
    ),
    SettingSpec(
        key="config_aoe_bearer_token",
        type="secret",
        default=None,
        tier="sysadmin",
        group="aoe",
        title="Bearer token",
        description="",
        widget="secret",
    ),
    SettingSpec(
        key="config_aoe_bearer_token_file",
        type="secret",
        default=None,
        tier="sysadmin",
        group="aoe",
        title="Bearer token file",
        description="",
        widget="secret_path",
    ),
    SettingSpec(
        key="config_aoe_notify_template",
        type="string",
        default=DEFAULT_TEMPLATE,
        tier="sysadmin",
        group="aoe",
        title="Notify template",
        description="",
        widget="template",
    ),
    SettingSpec(
        key="config_aoe_timeout_ms",
        type="int",
        default=DEFAULT_TIMEOUT_MS,
        tier="sysadmin",
        group="aoe",
        title="Timeout (ms)",
        description="",
        widget="int_ms",
    ),
    # -- Delivery transport / fallback push (ADR-0021) -----------------
    # The tunable per-project policy for when agent-mcp pushes a skinny
    # notification down a worker's registered delivery transport (the
    # fallback for sessions that don't poll wait_for_events). All
    # operator-tier (none match the sysadmin _CONFIG_AOE_KEY_RE gate).
    SettingSpec(
        key="config_delivery_enabled",
        type="bool",
        default=False,
        tier="operator",
        group="delivery",
        title="Fallback delivery channel",
        description=(
            "When on, agent-mcp pushes skinny notifications (message/task "
            "id, title, status — never the body) to a worker's registered "
            "delivery transport when the agent falls behind (see the "
            "triggers below), so a session that isn't polling still gets "
            "poked. Off by default; a runtime (e.g. the AoE bridge) must "
            "also register the transport for a worker. Supersedes the "
            "legacy config_aoe_* notify push."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_delivery_on_unread_messages",
        type="bool",
        default=True,
        tier="operator",
        group="delivery",
        title="Deliver on unread messages",
        description=(
            "Arm the fallback while the agent has unread inbox messages."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_delivery_on_unfinished_tasks",
        type="bool",
        default=True,
        tier="operator",
        group="delivery",
        title="Deliver on unfinished tasks",
        description=(
            "Arm the fallback while the agent has OPEN tasks assigned to it "
            "(not completed/cancelled/failed)."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_delivery_on_unassigned_tasks",
        type="bool",
        default=False,
        tier="operator",
        group="delivery",
        title="Deliver on unassigned tasks",
        description=(
            "Arm the fallback while there are unassigned tasks in the pool "
            "the agent could claim. Off by default — this can be noisy."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_delivery_backoff_initial_seconds",
        type="int",
        default=30,
        tier="operator",
        group="delivery",
        title="Initial re-ping delay",
        description=(
            "First delay before re-pinging while a condition stays unmet. "
            "The delay widens on each re-ping (escalating backoff) up to "
            "the max, and resets the moment the condition clears."
        ),
        widget="int_duration",
    ),
    SettingSpec(
        key="config_delivery_backoff_max_seconds",
        type="int",
        default=3600,  # 1 hour
        tier="operator",
        group="delivery",
        title="Max re-ping delay",
        description=(
            "The ceiling the escalating re-ping delay backs off to. "
            "Default 1 hour."
        ),
        widget="int_duration",
    ),
    SettingSpec(
        key="config_delivery_cooldown_seconds",
        type="int",
        default=60,
        tier="operator",
        group="delivery",
        title="Post-ping cooldown",
        description=(
            "Minimum quiet window after a ping before another may fire for "
            "the same worker (also the window a just-active session is left "
            "alone)."
        ),
        widget="int_duration",
    ),
    SettingSpec(
        key="config_delivery_wake_dormant",
        type="bool",
        default=False,
        tier="operator",
        group="delivery",
        title="Wake dormant sessions",
        description=(
            "When on, a fallback ping may wake a dormant "
            "(stopped-but-revivable) session. When off (default), dormant "
            "sessions are left asleep — only idle sessions are pinged."
        ),
        widget="switch",
    ),
)


# ---------------------------------------------------------------------------
# Derived indexes + helpers
# ---------------------------------------------------------------------------

_SPEC_BY_KEY: dict[str, SettingSpec] = {s.key: s for s in SETTINGS_SCHEMA}

#: Every setting key the backend knows about.
KNOWN_SETTING_KEYS: frozenset[str] = frozenset(_SPEC_BY_KEY)

#: The genuinely-secret keys, derived from the schema (single source).
#: ``tools/project_settings_tools._SECRET_SETTING_KEYS`` binds to this.
SECRET_SETTING_KEYS: frozenset[str] = frozenset(
    s.key for s in SETTINGS_SCHEMA if s.type == "secret"
)


def spec_for(key: str) -> Optional[SettingSpec]:
    """Return the :class:`SettingSpec` for ``key`` (or None if unknown)."""
    return _SPEC_BY_KEY.get(key)


def default_for(key: str) -> object:
    """Return the registered default for ``key``.

    Raises :class:`KeyError` for an unknown key — a config reader that
    asks for a default the schema doesn't declare is a bug (the reader
    and the schema have drifted), and failing loudly beats silently
    resolving a wrong default.
    """
    return _SPEC_BY_KEY[key].default
