from __future__ import annotations

import getpass
import logging
import os
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_DEPRECATED_ENV_WARNED: set[str] = set()


def _warn_deprecated_env(legacy_name: str, new_name: str) -> None:
    if legacy_name in _DEPRECATED_ENV_WARNED:
        return
    _DEPRECATED_ENV_WARNED.add(legacy_name)
    log.warning(
        "%s is deprecated and will be removed in a future release; rename it to %s in your environment/.env.",
        legacy_name,
        new_name,
    )


def _env_raw(name: str) -> str:
    """Read an env var, falling back to its deprecated ROVER_* spelling."""
    value = os.getenv(name, "").strip()
    if value:
        return value
    if name.startswith("MENISIK_"):
        legacy_name = "ROVER_" + name[len("MENISIK_"):]
        legacy_value = os.getenv(legacy_name, "").strip()
        if legacy_value:
            _warn_deprecated_env(legacy_name, name)
            return legacy_value
    return ""


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
    explicit = _env_raw("MENISIK_STORAGE_MODE").lower()
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
    raw = _env_raw(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _env_raw(name)
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env_raw(name)
    if not raw:
        return default
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    return values or default


def _default_persistent_home(name_title: str, name_lower: str) -> Path:
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / name_title
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / name_title
    xdg_state_home = os.getenv("XDG_STATE_HOME", "").strip()
    if xdg_state_home:
        return Path(xdg_state_home).expanduser() / name_lower
    return Path.home() / ".local" / "state" / name_lower


def _default_ephemeral_home(name_lower: str) -> Path:
    user = os.getenv("USER") or os.getenv("USERNAME") or getpass.getuser() or "user"
    return Path(tempfile.gettempdir()) / f"{name_lower}-{user}"


def _candidate_menisik_homes(mode: str) -> list[tuple[Path, Path | None]]:
    """Candidate home dirs as (new, legacy) pairs; legacy is the pre-rename rover path."""
    candidates: list[tuple[Path, Path | None]] = []
    if explicit := _env_raw("MENISIK_HOME"):
        candidates.append((Path(explicit).expanduser(), None))
    if mode == "workspace":
        candidates.append((ROOT / ".menisik", ROOT / ".rover"))
    elif mode == "ephemeral":
        candidates.append((_default_ephemeral_home("menisik"), _default_ephemeral_home("rover")))
    else:
        candidates.append(
            (_default_persistent_home("Menisik", "menisik"), _default_persistent_home("Rover", "rover"))
        )
    candidates.append((Path.home() / ".menisik", Path.home() / ".rover"))
    candidates.append((ROOT / ".menisik", ROOT / ".rover"))
    return candidates


def _adopt_legacy_home(new: Path, legacy: Path) -> Path:
    """Move a pre-rename rover storage dir to its menisik location once.

    If the move fails (permissions, cross-device, locks), keep reading from the
    legacy location and warn instead of splitting state across two dirs.
    """
    if new.exists() or not legacy.is_dir():
        return new
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        legacy.rename(new)
        log.warning("Migrated legacy storage dir %s -> %s", legacy, new)
        return new
    except OSError:
        pass
    try:
        shutil.move(str(legacy), str(new))
        log.warning("Migrated legacy storage dir %s -> %s", legacy, new)
        return new
    except (OSError, shutil.Error):
        log.warning(
            "Found legacy storage dir %s but could not migrate it to %s; "
            "continuing to use the legacy location.",
            legacy,
            new,
        )
        return legacy


def _resolve_menisik_home(mode: str) -> tuple[Path, bool]:
    for candidate, legacy in _candidate_menisik_homes(mode):
        resolved = candidate.expanduser()
        if legacy is not None:
            resolved = _adopt_legacy_home(resolved, legacy.expanduser())
        if _ensure_dir(resolved):
            return resolved.resolve(), True
    fallback = (ROOT / ".menisik").resolve()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback, False


def _resolve_storage_subdir(env_name: str, default_name: str, *, home: Path) -> tuple[Path, bool]:
    if explicit := _env_raw(env_name):
        candidate = Path(explicit).expanduser()
    else:
        candidate = home / default_name
    if _ensure_dir(candidate):
        return candidate.resolve(), True
    fallback = (home / default_name).resolve()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback, False


MENISIK_STORAGE_MODE = _detect_storage_mode()
MENISIK_HOME, MENISIK_HOME_WRITABLE = _resolve_menisik_home(MENISIK_STORAGE_MODE)
MENISIK_STATE_DIR, MENISIK_STATE_DIR_WRITABLE = _resolve_storage_subdir(
    "MENISIK_STATE_DIR", "state", home=MENISIK_HOME
)
MENISIK_CACHE_DIR, MENISIK_CACHE_DIR_WRITABLE = _resolve_storage_subdir(
    "MENISIK_CACHE_DIR", "cache", home=MENISIK_HOME
)
MENISIK_ARTIFACT_DIR, MENISIK_ARTIFACT_DIR_WRITABLE = _resolve_storage_subdir(
    "MENISIK_ARTIFACT_DIR", "artifacts", home=MENISIK_HOME
)
MENISIK_CONFIG_DIR, MENISIK_CONFIG_DIR_WRITABLE = _resolve_storage_subdir(
    "MENISIK_CONFIG_DIR", "config", home=MENISIK_HOME
)
PR_LOG_FILE = MENISIK_STATE_DIR / "pr_log.json"
SECURITY_BLACKLIST_FILE = MENISIK_STATE_DIR / "security_blacklist.json"
PROJECT_BLACKLIST_FILE = MENISIK_STATE_DIR / "project_blacklist.json"

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

# ── OpenRouter / OpenAI-compatible HTTP backend (AI_BACKEND=openrouter) ──
# Works with any OpenAI-compatible chat-completions endpoint: point
# OPENROUTER_BASE_URL at OpenRouter (default), 9router, OpenAI, or a local server.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "").strip()
OPENROUTER_BASE_URL = (
    os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip().rstrip("/")
)


def _default_agent_tool() -> str:
    if AI_BACKEND == "claude":
        return "Claude Code"
    if AI_BACKEND == "openrouter":
        return "OpenRouter"
    return "Codex"


def _default_model_series() -> str:
    if AI_BACKEND == "claude":
        return "Claude"
    if AI_BACKEND == "openrouter":
        model = OPENROUTER_MODEL.lower()
        if "claude" in model:
            return "Claude"
        if "deepseek" in model:
            return "DeepSeek"
        if "gemini" in model:
            return "Gemini"
        if "gpt" in model or "openai" in model:
            return "GPT"
        return "Other"
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
    configured = _env_raw("MENISIK_NOTIFY_TRANSPORT").lower()
    if configured:
        return configured
    if OPENCLAW_NOTIFY_TARGET:
        return "openclaw"
    if TELEGRAM_TOKEN and TELEGRAM_CHAT:
        return "telegram"
    return ""


MENISIK_NOTIFY_TRANSPORT = _default_notify_transport()
MENISIK_NOTIFY_PROGRESS = _env_bool("MENISIK_NOTIFY_PROGRESS", False)
MENISIK_NOTIFY_INTERVAL_SECONDS = _env_int("MENISIK_NOTIFY_INTERVAL_SECONDS", 60, minimum=5)
MENISIK_NOTIFY_STALL_SECONDS = _env_int("MENISIK_NOTIFY_STALL_SECONDS", 300, minimum=0)
MENISIK_NOTIFY_ONLY_ON_CHANGE = _env_bool("MENISIK_NOTIFY_ONLY_ON_CHANGE", True)
MENISIK_NOTIFY_ON_EVENT_TYPES = _env_csv(
    "MENISIK_NOTIFY_ON_EVENT_TYPES",
    ("started", "repo_selected", "stage", "patch_generated", "pr_submitted", "completed", "failed", "stalled"),
)
MENISIK_NOTIFY_MAX_MESSAGE_CHARS = _env_int("MENISIK_NOTIFY_MAX_MESSAGE_CHARS", 3500, minimum=200)
PR_MONITOR_INTERVAL_SECONDS = _env_int("PR_MONITOR_INTERVAL_SECONDS", 0, minimum=0)
TELEGRAM_BOT_ENABLED = _env_bool("TELEGRAM_BOT_ENABLED", False)


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
