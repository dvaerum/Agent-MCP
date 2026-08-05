"""Unit tests for the delivery-transport fallback policy (ADR-0021).

The policy is a PURE decision function: given a worker's signals (unread
messages, open tasks, unassigned tasks, transport-status), the per-project
config, and per-worker bookkeeping, decide whether to ping NOW and how the
bookkeeping advances. No I/O, no clock — ``now`` is passed in — so every
branch (triggers, escalating backoff, cooldown, status gating, arm/clear)
is deterministically testable.
"""

from __future__ import annotations

from agent_mcp.features.delivery_policy import (
    DeliveryPolicyConfig,
    PingBookkeeping,
    WorkerSignals,
    evaluate,
)


def _cfg(**over) -> DeliveryPolicyConfig:
    base = dict(
        enabled=True,
        on_unread_messages=True,
        on_unfinished_tasks=True,
        on_unassigned_tasks=False,
        backoff_initial_seconds=30,
        backoff_max_seconds=3600,
        cooldown_seconds=60,
        wake_dormant=False,
    )
    base.update(over)
    return DeliveryPolicyConfig(**base)


def _sig(**over) -> WorkerSignals:
    base = dict(
        unread_messages=0,
        open_tasks=0,
        unassigned_tasks=0,
        transport_status="idle",
    )
    base.update(over)
    return WorkerSignals(**base)


FRESH = PingBookkeeping()


# ── master switch + triggers ────────────────────────────────────────


def test_disabled_never_pings():
    d = evaluate(_cfg(enabled=False), _sig(unread_messages=5), FRESH, now=100.0)
    assert d.should_ping is False


def test_no_condition_no_ping_and_disarmed():
    d = evaluate(_cfg(), _sig(), FRESH, now=100.0)
    assert d.should_ping is False
    assert d.bookkeeping.armed_since is None


def test_unread_arms_and_pings_immediately_when_idle():
    d = evaluate(_cfg(), _sig(unread_messages=3), FRESH, now=100.0)
    assert d.should_ping is True
    assert d.reason == "unread_messages"
    assert d.bookkeeping.ping_count == 1
    assert d.bookkeeping.last_ping_at == 100.0


def test_trigger_toggle_off_does_not_arm():
    d = evaluate(
        _cfg(on_unread_messages=False), _sig(unread_messages=3), FRESH, now=100.0
    )
    assert d.should_ping is False


def test_unfinished_tasks_trigger():
    d = evaluate(_cfg(), _sig(open_tasks=2), FRESH, now=100.0)
    assert d.should_ping is True
    assert d.reason == "unfinished_tasks"


def test_unassigned_off_by_default():
    d = evaluate(_cfg(), _sig(unassigned_tasks=4), FRESH, now=100.0)
    assert d.should_ping is False
    d2 = evaluate(
        _cfg(on_unassigned_tasks=True), _sig(unassigned_tasks=4), FRESH, now=100.0
    )
    assert d2.should_ping is True
    assert d2.reason == "unassigned_tasks"


# ── escalating backoff + cooldown ───────────────────────────────────


def test_within_backoff_does_not_re_ping():
    after1 = evaluate(_cfg(), _sig(unread_messages=1), FRESH, now=100.0).bookkeeping
    # 10s later — well under the 60s cooldown floor.
    d = evaluate(_cfg(), _sig(unread_messages=1), after1, now=110.0)
    assert d.should_ping is False


def test_re_pings_after_backoff_elapses():
    after1 = evaluate(_cfg(), _sig(unread_messages=1), FRESH, now=100.0).bookkeeping
    # 60s later — cooldown/first-backoff elapsed.
    d = evaluate(_cfg(), _sig(unread_messages=1), after1, now=160.0)
    assert d.should_ping is True
    assert d.bookkeeping.ping_count == 2


def test_backoff_escalates_and_caps():
    cfg = _cfg(backoff_initial_seconds=30, backoff_max_seconds=120, cooldown_seconds=0)
    bk = FRESH
    now = 0.0
    gaps = []
    prev_ping = None
    # Drive 6 pings, always eligible, record the gap the policy required.
    for _ in range(6):
        # advance until it pings
        step = 0.0
        while True:
            d = evaluate(cfg, _sig(unread_messages=1), bk, now=now + step)
            if d.should_ping:
                if prev_ping is not None:
                    gaps.append(round((now + step) - prev_ping, 3))
                prev_ping = now + step
                bk = d.bookkeeping
                now = now + step
                break
            step += 1.0
    # Escalating: 30, 60, 120, then capped at 120, 120…
    assert gaps == [30.0, 60.0, 120.0, 120.0, 120.0]


def test_cooldown_is_the_floor():
    cfg = _cfg(backoff_initial_seconds=5, cooldown_seconds=100)
    after1 = evaluate(cfg, _sig(unread_messages=1), FRESH, now=0.0).bookkeeping
    # 5s (initial backoff) elapsed but under the 100s cooldown → no ping.
    assert evaluate(cfg, _sig(unread_messages=1), after1, now=5.0).should_ping is False
    assert evaluate(cfg, _sig(unread_messages=1), after1, now=100.0).should_ping is True


def test_condition_clear_resets_backoff():
    after1 = evaluate(_cfg(), _sig(unread_messages=1), FRESH, now=100.0).bookkeeping
    # Agent reads the message → condition clears → disarm.
    cleared = evaluate(_cfg(), _sig(unread_messages=0), after1, now=110.0)
    assert cleared.should_ping is False
    assert cleared.bookkeeping.armed_since is None
    assert cleared.bookkeeping.ping_count == 0
    # A new message arms fresh → immediate ping again (backoff reset).
    d = evaluate(_cfg(), _sig(unread_messages=1), cleared.bookkeeping, now=120.0)
    assert d.should_ping is True
    assert d.bookkeeping.ping_count == 1


# ── transport-status gating ─────────────────────────────────────────


def test_working_suppresses_ping():
    d = evaluate(_cfg(), _sig(unread_messages=1, transport_status="working"), FRESH, now=100.0)
    assert d.should_ping is False
    # Still armed (so it fires once idle), just suppressed now.
    assert d.bookkeeping.armed_since == 100.0


def test_dead_never_pings():
    d = evaluate(_cfg(), _sig(unread_messages=1, transport_status="dead"), FRESH, now=100.0)
    assert d.should_ping is False


def test_dormant_gated_by_wake_flag():
    off = evaluate(_cfg(wake_dormant=False), _sig(unread_messages=1, transport_status="dormant"), FRESH, now=100.0)
    assert off.should_ping is False
    on = evaluate(_cfg(wake_dormant=True), _sig(unread_messages=1, transport_status="dormant"), FRESH, now=100.0)
    assert on.should_ping is True
