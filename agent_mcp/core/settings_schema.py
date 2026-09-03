"""Settings-schema registry — the single source of truth for every
per-project ``config_*`` setting (ADR-0018).

Before this module the schema for each setting — its default, its group,
its human title/description, its tier — was declared in TWO places: the
frontend (``settings-dashboard.tsx``'s hardcoded policy arrays) AND,
partially, the backend (``tools/access._TOGGLE_DEFAULTS`` — only 4 of 6
bool keys — plus scattered ``default=`` literals per tool). Two owners
of the same fact drift; the UI's "(using default: …)" hint could
silently lie.

This registry makes the BACKEND the single owner. Every default reader
resolves through :func:`default_for`; the frontend consumes the schema
over ``GET /api/settings-schema``.

Every registered setting is operator-tier: the ``config_*`` namespace
is writable by any confirmed operator (the ``system.config.write`` cap
gate in ``tools/project_settings_tools.py`` is the enforcer).
:attr:`SettingSpec.tier` drives the UI. (The former sysadmin-only
AoE integration keys were retired with the AoE notify feature — see
the AoE-removal ADR.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


SettingType = Literal["bool", "int", "string", "secret"]
SettingTier = Literal["operator", "sysadmin"]
SettingGroup = Literal[
    "worker_permissions", "event_loop", "retention", "agent_profiles",
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
        key="config_allow_worker_view_foreign_tasks",
        type="bool",
        default=True,
        tier="operator",
        group="worker_permissions",
        title="Allow workers to view tasks assigned to other agents",
        description=(
            "When on (default), a worker's view_tasks / search_tasks / "
            "ask_project_rag calls are no longer scoped to just their "
            "own tasks plus the unassigned pool — they can also see and "
            "search tasks assigned to a DIFFERENT agent. When off, "
            "cross-agent task visibility is denied (a foreign-owned "
            "task_id resolves to the same phantom 'not found' a "
            "nonexistent task_id does, so a worker cannot enumerate "
            "foreign tasks). Does not affect who may edit or reassign a "
            "task — only who can see it."
        ),
        widget="switch",
    ),
    SettingSpec(
        key="config_allow_worker_comment_foreign_tasks",
        type="bool",
        default=True,
        tier="operator",
        group="worker_permissions",
        title="Allow workers to comment on tasks assigned to other agents",
        description=(
            "When on (default, implies view), a worker may call "
            "add_task_comment on a task assigned to a DIFFERENT agent — "
            "the comment is authored and timestamped as normal, and "
            "editing/deleting someone else's comment stays author-only "
            "regardless of this setting. When off, add_task_comment on "
            "a foreign-owned task is denied (same phantom 'not found' "
            "as an unassigned/nonexistent task). Never affects task "
            "status, reassignment, subtask creation, or bulk "
            "operations — those stay owner-only."
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
    # -- Delivery transport / fallback push (ADR-0021) -----------------
    # The tunable per-project policy for when agent-mcp pushes a skinny
    # notification down a worker's registered delivery transport (the
    # fallback for sessions that don't poll wait_for_events). All
    # operator-tier.
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
            "also register the transport for a worker."
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
        key="config_delivery_on_due_directives",
        type="bool",
        default=True,
        tier="operator",
        group="delivery",
        title="Deliver on due scheduled directives",
        description=(
            "Arm the fallback while the agent has a scheduled directive "
            "that is due now. On by default — a due directive is an "
            "explicit, deliberate obligation, not an ambient signal."
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
