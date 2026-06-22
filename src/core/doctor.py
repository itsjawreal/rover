from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from src.core.agent_models import get_runtime_profile, iter_agent_tool_support
from src.core.cli_ui import box_title, bullet_block, key_value_block, table
from src.core.config import (
    AI_BACKEND,
    CLAUDE_CMD,
    CLAUDE_CMD_RESOLUTION,
    CODEX_CMD,
    CODEX_CMD_RESOLUTION,
    ENV_FILE,
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    OPENCLAW_CMD,
    OPENCLAW_CMD_RESOLUTION,
    OPENCLAW_NOTIFY_ACCOUNT,
    OPENCLAW_NOTIFY_CHANNEL,
    OPENCLAW_NOTIFY_TARGET,
    OPENCLAW_NOTIFY_THREAD_ID,
    ROOT,
    ROVER_NOTIFY_INTERVAL_SECONDS,
    ROVER_NOTIFY_ON_EVENT_TYPES,
    ROVER_NOTIFY_PROGRESS,
    ROVER_NOTIFY_STALL_SECONDS,
    ROVER_NOTIFY_TRANSPORT,
    ROVER_ARTIFACT_DIR,
    ROVER_ARTIFACT_DIR_WRITABLE,
    ROVER_CACHE_DIR,
    ROVER_CACHE_DIR_WRITABLE,
    ROVER_CONFIG_DIR,
    ROVER_CONFIG_DIR_WRITABLE,
    ROVER_HOME,
    ROVER_HOME_WRITABLE,
    ROVER_STATE_DIR,
    ROVER_STATE_DIR_WRITABLE,
    ROVER_STORAGE_MODE,
    TELEGRAM_CHAT,
    TELEGRAM_TOKEN,
    _is_unusable_cross_os_cli_path,
)
from src.core.github_auth import github_auth_mode, resolve_github_token

OPENCLAW_ROOT = Path(os.getenv("OPENCLAW_HOME", "~/.openclaw")).expanduser()
OPENCLAW_LEGACY_ROOT = Path("~/openclaw").expanduser()
OPENCLAW_SKILL_CANDIDATES = [
    OPENCLAW_ROOT / "workspace" / "skills" / "rover" / "SKILL.md",
    OPENCLAW_ROOT / "skills" / "rover" / "SKILL.md",
    OPENCLAW_ROOT / "workspace" / "skills" / "github-contribution-engine" / "SKILL.md",
    OPENCLAW_ROOT / "skills" / "github-contribution-engine" / "SKILL.md",
    OPENCLAW_LEGACY_ROOT / "workspace" / "skills" / "rover" / "SKILL.md",
    OPENCLAW_LEGACY_ROOT / "skills" / "rover" / "SKILL.md",
    OPENCLAW_LEGACY_ROOT / "workspace" / "skills" / "github-contribution-engine" / "SKILL.md",
    OPENCLAW_LEGACY_ROOT / "skills" / "github-contribution-engine" / "SKILL.md",
]
OPENCLAW_WRAPPER_CANDIDATES = [
    OPENCLAW_ROOT / "tools" / "rover.py",
    OPENCLAW_ROOT / "tools" / "contribution.py",
    OPENCLAW_LEGACY_ROOT / "tools" / "rover.py",
    OPENCLAW_LEGACY_ROOT / "tools" / "contribution.py",
]
OPENCLAW_CONFIG_PATH = OPENCLAW_ROOT / "openclaw.json"
HERMES_CONFIG_PATH = Path(os.getenv("HERMES_CONFIG_PATH", "~/.hermes/config.yaml")).expanduser()


@dataclass
class DoctorCheck:
    name: str
    status: str
    detail: str


def _status_badge(status: str) -> str:
    return {
        "ok": "[OK]",
        "warn": "[WARN]",
        "fail": "[FAIL]",
    }.get(status.lower(), "[INFO]")


def _command_exists(command: str) -> bool:
    if not command:
        return False
    if _is_unusable_cross_os_cli_path(command):
        return False
    try:
        if Path(command).exists():
            return True
    except OSError:
        pass
    found = shutil.which(command)
    if not found:
        return False
    return not _is_unusable_cross_os_cli_path(found)


def _cli_check_detail(label: str, command: str, ok: bool, resolution) -> str:
    if ok:
        source = {
            "env": f"resolved from {resolution.env_name}",
            "path": "resolved from PATH",
            "fallback": "resolved from a common user-local path",
        }.get(resolution.source, "available")
        if resolution.notes:
            return f"{label}={command!r} available ({source}; {resolution.notes[-1]})"
        return f"{label}={command!r} available ({source})"
    if resolution.notes:
        return f"{label}={command!r} not found ({'; '.join(resolution.notes)})"
    return f"{label}={command!r} not found (checked {resolution.env_name}, PATH, and common user-local paths)"


def _path_is_within(path: Path | None, root: Path) -> bool:
    if path is None:
        return False
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _json_load(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not raw:
        return {}
    import json
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _openclaw_mcp_ready() -> tuple[bool, str]:
    config = _json_load(OPENCLAW_CONFIG_PATH)
    server = (((config.get("mcp") or {}).get("servers") or {}).get("rover") or {})
    command = str(server.get("command") or "").strip()
    if command:
        return True, f"{OPENCLAW_CONFIG_PATH} → mcp.servers.rover"
    return False, f"{OPENCLAW_CONFIG_PATH} missing mcp.servers.rover"


def _hermes_mcp_ready() -> tuple[bool, str]:
    try:
        text = HERMES_CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return False, f"{HERMES_CONFIG_PATH} not found"
    if re.search(r"(?m)^mcp_servers:\s*(?:\n|$)", text) and re.search(r"(?m)^  rover:\s*(?:\n|$)", text):
        return True, f"{HERMES_CONFIG_PATH} → mcp_servers.rover"
    return False, f"{HERMES_CONFIG_PATH} missing mcp_servers.rover"


def _notification_route_check() -> DoctorCheck:
    transport = ROVER_NOTIFY_TRANSPORT.strip().lower()
    if not transport:
        return DoctorCheck("notify-route", "warn", "notifications disabled; no transport configured")
    if transport == "openclaw":
        if not OPENCLAW_NOTIFY_TARGET:
            return DoctorCheck("notify-route", "warn", "transport=openclaw but OPENCLAW_NOTIFY_TARGET is missing")
        detail = (
            f"transport=openclaw channel={OPENCLAW_NOTIFY_CHANNEL} target={OPENCLAW_NOTIFY_TARGET} "
            f"interval={ROVER_NOTIFY_INTERVAL_SECONDS}s progress={'on' if ROVER_NOTIFY_PROGRESS else 'off'} "
            f"stall={ROVER_NOTIFY_STALL_SECONDS}s"
        )
        if OPENCLAW_NOTIFY_ACCOUNT:
            detail += f" account={OPENCLAW_NOTIFY_ACCOUNT}"
        if OPENCLAW_NOTIFY_THREAD_ID:
            detail += f" thread={OPENCLAW_NOTIFY_THREAD_ID}"
        return DoctorCheck("notify-route", "ok", detail)
    if transport == "telegram":
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
            return DoctorCheck("notify-route", "warn", "transport=telegram but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
        return DoctorCheck(
            "notify-route",
            "ok",
            f"transport=telegram chat={TELEGRAM_CHAT} interval={ROVER_NOTIFY_INTERVAL_SECONDS}s progress={'on' if ROVER_NOTIFY_PROGRESS else 'off'} stall={ROVER_NOTIFY_STALL_SECONDS}s",
        )
    return DoctorCheck("notify-route", "warn", f"unknown notification transport: {transport}")


def _notification_transport_check() -> DoctorCheck:
    transport = ROVER_NOTIFY_TRANSPORT.strip().lower()
    events = ",".join(ROVER_NOTIFY_ON_EVENT_TYPES)
    if transport != "openclaw":
        return DoctorCheck("notify-transport", "ok", f"transport={transport or 'disabled'} events={events}")
    openclaw_ok = _command_exists(OPENCLAW_CMD)
    detail = _cli_check_detail("OPENCLAW_CMD", OPENCLAW_CMD, openclaw_ok, OPENCLAW_CMD_RESOLUTION)
    if events:
        detail = f"{detail} | events={events}"
    return DoctorCheck("notify-transport", "ok" if openclaw_ok else "warn", detail)


def _project_venv_dirs() -> list[Path]:
    candidates = [ROOT / ".venv" / "bin", ROOT / ".venv" / "Scripts"]
    resolved: list[Path] = []
    for candidate in candidates:
        try:
            resolved.append(candidate.resolve())
        except OSError:
            resolved.append(candidate)
    return resolved


def _entrypoint_check() -> DoctorCheck:
    argv0 = Path(sys.argv[0]).expanduser()
    try:
        argv0_resolved = argv0.resolve()
    except OSError:
        argv0_resolved = argv0

    python_path = Path(sys.executable).expanduser()
    try:
        python_resolved = python_path.resolve()
    except OSError:
        python_resolved = python_path

    rover_on_path_raw = shutil.which("rover") or ""
    rover_on_path = Path(rover_on_path_raw).expanduser() if rover_on_path_raw else None
    if rover_on_path is not None:
        try:
            rover_on_path = rover_on_path.resolve()
        except OSError:
            pass

    runtime_dir = python_resolved.parent
    project_venv_dirs = _project_venv_dirs()
    looks_local_module = argv0_resolved.name in {"builder.py", "contribute.py"}
    same_runtime = argv0_resolved.parent == runtime_dir
    same_path_runtime = rover_on_path is not None and rover_on_path.parent == runtime_dir
    same_entrypoint = rover_on_path is not None and rover_on_path == argv0_resolved
    project_venv_entrypoint = any(
        _path_is_within(path, candidate)
        for candidate in project_venv_dirs
        for path in (argv0_resolved, rover_on_path)
    )
    status = (
        "ok"
        if (
            looks_local_module
            or same_runtime
            or same_path_runtime
            or same_entrypoint
            or project_venv_entrypoint
        )
        else "warn"
    )
    rover_label = str(rover_on_path) if rover_on_path else "not found on PATH"
    detail = (
        f"argv0={argv0_resolved} | python={python_resolved} | rover={rover_label} | root={ROOT}"
    )
    return DoctorCheck("entrypoint", status, detail)


def _run_command(command: list[str], timeout: int = 20) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "command not found"
    except Exception as exc:
        return False, str(exc)

    output = (result.stdout or result.stderr).strip()
    if result.returncode == 0:
        return True, output or "ok"
    return False, output or f"exit code {result.returncode}"


def _masked_present(value: str) -> str:
    return "set" if value.strip() else "missing"


def _sanitize_cli_status(
    tool_name: str,
    ok: bool,
    detail: str,
    *,
    success_fallback: str,
    login_hint: str,
    missing_fallback: str,
    error_fallback: str,
) -> tuple[bool, str]:
    first_line = (detail or "").splitlines()[0].strip()
    lowered = (detail or "").lower()

    if ok:
        if not first_line or first_line in {"ok", "ready"}:
            return True, success_fallback
        if "file:///" in first_line or ".js:" in first_line or "traceback" in lowered:
            return False, error_fallback
        return True, first_line

    if "command not found" in lowered:
        return False, missing_fallback
    if any(
        marker in lowered
        for marker in (
            "not logged in",
            "login required",
            "auth required",
            "authentication required",
            "run codex login",
            "please login",
        )
    ):
        return False, login_hint
    if "file:///" in lowered or ".js:" in lowered or "traceback" in lowered:
        return False, error_fallback
    if first_line:
        return False, first_line
    return False, error_fallback


def _codex_auth_ready() -> tuple[bool, str]:
    if os.getenv("OPENAI_API_KEY", "").strip():
        return True, "OPENAI_API_KEY is set"
    if not _command_exists(CODEX_CMD):
        return False, "Codex CLI not installed"
    ok, detail = _run_command([CODEX_CMD, "login", "status"])
    return _sanitize_cli_status(
        "Codex",
        ok,
        detail,
        success_fallback="Codex login is active",
        login_hint="Codex login is not active; run `codex login`",
        missing_fallback="Codex CLI not installed",
        error_fallback="Codex CLI returned an unexpected auth error; run `codex login status` manually",
    )


def collect_doctor_checks() -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    runtime = get_runtime_profile()

    checks.append(
        DoctorCheck(
            "python",
            "ok" if sys.version_info >= (3, 10) else "warn",
            f"running Python {sys.version.split()[0]}",
        )
    )

    checks.append(
        DoctorCheck(
            "workspace",
            "ok" if ROOT.exists() else "fail",
            f"root={ROOT}",
        )
    )
    checks.append(_entrypoint_check())

    git_ok = shutil.which("git") is not None
    checks.append(
        DoctorCheck(
            "git",
            "ok" if git_ok else "fail",
            "git found on PATH" if git_ok else "git is required but not on PATH",
        )
    )

    gh_ok = shutil.which("gh") is not None
    checks.append(
        DoctorCheck(
            "github-cli",
            "ok" if gh_ok else "fail",
            "gh found on PATH" if gh_ok else "gh CLI is required for fork/PR operations",
        )
    )
    if gh_ok:
        auth_ok, auth_detail = _run_command(["gh", "auth", "status"])
        checks.append(
            DoctorCheck(
                "github-auth",
                "ok" if auth_ok else "warn",
                auth_detail.splitlines()[0] if auth_detail else "no stored gh auth session detected",
            )
        )

    checks.append(
        DoctorCheck(
            "env-file",
            "ok" if ENV_FILE.exists() else "warn",
            ".env found" if ENV_FILE.exists() else ".env missing; defaults will be used",
        )
    )
    checks.append(
        DoctorCheck(
            "storage-mode",
            "ok",
            f"mode={ROVER_STORAGE_MODE} home={ROVER_HOME}",
        )
    )
    checks.append(
        DoctorCheck(
            "storage-state",
            "ok" if ROVER_STATE_DIR_WRITABLE else "warn",
            f"state={ROVER_STATE_DIR}",
        )
    )
    checks.append(
        DoctorCheck(
            "storage-cache",
            "ok" if ROVER_CACHE_DIR_WRITABLE else "warn",
            f"cache={ROVER_CACHE_DIR}",
        )
    )
    checks.append(
        DoctorCheck(
            "storage-artifacts",
            "ok" if ROVER_ARTIFACT_DIR_WRITABLE else "warn",
            f"artifacts={ROVER_ARTIFACT_DIR}",
        )
    )
    checks.append(
        DoctorCheck(
            "storage-config",
            "ok" if ROVER_CONFIG_DIR_WRITABLE and ROVER_HOME_WRITABLE else "warn",
            f"config={ROVER_CONFIG_DIR}",
        )
    )

    checks.append(
        DoctorCheck(
            "ai-backend",
            "ok" if AI_BACKEND in {"codex", "claude", "openrouter"} else "warn",
            f"configured backend={AI_BACKEND} runtime={runtime.backend}",
        )
    )

    if AI_BACKEND == "openrouter":
        from src.core.config import OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_BASE_URL
        if OPENROUTER_API_KEY and OPENROUTER_MODEL:
            or_status, or_detail = "ok", f"key set, model={OPENROUTER_MODEL}, base={OPENROUTER_BASE_URL}"
        elif OPENROUTER_API_KEY:
            or_status, or_detail = "warn", "OPENROUTER_API_KEY set but OPENROUTER_MODEL is empty"
        else:
            or_status, or_detail = "fail", "OPENROUTER_API_KEY is not set"
        checks.append(DoctorCheck("openrouter-api", or_status, or_detail))

    codex_ok = _command_exists(CODEX_CMD)
    claude_ok = _command_exists(CLAUDE_CMD)
    checks.append(
        DoctorCheck(
            "codex-cli",
            "ok" if codex_ok else "warn",
            _cli_check_detail("CODEX_CMD", CODEX_CMD, codex_ok, CODEX_CMD_RESOLUTION),
        )
    )
    checks.append(
        DoctorCheck(
            "claude-cli",
            "ok" if claude_ok else "warn",
            _cli_check_detail("CLAUDE_CMD", CLAUDE_CMD, claude_ok, CLAUDE_CMD_RESOLUTION),
        )
    )

    openrouter_ok = bool(OPENROUTER_API_KEY and OPENROUTER_MODEL)
    if runtime.backend == "openrouter-api":
        selected_backend_ok = openrouter_ok
    elif runtime.backend == "claude-cli":
        selected_backend_ok = claude_ok
    else:
        selected_backend_ok = codex_ok
    codex_auth_ok, codex_auth_detail = _codex_auth_ready()
    checks.append(
        DoctorCheck(
            "codex-auth",
            "ok" if codex_auth_ok else "warn",
            codex_auth_detail,
        )
    )
    checks.append(
        DoctorCheck(
            "agent-runtime",
            "ok" if runtime.support_level in {"tested", "supported"} else "warn",
            f"agent_tool={runtime.agent_tool} backend={runtime.backend} support={runtime.support_level}",
        )
    )
    selected_backend_auth_ok = True
    selected_backend_auth_detail = "selected backend auth looks ready"
    if runtime.backend == "codex-cli":
        selected_backend_auth_ok = codex_auth_ok
        selected_backend_auth_detail = codex_auth_detail
    checks.append(
        DoctorCheck(
            "selected-backend",
            "ok" if selected_backend_ok else "fail",
            f"{runtime.backend} {'available' if selected_backend_ok else 'not found'} for the active runtime path",
        )
    )
    checks.append(
        DoctorCheck(
            "selected-backend-auth",
            "ok" if selected_backend_auth_ok else "warn",
            selected_backend_auth_detail,
        )
    )

    if AI_BACKEND == "codex" and not codex_ok:
        fallback_status = "ok" if claude_ok else "fail"
        fallback_detail = (
            "Codex missing, but Claude CLI fallback is available."
            if claude_ok
            else "Codex missing and no Claude CLI fallback found."
        )
        checks.append(DoctorCheck("ai-fallback", fallback_status, fallback_detail))
    elif AI_BACKEND == "claude" and not claude_ok:
        fallback_status = "ok" if codex_ok else "fail"
        fallback_detail = (
            "Claude missing, but Codex CLI is available."
            if codex_ok
            else "Claude missing and no Codex CLI fallback found."
        )
        checks.append(DoctorCheck("ai-fallback", fallback_status, fallback_detail))

    api_key_only = any(
        os.getenv(name, "").strip()
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")
    )
    if api_key_only and not (codex_ok or claude_ok):
        checks.append(
            DoctorCheck(
                "api-key-only-mode",
                "warn",
                "API key detected but this repo does not yet support API-key-only generation without a CLI adapter.",
            )
        )

    github_token = resolve_github_token()
    auth_mode = github_auth_mode()
    auth_detail = "no GitHub token available from environment or gh auth"
    if auth_mode == "gh-token-env":
        auth_detail = f"GH_TOKEN is {_masked_present(os.getenv('GH_TOKEN', ''))}"
    elif auth_mode == "github-token-env":
        auth_detail = f"GITHUB_TOKEN is {_masked_present(os.getenv('GITHUB_TOKEN', ''))}"
    elif auth_mode == "gh-auth":
        auth_detail = "GitHub API auth is available via `gh auth token`"
    checks.append(
        DoctorCheck(
            "github-token",
            "ok" if github_token else "warn",
            auth_detail,
        )
    )

    telegram_ready = bool(TELEGRAM_TOKEN.strip() and TELEGRAM_CHAT.strip())
    checks.append(
        DoctorCheck(
            "telegram",
            "ok" if telegram_ready else "warn",
            "Telegram notifications configured" if telegram_ready else "Telegram notifications disabled or incomplete",
        )
    )
    checks.append(_notification_route_check())
    checks.append(_notification_transport_check())

    openclaw_skill_path = next((str(path) for path in OPENCLAW_SKILL_CANDIDATES if path.exists()), "")
    checks.append(
        DoctorCheck(
            "openclaw-skill",
            "ok" if openclaw_skill_path else "warn",
            openclaw_skill_path or "OpenClaw skill not installed for rover",
        )
    )
    openclaw_wrapper_path = next((str(path) for path in OPENCLAW_WRAPPER_CANDIDATES if path.exists()), "")
    checks.append(
        DoctorCheck(
            "openclaw-wrapper",
            "ok" if openclaw_wrapper_path else "warn",
            openclaw_wrapper_path or "OpenClaw wrapper not installed under ~/.openclaw/tools",
        )
    )
    openclaw_mcp_ok, openclaw_mcp_detail = _openclaw_mcp_ready()
    checks.append(DoctorCheck("openclaw-mcp", "ok" if openclaw_mcp_ok else "warn", openclaw_mcp_detail))
    hermes_mcp_ok, hermes_mcp_detail = _hermes_mcp_ready()
    checks.append(DoctorCheck("hermes-mcp", "ok" if hermes_mcp_ok else "warn", hermes_mcp_detail))
    legacy_details: list[str] = []
    if any(_path_is_within(path, OPENCLAW_LEGACY_ROOT) for path in OPENCLAW_SKILL_CANDIDATES if path.exists()):
        legacy_details.append("legacy skill under ~/openclaw")
    if any(_path_is_within(path, OPENCLAW_LEGACY_ROOT) for path in OPENCLAW_WRAPPER_CANDIDATES if path.exists()):
        legacy_details.append("legacy wrapper under ~/openclaw")
    checks.append(
        DoctorCheck(
            "openclaw-legacy",
            "warn" if legacy_details else "ok",
            ", ".join(legacy_details) if legacy_details else "no stale ~/openclaw install detected",
        )
    )

    user_specific_path = any(
        resolution.source == "env"
        and resolution.env_value.startswith("/home/")
        for resolution in (CLAUDE_CMD_RESOLUTION, CODEX_CMD_RESOLUTION)
    )
    if user_specific_path:
        checks.append(
            DoctorCheck(
                "portability",
                "warn",
                "CLI command env vars contain a user-specific absolute path; PATH-based commands are more portable.",
            )
        )

    return checks


def build_doctor_report() -> str:
    checks = collect_doctor_checks()
    runtime = get_runtime_profile()
    lines = [box_title("Contribution Engine Doctor"), ""]
    overall = "ok"
    counts = {"ok": 0, "warn": 0, "fail": 0}
    check_rows: list[list[object]] = []
    for check in checks:
        if check.status == "fail":
            overall = "fail"
        elif check.status == "warn" and overall != "fail":
            overall = "warn"
        counts[check.status] = counts.get(check.status, 0) + 1
        check_rows.append([_status_badge(check.status), check.name, check.detail])

    lines.append(table("Checks", ["Status", "Check", "Detail"], check_rows))
    lines.extend(
        [
            "",
            "Summary:",
            key_value_block(
                "Summary",
                [
                    ("OK", counts.get("ok", 0)),
                    ("Warn", counts.get("warn", 0)),
                    ("Fail", counts.get("fail", 0)),
                    ("Overall", overall.upper()),
                    ("Active runtime", f"tool={runtime.agent_tool} backend={runtime.backend} support={runtime.support_level}"),
                ],
            ),
            "",
            "Operator readiness:",
        ]
    )

    gh_ready = (
        any(check.name == "github-cli" and check.status == "ok" for check in checks)
        and any(check.name == "github-token" and check.status == "ok" for check in checks)
    )
    ai_ready = any(check.name in {"codex-cli", "claude-cli", "openrouter-api"} and check.status == "ok" for check in checks)
    selected_auth_ready = any(check.name == "selected-backend-auth" and check.status == "ok" for check in checks)
    if gh_ready and ai_ready and selected_auth_ready:
        readiness = ["Ready for full contribution runs, including PR submission."]
    elif ai_ready and selected_auth_ready:
        readiness = ["Ready for local generation and inspect flows, but GitHub auth still needs attention."]
    elif ai_ready:
        readiness = ["Backend CLI is installed, but authentication is incomplete for the active runtime."]
    else:
        readiness = ["Not ready for contribution generation until at least one supported AI CLI is available."]
    lines.append(bullet_block("Operator readiness", readiness))

    actions: list[str] = []
    for check in checks:
        if check.name == "github-cli" and check.status == "fail":
            actions.append("Install GitHub CLI (`gh`) so the engine can open forks and pull requests.")
        elif check.name == "github-auth" and check.status != "ok":
            actions.append("Stored `gh auth login` session not found; token-only mode is still fine if `GH_TOKEN` or `GITHUB_TOKEN` is set.")
        elif check.name == "env-file" and check.status != "ok":
            actions.append("Create a local `.env` from `.env.example` before enabling autoruns or notifications.")
        elif check.name == "api-key-only-mode" and check.status != "ok":
            actions.append("API-key-only generation is still a planned feature; use Codex or Claude CLI for now.")
        elif check.name == "portability" and check.status != "ok":
            actions.append("Replace user-specific absolute CLI paths in `.env` with PATH-based commands for portability.")
        elif check.name == "entrypoint" and check.status != "ok":
            actions.append(
                "Active rover entrypoint may be stale for this Python environment; reinstall with `python -m pip install -e .` and refresh your shell command cache."
            )
        elif check.name.startswith("storage-") and check.status != "ok":
            actions.append(
                "Check Rover local storage permissions or override `ROVER_HOME` / `ROVER_STATE_DIR` to a writable user-local path."
            )
        elif check.name == "github-token" and check.status != "ok":
            actions.append("Add `GH_TOKEN` or `GITHUB_TOKEN`, or run `gh auth login`, so the engine can make authenticated GitHub API calls.")
        elif check.name == "codex-auth" and check.status != "ok":
            actions.append("Finish Codex authentication with `codex login --device-auth`, `codex login`, or set `OPENAI_API_KEY`.")
        elif check.name == "openclaw-skill" and check.status != "ok":
            actions.append("Install the canonical Rover OpenClaw skill under `~/.openclaw/workspace/skills/rover` or `~/.openclaw/skills/rover`.")
        elif check.name == "openclaw-wrapper" and check.status != "ok":
            actions.append("Install the Rover OpenClaw wrapper at `~/.openclaw/tools/rover.py` so native skill commands can execute.")
        elif check.name == "openclaw-mcp" and check.status != "ok":
            actions.append("Upsert `mcp.servers.rover` in `~/.openclaw/openclaw.json` so OpenClaw can call Rover over MCP.")
        elif check.name == "hermes-mcp" and check.status != "ok":
            actions.append("Add `mcp_servers.rover` to `~/.hermes/config.yaml` so Hermes can call Rover over MCP.")
        elif check.name == "openclaw-legacy" and check.status != "ok":
            actions.append("Treat `~/openclaw` as legacy only; move active skills and wrappers to `~/.openclaw`.")

    if actions:
        lines.extend(["", bullet_block("Recommended actions", actions)])

    lines.extend(
        [
            "",
            "Support matrix:",
            table(
                "Support matrix",
                ["Tool family", "Support level", "Notes"],
                [
                    ["Codex", "tested", "default CLI path"],
                    ["Claude Code", "supported", "fallback CLI path"],
                    ["OpenCode/Aider/Cline/Cursor/Windsurf/Other", "label-only", "real backend adapter not installed"],
                ],
            ),
            "",
            bullet_block(
                "Open-source readiness note",
                [
                    "CLI-based Codex/Claude operation is supported now.",
                    "API-key-only LLM mode is not implemented yet; treat that as a planned portability feature.",
                ],
            ),
            "",
            "OpenClaw integration note:",
            bullet_block(
                "OpenClaw integration note",
                [
                    "Canonical OpenClaw paths are `~/.openclaw/workspace/skills/rover/SKILL.md`, `~/.openclaw/skills/rover/SKILL.md`, and `~/.openclaw/tools/rover.py`.",
                    "Primary automation path is `mcp.servers.rover` in `~/.openclaw/openclaw.json`; the native skill is optional compatibility/UX.",
                    "`github-contribution-engine` and `~/openclaw` are compatibility surfaces only.",
                ],
            ),
        ]
    )
    return "\n".join(lines)
