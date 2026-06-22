from __future__ import annotations

"""Convenience exports for contribution engine submodules.

Keeps the PR engine surface in one place while the implementation lives in the
domain modules directly.
"""

from src.contrib.contribution_store import ContributionStore, PREngineStore, PR_ENGINE_DB_FILE
from src.contrib.opportunity_engine import (
    Opportunity,
    PatternScanner,
    QualificationResult,
    qualify_opportunity,
)
from src.analysis.repo_intelligence import RepoShortlister

__all__ = [
    "ContributionStore",
    "Opportunity",
    "PREngineStore",
    "PR_ENGINE_DB_FILE",
    "PatternScanner",
    "QualificationResult",
    "RepoShortlister",
    "qualify_opportunity",
]
