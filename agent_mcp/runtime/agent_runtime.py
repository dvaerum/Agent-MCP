# Agent-MCP/agent_mcp/runtime/agent_runtime.py
"""AgentRuntime — the named home of the "boot, prompt, discover, tear-down
an agent" concept.

Round 1 (PRs #146–#155) promoted "module of functions" repositories
into class-based ``TaskRepository`` / ``AgentRepository`` /
``MessageRepository``. PR #156 (round 2 PR A) did the same for the
atomic-write seam by promoting it to ``atomic_with_audit``. This PR
(round 2 PR B) does the same for what was hiding inside
``utils/tmux_utils.py`` (564 lines) and ``utils/worktree_utils.py``
(576 lines).

These two utility files were not really "utilities" — together they
own the agent runtime: how an agent process is launched (tmux), how
its working tree is set up (git worktree), how it is sent a prompt,
how active agents are discovered after a restart, and how an agent's
session/worktree is cleaned up. Promoting them to a named module with
a small, intention-revealing interface makes the concept visible and
gives future PRs a place to attach lifecycle (e.g. async cleanup,
process supervision, audit hooks).

What this module owns:

* The **low-level subprocess primitives** that talk to ``tmux`` and
  ``git`` (free functions — kept module-level so subprocess calls can
  be monkeypatched in tests via
  ``agent_mcp.runtime.agent_runtime.subprocess``).
* The :class:`AgentRuntime` class — the small intention-revealing
  interface (``send_prompt`` / ``discover_active`` / ``is_alive`` /
  ``cleanup`` / ``create_worktree``) that callers should reach for.
* The naming policy (``generate_agent_session_name`` /
  ``parse_agent_session_name`` / ``get_token_suffix``) — kept
  module-level because both the class methods and the legacy shim
  re-exports need them. ``get_admin_token_suffix`` is a back-compat
  alias for ``get_token_suffix`` (the parameter has been a per-agent
  token, not an admin token, since Wave 3 of prancy-napping-pie).

What stays in :mod:`agent_mcp.utils`:

* ``utils/tmux_utils.py`` and ``utils/worktree_utils.py`` shrink to
  ~40-line re-export shims (the canonical pattern set by PR #153's
  ``db/actions/task_db.py``). Existing call sites keep working
  unchanged; new call sites should import from
  :mod:`agent_mcp.runtime`.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from ..core.config import logger as _config_logger  # type: ignore
except Exception:  # pragma: no cover — fallback if core.config not importable
    _config_logger = logging.getLogger(__name__)

logger = _config_logger


# ---------------------------------------------------------------------------
# Tmux subprocess primitives (was utils/tmux_utils.py)
# ---------------------------------------------------------------------------


def is_tmux_available() -> bool:
    """Check if tmux is installed and available."""
    try:
        result = subprocess.run(
            ["tmux", "-V"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
        return False


def sanitize_session_name(name: str) -> str:
    """Sanitize session name to be safe for tmux.

    Tmux session names cannot contain: ``.``, ``:``, ``[``, ``]``,
    space, ``$`` and other special chars.
    """
    sanitized = re.sub(r'[.:\[\]\s$\'"`\\]', "_", name)
    sanitized = re.sub(r"_+", "_", sanitized)
    sanitized = sanitized.strip("_")
    if sanitized and not sanitized[0].isalnum():
        sanitized = "agent_" + sanitized
    return sanitized or "agent_session"


def agent_setup_delay() -> float:
    """Seconds to wait between tmux setup commands during agent launch.

    Production defaults to 1.0s so each `send-keys` settles before the
    next lands. Overridable via ``AGENT_MCP_AGENT_SETUP_DELAY`` — the
    test suite sets it to 0 so create_agent / task-assignment tests
    (which spin up real tmux sessions) don't pay ~6 × 1s of blocking
    sleeps per call. Single source of truth for both admin_tools and
    task_tools.
    """
    return float(os.environ.get("AGENT_MCP_AGENT_SETUP_DELAY", "1.0"))


def create_tmux_session(
    session_name: str,
    working_dir: str,
    command: Optional[str] = None,
    env_vars: Optional[Dict[str, str]] = None,
) -> bool:
    """Create a new tmux session with the given name and working directory."""
    if not is_tmux_available():
        logger.error("tmux is not available on this system")
        return False

    clean_session_name = sanitize_session_name(session_name)

    if session_exists(clean_session_name):
        logger.warning(f"tmux session '{clean_session_name}' already exists")
        return False

    try:
        Path(working_dir).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"Failed to create working directory {working_dir}: {e}")
        return False

    try:
        tmux_cmd = [
            "tmux",
            "new-session",
            "-d",
            "-s",
            clean_session_name,
            "-c",
            working_dir,
        ]

        env = None
        if env_vars:
            env = os.environ.copy()
            env.update(env_vars)

        if command:
            tmux_cmd.append(command)

        result = subprocess.run(
            tmux_cmd,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )

        if result.returncode == 0:
            logger.info(f"Created tmux session '{clean_session_name}' in {working_dir}")
            return True
        logger.error(f"Failed to create tmux session: {result.stderr}")
        return False

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout creating tmux session '{clean_session_name}'")
        return False
    except Exception as e:
        logger.error(f"Error creating tmux session '{clean_session_name}': {e}")
        return False


def session_exists(session_name: str) -> bool:
    """Check if a tmux session with the given name exists."""
    if not is_tmux_available():
        return False

    clean_session_name = sanitize_session_name(session_name)

    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", clean_session_name],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False


def list_tmux_sessions() -> List[Dict[str, Any]]:
    """List all tmux sessions with detailed information."""
    if not is_tmux_available():
        return []

    try:
        result = subprocess.run(
            [
                "tmux",
                "list-sessions",
                "-F",
                "#{session_name}|#{session_created}|#{session_attached}|#{session_windows}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            if "no server running" in result.stderr:
                return []
            logger.warning(f"Failed to list tmux sessions: {result.stderr}")
            return []

        sessions = []
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split("|")
                if len(parts) >= 4:
                    sessions.append(
                        {
                            "name": parts[0],
                            "created": parts[1],
                            "attached": parts[2] == "1",
                            "windows": int(parts[3]),
                        }
                    )

        return sessions

    except subprocess.TimeoutExpired:
        logger.error("Timeout listing tmux sessions")
        return []
    except Exception as e:
        logger.error(f"Error listing tmux sessions: {e}")
        return []


def kill_tmux_session(session_name: str) -> bool:
    """Kill a tmux session by name."""
    if not is_tmux_available():
        logger.error("tmux is not available on this system")
        return False

    clean_session_name = sanitize_session_name(session_name)

    if not session_exists(clean_session_name):
        logger.warning(f"tmux session '{clean_session_name}' does not exist")
        return True  # Idempotent — already gone counts as success

    try:
        result = subprocess.run(
            ["tmux", "kill-session", "-t", clean_session_name],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            logger.info(f"Killed tmux session '{clean_session_name}'")
            return True
        logger.error(
            f"Failed to kill tmux session '{clean_session_name}': {result.stderr}"
        )
        return False

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout killing tmux session '{clean_session_name}'")
        return False
    except Exception as e:
        logger.error(f"Error killing tmux session '{clean_session_name}': {e}")
        return False


def get_session_status(session_name: str) -> Optional[Dict[str, Any]]:
    """Get detailed status information for a specific tmux session."""
    if not is_tmux_available():
        return None

    clean_session_name = sanitize_session_name(session_name)

    if not session_exists(clean_session_name):
        return None

    try:
        result = subprocess.run(
            [
                "tmux",
                "display-message",
                "-t",
                clean_session_name,
                "-p",
                "#{session_name}|#{session_created}|#{session_attached}|#{session_windows}|#{session_id}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode == 0:
            parts = result.stdout.strip().split("|")
            if len(parts) >= 5:
                return {
                    "name": parts[0],
                    "created": parts[1],
                    "attached": parts[2] == "1",
                    "windows": int(parts[3]),
                    "session_id": parts[4],
                    "exists": True,
                }

        return None

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout getting status for tmux session '{clean_session_name}'")
        return None
    except Exception as e:
        logger.error(f"Error getting status for tmux session '{clean_session_name}': {e}")
        return None


def send_command_to_session(session_name: str, command: str) -> bool:
    """Send a command to a tmux session."""
    if not is_tmux_available():
        return False

    clean_session_name = sanitize_session_name(session_name)

    if not session_exists(clean_session_name):
        logger.warning(f"tmux session '{clean_session_name}' does not exist")
        return False

    try:
        result = subprocess.run(
            ["tmux", "send-keys", "-t", clean_session_name, command, "Enter"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout sending command to tmux session '{clean_session_name}'")
        return False
    except Exception as e:
        logger.error(f"Error sending command to tmux session '{clean_session_name}': {e}")
        return False


def send_prompt_to_session(
    session_name: str, prompt: str, delay_seconds: int = 3
) -> bool:
    """Send a prompt to a tmux session after a delay.

    Uses proper tmux command separation: first type the text, then send Enter.
    """
    if not is_tmux_available():
        return False

    clean_session_name = sanitize_session_name(session_name)

    if not session_exists(clean_session_name):
        logger.warning(f"tmux session '{clean_session_name}' does not exist")
        return False

    try:
        logger.info(
            f"Waiting {delay_seconds} seconds for Claude to start up in session '{clean_session_name}'"
        )
        time.sleep(delay_seconds)

        logger.debug(f"Typing prompt to session '{clean_session_name}'")
        result = subprocess.run(
            ["tmux", "send-keys", "-t", clean_session_name, prompt],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            logger.error(f"Failed to type prompt to session: {result.stderr}")
            return False

        time.sleep(0.5)

        logger.debug(f"Sending Enter to session '{clean_session_name}'")
        result = subprocess.run(
            ["tmux", "send-keys", "-t", clean_session_name, "Enter"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode == 0:
            logger.info(
                f"Successfully sent prompt to tmux session '{clean_session_name}'"
            )
            return True
        logger.error(f"Failed to send Enter to session: {result.stderr}")
        return False

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout sending prompt to tmux session '{clean_session_name}'")
        return False
    except Exception as e:
        logger.error(f"Error sending prompt to tmux session '{clean_session_name}': {e}")
        return False


def send_prompt_async(session_name: str, prompt: str, delay_seconds: int = 3) -> None:
    """Send a prompt to a tmux session asynchronously in a background thread."""

    def _send_prompt() -> None:
        send_prompt_to_session(session_name, prompt, delay_seconds)

    thread = threading.Thread(target=_send_prompt, daemon=True)
    thread.start()


def cleanup_agent_sessions(active_agent_ids: List[str]) -> int:
    """Clean up tmux sessions that don't correspond to active agents."""
    if not is_tmux_available():
        return 0

    sessions = list_tmux_sessions()
    cleaned_count = 0

    for session in sessions:
        session_name = session["name"]

        if session_name.startswith("agent_") or any(
            session_name == sanitize_session_name(agent_id)
            for agent_id in active_agent_ids
        ):
            potential_agent_id = session_name.replace("agent_", "")
            clean_agent_ids = [sanitize_session_name(aid) for aid in active_agent_ids]

            if (
                session_name not in clean_agent_ids
                and potential_agent_id not in active_agent_ids
            ):
                logger.info(f"Cleaning up orphaned agent session: {session_name}")
                if kill_tmux_session(session_name):
                    cleaned_count += 1

    return cleaned_count


# ---------------------------------------------------------------------------
# Naming policy (session-name <-> agent_id round-trip)
# ---------------------------------------------------------------------------


def get_token_suffix(token: str) -> str:
    """Get the last 4 characters of an agent token for session naming.

    retire-system-token Wave 5 renamed the parameter from
    ``admin_token`` — the suffix is always derived from the agent's
    own per-agent token now (see ``admin_tools.create_agent_session_name``
    callsite). The old name predates Wave 3 of the prancy-napping-pie
    retirement, which switched the session-name suffix to the new
    agent's own token. ``get_admin_token_suffix`` is kept as a back-
    compat alias for the ``utils.tmux_utils`` shim.
    """
    if not token or len(token) < 4:
        return "0000"
    return token[-4:].lower()


# Back-compat alias kept so the ``utils.tmux_utils`` re-export shim
# (and any external callers that imported the old name) keep working.
get_admin_token_suffix = get_token_suffix


def generate_agent_session_name(agent_id: str, token: str) -> str:
    """Generate a smart tmux session name in the format ``<agent_id>-<suffix>``."""
    suffix = get_token_suffix(token)
    clean_agent_id = sanitize_session_name(agent_id)
    return f"{clean_agent_id}-{suffix}"


def parse_agent_session_name(session_name: str, token: str) -> Optional[str]:
    """Parse an agent session name to extract the agent ID."""
    suffix = get_token_suffix(token)

    if not session_name.endswith(f"-{suffix}"):
        return None

    agent_id = session_name[: -len(f"-{suffix}")]

    if not agent_id:
        return None

    return agent_id


def discover_active_agents_from_tmux(token: str) -> List[Dict[str, Any]]:
    """Discover active agents by scanning tmux sessions for our naming pattern."""
    discovered_agents: List[Dict[str, Any]] = []

    try:
        sessions = list_tmux_sessions()
        suffix = get_token_suffix(token)

        for session in sessions:
            session_name = session["name"]

            agent_id = parse_agent_session_name(session_name, token)

            if agent_id:
                discovered_agents.append(
                    {
                        "agent_id": agent_id,
                        "session_name": session_name,
                        "session_created": session.get("created"),
                        "session_attached": session.get("attached", False),
                        "session_windows": session.get("windows", 1),
                        "discovered_from_tmux": True,
                    }
                )
                logger.info(
                    f"Discovered agent '{agent_id}' in tmux session '{session_name}'"
                )

        logger.info(
            f"Discovered {len(discovered_agents)} agents from tmux sessions with suffix '{suffix}'"
        )

    except Exception as e:
        logger.error(f"Error discovering agents from tmux: {e}")

    return discovered_agents


def sync_agents_from_tmux(token: str) -> Dict[str, Any]:
    """Synchronize agent tracking by discovering active agents from tmux sessions."""
    discovered_agents = discover_active_agents_from_tmux(token)

    # Import here to avoid circular imports
    from ..core import globals as g

    sync_summary: Dict[str, Any] = {
        "discovered_count": len(discovered_agents),
        "discovered_agents": [],
        "already_tracked": [],
        "newly_tracked": [],
    }

    for agent_info in discovered_agents:
        agent_id = agent_info["agent_id"]
        session_name = agent_info["session_name"]

        sync_summary["discovered_agents"].append(
            {
                "agent_id": agent_id,
                "session_name": session_name,
                "session_attached": agent_info["session_attached"],
            }
        )

        if agent_id in g.agent_tmux_sessions:
            if g.agent_tmux_sessions[agent_id] == session_name:
                sync_summary["already_tracked"].append(agent_id)
            else:
                g.agent_tmux_sessions[agent_id] = session_name
                sync_summary["newly_tracked"].append(agent_id)
                logger.info(
                    f"Updated session tracking for agent '{agent_id}': {session_name}"
                )
        else:
            g.agent_tmux_sessions[agent_id] = session_name
            sync_summary["newly_tracked"].append(agent_id)
            logger.info(
                f"Started tracking agent '{agent_id}' in session '{session_name}'"
            )

    return sync_summary


# ---------------------------------------------------------------------------
# Git worktree primitives (was utils/worktree_utils.py)
# ---------------------------------------------------------------------------


def is_git_repository(path: str = ".") -> bool:
    """Check if the given path is within a Git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
        return False


def get_current_branch(path: str = ".") -> Optional[str]:
    """Get the current branch name."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception as e:
        logger.error(f"Error getting current branch: {e}")
        return None


def branch_exists(branch_name: str, path: str = ".") -> bool:
    """Check if a branch exists in the repository."""
    try:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            cwd=path,
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def create_git_worktree(
    path: str,
    branch: str,
    base_branch: str = "main",
    repo_path: str = ".",
) -> Dict[str, Any]:
    """Create a new Git worktree."""
    try:
        abs_path = os.path.abspath(path)
        if os.path.exists(abs_path):
            return {
                "success": False,
                "error": f"Path already exists: {abs_path}",
                "path": abs_path,
            }

        parent_dir = os.path.dirname(abs_path)
        os.makedirs(parent_dir, exist_ok=True)

        if branch_exists(branch, repo_path):
            cmd = ["git", "worktree", "add", abs_path, branch]
            action = f"checkout existing branch '{branch}'"
        else:
            cmd = ["git", "worktree", "add", abs_path, "-b", branch, base_branch]
            action = f"create new branch '{branch}' from '{base_branch}'"

        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode == 0:
            logger.info(f"Created worktree at {abs_path} ({action})")
            return {
                "success": True,
                "path": abs_path,
                "branch": branch,
                "base_branch": base_branch,
                "action": action,
                "message": f"Worktree created at {abs_path}",
            }
        logger.error(f"Failed to create worktree: {result.stderr}")
        return {
            "success": False,
            "error": result.stderr.strip(),
            "command": " ".join(cmd),
            "path": abs_path,
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "Timeout creating worktree (repository might be very large)",
            "path": path,
        }
    except Exception as e:
        logger.error(f"Exception creating worktree: {e}")
        return {"success": False, "error": str(e), "path": path}


def list_git_worktrees(repo_path: str = ".") -> List[Dict[str, Any]]:
    """List all Git worktrees in the repository."""
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            logger.error(f"Failed to list worktrees: {result.stderr}")
            return []

        worktrees: List[Dict[str, Any]] = []
        current_worktree: Dict[str, Any] = {}

        for line in result.stdout.strip().split("\n"):
            if not line:
                continue

            if line.startswith("worktree "):
                if current_worktree:
                    worktrees.append(current_worktree)
                current_worktree = {"path": line[9:]}
            elif line.startswith("HEAD "):
                current_worktree["commit"] = line[5:]
            elif line.startswith("branch "):
                current_worktree["branch"] = line[7:]
            elif line == "bare":
                current_worktree["bare"] = True
            elif line == "detached":
                current_worktree["detached"] = True
            elif line == "locked":
                current_worktree["locked"] = True
            elif line == "prunable":
                current_worktree["prunable"] = True

        if current_worktree:
            worktrees.append(current_worktree)

        for wt in worktrees:
            wt["exists"] = os.path.exists(wt["path"])

        logger.debug(f"Found {len(worktrees)} worktrees")
        return worktrees

    except subprocess.TimeoutExpired:
        logger.error("Timeout listing worktrees")
        return []
    except Exception as e:
        logger.error(f"Error listing worktrees: {e}")
        return []


def has_uncommitted_changes(worktree_path: str) -> bool:
    """Check if a worktree has uncommitted changes."""
    try:
        if not os.path.exists(worktree_path):
            return False

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
        )

        return bool(result.stdout.strip()) if result.returncode == 0 else False

    except Exception as e:
        logger.error(f"Error checking for uncommitted changes in {worktree_path}: {e}")
        return True


def cleanup_git_worktree(
    path: str, force: bool = False, repo_path: str = "."
) -> Dict[str, Any]:
    """Remove a Git worktree."""
    try:
        abs_path = os.path.abspath(path)

        if not os.path.exists(abs_path):
            return {
                "success": True,
                "message": f"Worktree at {abs_path} doesn't exist",
                "path": abs_path,
            }

        if not force and has_uncommitted_changes(abs_path):
            return {
                "success": False,
                "error": "Worktree has uncommitted changes. Use force=True to override.",
                "uncommitted_changes": True,
                "path": abs_path,
            }

        cmd = ["git", "worktree", "remove", abs_path]
        if force:
            cmd.append("--force")

        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 0:
            logger.info(f"Removed worktree at {abs_path}")
            return {
                "success": True,
                "message": f"Worktree at {abs_path} removed successfully",
                "path": abs_path,
            }
        logger.error(f"Failed to remove worktree: {result.stderr}")
        return {
            "success": False,
            "error": result.stderr.strip(),
            "command": " ".join(cmd),
            "path": abs_path,
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "Timeout removing worktree",
            "path": path,
        }
    except Exception as e:
        logger.error(f"Exception removing worktree: {e}")
        return {"success": False, "error": str(e), "path": path}


def detect_project_setup_commands(worktree_path: str) -> List[str]:
    """Auto-detect common setup commands for the project type."""
    setup_commands: List[str] = []

    try:
        if os.path.exists(os.path.join(worktree_path, "package.json")):
            if os.path.exists(os.path.join(worktree_path, "yarn.lock")):
                setup_commands.append("yarn install")
            elif os.path.exists(os.path.join(worktree_path, "pnpm-lock.yaml")):
                setup_commands.append("pnpm install")
            else:
                setup_commands.append("npm install")

        if os.path.exists(os.path.join(worktree_path, "requirements.txt")):
            setup_commands.append("pip install -r requirements.txt")
        elif os.path.exists(os.path.join(worktree_path, "pyproject.toml")):
            setup_commands.append("pip install -e .")
        elif os.path.exists(os.path.join(worktree_path, "setup.py")):
            setup_commands.append("pip install -e .")

        if os.path.exists(os.path.join(worktree_path, "Cargo.toml")):
            setup_commands.append("cargo build")

        if os.path.exists(os.path.join(worktree_path, "go.mod")):
            setup_commands.append("go mod download")

        if os.path.exists(os.path.join(worktree_path, "pom.xml")):
            setup_commands.append("mvn dependency:resolve")

        if os.path.exists(
            os.path.join(worktree_path, "build.gradle")
        ) or os.path.exists(os.path.join(worktree_path, "build.gradle.kts")):
            setup_commands.append("./gradlew build")

        logger.debug(f"Detected setup commands for {worktree_path}: {setup_commands}")
        return setup_commands

    except Exception as e:
        logger.error(f"Error detecting setup commands: {e}")
        return []


def run_setup_commands(
    worktree_path: str, commands: List[str], timeout: int = 300
) -> Dict[str, Any]:
    """Run setup commands in the worktree directory."""
    results: List[Dict[str, Any]] = []
    original_cwd = os.getcwd()

    try:
        if not os.path.exists(worktree_path):
            return {
                "success": False,
                "error": f"Worktree path doesn't exist: {worktree_path}",
                "results": [],
            }

        os.chdir(worktree_path)
        logger.info(f"Running {len(commands)} setup commands in {worktree_path}")

        for cmd in commands:
            logger.debug(f"Running: {cmd}")
            try:
                result = subprocess.run(
                    cmd.split(),
                    cwd=worktree_path,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )

                cmd_result: Dict[str, Any] = {
                    "command": cmd,
                    "success": result.returncode == 0,
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }

                if result.returncode == 0:
                    logger.debug(f"{cmd} completed successfully")
                else:
                    logger.warning(f"{cmd} failed with code {result.returncode}")

                results.append(cmd_result)

            except subprocess.TimeoutExpired:
                cmd_result = {
                    "command": cmd,
                    "success": False,
                    "error": f"Command timed out after {timeout} seconds",
                }
                results.append(cmd_result)
                logger.error(f"{cmd} timed out")

            except Exception as e:
                cmd_result = {
                    "command": cmd,
                    "success": False,
                    "error": str(e),
                }
                results.append(cmd_result)
                logger.error(f"{cmd} failed with exception: {e}")

        success_count = sum(1 for r in results if r["success"])
        overall_success = success_count == len(commands)

        logger.info(
            f"Setup complete: {success_count}/{len(commands)} commands succeeded"
        )

        return {
            "success": overall_success,
            "results": results,
            "success_count": success_count,
            "total_commands": len(commands),
            "worktree_path": worktree_path,
        }

    except Exception as e:
        logger.error(f"Error running setup commands: {e}")
        return {"success": False, "error": str(e), "results": results}
    finally:
        os.chdir(original_cwd)


def generate_worktree_path(
    agent_id: str, token_suffix: str, base_path: str = "../agents"
) -> str:
    """Generate a standardized worktree path for an agent.

    ``token_suffix`` is the last 4 chars of the agent's own per-agent
    token (see ``get_token_suffix``). Was named ``admin_token_suffix``
    before retire-system-token Wave 5 — the suffix has been per-agent,
    not per-admin, since Wave 3 of prancy-napping-pie.
    """
    worktree_dir = f"{agent_id}-{token_suffix}"
    return os.path.abspath(os.path.join(base_path, worktree_dir))


def generate_branch_name(agent_id: str, custom_branch: Optional[str] = None) -> str:
    """Generate a standardized branch name for an agent."""
    if custom_branch:
        return custom_branch
    return f"agent/{agent_id}"


def validate_worktree_requirements(repo_path: str = ".") -> Dict[str, Any]:
    """Validate that worktree operations can be performed."""
    issues: List[str] = []

    if not is_git_repository(repo_path):
        issues.append("Not a Git repository")

    try:
        result = subprocess.run(
            ["git", "--version"], capture_output=True, timeout=5
        )
        if result.returncode != 0:
            issues.append("Git command not available")
    except Exception:
        issues.append("Git command not available")

    try:
        result = subprocess.run(
            ["git", "worktree", "--help"], capture_output=True, timeout=5
        )
        if result.returncode != 0:
            issues.append("Git worktree command not available (requires Git 2.5+)")
    except Exception:
        issues.append("Git worktree command not available")

    return {"valid": len(issues) == 0, "issues": issues}


# ---------------------------------------------------------------------------
# AgentRuntime — the small, intention-revealing class interface
# ---------------------------------------------------------------------------


class AgentRuntime:
    """Single owner of the "boot + prompt + discover + cleanup" seam for agents.

    The free-function primitives above remain the implementation —
    this class is the *interface*. Call sites that previously reached
    for ``send_prompt_async`` / ``discover_active_agents_from_tmux``
    /  ``session_exists`` / ``kill_tmux_session`` /
    ``create_git_worktree`` directly should hold an
    ``AgentRuntime`` instance and call its methods instead.

    The class has no per-instance state today — each method is a
    thin wrapper over the module-level subprocess primitives — so
    multiple instances are equivalent. The class exists so future
    PRs have a place to attach lifecycle (process supervision, audit
    hooks, async cleanup pools) without rewriting call sites.
    """

    # --- prompt delivery -------------------------------------------------

    def send_prompt(
        self, agent_id_or_session: str, prompt: str, *, delay_seconds: int = 3
    ) -> bool:
        """Deliver ``prompt`` to the agent's tmux session.

        Returns ``True`` on success, ``False`` if the session does not
        exist or the underlying ``tmux send-keys`` failed. Wire-
        equivalent to ``send_prompt_to_session`` — the legacy
        ``send_prompt_async`` helper is preserved at module scope for
        background callers.
        """
        return send_prompt_to_session(
            agent_id_or_session, prompt, delay_seconds=delay_seconds
        )

    # --- discovery -------------------------------------------------------

    def discover_active(self, token: str) -> List[Dict[str, Any]]:
        """Discover active agents by scanning tmux for the per-agent-token suffix."""
        return discover_active_agents_from_tmux(token)

    # --- liveness --------------------------------------------------------

    def is_alive(self, session_name: str) -> bool:
        """True iff the tmux session is currently alive."""
        return session_exists(session_name)

    # --- cleanup ---------------------------------------------------------

    def cleanup(self, session_name: str) -> bool:
        """Kill the tmux session for an agent. Idempotent on missing sessions."""
        return kill_tmux_session(session_name)

    # --- worktree primitive ---------------------------------------------

    def create_worktree(
        self,
        path: str,
        branch: str,
        base_branch: str = "main",
        repo_path: str = ".",
    ) -> Dict[str, Any]:
        """Create a git worktree for the agent's branch.

        Returns the same ``{"success": bool, ...}`` shape as
        :func:`create_git_worktree` — preserves wire-equivalent
        semantics so the existing
        :func:`agent_mcp.features.worktree_integration` call sites
        keep working when they migrate.
        """
        return create_git_worktree(path, branch, base_branch=base_branch, repo_path=repo_path)


# ---------------------------------------------------------------------------
# Module-level singleton accessor (mirrors the repositories pattern)
# ---------------------------------------------------------------------------


_runtime_instance: Optional[AgentRuntime] = None


def get_runtime() -> AgentRuntime:
    """Return the canonical :class:`AgentRuntime` instance.

    The class has no state today, so lazy single-instance reuse is
    fine — there is no lifespan binding to wire up. If future PRs add
    per-instance state, this is the seam to swap for a lifecycle-owned
    slot (mirroring :func:`agent_mcp.repositories.get_task_repo`).
    """
    global _runtime_instance
    if _runtime_instance is None:
        _runtime_instance = AgentRuntime()
    return _runtime_instance


# Canonical module-attribute instance (for parity with
# ``agent_mcp.repositories.task_repo`` shape). The test contract pins
# ``agent_runtime.get_runtime()`` as the resolution path; we also
# expose ``agent_runtime_instance`` as a convenience alias.
agent_runtime_instance = get_runtime()


__all__ = [
    # Class + accessor
    "AgentRuntime",
    "agent_runtime_instance",
    "get_runtime",
    # Tmux primitives
    "is_tmux_available",
    "sanitize_session_name",
    "create_tmux_session",
    "session_exists",
    "list_tmux_sessions",
    "kill_tmux_session",
    "get_session_status",
    "send_command_to_session",
    "send_prompt_to_session",
    "send_prompt_async",
    "cleanup_agent_sessions",
    # Naming policy
    "get_token_suffix",
    "get_admin_token_suffix",  # back-compat alias for get_token_suffix
    "generate_agent_session_name",
    "parse_agent_session_name",
    "discover_active_agents_from_tmux",
    "sync_agents_from_tmux",
    # Git worktree primitives
    "is_git_repository",
    "get_current_branch",
    "branch_exists",
    "create_git_worktree",
    "list_git_worktrees",
    "has_uncommitted_changes",
    "cleanup_git_worktree",
    "detect_project_setup_commands",
    "run_setup_commands",
    "generate_worktree_path",
    "generate_branch_name",
    "validate_worktree_requirements",
]
