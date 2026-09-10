"""Regression guard: the daemon-agent unit depends on the router it will
actually talk to.

Background (live incident, 2026-09-08)
---------------------------------------

``agent-mcp-daemon-agent@<instance>.service``'s ``After``/``Wants`` used
to hardcode ``agent-mcp-router.service`` unconditionally, regardless of
``services.agent-mcp.router.impl`` (an A/B option that has since been
removed together with the rest of the Python implementation). Every
daemon-agent activation -- including one on a project already flipped
to ``router.impl = "rust"`` -- therefore ``Want``ed -- and so started --
the Python router alongside an already-running ``conexus-router``, both
competing for the same port. Confirmed live: the Python router
crash-looped (``address already in use``) after every subsequent
``home-manager switch``, requiring a manual ``systemctl --user stop
agent-mcp-router`` each time.

Retirement note
----------------

``router.impl`` and the Python router unit (``agent-mcp-router``) were
retired together with the rest of the Python implementation;
``conexus-router`` is now the ONLY router, so the daemon-agent
template's ``After``/``Wants`` are unconditionally
``conexus-router.service`` -- no more per-project ``router.impl``
branching to get wrong. This test keeps that unconditional dependency
pinned to the right unit name (a plain string typo here would silently
degrade to "waits on nothing that actually exists", which systemd
treats as immediately satisfied -- the daemon-agent would race the
router's own startup rather than fail loudly).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "nix" / "tests" / "eval-home-manager-module.nix"

_DRIVER = """
let
  repo = builtins.getFlake "@REPO@";
  base = repo.inputs.nixpkgs.legacyPackages.${builtins.currentSystem};
  harness = import @HARNESS@;
in (harness { pkgs = base; src = "@REPO@"; }).daemonAgentRouterDependency
"""


def _eval_daemon_agent_router_dependency() -> dict[str, list[str]]:
    if shutil.which("nix") is None:
        pytest.skip("nix is not available on PATH")

    proc = subprocess.run(
        [
            "nix",
            "eval",
            "--impure",
            "--json",
            "--expr",
            _DRIVER.replace("@REPO@", str(REPO_ROOT)).replace(
                "@HARNESS@", str(HARNESS)
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=1800,
        env={**os.environ, "NIX_CONFIG": "experimental-features = nix-command flakes"},
    )
    if proc.returncode != 0:
        pytest.fail(f"nix eval failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_daemon_agent_depends_on_conexus_router() -> None:
    """Every daemon-agent instance waits on and starts `conexus-router`.

    Unconditional now that `conexus-router` is the only router
    implementation -- see the module doc above for the live incident
    this pins a regression of.
    """
    dep = _eval_daemon_agent_router_dependency()
    assert dep["after"] == ["conexus-router.service"]
    assert dep["wants"] == ["conexus-router.service"]
