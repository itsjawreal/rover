from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from textwrap import dedent

from src.agent_models import get_runtime_profile
from src.fix_planner import plan_fix
from src.issue_analyzer import analyze_repository_issue
from src.patch_generator import generate_patch
from src.pr_writer import prepare_pr
from src.project_inspector import inspect_project
from src.repo_cloner import clone_repository
from src.repo_discovery import discover_repository
from src.run_logger import RunArtifact, save_run_log
from src.state import cleanup_old_logs, setup_logging
from src.validator import validate_patch


def _stage(index: int, total: int, label: str) -> None:
    print(f"[{index}/{total}] {label}...", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="open-source-contributor-agent",
        description="Dry-run oriented autonomous open-source contribution planner.",
        epilog=dedent(
            """\
            Demo:
              python main.py --dry-run
              python main.py --live --repo <repo-url> --issue <issue-url> --dry-run
            """
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", default="", help="Pinned repo URL or owner/repo.")
    parser.add_argument("--issue", default="", help="Optional issue URL to include in the run log. Live issue ingestion is not implemented yet.")
    parser.add_argument("--goal", choices=["bugfix", "feature_upgrade", "feature_add"], default="bugfix")
    parser.add_argument("--first-pr", action="store_true", help="Bias discovery toward smaller test-backed repos.")
    parser.add_argument("--agent-tool", default="", help="Override the user-facing agent tool label for this run.")
    parser.add_argument("--model-series", default="", help="Override the user-facing primary model series label for this run.")
    parser.add_argument("--live", action="store_true", help="Run the live GitHub discovery and AI workflow instead of the deterministic demo flow.")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Prepare the contribution plan without opening a PR.")
    return parser


def _demo_artifact(agent_tool: str, model_series: str, issue_url: str = "", repo: str = "") -> RunArtifact:
    runtime = get_runtime_profile()
    selected_repo = repo or "HKUDS/Vibe-Trading"
    selected_issue = issue_url or "missing_input_validation in agent/backtest/validation.py"
    pr_body = (
        "## Summary\n"
        "Add CLI path validation before running standalone backtest validation so malformed or missing run directories fail clearly.\n\n"
        "## Why it matters\n"
        "Without this guard, operators can hit low-level path errors instead of a clear actionable message on a valid command path.\n\n"
        "## Testing\n"
        "Added focused regression coverage for missing, blank, malformed, and non-directory run_dir inputs.\n"
    )
    return RunArtifact(
        selected_repo=selected_repo,
        selected_issue=selected_issue,
        reason_for_selection=(
            "Selected because the repo is active, test-backed, and exposes a narrow input-validation bug "
            "with a maintainer-friendly two-file patch that fits an acceptance-first contribution workflow."
        ),
        planned_fix=(
            "Introduce a small argument parser for the standalone validation entrypoint, preserve valid behavior, "
            "and add regression coverage in the existing test layout."
        ),
        changed_files=[
            "agent/backtest/validation.py",
            "agent/tests/test_validation_cli.py",
        ],
        validation_result=(
            "passed: engine layout checks, syntax checks, diff safety checks, and focused regression validation all passed "
            "in the dry-run contribution plan."
        ),
        pr_title="fix: validate backtest run_dir CLI input",
        pr_body=pr_body,
        metadata={
            "dry_run": True,
            "mode": "demo",
            "goal": "bugfix",
            "agent_tool": agent_tool,
            "model_series": model_series,
            "backend": runtime.backend,
            "backend_support_level": runtime.support_level,
            "backend_support_note": runtime.support_note,
            "issue_type": "bug",
            "qualification_score": 92,
            "validation_command": "pytest agent/tests/test_validation_cli.py -q",
            "risk_level": "low",
        },
    )


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    log = setup_logging()
    cleanup_old_logs()
    runtime = get_runtime_profile()
    agent_tool = args.agent_tool or runtime.agent_tool
    model_series = args.model_series or runtime.model_series

    if not args.live:
        _stage(1, 5, "Loading deterministic demo contribution")
        artifact = _demo_artifact(agent_tool, model_series, issue_url=args.issue, repo=args.repo)
        _stage(2, 5, "Preparing acceptance-first contribution summary")
    else:
        _stage(1, 6, "Selecting repository")
        if args.issue:
            log.info("Issue URL provided for metadata only; live issue ingestion is not implemented yet.")
        discovery = discover_repository(log, repo=args.repo, first_pr=args.first_pr)
        _stage(2, 6, "Inspecting project structure")
        clone_result = clone_repository(discovery.candidate.url, log) if args.repo else None
        inspection = inspect_project(discovery.candidate, clone_result.checkout_path if clone_result else None)
        _stage(3, 6, "Analyzing contribution opportunity")
        analysis = analyze_repository_issue(discovery.candidate, goal=args.goal)
        _stage(4, 6, "Preparing minimal patch plan")
        fix_plan = plan_fix(discovery.candidate, analysis)
        patch = generate_patch(discovery.candidate, log, goal=args.goal)
        _stage(5, 6, "Validating contribution safety")
        validation = validate_patch(patch, inspection)
        prepared_pr = prepare_pr(fix_plan, patch, validation)
        artifact = RunArtifact(
            selected_repo=discovery.candidate.full_name,
            selected_issue=args.issue or analysis.selected_issue,
            reason_for_selection=discovery.reason,
            planned_fix=fix_plan.planned_fix,
            changed_files=list(patch.improvement.changed_files.keys()),
            validation_result=f"{validation.status}: {validation.summary}",
            pr_title=prepared_pr.title,
            pr_body=prepared_pr.body,
            metadata={
                "dry_run": args.dry_run,
                "mode": "live",
                "goal": args.goal,
                "agent_tool": agent_tool,
                "model_series": model_series,
                "backend": runtime.backend,
                "backend_support_level": runtime.support_level,
                "backend_support_note": runtime.support_note,
                "issue_analysis_reason": analysis.reason,
                "issue_type": analysis.issue_type,
                "qualification_score": analysis.qualification_score,
                "project_inspection": {
                    "language": inspection.language,
                    "package_manager": inspection.package_manager,
                    "test_command": inspection.test_command,
                    "lint_command": inspection.lint_command,
                    "project_structure": inspection.project_structure,
                },
                "clone": {
                    "checkout_path": str(clone_result.checkout_path) if clone_result else "",
                    "note": clone_result.note if clone_result else "Clone skipped in search mode.",
                },
            },
        )
    _stage(3 if not args.live else 6, 5 if not args.live else 6, "Writing run artifacts")
    run_log = save_run_log(artifact)
    if not args.live:
        _stage(4, 5, "Formatting submission-ready output")
        _stage(5, 5, "Done")
    else:
        print("[done] Live dry-run summary ready.", flush=True)

    print(json.dumps(
        {
            "selected_repo": artifact.selected_repo,
            "selected_issue": artifact.selected_issue,
            "reason_for_selection": artifact.reason_for_selection,
            "agent_tool": agent_tool,
            "model_series": model_series,
            "backend": runtime.backend,
            "backend_support_level": runtime.support_level,
            "backend_support_note": runtime.support_note,
            "planned_fix": artifact.planned_fix,
            "changed_files": artifact.changed_files,
            "validation_result": artifact.validation_result,
            "pr_title": artifact.pr_title,
            "pr_body": artifact.pr_body,
            "metadata": artifact.metadata,
            "run_log_json": str(run_log.json_path),
            "run_log_markdown": str(run_log.markdown_path),
        },
        indent=2,
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
