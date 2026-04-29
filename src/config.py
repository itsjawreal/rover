from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path


def _looks_like_repo_root(path: Path) -> bool:
    return (
        (path / "README.md").exists()
        and (path / "app" / "builder.py").exists()
        and (path / "src" / "config.py").exists()
    )


def _discover_root() -> Path:
    env_root = os.getenv("GITHUB_CONTRIBUTION_ENGINE_ROOT", "").strip()
    if env_root:
        candidate = Path(env_root).expanduser().resolve()
        if _looks_like_repo_root(candidate):
            return candidate

    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if _looks_like_repo_root(candidate):
            return candidate

    return Path(__file__).resolve().parent.parent


# ── Environment ──────────────────────────────────────────────
ROOT = _discover_root()
ENV_FILE = ROOT / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


# ── Paths ────────────────────────────────────────────────────
APP_DIR = ROOT / "app"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
RUNS_DIR = ROOT / "runs"
PR_LOG_FILE = DATA_DIR / "pr_log.json"
SECURITY_BLACKLIST_FILE = DATA_DIR / "security_blacklist.json"
PROJECT_BLACKLIST_FILE = DATA_DIR / "project_blacklist.json"
STREAM_DIR = ROOT / ".stream_partials"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
RUNS_DIR.mkdir(exist_ok=True)


# ── Runtime flags ────────────────────────────────────────────
DRY_RUN = "--dry-run" in sys.argv
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "7"))


# ── AI backend ───────────────────────────────────────────────
def _detect_ai_backend() -> str:
    if "--codex" in sys.argv:
        return "codex"
    if "--claude" in sys.argv:
        return "claude"
    return os.getenv("AI_BACKEND", "codex").strip().lower() or "codex"


def _split_args(raw: str, default: list[str]) -> list[str]:
    try:
        parsed = shlex.split(raw, posix=False)
    except Exception:
        parsed = []
    return parsed or default


def _find_claude() -> str:
    if env := os.getenv("CLAUDE_CMD"):
        return env
    if found := shutil.which("claude"):
        return found
    return "claude"


def _find_codex() -> str:
    if env := os.getenv("CODEX_CMD"):
        return env
    if found := shutil.which("codex"):
        return found
    return "codex"


AI_BACKEND = _detect_ai_backend()
CLAUDE_CMD = _find_claude()
CODEX_CMD = _find_codex()
CLAUDE_ARGS = _split_args(os.getenv("CLAUDE_ARGS", "-p -"), ["-p", "-"])
CODEX_ARGS = _split_args(
    os.getenv("CODEX_ARGS", "exec --skip-git-repo-check -s read-only -"),
    ["exec", "--skip-git-repo-check", "-s", "read-only", "-"],
)
if CODEX_ARGS == ["-p", "-"]:
    CODEX_ARGS = ["exec", "--skip-git-repo-check", "-s", "read-only", "-"]


def _default_agent_tool() -> str:
    if AI_BACKEND == "claude":
        return "Claude Code"
    return "Codex"


def _default_model_series() -> str:
    if AI_BACKEND == "claude":
        return "Claude"
    return "GPT"


AGENT_TOOL = os.getenv("AGENT_TOOL", _default_agent_tool()).strip() or _default_agent_tool()
MODEL_SERIES = os.getenv("MODEL_SERIES", _default_model_series()).strip() or _default_model_series()

AI_TIMEOUT_MULTIPLIER = float(os.getenv("AI_TIMEOUT_MULTIPLIER", "1.0"))
AI_TIMEOUT_MAX_SECONDS = int(os.getenv("AI_TIMEOUT_MAX_SECONDS", "900"))


# ── Notifications ────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")


# ── Error classification ─────────────────────────────────────
CREDIT_LIMIT_KEYWORDS = [
    "credit balance",
    "quota exceeded",
    "billing",
    "insufficient credits",
    "upgrade your plan",
    "usage limit reached",
    "you've reached your limit",
    "monthly limit",
    "weekly limit",
]
RATE_LIMIT_KEYWORDS = [
    "rate limit",
    "too many requests",
    "429",
    "overloaded",
    "try again later",
    "try again in",
]


class CreditLimitError(Exception):
    """Raised when the selected AI backend reports exhausted credits or quota."""
