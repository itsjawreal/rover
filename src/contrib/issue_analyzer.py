from __future__ import annotations

from dataclasses import dataclass

from src.contrib.opportunity_engine import Opportunity, PatternScanner, qualify_opportunity
from src.github.scraper import RepoCandidate


@dataclass
class IssueAnalysis:
    issue_type: str
    selected_issue: str
    reason: str
    opportunity: Opportunity
    qualification_score: int


def classify_issue_type(pattern_type: str) -> str:
    mapping = {
        "missing_timeout": "bug",
        "unchecked_response_shape": "bug",
        "missing_input_validation": "bug",
        "resource_cleanup_gap": "bug",
        "unsafe_file_write_or_path": "bug",
        "overbroad_exception_handling": "refactor",
        "missing_regression_test_for_obvious_bugfix": "test",
        "maintainer_todo_feature_upgrade": "small feature",
        "issue_backed_feature_add": "small feature",
    }
    return mapping.get(pattern_type, "bug")


def analyze_repository_issue(candidate: RepoCandidate, goal: str = "bugfix") -> IssueAnalysis:
    scanner = PatternScanner()
    opportunities = scanner.scan(candidate)
    filtered: list[tuple[int, Opportunity]] = []
    for opportunity in opportunities:
        qualification = qualify_opportunity(candidate, opportunity)
        if not qualification.accepted:
            continue
        if goal == "bugfix" and opportunity.opportunity_kind != "bugfix":
            continue
        if goal == "feature_upgrade" and opportunity.opportunity_kind != "feature_upgrade":
            continue
        if goal == "feature_add" and opportunity.opportunity_kind != "feature_add":
            continue
        filtered.append((qualification.score, opportunity))

    if not filtered:
        raise RuntimeError(f"No qualified opportunities found for goal={goal}.")

    score, selected = max(filtered, key=lambda item: item[0])
    issue_type = classify_issue_type(selected.pattern_type)
    return IssueAnalysis(
        issue_type=issue_type,
        selected_issue=f"{selected.pattern_type} in {selected.target_file}",
        reason=selected.failure_mode,
        opportunity=selected,
        qualification_score=score,
    )
