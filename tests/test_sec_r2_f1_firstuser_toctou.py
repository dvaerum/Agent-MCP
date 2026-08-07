"""Pentest R2-F1 — atomic first-user creation (setup-wizard TOCTOU).

``identity.create_user`` decides the security-critical *"first user on
an empty table becomes sysadmin"* bootstrap by reading ``was_empty`` and
then INSERTing + promoting. On origin/main those steps ran under Python's
default autocommit: a bare ``SELECT`` is NOT enlisted with the later
INSERT's deferred transaction, so the check-then-insert is unguarded.

On the current single-process aiohttp server the event loop serialises
``create_user`` (it is fully synchronous), so it is not live-exploitable
today — but it ARMS into a real two-sysadmins race under a multi-worker
deployment (which ``rate_limit.py`` already anticipates) or a future
``asyncio.to_thread(create_user, …)`` refactor (a pattern the codebase
already uses for blocking DB work). There is also a reachable-now (low)
variant: two parties racing the operator's first-boot setup both pass the
top-level empty-check and each mint a first operator.

The fix mirrors the same-tree ``BEGIN IMMEDIATE`` idiom that
``admin_users_api.py`` already uses at every OTHER check-then-act site
(edit/delete user at :818/:867, add_group_member at :1176): wrap the
emptiness probe + INSERT + ``bootstrap_first_operator`` in ONE immediate
transaction on one connection, so a concurrent second ``create_user``
blocks then re-reads ``was_empty=False`` — neither crowned nor
bootstrapped.

These tests pin that contract:

* ``test_first_user_check_runs_inside_immediate_transaction`` —
  deterministic: the emptiness probe observes an OPEN transaction
  (``conn.in_transaction is True``), proving BEGIN IMMEDIATE was taken
  BEFORE the check. RED on origin/main (autocommit → probe sees no
  transaction).
* ``test_concurrent_distinct_first_users_yield_single_sysadmin`` — two
  threads racing ``create_user`` with DISTINCT usernames on an empty
  table → exactly ONE sysadmin (the loser is serialised behind the
  winner's commit and re-reads a non-empty table). RED on origin/main
  (both read empty → two sysadmins).
* ``test_concurrent_same_username_second_is_rejected`` — the manual-
  transaction rollback path stays correct: a colliding second creator is
  rejected with ``UsernameAlreadyExistsError`` and leaves one user / one
  sysadmin.
* ``test_multiple_sysadmins_allowed_after_bootstrap`` — the transaction
  guards only the BOOTSTRAP decision; promoting a later user to sysadmin
  (the proxy-header ``is_sysadmin`` path) is still allowed.
* ``test_sso_passwordless_first_user_check_is_transactional`` — the SSO
  passwordless creator routes through ``create_user`` and inherits the
  same atomic first-user check (``bootstrap_sysadmin=False`` opt-out).
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

# ── Fixtures (self-contained; mirror tests/test_router_identity.py) ──


@pytest.fixture
def router_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Tmp router.db path, env-injected, fresh module import."""
    db_path = tmp_path / "router.db"
    monkeypatch.setenv("AGENT_MCP_ROUTER_DB", str(db_path))
    projects_file = tmp_path / "projects.local.json"
    monkeypatch.setenv("AGENT_MCP_PROJECTS_FILE", str(projects_file))
    for mod in [
        "agent_mcp.router.identity",
        "agent_mcp.router.migrations_runner",
        "agent_mcp.router.project_registry",
    ]:
        sys.modules.pop(mod, None)
    return db_path


@pytest.fixture
def identity(router_db: Path):
    """Freshly imported, migrated ``agent_mcp.router.identity``."""
    import agent_mcp.router.identity as identity

    importlib.reload(identity)
    identity.init_router_db()
    return identity


def _count(db_path: Path, sql: str) -> int:
    """Read a scalar count from an INDEPENDENT connection (the DB is the
    authoritative source of truth — never the module's cached view)."""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


# ── 1. Deterministic: the probe runs inside BEGIN IMMEDIATE ──────────


def test_first_user_check_runs_inside_immediate_transaction(
    identity, router_db: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The emptiness probe must observe an OPEN write transaction.

    RED on origin/main: ``create_user`` runs the probe under autocommit,
    so ``conn.in_transaction`` is False at check time (the bare SELECT
    does not begin a transaction). GREEN after the fix: the probe runs
    after ``BEGIN IMMEDIATE``, so ``conn.in_transaction`` is True — the
    check and the INSERT provably share one write-locked transaction.
    """
    captured: dict[str, object] = {}
    real_empty = identity.users_table_is_empty

    def spy(conn=None):
        captured["in_transaction"] = (
            conn.in_transaction if conn is not None else None
        )
        return real_empty(conn=conn)

    monkeypatch.setattr(identity, "users_table_is_empty", spy)

    identity.create_user(username="alice", password="password123")

    assert captured["in_transaction"] is True, (
        "first-user emptiness probe must run inside an open BEGIN "
        "IMMEDIATE transaction so the check + INSERT + bootstrap are "
        "atomic; saw autocommit (no transaction)."
    )
    assert _count(router_db, "SELECT COUNT(*) FROM users WHERE is_sysadmin=1") == 1


# ── 2. Concurrency: distinct first users → exactly one sysadmin ──────


def test_concurrent_distinct_first_users_yield_single_sysadmin(
    identity, router_db: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Two threads racing ``create_user`` (distinct usernames) on an
    empty table must produce EXACTLY ONE sysadmin.

    We add a busy_timeout so the loser of ``BEGIN IMMEDIATE`` BLOCKS on
    the write-lock (rather than erroring immediately under the default
    zero timeout) and observably re-reads a non-empty table after the
    winner commits. Thread A pauses AFTER its probe (holding the write-
    lock under the fix); thread B is launched into that window.

    RED on origin/main: A's autocommit probe holds no lock, so B's probe
    also reads empty → both bootstrap → two sysadmins. GREEN after the
    fix: B blocks at BEGIN IMMEDIATE until A commits, then re-reads
    ``was_empty=False`` → only A is crowned.
    """
    real_open = identity.open_connection

    def open_with_timeout():
        conn = real_open()
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    monkeypatch.setattr(identity, "open_connection", open_with_timeout)

    a_probed = threading.Event()
    real_empty = identity.users_table_is_empty

    def spy(conn=None):
        result = real_empty(conn=conn)
        if threading.current_thread().name == "creator-A":
            # A has taken the write-lock (under the fix) and read the
            # table; hold here so B is forced to contend for the lock.
            a_probed.set()
            time.sleep(0.5)
        return result

    monkeypatch.setattr(identity, "users_table_is_empty", spy)

    results: dict[str, object] = {}

    def make(name: str, username: str):
        def run():
            try:
                results[name] = identity.create_user(
                    username=username, password="password123",
                )
            except Exception as exc:  # noqa: BLE001 - record for assert
                results[name] = exc

        return run

    ta = threading.Thread(target=make("A", "alice"), name="creator-A")
    tb = threading.Thread(target=make("B", "bob"), name="creator-B")

    ta.start()
    assert a_probed.wait(timeout=3), "thread A never reached its probe"
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)
    assert not ta.is_alive() and not tb.is_alive(), "creator thread hung"

    n_sysadmin = _count(
        router_db, "SELECT COUNT(*) FROM users WHERE is_sysadmin=1",
    )
    n_users = _count(router_db, "SELECT COUNT(*) FROM users")

    assert n_sysadmin == 1, (
        f"exactly one first-operator sysadmin expected; got {n_sysadmin} "
        "(dual-sysadmin race)"
    )
    # Both distinct users legitimately exist; only the bootstrap decision
    # is guarded. The second is a valid NON-sysadmin operator.
    assert n_users == 2


# ── 3. Concurrency: same username → second rejected, rollback clean ──


def test_concurrent_same_username_second_is_rejected(
    identity, router_db: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A colliding second creator is rejected via ``UsernameAlreadyExists``
    and the manual BEGIN IMMEDIATE / ROLLBACK path leaves the DB clean —
    one user, one sysadmin, no dangling transaction/lock."""
    real_open = identity.open_connection

    def open_with_timeout():
        conn = real_open()
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    monkeypatch.setattr(identity, "open_connection", open_with_timeout)

    a_probed = threading.Event()
    real_empty = identity.users_table_is_empty

    def spy(conn=None):
        result = real_empty(conn=conn)
        if threading.current_thread().name == "creator-A":
            a_probed.set()
            time.sleep(0.5)
        return result

    monkeypatch.setattr(identity, "users_table_is_empty", spy)

    results: dict[str, object] = {}

    def make(name: str):
        def run():
            try:
                results[name] = identity.create_user(
                    username="root", password="password123",
                )
            except Exception as exc:  # noqa: BLE001
                results[name] = exc

        return run

    ta = threading.Thread(target=make("A"), name="creator-A")
    tb = threading.Thread(target=make("B"), name="creator-B")
    ta.start()
    assert a_probed.wait(timeout=3)
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)
    assert not ta.is_alive() and not tb.is_alive()

    # Exactly one of the two raised UsernameAlreadyExistsError; the other
    # returned a user_id.
    errors = [
        v for v in results.values()
        if isinstance(v, identity.UsernameAlreadyExistsError)
    ]
    ok = [v for v in results.values() if isinstance(v, str)]
    assert len(errors) == 1 and len(ok) == 1

    assert _count(router_db, "SELECT COUNT(*) FROM users") == 1
    assert _count(router_db, "SELECT COUNT(*) FROM users WHERE is_sysadmin=1") == 1

    # The connection lock was released (no dangling IMMEDIATE txn): a
    # fresh write succeeds immediately.
    conn = sqlite3.connect(str(router_db))
    try:
        conn.execute("PRAGMA busy_timeout=1000")
        conn.execute(
            "INSERT INTO users (user_id, username, created_at, is_sysadmin) "
            "VALUES ('deadbeefdeadbeef', 'later', '2026-01-01T00:00:00.000+00:00', 0)"
        )
        conn.commit()
    finally:
        conn.close()


# ── 4. Post-bootstrap multi-sysadmin remains legitimate ─────────────


def test_multiple_sysadmins_allowed_after_bootstrap(
    identity, router_db: Path,
):
    """The transaction guards ONLY the bootstrap decision — it does not
    forbid legitimate later multi-sysadmin (an operator promoting a user,
    or the proxy-header ``is_sysadmin`` path)."""
    identity.create_user(username="alice", password="password123")
    # Table is now non-empty → no bootstrap; ``is_sysadmin=True`` forces
    # the bit on regardless (proxy-header default_is_sysadmin path).
    identity.create_user(
        username="bob", password="password123", is_sysadmin=True,
    )

    assert _count(router_db, "SELECT COUNT(*) FROM users") == 2
    assert _count(router_db, "SELECT COUNT(*) FROM users WHERE is_sysadmin=1") == 2


# ── 5. SSO passwordless first-user inherits the atomic check ─────────


def test_sso_passwordless_first_user_check_is_transactional(
    identity, router_db: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The SSO passwordless creator (``sso.find_or_create_sso_user`` →
    ``create_user`` with ``password_hash=None``) routes through the same
    guarded path, so its first-user check is also enlisted in BEGIN
    IMMEDIATE. Exercised directly via ``create_user`` (the seam SSO uses)
    with the SSO opt-out (``bootstrap_sysadmin=False``)."""
    captured: dict[str, object] = {}
    real_empty = identity.users_table_is_empty

    def spy(conn=None):
        captured["in_transaction"] = (
            conn.in_transaction if conn is not None else None
        )
        return real_empty(conn=conn)

    monkeypatch.setattr(identity, "users_table_is_empty", spy)

    identity.create_user(
        username="ssouser",
        password=None,
        password_hash=None,
        sso_subject="oidc:sub-123",
        is_sysadmin=False,
        bootstrap_sysadmin=False,
    )

    assert captured["in_transaction"] is True
    # SSO opt-out: first user is created WITHOUT sysadmin.
    assert _count(router_db, "SELECT COUNT(*) FROM users WHERE is_sysadmin=1") == 0
    assert _count(router_db, "SELECT COUNT(*) FROM users") == 1
