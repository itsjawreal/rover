from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from src.contrib.pr_generator import PRImprovement, generate_pr_improvement
from src.github.scraper import RepoCandidate

_BLOCKED_PATTERNS = ("overbroad_exception_handling",)
_ERROR_CONTEXT_PREFIX = "[SANDBOX_ERROR]"


@dataclass
class PatchPlan:
    improvement: PRImprovement
    sandbox_retry_used: bool = False
    sandbox_outcome: str = ""


def generate_patch(candidate: RepoCandidate, log: logging.Logger, goal: str = "bugfix") -> PatchPlan:
    return PatchPlan(improvement=generate_pr_improvement(candidate, log, goal=goal))


def generate_patch_with_retry(
    candidate: RepoCandidate,
    log: logging.Logger,
    goal: str = "bugfix",
    pattern_type: str = "",
) -> PatchPlan:
    """Generate patch with one self-debug retry if sandbox validation fails.

    Retry is skipped for pattern types that are structurally low-yield.
    Error output from the sandbox is fed back into the candidate description
    so the AI has context for what went wrong.
    """
    from src.contrib.validator import run_sandbox_validation

    initial = generate_patch(candidate, log, goal=goal)
    changed = initial.improvement.changed_files
    if not changed:
        return initial

    sandbox = run_sandbox_validation(changed)
    if sandbox.sandbox_verified:
        return PatchPlan(
            improvement=initial.improvement,
            sandbox_retry_used=False,
            sandbox_outcome="sandbox_verified",
        )

    if not sandbox.sandbox_output:
        return PatchPlan(
            improvement=initial.improvement,
            sandbox_retry_used=False,
            sandbox_outcome="sandbox_infra_failure",
        )

    if pattern_type in _BLOCKED_PATTERNS:
        log.info("Sandbox retry skipped for blocked pattern: %s", pattern_type)
        return PatchPlan(
            improvement=initial.improvement,
            sandbox_retry_used=False,
            sandbox_outcome="sandbox_retry_blocked_pattern",
        )

    log.info("Sandbox failed with actionable error; attempting self-debug retry.")
    error_snippet = sandbox.sandbox_output[:500]
    retry_desc = (
        f"{candidate.description} "
        f"{_ERROR_CONTEXT_PREFIX} Previous patch failed: {error_snippet}"
    )
    retry_candidate = replace(candidate, description=retry_desc)

    try:
        retry = generate_patch(retry_candidate, log, goal=goal)
    except Exception as exc:
        log.warning("Self-debug retry failed: %s", exc)
        return PatchPlan(
            improvement=initial.improvement,
            sandbox_retry_used=True,
            sandbox_outcome="sandbox_retry_failed",
        )

    retry_sandbox = run_sandbox_validation(retry.improvement.changed_files)
    if retry_sandbox.sandbox_verified:
        log.info("Self-debug retry succeeded.")
        return PatchPlan(
            improvement=retry.improvement,
            sandbox_retry_used=True,
            sandbox_outcome="sandbox_retry_success",
        )

    log.info("Self-debug retry still failing; using initial patch.")
    return PatchPlan(
        improvement=initial.improvement,
        sandbox_retry_used=True,
        sandbox_outcome="sandbox_retry_failed",
    )
