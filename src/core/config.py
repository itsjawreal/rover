from __future__ import annotations

import getpass
import os
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


def _looks_like_repo_root(path: Path) -> bool:
    return (
        (path / "README.md").exists()
        and (path / "app" / "builder.py").exists()
        and (path / "src" / "core" / "config.py").exists()
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

    return Path(__file__).resolve().parents[2]


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

if github_token := os.getenv("GITHUB_TOKEN", "").strip():
    os.environ.setdefault("GH_TOKEN", github_token)
elif gh_token := os.getenv("GH_TOKEN", "").strip():
    os.environ.setdefault("GITHUB_TOKEN", gh_token)


# ── Paths ────────────────────────────────────────────────────
APP_DIR = ROOT / "app"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
RUNS_DIR = ROOT / "runs"
STREAM_DIR = ROOT / ".stream_partials"


def _ensure_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _detect_storage_mode() -> str:
    explicit = os.getenv("ROVER_STORAGE_MODE", "").strip().lower()
    if explicit in {"persistent", "workspace", "ephemeral"}:
        return explicit
    ci_markers = (
        "CI",
        "GITHUB_ACTIONS",
        "CODESPACES",
        "GITPOD_WORKSPACE_ID",
        "REPL_ID",
        "REPL_SLUG",
    )
    if any(os.getenv(name, "").strip() for name in ci_markers):
        return "ephemeral"
    return "persistent"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    return values or default


def _default_persistent_rover_home() -> Path:
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "Rover"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Rover"
    xdg_state_home = os.getenv("XDG_STATE_HOME", "").strip()
    if xdg_state_home:
        return Path(xdg_state_home).expanduser() / "rover"
    return Path.home() / ".local" / "state" / "rover"


def _default_ephemeral_rover_home() -> Path:
    user = os.getenv("USER") or os.getenv("USERNAME") or getpass.getuser() or "user"
    return Path(tempfile.gettempdir()) / f"rover-{user}"


def _candidate_rover_homes(mode: str) -> list[Path]:
    candidates: list[Path] = []
    if explicit := os.getenv("ROVER_HOME", "").strip():
        candidates.append(Path(explicit).expanduser())
    if mode == "workspace":
        candidates.append(ROOT / ".rover")
    elif mode == "ephemeral":
        candidates.append(_default_ephemeral_rover_home())
    else:
        candidates.append(_default_persistent_rover_home())
    candidates.append(Path.home() / ".rover")
    candidates.append(ROOT / ".rover")
    return candidates


def _resolve_rover_home(mode: str) -> tuple[Path, bool]:
    for candidate in _candidate_rover_homes(mode):
        resolved = candidate.expanduser()
        if _ensure_dir(resolved):
            return resolved.resolve(), True
    fallback = (ROOT / ".rover").resolve()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback, False


def _resolve_storage_subdir(env_name: str, default_name: str, *, home: Path) -> tuple[Path, bool]:
    if explicit := os.getenv(env_name, "").strip():
        candidate = Path(explicit).expanduser()
    else:
        candidate = home / default_name
    if _ensure_dir(candidate):
        return candidate.resolve(), True
    fallback = (home / default_name).resolve()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback, False


ROVER_STORAGE_MODE = _detect_storage_mode()
ROVER_HOME, ROVER_HOME_WRITABLE = _resolve_rover_home(ROVER_STORAGE_MODE)
ROVER_STATE_DIR, ROVER_STATE_DIR_WRITABLE = _resolve_storage_subdir(
    "ROVER_STATE_DIR", "state", home=ROVER_HOME
)
ROVER_CACHE_DIR, ROVER_CACHE_DIR_WRITABLE = _resolve_storage_subdir(
    "ROVER_CACHE_DIR", "cache", home=ROVER_HOME
)
ROVER_ARTIFACT_DIR, ROVER_ARTIFACT_DIR_WRITABLE = _resolve_storage_subdir(
    "ROVER_ARTIFACT_DIR", "artifacts", home=ROVER_HOME
)
ROVER_CONFIG_DIR, ROVER_CONFIG_DIR_WRITABLE = _resolve_storage_subdir(
    "ROVER_CONFIG_DIR", "config", home=ROVER_HOME
)
PR_LOG_FILE = ROVER_STATE_DIR / "pr_log.json"
SECURITY_BLACKLIST_FILE = ROVER_STATE_DIR / "security_blacklist.json"
PROJECT_BLACKLIST_FILE = ROVER_STATE_DIR / "project_blacklist.json"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
RUNS_DIR.mkdir(exist_ok=True)


# ── Runtime flags ────────────────────────────────────────────
DRY_RUN = "--dry-run" in sys.argv
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "7"))
ENABLE_DEP_UPDATE = os.getenv("ENABLE_DEP_UPDATE", "true").strip().lower() != "false"


# ── AI backend ───────────────────────────────────────────────
def _detect_ai_backend() -> str:
    if "--codex" in sys.argv:
        return "codex"
    if "--claude" in sys.argv:
        return "claude"
    return os.getenv("AI_BACKEND", "codex").strip().lower() or "codex"


def _allow_backend_fallback() -> bool:
    raw = (
        os.getenv("ALLOW_BACKEND_FALLBACK")
        or os.getenv("ALLOW_BACKEND_FALBACK")
        or os.getenv("ALLOW_CLAUDE_FALLBACK")
        or ""
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _split_args(raw: str, default: list[str]) -> list[str]:
    try:
        parsed = shlex.split(raw, posix=False)
    except Exception:
        parsed = []
    return parsed or default


def _is_unusable_cross_os_cli_path(candidate: str) -> bool:
    if os.name == "nt":
        return False
    normalized = candidate.replace("\\", "/").lower()
    if not normalized.startswith("/mnt/"):
        return False
    return any(marker in normalized for marker in ("/appdata/", "/program files/", "/windowsapps/", "/users/"))


@dataclass(frozen=True)
class ResolvedCliCommand:
    env_name: str
    command_name: str
    command: str
    source: str
    env_value: str
    notes: tuple[str, ...]


def _looks_like_existing_cli(candidate: str) -> bool:
    if not candidate or _is_unusable_cross_os_cli_path(candidate):
        return False
    try:
        if Path(candidate).expanduser().exists():
            return True
    except OSError:
        pass
    found = shutil.which(candidate)
    if not found:
        return False
    return not _is_unusable_cross_os_cli_path(found)


def _common_cli_fallback_candidates(command_name: str) -> list[str]:
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / command_name,
        home / ".local" / "npm" / "bin" / command_name,
        home / ".npm-global" / "bin" / command_name,
        home / ".bun" / "bin" / command_name,
    ]
    if os.name == "nt":
        local_app_data = Path(os.getenv("LOCALAPPDATA") or (home / "AppData" / "Local"))
        app_data = Path(os.getenv("APPDATA") or (home / "AppData" / "Roaming"))
        candidates.extend(
            [
                local_app_data / "Programs" / command_name / f"{command_name}.exe",
                app_data / "npm" / f"{command_name}.cmd",
                app_data / "npm" / f"{command_name}.exe",
            ]
        )
    return [str(path.expanduser()) for path in candidates]


def _resolve_cli_command_details(env_name: str, command_name: str) -> ResolvedCliCommand:
    env = os.getenv(env_name, "").strip()
    notes: list[str] = []
    if env:
        if _is_unusable_cross_os_cli_path(env):
            notes.append(f"{env_name} points to a cross-OS path that is unusable here: {env}")
        elif _looks_like_existing_cli(env):
            return ResolvedCliCommand(env_name, command_name, env, "env", env, tuple(notes))
        else:
            notes.append(f"{env_name} was set but not found: {env}")

    found = shutil.which(command_name)
    if found and not _is_unusable_cross_os_cli_path(found):
        return ResolvedCliCommand(env_name, command_name, found, "path", env, tuple(notes))

    for candidate in _common_cli_fallback_candidates(command_name):
        if _looks_like_existing_cli(candidate):
            notes.append(f"resolved from common fallback path: {candidate}")
            return ResolvedCliCommand(env_name, command_name, candidate, "fallback", env, tuple(notes))

    return ResolvedCliCommand(env_name, command_name, command_name, "missing", env, tuple(notes))


def _find_claude() -> str:
    return _resolve_cli_command_details("CLAUDE_CMD", "claude").command


def _find_codex() -> str:
    return _resolve_cli_command_details("CODEX_CMD", "codex").command


AI_BACKEND = _detect_ai_backend()
ALLOW_BACKEND_FALLBACK = _allow_backend_fallback()
CLAUDE_CMD_RESOLUTION = _resolve_cli_command_details("CLAUDE_CMD", "claude")
CODEX_CMD_RESOLUTION = _resolve_cli_command_details("CODEX_CMD", "codex")
CLAUDE_CMD = CLAUDE_CMD_RESOLUTION.command
CODEX_CMD = CODEX_CMD_RESOLUTION.command
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
OPENCLAW_CMD_RESOLUTION = _resolve_cli_command_details("OPENCLAW_CMD", "openclaw")
OPENCLAW_CMD = OPENCLAW_CMD_RESOLUTION.command
OPENCLAW_NOTIFY_CHANNEL = os.getenv("OPENCLAW_NOTIFY_CHANNEL", "telegram").strip().lower() or "telegram"
OPENCLAW_NOTIFY_TARGET = os.getenv("OPENCLAW_NOTIFY_TARGET", "").strip()
OPENCLAW_NOTIFY_ACCOUNT = os.getenv("OPENCLAW_NOTIFY_ACCOUNT", "").strip()
OPENCLAW_NOTIFY_THREAD_ID = os.getenv("OPENCLAW_NOTIFY_THREAD_ID", "").strip()


def _default_notify_transport() -> str:
    configured = os.getenv("ROVER_NOTIFY_TRANSPORT", "").strip().lower()
    if configured:
        return configured
    if OPENCLAW_NOTIFY_TARGET:
        return "openclaw"
    if TELEGRAM_TOKEN and TELEGRAM_CHAT:
        return "telegram"
    return ""


ROVER_NOTIFY_TRANSPORT = _default_notify_transport()
ROVER_NOTIFY_PROGRESS = _env_bool("ROVER_NOTIFY_PROGRESS", False)
ROVER_NOTIFY_INTERVAL_SECONDS = _env_int("ROVER_NOTIFY_INTERVAL_SECONDS", 60, minimum=5)
ROVER_NOTIFY_STALL_SECONDS = _env_int("ROVER_NOTIFY_STALL_SECONDS", 300, minimum=0)
ROVER_NOTIFY_ONLY_ON_CHANGE = _env_bool("ROVER_NOTIFY_ONLY_ON_CHANGE", True)
ROVER_NOTIFY_ON_EVENT_TYPES = _env_csv(
    "ROVER_NOTIFY_ON_EVENT_TYPES",
    ("started", "repo_selected", "stage", "patch_generated", "pr_submitted", "completed", "failed", "stalled"),
)
ROVER_NOTIFY_MAX_MESSAGE_CHARS = _env_int("ROVER_NOTIFY_MAX_MESSAGE_CHARS", 3500, minimum=200)


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
