"""Regression guards: pyproject.toml is the single source of truth for the
product version.

Everything user-visible (the dashboard sidebar) or programmatic
(``agent_mcp.__version__``) must *derive* from the packaged version, never
carry a hand-maintained literal. Historically four copies drifted apart:
``__init__.__version__`` froze at "2.2.0", the dashboard sidebar hardcoded
"v3.4.0", the dashboard ``package.json`` sat at the Next.js scaffold default,
and git tags stalled a scheme behind ``pyproject.toml`` (5.0.71). These tests
fail the moment any of those re-hardcodes.
"""

import re
import tomllib
from importlib.metadata import version as pkg_version
from pathlib import Path

import agent_mcp

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
DASHBOARD = REPO_ROOT / "agent_mcp" / "dashboard"
SIDEBAR = DASHBOARD / "components" / "layout" / "app-sidebar.tsx"
NEXT_CONFIG = DASHBOARD / "next.config.ts"


def _pyproject_version() -> str:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_dunder_version_matches_installed_metadata() -> None:
    # __version__ must come from installed package metadata, not a
    # hand-maintained literal that drifts (it was frozen at "2.2.0").
    assert agent_mcp.__version__ == pkg_version("agent-mcp")


def test_dunder_version_matches_pyproject() -> None:
    assert agent_mcp.__version__ == _pyproject_version()


def test_sidebar_has_no_hardcoded_version_literal() -> None:
    src = SIDEBAR.read_text()
    # No `v1.2.3`-style literal baked into the component (it was "v3.4.0").
    hardcoded = re.search(r"v\d+\.\d+\.\d+", src)
    assert hardcoded is None, (
        f"hardcoded version literal {hardcoded.group()!r} found in "
        "app-sidebar.tsx; derive it from NEXT_PUBLIC_AGENT_MCP_VERSION instead"
    )
    # ...and it must actually read the derived env var.
    assert "NEXT_PUBLIC_AGENT_MCP_VERSION" in src


def test_next_config_wires_version_env() -> None:
    # next.config.ts is what makes the version reach the client bundle
    # (env-var first, pyproject fallback for plain `npm run dev`).
    assert "NEXT_PUBLIC_AGENT_MCP_VERSION" in NEXT_CONFIG.read_text()
