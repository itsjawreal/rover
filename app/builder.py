#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import difflib
import io
import json
import logging
import os
import re
import sys
from pathlib import Path
from textwrap import dedent

from src.core.ai import get_usage, reset_usage
from src.core.command_router import parse_command_text
from src.core.config import DRY_RUN
from src.contrib.contribution_store import PREngineStore
from src.core.doctor import build_doctor_report, collect_doctor_checks
from src.github.fork import (
    ForkError,
    PRAlreadyExistsError,
    fix_and_push_own_repo,
    fork_and_submit_pr,
    get_current_github_login,
)
from src.core.notify import notify
from src.contrib.pr_generator import (
    _PR_TARGETED_ALLOW_BROAD,
    PRGeneratorError,
    build_contribution_report,
    build_repo_inspect_report,
    fetch_repo_metadata,
    get_repo_inspect_data,
    can_submit_contribution_to_repo,
    check_all_prs,
    check_pr_feedback,
    get_contribution_report_data,
    fetch_repo_candidate,
    fetch_repo_candidate_with_scope,
    find_pr_target,
    finish_pr_engine_run,
    generate_pr_improvement,
    get_pr_submitted_repos,
    resolve_repo_full_name,
    save_pr_log,
    _set_run_stage,
    start_pr_engine_run,
    write_repo_inspect_artifact,
)
from src.github.scraper import ScraperError
from src.contrib.validator import run_sandbox_validation
from src.core.state import cleanup_old_logs, get_security_blacklisted_sources, setup_logging


def _preferred_user_bin(name: str) -> str | None:
    candidate = (Path.home() / ".local" / "bin" / name).expanduser()
    if candidate.exists():
        return str(candidate)
    return None


def _known_open_pr(repo_full_name: str) -> dict | None:
    try:
        return PREngineStore().find_open_pr(repo_full_name)
    except Exception:
        return None


def collect_profile_payload() -> dict[str, object]:
    github_owner_env = os.getenv("GITHUB_OWNER", "").strip()
    token_present = bool(os.getenv("GH_TOKEN", "").strip() or os.getenv("GITHUB_TOKEN", "").strip())
    try:
        github_login = get_current_github_login().strip()
        github_authenticated = True
        auth_error = ""
    except Exception as exc:
        github_login = ""
        github_authenticated = False
        auth_error = str(exc)
    return {
        "action": "profile",
        "github_login": github_login,
        "github_authenticated": github_authenticated,
        "github_owner_env": github_owner_env,
        "token_present": token_present,
        "auth_error": auth_error,
        "rendered": build_profile_report(
            github_login=github_login,
            github_authenticated=github_authenticated,
            github_owner_env=github_owner_env,
            token_present=token_present,
            auth_error=auth_error,
        ),
    }


def build_profile_report(
    *,
    github_login: str,
    github_authenticated: bool,
    github_owner_env: str,
    token_present: bool,
    auth_error: str,
) -> str:
    lines = [
        "Rover Profile",
        "=============",
        "",
        f"GitHub login: {github_login or '-'}",
        f"Authenticated: {'yes' if github_authenticated else 'no'}",
        f"GITHUB_OWNER: {github_owner_env or '-'}",
        f"Token present: {'yes' if token_present else 'no'}",
    ]
    if auth_error:
        lines.extend(["", f"Auth error: {auth_error}"])
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.builder",
        description=dedent(
            """\
            Rover — automated open-source PR bot.

            Searches GitHub for active repos, scans for real bugs and missing fixes,
            generates minimal patches using AI, and submits pull requests.
            Tracks every PR lifecycle (open → merged/closed) in a local SQLite database.
            """
        ),
        epilog=dedent(
            """\
            ── QUICK START ──────────────────────────────────────────────────
              1. Copy .env.example to .env and choose GitHub auth + AI backend
              2. rover doctor                             # verify setup
              3. rover run 1                             # submit 1 PR (auto-search)
              4. rover check                             # check PR status next day

            ── COMMON WORKFLOWS ─────────────────────────────────────────────
              # Search mode — engine picks the best target automatically
              rover run 1
              rover run 3                                 # submit 3 PRs
              rover run --first-pr                        # prefer beginner-friendly repos

              # Targeted mode — pin a specific repo
              rover owner/repo
              rover run 1 owner/repo --goal feature_upgrade

              # Dry run — generate patch, skip submission
              rover run --dry-run
              rover run owner/repo --override-limits       # bypass .env contribution limits

              # Inspect a repo without submitting
              rover inspect owner/repo

              # PR lifecycle
              rover check                # poll open PRs + handle feedback
              python -m app.builder --contrib-respond   # low-level maintainer response path
              rover report               # show run history + queued opportunities

              # Diagnostics
              rover doctor

            ── KEY ENV VARS (.env) ──────────────────────────────────────────
              GH_TOKEN / GITHUB_TOKEN  Optional PAT for token-based GitHub auth
              GITHUB_OWNER          Your GitHub username (required)
              AI_BACKEND            claude | codex  (default: codex)
              CLAUDE_CMD            Path to claude CLI binary
              CODEX_CMD             Path to codex CLI binary
              CONTRIB_LANE          Search lane: general | crypto | devtools | ml | ...
              CONTRIB_TOPIC_KEYWORDS  Comma-separated topic keywords to filter repos
              PR_MIN_STARS / PR_MAX_STARS  Repo star range filter (default: 300–6000)
              PR_MAX_PUSHED_DAYS    Skip repos inactive longer than N days (default: 45)

            ── DEPRECATION POLICY ───────────────────────────────────────────
              rover-engine, --pr, --pr-check, and --pr-respond are deprecated.
              Warning period: 0.1.x
              Planned removal: 0.2.0 or next intentionally breaking CLI release.
            """
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=(
            "rover 0.1.0\n"
            f"root={Path(__file__).resolve().parents[1]}\n"
            f"python={sys.executable}"
        ),
    )

    # ── Contribution actions ──────────────────────────────────
    action = parser.add_argument_group("contribution actions")
    action.add_argument(
        "--contrib", "--pr",
        nargs="?", const="", default=None,
        metavar="OWNER/REPO",
        help=(
            "Run the contribution engine. Without a repo argument, the engine searches "
            "GitHub automatically based on CONTRIB_LANE and topic keywords in .env. "
            "Pass owner/repo or a GitHub URL to target a specific repo."
        ),
    )
    action.add_argument(
        "--contrib-check", "--pr-check",
        action="store_true",
        help="Poll all open PRs for status changes (merged/closed) and fetch maintainer feedback.",
    )
    action.add_argument(
        "--contrib-respond", "--pr-respond",
        action="store_true",
        help="Fetch and handle maintainer comments on open PRs only (skips status polling).",
    )
    action.add_argument(
        "--contrib-report",
        action="store_true",
        help="Print a summary of recent engine runs, queued opportunities, and top rejection reasons.",
    )
    action.add_argument(
        "--repo-inspect",
        metavar="OWNER/REPO",
        help="Fetch and analyze a repo without generating or submitting a PR. Good for debugging target selection.",
    )
    action.add_argument(
        "--scan-repo",
        metavar="OWNER/REPO",
        help="Run a deterministic repo scan and return evidence-backed findings without modifying the repo.",
    )
    action.add_argument(
        "--scan-kind",
        choices=("security", "bug", "trust", "audit"),
        default="security",
        help="Scanner profile to use with --scan-repo (default: security).",
    )
    action.add_argument(
        "--cached",
        action="store_true",
        help="For repo inspect only: use the latest local inspect snapshot without refreshing from GitHub.",
    )
    action.add_argument(
        "--refresh",
        action="store_true",
        help="For repo inspect only: force a fresh inspect even if the local snapshot still looks current.",
    )
    action.add_argument(
        "--doctor",
        action="store_true",
        help="Check that all required tools (gh, git, AI backend) are installed and configured correctly.",
    )
    action.add_argument(
        "--profile",
        action="store_true",
        help="Show the currently active GitHub login and related operator identity details.",
    )
    action.add_argument(
        "--test-notify",
        action="store_true",
        help="Send a test notification to verify Telegram credentials are working.",
    )

    # ── Contribution options ──────────────────────────────────
    opts = parser.add_argument_group("contribution options")
    opts.add_argument(
        "--goal",
        type=_goal_type,
        default="bugfix",
        metavar="{bugfix,dep_update,feature_upgrade,feature_add}",
        help=(
            "Type of contribution to generate. "
            "'bugfix' targets real failure modes (default). "
            "'dep_update' updates pinned dependencies with local verification. "
            "'feature_upgrade' implements a maintainer-signaled TODO. "
            "'feature_add' requires a backing GitHub issue. "
            "Aliases accepted: upgrade, fix, add, upgrade_feature, etc."
        ),
    )
    opts.add_argument(
        "--count",
        type=int, default=None,
        metavar="N",
        help=(
            "Number of PRs to submit in one run (default: 1). "
            "Shorthand: --1, --2, --3, etc. also work."
        ),
    )
    opts.add_argument(
        "--first-pr",
        action="store_true",
        help=(
            "Prefer smaller, well-tested repos with responsive maintainers. "
            "Useful when building a new contribution history."
        ),
    )
    opts.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and validate the patch locally but do not fork or submit a PR. Useful for testing.",
    )
    opts.add_argument(
        "--override-limits",
        action="store_true",
        help=(
            "Bypass .env contribution limits for this run, including lane, star/fork/issue, "
            "activity, file-surface, and first-PR filters. Safety gates still apply."
        ),
    )
    approval = opts.add_mutually_exclusive_group()
    approval.add_argument(
        "--human-approval",
        dest="human_approval",
        action="store_true",
        default=None,
        help="Pause before PR submission and ask the operator to submit, queue, or reject the patch.",
    )
    approval.add_argument(
        "--no-human-approval",
        dest="human_approval",
        action="store_false",
        help="Disable the pre-submit human approval gate for this run.",
    )

    # ── Backend selection ─────────────────────────────────────
    backend = parser.add_argument_group("AI backend (overrides AI_BACKEND in .env)")
    backend.add_argument(
        "--claude",
        action="store_true",
        help="Use Claude CLI as the AI backend for this run.",
    )
    backend.add_argument(
        "--codex",
        action="store_true",
        help="Use Codex CLI as the AI backend for this run.",
    )

    # ── Advanced ──────────────────────────────────────────────
    advanced = parser.add_argument_group("advanced")
    advanced.add_argument(
        "--command-text",
        default="",
        metavar="TEXT",
        help=(
            "Pass a natural-language instruction that gets mapped to a CLI action automatically. "
            "Example: --command-text 'inspect owner/repo' or 'run 2 PRs dry run'."
        ),
    )
    advanced.add_argument(
        "--route-only",
        action="store_true",
        help="Map --command-text to a canonical Rover action and exit without executing it.",
    )
    advanced.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of styled terminal output for supported commands.",
    )
    advanced.add_argument(
        "--external-run-id",
        default="",
        help=argparse.SUPPRESS,
    )

    # ── Integrations ──────────────────────────────────────────
    integ = parser.add_argument_group("integrations")
    integ.add_argument(
        "--install-openclaw",
        action="store_true",
        help=(
            "Install (or reinstall) the canonical Rover OpenClaw skill, wrapper, and mcp.servers.rover entry under ~/.openclaw."
        ),
    )
    integ.add_argument(
        "--install-hermes",
        action="store_true",
        help="Install or update ~/.hermes/config.yaml with mcp_servers.rover.",
    )
    integ.add_argument(
        "--install-mcp",
        action="store_true",
        help=(
            "Generate .mcp.json for this machine. "
            "Claude Code auto-discovers this file and spawns the MCP server on session start. "
            "Re-run after moving the project to a new path or distro."
        ),
    )

    # ── PR listing ────────────────────────────────────────────
    listing = parser.add_argument_group("PR listing")
    listing.add_argument(
        "--list-prs",
        nargs="?",
        const="all",
        default=None,
        metavar="{all,open,merged,closed}",
        help=(
            "Print a table of submitted PRs from the local database. "
            "Filter by status: open, merged, or closed. Defaults to all. "
            "Example: --list-prs open"
        ),
    )

    return parser


def _legacy_count_arg(argv: list[str]) -> int | None:
    for arg in argv:
        match = re.match(r"^--(\d+)$", arg)
        if match:
            return int(match.group(1))
    return None


_COMMON_MISTAKES: dict[str, str] = {
    "--bugfix":           "--goal bugfix",
    "--feature_upgrade":  "--goal feature_upgrade",
    "--upgrade_feature":  "--goal feature_upgrade",
    "--feature-upgrade":  "--goal feature_upgrade",
    "--feature_add":      "--goal feature_add",
    "--add_feature":      "--goal feature_add",
    "--check":            "--contrib-check",
    "--pr-check":         "--contrib-check",
    "--report":           "--contrib-report",
    "--respond":          "--contrib-respond",
    "--pr-respond":       "--contrib-respond",
    "--inspect":          "--repo-inspect <owner/repo>",
    "--pr":               "--contrib",
    "--dryrun":           "--dry-run",
    "--dry_run":          "--dry-run",
    "--force":            "--override-limits",
    "--force-run":        "--override-limits",
    "--first_pr":         "--first-pr",
    "--count":            "--count N  (or shorthand --1, --2, ...)",
}

_VALID_FLAGS = [
    "--contrib", "--contrib-check", "--contrib-respond", "--contrib-report",
    "--repo-inspect", "--doctor", "--goal", "--count", "--first-pr", "--dry-run", "--override-limits",
    "--claude", "--codex", "--command-text", "--route-only", "--json", "--install-openclaw", "--install-hermes", "--install-mcp",
    "--list-prs",
]

_GOAL_ALIASES: dict[str, str] = {
    "bugfix":          "bugfix",
    "bug":             "bugfix",
    "fix":             "bugfix",
    "feature_upgrade": "feature_upgrade",
    "upgrade_feature": "feature_upgrade",
    "upgrade":         "feature_upgrade",
    "feature-upgrade": "feature_upgrade",
    "feature_add":     "feature_add",
    "add_feature":     "feature_add",
    "add":             "feature_add",
    "feature-add":     "feature_add",
    "feature":         "feature_add",
    "dep_update":      "dep_update",
    "dependency":      "dep_update",
    "deps":            "dep_update",
    "dependencies":    "dep_update",
    "update-deps":     "dep_update",
    "update_deps":     "dep_update",
}


def _goal_type(value: str) -> str:
    key = value.strip().lower()
    if key in _GOAL_ALIASES:
        return _GOAL_ALIASES[key]
    valid = ["bugfix", "feature_upgrade", "feature_add", "dep_update"]
    close = difflib.get_close_matches(key, valid, n=1, cutoff=0.5)
    hint = f" — did you mean '{close[0]}'?" if close else ""
    raise argparse.ArgumentTypeError(
        f"invalid goal '{value}'{hint}\n  Valid options: {', '.join(valid)}"
    )


def _suggest(bad_arg: str) -> str:
    key = bad_arg.split("=")[0].lower()
    if re.match(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$", bad_arg):
        return f"  Did you mean: --contrib {bad_arg} --goal ..."
    if key in _COMMON_MISTAKES:
        return f"  Did you mean: {_COMMON_MISTAKES[key]}"
    close = difflib.get_close_matches(key, _VALID_FLAGS, n=1, cutoff=0.6)
    if close:
        return f"  Did you mean: {close[0]}"
    return ""


# ── Progress output ───────────────────────────────────────────
def _status(msg: str) -> None:
    print(msg, flush=True)


_DEPRECATED_FLAG_HINTS: dict[str, str] = {
    "--pr": "--contrib",
    "--pr-check": "--contrib-check",
    "--pr-respond": "--contrib-respond",
}


def _format_modern_contrib_command(args: argparse.Namespace) -> str:
    parts: list[str] = ["rover", "run"]
    if args.count:
        parts.append(str(args.count))
    if args.contrib:
        parts.append(str(args.contrib))
    if args.goal and args.goal != "bugfix":
        parts.extend(["--goal", str(args.goal)])
    if args.first_pr:
        parts.append("--first-pr")
    if args.dry_run:
        parts.append("--dry-run")
    if getattr(args, "override_limits", False):
        parts.append("--override-limits")
    return " ".join(parts)


def _suggest_modern_command(args: argparse.Namespace) -> str:
    if args.doctor:
        return "rover doctor"
    if args.contrib_check:
        return "rover check"
    if args.contrib_report:
        return "rover report"
    if args.repo_inspect:
        return f"rover inspect {args.repo_inspect}"
    if args.contrib_respond:
        return "python -m app.builder --contrib-respond"
    if args.contrib is not None:
        return _format_modern_contrib_command(args)
    return "rover doctor"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _human_approval_required(args: argparse.Namespace) -> bool:
    configured = getattr(args, "human_approval", None)
    if configured is not None:
        return bool(configured)
    return _env_bool("ROVER_HUMAN_APPROVAL") or _env_bool("CONTRIB_HUMAN_APPROVAL")


def _is_terminal_generation_failure(exc: PRGeneratorError) -> bool:
    message = str(exc).lower()
    terminal_markers = (
        "structural retry kill switch engaged",
        "patch did not modify the chosen target file",
        "generated files were identical to the originals",
    )
    return any(marker in message for marker in terminal_markers)


def _changed_file_summary(changed_files: dict[str, str]) -> str:
    files = list(changed_files)
    if not files:
        return "-"
    if len(files) <= 4:
        return ", ".join(files)
    return ", ".join(files[:4]) + f", +{len(files) - 4} more"


def _repo_surface_summary(candidate: object) -> str:
    files = getattr(candidate, "files", {}) or {}
    py_count = sum(1 for path in files if str(path).endswith(".py"))
    ts_count = sum(1 for path in files if str(path).endswith((".ts", ".tsx")))
    test_count = sum(1 for path in files if "test" in str(path).lower())
    return f"files={len(files)}  py={py_count}  ts={ts_count}  tests={test_count}"


def _patch_risk_summary(changed_files: dict[str, str]) -> str:
    file_count = len(changed_files)
    changed_tests = [
        path for path in changed_files
        if "test" in path.lower().replace("\\", "/") or path.endswith((".spec.ts", ".test.ts", "_test.py"))
    ]
    risks: list[str] = []
    if file_count > 2:
        risks.append(f"{file_count} files changed")
    if not changed_tests:
        risks.append("no test file changed")
    return "; ".join(risks) if risks else "narrow patch with test coverage signal"


def _patch_risk_level(improvement_type: str, changed_files: dict[str, str]) -> str:
    normalized = (improvement_type or "").strip().lower()
    if normalized in {"docs_fix", "documentation", "docs"}:
        return "low"
    if normalized in {"feature_add", "feature"}:
        return "high"
    if len(changed_files) > 2:
        return "high"
    return "medium"


def _read_operator_reason(default: str) -> str:
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write("  reason (optional): ")
            tty.flush()
            reason = tty.readline().strip()
    except OSError:
        try:
            reason = input("reason (optional): ").strip()
        except (EOFError, KeyboardInterrupt):
            reason = ""
    return reason or default


def _handle_human_approval(
    args: argparse.Namespace,
    candidate,
    improvement,
    log: logging.Logger,
) -> str:
    if not _human_approval_required(args):
        return "submit"
    store = PREngineStore()
    actor = os.getenv("GITHUB_OWNER", "").strip() or os.getenv("GITHUB_LOGIN", "").strip() or "operator"
    base_details = {
        "actor": actor,
        "opportunity_id": improvement.opportunity_id,
        "title": improvement.title,
        "improvement_type": improvement.improvement_type,
        "files": list(improvement.changed_files),
        "risk": _patch_risk_summary(improvement.changed_files),
        "risk_level": _patch_risk_level(improvement.improvement_type, improvement.changed_files),
    }

    if not sys.stdin.isatty():
        log.warning("Human approval requested but no interactive TTY is available; queueing patch instead of submitting.")
        if improvement.opportunity_id is not None:
            store.transition_opportunity(
                improvement.opportunity_id,
                "READY",
                why_advanced="Queued because human approval was requested without an interactive TTY.",
            )
        store.record_repo_event(
            None,
            candidate.full_name,
            "human_approval_queue",
            f"{actor} queued patch because no interactive TTY was available.",
            {**base_details, "decision": "queue", "reason": "no_tty"},
        )
        notify(f"PR queued: {candidate.full_name} - {improvement.title}")
        return "queue"

    from src.core.cli_ui import _choose_arrow, print_blank, print_info, print_item, print_section

    print_blank()
    print_section("Human Approval")
    print_info(f"  repo    [bold]{candidate.full_name}[/]")
    print_info(f"  title   [bold]{improvement.title}[/]")
    print_info(f"  type    [dim]{improvement.improvement_type}[/]")
    print_info(f"  files   [dim]{_changed_file_summary(improvement.changed_files)}[/]")
    print_info(f"  risk    [yellow]{_patch_risk_summary(improvement.changed_files)}[/]")
    if improvement.rationale:
        print_item(f"why     {improvement.rationale}")
    print_blank()

    answer = _choose_arrow(
        "Review decision",
        [
            "Submit PR",
            "Queue for later",
            "Reject patch",
        ],
    )
    if answer.startswith("Submit"):
        store.record_repo_event(
            None,
            candidate.full_name,
            "human_approval_submit",
            f"{actor} approved patch for submission.",
            {**base_details, "decision": "submit"},
        )
        log.info("Operator %s approved patch for %s before submission", actor, candidate.full_name)
        return "submit"
    if answer.startswith("Queue"):
        reason = _read_operator_reason("operator_queue")
        if improvement.opportunity_id is not None:
            store.transition_opportunity(
                improvement.opportunity_id,
                "READY",
                why_advanced=f"Queued by operator during human approval: {reason}",
            )
        store.record_repo_event(
            None,
            candidate.full_name,
            "human_approval_queue",
            f"{actor} queued patch during human approval: {reason}",
            {**base_details, "decision": "queue", "reason": reason},
        )
        log.info("Operator %s queued patch for %s before submission", actor, candidate.full_name)
        notify(f"PR queued: {candidate.full_name} - {improvement.title}")
        return "queue"
    reason = _read_operator_reason("operator_rejected")
    if improvement.opportunity_id is not None:
        store.reject_opportunity_by_id(
            improvement.opportunity_id,
            reason_code="operator_rejected",
            human_summary=f"Operator rejected the generated patch during human approval: {reason}",
            state="VERIFY",
        )
    store.record_repo_event(
        None,
        candidate.full_name,
        "human_approval_reject",
        f"{actor} rejected patch during human approval: {reason}",
        {**base_details, "decision": "reject", "reason": reason},
    )
    log.info("Operator %s rejected patch for %s before submission", actor, candidate.full_name)
    notify(f"PR rejected: {candidate.full_name} - {improvement.title}")
    return "reject"


def _warn_deprecated_aliases(raw_argv: list[str], parser: argparse.ArgumentParser | None = None) -> None:
    warned: set[str] = set()
    parsed_args: argparse.Namespace | None = None
    messages: list[str] = []

    if parser is not None:
        try:
            parsed_args, _ = parser.parse_known_args(raw_argv)
        except SystemExit:
            parsed_args = None

    argv0 = Path(sys.argv[0]).name.lower() if sys.argv else ""
    if argv0 == "rover-engine":
        replacement = _suggest_modern_command(parsed_args) if parsed_args is not None else "rover doctor"
        messages.append(
            f"`rover-engine` is a compatibility alias. Prefer modern commands. Try: `{replacement}`."
        )

    for arg in raw_argv:
        replacement = _DEPRECATED_FLAG_HINTS.get(arg)
        if not replacement or arg in warned:
            continue
        warned.add(arg)
        extra = ""
        if parsed_args is not None:
            if arg == "--pr-check":
                extra = " Equivalent command: `rover check`."
            elif arg == "--pr-respond":
                extra = " Equivalent command: `python -m app.builder --contrib-respond`."
            elif arg == "--pr":
                extra = f" Equivalent command: `{_format_modern_contrib_command(parsed_args)}`."
        messages.append(
            f"`{arg}` is deprecated. Prefer `{replacement}`.{extra}"
        )

    if messages:
        print(f"[warn] {' '.join(messages)}", file=sys.stderr)


# ── Contribution mode ────────────────────────────────────────
def run_contribution_mode(args: argparse.Namespace, log: logging.Logger) -> dict[str, object] | None:
    from src.core.cli_ui import (
        _choose_arrow,
        _console,
        print_blank,
        print_err,
        print_info,
        print_item,
        print_ok,
        print_pr_summary,
        print_section,
        print_warn,
    )

    def _status_label(message: str) -> str:
        return f"  [dim cyan]↻[/]  [dim]{message}[/]"

    target = args.count or _legacy_count_arg(sys.argv) or 1
    repo_url = args.contrib or ""
    current_login = ""
    try:
        current_login = get_current_github_login().lower()
    except ForkError:
        current_login = ""
    own_repo = bool(current_login and repo_url and re.search(rf"(?i){re.escape(current_login)}/", repo_url))

    dry = DRY_RUN or args.dry_run
    mode_label = f"targeted" if repo_url else "search"
    override_limits = bool(getattr(args, "override_limits", False))

    pinned_candidate = None
    if repo_url:
        try:
            print_item(f"target check  [bold]{repo_url}[/]")
            with _console.status(
                _status_label(f"checking target repo: {repo_url}"),
                spinner="dots",
                spinner_style="dim cyan",
            ) as _target_status:
                def _target_status_cb(msg: str) -> None:
                    _target_status.update(_status_label(msg))
                pinned_candidate = fetch_repo_candidate(repo_url, log, override_limits=override_limits, status_cb=_target_status_cb)
            print_ok(f"target ready  [bold]{pinned_candidate.full_name}[/]")
            print_info(
                f"stats   [dim]{pinned_candidate.stars}★  forks={getattr(pinned_candidate, 'forks', 0)}  "
                f"{pinned_candidate.license}  pushed={pinned_candidate.pushed_days_ago}d ago[/]"
            )
            print_info(f"surface [dim]{_repo_surface_summary(pinned_candidate)}[/]")
        except ScraperError as exc:
            print_err(f"cannot use target repo: {exc}")
            log.info("Cannot use target repo %r: %s", repo_url, exc)
            log.info("Targeted mode still requires a Python/TypeScript repo with a confident local inspection window.")
            return None
        except Exception as exc:
            print_err(f"cannot fetch repo: {exc}")
            log.info("Cannot fetch target repo %r: %s", repo_url, exc)
            return None

    print_section("Contribution run")
    print_info(f"  mode    [bold]{mode_label}[/]{'  ' + repo_url if repo_url else ''}")
    print_info(f"  goal    [dim]{args.goal}[/]")
    print_info(f"  target  [dim]{target} PR{'s' if target != 1 else ''}[/]")
    if dry:
        print_info("  dry     [yellow]yes — patch only, no submission[/]")
    if _human_approval_required(args):
        print_info("  review  [yellow]human approval before submit[/]")
    if override_limits:
        print_info("  limits  [yellow]override — .env contribution filters bypassed[/]")
    print_blank()

    log.info("=" * 55)
    if repo_url:
        log.info("  CONTRIBUTION MODE (targeted): %s - target=%d", repo_url, target)
        if override_limits:
            log.warning("  contribution limit override is active for this pinned repo")
        elif _PR_TARGETED_ALLOW_BROAD:
            log.warning("  targeted broad-repo bypass is active; scope guardrails are relaxed for this pinned repo")
    else:
        log.info("  CONTRIBUTION MODE (search): target=%d contribution PR(s)", target)
    log.info("=" * 55)

    reset_usage()
    run_id = start_pr_engine_run(
        "targeted" if repo_url else "search",
        target,
        external_run_id=str(getattr(args, "external_run_id", "") or ""),
    )
    if isinstance(run_id, int):
        print_info(f"  run     [dim]#{run_id}[/]")
        print_blank()

    submitted = 0
    attempts = 0
    max_attempts = target * (4 if repo_url else 8)
    already_prd = get_pr_submitted_repos()
    blacklisted = get_security_blacklisted_sources()

    pinned_failures = 0
    while submitted < target and attempts < max_attempts:
        attempts += 1
        log.info("Contribution %d/%d (attempt %d)", submitted + 1, target, attempts)
        print_item(f"[dim][{submitted + 1}/{target}][/] attempt  [dim]{attempts}/{max_attempts}[/]")

        if pinned_candidate:
            candidate = pinned_candidate
        else:
            with _console.status(
                _status_label(f"[{submitted + 1}/{target}] searching GitHub for a suitable repo"),
                spinner="dots",
                spinner_style="dim cyan",
            ):
                if args.first_pr:
                    candidate = find_pr_target(
                        blacklisted,
                        already_prd,
                        log,
                        first_pr_mode=True,
                        override_limits=override_limits,
                    )
                else:
                    candidate = find_pr_target(blacklisted, already_prd, log, override_limits=override_limits)
            if not candidate:
                print_warn(f"[{submitted + 1}/{target}] no suitable target found — stopping")
                log.warning("No suitable contribution target found after %d attempt(s)", attempts)
                break
            already_prd = already_prd | {candidate.full_name.lower()}

        is_own = own_repo or bool(current_login and candidate.full_name.lower().startswith(f"{current_login}/"))
        print_item(
            f"[dim][{submitted + 1}/{target}][/] target  [bold]{candidate.full_name}[/]"
            f"   [dim]{candidate.stars}★  {candidate.license}  pushed {candidate.pushed_days_ago}d ago[/]"
        )
        log.info(
            "Target: %s (%d stars, %s license, pushed %dd ago)",
            candidate.full_name,
            candidate.stars,
            candidate.license,
            candidate.pushed_days_ago,
        )
        print_info(f"surface [dim]{_repo_surface_summary(candidate)}[/]")

        if not is_own and not can_submit_contribution_to_repo(candidate.full_name):
            existing_pr = _known_open_pr(candidate.full_name)
            print_warn(f"[{submitted + 1}/{target}] queued — existing open PR in {candidate.full_name}")
            if existing_pr and existing_pr.get("pr_url"):
                print_info(f"     [link={existing_pr['pr_url']}][dim]{existing_pr['pr_url']}[/][/]")
            log.info("Skipping generation for %s because an open PR is already active", candidate.full_name)
            if existing_pr and existing_pr.get("pr_url"):
                log.info("Known open PR for %s: %s", candidate.full_name, existing_pr["pr_url"])
            if isinstance(run_id, int):
                PREngineStore().record_repo_event(
                    run_id,
                    candidate.full_name,
                    "pr_already_open",
                    f"Existing PR already open: {existing_pr.get('pr_title') or existing_pr.get('pr_url') or candidate.full_name}",
                    {
                        "pr_url": existing_pr.get("pr_url", "") if existing_pr else "",
                        "pr_title": existing_pr.get("pr_title", "") if existing_pr else "",
                        "source": existing_pr.get("source", "") if existing_pr else "",
                    },
                )
            continue

        print_item(f"[dim][{submitted + 1}/{target}][/] scanning opportunities and generating patch with AI ...")
        print_info("stage   [dim]qualify → plan → generate → structural review[/]")
        try:
            improvement = generate_pr_improvement(
                candidate,
                log,
                char_budget=20_000 if is_own else 40_000,
                goal=args.goal,
                targeted_mode=bool(repo_url),
            )
        except PRGeneratorError as exc:
            print_warn(f"[{submitted + 1}/{target}] generation failed: {exc}")
            log.info("Generation failed for %s: %s", candidate.full_name, exc)
            if pinned_candidate:
                if _is_terminal_generation_failure(exc):
                    print_err("pinned target hit a terminal structural rejection — stopping")
                    log.info("Pinned target hit terminal structural rejection; stopping: %s", exc)
                    break
                pinned_failures += 1
                if pinned_failures >= 3:
                    print_err(f"pinned target failed {pinned_failures} times — stopping")
                    log.info("Pinned target failed %d times; stopping", pinned_failures)
                    break
            continue

        print_item(f"[dim][{submitted + 1}/{target}][/] validating generated patch ...")
        print_info(f"files   [dim]{_changed_file_summary(improvement.changed_files)}[/]")
        print_info(f"risk    [dim]{_patch_risk_summary(improvement.changed_files)}[/]")

        # Multi-turn repair loop — max iterations depend on pattern policy
        _REPAIR_MAX: dict[str, int] = {"live-safe": 3}
        _repair_max = _REPAIR_MAX.get(getattr(improvement, "execution_mode", ""), 2)
        _repair_attempt = 0
        _sandbox_passed = False
        _error_ctx = ""
        from dataclasses import replace as _dc_replace

        sandbox = run_sandbox_validation(improvement.changed_files)
        if sandbox.sandbox_verified:
            _sandbox_passed = True
            print_ok(f"[dim][{submitted + 1}/{target}][/] sandbox validation passed")
        elif not sandbox.sandbox_output:
            # Non-actionable (infra/import error) — skip repair, proceed as-is
            print_warn(f"[{submitted + 1}/{target}] sandbox failed (non-actionable) — proceeding")
            log.info("Sandbox non-actionable failure for %s — skipping repair loop.", candidate.full_name)
        else:
            _error_ctx = sandbox.sandbox_output
            _repair_candidate = candidate
            while _repair_attempt < _repair_max - 1 and _error_ctx:
                _repair_attempt += 1
                print_warn(
                    f"[{submitted + 1}/{target}] sandbox failed — repair attempt "
                    f"{_repair_attempt}/{_repair_max - 1}"
                )
                log.info(
                    "Repair loop attempt %d/%d for %s.",
                    _repair_attempt, _repair_max - 1, candidate.full_name,
                )
                _repair_candidate = _dc_replace(
                    _repair_candidate,
                    description=(
                        f"{candidate.description} "
                        f"[SANDBOX_ERROR_ITER_{_repair_attempt}] {_error_ctx[:300]}"
                    ),
                )
                try:
                    _retry_imp = generate_pr_improvement(
                        _repair_candidate,
                        log,
                        char_budget=20_000 if is_own else 40_000,
                        goal=args.goal,
                        targeted_mode=bool(repo_url),
                    )
                    _retry_sb = run_sandbox_validation(_retry_imp.changed_files)
                    improvement = _retry_imp
                    if _retry_sb.sandbox_verified:
                        _sandbox_passed = True
                        print_ok(
                            f"[dim][{submitted + 1}/{target}][/] sandbox repair passed "
                            f"(iter {_repair_attempt})"
                        )
                        log.info(
                            "repair_loop_success_iter_%d for %s.",
                            _repair_attempt, candidate.full_name,
                        )
                        break
                    else:
                        _error_ctx = _retry_sb.sandbox_output or _error_ctx
                        log.info(
                            "Repair attempt %d still failing for %s.",
                            _repair_attempt, candidate.full_name,
                        )
                except PRGeneratorError as _repair_exc:
                    log.info(
                        "Repair attempt %d generation failed for %s: %s",
                        _repair_attempt, candidate.full_name, _repair_exc,
                    )
                    break
            if not _sandbox_passed:
                log.info(
                    "repair_loop_exhausted for %s after %d attempt(s) — proceeding with best patch.",
                    candidate.full_name, _repair_attempt + 1,
                )
                print_warn(
                    f"[{submitted + 1}/{target}] repair exhausted after "
                    f"{_repair_attempt + 1} attempt(s) — using best patch"
                )

        print_ok(f"[dim][{submitted + 1}/{target}][/] patch ready   [bold]{improvement.title}[/]")
        print_info(f"type    [dim]{improvement.improvement_type}[/]")
        if getattr(improvement, "target_file", ""):
            print_info(f"target  [dim]{improvement.target_file}[/]")
        log.info("Type: %s | Title: %s", improvement.improvement_type, improvement.title)
        log.info("Rationale: %s", improvement.rationale)
        log.info("Files: %s", list(improvement.changed_files.keys()))
        if isinstance(run_id, int):
            PREngineStore().record_repo_event(
                run_id,
                candidate.full_name,
                "patch_generated",
                f"Patch generated: {improvement.title}",
                {
                    "title": improvement.title,
                    "improvement_type": improvement.improvement_type,
                    "files": list(improvement.changed_files.keys()),
                },
            )

        if DRY_RUN or args.dry_run:
            print_ok(f"[dim][{submitted + 1}/{target}][/] [yellow]dry-run[/] — patch validated, PR not submitted")
            log.info("[DRY RUN] would submit contribution to %s", candidate.full_name)
            submitted += 1
            continue

        if getattr(improvement, "execution_mode", "live-safe") == "live-review":
            print_warn(f"[{submitted + 1}/{target}] queued for human review — PR not submitted")
            log.info("Patch for %s requires human review before live submit", candidate.full_name)
            if isinstance(run_id, int):
                PREngineStore().record_repo_event(
                    run_id,
                    candidate.full_name,
                    "manual_review_required",
                    f"Patch queued for human review: {improvement.title}",
                    {
                        "title": improvement.title,
                        "pattern_type": improvement.pattern_type,
                        "target_file": improvement.target_file,
                    },
                )
            if pinned_candidate:
                break
            continue

        decision = _handle_human_approval(args, candidate, improvement, log)
        if decision == "queue":
            print_warn(f"[{submitted + 1}/{target}] queued by operator — PR not submitted")
            if pinned_candidate:
                break
            continue
        if decision == "reject":
            print_warn(f"[{submitted + 1}/{target}] rejected by operator — PR not submitted")
            if pinned_candidate:
                break
            continue

        print_item(f"[dim][{submitted + 1}/{target}][/] submitting PR to [bold]{candidate.full_name}[/] ...")
        _set_run_stage(
            "submit",
            candidate.full_name,
            {
                "target_file": improvement.target_file,
                "pattern_type": improvement.pattern_type,
                "improvement_type": improvement.improvement_type,
            },
        )
        try:
            if is_own:
                pr_result = fix_and_push_own_repo(
                    full_name=candidate.full_name,
                    default_branch=candidate.default_branch,
                    changed_files=improvement.changed_files,
                    commit_msg=improvement.title,
                    log=log,
                    improvement_type=improvement.improvement_type,
                )
                submitted += 1
                print_ok(f"[{submitted}/{target}] own-repo fix pushed")
                print_info(f"     [link={pr_result.pr_url}][dim]{pr_result.pr_url}[/][/]")
                log.info("[%d/%d] Own-repo fix pushed: %s", submitted, target, pr_result.pr_url)
                if isinstance(run_id, int):
                    PREngineStore().record_repo_event(
                        run_id,
                        candidate.full_name,
                        "pr_submitted",
                        f"Own-repo fix pushed: {pr_result.pr_title}",
                        {"pr_url": pr_result.pr_url, "pr_title": pr_result.pr_title, "kind": "own_repo"},
                    )
            else:
                pr_result = fork_and_submit_pr(
                    full_name=candidate.full_name,
                    default_branch=candidate.default_branch,
                    changed_files=improvement.changed_files,
                    pr_title=improvement.title,
                    pr_body=improvement.body,
                    log=log,
                    improvement_type=improvement.improvement_type,
                )
                save_pr_log(
                    pr_result,
                    improvement_type=improvement.improvement_type,
                    opportunity_id=improvement.opportunity_id,
                )
                submitted += 1
                print_ok(f"[{submitted}/{target}] PR submitted")
                print_info(f"     [link={pr_result.pr_url}][dim]{pr_result.pr_url}[/][/]")
                log.info("[%d/%d] PR submitted: %s", submitted, target, pr_result.pr_url)
                if isinstance(run_id, int):
                    PREngineStore().record_repo_event(
                        run_id,
                        candidate.full_name,
                        "pr_submitted",
                        f"PR submitted: {pr_result.pr_title}",
                        {"pr_url": pr_result.pr_url, "pr_title": pr_result.pr_title, "kind": "fork"},
                    )
                print_pr_summary(
                    pr_url=pr_result.pr_url,
                    pr_title=pr_result.pr_title,
                    repo=candidate.full_name,
                    improvement_type=improvement.improvement_type,
                    changed_files=improvement.changed_files,
                    rationale=improvement.rationale,
                )

            notify(
                f"[{submitted}/{target}] Contribution submitted\n"
                f"Repo: {candidate.full_name}\n"
                f"Title: {pr_result.pr_title}\n"
                f"URL: {pr_result.pr_url}"
            )
        except PRAlreadyExistsError as exc:
            print_warn(f"PR already open for {candidate.full_name} — skipping")
            log.warning("PR already open for %s: %s", candidate.full_name, exc)
            existing_pr = _known_open_pr(candidate.full_name)
            if existing_pr and existing_pr.get("pr_url"):
                print_info(f"     [link={existing_pr['pr_url']}][dim]{existing_pr['pr_url']}[/][/]")
                log.info("Known existing PR for %s: %s", candidate.full_name, existing_pr["pr_url"])
                if isinstance(run_id, int):
                    PREngineStore().record_repo_event(
                        run_id,
                        candidate.full_name,
                        "pr_already_open",
                        f"Existing PR already open: {existing_pr.get('pr_title') or existing_pr['pr_url']}",
                        {
                            "pr_url": existing_pr["pr_url"],
                            "pr_title": existing_pr.get("pr_title", ""),
                            "source": existing_pr.get("source", ""),
                        },
                    )
        except ForkError as exc:
            print_err(f"fork/PR failed for {candidate.full_name}: {exc}")
            log.info("Fork/PR failed for %s: %s", candidate.full_name, exc)

    usage = get_usage()
    log.info(
        "Contribution mode complete - submitted=%d/%d | attempts=%d | calls=%d | ~%d tokens",
        submitted,
        target,
        attempts,
        usage["calls"],
        usage["est_tokens"],
    )

    print_blank()
    print_section("Done")
    color = "green" if submitted >= target else ("yellow" if submitted > 0 else "red")
    print_info(
        f"  [{color}]{submitted} / {target} submitted[/]"
        f"   [dim]{attempts} attempts   {usage['calls']} AI calls[/]"
    )
    print_blank()

    summary = finish_pr_engine_run(submitted, target, attempts, usage, log)

    if submitted > 0:
        try:
            answer = _choose_arrow("Submit another PR?", ["Yes — find another repo", "No — I'm done"])
            if answer.startswith("Yes"):
                print_blank()
                run_contribution_mode(args, log)
                return summary
        except (KeyboardInterrupt, Exception):
            pass
        print_blank()

    if submitted == 0 and summary:
        top = summary.get("top_rejections", [])
        if top:
            print_section("Why nothing was submitted")
            for reason, count in top[:5]:
                print_item(f"[yellow]{reason}[/]  ×{count}")
            bottleneck = summary.get("bottleneck", "")
            if bottleneck:
                print_item(f"bottleneck: [bold]{bottleneck}[/]")
            if summary.get("shortlist_summary"):
                print_item(f"shortlisted: [bold]{summary.get('shortlisted', 0)}[/]")
            if summary.get("planned") is not None:
                print_item(f"planned patches: [bold]{summary.get('planned', 0)}[/]")
            if summary.get("generated") is not None:
                print_item(f"generated patches: [bold]{summary.get('generated', 0)}[/]")
            if summary.get("broad_rejected_early") is not None:
                print_item(f"broad rejected early: [bold]{summary.get('broad_rejected_early', 0)}[/]")
            print_blank()
        print_section("Next steps")
        print_item("[bold]rover report[/]               [dim]— full rejection history[/]")
        print_item("[bold]rover inspect[/] owner/repo   [dim]— diagnose a specific repo[/]")
        print_item("adjust [bold]CONTRIB_LANE[/] or [bold]CONTRIB_TOPIC_KEYWORDS[/] in [dim].env[/]")
        print_blank()

    return summary


_STATUS_ICON = {"open": "🟡", "merged": "🟢", "closed": "🔴"}
_VALID_STATUSES = {"all", "open", "merged", "closed"}


def list_prs(status_filter: str) -> None:
    from src.contrib.contribution_store import ContributionStore

    if status_filter not in _VALID_STATUSES:
        print(f"[error] invalid status filter '{status_filter}'. Choose from: all, open, merged, closed")
        return

    store = ContributionStore()
    rows = store.list_pull_requests(
        limit=100,
        status_filter=None if status_filter == "all" else status_filter,
    )

    if not rows:
        label = f" ({status_filter})" if status_filter != "all" else ""
        print(f"No PRs found{label}. Run --contrib to submit your first PR.")
        return

    counts: dict[str, int] = {}
    for row in rows:
        s = row["status"]
        counts[s] = counts.get(s, 0) + 1

    summary_parts = [f"{v} {k}" for k, v in sorted(counts.items())]
    print(f"\nPRs ({len(rows)} total — {', '.join(summary_parts)}):\n")

    col_repo = 28
    col_title = 42
    col_status = 8
    col_date = 10
    header = (
        f"{'REPO':<{col_repo}}  {'TITLE':<{col_title}}  {'STATUS':<{col_status}}  {'DATE':<{col_date}}  URL"
    )
    print(header)
    print("-" * (col_repo + col_title + col_status + col_date + 4 + 46))

    for row in rows:
        repo = row["repo_full_name"] or ""
        title = row["pr_title"] or ""
        status = row["status"] or "?"
        url = row["pr_url"] or ""
        date = (row["submitted_at"] or "")[:10]
        icon = _STATUS_ICON.get(status, " ")

        repo_col = repo[:col_repo].ljust(col_repo)
        title_col = (title[:col_title - 1] + "…" if len(title) > col_title else title).ljust(col_title)
        status_col = f"{icon} {status}"[:col_status].ljust(col_status)

        print(f"{repo_col}  {title_col}  {status_col}  {date:<{col_date}}  {url}")

    print()


def inspect_repo(
    repo: str,
    log: logging.Logger,
    *,
    cached_only: bool = False,
    force_refresh: bool = False,
) -> None:
    from src.core.cli_ui import (
        _console,
        print_blank,
        print_err,
        print_info,
        print_item,
        print_ok,
        print_repo_inspect_description,
        print_repo_inspect_next_steps,
        print_repo_inspect_overview,
        print_repo_inspect_scope_notes,
        print_repo_inspect_topics,
        print_section,
        print_warn,
    )
    total_steps = 6

    def _status_label(step: int, message: str) -> str:
        return f"  [dim cyan]↻[/]  [dim][{step}/{total_steps}] {message}[/]"

    print_section("Repo Inspect")
    print_item(f"target  {repo}")
    store = PREngineStore()
    cached_inspect: dict[str, object] | None = None
    try:
        cached_full_name = resolve_repo_full_name(repo)
        cached_inspect = store.get_repo_inspect_snapshot(cached_full_name)
    except ScraperError:
        cached_inspect = None

    if cached_only and force_refresh:
        print_err("cannot combine --cached and --refresh for the same inspect run")
        print_blank()
        return

    if cached_only and not cached_inspect:
        print_err(f"no cached inspect snapshot found for {repo}")
        print_blank()
        return

    metadata_error: Exception | None = None
    full_name = ""
    metadata: dict[str, object] | None = None
    if cached_only:
        inspect_data = cached_inspect
        artifact_path = str((cached_inspect or {}).get("artifact_path", "") or "")
        print_ok("using cached inspect snapshot")
        if (cached_inspect or {}).get("inspected_at"):
            print_info(f"last inspected at {cached_inspect['inspected_at']}")
        if artifact_path:
            print_info(f"artifact  {artifact_path}")
        print_blank()
    else:
        with _console.status(
            _status_label(1, f"checking repo metadata and local inspect snapshot for {repo}"),
            spinner="dots",
            spinner_style="dim cyan",
        ):
            try:
                full_name, metadata = fetch_repo_metadata(repo, log)
            except Exception as exc:
                metadata_error = exc

        inspect_data: dict[str, object] | None = None
        artifact_path = ""
        source_pushed_at = str((metadata or {}).get("pushed_at", "") or "")
        if metadata_error is not None and not cached_inspect:
            log.info("Cannot inspect repo %s: %s", repo, metadata_error)
            print_err(f"cannot inspect repo: {metadata_error}")
            print_blank()
            return

        if (
            not force_refresh
            and metadata_error is None
            and cached_inspect
            and cached_inspect.get("source_pushed_at") == source_pushed_at
        ):
            with _console.status(
                _status_label(2, "loading cached inspect snapshot from local storage"),
                spinner="dots",
                spinner_style="dim cyan",
            ):
                inspect_data = cached_inspect
                artifact_path = str(cached_inspect.get("artifact_path", "") or "")
            print_ok("using cached inspect snapshot")
            print_info(f"{full_name} unchanged; source download skipped")
            if cached_inspect.get("inspected_at"):
                print_info(f"last inspected at {cached_inspect['inspected_at']}")
            if artifact_path:
                print_info(f"artifact  {artifact_path}")
            print_blank()
        else:
            if metadata_error is not None and cached_inspect:
                print_warn("GitHub metadata refresh failed; falling back to the last local inspect snapshot")
                inspect_data = cached_inspect
                artifact_path = str(cached_inspect.get("artifact_path", "") or "")
                print_info(str(metadata_error))
                if cached_inspect.get("inspected_at"):
                    print_info(f"last inspected at {cached_inspect['inspected_at']}")
                if artifact_path:
                    print_info(f"artifact  {artifact_path}")
                print_blank()
            else:
                if force_refresh and cached_inspect:
                    print_warn("refresh requested; local snapshot will be replaced")
                elif cached_inspect:
                    print_warn("repo changed since the last inspect snapshot; refreshing source data")
                else:
                    print_item("no cached inspect snapshot found; running a fresh inspect")
                candidate: object
                with _console.status(
                    _status_label(2, f"fetching repo metadata and source snapshot for {full_name or repo}"),
                    spinner="dots",
                    spinner_style="dim cyan",
                ):
                    try:
                        candidate = fetch_repo_candidate_with_scope(repo, log, enforce_scope=False)
                    except Exception as exc:
                        log.info("Cannot inspect repo %s: %s", repo, exc)
                        print_err(f"cannot inspect repo: {exc}")
                        print_blank()
                        return
                print_ok("repo snapshot ready")
                print_info(
                    f"{candidate.full_name}  stars={candidate.stars}  forks={candidate.forks}  "
                    f"pushed={candidate.pushed_days_ago}d ago"
                )
                print_info(
                    f"downloaded surface: files={len(candidate.files)}  topics={len(candidate.topics)}"
                )

                with _console.status(
                    _status_label(3, "evaluating lane fit, first-PR fit, and contribution scope"),
                    spinner="dots",
                    spinner_style="dim cyan",
                ):
                    inspect_data = get_repo_inspect_data(candidate)
                    artifact_path = str(write_repo_inspect_artifact(inspect_data))
                    store.save_repo_inspect_snapshot(
                        candidate,
                        inspect_data,
                        source_pushed_at=source_pushed_at,
                        artifact_path=artifact_path,
                    )
                print_ok("analysis complete")
                print_info(
                    f"lane={'matched' if inspect_data.get('lane_match') else 'not matched'}  "
                    f"first-pr={'friendly' if inspect_data.get('first_pr_friendly') else 'not ideal'}  "
                    f"targeted={inspect_data.get('targeted_scope')}"
                )
                print_info(f"snapshot saved to {artifact_path}")
                print_blank()

    if inspect_data is None:
        print_err("inspect snapshot could not be prepared")
        print_blank()
        return

    with _console.status(
        _status_label(4, "printing repo overview"),
        spinner="dots",
        spinner_style="dim cyan",
    ):
        print_repo_inspect_overview(inspect_data)
    print_blank()

    with _console.status(
        _status_label(5, "printing description and source summary"),
        spinner="dots",
        spinner_style="dim cyan",
    ):
        print_repo_inspect_description(inspect_data)

    with _console.status(
        _status_label(6, "printing topics and repository signals"),
        spinner="dots",
        spinner_style="dim cyan",
    ):
        print_repo_inspect_topics(inspect_data)
    topics = inspect_data.get("topics") or []
    if topics:
        preview = ", ".join(str(topic) for topic in list(topics)[:4])
        more = "" if len(topics) <= 4 else f" +{len(topics) - 4} more"
        print_item("topic signals collected")
        print_info(f"{preview}{more}")
        print_blank()

    print_repo_inspect_scope_notes(inspect_data)
    print_repo_inspect_next_steps(inspect_data)
    print_ok("inspect report ready")


def collect_repo_inspect_payload(
    repo: str,
    log: logging.Logger,
    *,
    cached_only: bool = False,
    force_refresh: bool = False,
) -> dict[str, object]:
    store = PREngineStore()
    cached_inspect: dict[str, object] | None = None
    try:
        cached_full_name = resolve_repo_full_name(repo)
        cached_inspect = store.get_repo_inspect_snapshot(cached_full_name)
    except ScraperError:
        cached_inspect = None

    if cached_only and force_refresh:
        raise ValueError("cannot combine --cached and --refresh for the same inspect run")
    if cached_only and not cached_inspect:
        raise ValueError(f"no cached inspect snapshot found for {repo}")
    if cached_only and cached_inspect:
        payload = dict(cached_inspect)
        payload["source"] = "cache"
        return payload

    full_name, metadata = fetch_repo_metadata(repo, log)
    source_pushed_at = str((metadata or {}).get("pushed_at", "") or "")
    if (
        not force_refresh
        and cached_inspect
        and cached_inspect.get("source_pushed_at") == source_pushed_at
    ):
        payload = dict(cached_inspect)
        payload["source"] = "cache"
        return payload

    candidate = fetch_repo_candidate_with_scope(repo, log, enforce_scope=False)
    inspect_data = get_repo_inspect_data(candidate)
    artifact_path = str(write_repo_inspect_artifact(inspect_data))
    store.save_repo_inspect_snapshot(
        candidate,
        inspect_data,
        source_pushed_at=source_pushed_at,
        artifact_path=artifact_path,
    )
    payload = dict(inspect_data)
    payload.update(
        {
            "repo": full_name,
            "artifact_path": artifact_path,
            "source_pushed_at": source_pushed_at,
            "source": "fresh",
            "rendered": build_repo_inspect_report(candidate),
        }
    )
    return payload


def _capture_builder_action(fn, *args, **kwargs) -> str:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        fn(*args, **kwargs)
    return (stdout.getvalue() + stderr.getvalue()).strip()


def _print_json_payload(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _apply_command_text(args: argparse.Namespace, log: logging.Logger) -> None:
    if not args.command_text:
        return
    if any((args.profile, args.doctor, args.contrib_report, args.repo_inspect, args.scan_repo, args.contrib_check, args.contrib_respond, args.contrib is not None)):
        log.info("Skipping natural-language command routing because an explicit CLI action was already provided.")
        return

    request = parse_command_text(args.command_text)
    log.info(
        "Natural-language command mapped to action=%s repo=%s count=%d dry_run=%s confidence=%s",
        request.action,
        request.repo or "<search>",
        request.count,
        request.dry_run,
        request.confidence,
    )
    for reason in request.rationale:
        log.info("  route: %s", reason)

    if request.action == "profile":
        args.profile = True
        return
    if request.action == "doctor":
        args.doctor = True
        return
    if request.action == "contrib_report":
        args.contrib_report = True
        return
    if request.action == "contrib_check":
        args.contrib_check = True
        return
    if request.action == "contrib_respond":
        args.contrib_respond = True
        return
    if request.action == "repo_inspect":
        args.repo_inspect = request.repo
        return
    if request.action == "repo_scan":
        args.scan_repo = request.repo
        args.scan_kind = request.scan_kind
        return

    args.contrib = request.repo if request.repo else ""
    args.count = request.count
    args.goal = request.goal
    args.first_pr = request.first_pr
    args.dry_run = request.dry_run


def main(argv: list[str] | None = None) -> None:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    _warn_deprecated_aliases(raw_argv, parser)
    args, unknown = parser.parse_known_args(raw_argv)
    unexpected = [arg for arg in unknown if not re.match(r"^--\d+$", arg)]
    if unexpected:
        lines = [f"unrecognized argument: {unexpected[0]}"]
        hint = _suggest(unexpected[0])
        if hint:
            lines.append(hint)
        else:
            lines.append("  Run with --help to see all available commands and examples.")
        parser.error("\n".join(lines))

    if args.list_prs is not None:
        list_prs(args.list_prs or "all")
        return

    if args.route_only:
        request = parse_command_text(args.command_text)
        payload = {
            "action": "route_command",
            "mapped_action": request.action,
            "repo": request.repo,
            "count": request.count,
            "goal": request.goal,
            "scan_kind": request.scan_kind,
            "dry_run": request.dry_run,
            "first_pr": request.first_pr,
            "confidence": request.confidence,
            "rationale": request.rationale,
            "cli_args": request.to_cli_args(),
        }
        _print_json_payload(payload)
        return

    if args.install_mcp:
        from src.platform.mcp_install import install_mcp
        from src.core.config import ROOT
        out = install_mcp(ROOT)
        if args.json:
            _print_json_payload({"action": "install_mcp", "path": str(out)})
        else:
            print(f"MCP config written: {out}")
            print("Restart Claude Code to pick up the new config.")
        return

    if args.install_openclaw:
        from src.platform.openclaw_install import install_openclaw_assets
        import shutil as _shutil
        from pathlib import Path as _Path

        rover_bin = (
            _preferred_user_bin("rover")
            or _shutil.which("rover")
            or str((_Path(sys.executable).resolve().parent / "rover"))
        )
        rover_mcp_bin = (
            _preferred_user_bin("rover-mcp")
            or _shutil.which("rover-mcp")
            or str((_Path(sys.executable).resolve().parent / "rover-mcp"))
        )
        _python = _shutil.which("python3") or sys.executable
        skill_path, tool_path = install_openclaw_assets(
            rover_bin=rover_bin,
            python_bin=_python,
            rover_mcp_bin=rover_mcp_bin,
        )
        if args.json:
            _print_json_payload(
                {"action": "install_openclaw", "skill_path": str(skill_path), "wrapper_path": str(tool_path)}
            )
        else:
            print(f"OpenClaw skill : {skill_path}")
            print(f"OpenClaw wrapper: {tool_path}")
        return

    if args.install_hermes:
        from src.platform.hermes_install import install_hermes_config
        import shutil as _shutil
        from pathlib import Path as _Path

        rover_mcp_bin = _shutil.which("rover-mcp") or str((_Path(sys.executable).resolve().parent / "rover-mcp"))
        config_path = install_hermes_config(rover_mcp_bin=rover_mcp_bin)
        if args.json:
            _print_json_payload({"action": "install_hermes", "config_path": str(config_path)})
        else:
            print(f"Hermes config: {config_path}")
        return

    if args.doctor:
        if args.json:
            _print_json_payload(
                {
                    "action": "doctor",
                    "checks": [check.__dict__ for check in collect_doctor_checks()],
                    "rendered": build_doctor_report(),
                }
            )
        else:
            print(build_doctor_report())
        return
    if args.profile:
        payload = collect_profile_payload()
        if args.json:
            _print_json_payload(payload)
        else:
            print(payload["rendered"])
        return
    if args.test_notify:
        from src.core.notify import notify
        success = notify("🧪 Rover test notification - credentials working!")
        if success:
            print_ok("Test notification sent successfully")
        else:
            print_err("Test notification failed - check credentials and logs")
        return
    if args.contrib_report:
        from src.contrib.pr_generator import get_contribution_report_data
        summaries, queued = get_contribution_report_data(limit=5)
        if args.json:
            _print_json_payload(
                {
                    "action": "contrib_report",
                    "limit": 5,
                    "summaries": summaries,
                    "queued": queued,
                    "rendered": build_contribution_report(limit=5),
                }
            )
        else:
            from src.core.cli_ui import print_styled_report
            print_styled_report(summaries, queued)
        return

    log = setup_logging()
    log.info("=" * 55)
    log.info("  GitHub Contribution Engine started")
    log.info("=" * 55)
    cleanup_old_logs()
    _apply_command_text(args, log)

    if args.doctor:
        if args.json:
            _print_json_payload(
                {
                    "action": "doctor",
                    "checks": [check.__dict__ for check in collect_doctor_checks()],
                    "rendered": build_doctor_report(),
                }
            )
        else:
            print(build_doctor_report())
        return
    if args.profile:
        payload = collect_profile_payload()
        if args.json:
            _print_json_payload(payload)
        else:
            print(payload["rendered"])
        return
    if args.test_notify:
        from src.core.notify import notify
        success = notify("🧪 Rover test notification - credentials working!")
        if success:
            print_ok("Test notification sent successfully")
        else:
            print_err("Test notification failed - check credentials and logs")
        return
    if args.contrib_report:
        from src.contrib.pr_generator import get_contribution_report_data
        summaries, queued = get_contribution_report_data(limit=5)
        if args.json:
            _print_json_payload(
                {
                    "action": "contrib_report",
                    "limit": 5,
                    "summaries": summaries,
                    "queued": queued,
                    "rendered": build_contribution_report(limit=5),
                }
            )
        else:
            from src.core.cli_ui import print_styled_report
            print_styled_report(summaries, queued)
        return
    if args.repo_inspect:
        if args.json:
            payload = collect_repo_inspect_payload(
                args.repo_inspect,
                log,
                cached_only=bool(getattr(args, "cached", False)),
                force_refresh=bool(getattr(args, "refresh", False)),
            )
            payload.setdefault("action", "repo_inspect")
            _print_json_payload(payload)
        else:
            inspect_repo(
                args.repo_inspect,
                log,
                cached_only=bool(getattr(args, "cached", False)),
                force_refresh=bool(getattr(args, "refresh", False)),
            )
        return
    if args.scan_repo:
        from src.contrib.repo_scan import build_scan_payload
        from src.core.cli_ui import print_err
        try:
            payload = build_scan_payload(args.scan_repo, log, kind=args.scan_kind)
        except (ScraperError, ValueError) as exc:
            if args.json:
                _print_json_payload(
                    {
                        "action": "repo_scan",
                        "repo": args.scan_repo,
                        "kind": args.scan_kind,
                        "ok": False,
                        "error": str(exc),
                    }
                )
            else:
                print_err(f"cannot scan repo: {exc}")
            return
        if args.json:
            _print_json_payload(payload)
        else:
            print(payload["rendered"])
        return
    if args.contrib_check:
        if args.json:
            _print_json_payload(
                {
                    "action": "contrib_check",
                    "ok": True,
                    "output": _capture_builder_action(check_all_prs, log),
                }
            )
        else:
            check_all_prs(log)
        return
    if args.contrib_respond:
        if args.json:
            _print_json_payload(
                {
                    "action": "contrib_respond",
                    "ok": True,
                    "output": _capture_builder_action(check_pr_feedback, log),
                }
            )
        else:
            check_pr_feedback(log)
        return
    if args.contrib is not None:
        summary = run_contribution_mode(args, log)
        if args.json:
            _print_json_payload(
                {
                    "action": "run",
                    "repo": args.contrib or "",
                    "goal": args.goal,
                    "count": args.count or _legacy_count_arg(raw_argv) or 1,
                    "dry_run": bool(DRY_RUN or args.dry_run),
                    "summary": summary,
                }
            )
        return

    parser.print_help()


if __name__ == "__main__":
    main()
