from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.ai import call_ai, _parse_json, _syntax_ok, _syntax_ok_ts, get_scaled_timeout, get_usage
from src.core.config import DATA_DIR, ROVER_ARTIFACT_DIR
from src.contrib.contribution_engine import ContributionEngine
from src.contrib.contribution_store import PREngineStore
from src.contrib.opportunity_engine import (
    Opportunity,
    PatternScanner,
    TEST_FILE_RE,
    expected_test_root,
    guess_test_target,
    qualify_opportunity,
    test_root_for_path,
)
from src.analysis.repo_intelligence import RepoShortlister
from src.github.scraper import (
    RepoCandidate,
    ScraperError,
    _ALLOWED_LICENSES,
    _GITHUB_API,
    _MAX_REPO_FILES,
    _gh_get,
    _get_license,
    _metadata_security_ok,
    download_repo_files,
    fetch_maintainer_signals,
    fetch_file_git_history,
)

_GITHUB_URL_RE = re.compile(
    r"(?:https?://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def resolve_repo_full_name(repo_url: str) -> str:
    m = _GITHUB_URL_RE.match(repo_url.strip())
    if not m:
        raise ScraperError(f"Cannot parse repo URL/name: {repo_url!r}")
    return m.group(1)


def fetch_repo_metadata(repo_url: str, log: logging.Logger) -> tuple[str, dict]:
    full_name = resolve_repo_full_name(repo_url)
    log.info("Fetching repo metadata: %s", full_name)
    try:
        data = _gh_get(f"{_GITHUB_API}/repos/{full_name}")
    except Exception as exc:
        raise ScraperError(f"GitHub API error for {full_name}: {exc}") from exc
    return full_name, data


log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────
PR_LOG_FILE    = DATA_DIR / "pr_log.json"


class _LazyEngineStore:
    def __init__(self) -> None:
        self._store: PREngineStore | None = None

    def _get_store(self) -> PREngineStore:
        if self._store is None:
            self._store = PREngineStore()
        return self._store

    def __getattr__(self, name: str):
        return getattr(self._get_store(), name)


_ENGINE_STORE = _LazyEngineStore()
_CONTRIBUTION_ENGINE: ContributionEngine | None = None
_CONTRIBUTION_ENGINE_STORE_REF: object | None = None
_PATTERN_SCANNER = PatternScanner()
_ACTIVE_RUN_ID: int | None = None

_PR_MIN_STARS  = int(os.getenv("PR_MIN_STARS",  "300"))
_PR_MAX_STARS  = int(os.getenv("PR_MAX_STARS",  "6000"))
_PR_MAX_PUSHED = int(os.getenv("PR_MAX_PUSHED_DAYS", "45"))
_PR_MIN_ISSUES = int(os.getenv("PR_MIN_OPEN_ISSUES", "1"))

_PR_MIN_FORKS = int(os.getenv("PR_MIN_FORKS", "20"))
_PR_MAX_TOTAL_FILES = int(os.getenv("PR_MAX_TOTAL_FILES", "130"))
_PR_MAX_PY_FILES = int(os.getenv("PR_MAX_PY_FILES", "75"))
_PR_ACCEPTANCE_SHORTLIST = int(os.getenv("PR_ACCEPTANCE_SHORTLIST", "6"))
_PR_TARGETED_MAX_TOTAL_FILES = int(os.getenv("PR_TARGETED_MAX_TOTAL_FILES", str(_PR_MAX_TOTAL_FILES)))
_PR_TARGETED_MAX_PY_FILES = int(os.getenv("PR_TARGETED_MAX_PY_FILES", str(_PR_MAX_PY_FILES)))
_PR_TARGETED_ALLOW_BROAD = os.getenv("PR_TARGETED_ALLOW_BROAD", "").strip().lower() in {"1", "true", "yes", "on"}
_PR_RECON_ENABLED = os.getenv("PR_RECON_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
_FIRST_PR_MODE_DEFAULT = os.getenv("CONTRIB_FIRST_PR_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
_FIRST_PR_MAX_STARS = int(os.getenv("FIRST_PR_MAX_STARS", "2500"))
_FIRST_PR_MAX_PUSHED_DAYS = int(os.getenv("FIRST_PR_MAX_PUSHED_DAYS", "30"))
_FIRST_PR_MAX_TOTAL_FILES = int(os.getenv("FIRST_PR_MAX_TOTAL_FILES", "90"))
_FIRST_PR_MAX_PY_FILES = int(os.getenv("FIRST_PR_MAX_PY_FILES", "45"))
_FIRST_PR_MIN_TEST_FILES = int(os.getenv("FIRST_PR_MIN_TEST_FILES", "1"))
_LANE_PRESETS: dict[str, dict[str, object]] = {
    "general": {
        "keywords": set(),
        "queries": [
            ("python", "python cli"),
            ("python", "python sdk"),
            ("python", "python api client"),
            ("python", "python developer tool"),
            ("python", "python automation library"),
            ("python", "python integration tool"),
            ("python", "python package utility"),
            ("typescript", "typescript cli"),
            ("typescript", "typescript sdk"),
            ("typescript", "typescript api client"),
            ("typescript", "typescript developer tool"),
            ("typescript", "typescript automation library"),
        ],
    },
    "crypto": {
        "keywords": {
            "crypto", "blockchain", "defi", "dex", "web3", "ethereum", "solana",
            "bitcoin", "evm", "token", "wallet", "nft", "uniswap", "trading",
            "on-chain", "onchain", "swap", "yield", "staking", "mev",
        },
        "queries": [
            ("python", "crypto python sdk"),
            ("python", "web3 python library"),
            ("python", "defi python client"),
            ("typescript", "crypto typescript sdk"),
            ("typescript", "web3 typescript library"),
            ("typescript", "defi typescript client"),
        ],
    },
    "devtools": {
        "keywords": {"cli", "developer", "tooling", "sdk", "library", "plugin", "mcp", "automation"},
        "queries": [
            ("python", "python developer tool"),
            ("python", "python cli utility"),
            ("python", "python mcp server"),
            ("typescript", "typescript developer tool"),
            ("typescript", "typescript cli utility"),
            ("typescript", "typescript mcp server"),
        ],
    },
    "frontend": {
        "keywords": {"react", "nextjs", "frontend", "ui", "design-system", "component", "typescript"},
        "queries": [
            ("typescript", "react typescript library"),
            ("typescript", "nextjs typescript app"),
            ("typescript", "frontend component library"),
            ("typescript", "design system typescript"),
        ],
    },
    "data": {
        "keywords": {"data", "etl", "pipeline", "analytics", "warehouse", "ingestion", "python"},
        "queries": [
            ("python", "python data pipeline"),
            ("python", "python analytics library"),
            ("python", "python etl tool"),
            ("python", "python ingestion framework"),
        ],
    },
    "infra": {
        "keywords": {"devops", "infra", "deployment", "kubernetes", "terraform", "ops", "automation"},
        "queries": [
            ("python", "python devops tool"),
            ("python", "python deployment automation"),
            ("typescript", "typescript devops tool"),
            ("typescript", "infrastructure automation typescript"),
        ],
    },
    "ml": {
        "keywords": {"ml", "machine-learning", "llm", "rag", "inference", "dataset", "training"},
        "queries": [
            ("python", "python machine learning tool"),
            ("python", "python llm framework"),
            ("python", "python rag library"),
            ("typescript", "typescript llm sdk"),
        ],
    },
    "docs": {
        "keywords": {"documentation", "docs", "markdown", "static-site", "docgen"},
        "queries": [
            ("python", "python documentation tool"),
            ("typescript", "typescript documentation tool"),
            ("typescript", "markdown static site"),
        ],
    },
}
_DEFAULT_LANE_NAME = "general"


def _parse_csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _normalize_queries(raw_queries: list[str]) -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    for raw in raw_queries:
        if ":" in raw:
            lang, query = raw.split(":", 1)
            lang = lang.strip().lower()
            query = query.strip()
        else:
            lang = "python"
            query = raw.strip()
        if lang in {"python", "typescript"} and query:
            queries.append((lang, query))
    return queries


def _get_lane_preset(lane: str) -> dict[str, object]:
    normalized = (lane or _DEFAULT_LANE_NAME).strip().lower()
    return _LANE_PRESETS.get(normalized, _LANE_PRESETS[_DEFAULT_LANE_NAME])


def _get_configured_lane_name() -> str:
    return os.getenv("CONTRIB_LANE", _DEFAULT_LANE_NAME).strip().lower() or _DEFAULT_LANE_NAME


def _load_pr_search_queries() -> list[tuple[str, str]]:
    raw_queries = _parse_csv_env("CONTRIB_SEARCH_QUERIES")
    if raw_queries:
        queries = _normalize_queries(raw_queries)
        if queries:
            return queries
    preset = _get_lane_preset(_get_configured_lane_name())
    return list(preset.get("queries", []))


def _load_lane_keywords() -> set[str]:
    configured = {item.lower() for item in _parse_csv_env("CONTRIB_TOPIC_KEYWORDS")}
    if configured:
        return configured
    preset = _get_lane_preset(_get_configured_lane_name())
    return set(preset.get("keywords", set()))


_PR_SEARCH_QUERIES = _load_pr_search_queries()
_LANE_KEYWORDS = _load_lane_keywords()
_LANE_NAME = _get_configured_lane_name()
_FEATURE_LABELS = {"enhancement", "feature", "help wanted", "good first issue"}
_BUG_LABELS = {"bug", "defect", "fix", "regression", "good first issue", "help wanted"}
_GOOD_FIRST_ISSUE_LABELS = {"good first issue", "good-first-issue", "beginner", "starter"}
_REPRO_MARKERS = (
    "traceback", "error:", "exception:", "stacktrace", "stack trace",
    "steps to reproduce", "to reproduce", "repro", "expected behavior",
    "actual behavior", "how to reproduce", "```", "assert", "raises",
)
_GIT_REVERT_MARKERS = ("revert", "reverts", "rollback", "roll back", "undo", "undone")
_GIT_FRAGILE_MARKERS = ("hotfix", "hot fix", "critical", "emergency", "broken", "regression")


def _build_git_history_section(
    candidate: RepoCandidate,
    target_file: str,
    log: logging.Logger,
) -> tuple[str, bool]:
    """Fetch git history for target_file and return (formatted_section, has_revert_history).

    has_revert_history=True means this file was previously reverted — patch with caution
    and downgrade execution_mode to live-review.
    """
    commits = fetch_file_git_history(candidate, target_file, max_commits=10)
    if not commits:
        return "", False

    has_revert = False
    lines = [f"\nGit history for `{target_file}` (last {len(commits)} commit(s)):"]
    for commit in commits:
        sha = str(commit.get("sha", ""))[:7]
        info = commit.get("commit", {})
        message = str(info.get("message", "")).split("\n")[0][:120]
        author = (info.get("author") or {}).get("name", "unknown")
        date = str((info.get("author") or {}).get("date", ""))[:10]
        lower_msg = message.lower()
        if any(m in lower_msg for m in _GIT_REVERT_MARKERS):
            has_revert = True
            lines.append(f"  [{sha}] REVERT: {message} ({author}, {date})")
        elif any(m in lower_msg for m in _GIT_FRAGILE_MARKERS):
            lines.append(f"  [{sha}] HOTFIX: {message} ({author}, {date})")
        else:
            lines.append(f"  [{sha}] {message} ({author}, {date})")

    if has_revert:
        lines.append(
            "  WARNING: This file has revert history — patch conservatively "
            "and prefer additive changes over behavioral rewrites."
        )
        log.info("Revert history detected for %s in %s — will downgrade to live-review.", target_file, candidate.full_name)

    return "\n".join(lines) + "\n", has_revert
_NEGATIVE_PR_SIGNAL_RE = re.compile(
    r"\b(ai|bot|generated|spam|unsolicited|no tests?|missing tests?|failing tests?|ci failed|too broad|low quality)\b",
    re.IGNORECASE,
)
_TARGETED_SHORTLIST_LIMIT = int(os.getenv("PR_TARGETED_SHORTLIST_LIMIT", "2"))
_TARGETED_VIABLE_LIMIT = int(os.getenv("PR_TARGETED_VIABLE_LIMIT", "1"))
_TARGETED_PLAN_ATTEMPTS = int(os.getenv("PR_TARGETED_PLAN_ATTEMPTS", "2"))
_TARGETED_GENERATE_ATTEMPTS = int(os.getenv("PR_TARGETED_GENERATE_ATTEMPTS", "2"))
_TARGETED_SELF_REVIEW_RETRIES = int(os.getenv("PR_TARGETED_SELF_REVIEW_RETRIES", "1"))
_TARGETED_MAX_CHANGED_FILES = int(os.getenv("PR_TARGETED_MAX_CHANGED_FILES", "2"))
_TARGETED_MAX_DIFF_LINES = int(os.getenv("PR_TARGETED_MAX_DIFF_LINES", "120"))
_TARGETED_MIN_PATCHABILITY_SCORE = int(os.getenv("PR_TARGETED_MIN_PATCHABILITY_SCORE", "72"))
_TARGETED_PATTERN_GENERATE_BUDGETS = {
    "overbroad_exception_handling": 1,
    "resource_cleanup_gap": 1,
    "missing_input_validation": 2,
    "unchecked_response_shape": 2,
    "missing_timeout": 2,
}
_TARGETED_PATTERN_REVIEW_BUDGETS = {
    "overbroad_exception_handling": 1,
    "resource_cleanup_gap": 1,
    "missing_input_validation": 2,
    "unchecked_response_shape": 2,
    "missing_timeout": 2,
}
_CORE_TARGET_FILE_RE = re.compile(r"(^|/)(cli|main|app|core|index|__init__)\.(py|ts|tsx|js)$", re.IGNORECASE)
_TARGETED_POLICY_SURFACE_RE = re.compile(r"(^|/)(context_system|config|settings|loader|parser|middleware|hooks?)(/|$)", re.IGNORECASE)


def _get_contribution_engine() -> ContributionEngine:
    global _CONTRIBUTION_ENGINE, _CONTRIBUTION_ENGINE_STORE_REF
    if _CONTRIBUTION_ENGINE is None or _CONTRIBUTION_ENGINE_STORE_REF is not _ENGINE_STORE:
        _CONTRIBUTION_ENGINE = ContributionEngine(_ENGINE_STORE)
        _CONTRIBUTION_ENGINE_STORE_REF = _ENGINE_STORE
    return _CONTRIBUTION_ENGINE


# ── Exceptions ────────────────────────────────────────────────
class PRGeneratorError(Exception):
    """Raised when AI fails to produce a valid, diffable improvement."""


# ── Data models ───────────────────────────────────────────────
@dataclass
class PRImprovement:
    title: str
    body: str
    improvement_type: str
    changed_files: dict[str, str]
    rationale: str
    opportunity_id: int | None = None
    target_file: str = ""
    pattern_type: str = ""
    patch_plan: dict[str, object] | None = None
    execution_mode: str = "live-safe"


@dataclass
class PatchPlan:
    target_file: str
    failure_mode: str
    expected_files: list[str]
    test_target: str
    why_narrow: str
    proof_path: str


@dataclass(frozen=True)
class PatternSubmitPolicy:
    mode: str
    max_changed_files: int
    max_diff_lines: int
    require_test_target: bool = False
    banned_surface_re: re.Pattern[str] | None = None
    semantic_review_enough: bool = True
    summary: str = ""


@dataclass(frozen=True)
class PatchShape:
    risk: str
    reason: str = ""


_PATCH_PLAN_DRIFT_MARKERS = (
    "Patch plan drifted away from the chosen target file",
    "Patch plan does not include the chosen target file",
)


_ACTIVE_RUN_METRICS: dict[str, object] = {}
_TARGETED_PATTERN_POLICIES: dict[str, PatternSubmitPolicy] = {
    "missing_input_validation": PatternSubmitPolicy(
        mode="auto_live_safe",
        max_changed_files=2,
        max_diff_lines=120,
        require_test_target=False,
        summary="validation guard patch",
    ),
    "unchecked_response_shape": PatternSubmitPolicy(
        mode="auto_live_safe",
        max_changed_files=2,
        max_diff_lines=120,
        require_test_target=True,
        summary="response shape guard patch",
    ),
    "missing_timeout": PatternSubmitPolicy(
        mode="auto_live_safe",
        max_changed_files=2,
        max_diff_lines=120,
        require_test_target=False,
        summary="timeout hardening patch",
    ),
    "resource_cleanup_gap": PatternSubmitPolicy(
        mode="dry_run_first",
        max_changed_files=2,
        max_diff_lines=100,
        require_test_target=True,
        semantic_review_enough=False,
        summary="resource cleanup patch",
    ),
    "overbroad_exception_handling": PatternSubmitPolicy(
        mode="blocked_for_targeted_live",
        max_changed_files=1,
        max_diff_lines=80,
        require_test_target=True,
        banned_surface_re=_TARGETED_POLICY_SURFACE_RE,
        summary="exception-policy patch",
    ),
    "issue_backed_bugfix": PatternSubmitPolicy(
        mode="auto_live_safe",
        max_changed_files=2,
        max_diff_lines=150,
        require_test_target=False,
        semantic_review_enough=True,
        summary="maintainer-reported bug fix from open issue",
    ),
}


def _reset_active_run_metrics() -> None:
    _ACTIVE_RUN_METRICS.clear()
    _ACTIVE_RUN_METRICS.update(
        {
            "shortlisted": 0,
            "planned": 0,
            "generated": 0,
            "self_review_rejected": 0,
            "broad_rejected_early": 0,
            "shape_rejected_early": 0,
            "manual_review_queued": 0,
            "token_spend_by_stage": {"plan": 0, "generate": 0, "review": 0},
            "shortlist_summary": [],
            "last_patch_plan": None,
            "min_patchability_score": _TARGETED_MIN_PATCHABILITY_SCORE,
            "best_patchability_score": 0,
            "current_stage": "qualify",
            "current_candidate": "",
            "current_target_file": "",
            "current_pattern_type": "",
            "current_pattern_policy": "",
            "outcome_code": "",
            "death_stage": "",
            "candidate_history": [],
            "token_spend_by_pattern": {},
            "seen_title_families": [],
            "seen_exception_policy_wording": False,
        }
    )


def _usage_tokens() -> int:
    return int(get_usage().get("est_tokens", 0))


def _record_stage_token_spend(stage: str, before_tokens: int) -> None:
    after_tokens = _usage_tokens()
    delta = max(0, after_tokens - before_tokens)
    stage_map = _ACTIVE_RUN_METRICS.setdefault("token_spend_by_stage", {})
    stage_map[stage] = int(stage_map.get(stage, 0)) + delta
    pattern = str(_ACTIVE_RUN_METRICS.get("current_pattern_type") or "")
    if pattern:
        pattern_map = _ACTIVE_RUN_METRICS.setdefault("token_spend_by_pattern", {})
        pattern_map[pattern] = int(pattern_map.get(pattern, 0)) + delta


def _bump_run_metric(name: str, amount: int = 1) -> None:
    _ACTIVE_RUN_METRICS[name] = int(_ACTIVE_RUN_METRICS.get(name, 0)) + amount


def _consume_active_run_metrics() -> dict[str, object]:
    snapshot = dict(_ACTIVE_RUN_METRICS)
    snapshot["token_spend_by_stage"] = dict(_ACTIVE_RUN_METRICS.get("token_spend_by_stage", {}))
    snapshot["token_spend_by_pattern"] = dict(_ACTIVE_RUN_METRICS.get("token_spend_by_pattern", {}))
    snapshot["shortlist_summary"] = list(_ACTIVE_RUN_METRICS.get("shortlist_summary", []))
    snapshot["candidate_history"] = list(_ACTIVE_RUN_METRICS.get("candidate_history", []))
    snapshot["seen_title_families"] = list(_ACTIVE_RUN_METRICS.get("seen_title_families", []))
    return snapshot


def _set_run_stage(stage: str, candidate_full_name: str = "", details: dict[str, object] | None = None) -> None:
    _ACTIVE_RUN_METRICS["current_stage"] = stage
    if candidate_full_name:
        _ACTIVE_RUN_METRICS["current_candidate"] = candidate_full_name
    details = details or {}
    if details.get("target_file"):
        _ACTIVE_RUN_METRICS["current_target_file"] = str(details["target_file"])
    if details.get("pattern_type"):
        _ACTIVE_RUN_METRICS["current_pattern_type"] = str(details["pattern_type"])
    if details.get("pattern_policy"):
        _ACTIVE_RUN_METRICS["current_pattern_policy"] = str(details["pattern_policy"])
    history = _ACTIVE_RUN_METRICS.setdefault("candidate_history", [])
    if candidate_full_name and stage in {"qualify", "plan", "generate", "review", "submit"}:
        history.append(
            {
                "stage": stage,
                "repo": candidate_full_name,
                "target_file": details.get("target_file", ""),
                "pattern_type": details.get("pattern_type", ""),
                "pattern_policy": details.get("pattern_policy", ""),
            }
        )
    if _ACTIVE_RUN_ID is not None:
        _ENGINE_STORE.record_repo_event(
            _ACTIVE_RUN_ID,
            candidate_full_name or str(_ACTIVE_RUN_METRICS.get("current_candidate") or ""),
            "run_stage",
            f"Run stage: {stage}",
            {"stage": stage, **details},
        )


def start_pr_engine_run(mode: str, target_count: int, external_run_id: str = "") -> int:
    global _ACTIVE_RUN_ID
    _reset_active_run_metrics()
    _ACTIVE_RUN_ID = _get_contribution_engine().start_run(
        mode=mode,
        target_count=target_count,
        external_run_id=external_run_id,
    )
    return _ACTIVE_RUN_ID


def finish_pr_engine_run(
    submitted: int,
    target: int,
    attempts: int,
    usage: dict[str, int],
    log: logging.Logger,
) -> dict:
    return _get_contribution_engine().finish_run(
        submitted,
        target,
        attempts,
        usage,
        log,
        extra_summary=_consume_active_run_metrics(),
    )


def can_submit_contribution_to_repo(full_name: str) -> bool:
    return _get_contribution_engine().can_submit_to_repo(full_name)


def build_contribution_report(limit: int = 5) -> str:
    return _get_contribution_engine().build_operator_report(limit=limit)


def get_contribution_report_data(limit: int = 5) -> tuple[list[dict], list[dict]]:
    return _get_contribution_engine().get_report_data(limit=limit)


def _format_compact_count(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".rstrip("0").rstrip(".")
    if value >= 1_000:
        return f"{value / 1_000:.1f}k".rstrip("0").rstrip(".")
    return str(value)


def _count_repo_files(files: dict[str, str]) -> tuple[int, int, int, bool]:
    py_count = sum(1 for f in files if f.endswith(".py"))
    ts_count = sum(1 for f in files if f.endswith((".ts", ".tsx")))
    test_count = sum(
        1
        for f in files
        if "test" in f.lower().replace("\\", "/")
    )
    has_tests = test_count > 0
    return py_count, ts_count, test_count, has_tests


def _matches_contribution_lane(candidate: RepoCandidate) -> bool:
    if not _LANE_KEYWORDS:
        return True
    haystack = " ".join(
        [
            candidate.name.lower(),
            (candidate.description or "").lower(),
            " ".join(candidate.topics).lower(),
        ]
    )
    return any(keyword in haystack for keyword in _LANE_KEYWORDS)


def _validate_candidate_scope(
    candidate: RepoCandidate,
    *,
    targeted: bool,
    override_limits: bool = False,
) -> tuple[int, int, int]:
    py_count, ts_count, test_count, _has_tests = _count_repo_files(candidate.files)
    total_files = len(candidate.files)
    max_total = _PR_TARGETED_MAX_TOTAL_FILES if targeted else _PR_MAX_TOTAL_FILES
    max_py = _PR_TARGETED_MAX_PY_FILES if targeted else _PR_MAX_PY_FILES
    allow_broad = override_limits or (targeted and _PR_TARGETED_ALLOW_BROAD)

    if py_count == 0 and ts_count == 0:
        raise ScraperError(f"No Python or TypeScript files found in {candidate.full_name}")
    if not allow_broad and total_files > max_total:
        raise ScraperError(
            f"Repo too broad for a narrow contribution run ({total_files} downloaded files > {max_total} allowed)."
        )
    if not allow_broad and py_count > max_py:
        raise ScraperError(
            f"Python surface too broad for a narrow contribution run (py={py_count} > {max_py} allowed)."
        )
    if targeted and not allow_broad and total_files >= _MAX_REPO_FILES:
        raise ScraperError(
            "Downloaded file set hit the configured cap, so the repo is only partially inspected. "
            "Choose a smaller repo or raise the inspection limits deliberately."
        )

    return py_count, ts_count, test_count


def get_repo_inspect_data(candidate: RepoCandidate) -> dict[str, object]:
    py_count, ts_count, test_count, _has_tests = _count_repo_files(candidate.files)
    lane_match = _matches_contribution_lane(candidate)
    first_pr_friendly, first_pr_reason = _first_pr_repo_fit(candidate, candidate.files)
    live_fit = _ENGINE_STORE.repo_live_fit(candidate.full_name)

    search_scope = "search-ready"
    targeted_scope = "targeted-ready"
    scope_notes: list[str] = []

    if candidate.archived:
        search_scope = "inspect-only"
        targeted_scope = "inspect-only"
        scope_notes.append("repo is archived on GitHub and should not receive contribution runs")
    elif candidate.disabled:
        search_scope = "inspect-only"
        targeted_scope = "inspect-only"
        scope_notes.append("repo is disabled on GitHub and cannot receive contribution runs")
    elif candidate.pushed_days_ago > _PR_MAX_PUSHED:
        targeted_scope = "inspect-only"
        scope_notes.append(
            f"targeted mode: inactive repo ({candidate.pushed_days_ago}d since last push; limit {_PR_MAX_PUSHED}d)"
        )

    if search_scope != "inspect-only":
        try:
            _validate_candidate_scope(candidate, targeted=False)
        except ScraperError as exc:
            search_scope = "too broad for search mode"
            scope_notes.append(f"search mode: {exc}")

    if targeted_scope not in {"inspect-only"}:
        try:
            _validate_candidate_scope(candidate, targeted=True)
        except ScraperError as exc:
            targeted_scope = "inspect-only unless targeted override is enabled"
            scope_notes.append(f"targeted mode: {exc}")

    live_fit_state = str(live_fit.get("state", "dry-run-only"))
    if targeted_scope == "targeted-ready":
        targeted_scope = live_fit_state
        if live_fit_state != "live-targeted-ready":
            scope_notes.append(f"live targeted auto-submit: {live_fit_state}")

    next_steps: list[str] = []
    if targeted_scope == "live-targeted-ready":
        next_steps.append(
            f"Run `python -m app.builder --contrib {candidate.full_name} --1` for a pinned contribution attempt."
        )
    elif targeted_scope == "dry-run-only":
        next_steps.append(
            f"Run `python -m app.builder --contrib {candidate.full_name} --1 --dry-run` before any live PR."
        )
    elif targeted_scope == "inspect-only":
        next_steps.append(
            f"Keep this repo in inspect-only mode. Use `rover inspect {candidate.full_name}` to review fit, but do not run a pinned contribution attempt."
        )
    elif _PR_TARGETED_ALLOW_BROAD:
        next_steps.append(
            f"Targeted broad-repo override is enabled, so `python -m app.builder --contrib {candidate.full_name} --1` can still proceed with extra caution."
        )
    else:
        next_steps.append(
            "Use inspect mode to study this repo first, or raise targeted breadth limits deliberately in `.env` before attempting a pinned PR."
        )

    if not lane_match:
        next_steps.append(
            f"Consider switching `CONTRIB_LANE` if this repo is intentionally outside the current `{_LANE_NAME}` niche."
        )

    return {
        "repo": candidate.full_name,
        "url": candidate.url,
        "stars": _format_compact_count(candidate.stars),
        "forks": _format_compact_count(candidate.forks),
        "license": candidate.license,
        "pushed_days_ago": candidate.pushed_days_ago,
        "files": len(candidate.files),
        "py": py_count,
        "ts": ts_count,
        "tests": test_count,
        "lane_name": _LANE_NAME,
        "lane_match": lane_match,
        "first_pr_friendly": first_pr_friendly,
        "first_pr_reason": first_pr_reason,
        "search_scope": search_scope,
        "targeted_scope": targeted_scope,
        "live_fit_score": live_fit.get("score", 0),
        "live_fit_reasons": list(live_fit.get("reasons", [])),
        "description": candidate.description.strip(),
        "topics": list(candidate.topics),
        "scope_notes": scope_notes,
        "next_steps": next_steps,
        "first_pr_label": "good fit" if first_pr_friendly else "needs caution",
    }


def build_repo_inspect_report_from_data(data: dict[str, object]) -> str:
    lines = [
        "Repo Inspect",
        "============",
        "",
        f"Repo: {data['repo']}",
        f"URL: {data['url']}",
        f"Stats: stars={data['stars']} forks={data['forks']} license={data['license']} pushed={data['pushed_days_ago']}d ago",
        f"Surface: files={data['files']} py={data['py']} ts={data['ts']} tests={data['tests']}",
        f"Lane fit: configured lane `{data['lane_name']}` is {'matched' if data['lane_match'] else 'not matched'}",
        f"First-PR fit: {'friendly' if data['first_pr_friendly'] else 'not ideal'} ({data['first_pr_reason']})",
        f"Scope fit: search={data['search_scope']} | targeted={data['targeted_scope']}",
        f"Live fit: score={data.get('live_fit_score', 0)}",
    ]

    if data["description"]:
        lines.extend(["", "Description:", str(data["description"])])

    if data["topics"]:
        lines.extend(["", "Topics:", ", ".join(data["topics"])])

    if data["scope_notes"]:
        lines.extend(["", "Scope notes:"])
        lines.extend(f"- {note}" for note in data["scope_notes"])

    if data.get("live_fit_reasons"):
        lines.extend(["", "Live-fit reasons:"])
        lines.extend(f"- {note}" for note in data["live_fit_reasons"])

    lines.extend(["", "Suggested next step:"])
    lines.extend(f"- {step}" for step in data["next_steps"])

    return "\n".join(lines)


def build_repo_inspect_report(candidate: RepoCandidate) -> str:
    return build_repo_inspect_report_from_data(get_repo_inspect_data(candidate))


def write_repo_inspect_artifact(data: dict[str, object]) -> Path:
    repo_slug = str(data.get("repo", "unknown")).replace("/", "__")
    artifact_dir = ROVER_ARTIFACT_DIR / "inspect" / repo_slug
    artifact_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    artifact_path = artifact_dir / f"{timestamp}.md"
    latest_path = artifact_dir / "latest.md"
    content = build_repo_inspect_report_from_data(data)
    artifact_path.write_text(content, encoding="utf-8")
    latest_path.write_text(content, encoding="utf-8")
    return artifact_path


def _acceptance_score(candidate: RepoCandidate, files: dict[str, str]) -> int:
    """Higher score = more likely to produce a small, acceptable PR quickly."""
    py_count, ts_count, test_count, has_tests = _count_repo_files(files)
    total_files = len(files)

    score = 0
    score += min(candidate.stars // 200, 20)
    score += min(candidate.forks // 50, 10)
    score += 12 if has_tests else -8
    score += min(test_count, 10)

    # Prefer smaller, library-like repos over broad frameworks.
    if total_files <= 60:
        score += 18
    elif total_files <= 100:
        score += 8
    elif total_files > _PR_MAX_TOTAL_FILES:
        score -= 20

    if py_count <= 25:
        score += 12
    elif py_count <= 45:
        score += 5
    elif py_count > _PR_MAX_PY_FILES:
        score -= 15

    desc = (candidate.description or "").lower()
    name = candidate.name.lower()
    haystack = " ".join([name, desc, " ".join(candidate.topics).lower()])

    easy_win_markers = (
        "cli", "sdk", "client", "mcp", "tool", "utils", "wrapper", "api",
    )
    framework_markers = (
        "framework", "plugin system", "ecosystem", "platform", "gateway",
    )
    if any(marker in haystack for marker in easy_win_markers):
        score += 8
    if any(marker in haystack for marker in framework_markers):
        score -= 8

    # License clarity — unclear/missing license reduces maintainer trust signal
    lic = (candidate.license or "").lower().strip()
    if lic and lic not in ("", "none", "unknown", "other"):
        score += 3
    else:
        score -= 5

    # Dependency file presence — signals maintained project with defined dev workflow
    if files.get("requirements.txt") or files.get("pyproject.toml") or files.get("package.json"):
        score += 4

    return score


def _first_pr_repo_fit(candidate: RepoCandidate, files: dict[str, str]) -> tuple[bool, str]:
    py_count, _ts_count, test_count, _has_tests = _count_repo_files(files)
    total_files = len(files)

    if candidate.stars > _FIRST_PR_MAX_STARS:
        return False, f"stars {candidate.stars} > {_FIRST_PR_MAX_STARS}"
    if candidate.pushed_days_ago > _FIRST_PR_MAX_PUSHED_DAYS:
        return False, f"pushed {candidate.pushed_days_ago}d ago > {_FIRST_PR_MAX_PUSHED_DAYS}d"
    if total_files > _FIRST_PR_MAX_TOTAL_FILES:
        return False, f"files {total_files} > {_FIRST_PR_MAX_TOTAL_FILES}"
    if py_count > _FIRST_PR_MAX_PY_FILES:
        return False, f"py files {py_count} > {_FIRST_PR_MAX_PY_FILES}"
    if test_count < _FIRST_PR_MIN_TEST_FILES:
        return False, f"tests {test_count} < {_FIRST_PR_MIN_TEST_FILES}"
    return True, "small active repo with test coverage"


def _issue_labels(issue: dict) -> set[str]:
    labels = set()
    for label in issue.get("labels", []):
        if isinstance(label, dict):
            name = str(label.get("name", "")).strip().lower()
        else:
            name = str(label).strip().lower()
        if name:
            labels.add(name)
    return labels


def _match_issue_target_file(candidate: RepoCandidate, issue_text: str) -> str:
    normalized = issue_text.lower()
    direct_matches = [path for path in candidate.files if path.lower() in normalized]
    if len(direct_matches) == 1:
        return direct_matches[0]

    basename_matches = []
    for path in candidate.files:
        base = path.replace("\\", "/").split("/")[-1].lower()
        if base and base in normalized:
            basename_matches.append(path)
    unique = sorted(set(basename_matches))
    if len(unique) == 1:
        return unique[0]
    return ""


def _issue_has_repro_steps(body: str) -> bool:
    """Return True if issue body contains concrete repro / error evidence."""
    lower = body.lower()
    return any(marker in lower for marker in _REPRO_MARKERS)


def _issue_rank_score(issue: dict, labels: set[str]) -> int:
    """Score an issue for contribution priority (higher = better)."""
    score = 0
    # Recency: updated recently = more likely still relevant
    import datetime
    updated_at = issue.get("updated_at", "")
    try:
        updated = datetime.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        days_old = (datetime.datetime.now(datetime.timezone.utc) - updated).days
        if days_old < 14:
            score += 20
        elif days_old < 60:
            score += 10
        elif days_old > 365:
            score -= 10
    except Exception:
        pass
    # Label priority
    if labels & _GOOD_FIRST_ISSUE_LABELS:
        score += 25
    if "bug" in labels or "defect" in labels or "regression" in labels:
        score += 15
    if "help wanted" in labels:
        score += 10
    # Comment activity: some comments = maintainer engaged, too many = contentious
    comments = int(issue.get("comments", 0))
    if 1 <= comments <= 5:
        score += 8
    elif comments > 10:
        score -= 5
    # Has repro steps
    body = str(issue.get("body") or "")
    if _issue_has_repro_steps(body):
        score += 12
    return score


def _discover_issue_backed_bugfixes(candidate: RepoCandidate) -> list[Opportunity]:
    """Fetch open bug issues and create Opportunity objects for each matched one."""
    try:
        issues = _gh_get(
            f"{_GITHUB_API}/repos/{candidate.full_name}/issues",
            params={"state": "open", "per_page": 30, "sort": "updated", "direction": "desc"},
            timeout=20,
        )
    except Exception:
        return []

    scored: list[tuple[int, dict, set]] = []
    for issue in issues:
        if issue.get("pull_request"):
            continue
        labels = _issue_labels(issue)
        if not labels.intersection(_BUG_LABELS):
            continue
        body = str(issue.get("body") or "")
        if len(body.strip()) < 30:
            continue
        rank = _issue_rank_score(issue, labels)
        scored.append((rank, issue, labels))

    scored.sort(key=lambda t: t[0], reverse=True)

    opportunities: list[Opportunity] = []
    for rank, issue, labels in scored[:10]:
        title = str(issue.get("title") or "").strip()
        body = str(issue.get("body") or "")
        issue_text = f"{title}\n{body}"
        target_file = _match_issue_target_file(candidate, issue_text)
        if not target_file:
            continue

        has_repro = _issue_has_repro_steps(body)
        is_good_first = bool(labels & _GOOD_FIRST_ISSUE_LABELS)

        base_score = 85 + (15 if is_good_first else 0)
        failure_mode = (
            f"Open issue #{issue.get('number')} reports a concrete bug in this area: {title[:120]}."
        )
        evidence = (
            f"Issue #{issue.get('number')}: {title}. "
            f"Labels={sorted(labels)}. "
            f"Has repro={'yes' if has_repro else 'no'}. "
            f"Matched target file: {target_file}."
        )

        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file=target_file,
            pattern_type="issue_backed_bugfix",
            failure_mode=failure_mode,
            evidence=evidence,
            patch_scope=1,
            test_target=guess_test_target(candidate.files, target_file),
            acceptance_score=base_score,
            opportunity_kind="bugfix",
            source_ref=f"issue:{issue.get('number')}",
            maintainer_intent=True,
            source_issue_number=int(issue.get("number", 0)),
            source_issue_url=str(issue.get("html_url", "")),
            issue_body_snippet=body[:400],
        )
        opportunities.append(opportunity)

    return opportunities


def _discover_issue_backed_feature_adds(candidate: RepoCandidate) -> list[Opportunity]:
    try:
        issues = _gh_get(
            f"{_GITHUB_API}/repos/{candidate.full_name}/issues",
            params={"state": "open", "per_page": 20, "sort": "updated", "direction": "desc"},
            timeout=20,
        )
    except Exception:
        return []

    opportunities: list[Opportunity] = []
    for issue in issues:
        if issue.get("pull_request"):
            continue
        labels = _issue_labels(issue)
        if not labels.intersection(_FEATURE_LABELS):
            continue
        body = str(issue.get("body") or "")
        title = str(issue.get("title") or "").strip()
        issue_text = f"{title}\n{body}"
        target_file = _match_issue_target_file(candidate, issue_text)
        if not target_file:
            continue
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file=target_file,
            pattern_type="issue_backed_feature_add",
            failure_mode=(
                "An open maintainer-labeled enhancement request identifies a missing capability in this target area, "
                "and the repository does not yet expose that behavior."
            ),
            evidence=(
                f"Issue #{issue.get('number')}: {title}. Labels={sorted(labels)}. "
                f"Matched target file from issue text: {target_file}."
            ),
            patch_scope=2,
            test_target=guess_test_target(candidate.files, target_file),
            acceptance_score=84,
            opportunity_kind="feature_add",
            source_ref=f"issue:{issue.get('number')}",
            maintainer_intent=True,
        )
        opportunities.append(opportunity)
    return opportunities


def _looks_like_core_target(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return bool(_CORE_TARGET_FILE_RE.search(normalized))


def _targeted_pattern_policy(opportunity: Opportunity) -> PatternSubmitPolicy:
    return _TARGETED_PATTERN_POLICIES.get(
        opportunity.pattern_type,
        PatternSubmitPolicy(
            mode="dry_run_first",
            max_changed_files=_TARGETED_MAX_CHANGED_FILES,
            max_diff_lines=_TARGETED_MAX_DIFF_LINES,
            require_test_target=False,
            semantic_review_enough=False,
            summary="default targeted policy",
        ),
    )


# Paths that are never real business logic — logic patterns are always false positives here.
_INFRA_DIR_MARKERS = (
    # Test fixture / data directories
    "test/suite/", "test/data/", "test/fixtures/", "tests/fixtures/",
    "tests/data/", "test/samples/", "tests/samples/",
    # Editor/tool config dirs — hook utilities, not user-facing logic
    ".claude/hooks/", ".claude/commands/",
    # Vendored code
    "vendor/", "vendors/", "third_party/", "third-party/",
)
_LOGIC_PATTERNS = frozenset({
    "unchecked_response_shape", "missing_input_validation", "missing_timeout",
    "resource_cleanup_gap", "overbroad_exception_handling",
})


def _universal_path_rejection(opportunity: Opportunity) -> str | None:
    """Block patterns that fire on non-business-logic paths in ALL modes."""
    if opportunity.pattern_type not in _LOGIC_PATTERNS:
        return None
    normalized_lower = opportunity.target_file.replace("\\", "/").lower()
    for marker in _INFRA_DIR_MARKERS:
        if marker in normalized_lower:
            return f"{opportunity.pattern_type} fired on an infrastructure/config path — not executable business logic."
    return None


def _targeted_breadth_rejection(candidate: RepoCandidate, opportunity: Opportunity) -> str | None:
    content = candidate.files.get(opportunity.target_file, "")
    line_count = len(content.splitlines()) if content else 0
    policy = _targeted_pattern_policy(opportunity)

    # Hard block: pattern explicitly excluded from targeted live mode
    if policy.mode == "blocked_for_targeted_live":
        return f"{opportunity.pattern_type} is blocked for targeted live mode by submit policy."

    # File size ceiling — based on AI context capacity, not diff budget.
    # Raised from 260→500: mature repos have 300-400 line service files that still have narrow fixable bugs.
    # Core entrypoints (app.py, __init__.py, main.py) stay at a lower limit because they carry global risk.
    if _looks_like_core_target(opportunity.target_file) and line_count > 300:
        return "Target file is a core entrypoint and too large for a reliable narrow targeted PR."
    if line_count > 500:
        return "Target file is too large for AI to reason about safely in a narrow targeted PR."

    if opportunity.patch_scope > 1 and not opportunity.test_target:
        return "Opportunity implies cross-file edits without a concrete nearby proof path."

    normalized = opportunity.target_file.replace("\\", "/")

    # Sparse-test repos: relax require_test_target to a score penalty rather than a hard block.
    # A repo with <10 test files rarely has a neat 1-to-1 test mapping; blocking on it wastes all opportunities.
    test_file_count = sum(1 for p in candidate.files if TEST_FILE_RE.search(p.replace("\\", "/").lower()))
    sparse_tests = test_file_count < 10
    if policy.require_test_target and not opportunity.test_target and not sparse_tests:
        return f"{opportunity.pattern_type} needs a focused nearby regression test before targeted live mode will try it."

    if policy.banned_surface_re and policy.banned_surface_re.search(normalized):
        return f"{opportunity.pattern_type} in this surface is too risky for targeted live mode."

    return None


def _targeted_patchability_score(candidate: RepoCandidate, opportunity: Opportunity, score: int) -> int:
    content = candidate.files.get(opportunity.target_file, "")
    line_count = len(content.splitlines()) if content else 0
    value = score
    normalized = opportunity.target_file.replace("\\", "/")
    policy = _targeted_pattern_policy(opportunity)

    # Sparse-test repos: many active repos have low test/code ratios.
    # Applying the full test-target penalty in this case kills all candidates — reduce it.
    test_file_count = sum(1 for p in candidate.files if TEST_FILE_RE.search(p.replace("\\", "/").lower()))
    sparse_tests = test_file_count < 10

    if _looks_like_core_target(opportunity.target_file):
        value -= 32
    # Hard penalty tiers for large files: AI must output the full modified file,
    # so very large targets reliably time out the generate phase.
    if line_count > 600:
        value -= 55  # almost certainly below threshold; prevents generate timeouts
    elif line_count > 400:
        value -= 30
    elif line_count > 220:
        value -= 16 if sparse_tests else 24
    elif line_count > 160:
        value -= 8 if sparse_tests else 14
    elif line_count > 120:
        value -= 4 if sparse_tests else 6
    if opportunity.patch_scope > 1:
        value -= 22
    if not opportunity.test_target:
        value -= 5 if sparse_tests else 14
    elif expected_test_root(candidate.files, opportunity.target_file):
        value += 6
    line_no = int(getattr(opportunity, "line_no", 0) or 0)
    if line_no > 0:
        value += 2
    if opportunity.pattern_type in {"missing_input_validation", "unchecked_response_shape", "missing_timeout"}:
        value += 8
    if policy.mode == "blocked_for_targeted_live":
        value -= 100
    elif policy.mode in {"dry_run_first", "manual_only"}:
        value -= 16
    if opportunity.pattern_type == "overbroad_exception_handling":
        value -= 18
        if policy.banned_surface_re and policy.banned_surface_re.search(normalized):
            value -= 20
    if opportunity.target_file.replace("\\", "/").lower().startswith(("tests/", "test/")):
        value -= 18
    return value


def _diff_line_count(original: str, updated: str) -> int:
    original_lines = original.splitlines()
    updated_lines = updated.splitlines()
    max_len = max(len(original_lines), len(updated_lines))
    changed = 0
    for idx in range(max_len):
        before = original_lines[idx] if idx < len(original_lines) else None
        after = updated_lines[idx] if idx < len(updated_lines) else None
        if before != after:
            changed += 1
    return changed


def _changed_diff_line_count(original_files: dict[str, str], changed_files: dict[str, str]) -> int:
    total = 0
    for path, updated in changed_files.items():
        total += _diff_line_count(original_files.get(path, ""), updated)
    return total


def _classify_patch_shape(
    original_files: dict[str, str],
    changed_files: dict[str, str],
    opportunity: Opportunity,
) -> PatchShape:
    risky_surface_re = re.compile(
        r"(^|/)(context|context_system|config|settings|auth|loader|parser|middleware|hooks?)(/|$|\.)",
        re.IGNORECASE,
    )
    for path, updated in changed_files.items():
        normalized = path.replace("\\", "/")
        original = original_files.get(path, "")
        if risky_surface_re.search(normalized):
            return PatchShape("high", f"{path}: behavior-routing surface requires manual review")
        lowered_original = original.lower()
        lowered_updated = updated.lower()
        if "except" in lowered_original or "except" in lowered_updated:
            if lowered_original.count("except") != lowered_updated.count("except") or "raise" in lowered_updated:
                return PatchShape("high", f"{path}: exception policy changed")
        if lowered_original.count("return") != lowered_updated.count("return"):
            return PatchShape("high", f"{path}: return-path count changed")
        fallback_markers = ("return none", "return {}", "return []", "return false", "return true")
        had_fallback = any(marker in lowered_original for marker in fallback_markers)
        has_raise = "raise " in lowered_updated
        if had_fallback and has_raise:
            return PatchShape("high", f"{path}: fallback behavior changed to raised error")
    if opportunity.pattern_type == "overbroad_exception_handling":
        return PatchShape("high", "exception handling patches require manual review")
    return PatchShape("low", "")


def _targeted_execution_mode(policy: PatternSubmitPolicy, patch_shape: PatchShape) -> str:
    if policy.mode == "auto_live_safe" and patch_shape.risk == "low" and policy.semantic_review_enough:
        return "live-safe"
    if policy.mode in {"dry_run_first", "manual_only"} or patch_shape.risk == "high":
        return "live-review"
    return "stop"


def _title_family(title: str) -> str:
    normalized = re.sub(r"^(fix|bugfix|chore|refactor|docs|test)(\([^)]*\))?:\s*", "", title.lower()).strip()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split()[:8])


def _uses_exception_policy_wording(*chunks: str) -> bool:
    haystack = " ".join(chunk.lower() for chunk in chunks if chunk)
    return "surface" in haystack and "error" in haystack and "swallow" in haystack


def _duplicate_patch_family_rejection(result: dict) -> str | None:
    title_family = _title_family(str(result.get("pr_title") or ""))
    seen_titles = set(str(item) for item in _ACTIVE_RUN_METRICS.get("seen_title_families", []))
    repeats_exception_wording = _uses_exception_policy_wording(
        str(result.get("pr_title") or ""),
        str(result.get("pr_body") or ""),
        str(result.get("rationale") or ""),
        str(result.get("safety_proof") or ""),
    )
    if repeats_exception_wording and bool(_ACTIVE_RUN_METRICS.get("seen_exception_policy_wording")):
        return "Repeated exception-policy wording family in the same targeted run."
    if title_family and title_family in seen_titles:
        return f"Repeated PR title family `{title_family}` in the same targeted run."
    if title_family:
        _ACTIVE_RUN_METRICS.setdefault("seen_title_families", []).append(title_family)
    if repeats_exception_wording:
        _ACTIVE_RUN_METRICS["seen_exception_policy_wording"] = True
    return None


def _discover_opportunities(
    candidate: RepoCandidate,
    log: logging.Logger,
    goal: str,
    targeted_mode: bool = False,
) -> tuple[Opportunity, int]:
    _set_run_stage("qualify", candidate.full_name, {"goal": goal, "targeted_mode": targeted_mode})
    base_score = _acceptance_score(candidate, candidate.files)
    _ENGINE_STORE.upsert_repo_profile(candidate, base_score)

    # In targeted mode, skip patterns that are unconditionally blocked — no point generating
    # opportunities that will always be rejected at the breadth-rejection step.
    _targeted_skip: set[str] | None = None
    if targeted_mode:
        _targeted_skip = {pt for pt, pol in _TARGETED_PATTERN_POLICIES.items() if pol.mode == "blocked_for_targeted_live"}
    opportunities = _PATTERN_SCANNER.scan(candidate, excluded_patterns=_targeted_skip)
    if goal == "bugfix":
        pattern_bugs = [o for o in opportunities if o.opportunity_kind == "bugfix"]
        issue_bugs = _discover_issue_backed_bugfixes(candidate)
        if issue_bugs:
            log.info(
                "Issue-driven bugfixes found for %s: %d candidate(s) from open issues",
                candidate.full_name,
                len(issue_bugs),
            )
        # Issue-backed opportunities prepended: tried first (higher acceptance_score)
        opportunities = issue_bugs + pattern_bugs
    elif goal == "feature_upgrade":
        opportunities = [opportunity for opportunity in opportunities if opportunity.opportunity_kind == "feature_upgrade"]
    elif goal == "feature_add":
        opportunities = _discover_issue_backed_feature_adds(candidate)
    else:
        raise PRGeneratorError(f"Unknown contribution goal: {goal}")

    if not opportunities:
        _ENGINE_STORE.record_repo_event(
            _ACTIVE_RUN_ID,
            candidate.full_name,
            "scan_rejected",
            f"No qualified opportunities found for goal={goal}.",
            {"reason_code": "no_whitelisted_pattern", "goal": goal},
        )
        raise PRGeneratorError(
            f"No qualified opportunities found for goal={goal} in {candidate.full_name}"
        )

    qualified: list[tuple[int, Opportunity, int]] = []
    rejections: list[str] = []
    for opportunity in opportunities:
        opportunity.acceptance_score += base_score
        source = "issue_ingest" if opportunity.source_ref.startswith("issue:") else "code_scan"
        opportunity_id = _ENGINE_STORE.create_opportunity(_ACTIVE_RUN_ID, opportunity, source=source)
        opportunity.opportunity_id = opportunity_id
        universal_rejection = _universal_path_rejection(opportunity)
        if universal_rejection:
            _ENGINE_STORE.reject_opportunity(
                _ACTIVE_RUN_ID,
                opportunity,
                "infra_path_false_positive",
                universal_rejection,
                "QUALIFY",
                opportunity_id=opportunity_id,
            )
            _bump_run_metric("broad_rejected_early")
            rejections.append(f"{opportunity.pattern_type}:infra_path_false_positive")
            continue
        targeted_breadth = _targeted_breadth_rejection(candidate, opportunity) if targeted_mode else None
        if targeted_breadth:
            _ENGINE_STORE.reject_opportunity(
                _ACTIVE_RUN_ID,
                opportunity,
                "target_area_too_broad",
                targeted_breadth,
                "QUALIFY",
                opportunity_id=opportunity_id,
            )
            _bump_run_metric("broad_rejected_early")
            rejections.append(f"{opportunity.pattern_type}:target_area_too_broad")
            continue
        qualification = qualify_opportunity(candidate, opportunity)
        if not qualification.accepted:
            _ENGINE_STORE.reject_opportunity(
                _ACTIVE_RUN_ID,
                opportunity,
                qualification.reason_code,
                qualification.summary,
                "QUALIFY",
                opportunity_id=opportunity_id,
            )
            rejections.append(f"{opportunity.pattern_type}:{qualification.reason_code}")
            continue
        opportunity.acceptance_score = qualification.score
        _ENGINE_STORE.transition_opportunity(
            opportunity_id,
            "QUALIFY",
            why_advanced=qualification.summary,
        )
        patchability = _targeted_patchability_score(candidate, opportunity, qualification.score) if targeted_mode else qualification.score
        qualified.append((patchability, opportunity, opportunity_id))

    if not qualified:
        _ENGINE_STORE.record_repo_event(
            _ACTIVE_RUN_ID,
            candidate.full_name,
            "qualify_rejected",
            "All scanned opportunities were rejected during qualification.",
            {"rejections": rejections},
        )
        raise PRGeneratorError(
            f"All pattern-first opportunities were rejected for {candidate.full_name}: {', '.join(rejections[:4])}"
        )

    qualified.sort(key=lambda item: item[0], reverse=True)
    if targeted_mode:
        shortlist = qualified[:_TARGETED_SHORTLIST_LIMIT]
        viable = [item for item in shortlist if item[0] >= _TARGETED_MIN_PATCHABILITY_SCORE][:_TARGETED_VIABLE_LIMIT]
        shortlist_summary = [
            {
                "target_file": item[1].target_file,
                "pattern_type": item[1].pattern_type,
                "pattern_policy": _targeted_pattern_policy(item[1]).mode,
                "score": item[0],
                "test_target": item[1].test_target,
            }
            for item in shortlist
        ]
        _ACTIVE_RUN_METRICS["shortlisted"] = len(shortlist)
        _ACTIVE_RUN_METRICS["shortlist_summary"] = shortlist_summary
        _ACTIVE_RUN_METRICS["min_patchability_score"] = _TARGETED_MIN_PATCHABILITY_SCORE
        _ACTIVE_RUN_METRICS["best_patchability_score"] = int(shortlist[0][0]) if shortlist else 0
        _ENGINE_STORE.record_repo_event(
            _ACTIVE_RUN_ID,
            candidate.full_name,
            "targeted_shortlist",
            f"Shortlisted {len(shortlist)} narrow targeted opportunities.",
            {"items": shortlist_summary, "min_patchability_score": _TARGETED_MIN_PATCHABILITY_SCORE},
        )
        if not viable:
            raise PRGeneratorError(
                f"No viable targeted shortlist survived for {candidate.full_name}; "
                f"best candidates stayed below the patchability threshold of {_TARGETED_MIN_PATCHABILITY_SCORE}."
            )
        score, opportunity, opportunity_id = viable[0]
    else:
        score, opportunity, opportunity_id = qualified[0]
    _ENGINE_STORE.record_repo_event(
        _ACTIVE_RUN_ID,
        candidate.full_name,
        "opportunity_selected",
        f"Selected {opportunity.pattern_type} in {opportunity.target_file}",
        {
            "score": score,
            "pattern_type": opportunity.pattern_type,
            "target_file": opportunity.target_file,
        },
    )
    return opportunity, opportunity_id


def _recent_pr_recon(candidate: RepoCandidate, log: logging.Logger) -> dict:
    if not _PR_RECON_ENABLED:
        return {"enabled": False}

    fields = "number,title,author,labels,url"
    samples: dict[str, list[dict]] = {}
    for state in ("merged", "closed"):
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "list",
                    "--repo", candidate.full_name,
                    "--state", state,
                    "--limit", "10",
                    "--json", fields,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            log.warning("Repo recon skipped for %s: gh pr list timed out", candidate.full_name)
            return {"enabled": True, "available": False, "reason": "gh_timeout"}
        except FileNotFoundError:
            log.warning("Repo recon skipped for %s: gh CLI not found", candidate.full_name)
            return {"enabled": True, "available": False, "reason": "gh_not_found"}
        if result.returncode != 0:
            detail = (result.stdout + result.stderr).strip()[:200]
            log.warning("Repo recon skipped for %s: %s", candidate.full_name, detail)
            return {"enabled": True, "available": False, "reason": detail}
        try:
            parsed = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            parsed = []
        samples[state] = parsed if isinstance(parsed, list) else []

    closed = samples.get("closed", [])
    merged = samples.get("merged", [])
    negative_samples: list[str] = []
    for pr in closed:
        labels = pr.get("labels", []) if isinstance(pr, dict) else []
        label_text = " ".join(
            str(label.get("name", "")) if isinstance(label, dict) else str(label)
            for label in labels
        )
        author = pr.get("author", {}) if isinstance(pr, dict) else {}
        author_login = author.get("login", "") if isinstance(author, dict) else ""
        haystack = " ".join([str(pr.get("title", "")), label_text, str(author_login)])
        if _NEGATIVE_PR_SIGNAL_RE.search(haystack):
            negative_samples.append(str(pr.get("url") or pr.get("number") or pr.get("title") or "closed PR"))

    details = {
        "enabled": True,
        "available": True,
        "merged_sample": len(merged),
        "closed_sample": len(closed),
        "negative_samples": negative_samples[:5],
    }
    _ENGINE_STORE.record_repo_event(
        _ACTIVE_RUN_ID,
        candidate.full_name,
        "repo_recon",
        (
            f"Recent PR recon: merged={len(merged)} closed={len(closed)} "
            f"negative_signals={len(negative_samples)}"
        ),
        details,
    )
    if len(negative_samples) >= 2:
        raise PRGeneratorError(
            "Recent closed PRs show repeated negative maintainer signals "
            f"({len(negative_samples)} matches: {', '.join(negative_samples[:2])})."
        )
    return details


def _select_bug_brief(
    candidate: RepoCandidate,
    source_dump: str,
    dep_section: str,
    lang_label: str,
    log: logging.Logger,
) -> dict:
    """Pick one narrow, evidence-backed bug target before asking for a patch.

    This intentionally spends an extra AI call so the later codegen step starts
    from a concrete failure mode rather than from a vague "find any improvement"
    instruction.
    """
    prompt = f"""You are a senior {lang_label} engineer preparing a highly targeted open-source bug-fix PR.

Repo: {candidate.full_name}
Description: {candidate.description}
Stars: {candidate.stars} | License: {candidate.license}

Source files:
{source_dump}{dep_section}

Task:
Choose EXACTLY ONE bug or reliability issue worth fixing.

Rules:
- Prefer a single function or a single file
- The issue must be concrete and testable
- Do NOT propose style cleanups, defensive refactors, or speculative API contract changes
- Do NOT choose improvements unless you can describe a specific failing input/state/timing case
- If no strong bug is visible, say so

Evidence bar:
- Name the exact failing input, state, or concurrent timing
- Name the exact file to patch
- Say whether a regression test should be added

Respond with JSON only:
{{
  "action": "patch|skip",
  "confidence": "high|medium|low",
  "improvement_type": "bug_fix|security|resource_leak|error_handling|race_condition",
  "target_file": "relative/path.py",
  "bug_hypothesis": "one sentence describing the exact bug",
  "failure_mode": "specific failing input/state/timing case",
  "evidence": "why this is likely real from the code shown",
  "test_plan": "one sentence regression test idea",
  "why_now": "one sentence why this is a good PR target"
}}"""
    raw = call_ai(prompt, timeout=get_scaled_timeout(180, 1))
    result = _parse_json(raw)
    if result.get("action") != "patch":
        raise PRGeneratorError(
            f"Target analysis found no strong bug candidate for {candidate.full_name}"
        )
    if str(result.get("confidence", "")).lower() not in {"high", "medium"}:
        raise PRGeneratorError(
            f"Target analysis confidence too low for {candidate.full_name}: {result.get('confidence')}"
        )
    required = {"target_file", "bug_hypothesis", "failure_mode", "evidence", "test_plan", "improvement_type"}
    missing = required - set(result.keys())
    if missing:
        raise PRGeneratorError(f"Target analysis missing fields: {missing}")
    log.info(
        "Targeted bug selected: %s in %s",
        result.get("bug_hypothesis", "").strip()[:120],
        result.get("target_file", ""),
    )
    return result


def _has_test_changes(changed_files: dict[str, str]) -> bool:
    return any(
        path.lower().startswith("test")
        or "/tests/" in path.lower().replace("\\", "/")
        or path.lower().endswith(("_test.py", ".spec.ts", ".test.ts", ".test.tsx", ".spec.tsx"))
        for path in changed_files
    )


def _check_pr_evidence_quality(result: dict, changed_files: dict[str, str]) -> str | None:
    """Reject weak bug-fix claims that lack proof or validation.

    We want the engine to avoid "this feels safer" drive-by PRs. A PR can still
    pass without a new test, but only if the write-up cites a concrete observed
    failure mode rather than an ambiguous or defensive hunch.
    """
    improvement_type = str(result.get("improvement_type", "")).strip().lower()
    if improvement_type not in {"bug_fix", "security", "error_handling", "race_condition", "resource_leak"}:
        return None

    body = str(result.get("pr_body", ""))
    rationale = str(result.get("rationale", ""))
    safety_proof = str(result.get("safety_proof", ""))
    combined = " ".join((body, rationale, safety_proof)).lower()

    weak_claim_markers = (
        "ambiguous api request",
        "ambiguous request",
        "safer and more consistent",
        "defensive rather than",
        "defensive change",
        "could mean",
        "might be treated",
        "if the api accepts",
        "happy to revert",
        "not clearly justified",
        "appears to be intentional",
    )
    strong_evidence_markers = (
        "raises ",
        "exception",
        "traceback",
        "reproduce",
        "reproduced",
        "observed crash",
        "observed exception",
        "observed failure",
        "observed wrong",
        "failing test",
        "regression",
        "status code",
        "http 4",
        "http 5",
        "invalid literal",
        "typeerror",
        "valueerror",
        "keyerror",
        "indexerror",
        "returns wrong",
        "incorrect result",
        "data loss",
        "deadlock",
        "race condition",
        "path traversal",
        "injection",
        "timeout",
        "leak",
        "crash",
    )

    mentions_weak_claim = any(marker in combined for marker in weak_claim_markers)
    has_strong_evidence = any(marker in combined for marker in strong_evidence_markers)
    has_test_changes = _has_test_changes(changed_files)

    if mentions_weak_claim and not has_strong_evidence:
        return (
            "PR rationale is speculative/defensive without concrete evidence of breakage. "
            "Do not submit this as a bug fix."
        )

    if not has_test_changes and not has_strong_evidence:
        return (
            "PR lacks both changed tests and concrete evidence of a real failure mode. "
            "Skip this improvement unless you can prove the bug."
        )

    return None


# ── PR log ────────────────────────────────────────────────────
def load_pr_log() -> dict:
    if PR_LOG_FILE.exists():
        data = json.loads(PR_LOG_FILE.read_text(encoding="utf-8"))
    else:
        data = {"submitted": []}
    # Deduplicate entries by pr_url — keep the one with the most resolved status
    _STATUS_RANK = {"merged": 3, "closed": 2, "open": 1, "": 0}
    seen: dict[str, dict] = {}
    for entry in data.get("submitted", []):
        url = entry.get("pr_url", "")
        if not url:
            continue
        existing = seen.get(url)
        if existing is None:
            seen[url] = entry
        else:
            if _STATUS_RANK.get(entry.get("status", ""), 0) >= _STATUS_RANK.get(existing.get("status", ""), 0):
                seen[url] = entry
    data["submitted"] = list(seen.values())
    return data


def save_pr_log(pr_result, improvement_type: str = "", opportunity_id: int | None = None) -> None:
    """Append a submitted PR to the log."""
    data = load_pr_log()
    data.setdefault("submitted", []).append({
        "full_name":        pr_result.full_name,
        "owner_login":      getattr(pr_result, "owner_login", ""),
        "pr_url":           pr_result.pr_url,
        "pr_title":         pr_result.pr_title,
        "fork_name":        pr_result.fork_name,
        "branch_name":      pr_result.branch_name,
        "files_changed":    pr_result.files_changed,
        "submitted_at":     pr_result.submitted_at,
        "improvement_type": improvement_type,
        "status":           "open",
        "notified_merge":   False,
        "opportunity_id":   opportunity_id,
    })
    PR_LOG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    _ENGINE_STORE.record_pull_request(
        opportunity_id=opportunity_id,
        repo_full_name=pr_result.full_name,
        pr_url=pr_result.pr_url,
        pr_title=pr_result.pr_title,
        fork_name=pr_result.fork_name,
        branch_name=pr_result.branch_name,
        improvement_type=improvement_type,
        status="open",
        owner_login=getattr(pr_result, "owner_login", ""),
    )


def get_pr_submitted_repos() -> set[str]:
    """Return lowercased full_names of repos we already submitted a PR to."""
    repos = _ENGINE_STORE.submitted_repos()
    if repos:
        return repos
    try:
        from src.github.fork import get_current_github_login
        current_login = get_current_github_login().lower()
    except Exception:
        current_login = ""
    submitted: set[str] = set()
    for entry in load_pr_log().get("submitted", []):
        if not isinstance(entry, dict):
            continue
        full_name = str(entry.get("full_name") or "").strip().lower()
        if not full_name:
            continue
        if current_login:
            entry_owner = str(entry.get("owner_login") or "").strip().lower()
            fork_name = str(entry.get("fork_name") or "").strip().lower()
            if entry_owner and entry_owner != current_login:
                continue
            if not entry_owner and fork_name and not fork_name.startswith(f"{current_login}/"):
                continue
        submitted.add(full_name)
    return submitted


def get_improvement_stats() -> dict[str, dict]:
    """Aggregate merge/close rates per improvement_type from pr_log.json.

    Returns dict like:
    {
      "bug_fix":        {"merged": 5, "closed": 1, "open": 2, "rate": 0.83},
      "error_handling": {"merged": 3, "closed": 2, "open": 1, "rate": 0.60},
      ...
    }
    Only includes types with at least 1 resolved (merged or closed) entry.
    """
    entries = load_pr_log().get("submitted", [])
    counts: dict[str, dict] = {}
    for e in entries:
        itype = e.get("improvement_type", "").strip()
        if not itype:
            continue
        status = e.get("status", "open")
        if itype not in counts:
            counts[itype] = {"merged": 0, "closed": 0, "open": 0}
        counts[itype][status if status in ("merged", "closed") else "open"] += 1

    stats = {}
    for itype, c in counts.items():
        resolved = c["merged"] + c["closed"]
        stats[itype] = {
            **c,
            "rate": round(c["merged"] / resolved, 2) if resolved > 0 else None,
        }
    return stats


# ── Fork cleanup ──────────────────────────────────────────
def _delete_fork(entry: dict, log: logging.Logger) -> None:
    """Delete the current operator's fork of a repo after its PR is resolved.

    No-ops if:
    - fork_name is missing or doesn't match the current GitHub login
    - fork_name points at the target repo itself (not a disposable fork)
    - fork was already deleted (fork_deleted flag set)
    - gh repo delete fails for any reason (non-blocking)
    """
    import subprocess
    from src.github.fork import ForkError, get_current_github_login, gh_safe_env

    fork_name = str(entry.get("fork_name", "")).strip()
    full_name = str(entry.get("full_name", "")).strip()
    try:
        current_login = get_current_github_login().lower()
    except ForkError:
        return
    if not fork_name or not fork_name.lower().startswith(f"{current_login}/"):
        return
    if full_name and fork_name.lower() == full_name.lower():
        log.info("Skipping fork delete for %s — entry points to the target repo itself", fork_name)
        entry["fork_deleted"] = False
        return
    if entry.get("fork_deleted"):
        return

    log.info("Deleting fork: %s", fork_name)
    try:
        r = subprocess.run(
            ["gh", "repo", "delete", fork_name, "--yes"],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            env=gh_safe_env(),
        )
        if r.returncode == 0:
            log.info("Fork deleted: %s", fork_name)
            entry["fork_deleted"] = True
        else:
            stderr = (r.stdout + r.stderr).strip()[:200]
            # 404 = already gone
            if "404" in stderr or "Could not resolve" in stderr or "not found" in stderr.lower():
                log.info("Fork already gone: %s", fork_name)
                entry["fork_deleted"] = True
            # 403 = missing delete_repo scope — skip silently, don't retry
            elif "403" in stderr or "delete_repo" in stderr or "admin rights" in stderr.lower():
                log.info("Fork delete skipped (needs delete_repo scope): %s — run: gh auth refresh -h github.com -s delete_repo", fork_name)
                entry["fork_deleted"] = True
            else:
                log.warning("Fork delete failed for %s: %s", fork_name, stderr)
    except Exception as exc:
        log.warning("Fork delete error for %s: %s", fork_name, exc)


# ── PR status checker ─────────────────────────────────────
def check_pr_statuses(log: logging.Logger) -> None:
    """Poll all open PRs in pr_log.json and send Telegram notifications on merge/close.

    Skips entries without a real PR URL (e.g. own-repo direct pushes).
    Updates the log in-place with current status.
    """
    import subprocess
    from src.github.fork import ForkError, get_current_github_login, gh_safe_env
    from src.core.notify import notify

    from src.core.cli_ui import print_section, print_item, print_ok, print_warn, print_blank

    data = load_pr_log()
    entries = data.get("submitted", [])
    print_section("PR Status")
    if not entries:
        print_item("No PRs in log — nothing to check.")
        print_blank()
        log.info("PR log is empty — nothing to check")
        return

    changed = False
    try:
        current_login = get_current_github_login().lower()
    except ForkError:
        current_login = ""
    def _matches_current_owner(entry: dict) -> bool:
        if not current_login:
            return True
        entry_owner = str(entry.get("owner_login") or "").strip().lower()
        fork_name = str(entry.get("fork_name") or "").strip().lower()
        if entry_owner:
            return entry_owner == current_login
        if fork_name:
            return fork_name.startswith(f"{current_login}/")
        return False
    entries = [e for e in entries if _matches_current_owner(e)]
    open_count = sum(1 for e in entries if e.get("status", "open") == "open")
    pending_cleanup = sum(
        1
        for e in entries
        if e.get("status") in ("merged", "closed")
        and (e.get("fork_name", "").lower().startswith(f"{current_login}/") if current_login else False)
        and not e.get("fork_deleted")
    )
    log.info("Checking %d open PR(s) in log...", open_count)
    if pending_cleanup:
        log.info("Found %d resolved PR(s) with pending fork cleanup", pending_cleanup)

    seen_urls: set[str] = set()
    for entry in entries:
        pr_url = entry.get("pr_url", "")
        status  = entry.get("status", "open")

        if status in ("merged", "closed"):
            if current_login and entry.get("fork_name", "").lower().startswith(f"{current_login}/") and not entry.get("fork_deleted"):
                _delete_fork(entry, log)
                changed = True
            continue

        # Skip already-resolved or own-repo push entries (no PR number in URL)
        if not pr_url or "/pull/" not in pr_url:
            continue
        if pr_url in seen_urls:
            continue
        seen_urls.add(pr_url)

        full_name = entry.get("full_name", "")
        pr_title  = entry.get("pr_title", "")
        pr_number_match = re.search(r"/pull/(\d+)$", pr_url)
        if not pr_number_match:
            continue
        pr_number = pr_number_match.group(1)

        try:
            r = subprocess.run(
                ["gh", "api", f"repos/{full_name}/pulls/{pr_number}"],
                capture_output=True, text=True, encoding="utf-8", timeout=20,
                env=gh_safe_env(),
            )
            if r.returncode != 0:
                err = r.stderr.strip()
                if "404" in err or "Not Found" in err:
                    log.info("PR not found (404), marking closed: %s", pr_url)
                    entry["status"] = "closed"
                    changed = True
                else:
                    log.warning("Could not fetch PR %s: %s", pr_url, err[:200])
                continue

            info = json.loads(r.stdout)
        except Exception as exc:
            log.warning("Error checking PR %s: %s", pr_url, exc)
            continue

        state = info.get("state", "").upper()
        merged_at = info.get("merged_at") or ""
        if state == "CLOSED" and merged_at:
            state = "MERGED"
        review_decision = ""
        if state == "OPEN":
            try:
                rr = subprocess.run(
                    ["gh", "api", f"repos/{full_name}/pulls/{pr_number}/reviews"],
                    capture_output=True, text=True, encoding="utf-8", timeout=20,
                    env=gh_safe_env(),
                )
                if rr.returncode == 0:
                    try:
                        reviews = json.loads(rr.stdout)
                        if any((review.get("state") or "").upper() == "APPROVED" for review in reviews):
                            review_decision = "APPROVED"
                    except Exception:
                        pass
            except Exception as exc:
                log.warning("Error fetching reviews for PR %s: %s", pr_url, exc)

        if state == "MERGED" and not entry.get("notified_merge"):
            print_ok(f"MERGED  {full_name}  {pr_url}")
            log.info("MERGED: %s — %s", full_name, pr_url)
            notify(
                f"PR MERGED\n"
                f"Repo: {full_name}\n"
                f"Title: {pr_title}\n"
                f"URL: {pr_url}"
            )
            entry["status"]         = "merged"
            entry["notified_merge"] = True
            entry["resolved_at"]    = merged_at or datetime.now(timezone.utc).isoformat()
            changed = True
            _delete_fork(entry, log)
            _ENGINE_STORE.update_pr_status(
                pr_url,
                "merged",
                maintainer_signal="merged",
                resolved_at=entry["resolved_at"],
            )

        elif state == "CLOSED" and not entry.get("notified_merge"):
            print_warn(f"CLOSED  {full_name}  {pr_title}")
            print_item(f"→ rover report  # see rejection patterns")
            log.info("CLOSED (not merged): %s — %s", full_name, pr_url)
            notify(
                f"PR closed (not merged)\n"
                f"Repo: {full_name}\n"
                f"Title: {pr_title}\n"
                f"URL: {pr_url}"
            )
            entry["status"]         = "closed"
            entry["notified_merge"] = True
            entry["resolved_at"]    = datetime.now(timezone.utc).isoformat()
            changed = True
            _delete_fork(entry, log)
            _ENGINE_STORE.update_pr_status(
                pr_url,
                "closed",
                maintainer_signal="closed",
                resolved_at=entry["resolved_at"],
            )

        elif state == "OPEN" and review_decision == "APPROVED" and not entry.get("notified_approved"):
            log.info("APPROVED (awaiting merge): %s — %s", full_name, pr_url)
            notify(
                f"PR APPROVED\n"
                f"Repo: {full_name}\n"
                f"Title: {pr_title}\n"
                f"URL: {pr_url}"
            )
            entry["notified_approved"] = True
            changed = True
            _ENGINE_STORE.update_pr_status(pr_url, "open", maintainer_signal="approved")

        else:
            print_item(f"● open   {full_name}   #{pr_url.rsplit('/', 1)[-1]}")
            log.info("  still open: %s", pr_url)

        time.sleep(0.3)

    print_blank()
    if changed:
        PR_LOG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("PR log updated")
    else:
        print_item("No status changes.")


# ── Fetch specific repo ───────────────────────────────────
def fetch_repo_candidate(
    repo_url: str,
    log: logging.Logger,
    *,
    override_limits: bool = False,
    status_cb: "Callable[[str], None] | None" = None,
) -> RepoCandidate:
    """Fetch a specific repo by URL or owner/repo and return a RepoCandidate.

    Works for both external repos and operator-owned repos.
    Raises ScraperError if the repo can't be fetched or has no Python/TypeScript files.
    """
    return fetch_repo_candidate_with_scope(repo_url, log, enforce_scope=True, override_limits=override_limits, status_cb=status_cb)


def fetch_repo_candidate_with_scope(
    repo_url: str,
    log: logging.Logger,
    *,
    enforce_scope: bool,
    override_limits: bool = False,
    status_cb: "Callable[[str], None] | None" = None,
) -> RepoCandidate:
    """Fetch a specific repo and optionally enforce narrow contribution scope gates."""

    full_name, data = fetch_repo_metadata(repo_url, log)

    pushed_at = data.get("pushed_at", "")
    try:
        from datetime import datetime, timezone
        pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        pushed_days_ago = (datetime.now(timezone.utc) - pushed).days
    except Exception:
        pushed_days_ago = 0
    if enforce_scope and data.get("archived"):
        raise ScraperError(
            f"{full_name} is archived and should be handled with inspect-only flow, not contribution runs"
        )
    if enforce_scope and data.get("disabled"):
        raise ScraperError(f"{full_name} is disabled on GitHub and cannot accept contribution runs")
    if enforce_scope and not override_limits and pushed_days_ago > _PR_MAX_PUSHED:
        raise ScraperError(
            f"{full_name} looks inactive (last push {pushed_days_ago}d ago; limit {_PR_MAX_PUSHED}d). "
            f"Use 'rover inspect {full_name}' instead."
        )

    lic = _get_license(data)

    candidate = RepoCandidate(
        name=data["name"],
        full_name=full_name,
        description=(data.get("description") or "")[:200],
        stars=data.get("stargazers_count", 0),
        forks=data.get("forks_count", 0),
        license=lic,
        url=data["html_url"],
        default_branch=data.get("default_branch", "main"),
        pushed_days_ago=pushed_days_ago,
        topics=data.get("topics", []),
        archived=bool(data.get("archived")),
        disabled=bool(data.get("disabled")),
    )

    log.info(
        "Repo: %s (%d★, %s license, pushed %dd ago)",
        full_name, candidate.stars, lic, pushed_days_ago,
    )

    allow_broad = override_limits or _PR_TARGETED_ALLOW_BROAD
    _pre_max_py = _PR_TARGETED_MAX_PY_FILES if not allow_broad else None
    _pre_max_total = _PR_TARGETED_MAX_TOTAL_FILES if not allow_broad else None
    candidate.files = download_repo_files(
        candidate,
        max_py=_pre_max_py,
        max_total=_pre_max_total,
        allowed_exts=(".py", ".ts", ".tsx", ".json", ".toml", ".yml", ".yaml"),
        status_cb=status_cb,
    )
    if not candidate.files:
        raise ScraperError(f"No files downloaded from {full_name}")

    try:
        if status_cb:
            status_cb(f"checking maintainer signals for {full_name}...")
        candidate.maintainer_signals = fetch_maintainer_signals(candidate)
        log.info("Maintainer signals for %s: %s", full_name, {k: v for k, v in candidate.maintainer_signals.items() if k != "contributing_snippet"})
    except Exception:
        pass

    py_count, ts_count, test_count, _has_tests = _count_repo_files(candidate.files)
    if py_count == 0 and ts_count == 0:
        raise ScraperError(f"No Python or TypeScript files found in {full_name}")

    if enforce_scope:
        py_count, ts_count, test_count = _validate_candidate_scope(
            candidate,
            targeted=True,
            override_limits=override_limits,
        )
    else:
        try:
            _validate_candidate_scope(candidate, targeted=True, override_limits=override_limits)
        except ScraperError as exc:
            log.warning("Inspect-only note for %s: %s", full_name, exc)

    log.info(
        "Downloaded %d files (py=%d ts=%d tests=%d) from %s",
        len(candidate.files), py_count, ts_count, test_count, full_name,
    )
    return candidate


# ── PR feedback responder ─────────────────────────────────
@dataclass
class PRFeedbackAction:
    pr_url: str
    full_name: str
    fork_name: str
    branch_name: str
    comment_id: int
    comment_body: str
    comment_author: str
    reply: str
    changed_files: dict[str, str]
    commit_msg: str


def _fetch_pr_comments(full_name: str, pr_number: int) -> list[dict]:
    """Fetch all issue-level comments on a PR (not review comments)."""
    import subprocess
    from src.github.fork import gh_safe_env
    r = subprocess.run(
        ["gh", "api", f"repos/{full_name}/issues/{pr_number}/comments",
         "--jq", "[.[] | {id: .id, user: .user.login, body: .body, created_at: .created_at}]"],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
        env=gh_safe_env(),
    )
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout)
    except Exception:
        return []


def _fetch_pr_review_comments(full_name: str, pr_number: int) -> list[dict]:
    """Fetch inline review comments (on specific lines of code)."""
    import subprocess
    from src.github.fork import gh_safe_env
    r = subprocess.run(
        ["gh", "api", f"repos/{full_name}/pulls/{pr_number}/comments",
         "--jq", "[.[] | {id: .id, user: .user.login, body: .body, path: .path, line: (.line // .original_line), created_at: .created_at}]"],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
        env=gh_safe_env(),
    )
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout)
    except Exception:
        return []


def _fetch_pr_reviews(full_name: str, pr_number: int) -> list[dict]:
    """Fetch PR review submissions (APPROVED, CHANGES_REQUESTED, COMMENTED)."""
    import subprocess
    from src.github.fork import gh_safe_env
    r = subprocess.run(
        ["gh", "api", f"repos/{full_name}/pulls/{pr_number}/reviews",
         "--jq", "[.[] | {id: .id, user: .user.login, state: .state, body: .body, submitted_at: .submitted_at}]"],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
        env=gh_safe_env(),
    )
    if r.returncode != 0:
        return []
    try:
        # Only include reviews with meaningful body or CHANGES_REQUESTED
        items = json.loads(r.stdout)
        return [
            i for i in items
            if i.get("body", "").strip() or i.get("state") == "CHANGES_REQUESTED"
        ]
    except Exception:
        return []


def _fetch_branch_files(fork_full: str, branch_name: str, file_paths: list[str]) -> dict[str, str]:
    """Fetch specific files from a fork branch via GitHub API."""
    import subprocess
    from src.github.fork import gh_safe_env
    files = {}
    for path in file_paths[:10]:  # cap at 10 files
        r = subprocess.run(
            ["gh", "api", f"repos/{fork_full}/contents/{path}?ref={branch_name}",
             "--jq", ".content"],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
            env=gh_safe_env(),
        )
        if r.returncode == 0:
            try:
                import base64
                files[path] = base64.b64decode(r.stdout.strip()).decode("utf-8", errors="replace")
            except Exception:
                pass
    return files


_APPROVED_MARKERS = (
    "lgtm", "looks good", "looks great", "approved", "ship it", "merge it",
    "thank you", "thanks", "great work", "nice work", "good work", "well done",
    ":+1:", "👍", "🎉", "✅",
)
_TEST_MARKERS = (
    "test", "tests", "spec", "coverage", "unittest", "pytest", "assert",
    "missing test", "add test", "need test", "write test",
)
_STYLE_MARKERS = (
    "style", "format", "lint", "pep8", "flake8", "black", "isort",
    "whitespace", "indentation", "naming", "convention",
)
_WRONG_APPROACH_MARKERS = (
    "revert", "undo", "wrong approach", "different approach", "not the right",
    "this is not", "instead you should", "please use", "consider using",
    "this breaks", "this changes behavior", "too broad", "out of scope",
)


def _classify_maintainer_comment(body: str, review_state: str = "") -> str:
    """Classify maintainer comment into one of four categories.

    Returns one of: 'approved', 'needs_test', 'style_issue',
    'wrong_approach', or 'needs_change' (generic).
    """
    lower = body.lower().strip()

    # CHANGES_REQUESTED from review always means needs_change at minimum
    if review_state == "CHANGES_REQUESTED":
        # But still try to be more specific
        if any(m in lower for m in _TEST_MARKERS):
            return "needs_test"
        if any(m in lower for m in _STYLE_MARKERS):
            return "style_issue"
        if any(m in lower for m in _WRONG_APPROACH_MARKERS):
            return "wrong_approach"
        return "needs_change"

    # Short pure-approval comments
    if len(lower) < 120 and any(m in lower for m in _APPROVED_MARKERS):
        if not any(m in lower for m in _TEST_MARKERS + _STYLE_MARKERS + _WRONG_APPROACH_MARKERS):
            return "approved"

    if any(m in lower for m in _WRONG_APPROACH_MARKERS):
        return "wrong_approach"
    if any(m in lower for m in _TEST_MARKERS):
        return "needs_test"
    if any(m in lower for m in _STYLE_MARKERS):
        return "style_issue"
    return "needs_change"


def generate_pr_response(
    entry: dict,
    comment_body: str,
    comment_author: str,
    log: logging.Logger,
    inline_comments: list[dict] | None = None,
    review_state: str = "",
    comment_class: str = "",
) -> PRFeedbackAction:
    """Ask AI what to do in response to maintainer feedback on our PR.

    Handles both issue-level comments and inline review comments.
    Returns a PRFeedbackAction with reply, changed_files, and commit_msg.
    Raises PRGeneratorError if AI fails after retries.
    """
    fork_name     = entry.get("fork_name", "")
    branch_name   = entry.get("branch_name", "")
    pr_title      = entry.get("pr_title", "")
    pr_url        = entry.get("pr_url", "")
    full_name     = entry.get("full_name", "")
    files_changed = entry.get("files_changed", [])

    # Fetch current branch files for context
    # Include files mentioned in inline comments too
    inline_paths = list({c["path"] for c in (inline_comments or []) if c.get("path")})
    all_paths = list(dict.fromkeys(files_changed + inline_paths))  # preserve order, dedupe
    branch_files = _fetch_branch_files(fork_name, branch_name, all_paths)
    files_dump = ""
    for path, content in branch_files.items():
        chunk = f"\n# ── {path} ──\n{content}\n"
        if len(files_dump) + len(chunk) > 20_000:
            break
        files_dump += chunk

    # Build inline comments section
    inline_section = ""
    if inline_comments:
        lines = []
        for c in inline_comments:
            loc = f"{c['path']}:{c['line']}" if c.get('line') else c.get('path', '')
            lines.append(f"  [{loc}] @{c['user']}: {c['body']}")
        inline_section = "\nInline review comments (on specific lines):\n" + "\n".join(lines) + "\n"

    review_section = f"\nReview decision: {review_state}\n" if review_state else ""

    _CLASS_HINT = {
        "needs_test":     "The maintainer is asking for tests. Add or extend test coverage for the changed code.",
        "style_issue":    "The maintainer flagged a style/formatting issue. Fix formatting only — no logic changes.",
        "wrong_approach": "The maintainer wants a different approach. Discuss in the reply; propose an alternative if clear.",
        "needs_change":   "The maintainer wants a code change. Implement exactly what was asked.",
        "approved":       "The maintainer approved. No code changes needed — write a short thank-you reply.",
    }
    class_hint_section = ""
    if comment_class and comment_class in _CLASS_HINT:
        class_hint_section = f"\nFeedback classification: {comment_class} — {_CLASS_HINT[comment_class]}\n"

    prompt = f"""You are a senior engineer who submitted a PR to an open-source repo and received feedback.

PR: {pr_url}
PR title: {pr_title}
Repo: {full_name}
Our branch: {branch_name}

Current state of changed files:
{files_dump if files_dump else "(files not available)"}
{review_section}{class_hint_section}{inline_section}
Maintainer comment from @{comment_author}:
{comment_body}

Task: decide how to respond to this feedback.

If the feedback asks for code changes (add tests, fix a bug, address a finding, fix inline comment):
- Produce the fixed/new file(s) in changed_files
- Write a short professional reply explaining what was done

If the feedback is a question or approval that needs no code change:
- Leave changed_files empty {{}}
- Write a short professional reply

Rules:
- Keep changes minimal and targeted — only fix what was asked
- For inline comments: fix exactly the lines mentioned, do not rewrite surrounding code
- Do not rewrite unrelated code
- Reply must be concise (2-5 sentences max), professional, no fluff

Respond with JSON only:
{{
  "reply": "comment text to post on the PR",
  "commit_msg": "conventional commit message if files changed, else empty string",
  "changed_files": {{
    "relative/path/to/file": "complete new file content"
  }}
}}"""

    for attempt in range(1, 3):
        timeout = get_scaled_timeout(180, attempt)
        log.info("AI generating PR response (attempt %d, timeout=%ds)", attempt, timeout)
        try:
            raw = call_ai(prompt, timeout=timeout)
            result = _parse_json(raw)
        except Exception as exc:
            if attempt == 2:
                raise PRGeneratorError(f"AI call failed: {exc}") from exc
            log.warning("AI attempt %d failed: %s — retrying", attempt, exc)
            time.sleep(3)
            continue

        required = {"reply", "changed_files", "commit_msg"}
        missing = required - set(result.keys())
        if missing:
            if attempt == 2:
                raise PRGeneratorError(f"AI response missing fields: {missing}")
            log.warning("Missing fields in attempt %d: %s — retrying", attempt, missing)
            time.sleep(3)
            continue

        changed_files = result.get("changed_files") or {}

        # Syntax check Python files
        bad_syntax = [
            p for p, c in changed_files.items()
            if p.endswith(".py") and not _syntax_ok(c)
        ]
        if bad_syntax:
            if attempt == 2:
                raise PRGeneratorError(f"Syntax errors in: {bad_syntax}")
            log.warning("Syntax errors in attempt %d: %s — retrying", attempt, bad_syntax)
            time.sleep(3)
            continue

        return PRFeedbackAction(
            pr_url=pr_url,
            full_name=full_name,
            fork_name=fork_name,
            branch_name=branch_name,
            comment_id=entry.get("last_seen_comment_id", 0),
            comment_body=comment_body,
            comment_author=comment_author,
            reply=result["reply"].strip(),
            changed_files=changed_files,
            commit_msg=result.get("commit_msg", "").strip(),
        )

    raise PRGeneratorError("All AI attempts failed")


def check_pr_feedback(log: logging.Logger) -> None:
    """Check all open PRs for new maintainer comments and respond.

    For each open PR in pr_log.json:
    - Fetches comments since last_seen_comment_id
    - Skips bots and the current operator's own comments
    - For each new maintainer comment: generates AI response, applies code fix
      (if needed), pushes to existing branch, posts reply comment
    - Updates last_seen_comment_id in pr_log.json
    """
    import re as _re
    import subprocess
    from src.github.fork import push_to_branch, ForkError
    from src.core.notify import notify

    data = load_pr_log()
    entries = data.get("submitted", [])
    try:
        from src.github.fork import get_current_github_login
        current_login = get_current_github_login().lower()
    except Exception:
        current_login = ""

    def _matches_current_owner(entry: dict) -> bool:
        if not current_login:
            return True
        entry_owner = str(entry.get("owner_login") or "").strip().lower()
        fork_name = str(entry.get("fork_name") or "").strip().lower()
        if entry_owner:
            return entry_owner == current_login
        if fork_name:
            return fork_name.startswith(f"{current_login}/")
        return False

    open_entries = [
        e for e in entries
        if e.get("status", "open") == "open" and "/pull/" in e.get("pr_url", "") and _matches_current_owner(e)
    ]

    from src.core.cli_ui import print_section, print_item, print_ok, print_warn, print_blank

    print_section("Maintainer Feedback")
    if not open_entries:
        print_item("No open PRs.")
        print_blank()
        log.info("No open PRs to check for feedback")
        return

    log.info("Checking %d open PR(s) for maintainer feedback...", len(open_entries))
    changed = False

    def _is_human(user: str) -> bool:
        return not user.endswith("[bot]") and user.lower() != current_login

    for entry in open_entries:
        pr_url    = entry["pr_url"]
        full_name = entry["full_name"]

        # Parse PR number from URL
        m = _re.search(r"/pull/(\d+)$", pr_url)
        if not m:
            continue
        pr_number = int(m.group(1))
        last_seen = entry.get("last_seen_comment_id", 0)

        # Fetch all feedback types
        issue_comments  = _fetch_pr_comments(full_name, pr_number)
        review_comments = _fetch_pr_review_comments(full_name, pr_number)
        reviews         = _fetch_pr_reviews(full_name, pr_number)

        # Filter new human comments from each source
        new_issue = [
            c for c in issue_comments
            if c["id"] > last_seen and _is_human(c["user"])
        ]
        new_inline = [
            c for c in review_comments
            if c["id"] > last_seen and _is_human(c["user"])
        ]
        new_reviews = [
            r for r in reviews
            if r["id"] > last_seen and _is_human(r["user"])
        ]

        all_new = new_issue + new_inline + new_reviews
        if not all_new:
            log.debug("  %s — no new maintainer feedback", pr_url)
            # Advance last_seen to avoid re-checking
            all_ids = (
                [c["id"] for c in issue_comments]
                + [c["id"] for c in review_comments]
                + [r["id"] for r in reviews]
            )
            if all_ids:
                newest_id = max(all_ids)
                if newest_id > last_seen:
                    entry["last_seen_comment_id"] = newest_id
                    changed = True
            continue

        # Summarize what we found
        parts = []
        if new_issue:  parts.append(f"{len(new_issue)} issue comment(s)")
        if new_inline: parts.append(f"{len(new_inline)} inline comment(s)")
        if new_reviews: parts.append(f"{len(new_reviews)} review(s)")
        log.info("  %s — new feedback: %s", pr_url, ", ".join(parts))

        # Pick the latest item as the "trigger" comment
        all_new_sorted = sorted(all_new, key=lambda c: c["id"])
        latest = all_new_sorted[-1]
        comment_author = latest["user"]
        comment_body   = latest.get("body", "").strip()

        # Collect review state if any CHANGES_REQUESTED
        review_state = ""
        for r in new_reviews:
            if r.get("state") == "CHANGES_REQUESTED":
                review_state = "CHANGES_REQUESTED"
                break

        log.info("  Feedback from @%s: %s", comment_author, comment_body[:200])
        _ENGINE_STORE.record_feedback_signal(full_name, "maintainer_feedback")

        # Deterministic pre-classifier — avoids AI token waste on LGTM/trivial comments
        comment_class = _classify_maintainer_comment(comment_body, review_state)
        log.info("  Comment classified as: %s", comment_class)

        if comment_class == "approved":
            # No code change needed — post acknowledgment without AI call
            ack_text = "Thanks for the review! Glad it looks good."
            from src.github.fork import gh_safe_env
            try:
                r = subprocess.run(
                    ["gh", "pr", "comment", pr_url, "--body", ack_text],
                    capture_output=True, text=True, encoding="utf-8", timeout=20,
                    env=gh_safe_env(),
                )
                if r.returncode == 0:
                    log.info("  revision_skipped_approved: %s", pr_url)
                else:
                    log.warning("  Failed to post ack: %s", (r.stdout + r.stderr).strip()[:200])
            except Exception as exc:
                log.warning("  Failed to post ack for PR %s: %s", pr_url, exc)
        else:
            try:
                action = generate_pr_response(
                    entry, comment_body, comment_author, log,
                    inline_comments=new_inline or None,
                    review_state=review_state,
                    comment_class=comment_class,
                )
            except PRGeneratorError as e:
                log.warning("  AI failed for %s: %s — skipping", pr_url, e)
                continue

            # Push code fix if AI produced changed files
            from src.github.fork import gh_safe_env
            if action.changed_files:
                log.info("  Applying %d file(s) to branch %s", len(action.changed_files), action.branch_name)
                try:
                    push_to_branch(
                        fork_full=action.fork_name,
                        branch_name=action.branch_name,
                        changed_files=action.changed_files,
                        commit_msg=action.commit_msg or f"fix: address feedback from @{latest['user']}",
                        log=log,
                    )
                    log.info("  revision_pushed: %s", pr_url)
                except ForkError as e:
                    log.warning("  revision_push_failed: %s — %s", pr_url, e)
            else:
                log.info("  revision_skipped_no_changes: %s", pr_url)

            # Post reply comment
            reply_text = action.reply
            try:
                r = subprocess.run(
                    ["gh", "pr", "comment", pr_url, "--body", reply_text],
                    capture_output=True, text=True, encoding="utf-8", timeout=20,
                    env=gh_safe_env(),
                )
            except Exception as exc:
                log.warning("  Failed to post reply for PR %s: %s", pr_url, exc)
                r = None
            if r is not None and r.returncode == 0:
                log.info("  Reply posted: %s", pr_url)
                notify(
                    f"PR feedback addressed\n"
                    f"Repo: {full_name}\n"
                    f"Comment by: @{latest['user']}\n"
                    f"URL: {pr_url}"
                )
            elif r is not None:
                log.warning("  Failed to post reply: %s", (r.stdout + r.stderr).strip()[:200])

        # Advance last_seen past all fetched IDs
        all_ids = (
            [c["id"] for c in issue_comments]
            + [c["id"] for c in review_comments]
            + [r["id"] for r in reviews]
        )
        if all_ids:
            entry["last_seen_comment_id"] = max(all_ids)
        changed = True
        time.sleep(1)

    print_blank()
    if changed:
        PR_LOG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("PR log updated")
    else:
        print_item("No new feedback on any open PR.")


def check_all_prs(log: logging.Logger) -> None:
    """Unified per-PR loop: status check + feedback check in one pass."""
    import re as _re
    import subprocess
    from src.github.fork import ForkError, get_current_github_login, gh_safe_env, push_to_branch
    from src.core.notify import notify
    from src.core.cli_ui import print_section, print_item, print_ok, print_warn, print_blank

    data = load_pr_log()
    entries = data.get("submitted", [])

    print_section("PR Status")
    if not entries:
        print_item("No PRs in log — nothing to check.")
        print_blank()
        return

    try:
        current_login = get_current_github_login().lower()
    except ForkError:
        current_login = ""

    def _is_human(user: str) -> bool:
        return not user.endswith("[bot]") and user.lower() != current_login

    changed = False

    # Cleanup forks for already-resolved PRs
    for entry in entries:
        if entry.get("status") in ("merged", "closed"):
            if current_login and entry.get("fork_name", "").lower().startswith(f"{current_login}/") and not entry.get("fork_deleted"):
                _delete_fork(entry, log)
                changed = True

    # Collect open PRs (deduplicated)
    seen_urls: set[str] = set()
    open_entries: list[dict] = []
    for e in entries:
        url = e.get("pr_url", "")
        if e.get("status", "open") == "open" and "/pull/" in url and url not in seen_urls:
            seen_urls.add(url)
            open_entries.append(e)

    if not open_entries:
        print_item("No open PRs.")
        print_blank()
        if changed:
            PR_LOG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return

    for entry in open_entries:
        pr_url    = entry.get("pr_url", "")
        full_name = entry.get("full_name", "")
        pr_title  = entry.get("pr_title", "")

        m = _re.search(r"/pull/(\d+)$", pr_url)
        if not m:
            continue
        pr_number_str = m.group(1)
        pr_number_int = int(pr_number_str)

        # ── Status check ──────────────────────────────────────
        try:
            r = subprocess.run(
                ["gh", "api", f"repos/{full_name}/pulls/{pr_number_str}"],
                capture_output=True, text=True, encoding="utf-8", timeout=20,
                env=gh_safe_env(),
            )
            if r.returncode != 0:
                err = r.stderr.strip()
                if "404" in err or "Not Found" in err:
                    entry["status"] = "closed"
                    changed = True
                    print_warn(f"404 (not found)  {full_name}  #{pr_number_str}")
                else:
                    log.warning("Could not fetch PR %s: %s", pr_url, err[:200])
                print_blank()
                time.sleep(0.3)
                continue
            info = json.loads(r.stdout)
        except Exception as exc:
            log.warning("Error checking PR %s: %s", pr_url, exc)
            print_blank()
            continue

        state = info.get("state", "").upper()
        merged_at = info.get("merged_at") or ""
        if state == "CLOSED" and merged_at:
            state = "MERGED"

        review_decision = ""
        if state == "OPEN":
            try:
                rr = subprocess.run(
                    ["gh", "api", f"repos/{full_name}/pulls/{pr_number_str}/reviews"],
                    capture_output=True, text=True, encoding="utf-8", timeout=20,
                    env=gh_safe_env(),
                )
                if rr.returncode == 0:
                    try:
                        rvs = json.loads(rr.stdout)
                        if any((rv.get("state") or "").upper() == "APPROVED" for rv in rvs):
                            review_decision = "APPROVED"
                    except Exception:
                        pass
            except Exception as exc:
                log.warning("Error fetching reviews for PR %s: %s", pr_url, exc)

        if state == "MERGED" and not entry.get("notified_merge"):
            print_ok(f"MERGED  {full_name}  #{pr_number_str}")
            notify(f"PR MERGED\nRepo: {full_name}\nTitle: {pr_title}\nURL: {pr_url}")
            entry["status"]         = "merged"
            entry["notified_merge"] = True
            entry["resolved_at"]    = merged_at or datetime.now(timezone.utc).isoformat()
            changed = True
            _delete_fork(entry, log)
            _ENGINE_STORE.update_pr_status(pr_url, "merged", maintainer_signal="merged", resolved_at=entry["resolved_at"])
            print_blank()
            time.sleep(0.3)
            continue

        if state == "CLOSED" and not entry.get("notified_merge"):
            print_warn(f"CLOSED  {full_name}  {pr_title}")
            print_item("→ rover report  # see rejection patterns")
            notify(f"PR closed (not merged)\nRepo: {full_name}\nTitle: {pr_title}\nURL: {pr_url}")
            entry["status"]         = "closed"
            entry["notified_merge"] = True
            entry["resolved_at"]    = datetime.now(timezone.utc).isoformat()
            changed = True
            _delete_fork(entry, log)
            _ENGINE_STORE.update_pr_status(pr_url, "closed", maintainer_signal="closed", resolved_at=entry["resolved_at"])
            print_blank()
            time.sleep(0.3)
            continue

        if state == "OPEN" and review_decision == "APPROVED" and not entry.get("notified_approved"):
            notify(f"PR APPROVED\nRepo: {full_name}\nTitle: {pr_title}\nURL: {pr_url}")
            entry["notified_approved"] = True
            changed = True
            _ENGINE_STORE.update_pr_status(pr_url, "open", maintainer_signal="approved")

        # ── Open PR: print status line ─────────────────────────
        print_item(f"● open   {full_name}   #{pr_number_str}")

        # ── Feedback check ─────────────────────────────────────
        last_seen = entry.get("last_seen_comment_id", 0)
        issue_comments  = _fetch_pr_comments(full_name, pr_number_int)
        review_comments = _fetch_pr_review_comments(full_name, pr_number_int)
        reviews         = _fetch_pr_reviews(full_name, pr_number_int)

        new_issue   = [c for c in issue_comments  if c["id"] > last_seen and _is_human(c["user"])]
        new_inline  = [c for c in review_comments if c["id"] > last_seen and _is_human(c["user"])]
        new_reviews = [r for r in reviews          if r["id"] > last_seen and _is_human(r["user"])]
        all_new = new_issue + new_inline + new_reviews

        if not all_new:
            print_item("  ‣  no new maintainer feedback")
            all_ids = (
                [c["id"] for c in issue_comments]
                + [c["id"] for c in review_comments]
                + [r["id"] for r in reviews]
            )
            if all_ids:
                newest_id = max(all_ids)
                if newest_id > last_seen:
                    entry["last_seen_comment_id"] = newest_id
                    changed = True
        else:
            parts = []
            if new_issue:   parts.append(f"{len(new_issue)} issue comment(s)")
            if new_inline:  parts.append(f"{len(new_inline)} inline comment(s)")
            if new_reviews: parts.append(f"{len(new_reviews)} review(s)")
            print_item(f"  ‣  new feedback: {', '.join(parts)}")
            log.info("  %s — new feedback: %s", pr_url, ", ".join(parts))

            all_new_sorted = sorted(all_new, key=lambda c: c["id"])
            latest = all_new_sorted[-1]
            comment_author = latest["user"]
            comment_body   = latest.get("body", "").strip()

            review_state = ""
            for rv in new_reviews:
                if rv.get("state") == "CHANGES_REQUESTED":
                    review_state = "CHANGES_REQUESTED"
                    break

            log.info("  Feedback from @%s: %s", comment_author, comment_body[:200])
            _ENGINE_STORE.record_feedback_signal(full_name, "maintainer_feedback")
            notify(
                f"PR NEW COMMENT — auto-reviewing\n"
                f"Repo: {full_name}\n#{pr_number_str}: {pr_title}\n"
                f"From: @{comment_author}\n{comment_body[:200]}\nURL: {pr_url}"
            )

            # Deterministic pre-classifier — avoids AI token waste on LGTM/trivial comments
            comment_class = _classify_maintainer_comment(comment_body, review_state)
            log.info("  Comment classified as: %s", comment_class)

            from src.github.fork import gh_safe_env as _gh_env

            if comment_class == "approved":
                ack_text = "Thanks for the review! Glad it looks good."
                try:
                    rp = subprocess.run(
                        ["gh", "pr", "comment", pr_url, "--body", ack_text],
                        capture_output=True, text=True, encoding="utf-8", timeout=20,
                        env=_gh_env(),
                    )
                    if rp.returncode == 0:
                        log.info("  revision_skipped_approved: %s", pr_url)
                        print_ok(f"  ‣  approved — ack posted")
                    else:
                        log.warning("  Failed to post ack: %s", (rp.stdout + rp.stderr).strip()[:200])
                except Exception as exc:
                    log.warning("  Failed to post ack for PR %s: %s", pr_url, exc)
            else:
                try:
                    action = generate_pr_response(
                        entry, comment_body, comment_author, log,
                        inline_comments=new_inline or None,
                        review_state=review_state,
                        comment_class=comment_class,
                    )
                except PRGeneratorError as e:
                    log.warning("  AI failed for %s: %s — skipping", pr_url, e)
                    print_blank()
                    time.sleep(0.5)
                    continue

                fix_pushed = False
                if action.changed_files:
                    log.info("  Applying %d file(s) to branch %s", len(action.changed_files), action.branch_name)
                    try:
                        push_to_branch(
                            fork_full=action.fork_name,
                            branch_name=action.branch_name,
                            changed_files=action.changed_files,
                            commit_msg=action.commit_msg or f"fix: address feedback from @{latest['user']}",
                            log=log,
                        )
                        fix_pushed = True
                        log.info("  revision_pushed: %s", pr_url)
                    except ForkError as e:
                        log.warning("  revision_push_failed: %s — %s", pr_url, e)
                        notify(
                            f"PR FIX PUSH FAILED\nRepo: {full_name}\n#{pr_number_str}: {pr_title}\n"
                            f"Error: {e}\nURL: {pr_url}"
                        )
                else:
                    log.info("  revision_skipped_no_changes: %s", pr_url)

                try:
                    rp = subprocess.run(
                        ["gh", "pr", "comment", pr_url, "--body", action.reply],
                        capture_output=True, text=True, encoding="utf-8", timeout=20,
                        env=_gh_env(),
                    )
                except Exception as exc:
                    log.warning("  Failed to post reply for PR %s: %s", pr_url, exc)
                    rp = None
                if rp is not None and rp.returncode == 0:
                    log.info("  Reply posted: %s", pr_url)
                    files_info = (
                        f"Fixed {len(action.changed_files)} file(s): "
                        + ", ".join(list(action.changed_files.keys())[:3])
                        if fix_pushed else "No code changes needed"
                    )
                    notify(
                        f"PR AUTO-REVIEWED\nRepo: {full_name}\n#{pr_number_str}: {pr_title}\n"
                        f"From: @{latest['user']}\n{files_info}\nURL: {pr_url}"
                    )
                elif rp is not None:
                    log.warning("  Failed to post reply: %s", (rp.stdout + rp.stderr).strip()[:200])

            all_ids = (
                [c["id"] for c in issue_comments]
                + [c["id"] for c in review_comments]
                + [r["id"] for r in reviews]
            )
            if all_ids:
                entry["last_seen_comment_id"] = max(all_ids)
            changed = True

        print_blank()
        time.sleep(0.5)

    if changed:
        PR_LOG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("PR log updated")
    else:
        print_item("No status changes or new feedback.")
    print_blank()


# ── Search for PR target ──────────────────────────────────────
_FOLLOWUP_COOLDOWN_DAYS = int(os.getenv("PR_FOLLOWUP_COOLDOWN_DAYS", "30"))


def get_followup_candidates(
    blacklisted: set[str],
    already_prd: set[str],
) -> list[str]:
    """Return full_names of merged repos eligible for a follow-up PR.

    Eligible = merged at least once, not currently open (no open PR from us),
    and cooldown has passed since last submission to that repo.
    Returns list sorted by most-recently-merged first.
    """
    try:
        from src.github.fork import get_current_github_login
        current_login = get_current_github_login().lower()
    except Exception:
        current_login = ""

    entries = load_pr_log().get("submitted", [])
    now = datetime.now(timezone.utc)

    # Group entries by repo
    by_repo: dict[str, list[dict]] = {}
    for e in entries:
        fn = e.get("full_name", "").lower()
        by_repo.setdefault(fn, []).append(e)

    eligible = []
    for full_name, repo_entries in by_repo.items():
        if full_name in blacklisted:
            continue
        if full_name in already_prd:
            # Already attempted (or excluded) earlier in this run — re-surfacing it
            # makes the run loop retry the same target every attempt, burning AI
            # cycles on a candidate that already failed. Skip it.
            continue
        if current_login and full_name.startswith(f"{current_login}/"):
            continue

        statuses = {e.get("status", "open") for e in repo_entries}

        # Must have at least one merged PR
        if "merged" not in statuses:
            continue

        # Must not have an open PR right now
        if "open" in statuses:
            continue

        # Cooldown: check most recent submission date
        submitted_dates = []
        for e in repo_entries:
            try:
                submitted_dates.append(
                    datetime.fromisoformat(e["submitted_at"]).replace(tzinfo=timezone.utc)
                    if e.get("submitted_at") else None
                )
            except Exception:
                pass
        submitted_dates = [d for d in submitted_dates if d]
        if submitted_dates:
            last_submitted = max(submitted_dates)
            days_since = (now - last_submitted).days
            if days_since < _FOLLOWUP_COOLDOWN_DAYS:
                continue

        # Collect the most recent merged_at for sorting
        merged_entries = [e for e in repo_entries if e.get("status") == "merged"]
        last_merged_at = ""
        for e in merged_entries:
            if e.get("submitted_at", "") > last_merged_at:
                last_merged_at = e["submitted_at"]

        eligible.append((full_name, last_merged_at))

    # Sort by most recently merged first
    eligible.sort(key=lambda x: x[1], reverse=True)
    return [fn for fn, _ in eligible]


def find_pr_target(
    blacklisted: set[str],
    already_prd: set[str],
    log: logging.Logger,
    *,
    first_pr_mode: bool = False,
    override_limits: bool = False,
) -> RepoCandidate | None:
    """Search GitHub for an active Python/TypeScript repo worth contributing to.

    Checks follow-up candidates (already-merged repos) first before searching.
    Criteria for contribution targets:
    - Stars: PR_MIN_STARS..PR_MAX_STARS
    - Pushed within PR_MAX_PUSHED_DAYS
    - Has open issues (signal that maintainer is engaged)
    - Allowed license
    - Not the current operator's own repo
    - Not already PR'd or blacklisted
    """
    from src.core.cli_ui import print_item, print_ok, print_blank

    shortlister = RepoShortlister(_ENGINE_STORE)
    log.info("Contribution lane: %s", _LANE_NAME)
    if first_pr_mode:
        log.info("First-PR mode: enabled")
    if override_limits:
        log.warning("Contribution limit override is enabled for this run")
    try:
        from src.github.fork import get_current_github_login
        current_login = get_current_github_login().lower()
    except Exception:
        current_login = ""

    # ── Follow-up pass: prioritize repos where we already merged ─
    followup = get_followup_candidates(blacklisted, already_prd)
    if followup:
        log.info("Follow-up candidates available: %s", followup[:3])
        for full_name in followup:
            log.info("  Trying follow-up: %s", full_name)
            print_item(f"follow-up  [bold]{full_name}[/] ...")
            try:
                candidate = fetch_repo_candidate(full_name, log, override_limits=override_limits)
                log.info(
                    "  Follow-up target: %s (%d★, pushed %dd ago)",
                    full_name, candidate.stars, candidate.pushed_days_ago,
                )
                _ENGINE_STORE.record_repo_event(
                    _ACTIVE_RUN_ID,
                    full_name,
                    "discover_selected",
                    "Selected merged follow-up repo for another contribution pass.",
                    {"lane": "followup"},
                )
                return candidate
            except ScraperError as exc:
                log.info("  skip follow-up %s — %s", full_name, exc)
                print_item(f"[dim]skip  {full_name} — {exc}[/]")
            except Exception as exc:
                log.info("  skip follow-up %s — unexpected: %s", full_name, exc)
            time.sleep(0.5)
    shortlisted: list[tuple[int, RepoCandidate]] = []

    search_queries = (
        list(_LANE_PRESETS[_DEFAULT_LANE_NAME].get("queries", []))
        if override_limits
        else _PR_SEARCH_QUERIES
    )

    for lang, query in search_queries:
        log.info("PR target search [%s]: %r", lang, query)
        print_item(f"[dim]query  {lang}: {query}[/]")
        full_query = f"{query} language:{lang}"
        if not override_limits:
            full_query = f"{full_query} stars:{_PR_MIN_STARS}..{_PR_MAX_STARS}"
        try:
            data = _gh_get(
                f"{_GITHUB_API}/search/repositories",
                params={
                    "q": full_query,
                    "sort": "stars",
                    "order": "desc",
                    "per_page": 20,
                },
            )
        except Exception as exc:
            log.warning("Search failed for %r: %s", query, exc)
            continue

        for item in data.get("items", []):
            full_name = item.get("full_name", "")

            if current_login and full_name.lower().startswith(f"{current_login}/"):
                continue
            if full_name.lower() in already_prd or full_name.lower() in blacklisted:
                continue
            if item.get("archived") or item.get("disabled") or item.get("fork"):
                continue

            stars = item.get("stargazers_count", 0)
            if not override_limits and not (_PR_MIN_STARS <= stars <= _PR_MAX_STARS):
                continue
            if not override_limits and item.get("forks_count", 0) < _PR_MIN_FORKS:
                continue
            if not override_limits and item.get("open_issues_count", 0) < _PR_MIN_ISSUES:
                continue

            pushed_at = item.get("pushed_at", "")
            try:
                pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
                pushed_days_ago = (datetime.now(timezone.utc) - pushed).days
            except Exception:
                pushed_days_ago = 999
            if not override_limits and pushed_days_ago > _PR_MAX_PUSHED:
                continue

            lic = _get_license(item)
            if lic not in _ALLOWED_LICENSES:
                continue

            candidate = RepoCandidate(
                name=item["name"],
                full_name=full_name,
                description=(item.get("description") or "")[:200],
                stars=stars,
                forks=item.get("forks_count", 0),
                license=lic,
                url=item["html_url"],
                default_branch=item.get("default_branch", "main"),
                pushed_days_ago=pushed_days_ago,
                topics=item.get("topics", []),
                archived=bool(item.get("archived")),
                disabled=bool(item.get("disabled")),
            )

            if not _metadata_security_ok(candidate):
                log.info("  skip %s — suspicious metadata", full_name)
                continue

            if not override_limits and not _matches_contribution_lane(candidate):
                log.info("  skip %s — does not match current contribution lane", full_name)
                continue

            from src.core.cli_ui import _console as _ui_console
            _skip_msg: str | None = None
            _accept: int | None = None
            _py = _ts = _tests = 0
            with _ui_console.status(
                f"  [dim cyan]↻[/]  [dim]{full_name}  {stars}★  {lic}  pushed {pushed_days_ago}d ago[/]",
                spinner="dots",
                spinner_style="dim cyan",
            ):
                try:
                    candidate.files = download_repo_files(
                        candidate,
                        max_py=None if override_limits else _PR_MAX_PY_FILES,
                        max_total=None if override_limits else _PR_MAX_TOTAL_FILES,
                    )
                    if not candidate.files:
                        _skip_msg = "no files downloaded"
                    else:
                        try:
                            candidate.maintainer_signals = fetch_maintainer_signals(candidate)
                        except Exception:
                            pass
                        _py, _ts, _tests = _validate_candidate_scope(
                            candidate,
                            targeted=False,
                            override_limits=override_limits,
                        )
                        if first_pr_mode and not override_limits:
                            first_pr_friendly, reason = _first_pr_repo_fit(candidate, candidate.files)
                            if not first_pr_friendly:
                                log.info("  skip %s — not first-PR friendly (%s)", full_name, reason)
                                _skip_msg = reason
                        if _skip_msg is None:
                            _accept = shortlister.score(candidate, _acceptance_score(candidate, candidate.files))
                            log.info(
                                "Candidate PR target: %s (%d★, %d open issues, %s license, py=%d ts=%d tests=%d score=%d)",
                                full_name, stars, item.get("open_issues_count", 0), lic, _py, _ts, _tests, _accept,
                            )
                except ScraperError as exc:
                    log.info("  skip %s — %s", full_name, exc)
                    _skip_msg = str(exc).split("—")[0].strip() if "—" in str(exc) else str(exc)[:60]
                except Exception as exc:
                    log.info("  skip %s — unexpected: %s", full_name, exc)
                    _skip_msg = "unexpected error"

            if _skip_msg is not None:
                print_item(f"[dim]skip  {full_name} — {_skip_msg}[/]")
                time.sleep(0.5)
                continue

            if _accept is not None:
                print_ok(f"queued  [bold]{full_name}[/]  [dim]score={_accept}  py={_py}  tests={_tests}[/]")
                shortlisted.append((_accept, candidate))
                shortlisted.sort(key=lambda x: x[0], reverse=True)
                if len(shortlisted) >= _PR_ACCEPTANCE_SHORTLIST:
                    best_score, best_candidate = shortlisted[0]
                    log.info(
                        "Selected best PR target so far: %s (acceptance score=%d)",
                        best_candidate.full_name, best_score,
                    )
                    _ENGINE_STORE.record_repo_event(
                        _ACTIVE_RUN_ID,
                        best_candidate.full_name,
                        "discover_selected",
                        "Selected repo from shortlist based on acceptance-first scoring.",
                        {"score": best_score, "lane": "search"},
                    )
                    return best_candidate
            time.sleep(0.5)

    if shortlisted:
        best_score, best_candidate = max(shortlisted, key=lambda item: item[0])
        log.info(
            "Selected best PR target after search: %s (acceptance score=%d)",
            best_candidate.full_name, best_score,
        )
        _ENGINE_STORE.record_repo_event(
            _ACTIVE_RUN_ID,
            best_candidate.full_name,
            "discover_selected",
            "Selected best remaining repo after exhausting shortlist.",
            {"score": best_score, "lane": "search"},
        )
        return best_candidate

    return None


# ── Dependency update helpers ─────────────────────────────────
_PYPI_API = "https://pypi.org/pypi/{package}/json"
_NPM_API  = "https://registry.npmjs.org/{package}/latest"

# Packages that should never be bumped (internal, git-sourced, or intentionally pinned)
_DEP_SKIP_PREFIXES = ("genotools", "solanakit", "gw", "git+", "http")
_DEP_SKIP_EXACT = {"python", "pip", "setuptools", "wheel", "pkg_resources"}


def _fetch_json_url(url: str, timeout: int = 8) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rover/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _check_pypi_latest(package: str) -> str | None:
    data = _fetch_json_url(_PYPI_API.format(package=urllib.parse.quote(package, safe="")))
    return data.get("info", {}).get("version") if data else None


def _check_npm_latest(package: str) -> str | None:
    data = _fetch_json_url(_NPM_API.format(package=urllib.parse.quote(package, safe="")))
    return data.get("version") if data else None


def _version_tuple(v: str) -> tuple:
    """Convert semver-like string to comparable int tuple, ignoring pre-release tags."""
    base = re.split(r"[ab+]|rc", v)[0]
    parts = []
    for part in re.split(r"[._-]", base):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    try:
        return _version_tuple(latest) > _version_tuple(current)
    except Exception:
        return False


def _semver_triplet(v: str) -> tuple[int, int, int] | None:
    base = re.split(r"[ab+]|rc", v)[0]
    parts: list[int] = []
    for part in re.split(r"[._-]", base):
        if not part:
            continue
        try:
            parts.append(int(part))
        except ValueError:
            return None
    if not parts:
        return None
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _is_high_risk_dep_bump(current: str, latest: str) -> bool:
    current_triplet = _semver_triplet(current)
    latest_triplet = _semver_triplet(latest)
    if not current_triplet or not latest_triplet:
        return True
    current_major, current_minor, _ = current_triplet
    latest_major, latest_minor, _ = latest_triplet
    if latest_major != current_major:
        return True
    if current_major == 0 and latest_minor != current_minor:
        return True
    return False


def _replace_json_dependency_version(manifest: str, package: str, old_ver: str, new_ver: str) -> tuple[str, bool]:
    pattern = re.compile(
        rf'("{re.escape(package)}"\s*:\s*")(?P<prefix>[~^><=]*)(?P<version>{re.escape(old_ver)})(")'
    )
    updated_manifest, count = pattern.subn(
        lambda m: f'{m.group(1)}{m.group("prefix")}{new_ver}"',
        manifest,
        count=1,
    )
    return updated_manifest, count == 1


def _record_dep_update_opportunity(
    candidate: "RepoCandidate",
    updates: list[tuple[str, str, str]],
    *,
    target_file: str,
) -> int | None:
    if _ACTIVE_RUN_ID is None:
        return None
    summary = ", ".join(f"{pkg} {old}->{new}" for pkg, old, new in updates)
    acceptance_score = min(95, max(80, _acceptance_score(candidate, candidate.files)))
    opportunity = Opportunity(
        repo_full_name=candidate.full_name,
        target_file=target_file,
        pattern_type="dep_update",
        failure_mode="Outdated pinned dependencies in the manifest can miss upstream fixes or compatibility improvements.",
        evidence=f"Registry comparison found conservative version bumps for: {summary}.",
        patch_scope=1,
        test_target="",
        acceptance_score=acceptance_score,
        opportunity_kind="bugfix",
        source_ref=f"manifest:{target_file}",
        state="READY",
    )
    opportunity_id = _ENGINE_STORE.create_opportunity(_ACTIVE_RUN_ID, opportunity, source="manifest_scan")
    _ENGINE_STORE.transition_opportunity(
        opportunity_id,
        "READY",
        why_advanced="Conservative dependency update prepared without AI generation.",
    )
    return opportunity_id


def _parse_requirements_pinned(content: str) -> list[tuple[str, str]]:
    """Return (package, version) pairs for lines with exact == pins."""
    results = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-", "git+", "http")):
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+]+)\s*(?:#.*)?$", line)
        if m:
            results.append((m.group(1), m.group(2)))
    return results


def generate_dep_update(
    candidate: "RepoCandidate",
    log: logging.Logger,
) -> "PRImprovement | None":
    """Check PyPI/npm for outdated pinned dependencies, return a version-bump PRImprovement.

    Does NOT call AI — version data comes directly from registry APIs.
    Returns None if no outdated packages found or files unchanged.
    """
    req_content = candidate.files.get("requirements.txt", "")
    pkg_content = candidate.files.get("package.json", "")

    if not req_content and not pkg_content:
        return None

    updates: list[tuple[str, str, str]] = []  # (pkg, old_ver, new_ver)

    # ── Python: requirements.txt ──────────────────────────────
    if req_content:
        for pkg, current in _parse_requirements_pinned(req_content)[:30]:
            if any(pkg.lower().startswith(p) for p in _DEP_SKIP_PREFIXES):
                continue
            if pkg.lower() in _DEP_SKIP_EXACT:
                continue
            latest = _check_pypi_latest(pkg)
            if latest and _is_newer(latest, current):
                if _is_high_risk_dep_bump(current, latest):
                    log.info("  dep update skipped as high-risk: %s %s -> %s", pkg, current, latest)
                    continue
                updates.append((pkg, current, latest))
                log.info("  dep update candidate: %s %s -> %s", pkg, current, latest)
            time.sleep(0.12)
            if len(updates) >= 8:
                break

    # ── TypeScript: package.json ──────────────────────────────
    elif pkg_content:
        # Skip repos that use a non-npm lock file — we can only run `npm install`
        # for verification, so bun/yarn/pnpm repos will always fail at submission.
        _non_npm_locks = {"bun.lock", "bun.lockb", "yarn.lock", "pnpm-lock.yaml"}
        if _non_npm_locks & set(candidate.files):
            log.info("  dep update skipped: repo uses non-npm lock file (%s)",
                     ", ".join(_non_npm_locks & set(candidate.files)))
            return None
        try:
            pkg_data = json.loads(pkg_content)
        except Exception:
            return None
        all_deps = {
            **pkg_data.get("dependencies", {}),
            **pkg_data.get("devDependencies", {}),
        }
        for pkg, version_spec in list(all_deps.items())[:40]:
            if not isinstance(version_spec, str):
                continue
            current = version_spec.lstrip("^~>=")
            if not re.match(r"^\d+\.\d+", current):
                continue
            if any(pkg.lower().startswith(p) for p in _DEP_SKIP_PREFIXES):
                continue
            latest = _check_npm_latest(pkg)
            if latest and _is_newer(latest, current):
                if _is_high_risk_dep_bump(current, latest):
                    log.info("  dep update skipped as high-risk: %s %s -> %s", pkg, current, latest)
                    continue
                updates.append((pkg, current, latest))
                log.info("  dep update candidate: %s %s -> %s", pkg, current, latest)
            time.sleep(0.12)
            if len(updates) >= 8:
                break

    if not updates:
        log.info("  no outdated pinned deps found in %s", candidate.full_name)
        return None

    updates = updates[:5]  # keep PRs small and focused

    # ── Apply updates ──────────────────────────────────────────
    changed_files: dict[str, str] = {}

    applied_updates: list[tuple[str, str, str]] = []

    if req_content:
        new_req = req_content
        for pkg, old_ver, new_ver in updates:
            new_req, count = re.subn(
                rf"(?m)^({re.escape(pkg)})=={re.escape(old_ver)}",
                rf"\1=={new_ver}",
                new_req,
                flags=re.IGNORECASE,
            )
            if count:
                applied_updates.append((pkg, old_ver, new_ver))
        if new_req.strip() != req_content.strip():
            changed_files["requirements.txt"] = new_req

    elif pkg_content:
        new_pkg = pkg_content
        for pkg, old_ver, new_ver in updates:
            updated_pkg, changed = _replace_json_dependency_version(new_pkg, pkg, old_ver, new_ver)
            if changed:
                new_pkg = updated_pkg
                applied_updates.append((pkg, old_ver, new_ver))
        if new_pkg.strip() != pkg_content.strip():
            changed_files["package.json"] = new_pkg

    if not changed_files:
        return None

    if not applied_updates:
        return None

    updates = applied_updates
    n = len(updates)
    pkg_summary = ", ".join(f"`{p}` {o}->{nv}" for p, o, nv in updates)
    pr_title = f"chore(deps): bump {n} outdated dependenc{'y' if n == 1 else 'ies'}"
    pr_body = (
        "## Summary\n"
        f"Updates {n} pinned dependenc{'y' if n == 1 else 'ies'} to their latest "
        f"released versions: {pkg_summary}.\n\n"
        "## Why it matters\n"
        "Keeping pinned dependencies current reduces exposure to known CVEs and "
        "ensures compatibility with downstream packages.\n\n"
        "## Testing\n"
        "Registry versions were checked first. The submission flow must also pass "
        "repo-local verification before opening this PR."
    )
    target_file = "requirements.txt" if "requirements.txt" in changed_files else "package.json"
    opportunity_id = _record_dep_update_opportunity(candidate, updates, target_file=target_file)

    return PRImprovement(
        title=pr_title,
        body=pr_body,
        improvement_type="dep_update",
        changed_files=changed_files,
        rationale=f"Bumps {n} outdated pinned deps: {', '.join(f'{p} {o}->{nv}' for p, o, nv in updates)}",
        opportunity_id=opportunity_id,
        target_file=target_file,
        pattern_type="dep_update",
    )


# ── Diff safety helpers ───────────────────────────────────────
_ELSE_IF_RE = re.compile(
    r"(?:else\s+if\s*\(|elif\s+)",
    re.MULTILINE,
)
_BRANCH_BODY_RE = re.compile(
    r"(?:if|else\s+if|elif)\s*[(\[]?[^){:\n]{3,80}[):\]]?\s*\{?\s*\n\s*(\w+\s*\([^)]{0,60}\))",
    re.MULTILINE,
)


def _check_diff_safety(
    original_files: dict[str, str],
    changed_files: dict[str, str],
    log: logging.Logger,
) -> str | None:
    """Detect diffs that silently remove a branch of an exhaustive conditional.

    Returns a rejection reason string, or None if the diff looks safe.

    Specifically catches: a file that previously had N else-if/elif branches
    now has fewer, meaning a condition was removed rather than truly deduplicated.
    """
    for path, new_content in changed_files.items():
        original = original_files.get(path, "")
        if not original:
            continue

        # Count else-if / elif branches in original vs new
        orig_elseif_count = len(_ELSE_IF_RE.findall(original))
        new_elseif_count  = len(_ELSE_IF_RE.findall(new_content))

        if new_elseif_count < orig_elseif_count:
            removed = orig_elseif_count - new_elseif_count
            log.warning(
                "Diff safety: %s had %d else-if/elif branches, new has %d — %d branch(es) removed",
                path, orig_elseif_count, new_elseif_count, removed,
            )
            return (
                f"{path}: removed {removed} else-if/elif branch(es). "
                "Two branches with the same body but different conditions are NOT duplicates — "
                "they handle distinct input cases. Removing one changes behavior."
            )

        # Detect net line deletion above a threshold in non-test files
        if "test" not in path.lower():
            orig_lines = len([l for l in original.splitlines() if l.strip()])
            new_lines  = len([l for l in new_content.splitlines() if l.strip()])
            deleted_pct = (orig_lines - new_lines) / max(orig_lines, 1)
            if deleted_pct > 0.15 and orig_lines - new_lines > 10:
                log.warning(
                    "Diff safety: %s lost %d%% of non-blank lines (%d→%d) — suspicious mass deletion",
                    path, int(deleted_pct * 100), orig_lines, new_lines,
                )
                return (
                    f"{path}: deleted {int(deleted_pct*100)}% of non-blank lines ({orig_lines}→{new_lines}). "
                    "Mass deletions are high-risk — only targeted fixes are acceptable."
                )

    return None


def _is_style_only_change(
    original_files: dict[str, str],
    changed_files: dict[str, str],
) -> bool:
    """Return True if every changed file is semantically identical to its original.

    Uses AST equality for Python files, so a comment/whitespace/formatting-only
    edit is detected deterministically and can be rejected without spending an AI
    self-review call. Any new file, non-Python file, parse failure, or AST
    difference means the patch carries a real change and is NOT style-only.
    """
    compared = 0
    for path, new_content in changed_files.items():
        original = original_files.get(path, "")
        if not original:
            return False  # new file → genuine addition, not cosmetic
        if not path.endswith(".py"):
            return False  # cannot prove cosmetic-only without a parser
        try:
            orig_tree = ast.parse(original)
            new_tree = ast.parse(new_content)
        except SyntaxError:
            return False
        if ast.dump(orig_tree) != ast.dump(new_tree):
            return False  # genuine semantic change
        compared += 1
    return compared > 0


def _self_review_diff(
    candidate: "RepoCandidate",
    changed_files: dict[str, str],
    result: dict,
    log: logging.Logger,
    plan: PatchPlan | None = None,
) -> str | None:
    """Ask a second AI call to play skeptical reviewer.

    Returns a rejection reason if the reviewer finds problems, else None.
    Skips if no original content exists for comparison (new file additions).
    """
    # Deterministic pre-gate: reject AST-identical (style-only) patches without
    # spending an AI call. The semantic AI review fails open on error, so this
    # also closes the gap where a cosmetic patch could slip through.
    if _is_style_only_change(candidate.files, changed_files):
        log.warning("Self-review REJECTED: style-only change (AST identical to original)")
        return (
            "Patch is style-only: the abstract syntax tree is identical to the "
            "original, so it changes only comments, whitespace, or formatting "
            "with no correctness value."
        )

    # Build before/after pairs for files that exist in the original
    pairs = []
    for path, new_content in changed_files.items():
        original = candidate.files.get(path, "")
        if not original:
            continue
        # Only show the changed region — find first and last differing line
        orig_lines = original.splitlines()
        new_lines  = new_content.splitlines()
        first_diff = next(
            (i for i, (a, b) in enumerate(zip(orig_lines, new_lines)) if a != b),
            0,
        )
        ctx_start = max(0, first_diff - 5)
        orig_excerpt = "\n".join(orig_lines[ctx_start:ctx_start + 60])
        new_excerpt  = "\n".join(new_lines[ctx_start:ctx_start + 60])
        pairs.append(
            f"### {path}\n\n**BEFORE:**\n```\n{orig_excerpt}\n```\n\n**AFTER:**\n```\n{new_excerpt}\n```"
        )

    if not pairs:
        return None  # all new files — nothing to review

    plan_section = ""
    if plan is not None:
        plan_section = f"""
Approved patch plan:
- target_file: {plan.target_file}
- failure_mode: {plan.failure_mode}
- expected_files: {plan.expected_files}
- test_target: {plan.test_target or "none"}
- why_narrow: {plan.why_narrow}
- proof_path: {plan.proof_path}
"""

    review_prompt = f"""You are a skeptical senior code reviewer for an open-source crypto project.
A contributor claims the following change is a safe bug fix or improvement.
Your job: find any case where this change alters behavior for a valid input OR escapes the approved narrow patch plan.

Proposed improvement type: {result.get('improvement_type')}
Proposed title: {result.get('pr_title')}
Contributor's safety proof: {result.get('safety_proof', '')}
{plan_section}

{'---'.join(pairs)}

Answer these questions:
1. Is there ANY input combination (parameter values, state, timing) for which the BEFORE code would execute a code path that the AFTER code does not?
2. Does the change remove a branch that handles a distinct case, even if the branch body looks similar to another branch?
3. Is this change purely cosmetic (style, rename, comment rewrite) with no correctness value?
4. If an approved patch plan is present, does the change drift away from the approved dominant failure mode, proof path, or file scope?

Respond with JSON only:
{{
  "safe": true/false,
  "reason": "one sentence explanation — if safe=true say why; if safe=false name the exact input case, plan drift, or behavior change"
}}"""

    review: dict | None = None
    last_exc: Exception | None = None
    for review_attempt in range(2):
        try:
            raw = call_ai(review_prompt, timeout=get_scaled_timeout(120, 1))
            review = _parse_json(raw)
            break
        except Exception as exc:
            last_exc = exc
            log.warning("Self-review AI call failed (attempt %d/2): %s", review_attempt + 1, exc)
    if review is None:
        # Fail closed: a change we could not review is not proven safe. Rejecting a
        # patch we cannot verify protects maintainer trust far more than shipping an
        # unreviewed diff would. The retry above absorbs transient backend blips.
        log.warning("Self-review could not complete — rejecting unverified patch")
        return f"self-review could not complete ({last_exc}) — patch not verified safe"

    if not review.get("safe", True):
        reason = review.get("reason", "reviewer found a problem")
        log.warning("Self-review REJECTED: %s", reason)
        return reason

    log.info("Self-review passed: %s", review.get("reason", "ok"))
    return None


def _self_review_test_layout(
    candidate: "RepoCandidate",
    opportunity: Opportunity,
    changed_files: dict[str, str],
) -> str | None:
    expected_root = expected_test_root(candidate.files, opportunity.target_file)
    if not expected_root:
        return None

    changed_test_files = [
        path for path in changed_files
        if test_root_for_path(path) and (
            path.replace("\\", "/").endswith(("_test.py", ".test.ts", ".spec.ts"))
            or "/test" in path.replace("\\", "/").lower()
            or "/tests/" in path.replace("\\", "/").lower()
        )
    ]
    if not changed_test_files:
        return None

    for path in changed_test_files:
        normalized = path.replace("\\", "/")
        root = test_root_for_path(normalized)
        if root != expected_root and path not in candidate.files:
            return (
                f"new test file {path} does not match the repository test layout; "
                f"expected root near {expected_root}"
            )

    existing_import_lines: list[str] = []
    for path, content in candidate.files.items():
        normalized = path.replace("\\", "/")
        if test_root_for_path(normalized) != expected_root:
            continue
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("from ") or stripped.startswith("import "):
                existing_import_lines.append(stripped)

    prefers_non_agent_imports = any(
        line.startswith("from backtest ") or line.startswith("from backtest.") or line == "import backtest"
        for line in existing_import_lines
    )
    if not prefers_non_agent_imports:
        return None

    for path in changed_test_files:
        for line in changed_files[path].splitlines():
            stripped = line.strip()
            if stripped.startswith("from agent.") or stripped.startswith("import agent."):
                return (
                    f"new test file {path} uses agent.* imports, but the existing suite under "
                    f"{expected_root} imports the package directly"
                )
    return None


def _estimate_diff_lines(candidate: "RepoCandidate", changed_files: dict[str, str]) -> int:
    total = 0
    for path, new_content in changed_files.items():
        original = candidate.files.get(path, "")
        total += abs(len(new_content.splitlines()) - len(original.splitlines()))
    return total


def _validate_patch_plan(candidate: "RepoCandidate", opportunity: Opportunity, plan: PatchPlan) -> str | None:
    target_file = opportunity.target_file.replace("\\", "/")
    if plan.target_file.replace("\\", "/") != target_file:
        return f"Patch plan drifted away from the chosen target file ({target_file})."
    if len(plan.expected_files) == 0 or len(plan.expected_files) > _TARGETED_MAX_CHANGED_FILES:
        return "Patch plan needs more than the allowed narrow file budget."
    normalized_expected = [path.replace("\\", "/") for path in plan.expected_files]
    if target_file not in normalized_expected:
        return "Patch plan does not include the chosen target file."
    if normalized_expected[0] != target_file:
        return "Patch plan must list the chosen target file first in expected_files."
    if _looks_like_core_target(plan.target_file) and len(normalized_expected) > 1:
        return "Patch plan expands a core file fix beyond a narrow isolated patch."
    if len(plan.why_narrow.split()) < 6:
        return "Patch plan does not justify narrowness concretely."
    if len(plan.proof_path.split()) < 4:
        return "Patch plan does not name a concrete proof path."
    expected_root = expected_test_root(candidate.files, opportunity.target_file)
    if plan.test_target and expected_root:
        proposed_root = test_root_for_path(plan.test_target)
        if proposed_root != expected_root and plan.test_target not in candidate.files:
            return f"Patch plan test target does not match repo test layout (expected root near {expected_root})."
    return None


def _plan_pr_patch(
    candidate: "RepoCandidate",
    opportunity: Opportunity,
    focused_dump: str,
    dep_section: str,
    is_typescript: bool,
    log: logging.Logger,
) -> PatchPlan:
    _set_run_stage(
        "plan",
        candidate.full_name,
        {
            "target_file": opportunity.target_file,
            "pattern_type": opportunity.pattern_type,
            "pattern_policy": _targeted_pattern_policy(opportunity).mode,
        },
    )
    lang_label = "TypeScript" if is_typescript else "Python"
    file_ext = ".ts" if is_typescript else ".py"
    prompt = f"""You are a senior {lang_label} engineer preparing a minimal open-source bugfix PR.

Repo: {candidate.full_name}
Chosen target:
- target_file: {opportunity.target_file}
- pattern_type: {opportunity.pattern_type}
- failure_mode: {opportunity.failure_mode}
- evidence: {opportunity.evidence}
- preferred_test_target: {opportunity.test_target or "none"}

Relevant source:
{focused_dump}{dep_section}

Return a narrow patch plan before any code is written.

Hard rules:
- exactly one dominant failure mode
- prefer one changed source file plus at most one test file
- do not expand into refactors, cleanup, or unrelated modules
- do not move away from the chosen target file
- copy the chosen `target_file` verbatim into `target_file`
- `expected_files[0]` must equal the chosen `target_file`
- every item in `expected_files` must be an exact relative repo path, not a guess, alias, or module name
- if you cannot keep the plan narrow, fail

Respond with JSON only:
{{
  "target_file": "exact file path",
  "failure_mode": "one sentence",
  "expected_files": ["relative/path{file_ext}", "optional/test/file"],
  "test_target": "focused test file or empty string",
  "why_narrow": "one sentence explaining why this stays local",
  "proof_path": "one sentence describing the concrete assertion, error path, or regression proof"
}}"""
    last_error = "patch plan failed"
    for attempt in range(1, _TARGETED_PLAN_ATTEMPTS + 1):
        before_tokens = _usage_tokens()
        try:
            raw = call_ai(prompt, timeout=get_scaled_timeout(120, attempt))
            _record_stage_token_spend("plan", before_tokens)
            parsed = _parse_json(raw)
            plan = PatchPlan(
                target_file=str(parsed.get("target_file") or "").strip(),
                failure_mode=str(parsed.get("failure_mode") or "").strip(),
                expected_files=[str(item).strip() for item in list(parsed.get("expected_files") or []) if str(item).strip()],
                test_target=str(parsed.get("test_target") or "").strip(),
                why_narrow=str(parsed.get("why_narrow") or "").strip(),
                proof_path=str(parsed.get("proof_path") or "").strip(),
            )
        except Exception as exc:
            last_error = f"patch plan AI call failed: {exc}"
            continue
        # Detect self-rejection: plan AI acknowledging the opportunity doesn't apply to this file.
        # When this happens, continuing to generate is pointless — fail fast so the caller
        # can select a different opportunity rather than burning 320s on generate timeouts.
        _PLAN_SELF_REJECT_PHRASES = (
            "cannot plan", "not present in this", "does not map to", "should be rejected",
            "no response parsing", "refusing to expand", "not applicable", "cannot identify",
            "cannot fix", "does not contain", "no concrete", "no real logic",
        )
        self_reject = any(
            phrase in plan.failure_mode.lower() or phrase in plan.why_narrow.lower()
            for phrase in _PLAN_SELF_REJECT_PHRASES
        )
        if self_reject:
            raise PRGeneratorError(
                f"Plan AI self-rejected this opportunity: {plan.failure_mode[:120]}"
            )

        rejection = _validate_patch_plan(candidate, opportunity, plan)
        if rejection:
            last_error = rejection
            if any(marker in rejection for marker in _PATCH_PLAN_DRIFT_MARKERS):
                raise PRGeneratorError(f"{rejection} Patch-plan drift kill switch engaged.")
            continue
        _bump_run_metric("planned")
        _ACTIVE_RUN_METRICS["last_patch_plan"] = {
            "target_file": plan.target_file,
            "failure_mode": plan.failure_mode,
            "expected_files": list(plan.expected_files),
            "test_target": plan.test_target,
            "why_narrow": plan.why_narrow,
            "proof_path": plan.proof_path,
        }
        _ENGINE_STORE.record_repo_event(
            _ACTIVE_RUN_ID,
            candidate.full_name,
            "patch_plan_selected",
            f"Patch plan selected for {plan.target_file}",
            {
                "target_file": plan.target_file,
                "expected_files": list(plan.expected_files),
                "test_target": plan.test_target,
            },
        )
        return plan
    raise PRGeneratorError(last_error)


def _structural_review(
    candidate: "RepoCandidate",
    opportunity: Opportunity,
    changed_files: dict[str, str],
    plan: PatchPlan,
) -> str | None:
    if len(changed_files) > _TARGETED_MAX_CHANGED_FILES:
        return f"Patch changed {len(changed_files)} files; targeted runs allow at most {_TARGETED_MAX_CHANGED_FILES}."

    # Normalize paths before comparing — AI sometimes adds leading "./" or uses different slashes.
    def _norm(p: str) -> str:
        return p.replace("\\", "/").lstrip("./").strip()

    target_norm = _norm(opportunity.target_file)
    changed_norms = {_norm(p): p for p in changed_files}
    if target_norm not in changed_norms:
        # Try basename match as last resort (e.g., "autopep8.py" matches "src/autopep8.py")
        target_base = target_norm.split("/")[-1]
        base_match = next((orig for n, orig in changed_norms.items() if n.split("/")[-1] == target_base), None)
        if base_match is None:
            return "Patch did not modify the chosen target file."

    expected_files = {_norm(path) for path in plan.expected_files}
    changed_paths = {_norm(path) for path in changed_files}
    if expected_files and not changed_paths.issubset(expected_files):
        unexpected = sorted(changed_paths - expected_files)
        return f"Patch escaped the approved plan and touched unexpected files: {unexpected[:3]}"
    layout_rejection = _self_review_test_layout(candidate, opportunity, changed_files)
    if layout_rejection:
        return layout_rejection
    bad_syntax = [
        path for path, content in changed_files.items()
        if path.endswith(".py") and not _syntax_ok(content)
        or path.endswith((".ts", ".tsx")) and not _syntax_ok_ts(content)
    ]
    if bad_syntax:
        return f"Generated syntax errors in files: {bad_syntax}"
    if _estimate_diff_lines(candidate, changed_files) > _TARGETED_MAX_DIFF_LINES:
        return "Patch diff is too large for a narrow targeted PR."
    no_diff = [
        path for path, content in changed_files.items()
        if candidate.files.get(path, "").strip() == content.strip()
    ]
    if len(no_diff) == len(changed_files):
        return "Generated files were identical to the originals."
    return None


def _is_terminal_structural_rejection(reason: str) -> bool:
    normalized = (reason or "").strip().lower()
    # "identical to originals" is a true dead end — AI has no new signal to work with.
    # "did not modify target file" is retryable — the retry prompt will explicitly re-anchor the AI.
    return "generated files were identical to the originals" in normalized


# ── AI improvement generator ──────────────────────────────────
def generate_pr_improvement(
    candidate: RepoCandidate,
    log: logging.Logger,
    char_budget: int = 40_000,
    goal: str = "bugfix",
    targeted_mode: bool = False,
) -> PRImprovement:
    """Ask AI to produce one concrete, well-scoped improvement for the repo.

    First tries a no-AI dep update (checks PyPI/npm for outdated pinned packages).
    Falls through to AI-based improvement when no dep updates are available.

    Raises PRGeneratorError if:
    - AI call fails after retries
    - Response is missing required fields
    - Generated Python files have syntax errors
    - Changed files produce no actual diff vs originals
    """
    _recent_pr_recon(candidate, log)

    # ── Pre-check: no-AI dep version bump (fallback only) ────
    from src.core.config import ENABLE_DEP_UPDATE
    dep_improvement = generate_dep_update(candidate, log) if ENABLE_DEP_UPDATE and goal == "bugfix" else None
    if goal == "dep_update":
        dep_only = generate_dep_update(candidate, log)
        if dep_only is None:
            raise PRGeneratorError(f"No safe dependency update found for {candidate.full_name}")
        return dep_only

    # Detect primary language — prefer Python if both present
    py_files = {k: v for k, v in candidate.files.items() if k.endswith(".py")}
    ts_files = {k: v for k, v in candidate.files.items() if k.endswith((".ts", ".tsx"))}
    is_typescript = len(py_files) == 0 and len(ts_files) > 0
    lang_label = "TypeScript" if is_typescript else "Python"
    source_files = ts_files if is_typescript else py_files

    # Build source dump capped at char_budget
    source_dump = ""
    for path, content in sorted(source_files.items()):
        chunk = f"\n// ── {path} ──\n{content}\n" if is_typescript else f"\n# ── {path} ──\n{content}\n"
        if len(source_dump) + len(chunk) > char_budget:
            break
        source_dump += chunk

    # Include dependency manifest if present
    dep_file = "package.json" if is_typescript else "requirements.txt"
    dep_content = candidate.files.get(dep_file, "")
    dep_section = f"\n// ── {dep_file} ──\n{dep_content}\n" if dep_content else ""

    file_ext = ".ts" if is_typescript else ".py"

    # Build historical performance hint from pr_log
    stats = get_improvement_stats()
    if stats:
        resolved = {k: v for k, v in stats.items() if v["rate"] is not None}
        if resolved:
            sorted_types = sorted(resolved.items(), key=lambda x: x[1]["rate"], reverse=True)
            stats_lines = "\n".join(
                f"  - {k}: {int(v['rate']*100)}% merge rate ({v['merged']} merged / {v['merged']+v['closed']} resolved)"
                for k, v in sorted_types
            )
            stats_hint = f"\nHistorical merge rates for your past PRs:\n{stats_lines}\nPrefer improvement types with higher merge rates.\n"
        else:
            stats_hint = ""
    else:
        stats_hint = ""

    opportunity, opportunity_id = _discover_opportunities(candidate, log, goal=goal, targeted_mode=targeted_mode)
    target_file = str(opportunity.target_file).strip()

    # Fetch git history for target file — non-blocking, fails silently
    _git_history_section, _has_revert_history = _build_git_history_section(candidate, target_file, log)

    focused_dump = source_dump
    if target_file and target_file in source_files:
        target_content = source_files[target_file]
        # Cap large single files to char_budget — without this cap, a file larger than
        # char_budget bypasses the source_dump budget and inflates the prompt 2x+.
        if len(target_content) > char_budget:
            lines = target_content.splitlines()
            cap_lines: list[str] = []
            cap_len = 0
            for ln in lines:
                cap_len += len(ln) + 1
                if cap_len > char_budget:
                    break
                cap_lines.append(ln)
            trunc_marker = "# ── [file truncated to fit context budget] ──" if not is_typescript else "// ── [file truncated to fit context budget] ──"
            target_content = "\n".join(cap_lines) + f"\n{trunc_marker}\n"
        focused_dump = (
            f"\n# ── {target_file} ──\n{target_content}\n"
            if not is_typescript
            else f"\n// ── {target_file} ──\n{target_content}\n"
        )
        if len(focused_dump) < char_budget // 2:
            sibling_chunks: list[str] = []
            sibling_total = len(focused_dump)
            for path, content in sorted(source_files.items()):
                if path == target_file:
                    continue
                chunk = (
                    f"\n# ── {path} ──\n{content}\n"
                    if not is_typescript
                    else f"\n// ── {path} ──\n{content}\n"
                )
                if sibling_total + len(chunk) > char_budget:
                    break
                sibling_chunks.append(chunk)
                sibling_total += len(chunk)
            focused_dump = focused_dump + "".join(sibling_chunks)

    # Append git history context if available (keeps AI informed about fragile areas)
    if _git_history_section:
        focused_dump = focused_dump + _git_history_section

    # Plan phase gets half the context budget — it only needs to confirm scope, not generate code.
    _plan_char_cap = char_budget // 2
    plan_focused_dump = focused_dump
    if len(plan_focused_dump) > _plan_char_cap:
        cap = plan_focused_dump[:_plan_char_cap]
        last_nl = cap.rfind("\n")
        if last_nl > _plan_char_cap // 2:
            cap = cap[:last_nl + 1]
        plan_marker = "# ── [truncated for planning phase] ──" if not is_typescript else "// ── [truncated for planning phase] ──"
        plan_focused_dump = cap + plan_marker + "\n"

    patch_plan = _plan_pr_patch(candidate, opportunity, plan_focused_dump, dep_section, is_typescript, log) if targeted_mode else None

    if opportunity.opportunity_kind == "bugfix":
        plan_section = ""
        if patch_plan is not None:
            plan_section = f"""
Approved patch plan:
- target_file: {patch_plan.target_file}
- failure_mode: {patch_plan.failure_mode}
- expected_files: {patch_plan.expected_files}
- test_target: {patch_plan.test_target or "none"}
- why_narrow: {patch_plan.why_narrow}
- proof_path: {patch_plan.proof_path}
"""
        prompt = f"""You are a senior {lang_label} engineer making a targeted open-source contribution to a production repository.
{stats_hint}

Repo: {candidate.full_name}
Description: {candidate.description}
Stars: {candidate.stars} | License: {candidate.license} | Open issues: present

Chosen bug target:
- improvement_type: {opportunity.pattern_type}
- target_file: {opportunity.target_file}
- bug_hypothesis: {opportunity.failure_mode}
- failure_mode: {opportunity.failure_mode}
- evidence: {opportunity.evidence}
- test_plan: Add or update {opportunity.test_target or "a focused regression test"} when feasible.
- why_now: This is a narrow, evidence-backed opportunity surfaced by the local pattern scanner.

Relevant source files:
{focused_dump}{dep_section}
{plan_section}

## Task
Produce the minimal patch for the chosen bug target above.
Stay tightly scoped to that bug. If you cannot fix that exact bug safely, fail rather than pivoting to a different idea.

## What to look for (in priority order)
1. **Runtime bug** — code that will raise an exception, return wrong data, or silently corrupt state for some input
2. **Security issue** — hardcoded credential, unvalidated external input, path traversal, insecure default
3. **Resource leak** — file/socket/DB connection opened without guaranteed close, missing `finally`/context manager
4. **Missing error handling** — network call with no timeout, API response not validated before use, exception swallowed silently
5. **Race condition or data loss** — shared state mutated without lock, write with no fsync where data integrity matters

## HARD RULES — violating any of these is an automatic reject

**Rule 1 — Never collapse exhaustive conditionals.**
If two branches have the same body but different conditions, they are NOT duplicates — they handle distinct cases.
Example of what NOT to do:
```
# WRONG: removing the else-if changes behavior for input where silent=True
if (!silent && data.length > 50) {{ setSkills(data); }}
else if (silent) {{ setSkills(data); }}
→ if (!silent && data.length > 50) {{ setSkills(data); }}  # ← silent=True case now broken
```
Before removing any branch: verify the remaining condition is a strict superset of the union of all original conditions.

**Rule 2 — Never remove a code path you cannot prove is unreachable.**
"The body looks the same" is not proof. Prove it by showing the removed condition is logically implied by the remaining one.

**Rule 3 — Never change behavior for any valid input.**
Your change must be a strict improvement: identical outputs for all inputs that worked before, plus fixes for inputs that were broken.

**Rule 4 — No style-only changes.**
No variable renaming, comment rewriting, import reordering, or whitespace-only edits.

**Rule 5 — No new features.**
Only fix what is already broken or missing from existing logic.

**Rule 6 — Stay inside the approved patch plan.**
Do not touch files outside `expected_files`. Do not widen scope beyond the approved failure mode.

## safety_proof requirement
You MUST provide a `safety_proof` field. Write one paragraph explaining why your change cannot alter correct behavior for any valid input. If you cannot write a convincing proof, choose a different improvement.

## Output format
Respond with JSON only — no prose before or after:
{{
  "improvement_type": "bug_fix|security|resource_leak|error_handling|race_condition",
  "pr_title": "conventional-commit title, max 72 chars (fix:/security:)",
  "pr_body": "markdown PR body with exactly these sections:\\n## Summary\\n(1-2 sentences: what the bug was and what the fix does)\\n## Why it matters\\n(1-2 sentences: concrete failure scenario without the fix)\\n## Testing\\n(1-2 sentences: how correctness was verified)\\nNo fluff, no extra sections.",
  "rationale": "one sentence: why this will be accepted — name the specific failure the fix prevents",
  "safety_proof": "one paragraph proving the change cannot break any currently-working input path",
  "changed_files": {{
    "relative/path/to/file{file_ext}": "complete new file content"
  }}
}}"""
    else:
        prompt = f"""You are a senior {lang_label} engineer making a narrow maintainer-signaled enhancement to an open-source repository.
{stats_hint}

Repo: {candidate.full_name}
Description: {candidate.description}
Stars: {candidate.stars} | License: {candidate.license}

Chosen enhancement target:
- opportunity_kind: {opportunity.opportunity_kind}
- pattern_type: {opportunity.pattern_type}
- target_file: {opportunity.target_file}
- intent_source: {opportunity.source_ref}
- maintainer_intent: {opportunity.maintainer_intent}
- enhancement_summary: {opportunity.failure_mode}
- evidence: {opportunity.evidence}
- test_plan: Add or update {opportunity.test_target or "a focused regression or behavior test"} when feasible.

Relevant source files:
{focused_dump}{dep_section}

## Task
Implement the smallest safe enhancement that satisfies the explicit maintainer signal above.
Do not invent adjacent capabilities, and do not refactor beyond what is required for this one enhancement.

## Hard rules
- Preserve existing behavior for existing valid inputs unless the enhancement explicitly extends that path
- No broad refactors
- No style-only changes
- No speculative feature ideas not grounded in the provided issue/comment evidence
- Keep the patch to one or two files

## safety_proof requirement
You MUST provide a `safety_proof` field. Explain why the patch stays narrow and why existing working paths are preserved while adding only the requested capability.

## Output format
Respond with JSON only:
{{
  "improvement_type": "{opportunity.opportunity_kind}",
  "pr_title": "conventional-commit title, max 72 chars (feat:/enhancement:)",
  "pr_body": "markdown PR body with exactly these sections:\\n## Summary\\n(1-2 sentences: what enhancement was added)\\n## Why it matters\\n(1-2 sentences: what maintainer-signaled gap this addresses)\\n## Testing\\n(1-2 sentences: how correctness was verified)\\nNo fluff, no extra sections.",
  "rationale": "one sentence naming the explicit maintainer signal this patch satisfies",
  "safety_proof": "one paragraph explaining why the change stays narrow and preserves existing behavior",
  "changed_files": {{
    "relative/path/to/file{file_ext}": "complete new file content"
  }}
}}"""

    _UNCERTAINTY_MARKERS = (
        "cannot prove", "not sure", "might change", "could affect",
        "may break", "uncertain", "possibly", "could change behavior",
        "not entirely", "hard to say",
    )
    review_rejection_count = 0
    evidence_rejection_count = 0
    diff_rejection_count = 0
    max_category_rejections = 2
    _structural_retry_hint = ""  # injected into prompt when AI missed the target file
    _ENGINE_STORE.transition_opportunity(
        opportunity_id,
        "EXECUTE",
        why_advanced=f"Executing AI patch for {opportunity.pattern_type} in {opportunity.target_file}",
    )

    if targeted_mode:
        max_generate_attempts = min(
            _TARGETED_GENERATE_ATTEMPTS,
            _TARGETED_PATTERN_GENERATE_BUDGETS.get(opportunity.pattern_type, _TARGETED_GENERATE_ATTEMPTS),
        )
        max_review_rejections = _TARGETED_PATTERN_REVIEW_BUDGETS.get(
            opportunity.pattern_type,
            _TARGETED_SELF_REVIEW_RETRIES,
        )
    else:
        max_generate_attempts = 3
        max_review_rejections = max_category_rejections
    for attempt in range(1, max_generate_attempts + 1):
        _set_run_stage(
            "generate",
            candidate.full_name,
            {
                "target_file": opportunity.target_file,
                "pattern_type": opportunity.pattern_type,
                "pattern_policy": _targeted_pattern_policy(opportunity).mode,
                "attempt": attempt,
            },
        )
        timeout = get_scaled_timeout(200, attempt)
        log.info(
            "AI generating PR improvement for %s (attempt %d, timeout=%ds)",
            candidate.full_name, attempt, timeout,
        )
        _active_prompt = prompt
        if _structural_retry_hint:
            _active_prompt = prompt + f"\n\n[RETRY CONSTRAINT — previous attempt violated this]\n{_structural_retry_hint}"
        try:
            before_tokens = _usage_tokens()
            raw = call_ai(_active_prompt, timeout=timeout)
            _record_stage_token_spend("generate", before_tokens)
            result = _parse_json(raw)
            _ENGINE_STORE.record_attempt(opportunity_id, "EXECUTE", attempt, "parsed", "AI produced a candidate patch.")
            _bump_run_metric("generated")
        except Exception as exc:
            _ENGINE_STORE.record_attempt(opportunity_id, "EXECUTE", attempt, "failed", str(exc)[:300])
            if attempt == max_generate_attempts:
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "ai_could_not_stay_on_scope",
                    f"AI call failed after repeated attempts: {exc}",
                    "EXECUTE",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError(f"AI call failed after {attempt} attempts: {exc}") from exc
            log.warning("AI attempt %d failed: %s — retrying", attempt, exc)
            time.sleep(4)
            continue

        # ── Field validation ──────────────────────────────────
        required = {"pr_title", "pr_body", "changed_files", "improvement_type", "rationale", "safety_proof"}
        missing = required - set(result.keys())
        if missing:
            _ENGINE_STORE.record_attempt(opportunity_id, "EXECUTE", attempt, "missing_fields", ",".join(sorted(missing)))
            if attempt == max_generate_attempts:
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "ai_could_not_stay_on_scope",
                    f"AI response missing required fields: {sorted(missing)}",
                    "EXECUTE",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError(f"AI response missing fields: {missing}")
            log.warning("Missing fields in attempt %d: %s — retrying", attempt, missing)
            time.sleep(4)
            continue

        if targeted_mode:
            duplicate_rejection = _duplicate_patch_family_rejection(result)
            if duplicate_rejection:
                _ENGINE_STORE.record_attempt(
                    opportunity_id,
                    "EXECUTE",
                    attempt,
                    "duplicate_patch_family",
                    duplicate_rejection,
                )
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "duplicate_patch_family",
                    duplicate_rejection,
                    "EXECUTE",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError(duplicate_rejection)

        changed_files: dict[str, str] = result.get("changed_files") or {}
        if not changed_files:
            _ENGINE_STORE.record_attempt(opportunity_id, "EXECUTE", attempt, "empty_diff", "AI returned no changed files.")
            if attempt == max_generate_attempts:
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "patch_not_minimal",
                    "AI returned no changed files for the chosen opportunity.",
                    "EXECUTE",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError("AI returned no changed_files")
            log.warning("Empty changed_files in attempt %d — retrying", attempt)
            time.sleep(4)
            continue

        structural_rejection = _structural_review(candidate, opportunity, changed_files, patch_plan or PatchPlan(opportunity.target_file, opportunity.failure_mode, list(changed_files.keys()), opportunity.test_target, "", ""))
        if structural_rejection:
            review_rejection_count += 1
            _bump_run_metric("self_review_rejected")
            _ENGINE_STORE.record_attempt(opportunity_id, "VERIFY", attempt, "structural_review_rejected", structural_rejection)
            if "did not modify the chosen target file" in structural_rejection:
                _structural_retry_hint = (
                    f"CRITICAL: Your changed_files MUST include the key '{opportunity.target_file}' "
                    f"(exactly as written, no path prefix changes). "
                    f"Do not modify any other file unless it is the test file. "
                    f"Return the complete new content of '{opportunity.target_file}' with the fix applied."
                )
            if _is_terminal_structural_rejection(structural_rejection):
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "self_review_rejected",
                    f"{structural_rejection} (structural retry kill switch engaged)",
                    "VERIFY",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError(
                    "Structural review rejected this target. "
                    f"Structural retry kill switch engaged: {structural_rejection}"
                )
            if review_rejection_count >= max_review_rejections:
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "self_review_rejected",
                    structural_rejection,
                    "VERIFY",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError(
                    "Structural review rejected this target repeatedly. "
                    f"Last reason: {structural_rejection}"
                )
            log.warning("Structural review rejected attempt %d: %s", attempt, structural_rejection)
            time.sleep(4)
            continue

        # ── Safety proof uncertainty check ────────────────────
        safety_proof = (result.get("safety_proof") or "").lower()
        if any(marker in safety_proof for marker in _UNCERTAINTY_MARKERS):
            _ENGINE_STORE.record_attempt(opportunity_id, "VERIFY", attempt, "uncertain_safety_proof", safety_proof[:300])
            if attempt == max_generate_attempts:
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "behavior_change_unjustified",
                    f"Safety proof remained uncertain: {safety_proof[:200]}",
                    "VERIFY",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError(
                    f"AI safety proof expresses uncertainty — change is not safe: {safety_proof[:200]}"
                )
            log.warning("Safety proof uncertain in attempt %d — retrying with stricter prompt", attempt)
            time.sleep(4)
            continue

        evidence_rejection = _check_pr_evidence_quality(result, changed_files)
        if evidence_rejection:
            evidence_rejection_count += 1
            _ENGINE_STORE.record_attempt(opportunity_id, "VERIFY", attempt, "evidence_rejected", evidence_rejection)
            if evidence_rejection_count >= max_review_rejections:
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "evidence_too_weak",
                    evidence_rejection,
                    "QUALIFY",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError(
                    "Evidence quality rejected this target repeatedly. "
                    f"Last reason: {evidence_rejection}"
                )
            if attempt == max_generate_attempts:
                raise PRGeneratorError(f"Evidence quality check failed: {evidence_rejection}")
            log.warning("Evidence quality rejection in attempt %d: %s — retrying", attempt, evidence_rejection)
            time.sleep(4)
            continue

        policy = _targeted_pattern_policy(opportunity)
        if targeted_mode:
            changed_file_count = len(changed_files)
            changed_line_count = _changed_diff_line_count(candidate.files, changed_files)
            if changed_file_count > policy.max_changed_files or changed_line_count > policy.max_diff_lines:
                rejection = (
                    f"{opportunity.pattern_type} patch exceeds policy shape "
                    f"(files={changed_file_count}/{policy.max_changed_files}, "
                    f"diff_lines={changed_line_count}/{policy.max_diff_lines})"
                )
                _bump_run_metric("shape_rejected_early")
                _ENGINE_STORE.record_attempt(opportunity_id, "VERIFY", attempt, "shape_rejected", rejection)
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "patch_shape_too_broad",
                    rejection,
                    "VERIFY",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError(rejection)
            patch_shape = _classify_patch_shape(candidate.files, changed_files, opportunity)
            execution_mode = _targeted_execution_mode(policy, patch_shape)
            if execution_mode == "live-safe" and _has_revert_history:
                execution_mode = "live-review"
                log.info("Downgraded to live-review: revert history on %s.", target_file)
            if execution_mode == "stop":
                _bump_run_metric("shape_rejected_early")
                _ENGINE_STORE.record_attempt(opportunity_id, "VERIFY", attempt, "shape_rejected", patch_shape.reason)
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "patch_shape_high_risk",
                    patch_shape.reason,
                    "VERIFY",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError(patch_shape.reason)
            if execution_mode == "live-review":
                _bump_run_metric("manual_review_queued")
        else:
            execution_mode = "live-review" if _has_revert_history else "live-safe"

        # ── Semantic diff: detect exhaustive conditional collapse ─
        rejection = _check_diff_safety(candidate.files, changed_files, log)
        if rejection:
            diff_rejection_count += 1
            _ENGINE_STORE.record_attempt(opportunity_id, "VERIFY", attempt, "diff_rejected", rejection)
            if diff_rejection_count >= max_review_rejections:
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "patch_not_minimal",
                    rejection,
                    "VERIFY",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError(
                    "Diff safety rejected this target repeatedly. "
                    f"Last reason: {rejection}"
                )
            if attempt == max_generate_attempts:
                raise PRGeneratorError(f"Diff safety check failed: {rejection}")
            log.warning("Diff safety rejection in attempt %d: %s — retrying", attempt, rejection)
            time.sleep(4)
            continue
        # ── Semantic review gate ──────────────────────────────
        _set_run_stage(
            "review",
            candidate.full_name,
            {
                "target_file": opportunity.target_file,
                "pattern_type": opportunity.pattern_type,
                "pattern_policy": _targeted_pattern_policy(opportunity).mode,
                "attempt": attempt,
            },
        )
        before_tokens = _usage_tokens()
        review_rejection = _self_review_diff(candidate, changed_files, result, log, plan=patch_plan)
        _record_stage_token_spend("review", before_tokens)
        if review_rejection:
            review_rejection_count += 1
            _bump_run_metric("self_review_rejected")
            _ENGINE_STORE.record_attempt(opportunity_id, "VERIFY", attempt, "self_review_rejected", review_rejection)
            if targeted_mode:
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "self_review_rejected",
                    f"{review_rejection} (same-pattern retry kill switch engaged)",
                    "VERIFY",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError(
                    "Self-review rejected this target. "
                    f"Same-pattern retry kill switch engaged: {review_rejection}"
                )
            if review_rejection_count >= max_review_rejections:
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "self_review_rejected",
                    review_rejection,
                    "VERIFY",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError(
                    "Self-review rejected this target repeatedly. "
                    f"Last reason: {review_rejection}"
                )
            if attempt == max_generate_attempts:
                raise PRGeneratorError(f"Self-review rejected change: {review_rejection}")
            log.warning("Self-review rejection in attempt %d: %s — retrying", attempt, review_rejection)
            time.sleep(4)
            continue

        pr_title = result["pr_title"].strip()
        if len(pr_title) > 72:
            pr_title = pr_title[:69] + "..."

        log.info("PR improvement accepted: %s (%s)", pr_title, result["improvement_type"])
        _ENGINE_STORE.record_attempt(opportunity_id, "VERIFY", attempt, "passed", "Patch passed engine verification.")
        _ENGINE_STORE.transition_opportunity(
            opportunity_id,
            "READY",
            why_advanced="Patch verified and ready for submission.",
        )
        return PRImprovement(
            title=pr_title,
            body=result["pr_body"].strip(),
            improvement_type=result["improvement_type"],
            changed_files=changed_files,
            rationale=result["rationale"],
            opportunity_id=opportunity_id,
            target_file=opportunity.target_file,
            pattern_type=opportunity.pattern_type,
            patch_plan=_ACTIVE_RUN_METRICS.get("last_patch_plan"),
            execution_mode=execution_mode,
        )

    if dep_improvement is not None:
        log.info("  AI found no valid improvement — falling back to dep update")
        return dep_improvement
    raise PRGeneratorError("All AI attempts failed to produce a valid improvement")
