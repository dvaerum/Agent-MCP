# Agent-MCP/mcp_template/mcp_server_src/core/config.py
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# Product metadata. The version itself is NOT here — it lives at
# ``agent_mcp.__version__`` (importlib.metadata / pyproject.toml as the
# single source of truth; see tests/test_version_single_source.py). A
# hand-maintained ``VERSION = "2.0"`` literal used to live here and had
# drifted years behind the real 5.4.0 (arch-r4 #11b).
GITHUB_REPO = "rinadelph/Agent-MCP"
AUTHOR = "Luis Alejandro Rincon"
GITHUB_URL = "https://github.com/rinadelph"


# --- TUI Colors (ANSI Escape Codes) ---
class TUIColors:
    HEADER = "\033[95m"  # Light Magenta
    OKBLUE = "\033[94m"  # Light Blue
    OKCYAN = "\033[96m"  # Light Cyan
    OKGREEN = "\033[92m"  # Light Green
    WARNING = "\033[93m"  # Yellow
    FAIL = "\033[91m"  # Red
    ENDC = "\033[0m"  # Reset to default
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    DIM = "\033[2m"

    # Specific log level colors
    DEBUG = OKCYAN
    INFO = OKGREEN
    WARNING = WARNING
    ERROR = FAIL
    CRITICAL = BOLD + FAIL


class ColorfulFormatter(logging.Formatter):
    """Custom formatter to add colors to log messages for console output."""

    LOG_LEVEL_COLORS = {
        logging.DEBUG: TUIColors.DEBUG,
        logging.INFO: TUIColors.INFO,
        logging.WARNING: TUIColors.WARNING,
        logging.ERROR: TUIColors.ERROR,
        logging.CRITICAL: TUIColors.CRITICAL,
    }

    def format(self, record):
        color = self.LOG_LEVEL_COLORS.get(record.levelno, TUIColors.ENDC)
        record.levelname = (
            f"{color}{record.levelname:<8}{TUIColors.ENDC}"  # Pad levelname
        )
        record.name = f"{TUIColors.OKBLUE}{record.name}{TUIColors.ENDC}"
        return super().format(record)


# --- General Configuration ---
DB_FILE_NAME: str = "mcp_state.db"  # From main.py:39

# --- Logging Configuration ---
LOG_FILE_NAME: str = "mcp_server.log"  # Based on main.py:46
LOG_LEVEL: int = logging.INFO  # From main.py:43
LOG_FORMAT_FILE: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_FORMAT_CONSOLE: str = (
    f"%(asctime)s - %(name)s - %(levelname)s - {TUIColors.DIM}%(message)s{TUIColors.ENDC}"  # Dim message text
)

CONSOLE_LOGGING_ENABLED = (
    os.environ.get("MCP_DEBUG", "false").lower() == "true"
)  # Enable console logging in debug mode


def setup_logging():
    """Configures global logging for the application."""

    root_logger = logging.getLogger()  # Get the root logger
    root_logger.setLevel(LOG_LEVEL)  # Set level on the root logger

    # Clear any existing handlers on the root logger to avoid duplication
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # 1. File Handler (only in debug mode)
    debug_mode = os.environ.get("MCP_DEBUG", "false").lower() == "true"
    if debug_mode:
        file_formatter = logging.Formatter(LOG_FORMAT_FILE)
        file_handler = logging.FileHandler(
            LOG_FILE_NAME, mode="a", encoding="utf-8"
        )  # Append mode
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)

    # 2. Console Handler (with colors, conditional on MCP_DEBUG)
    if CONSOLE_LOGGING_ENABLED:
        console_formatter = ColorfulFormatter(
            LOG_FORMAT_CONSOLE, datefmt="%H:%M:%S"
        )  # Simpler datefmt for console
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(console_formatter)
        # Filter out less important messages for console if desired
        # console_handler.setLevel(logging.INFO)  # Example: only INFO and above for console
        root_logger.addHandler(console_handler)

    # 3. Stderr lifecycle handler — always attached at WARNING+ so
    # critical events (lifespan failures, DB errors, signal-driven
    # shutdowns) reach journald even when MCP_DEBUG is unset. Without
    # this, `journalctl --user -u agent-mcp@<project>.service` shows
    # only the banner + Python tracebacks; structured `logger.error(…)`
    # calls go to /dev/null because no handler is attached. Operators
    # then can't tell the difference between "lifespan crashed" and
    # "lifespan succeeded silently". WARNING (not INFO) is the floor
    # because INFO is too chatty for steady-state operation.
    if not any(
        isinstance(h, logging.StreamHandler) and h.stream == sys.stderr
        for h in root_logger.handlers
    ):
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(logging.Formatter(LOG_FORMAT_FILE))
        stderr_handler.setLevel(logging.WARNING)
        root_logger.addHandler(stderr_handler)

    # Suppress overly verbose logs from specific libraries for both file and console
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    # Uvicorn access logs are handled by Uvicorn's config (access_log=False in cli.py)
    # but we can also try to manage its error logger if needed.
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)  # General uvicorn logger
    logging.getLogger("mcp.server.lowlevel.server").propagate = (
        False  # Prevent duplication if it logs directly
    )


def enable_console_logging():
    """Enable console logging dynamically (used when debug mode is enabled)."""
    global CONSOLE_LOGGING_ENABLED
    CONSOLE_LOGGING_ENABLED = True
    # Re-setup logging to add file handler when debug mode is enabled
    setup_logging()

    root_logger = logging.getLogger()

    # Check if console handler already exists
    has_console_handler = any(
        isinstance(handler, logging.StreamHandler) and handler.stream == sys.stdout
        for handler in root_logger.handlers
    )

    if not has_console_handler:
        console_formatter = ColorfulFormatter(LOG_FORMAT_CONSOLE, datefmt="%H:%M:%S")
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(console_formatter)
        # Set logging level to DEBUG for more verbose output
        console_handler.setLevel(logging.DEBUG)
        root_logger.addHandler(console_handler)

        # Also set root logger to DEBUG level
        root_logger.setLevel(logging.DEBUG)


# Initialize logging when this module is imported
setup_logging()
logger = logging.getLogger("mcp_server")  # Main application logger

# --- Agent Appearance ---
AGENT_COLORS: List[str] = (
    [  # From main.py:160-164 (Note: original had 160-165, but list ends on 164)
        "#FF5733",
        "#33FF57",
        "#3357FF",
        "#FF33A1",
        "#A133FF",
        "#33FFA1",
        "#FFBD33",
        "#33FFBD",
        "#BD33FF",
        "#FF3333",
        "#33FF33",
        "#3333FF",
        "#FF8C00",
        "#00CED1",
        "#9400D3",
        "#FF1493",
        "#7FFF00",
        "#1E90FF",
    ]
)

# --- OpenAI Model Configuration ---
# Advanced mode flag - set by CLI
ADVANCED_EMBEDDINGS: bool = False  # Default to simple mode

# Auto-indexing control - set by CLI
DISABLE_AUTO_INDEXING: bool = False  # Default to automatic indexing

# --- OpenAI / Ollama defaults --------------------------------------
# When OPENAI_API_KEY is unset (or empty), default to the bundled
# local Ollama endpoint. Operators get a functional server out of the
# box; setting OPENAI_API_KEY to a real key switches over to the
# OpenAI cloud — we deliberately do NOT touch OPENAI_BASE_URL /
# OPENAI_MODEL in that branch because clobbering a user-supplied key
# with Ollama defaults would silently break the cloud path.
#
# Uses os.environ.setdefault so an operator who exported some of the
# vars but not OPENAI_API_KEY still wins where they set a value.
#
# ORDERING IS LOAD-BEARING: this block MUST run before the
# SIMPLE_EMBEDDING_MODEL / SIMPLE_EMBEDDING_DIMENSION reads below.
# Those constants are bound ONCE at import time from os.environ; if the
# Ollama setdefaults ran after them (as they used to), the constants
# froze to the OpenAI fallbacks (text-embedding-3-large / 1536) that
# Ollama does not serve, and every RAG indexing cycle 404'd.
_OPENAI_API_KEY_RAW = os.environ.get("OPENAI_API_KEY")
if not _OPENAI_API_KEY_RAW:
    os.environ.setdefault("OPENAI_API_KEY", "ollama")
    os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    os.environ.setdefault("OPENAI_MODEL", "qwen3:1.7b")
    os.environ.setdefault("AGENT_MCP_EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    os.environ.setdefault("AGENT_MCP_EMBEDDING_DIMENSION", "1024")
    logger.info(
        "OPENAI_API_KEY not set — defaulting to local Ollama at "
        "http://127.0.0.1:11434/v1 (qwen3:1.7b). Set OPENAI_API_KEY to "
        "use a different endpoint."
    )

# Original/Simple mode configuration (default).
#
# Both values are overridable at process start via env vars. With no
# provider env set the Ollama defaults seeded just above apply out of
# the box (qwen3-embedding:0.6b / 1024); with OPENAI_API_KEY set the
# block above is skipped and these fall back to the OpenAI cloud
# defaults (text-embedding-3-large / 1536). An explicit
# AGENT_MCP_EMBEDDING_MODEL + AGENT_MCP_EMBEDDING_DIMENSION always wins
# (setdefault won't clobber it); no code change required.
SIMPLE_EMBEDDING_MODEL: str = os.environ.get(
    "AGENT_MCP_EMBEDDING_MODEL", "text-embedding-3-large"
)
SIMPLE_EMBEDDING_DIMENSION: int = int(
    os.environ.get("AGENT_MCP_EMBEDDING_DIMENSION", "1536")
)

# Advanced mode configuration - new enhanced mode
ADVANCED_EMBEDDING_MODEL: str = "text-embedding-3-large"  # From main.py:178
ADVANCED_EMBEDDING_DIMENSION: int = (
    3072  # Full dimension for text-embedding-3-large for better code understanding
)


@dataclass(frozen=True)
class EmbeddingSettings:
    """Resolved (model, dimension, advanced) for the embedding seam.

    Single source of truth for "which embedding config is active right
    now" — replaces the old ``EMBEDDING_MODEL`` / ``EMBEDDING_DIMENSION``
    module-level constants, which were computed ONCE at import time and
    then mutated in place by ``server_bootstrap.apply_runtime_flags`` for
    ``--advanced`` mode. That mutation was only visible to callers who
    read the attribute live (``_config.EMBEDDING_DIMENSION``); a caller
    who did ``from ...core.config import EMBEDDING_DIMENSION`` bound the
    name at ITS OWN import time and silently kept the pre-mutation
    (simple-mode) value forever — an unenforced import-order dependency
    that, for ``db/schema.py``, could build the sqlite-vec column at the
    wrong dimension.

    ``embedding_settings()`` closes this: it is a function, so every
    call site re-resolves from the current state instead of freezing a
    name binding at import time.
    """

    model: str
    dimension: int
    advanced: bool


def embedding_settings(advanced: Optional[bool] = None) -> "EmbeddingSettings":
    """Resolve the embedding (model, dimension) for the current mode.

    ``advanced``, when given, resolves settings directly from that flag
    — the path for a caller holding a ``ServerConfig`` (e.g.
    ``server_bootstrap.apply_runtime_flags``), which needs no module
    state at all. When omitted, falls back to the ``ADVANCED_EMBEDDINGS``
    flag that ``apply_runtime_flags`` sets at boot — the path for the
    many call sites deep in the RAG / db layers that have no
    ``ServerConfig`` to hand and must consult "what mode is the running
    server in".
    """
    is_advanced = ADVANCED_EMBEDDINGS if advanced is None else advanced
    if is_advanced:
        return EmbeddingSettings(
            model=ADVANCED_EMBEDDING_MODEL,
            dimension=ADVANCED_EMBEDDING_DIMENSION,
            advanced=True,
        )
    return EmbeddingSettings(
        model=SIMPLE_EMBEDDING_MODEL,
        dimension=SIMPLE_EMBEDDING_DIMENSION,
        advanced=False,
    )

# Chat / task-analysis model names are no longer hardcoded here.
# v5.0.43 hardcoded "gpt-4.1-2025-04-14" — a non-existent OpenAI model
# id (typo) — which broke RAG on the first deployment that lacked an
# OPENAI_API_KEY. The model now flows through
# `agent_mcp.external.completion_service.completion_client()`, which
# picks Ollama vs OpenAI from env vars (OPENAI_API_KEY / OPENAI_MODEL
# / OLLAMA_MODEL). See that module's docstring for the decision table.
#
# Embedding likewise flows through
# `agent_mcp.external.embedding_service.embedding_client()`, which owns
# (model, dimension, base_url, api_key). The former MAX_EMBEDDING_BATCH_SIZE
# constant was dead (the real batch controls are
# PARALLEL_EMBEDDING_BATCH_SIZE / MAX_CONCURRENT_EMBEDDING_REQUESTS in
# features/rag/indexing.py) and was removed.
def _positive_int_env(name: str, default: int) -> int:
    """Parse a positive-integer env var, falling back GRACEFULLY.

    A non-integer or non-positive value must never crash the server on a
    typo — we log a warning and return ``default``.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; falling back to default %d.",
            name,
            raw,
            default,
        )
        return default
    if value <= 0:
        logger.warning(
            "%s=%r must be a positive integer; falling back to default %d.",
            name,
            raw,
            default,
        )
        return default
    return value


# The model's usable context-token budget, env-overridable via
# AGENT_MCP_MAX_CONTEXT_TOKENS. Defaults to 1,000,000 (GPT-4.1's window),
# so cloud deployments are unchanged. The RAG assembler
# (`features/rag/query.py:_append_within_budget`) truncates the retrieved
# context to fit this budget — so a small-context local model (llama-cpp /
# Ollama, ~8k window) should set this to its window minus headroom for the
# system prompt + query + answer, otherwise the model 400s on oversized
# RAG prompts (`exceed_context_size_error`).
MAX_CONTEXT_TOKENS: int = _positive_int_env("AGENT_MCP_MAX_CONTEXT_TOKENS", 1000000)
# Task analysis must also fit the model, so it shares the same budget.
TASK_ANALYSIS_MAX_TOKENS: int = MAX_CONTEXT_TOKENS

# --- Project Directory Helpers ---
# These rely on an environment variable "MCP_PROJECT_DIR" being set,
# typically by the CLI entry point (previously in main.py:1953, will be in cli.py).


def get_project_dir() -> Path:
    """Gets the resolved absolute path to the project directory."""
    project_dir_str = os.environ.get("MCP_PROJECT_DIR")
    if not project_dir_str:
        # This case should ideally be handled at startup by the CLI,
        # ensuring MCP_PROJECT_DIR is always set.
        logger.error("CRITICAL: MCP_PROJECT_DIR environment variable is not set.")
        # Fallback to current directory, but this is likely not intended for normal operation.
        return Path(".").resolve()
    return Path(project_dir_str).resolve()


def get_agent_dir() -> Path:
    """Gets the path to the .agent directory within the project directory."""
    return get_project_dir() / ".agent"


def get_db_path() -> Path:
    """Gets the full path to the SQLite database file."""
    return get_agent_dir() / DB_FILE_NAME


OPENAI_API_KEY_ENV: Optional[str] = os.environ.get("OPENAI_API_KEY")

# --- Task Placement Configuration (System 8) ---
ENABLE_TASK_PLACEMENT_RAG: bool = (
    os.getenv("ENABLE_TASK_PLACEMENT_RAG", "true").lower() == "true"
)
TASK_DUPLICATION_THRESHOLD: float = float(
    os.getenv("TASK_DUPLICATION_THRESHOLD", "0.8")
)
ALLOW_RAG_OVERRIDE: bool = os.getenv("ALLOW_RAG_OVERRIDE", "true").lower() == "true"
TASK_PLACEMENT_RAG_TIMEOUT: int = int(
    os.getenv("TASK_PLACEMENT_RAG_TIMEOUT", "5")
)  # seconds

# Log that configuration is loaded (optional)
logger.info("Core configuration loaded (with colorful logging setup).")
# Example of how other modules will use this logger:
# from mcp_server_src.core.config import logger
# logger.info("This is a log message from another module.")
