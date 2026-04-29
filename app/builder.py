#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import re
import sys
from textwrap import dedent

from src.ai import get_usage, reset_usage
from src.command_router import parse_command_text
from src.config import DRY_RUN
from src.doctor import build_doctor_report
from src.fork import (
    ForkError,
    PRAlreadyExistsError,
    fix_and_push_own_repo,
    fork_and_submit_pr,
    get_current_github_login,
)
from src.notify import notify
from src.pr_generator import (
    _PR_TARGETED_ALLOW_BROAD,
    PRGeneratorError,
    build_contribution_report,
    build_repo_inspect_report,
    can_submit_contribution_to_repo,
    check_pr_feedback,
    check_pr_statuses,
    fetch_repo_candidate,
    fetch_repo_candidate_with_scope,
    find_pr_target,
    finish_pr_engine_run,
    generate_pr_improvement,
    get_pr_submitted_repos,
    save_pr_log,
    start_pr_engine_run,
)
from src.scraper import ScraperError
from src.state import cleanup_old_logs, get_security_blacklisted_sources, setup_logging


# ── CLI ──────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="github-contribution-engine",
        description="Find, verify, submit, track, and learn from GitHub contribution PRs.",
        epilog=dedent(
            """\
            Examples:
              python -m app.builder --contrib --1
              python -m app.builder --contrib owner/repo --goal bugfix --1
              python -m app.builder --repo-inspect owner/repo
              python -m app.builder --doctor
            """
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--contrib", "--pr", nargs="?", const="", default=None, help="Run contribution PR mode, optionally against owner/repo or a GitHub URL.")
    parser.add_argument("--contrib-check", "--pr-check", action="store_true", help="Check open PR status and handle maintainer feedback.")
    parser.add_argument("--contrib-respond", "--pr-respond", action="store_true", help="Handle maintainer feedback only.")
    parser.add_argument("--contrib-report", action="store_true", help="Print engine run and queue summary.")
    parser.add_argument("--doctor", action="store_true", help="Run environment and portability checks for this machine.")
    parser.add_argument("--repo-inspect", metavar="OWNER/REPO", help="Fetch and summarize one repo without submitting a PR.")
    parser.add_argument("--goal", choices=["bugfix", "feature_upgrade", "feature_add"], default="bugfix", help="Contribution goal policy to apply.")
    parser.add_argument("--first-pr", action="store_true", help="Bias search mode toward smaller, test-backed repos that are friendlier for a first real PR.")
    parser.add_argument("--count", type=int, default=None, help="Number of PRs to submit in contribution mode.")
    parser.add_argument("--codex", action="store_true", help="Select Codex backend through src.config side effects.")
    parser.add_argument("--claude", action="store_true", help="Select Claude backend through src.config side effects.")
    parser.add_argument("--dry-run", action="store_true", help="Generate and verify changes without submitting a PR.")
    parser.add_argument("--command-text", default="", help="Interpret a natural-language command and map it onto a safe canonical engine action.")
    return parser


def _legacy_count_arg(argv: list[str]) -> int | None:
    for arg in argv:
        match = re.match(r"^--(\d+)$", arg)
        if match:
            return int(match.group(1))
    return None


# ── Contribution mode ────────────────────────────────────────
def run_contribution_mode(args: argparse.Namespace, log: logging.Logger) -> None:
    target = args.count or _legacy_count_arg(sys.argv) or 1
    repo_url = args.contrib or ""
    current_login = ""
    try:
        current_login = get_current_github_login().lower()
    except ForkError:
        current_login = ""
    own_repo = bool(current_login and repo_url and re.search(rf"(?i){re.escape(current_login)}/", repo_url))

    log.info("=" * 55)
    if repo_url:
        log.info("  CONTRIBUTION MODE (targeted): %s - target=%d", repo_url, target)
        if _PR_TARGETED_ALLOW_BROAD:
            log.warning("  targeted broad-repo bypass is active; scope guardrails are relaxed for this pinned repo")
    else:
        log.info("  CONTRIBUTION MODE (search): target=%d contribution PR(s)", target)
    log.info("=" * 55)

    reset_usage()
    start_pr_engine_run("targeted" if repo_url else "search", target)

    submitted = 0
    attempts = 0
    max_attempts = target * 8
    already_prd = get_pr_submitted_repos()
    blacklisted = get_security_blacklisted_sources()

    pinned_candidate = None
    if repo_url:
        try:
            pinned_candidate = fetch_repo_candidate(repo_url, log)
        except ScraperError as exc:
            log.error("Cannot use target repo %r: %s", repo_url, exc)
            log.error("Targeted mode still requires a narrow enough repo and a confident local inspection window.")
            finish_pr_engine_run(0, target, 0, get_usage(), log)
            return
        except Exception as exc:
            log.error("Cannot fetch target repo %r: %s", repo_url, exc)
            finish_pr_engine_run(0, target, 0, get_usage(), log)
            return

    pinned_failures = 0
    while submitted < target and attempts < max_attempts:
        attempts += 1
        log.info("Contribution %d/%d (attempt %d)", submitted + 1, target, attempts)

        if pinned_candidate:
            candidate = pinned_candidate
        else:
            if args.first_pr:
                candidate = find_pr_target(blacklisted, already_prd, log, first_pr_mode=True)
            else:
                candidate = find_pr_target(blacklisted, already_prd, log)
            if not candidate:
                log.warning("No suitable contribution target found after %d attempt(s)", attempts)
                break
            already_prd = already_prd | {candidate.full_name.lower()}

        is_own = own_repo or bool(current_login and candidate.full_name.lower().startswith(f"{current_login}/"))
        log.info(
            "Target: %s (%d stars, %s license, pushed %dd ago)",
            candidate.full_name,
            candidate.stars,
            candidate.license,
            candidate.pushed_days_ago,
        )

        try:
            improvement = generate_pr_improvement(
                candidate,
                log,
                char_budget=20_000 if is_own else 40_000,
                goal=args.goal,
            )
        except PRGeneratorError as exc:
            log.warning("Generation failed for %s: %s", candidate.full_name, exc)
            if pinned_candidate:
                pinned_failures += 1
                if pinned_failures >= 3:
                    log.error("Pinned target failed %d times; stopping", pinned_failures)
                    break
            continue

        log.info("Type: %s | Title: %s", improvement.improvement_type, improvement.title)
        log.info("Rationale: %s", improvement.rationale)
        log.info("Files: %s", list(improvement.changed_files.keys()))

        if not is_own and not can_submit_contribution_to_repo(candidate.full_name):
            log.info("Queueing opportunity for %s because an open PR is already active", candidate.full_name)
            continue

        if DRY_RUN or args.dry_run:
            log.info("[DRY RUN] would submit contribution to %s", candidate.full_name)
            submitted += 1
            continue

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
                log.info("[%d/%d] Own-repo fix pushed: %s", submitted, target, pr_result.pr_url)
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
                log.info("[%d/%d] PR submitted: %s", submitted, target, pr_result.pr_url)

            notify(
                f"[{submitted}/{target}] Contribution submitted\n"
                f"Repo: {candidate.full_name}\n"
                f"Title: {pr_result.pr_title}\n"
                f"URL: {pr_result.pr_url}"
            )
        except PRAlreadyExistsError as exc:
            log.warning("PR already open for %s: %s", candidate.full_name, exc)
        except ForkError as exc:
            log.error("Fork/PR failed for %s: %s", candidate.full_name, exc)

    usage = get_usage()
    log.info(
        "Contribution mode complete - submitted=%d/%d | attempts=%d | calls=%d | ~%d tokens",
        submitted,
        target,
        attempts,
        usage["calls"],
        usage["est_tokens"],
    )
    finish_pr_engine_run(submitted, target, attempts, usage, log)


def inspect_repo(repo: str, log: logging.Logger) -> None:
    try:
        candidate = fetch_repo_candidate_with_scope(repo, log, enforce_scope=False)
    except Exception as exc:
        log.error("Cannot inspect repo %s: %s", repo, exc)
        return

    print(build_repo_inspect_report(candidate))


def _apply_command_text(args: argparse.Namespace, log: logging.Logger) -> None:
    if not args.command_text:
        return
    if any((args.doctor, args.contrib_report, args.repo_inspect, args.contrib_check, args.contrib_respond, args.contrib is not None)):
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

    args.contrib = request.repo if request.repo else ""
    args.count = request.count
    args.goal = request.goal
    args.first_pr = request.first_pr
    args.dry_run = request.dry_run


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    unexpected = [arg for arg in unknown if not re.match(r"^--\d+$", arg)]
    if unexpected:
        parser.error(f"unrecognized arguments: {' '.join(unexpected)}")

    if args.doctor:
        print(build_doctor_report())
        return
    if args.contrib_report:
        print(build_contribution_report())
        return

    log = setup_logging()
    log.info("=" * 55)
    log.info("  GitHub Contribution Engine started")
    log.info("=" * 55)
    cleanup_old_logs()
    _apply_command_text(args, log)

    if args.doctor:
        print(build_doctor_report())
        return
    if args.contrib_report:
        print(build_contribution_report())
        return
    if args.repo_inspect:
        inspect_repo(args.repo_inspect, log)
        return
    if args.contrib_check:
        check_pr_statuses(log)
        check_pr_feedback(log)
        return
    if args.contrib_respond:
        check_pr_feedback(log)
        return
    if args.contrib is not None:
        run_contribution_mode(args, log)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
