from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _normalize_path(value: str | Path) -> str:
    return str(Path(value).expanduser()).replace("\\", "/")


def _quote(value: str) -> str:
    return f'"{value}"' if any(ch.isspace() for ch in value) else value


def _require_absolute_file(path: str, label: str) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path: {path}")
    return _normalize_path(candidate)


def _openclaw_root(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get("OPENCLAW_HOME"):
        return Path(os.environ["OPENCLAW_HOME"]).expanduser()
    return Path.home() / ".openclaw"


def _legacy_openclaw_root(primary_root: Path) -> Path:
    name = primary_root.name
    if name.startswith("."):
        return primary_root.with_name(name[1:])
    return primary_root


def _workspace_skill_root(explicit: str | None = None, openclaw_root: str | None = None) -> Path:
    if explicit:
        root = Path(explicit).expanduser()
        return root if root.name == "skills" else root / "skills"
    if os.environ.get("OPENCLAW_WORKSPACE"):
        return Path(os.environ["OPENCLAW_WORKSPACE"]).expanduser() / "skills"
    return _openclaw_root(openclaw_root) / "workspace" / "skills"


def _shared_skill_root(openclaw_root: str | None = None) -> Path:
    return _openclaw_root(openclaw_root) / "skills"


def _render_skill(name: str, invoke: str) -> str:
    return f"""---
name: {name}
description: Use the local Rover wrapper to inspect repos, read reports, and execute structured contribution workflows from OpenClaw.
metadata: {{"openclaw": {{"requires": {{"bins": ["python3"]}}}}}}
---

## EXECUTION RULES

- Prefer Rover MCP (`mcp.servers.rover`) for channel-driven automation and background work.
- Use this native wrapper only when OpenClaw is operating as a chat assistant instead of an MCP client.
- Execute the wrapper immediately for supported actions.
- If the user message starts with `rover `, treat it as an explicit Rover command surface, not a general chat request.
- For `rover ...` messages, call Rover first and answer from Rover output. Do not do exploratory reasoning, web-style investigation, or alternate tool selection before the Rover call.
- After `/new`, still treat `rover ...` as a built-in command prefix because this skill remains installed outside chat memory.
- Prefer machine-readable output; do not paraphrase success without the wrapper response.
- Map exact Rover prefix commands directly:
  - `rover profile` -> `profile`
  - `rover doctor` -> `doctor`
  - `rover scan security owner/repo` -> `scan --repo owner/repo --kind security`
  - `rover scan bug owner/repo` -> `scan --repo owner/repo --kind bug`
  - `rover scan trust owner/repo` -> `scan --repo owner/repo --kind trust`
  - `rover scan audit owner/repo` -> `scan --repo owner/repo --kind audit`
- For `rover profile`, return Rover profile output only. Never start, suggest, or summarize a contribution run from a profile request.
- If the user message starts with `rover scan`, treat it as an exact scan command. Do not reinterpret it as inspect, report, contribution, issue triage, or PR review.
- For `rover scan ...`, return Rover scan output only. Do not pivot into contribution recommendations, contribution-run suggestions, or "inspect already passed" summaries unless the user explicitly asks for next steps after the scan result.
- For `rover scan ...`, never fall back to repo inspection, PR list fetching, or contribution workflow summaries just because the model is unsure. If routing is uncertain, call Rover and answer from its scan payload.
- For `rover scan trust ...` and `rover scan audit ...`, do not claim that Python or TypeScript files are required. Trust and audit scans can still return repo-level trust signals for low-source or archive-heavy repos.
- Treat `run ...` / `jalankan ...` contribution requests as live submission attempts unless the user explicitly asks for preview or dry-run.
- Treat `preview ...`, `inspect ...`, and `cek repo ... dulu` as non-live actions.
- If a live Rover run is accepted, send one short acknowledgement with the `run_id` and stop there; do not continue with extra status chatter.
- After a live Rover run starts, use only Rover status/result tools for follow-up. Do not improvise with `gh`, manual issue browsing, direct GitHub checks, or sandbox/tooling remediation unless the user explicitly asks for those.
- Do not claim missing GitHub CLI, missing Python, MCP timeout, or similar environment problems if Rover already started a run or returned structured status.
- Do not emit multiple assistant progress messages for the same run. Rover progress updates arrive through its own notification channel/card.
- If Rover returns `accepted=false`, `status=blocked`, or `outcome_code=blocked_ineligible_repo`, treat that as the final answer.
- For blocked runs, echo the Rover reason, `scope_notes`, and `next_steps` only. Do not offer monitoring, retries, scheduling, dependency cleanup, doctor workflows, or any background follow-up.
- Never invent in-progress activity for a blocked run. A blocked run has no dependency install, no fork, no background worker, and no delayed completion.
- Never output placeholders such as `<work_in_progress>` or suggest that a blocked repo is still running.
- Do not suggest `override_limits`, forced targeted runs, or bypassing guardrails unless the user explicitly asks to override limits or force the run.

## Commands

- wrapper invoke: `{invoke}`
- health check: `doctor`
- active profile / auth context: `profile`
- latest contribution report: `contrib_report`
- inspect one repo: `repo_inspect --repo owner/repo`
- security scan: `scan --repo owner/repo --kind security`
- bug scan: `scan --repo owner/repo --kind bug`
- trust scan: `scan --repo owner/repo --kind trust`
- audit scan: `scan --repo owner/repo --kind audit`
- preview one queued contribution: `contrib_once --count 1`
- preview one targeted contribution: `contrib_targeted --repo owner/repo --count 1`
- live one queued contribution: `contrib_once --count 1 --live`
- live one targeted contribution: `contrib_targeted --repo owner/repo --count 1 --live`
- check maintainer feedback / PR state: `contrib_check`
- respond to maintainer feedback: `contrib_respond`
- route natural language safely: `route --text "buat 1 kontribusi"`
- execute natural language request: `message --text "buat 1 kontribusi"`
- explicit Rover prefix examples:
  - `rover profile`
  - `rover doctor`
  - `rover run owner/repo bugfix`
  - `rover scan security owner/repo`
  - `rover scan trust owner/repo`
  - `rover scan owner/repo --kind audit`

## Notes

- Rover is the canonical integration name.
- `github-contribution-engine` is a compatibility alias only.
- For Discord / Telegram / WhatsApp flows, prefer the MCP server so OpenClaw can start a background run and poll status by `run_id`.
- Do not write files into `~/.openclaw/sandboxes/...` unless the path is explicitly confirmed writable.
- If a response does not need a saved artifact, return plain text or JSON instead of creating a file.
- If a file must be created, use a real writable workspace or repo path, not an internal OpenClaw sandbox path.
"""


def _render_wrapper(rover_bin: str) -> str:
    return f"""#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROVER_BIN_CANDIDATES = [
    os.getenv("ROVER_BIN", "").strip(),
    {rover_bin!r},
    shutil.which("rover") or "",
    str(Path.home() / ".local" / "bin" / "rover"),
]


def resolve_rover_bin() -> str:
    for candidate in ROVER_BIN_CANDIDATES:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
        found = shutil.which(candidate)
        if found:
            return found
    raise FileNotFoundError("Rover executable not found. Checked ROVER_BIN, installed wrapper target, PATH, and ~/.local/bin/rover.")


def run_rover(args: list[str]) -> int:
    try:
        rover_bin = resolve_rover_bin()
    except FileNotFoundError as exc:
        sys.stdout.write(json.dumps({{"error": str(exc), "action": "wrapper_error"}}, indent=2) + "\\n")
        return 127
    proc = subprocess.run([rover_bin, *args], capture_output=True, text=True)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr and proc.returncode != 0:
        sys.stderr.write(proc.stderr)
    return proc.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenClaw wrapper for Rover.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor")
    sub.add_parser("profile")
    sub.add_parser("contrib_report")
    sub.add_parser("contrib_check")
    sub.add_parser("contrib_respond")

    repo_inspect = sub.add_parser("repo_inspect")
    repo_inspect.add_argument("--repo", required=True)

    contrib_once = sub.add_parser("contrib_once")
    contrib_once.add_argument("--count", type=int, default=1)
    contrib_once.add_argument("--goal", default="bugfix")
    contrib_once.add_argument("--first-pr", action="store_true")
    contrib_once.add_argument("--live", action="store_true")
    contrib_once.add_argument("--override-limits", action="store_true")

    contrib_targeted = sub.add_parser("contrib_targeted")
    contrib_targeted.add_argument("--repo", required=True)
    contrib_targeted.add_argument("--count", type=int, default=1)
    contrib_targeted.add_argument("--goal", default="bugfix")
    contrib_targeted.add_argument("--first-pr", action="store_true")
    contrib_targeted.add_argument("--live", action="store_true")
    contrib_targeted.add_argument("--override-limits", action="store_true")

    route = sub.add_parser("route")
    route.add_argument("--text", required=True)

    message = sub.add_parser("message")
    message.add_argument("--text", required=True)

    scan = sub.add_parser("scan")
    scan.add_argument("--repo", required=True)
    scan.add_argument("--kind", default="security")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "doctor":
        return run_rover(["doctor", "--json"])
    if args.command == "profile":
        return run_rover(["profile", "--json"])
    if args.command == "contrib_report":
        return run_rover(["report", "--json"])
    if args.command == "contrib_check":
        return run_rover(["check", "--json"])
    if args.command == "contrib_respond":
        return run_rover(["respond", "--json"])
    if args.command == "repo_inspect":
        return run_rover(["inspect", args.repo, "--json"])
    if args.command == "contrib_once":
        payload = ["run", str(args.count), "--goal", args.goal, "--json"]
        if args.first_pr:
            payload.append("--first-pr")
        if not args.live:
            payload.append("--dry-run")
        if args.override_limits:
            payload.append("--override-limits")
        return run_rover(payload)
    if args.command == "contrib_targeted":
        payload = ["run", str(args.count), args.repo, "--goal", args.goal, "--json"]
        if args.first_pr:
            payload.append("--first-pr")
        if not args.live:
            payload.append("--dry-run")
        if args.override_limits:
            payload.append("--override-limits")
        return run_rover(payload)
    if args.command == "route":
        return run_rover(["--command-text", args.text, "--route-only", "--json"])
    if args.command == "message":
        return run_rover(["--command-text", args.text, "--json"])
    if args.command == "scan":
        return run_rover(["scan", args.repo, "--kind", args.kind, "--json"])
    raise SystemExit(f"Unsupported command: {{args.command}}")


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _ensure_openclaw_config(
    root: Path,
    *,
    rover_mcp_bin: str,
    enable_skill: bool,
) -> Path:
    config_path = root / "openclaw.json"
    if config_path.exists():
        raw = config_path.read_text(encoding="utf-8").strip()
        config = json.loads(raw) if raw else {}
    else:
        config = {}

    mcp = config.setdefault("mcp", {})
    servers = mcp.setdefault("servers", {})
    servers["rover"] = {"command": rover_mcp_bin, "args": []}

    if enable_skill:
        skills = config.setdefault("skills", {})
        entries = skills.setdefault("entries", {})
        rover_entry = entries.setdefault("rover", {})
        rover_entry["enabled"] = True

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config_path


def install_openclaw_assets(
    *,
    rover_bin: str,
    python_bin: str,
    rover_mcp_bin: str,
    openclaw_root: str | None = None,
    openclaw_workspace: str | None = None,
    enable_skill: bool = True,
) -> tuple[Path, Path]:
    normalized_rover = _require_absolute_file(rover_bin, "rover_bin")
    _require_absolute_file(python_bin, "python_bin")
    normalized_rover_mcp = _require_absolute_file(rover_mcp_bin, "rover_mcp_bin")

    primary_root = _openclaw_root(openclaw_root)
    workspace_root = _workspace_skill_root(openclaw_workspace, openclaw_root)
    shared_root = _shared_skill_root(openclaw_root)

    canonical_wrapper = primary_root / "tools" / "rover.py"
    legacy_wrapper = primary_root / "tools" / "contribution.py"
    for tool_path in (canonical_wrapper, legacy_wrapper):
        tool_path.parent.mkdir(parents=True, exist_ok=True)
        tool_path.write_text(_render_wrapper(normalized_rover), encoding="utf-8")
        try:
            tool_path.chmod(0o755)
        except OSError:
            pass

    invoke = _quote(_normalize_path(canonical_wrapper))
    canonical_skill_path = workspace_root / "rover" / "SKILL.md"
    for skill_root, skill_name in (
        (workspace_root, "rover"),
        (shared_root, "rover"),
        (workspace_root, "github-contribution-engine"),
        (shared_root, "github-contribution-engine"),
    ):
        skill_dir = skill_root / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_text = _render_skill(skill_name, invoke)
        (skill_dir / "SKILL.md").write_text(skill_text, encoding="utf-8")

    _ensure_openclaw_config(primary_root, rover_mcp_bin=normalized_rover_mcp, enable_skill=enable_skill)
    return canonical_skill_path, canonical_wrapper


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install Rover OpenClaw skill, wrapper, and MCP config.")
    parser.add_argument("--rover-bin", required=True, help="Absolute path to the rover executable.")
    parser.add_argument("--python-bin", required=True, help="Absolute path to the Python interpreter OpenClaw should use.")
    parser.add_argument("--rover-mcp-bin", required=True, help="Absolute path to the rover-mcp executable.")
    parser.add_argument("--openclaw-root", default="", help="Optional override for the OpenClaw home directory.")
    parser.add_argument("--openclaw-workspace", default="", help="Optional override for the OpenClaw workspace directory.")
    parser.add_argument("--disable-skill", action="store_true", help="Do not enable the native Rover skill entry in openclaw.json.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    skill_path, tool_path = install_openclaw_assets(
        rover_bin=args.rover_bin,
        python_bin=args.python_bin,
        rover_mcp_bin=args.rover_mcp_bin,
        openclaw_root=args.openclaw_root or None,
        openclaw_workspace=args.openclaw_workspace or None,
        enable_skill=not args.disable_skill,
    )
    print(f"Installed OpenClaw skill: {skill_path}")
    print(f"Installed OpenClaw wrapper: {tool_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
