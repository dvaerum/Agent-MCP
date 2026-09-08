"""Regression guard: the daemon-agent unit depends on the ACTIVE router.

Background (live incident, 2026-09-08)
---------------------------------------

``agent-mcp-daemon-agent@<instance>.service``'s ``After``/``Wants`` used
to hardcode ``agent-mcp-router.service`` unconditionally, regardless of
``services.agent-mcp.router.impl``. Every daemon-agent activation
(including one on a project already flipped to ``router.impl = "rust"``)
therefore ``Want``ed -- and so started -- the Python router alongside an
already-running ``conexus-router``, both competing for the same port.
Confirmed live: the Python router crash-looped (``address already in
use``) after every subsequent ``home-manager switch``, requiring a
manual ``systemctl --user stop agent-mcp-router`` each time.

The fix mirrors ``conexus-router``'s own conditional ``Install.WantedBy``
(see that unit's inline comment in ``../nix/home-manager-module.nix``):
the daemon-agent template's ``After``/``Wants`` now resolve to whichever
router unit ``router.impl`` names as active, so a daemon-agent instance
only ever waits on and starts the router it will actually talk to.
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
in (harness { pkgs = base; src = "@REPO@"; routerImpl = "@IMPL@"; }).daemonAgentRouterDependency
"""


def _eval_daemon_agent_router_dependency(router_impl: str) -> dict[str, list[str]]:
    if shutil.which("nix") is None:
        pytest.skip("nix is not available on PATH")

    proc = subprocess.run(
        [
            "nix",
            "eval",
            "--impure",
            "--json",
            "--expr",
            _DRIVER.replace("@REPO@", str(REPO_ROOT))
            .replace("@HARNESS@", str(HARNESS))
            .replace("@IMPL@", router_impl),
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


def test_daemon_agent_depends_on_the_python_router_by_default() -> None:
    """`router.impl` defaults to "python" -- must reproduce today's
    behavior exactly (a daemon-agent instance depends on the Python
    router), the same "new option changes nothing by default" contract
    every other `router.impl`-aware unit in this module holds itself to.
    """
    dep = _eval_daemon_agent_router_dependency("python")
    assert dep["after"] == ["agent-mcp-router.service"]
    assert dep["wants"] == ["agent-mcp-router.service"]


def test_daemon_agent_depends_on_conexus_router_when_impl_is_rust() -> None:
    """The regression this test exists to pin: once a project has
    flipped `router.impl = "rust"`, its daemon-agent instances must
    depend on `conexus-router`, never the (by-then-inert) Python router.
    """
    dep = _eval_daemon_agent_router_dependency("rust")
    assert dep["after"] == ["conexus-router.service"]
    assert dep["wants"] == ["conexus-router.service"]
