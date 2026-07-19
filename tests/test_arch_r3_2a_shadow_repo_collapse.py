"""arch-deepening R3 #2a — shadow repository tree collapse.

The module-of-functions ``agent_mcp.core.repositories.agent_repo`` was
a shadow of the canonical class-based ``agent_mcp.repositories``
``AgentRepository`` singleton — same ``agents`` table, same
``state.active_agents`` / ``state.agent_working_dirs`` caches. #2a
deletes the shadow and repoints its callers at the TOP singleton.

Invariant: the two file-tool surfaces that resolve an agent's working
directory (``file_management_tools`` / ``file_metadata_tools``) and the
worker-prompt builder (``utils.project_utils.generate_system_prompt``)
all go through the SAME canonical ``AgentRepository`` and therefore
agree on the working directory. On origin/main these imported the core
module-of-functions; this test is RED there (the modules resolve to a
plain ``module`` object, not an ``AgentRepository`` instance) and GREEN
after the repoint.
"""
from __future__ import annotations

import inspect

from agent_mcp.app.main_app import create_app
from starlette.testclient import TestClient


def _make_client(project_dir):
    app = create_app(project_dir=str(project_dir))
    return TestClient(app)


def test_file_tools_and_project_utils_resolve_via_top_agent_repo(
    project_dir, reset_globals
):
    with _make_client(project_dir):
        from agent_mcp.repositories import get_agent_repo

        get_agent_repo().create(
            token="tok-wd",
            agent_id="wd-agent",
            status="active",
            working_directory="/tmp/wd-canonical",
            color="#abcdef",
        )

        from agent_mcp.tools import file_management_tools, file_metadata_tools

        # Both file-tool modules bind the canonical class-based repo, not
        # the deleted core module-of-functions (a plain ``module``).
        for mod in (file_management_tools, file_metadata_tools):
            repo = mod.agent_repo
            assert type(repo).__name__ == "AgentRepository", mod.__name__
            assert (
                type(repo).__module__
                == "agent_mcp.repositories.agent_repository"
            ), mod.__name__
            assert repo.get_working_directory("wd-agent") == "/tmp/wd-canonical"

        # project_utils resolves the working directory through the SAME
        # canonical repo. Its import is function-local (the prompt body is
        # owned by candidate #6a), so pin it at the source level plus a
        # behavioural check on the identical import path it performs.
        from agent_mcp.utils import project_utils

        src = inspect.getsource(project_utils.generate_system_prompt)
        assert "core.repositories" not in src
        assert "from ..repositories import agent_repo" in src

        from agent_mcp.repositories import agent_repo as pu_repo

        assert pu_repo.get_working_directory("wd-agent") == "/tmp/wd-canonical"


def test_core_repositories_shadow_modules_are_gone():
    """The four shadow modules must no longer be importable."""
    import importlib

    for name in ("agent_repo", "task_repo", "message_repo", "context_repo"):
        try:
            importlib.import_module(
                f"agent_mcp.core.repositories.{name}"
            )
        except ModuleNotFoundError:
            continue
        raise AssertionError(
            f"agent_mcp.core.repositories.{name} should have been deleted"
        )


def test_import_agent_mcp_has_no_circular_import():
    """Importing the package fresh must not hit the (now-removed) cycle."""
    import importlib

    mod = importlib.import_module("agent_mcp")
    assert mod is not None
