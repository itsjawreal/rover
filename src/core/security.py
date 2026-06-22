from __future__ import annotations

"""Compatibility security helpers for contribution-engine verification.

This module exists so operator verification commands and any older imports can
still resolve `src.security` after the contribution-engine split. The actual
security-sensitive repo filtering continues to live alongside scraping logic.
"""

from src.github.scraper import RepoCandidate, _metadata_security_ok


def repo_metadata_security_ok(candidate: RepoCandidate) -> bool:
    """Return True when repo metadata does not trip the local suspicious-word filter."""

    return _metadata_security_ok(candidate)


__all__ = ["repo_metadata_security_ok"]
