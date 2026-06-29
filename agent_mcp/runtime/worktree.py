# Agent-MCP/agent_mcp/runtime/worktree.py
"""Git worktree primitives — the surviving half of the old
``agent_mcp.runtime.agent_runtime`` module.

Wave 7 PR 3 (coordinator transition, 2026-06-29) deleted the
spawn-claude-via-tmux machinery that used to share a module with
these git-worktree helpers. The two were never coupled; they were
co-located because both were "agent runtime" primitives in the
pre-coordinator era. Under the coordinator model (agent-mcp mints
tokens, user owns the claude process), the tmux half is gone but
the worktree half is still useful for the
:mod:`agent_mcp.features.worktree_integration` surface (operator-
driven worktree provisioning for parallel agents working on
isolated branches).

Call sites should import from here directly::

    from agent_mcp.runtime.worktree import cleanup_git_worktree
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

try:
    from ..core.config import logger as _config_logger  # type: ignore
except Exception:  # pragma: no cover — fallback if core.config not importable
    _config_logger = logging.getLogger(__name__)

logger = _config_logger


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
    token. The historical helper that minted this suffix
    (``get_token_suffix`` in the deleted ``agent_runtime`` module) went
    away with Wave 7 PR 3; callers that still want a 4-char suffix
    derive it inline (``token[-4:].lower()``).
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


__all__ = [
    "branch_exists",
    "cleanup_git_worktree",
    "create_git_worktree",
    "detect_project_setup_commands",
    "generate_branch_name",
    "generate_worktree_path",
    "get_current_branch",
    "has_uncommitted_changes",
    "is_git_repository",
    "list_git_worktrees",
    "run_setup_commands",
    "validate_worktree_requirements",
]
