from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from src.ai import call_ai, _parse_json, _syntax_ok, _syntax_ok_ts, get_scaled_timeout
from src.config import DATA_DIR
from src.contribution_engine import ContributionEngine
from src.contribution_store import PREngineStore
from src.opportunity_engine import (
    Opportunity,
    PatternScanner,
    expected_test_root,
    guess_test_target,
    qualify_opportunity,
    test_root_for_path,
)
from src.repo_intelligence import RepoShortlister
from src.scraper import (
    RepoCandidate,
    ScraperError,
    _ALLOWED_LICENSES,
    _GITHUB_API,
    _MAX_REPO_FILES,
    _gh_get,
    _get_license,
    _metadata_security_ok,
    download_repo_files,
)

_GITHUB_URL_RE = re.compile(
    r"(?:https?://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────
PR_LOG_FILE    = DATA_DIR / "pr_log.json"
_ENGINE_STORE = PREngineStore()
_CONTRIBUTION_ENGINE = ContributionEngine(_ENGINE_STORE)
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


def start_pr_engine_run(mode: str, target_count: int) -> int:
    global _ACTIVE_RUN_ID
    _ACTIVE_RUN_ID = _CONTRIBUTION_ENGINE.start_run(mode=mode, target_count=target_count)
    return _ACTIVE_RUN_ID


def finish_pr_engine_run(
    submitted: int,
    target: int,
    attempts: int,
    usage: dict[str, int],
    log: logging.Logger,
) -> dict:
    return _CONTRIBUTION_ENGINE.finish_run(submitted, target, attempts, usage, log)


def can_submit_contribution_to_repo(full_name: str) -> bool:
    return _CONTRIBUTION_ENGINE.can_submit_to_repo(full_name)


def build_contribution_report(limit: int = 5) -> str:
    return _CONTRIBUTION_ENGINE.build_operator_report(limit=limit)


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
) -> tuple[int, int, int]:
    py_count, ts_count, test_count, _has_tests = _count_repo_files(candidate.files)
    total_files = len(candidate.files)
    max_total = _PR_TARGETED_MAX_TOTAL_FILES if targeted else _PR_MAX_TOTAL_FILES
    max_py = _PR_TARGETED_MAX_PY_FILES if targeted else _PR_MAX_PY_FILES
    allow_broad = targeted and _PR_TARGETED_ALLOW_BROAD

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


def build_repo_inspect_report(candidate: RepoCandidate) -> str:
    py_count, ts_count, test_count, _has_tests = _count_repo_files(candidate.files)
    lane_match = _matches_contribution_lane(candidate)
    first_pr_friendly, first_pr_reason = _first_pr_repo_fit(candidate, candidate.files)
    lines = [
        "Repo Inspect",
        "============",
        "",
        f"Repo: {candidate.full_name}",
        f"URL: {candidate.url}",
        f"Stats: stars={_format_compact_count(candidate.stars)} forks={_format_compact_count(candidate.forks)} license={candidate.license} pushed={candidate.pushed_days_ago}d ago",
        f"Surface: files={len(candidate.files)} py={py_count} ts={ts_count} tests={test_count}",
        f"Lane fit: configured lane `{_LANE_NAME}` is {'matched' if lane_match else 'not matched'}",
        f"First-PR fit: {'friendly' if first_pr_friendly else 'not ideal'} ({first_pr_reason})",
    ]

    search_scope = "search-ready"
    targeted_scope = "targeted-ready"
    scope_notes: list[str] = []

    try:
        _validate_candidate_scope(candidate, targeted=False)
    except ScraperError as exc:
        search_scope = "too broad for search mode"
        scope_notes.append(f"search mode: {exc}")

    try:
        _validate_candidate_scope(candidate, targeted=True)
    except ScraperError as exc:
        targeted_scope = "inspect-only unless targeted override is enabled"
        scope_notes.append(f"targeted mode: {exc}")

    lines.append(f"Scope fit: search={search_scope} | targeted={targeted_scope}")

    if candidate.description:
        lines.extend(["", "Description:", candidate.description.strip()])

    if candidate.topics:
        lines.extend(["", "Topics:", ", ".join(candidate.topics)])

    if scope_notes:
        lines.extend(["", "Scope notes:"])
        lines.extend(f"- {note}" for note in scope_notes)

    lines.extend(["", "Suggested next step:"])
    if targeted_scope == "targeted-ready":
        lines.append(f"- Run `python -m app.builder --contrib {candidate.full_name} --1` for a pinned contribution attempt.")
    elif _PR_TARGETED_ALLOW_BROAD:
        lines.append(
            f"- Targeted broad-repo override is enabled, so `python -m app.builder --contrib {candidate.full_name} --1` can still proceed with extra caution."
        )
    else:
        lines.append(
            "- Use inspect mode to study this repo first, or raise targeted breadth limits deliberately in `.env` before attempting a pinned PR."
        )

    if not lane_match:
        lines.append(f"- Consider switching `CONTRIB_LANE` if this repo is intentionally outside the current `{_LANE_NAME}` niche.")

    return "\n".join(lines)


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


def _discover_opportunities(
    candidate: RepoCandidate,
    log: logging.Logger,
    goal: str,
) -> tuple[Opportunity, int]:
    base_score = _acceptance_score(candidate, candidate.files)
    _ENGINE_STORE.upsert_repo_profile(candidate, base_score)

    opportunities = _PATTERN_SCANNER.scan(candidate)
    if goal == "bugfix":
        opportunities = [opportunity for opportunity in opportunities if opportunity.opportunity_kind == "bugfix"]
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
        qualified.append((qualification.score, opportunity, opportunity_id))

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
        return json.loads(PR_LOG_FILE.read_text(encoding="utf-8"))
    return {"submitted": []}


def save_pr_log(pr_result, improvement_type: str = "", opportunity_id: int | None = None) -> None:
    """Append a submitted PR to the log."""
    data = load_pr_log()
    data.setdefault("submitted", []).append({
        "full_name":        pr_result.full_name,
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
    )


def get_pr_submitted_repos() -> set[str]:
    """Return lowercased full_names of repos we already submitted a PR to."""
    return {e["full_name"].lower() for e in load_pr_log().get("submitted", [])}


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
    from src.fork import ForkError, get_current_github_login, gh_safe_env

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
            # 404 = already gone — treat as success
            if "404" in stderr or "Could not resolve" in stderr or "not found" in stderr.lower():
                log.info("Fork already gone: %s", fork_name)
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
    from src.fork import ForkError, get_current_github_login, gh_safe_env
    from src.notify import notify

    data = load_pr_log()
    entries = data.get("submitted", [])
    if not entries:
        log.info("PR log is empty — nothing to check")
        return

    changed = False
    open_count = sum(1 for e in entries if e.get("status", "open") == "open")
    try:
        current_login = get_current_github_login().lower()
    except ForkError:
        current_login = ""
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
                log.warning("Could not fetch PR %s: %s", pr_url, r.stderr.strip()[:200])
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

        if state == "MERGED" and not entry.get("notified_merge"):
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
            log.info("  still open: %s", pr_url)

        time.sleep(0.3)

    if changed:
        PR_LOG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("PR log updated")
    else:
        log.info("No status changes")


# ── Fetch specific repo ───────────────────────────────────
def fetch_repo_candidate(repo_url: str, log: logging.Logger) -> RepoCandidate:
    """Fetch a specific repo by URL or owner/repo and return a RepoCandidate.

    Works for both external repos and operator-owned repos.
    Raises ScraperError if the repo can't be fetched or has no Python/TypeScript files.
    """
    return fetch_repo_candidate_with_scope(repo_url, log, enforce_scope=True)


def fetch_repo_candidate_with_scope(
    repo_url: str,
    log: logging.Logger,
    *,
    enforce_scope: bool,
) -> RepoCandidate:
    """Fetch a specific repo and optionally enforce narrow contribution scope gates."""

    m = _GITHUB_URL_RE.match(repo_url.strip())
    if not m:
        raise ScraperError(f"Cannot parse repo URL/name: {repo_url!r}")
    full_name = m.group(1)

    log.info("Fetching repo metadata: %s", full_name)
    try:
        data = _gh_get(f"{_GITHUB_API}/repos/{full_name}")
    except Exception as exc:
        raise ScraperError(f"GitHub API error for {full_name}: {exc}") from exc

    pushed_at = data.get("pushed_at", "")
    try:
        from datetime import datetime, timezone
        pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        pushed_days_ago = (datetime.now(timezone.utc) - pushed).days
    except Exception:
        pushed_days_ago = 0

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
    )

    log.info(
        "Repo: %s (%d★, %s license, pushed %dd ago)",
        full_name, candidate.stars, lic, pushed_days_ago,
    )

    candidate.files = download_repo_files(candidate)
    if not candidate.files:
        raise ScraperError(f"No files downloaded from {full_name}")

    py_count, ts_count, test_count, _has_tests = _count_repo_files(candidate.files)
    if py_count == 0 and ts_count == 0:
        raise ScraperError(f"No Python or TypeScript files found in {full_name}")

    if enforce_scope:
        py_count, ts_count, test_count = _validate_candidate_scope(candidate, targeted=True)
    else:
        try:
            _validate_candidate_scope(candidate, targeted=True)
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
    from src.fork import gh_safe_env
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
    from src.fork import gh_safe_env
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
    from src.fork import gh_safe_env
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
    from src.fork import gh_safe_env
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


def generate_pr_response(
    entry: dict,
    comment_body: str,
    comment_author: str,
    log: logging.Logger,
    inline_comments: list[dict] | None = None,
    review_state: str = "",
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

    prompt = f"""You are a senior engineer who submitted a PR to an open-source repo and received feedback.

PR: {pr_url}
PR title: {pr_title}
Repo: {full_name}
Our branch: {branch_name}

Current state of changed files:
{files_dump if files_dump else "(files not available)"}
{review_section}{inline_section}
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
    from src.fork import push_to_branch, ForkError
    from src.notify import notify

    data = load_pr_log()
    entries = data.get("submitted", [])
    open_entries = [
        e for e in entries
        if e.get("status", "open") == "open" and "/pull/" in e.get("pr_url", "")
    ]

    if not open_entries:
        log.info("No open PRs to check for feedback")
        return

    log.info("Checking %d open PR(s) for maintainer feedback...", len(open_entries))
    changed = False

    try:
        from src.fork import get_current_github_login
        current_login = get_current_github_login().lower()
    except Exception:
        current_login = ""

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
            log.info("  %s — no new maintainer feedback", pr_url)
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

        try:
            action = generate_pr_response(
                entry, comment_body, comment_author, log,
                inline_comments=new_inline or None,
                review_state=review_state,
            )
        except PRGeneratorError as e:
            log.warning("  AI failed for %s: %s — skipping", pr_url, e)
            continue

        # Push code fix if AI produced changed files
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
                log.info("  Fix pushed to branch")
            except ForkError as e:
                log.warning("  Push failed: %s — will still post reply", e)

        # Post reply comment
        reply_text = action.reply
        from src.fork import gh_safe_env
        r = subprocess.run(
            ["gh", "pr", "comment", pr_url, "--body", reply_text],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
            env=gh_safe_env(),
        )
        if r.returncode == 0:
            log.info("  Reply posted: %s", pr_url)
            notify(
                f"PR feedback addressed\n"
                f"Repo: {full_name}\n"
                f"Comment by: @{latest['user']}\n"
                f"URL: {pr_url}"
            )
        else:
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

    if changed:
        PR_LOG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("PR log updated")


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
        from src.fork import get_current_github_login
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
    shortlister = RepoShortlister(_ENGINE_STORE)
    log.info("Contribution lane: %s", _LANE_NAME)
    if first_pr_mode:
        log.info("First-PR mode: enabled")
    try:
        from src.fork import get_current_github_login
        current_login = get_current_github_login().lower()
    except Exception:
        current_login = ""

    # ── Follow-up pass: prioritize repos where we already merged ─
    followup = get_followup_candidates(blacklisted, already_prd)
    if followup:
        log.info("Follow-up candidates available: %s", followup[:3])
        for full_name in followup:
            log.info("  Trying follow-up: %s", full_name)
            try:
                candidate = fetch_repo_candidate(full_name, log)
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
                log.warning("  skip follow-up %s — %s", full_name, exc)
            except Exception as exc:
                log.warning("  skip follow-up %s — unexpected: %s", full_name, exc)
            time.sleep(0.5)
    shortlisted: list[tuple[int, RepoCandidate]] = []

    for lang, query in _PR_SEARCH_QUERIES:
        log.info("PR target search [%s]: %r", lang, query)
        full_query = (
            f"{query} language:{lang} "
            f"stars:{_PR_MIN_STARS}..{_PR_MAX_STARS}"
        )
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
            if not (_PR_MIN_STARS <= stars <= _PR_MAX_STARS):
                continue
            if item.get("forks_count", 0) < _PR_MIN_FORKS:
                continue
            if item.get("open_issues_count", 0) < _PR_MIN_ISSUES:
                continue

            pushed_at = item.get("pushed_at", "")
            try:
                pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
                pushed_days_ago = (datetime.now(timezone.utc) - pushed).days
            except Exception:
                pushed_days_ago = 999
            if pushed_days_ago > _PR_MAX_PUSHED:
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
            )

            if not _metadata_security_ok(candidate):
                log.info("  skip %s — suspicious metadata", full_name)
                continue

            if not _matches_contribution_lane(candidate):
                log.info("  skip %s — does not match current contribution lane", full_name)
                continue

            try:
                candidate.files = download_repo_files(candidate)
                if not candidate.files:
                    continue
                py_count, ts_count, test_count = _validate_candidate_scope(candidate, targeted=False)
                if first_pr_mode:
                    first_pr_friendly, reason = _first_pr_repo_fit(candidate, candidate.files)
                    if not first_pr_friendly:
                        log.info("  skip %s — not first-PR friendly (%s)", full_name, reason)
                        continue
                # No full security scan for PR targets — we read the code, not execute it.
                # Metadata security check above is sufficient.
                acceptance = shortlister.score(candidate, _acceptance_score(candidate, candidate.files))
                log.info(
                    "Candidate PR target: %s (%d★, %d open issues, %s license, py=%d ts=%d tests=%d score=%d)",
                    full_name, stars, item.get("open_issues_count", 0), lic, py_count, ts_count, test_count, acceptance,
                )
                shortlisted.append((acceptance, candidate))
                shortlisted.sort(key=lambda item: item[0], reverse=True)
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
            except ScraperError as exc:
                log.warning("  skip %s — %s", full_name, exc)
            except Exception as exc:
                log.warning("  skip %s — unexpected: %s", full_name, exc)
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
        req = urllib.request.Request(url, headers={"User-Agent": "crypto-builder/1.0"})
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


def _self_review_diff(
    candidate: "RepoCandidate",
    changed_files: dict[str, str],
    result: dict,
    log: logging.Logger,
) -> str | None:
    """Ask a second AI call to play skeptical reviewer.

    Returns a rejection reason if the reviewer finds problems, else None.
    Skips if no original content exists for comparison (new file additions).
    """
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

    review_prompt = f"""You are a skeptical senior code reviewer for an open-source crypto project.
A contributor claims the following change is a safe bug fix or improvement.
Your job: find any case where this change alters behavior for a valid input.

Proposed improvement type: {result.get('improvement_type')}
Proposed title: {result.get('pr_title')}
Contributor's safety proof: {result.get('safety_proof', '')}

{'---'.join(pairs)}

Answer these questions:
1. Is there ANY input combination (parameter values, state, timing) for which the BEFORE code would execute a code path that the AFTER code does not?
2. Does the change remove a branch that handles a distinct case, even if the branch body looks similar to another branch?
3. Is this change purely cosmetic (style, rename, comment rewrite) with no correctness value?

Respond with JSON only:
{{
  "safe": true/false,
  "reason": "one sentence explanation — if safe=true say why; if safe=false name the exact input case that breaks"
}}"""

    try:
        raw = call_ai(review_prompt, timeout=get_scaled_timeout(120, 1))
        review = _parse_json(raw)
    except Exception as exc:
        log.warning("Self-review AI call failed: %s — allowing change through", exc)
        return None  # fail open: don't block on reviewer timeout

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


# ── AI improvement generator ──────────────────────────────────
def generate_pr_improvement(
    candidate: RepoCandidate,
    log: logging.Logger,
    char_budget: int = 40_000,
    goal: str = "bugfix",
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
    # ── Pre-check: no-AI dep version bump ────────────────────
    dep_improvement = generate_dep_update(candidate, log)
    if dep_improvement is not None and goal == "bugfix":
        log.info("  dep update found — skipping AI call")
        return dep_improvement

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

    opportunity, opportunity_id = _discover_opportunities(candidate, log, goal=goal)
    target_file = str(opportunity.target_file).strip()
    focused_dump = source_dump
    if target_file and target_file in source_files:
        target_content = source_files[target_file]
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

    if opportunity.opportunity_kind == "bugfix":
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
    _ENGINE_STORE.transition_opportunity(
        opportunity_id,
        "EXECUTE",
        why_advanced=f"Executing AI patch for {opportunity.pattern_type} in {opportunity.target_file}",
    )

    for attempt in range(1, 4):
        timeout = get_scaled_timeout(200, attempt)
        log.info(
            "AI generating PR improvement for %s (attempt %d, timeout=%ds)",
            candidate.full_name, attempt, timeout,
        )
        try:
            raw = call_ai(prompt, timeout=timeout)
            result = _parse_json(raw)
            _ENGINE_STORE.record_attempt(opportunity_id, "EXECUTE", attempt, "parsed", "AI produced a candidate patch.")
        except Exception as exc:
            _ENGINE_STORE.record_attempt(opportunity_id, "EXECUTE", attempt, "failed", str(exc)[:300])
            if attempt == 3:
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
            if attempt == 3:
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

        changed_files: dict[str, str] = result.get("changed_files") or {}
        if not changed_files:
            _ENGINE_STORE.record_attempt(opportunity_id, "EXECUTE", attempt, "empty_diff", "AI returned no changed files.")
            if attempt == 3:
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

        # ── Safety proof uncertainty check ────────────────────
        safety_proof = (result.get("safety_proof") or "").lower()
        if any(marker in safety_proof for marker in _UNCERTAINTY_MARKERS):
            _ENGINE_STORE.record_attempt(opportunity_id, "VERIFY", attempt, "uncertain_safety_proof", safety_proof[:300])
            if attempt == 3:
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
            if evidence_rejection_count >= max_category_rejections:
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
            if attempt == 3:
                raise PRGeneratorError(f"Evidence quality check failed: {evidence_rejection}")
            log.warning("Evidence quality rejection in attempt %d: %s — retrying", attempt, evidence_rejection)
            time.sleep(4)
            continue

        # ── Semantic diff: detect exhaustive conditional collapse ─
        rejection = _check_diff_safety(candidate.files, changed_files, log)
        if rejection:
            diff_rejection_count += 1
            _ENGINE_STORE.record_attempt(opportunity_id, "VERIFY", attempt, "diff_rejected", rejection)
            if diff_rejection_count >= max_category_rejections:
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
            if attempt == 3:
                raise PRGeneratorError(f"Diff safety check failed: {rejection}")
            log.warning("Diff safety rejection in attempt %d: %s — retrying", attempt, rejection)
            time.sleep(4)
            continue

        # ── Syntax check all generated source files ───────────
        bad_syntax = [
            path for path, content in changed_files.items()
            if path.endswith(".py") and not _syntax_ok(content)
            or path.endswith((".ts", ".tsx")) and not _syntax_ok_ts(content)
        ]
        if bad_syntax:
            _ENGINE_STORE.record_attempt(opportunity_id, "VERIFY", attempt, "syntax_error", ", ".join(bad_syntax))
            if attempt == 3:
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "test_failure",
                    f"Generated syntax errors in files: {bad_syntax}",
                    "VERIFY",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError(f"Syntax errors in generated files: {bad_syntax}")
            log.warning("Syntax errors in attempt %d: %s — retrying", attempt, bad_syntax)
            time.sleep(4)
            continue

        # ── Diff check: at least one file must actually change ─
        no_diff = [
            path for path, content in changed_files.items()
            if candidate.files.get(path, "").strip() == content.strip()
        ]
        if len(no_diff) == len(changed_files):
            _ENGINE_STORE.record_attempt(opportunity_id, "VERIFY", attempt, "no_diff", "Generated files were identical to originals.")
            if attempt == 3:
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "patch_not_minimal",
                    "Generated files were identical to the originals.",
                    "VERIFY",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError(
                    "AI produced no actual diff — all changed files are identical to originals"
                )
            log.warning("No real diff in attempt %d — retrying", attempt)
            time.sleep(4)
            continue

        # ── Self-review gate ──────────────────────────────────
        layout_rejection = _self_review_test_layout(candidate, opportunity, changed_files)
        if layout_rejection:
            review_rejection_count += 1
            _ENGINE_STORE.record_attempt(opportunity_id, "VERIFY", attempt, "self_review_rejected", layout_rejection)
            if review_rejection_count >= max_category_rejections:
                _ENGINE_STORE.reject_opportunity(
                    _ACTIVE_RUN_ID,
                    opportunity,
                    "self_review_rejected",
                    layout_rejection,
                    "VERIFY",
                    opportunity_id=opportunity_id,
                )
                raise PRGeneratorError(
                    "Self-review rejected this target repeatedly. "
                    f"Last reason: {layout_rejection}"
                )
            log.warning("Self-review layout rejected attempt %d: %s", attempt, layout_rejection)
            time.sleep(4)
            continue

        review_rejection = _self_review_diff(candidate, changed_files, result, log)
        if review_rejection:
            review_rejection_count += 1
            _ENGINE_STORE.record_attempt(opportunity_id, "VERIFY", attempt, "self_review_rejected", review_rejection)
            if review_rejection_count >= max_category_rejections:
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
            if attempt == 3:
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
        )

    raise PRGeneratorError("All AI attempts failed to produce a valid improvement")
