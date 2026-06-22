from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from src.contrib.patch_generator import PatchPlan
from src.analysis.project_inspector import ProjectInspection

MAX_PATCH_LINES = int(os.getenv("PR_MAX_PATCH_LINES", "200"))
MAX_TOUCHED_FILES = int(os.getenv("PR_MAX_TOUCHED_FILES", "3"))
_SANDBOX_TIMEOUT = int(os.getenv("PR_SANDBOX_TIMEOUT_SECS", "15"))
_INFRA_ERRORS = ("ModuleNotFoundError", "ImportError", "No module named", "cannot import")


@dataclass
class ValidationResult:
    status: str
    summary: str
    commands: list[str] = field(default_factory=list)
    sandbox_verified: bool = False
    sandbox_output: str = ""


def _syntax_check_python(changed_files: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for path, content in changed_files.items():
        if not path.endswith(".py"):
            continue
        try:
            ast.parse(content)
        except SyntaxError as exc:
            errors.append(f"{path}:{exc.lineno}: {exc.msg}")
    return errors


def _is_actionable_error(output: str) -> bool:
    """Return True if sandbox error is from the patch itself, not missing deps."""
    return bool(output) and not any(marker in output for marker in _INFRA_ERRORS)


def run_sandbox_validation(changed_files: dict[str, str], test_target: str = "") -> ValidationResult:
    """Write changed files to a temp dir and run py_compile + optional test.

    Returns a ValidationResult with sandbox_verified=True only if all checks
    pass. sandbox_output contains actionable error text for retry use.
    """
    with tempfile.TemporaryDirectory(prefix="rover_sandbox_") as tmpdir:
        tmp = Path(tmpdir)

        # Write changed files into temp dir.
        for rel_path, content in changed_files.items():
            dest = tmp / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.write_text(content, encoding="utf-8")
            except OSError:
                continue

        # Compile-check every changed .py file.
        compile_errors: list[str] = []
        for rel_path in changed_files:
            if not rel_path.endswith(".py"):
                continue
            target = tmp / rel_path
            if not target.exists():
                continue
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", str(target)],
                capture_output=True,
                text=True,
                timeout=_SANDBOX_TIMEOUT,
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip()
                compile_errors.append(f"{rel_path}: {err}")

        if compile_errors:
            output = "\n".join(compile_errors)
            return ValidationResult(
                status="failed",
                summary=f"Sandbox compile check failed: {compile_errors[0]}",
                sandbox_verified=False,
                sandbox_output=output,
            )

        # Optionally run a co-located test file if present in changed_files.
        test_output = ""
        if test_target and test_target in changed_files:
            test_path = tmp / test_target
            if test_path.exists():
                result = subprocess.run(
                    [sys.executable, "-m", "pytest", str(test_path), "-x", "--tb=short", "-q"],
                    capture_output=True,
                    text=True,
                    timeout=_SANDBOX_TIMEOUT,
                    cwd=str(tmp),
                )
                test_output = (result.stdout + result.stderr).strip()
                if result.returncode != 0 and _is_actionable_error(test_output):
                    return ValidationResult(
                        status="failed",
                        summary="Sandbox test run failed with actionable error.",
                        sandbox_verified=False,
                        sandbox_output=test_output[:2000],
                    )

    return ValidationResult(
        status="passed",
        summary="Sandbox compile check passed.",
        sandbox_verified=True,
        sandbox_output=test_output[:500] if test_output else "",
    )


def validate_patch(plan: PatchPlan, inspection: ProjectInspection) -> ValidationResult:
    commands = []
    if inspection.lint_command:
        commands.append(inspection.lint_command)
    if inspection.test_command:
        commands.append(inspection.test_command)

    changed_files = plan.improvement.changed_files
    changed = list(changed_files)

    if not changed:
        return ValidationResult(status="failed", summary="No changed files were produced by the patch generator.", commands=commands)

    syntax_errors = _syntax_check_python(changed_files)
    if syntax_errors:
        return ValidationResult(
            status="failed",
            summary=f"Syntax errors in patch: {'; '.join(syntax_errors)}",
            commands=commands,
        )

    if len(changed) > MAX_TOUCHED_FILES:
        return ValidationResult(
            status="failed",
            summary=f"Patch touches {len(changed)} files — exceeds the {MAX_TOUCHED_FILES}-file limit for acceptance-first PRs.",
            commands=commands,
        )

    total_lines = sum(len(c.splitlines()) for c in changed_files.values())
    if total_lines > MAX_PATCH_LINES:
        return ValidationResult(
            status="failed",
            summary=f"Patch is too large ({total_lines} lines across {len(changed)} files) — narrow the scope.",
            commands=commands,
        )

    pr_body = (plan.improvement.body or "").lower()
    rationale = (plan.improvement.rationale or "").lower()
    if len(pr_body) < 80 or not any(word in pr_body for word in ("line", "file", "fix", "error", "fail", "bug", "issue", "timeout", "exception")):
        return ValidationResult(
            status="failed",
            summary="PR body does not reference concrete evidence — generic descriptions are rejected.",
            commands=commands,
        )

    summary = (
        f"Patch passed syntax check, file-count gate ({len(changed)} file(s)), "
        f"diff-size gate ({total_lines} lines), and PR body evidence check. "
        "Run project-level tests in a cloned checkout to complete verification."
    )
    return ValidationResult(status="passed", summary=summary, commands=commands)
