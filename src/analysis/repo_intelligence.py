from __future__ import annotations

from src.contrib.contribution_store import ContributionStore
from src.github.scraper import RepoCandidate


class RepoShortlister:
    def __init__(self, store: ContributionStore) -> None:
        self.store = store

    def score(self, candidate: RepoCandidate, base_score: int) -> int:
        live_fit = self.store.repo_live_fit(candidate.full_name)
        fit_bonus = {"live-targeted-ready": 10, "dry-run-only": 0, "inspect-only": -24}.get(
            str(live_fit.get("state", "")),
            0,
        )
        return base_score + self.store.repo_score_adjustment(candidate.full_name) + fit_bonus

    def live_fit(self, candidate: RepoCandidate) -> dict:
        return self.store.repo_live_fit(candidate.full_name)
