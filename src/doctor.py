from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from src.agent_models import get_runtime_profile, iter_agent_tool_support
from src.config import (
    AI_BACKEND,
    CLAUDE_CMD,
    CODEX_CMD,
    ENV_FILE,
    ROOT,
    TELEGRAM_CHAT,
    TELEGRAM_TOKEN,
)


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
    if Path(command).exists():
        return True
    return shutil.which(command) is not None


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
                auth_detail.splitlines()[0] if auth_detail else "unable to determine gh auth state",
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
            "ai-backend",
            "ok" if AI_BACKEND in {"codex", "claude"} else "warn",
            f"configured backend={AI_BACKEND} runtime={runtime.backend}",
        )
    )

    codex_ok = _command_exists(CODEX_CMD)
    claude_ok = _command_exists(CLAUDE_CMD)
    checks.append(
        DoctorCheck(
            "codex-cli",
            "ok" if codex_ok else "warn",
            f"CODEX_CMD={CODEX_CMD!r} {'available' if codex_ok else 'not found'}",
        )
    )
    checks.append(
        DoctorCheck(
            "claude-cli",
            "ok" if claude_ok else "warn",
            f"CLAUDE_CMD={CLAUDE_CMD!r} {'available' if claude_ok else 'not found'}",
        )
    )

    selected_backend_ok = claude_ok if runtime.backend == "claude-cli" else codex_ok
    checks.append(
        DoctorCheck(
            "agent-runtime",
            "ok" if runtime.support_level in {"tested", "supported"} else "warn",
            f"agent_tool={runtime.agent_tool} backend={runtime.backend} support={runtime.support_level}",
        )
    )
    checks.append(
        DoctorCheck(
            "selected-backend",
            "ok" if selected_backend_ok else "fail",
            f"{runtime.backend} {'available' if selected_backend_ok else 'not found'} for the active runtime path",
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

    checks.append(
        DoctorCheck(
            "github-token",
            "ok" if os.getenv("GITHUB_TOKEN", "").strip() else "warn",
            f"GITHUB_TOKEN is {_masked_present(os.getenv('GITHUB_TOKEN', ''))}",
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

    user_specific_path = any(
        marker in (os.getenv(name, "") or "")
        for name in ("CLAUDE_CMD", "CODEX_CMD")
        for marker in ("C:\\Users\\", "/Users/", "/home/")
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
    lines = [
        "Contribution Engine Doctor",
        "==========================",
        "",
    ]
    overall = "ok"
    counts = {"ok": 0, "warn": 0, "fail": 0}
    for check in checks:
        if check.status == "fail":
            overall = "fail"
        elif check.status == "warn" and overall != "fail":
            overall = "warn"
        counts[check.status] = counts.get(check.status, 0) + 1
        lines.append(f"{_status_badge(check.status)} {check.name}: {check.detail}")

    lines.extend(
        [
            "",
            f"Summary: ok={counts.get('ok', 0)} warn={counts.get('warn', 0)} fail={counts.get('fail', 0)}",
            f"Overall: {overall.upper()}",
            f"Active runtime: tool={runtime.agent_tool} backend={runtime.backend} support={runtime.support_level}",
            "",
            "Operator readiness:",
        ]
    )

    gh_ready = any(check.name == "github-auth" and check.status == "ok" for check in checks)
    ai_ready = any(check.name in {"codex-cli", "claude-cli"} and check.status == "ok" for check in checks)
    if gh_ready and ai_ready:
        lines.append("- Ready for full contribution runs, including PR submission.")
    elif ai_ready:
        lines.append("- Ready for local generation and inspect flows, but GitHub auth still needs attention.")
    else:
        lines.append("- Not ready for contribution generation until at least one supported AI CLI is available.")

    actions: list[str] = []
    for check in checks:
        if check.name == "github-cli" and check.status == "fail":
            actions.append("Install GitHub CLI (`gh`) so the engine can open forks and pull requests.")
        elif check.name == "github-auth" and check.status != "ok":
            actions.append("Run `gh auth login` so PR submission and feedback polling can work.")
        elif check.name == "env-file" and check.status != "ok":
            actions.append("Create a local `.env` from `.env.example` before enabling autoruns or notifications.")
        elif check.name == "api-key-only-mode" and check.status != "ok":
            actions.append("API-key-only generation is still a planned feature; use Codex or Claude CLI for now.")
        elif check.name == "portability" and check.status != "ok":
            actions.append("Replace user-specific absolute CLI paths in `.env` with PATH-based commands for portability.")
        elif check.name == "github-token" and check.status != "ok":
            actions.append("Add `GITHUB_TOKEN` if you want the engine to make authenticated GitHub API calls.")

    if actions:
        lines.extend(["", "Recommended actions:"])
        for action in actions:
            lines.append(f"- {action}")

    lines.extend(
        [
            "",
            "Support matrix:",
            "- Codex: tested default CLI path.",
            "- Claude Code: supported fallback CLI path.",
            "- OpenCode/Aider/Cline/Cursor/Windsurf/Other: label-only until a real backend adapter exists.",
            "",
            "Open-source readiness note:",
            "- CLI-based Codex/Claude operation is supported now.",
            "- API-key-only LLM mode is not implemented yet; treat that as a planned portability feature.",
        ]
    )
    return "\n".join(lines)
