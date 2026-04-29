from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.command_router import parse_command_text
from src.doctor import build_doctor_report
from src.pr_generator import (
    build_contribution_report,
    build_repo_inspect_report,
    fetch_repo_candidate_with_scope,
)
from src.state import setup_logging

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - dependency may be absent in local test environments
    FastMCP = None


ROOT = Path(__file__).resolve().parents[2]
_LOG = logging.getLogger("contribution-mcp")


def route_command_payload(text: str) -> dict[str, Any]:
    request = parse_command_text(text)
    return {
        "action": request.action,
        "repo": request.repo,
        "count": request.count,
        "goal": request.goal,
        "dry_run": request.dry_run,
        "first_pr": request.first_pr,
        "confidence": request.confidence,
        "rationale": request.rationale,
        "cli_args": request.to_cli_args(),
    }


def repo_inspect_payload(repo: str) -> dict[str, Any]:
    log = setup_logging()
    candidate = fetch_repo_candidate_with_scope(repo, log, enforce_scope=False)
    return {
        "repo": candidate.full_name,
        "report": build_repo_inspect_report(candidate),
    }


def _run_builder(args: list[str]) -> dict[str, Any]:
    command = [sys.executable, "-m", "app.builder", *args]
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "ok": result.returncode == 0,
    }


def contribution_once_payload(dry_run: bool = True, first_pr: bool = False, goal: str = "bugfix") -> dict[str, Any]:
    args = ["--contrib", "--count", "1", "--goal", goal]
    if first_pr:
        args.append("--first-pr")
    if dry_run:
        args.append("--dry-run")
    return _run_builder(args)


def contribution_targeted_payload(repo: str, dry_run: bool = True, goal: str = "bugfix") -> dict[str, Any]:
    args = ["--contrib", repo, "--count", "1", "--goal", goal]
    if dry_run:
        args.append("--dry-run")
    return _run_builder(args)


def contribution_check_payload() -> dict[str, Any]:
    return _run_builder(["--contrib-check"])


def create_mcp() -> Any:
    if FastMCP is None:
        raise RuntimeError(
            "mcp[cli] is not installed. Install requirements first to run the contribution MCP server."
        )

    mcp = FastMCP(
        name="GitHub Contribution Engine",
        instructions=(
            "Expose safe contribution-engine actions to MCP-compatible agents.\n\n"
            "Primary tools:\n"
            "1. route_command -> map natural language to a canonical action\n"
            "2. doctor -> check environment portability and backend readiness\n"
            "3. contrib_report -> summarize recent engine runs and queue state\n"
            "4. repo_inspect -> inspect one repository before contribution\n"
            "5. contrib_once / contrib_targeted -> run one contribution cycle through the engine\n"
            "6. contrib_check -> poll PR status and maintainer feedback\n\n"
            "Safety: natural-language contribution requests default to dry-run until a caller explicitly disables it."
        ),
    )

    @mcp.tool()
    def doctor() -> dict[str, Any]:
        """Return the operator readiness and portability report for this machine."""
        return {"report": build_doctor_report()}

    @mcp.tool()
    def contrib_report(limit: int = 5) -> dict[str, Any]:
        """Return the latest contribution-engine report and queued opportunity summary."""
        return {"report": build_contribution_report(limit=limit)}

    @mcp.tool()
    def route_command(text: str) -> dict[str, Any]:
        """Map a natural-language request onto a canonical contribution-engine action."""
        return route_command_payload(text)

    @mcp.tool()
    def repo_inspect(repo: str) -> dict[str, Any]:
        """Fetch and summarize one repository without submitting a PR."""
        return repo_inspect_payload(repo)

    @mcp.tool()
    def contrib_once(dry_run: bool = True, first_pr: bool = False, goal: str = "bugfix") -> dict[str, Any]:
        """Run one contribution cycle in search mode through the existing builder CLI."""
        return contribution_once_payload(dry_run=dry_run, first_pr=first_pr, goal=goal)

    @mcp.tool()
    def contrib_targeted(repo: str, dry_run: bool = True, goal: str = "bugfix") -> dict[str, Any]:
        """Run one targeted contribution cycle for a specific repo through the existing builder CLI."""
        return contribution_targeted_payload(repo=repo, dry_run=dry_run, goal=goal)

    @mcp.tool()
    def contrib_check() -> dict[str, Any]:
        """Check open PR status and maintainer feedback through the existing builder CLI."""
        return contribution_check_payload()

    return mcp


def main() -> None:
    mcp = create_mcp()
    mcp.run()


if __name__ == "__main__":
    main()
