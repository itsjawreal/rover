from __future__ import annotations

from dataclasses import dataclass

from src.contrib.issue_analyzer import IssueAnalysis
from src.github.scraper import RepoCandidate


@dataclass
class FixPlan:
    planned_fix: str
    risk_level: str
    files_changed: list[str]
    validation_plan: list[str]


def plan_fix(candidate: RepoCandidate, analysis: IssueAnalysis) -> FixPlan:
    files_changed = [analysis.opportunity.target_file]
    if analysis.opportunity.test_target:
        files_changed.append(analysis.opportunity.test_target)
    risk_level = "low" if analysis.opportunity.patch_scope <= 2 else "medium"
    validation_plan = ["Engine syntax and self-review gates"]
    if analysis.opportunity.test_target:
        validation_plan.append(f"Update or add focused regression coverage in {analysis.opportunity.test_target}")
    return FixPlan(
        planned_fix=(
            f"Patch {analysis.opportunity.target_file} to address {analysis.opportunity.pattern_type}: "
            f"{analysis.reason}"
        ),
        risk_level=risk_level,
        files_changed=files_changed,
        validation_plan=validation_plan,
    )
