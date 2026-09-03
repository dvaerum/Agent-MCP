"""Differential-testing harness for the Python -> Rust migration.

Runs the SAME logical operation sequence through the real Python
repositories and through the compiled Rust ``conexus-db-differential``
binary (``rust/conexus-db/src/bin/conexus-db-differential.rs``), each
against its own SQLite file freshly initialized from the SAME real
schema (``agent_mcp.db.schema.init_database``, the ORM source of
truth), then diffs the resulting ``agents``/``project_context`` table
contents.

This is the differential-testing harness called out in the migration
plan (``/home/dennis/.claude/plans/prancy-napping-pie.md``, Phase B)
as the highest-leverage testability win: a pure offline comparison
against real schema, no server, no live traffic. Scope note: this
first version proves the MECHANISM against a freshly-initialized
(empty) schema, not yet against a captured copy of a real project's
populated database -- extending the fixtures to seed from a real
capture is a natural follow-up once more repositories are ported, not
a redesign of this harness.

Timestamp columns (created_at/updated_at/terminated_at/
profile_updated_at/profile_reviewed_at) are excluded from the diff:
Python's write paths stamp wall-clock ``datetime.now()`` internally,
while the Rust port takes an explicit ``now`` parameter (a deliberate
improvement -- see ``conexus-db/src/agent_repository.rs``'s module
docstring) -- so exact timestamp VALUES are expected to differ between
runs. This mirrors the plan's own stated e2e verification strategy:
"diff structurally with explicit allow-listed non-deterministic
fields (timestamps, request IDs)".

Skips gracefully (does not fail) if the Rust binary isn't available --
set CONEXUS_DB_DIFFERENTIAL_BIN to its path, or this test looks for it
at rust/target/{release,debug}/conexus-db-differential relative to the
repo root. CI builds it explicitly before running this file; a
Python-only contributor without a Rust toolchain sees this file skip,
not fail -- matching this repo's existing "gracefully degrade when an
optional native dependency is missing" pattern (see
is_vss_loadable()).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent_mcp.db.schema import init_database
from agent_mcp.db.unit_of_work import unit_of_work
from agent_mcp.repositories import project_context_repository
from agent_mcp.repositories.agent_repository import AgentRepository

REPO_ROOT = Path(__file__).resolve().parent.parent

# Excluded from comparison -- see module docstring.
_TIMESTAMP_FIELDS = {"created_at", "updated_at", "terminated_at", "profile_updated_at", "profile_reviewed_at"}


def _find_binary() -> Path | None:
    env_path = os.environ.get("CONEXUS_DB_DIFFERENTIAL_BIN")
    if env_path:
        candidate = Path(env_path)
        return candidate if candidate.is_file() else None
    for profile in ("release", "debug"):
        candidate = REPO_ROOT / "rust" / "target" / profile / "conexus-db-differential"
        if candidate.is_file():
            return candidate
    return None


@pytest.fixture
def differential_binary() -> Path:
    binary = _find_binary()
    if binary is None:
        pytest.skip(
            "conexus-db-differential binary not found; build it with "
            "`cargo build --release -p conexus-db --bin conexus-db-differential` "
            "in rust/, or set CONEXUS_DB_DIFFERENTIAL_BIN to its path"
        )
    return binary


def _init_project_schema(project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point MCP_PROJECT_DIR at `project_dir` and initialize the real
    (ORM-source-of-truth) schema there. Returns the resulting db path.
    """
    (project_dir / ".agent").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCP_PROJECT_DIR", str(project_dir))
    init_database()
    return project_dir / ".agent" / "mcp_state.db"


def _run_rust(binary: Path, db_path: Path, operations: list[dict]) -> dict:
    request = json.dumps({"db_path": str(db_path), "operations": operations})
    result = subprocess.run([str(binary)], input=request, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, f"conexus-db-differential failed: {result.stderr}"
    return json.loads(result.stdout)


def _dump_table(db_path: Path, table: str, order_by: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # table/order_by are always literals this test file passes in
        # (never external input) -- see call sites below.
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _normalize(row: dict) -> dict:
    result = {k: v for k, v in row.items() if k not in _TIMESTAMP_FIELDS}
    if "auto_event_loop" in result:
        # Raw sqlite3 gives back the stored INTEGER (0/1); the Rust
        # side serializes the typed `bool` field as JSON true/false.
        # Same fact, different wire representation -- normalize both.
        result["auto_event_loop"] = bool(result["auto_event_loop"])
    return result


def _normalize_all(rows: list[dict], sort_key: str) -> list[dict]:
    return [_normalize(r) for r in sorted(rows, key=lambda r: r[sort_key])]


def test_agent_lifecycle_matches_between_python_and_rust(tmp_path, monkeypatch, differential_binary):
    # -- Python side: real repository calls, real internal clock. --
    py_db = _init_project_schema(tmp_path / "py-project", monkeypatch)
    py_repo = AgentRepository()
    py_repo.create(
        token="t1",
        agent_id="alice",
        status="active",
        current_task=None,
        working_directory="/tmp",
        color=None,
        agent_role="worker",
    )
    py_repo.update_field("alice", "current_task", "task-1")
    py_repo.terminate("alice")

    # -- Rust side: same logical sequence via the differential binary. --
    rust_db = _init_project_schema(tmp_path / "rust-project", monkeypatch)
    operations = [
        {
            "op": "agent_create",
            "token": "t1",
            "agent_id": "alice",
            "created_at": "2026-01-01T00:00:00Z",
            "status": "active",
            "current_task": None,
            "working_directory": "/tmp",
            "color": None,
            "agent_role": "worker",
        },
        {
            "op": "agent_update_field",
            "agent_id": "alice",
            "field": "current_task",
            "value": {"kind": "optional_text", "value": "task-1"},
            "now": "2026-01-01T00:01:00Z",
        },
        {"op": "agent_terminate", "agent_id": "alice", "now": "2026-01-02T00:00:00Z"},
    ]
    rust_dump = _run_rust(differential_binary, rust_db, operations)

    py_rows = _dump_table(py_db, "agents", "agent_id")
    assert _normalize_all(py_rows, "agent_id") == _normalize_all(rust_dump["agents"], "agent_id")


def test_project_context_upsert_matches_between_python_and_rust(tmp_path, monkeypatch, differential_binary):
    # -- Python side --
    py_db = _init_project_schema(tmp_path / "py-project", monkeypatch)
    with unit_of_work() as u:
        project_context_repository.upsert(
            "greeting", "hello", "a friendly greeting", description_provided=True, actor="alice", connection=u.cursor
        )
    with unit_of_work() as u:
        # Value-only update (BL-R22-1 parity): description must survive.
        project_context_repository.upsert(
            "greeting", "hello v2", None, description_provided=False, actor="bob", connection=u.cursor
        )

    # -- Rust side --
    rust_db = _init_project_schema(tmp_path / "rust-project", monkeypatch)
    operations = [
        {
            "op": "context_upsert",
            "context_key": "greeting",
            "value": "hello",
            "description": "a friendly greeting",
            "description_provided": True,
            "actor": "alice",
            "now": "2026-01-01T00:00:00Z",
        },
        {
            "op": "context_upsert",
            "context_key": "greeting",
            "value": "hello v2",
            "description": None,
            "description_provided": False,
            "actor": "bob",
            "now": "2026-01-02T00:00:00Z",
        },
    ]
    rust_dump = _run_rust(differential_binary, rust_db, operations)

    py_rows = _dump_table(py_db, "project_context", "context_key")
    assert _normalize_all(py_rows, "context_key") == _normalize_all(rust_dump["project_context"], "context_key")

    # Assert the actual interesting fact both sides needed to agree on,
    # not just "the dicts matched": the description survived the
    # value-only update on BOTH sides.
    assert py_rows[0]["description"] == "a friendly greeting"
    assert rust_dump["project_context"][0]["description"] == "a friendly greeting"


def test_context_delete_many_matches_between_python_and_rust(tmp_path, monkeypatch, differential_binary):
    # -- Python side --
    py_db = _init_project_schema(tmp_path / "py-project", monkeypatch)
    with unit_of_work() as u:
        project_context_repository.upsert("a", "v", None, description_provided=False, actor="alice", connection=u.cursor)
    with unit_of_work() as u:
        project_context_repository.upsert("b", "v", None, description_provided=False, actor="alice", connection=u.cursor)
    with unit_of_work() as u:
        project_context_repository.delete_many(["a", "does-not-exist"], connection=u.cursor)

    # -- Rust side --
    rust_db = _init_project_schema(tmp_path / "rust-project", monkeypatch)
    operations = [
        {
            "op": "context_upsert",
            "context_key": "a",
            "value": "v",
            "description": None,
            "description_provided": False,
            "actor": "alice",
            "now": "2026-01-01T00:00:00Z",
        },
        {
            "op": "context_upsert",
            "context_key": "b",
            "value": "v",
            "description": None,
            "description_provided": False,
            "actor": "alice",
            "now": "2026-01-01T00:00:00Z",
        },
        {"op": "context_delete_many", "context_keys": ["a", "does-not-exist"]},
    ]
    rust_dump = _run_rust(differential_binary, rust_db, operations)

    py_rows = _dump_table(py_db, "project_context", "context_key")
    assert _normalize_all(py_rows, "context_key") == _normalize_all(rust_dump["project_context"], "context_key")
    # The interesting fact: "a" is gone, "b" survives, on BOTH sides.
    assert [r["context_key"] for r in py_rows] == ["b"]
