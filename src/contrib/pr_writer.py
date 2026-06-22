from __future__ import annotations

from dataclasses import dataclass

from src.contrib.fix_planner import FixPlan
from src.contrib.patch_generator import PatchPlan
from src.contrib.validator import ValidationResult


@dataclass
class PreparedPR:
    title: str
    body: str


def prepare_pr(plan: FixPlan, patch: PatchPlan, validation: ValidationResult) -> PreparedPR:
    body = patch.improvement.body.strip()
    if "## Summary" in body and "## Testing" in body:
        return PreparedPR(title=patch.improvement.title, body=body)

    files = "\n".join(f"- `{path}`" for path in plan.files_changed)
    commands = "\n".join(f"- `{command}`" for command in validation.commands) or "- not available"
    rebuilt = (
        "## Summary\n"
        f"{plan.planned_fix}\n\n"
        "## Why it matters\n"
        f"{patch.improvement.rationale}\n\n"
        "## Files changed\n"
        f"{files}\n\n"
        "## Validation result\n"
        f"- status: {validation.status}\n"
        f"- detail: {validation.summary}\n"
        f"{commands}\n\n"
        "## Risk level\n"
        f"- {plan.risk_level}\n"
    )
    return PreparedPR(title=patch.improvement.title, body=rebuilt)
