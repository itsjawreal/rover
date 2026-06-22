from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from src.github.scraper import RepoCandidate

# ── Constants ────────────────────────────────────────────────
MAX_TARGET_FILE_LINES = int(os.getenv("PR_MAX_TARGET_FILE_LINES", "400"))
# A large file may still host a tightly-scoped fix. When the evidence sits inside
# a small function, the patch is narrow regardless of total file size, so the
# whole-file breadth gate is overridden — but only up to a hard file-size cap so
# pathologically large files still cost no AI. Downstream diff-size/patch-shape
# gates measure the actual patch breadth precisely.
MAX_LOCAL_SCOPE_LINES = int(os.getenv("PR_MAX_LOCAL_SCOPE_LINES", "60"))
LOCALITY_FILE_CAP_FACTOR = int(os.getenv("PR_LOCALITY_FILE_CAP_FACTOR", "3"))
QUALIFY_MIN_SCORE = int(os.getenv("PR_QUALIFY_MIN_SCORE", "70"))
FEATURE_QUALIFY_MIN_SCORE = int(os.getenv("PR_FEATURE_QUALIFY_MIN_SCORE", "82"))
VAGUE_MARKERS = (
    "safer",
    "cleaner",
    "more consistent",
    "more robust",
    "defensive",
    "should be better",
    "probably",
    "might",
    "could",
)
PATTERN_PRIORITY = {
    "missing_timeout": 10,
    "missing_input_validation": 9,
    "unsafe_subprocess": 9,
    "unchecked_response_shape": 8,
    "resource_cleanup_gap": 7,
    "missing_retry_backoff": 7,
    "overbroad_exception_handling": 6,
    "unsafe_file_write_or_path": 6,
    "temp_file_cleanup_gap": 6,
    "missing_regression_test_for_obvious_bugfix": 4,
    "flaky_time_dependent_test": 5,
    "feature_upgrade_todo": 3,
}
TEST_FILE_RE = re.compile(r"(^|/)(test|tests)(/|$)|(_test\.py$)|(\.test\.ts$)|(\.spec\.ts$)")
FEATURE_TODO_RE = re.compile(r"(todo|fixme).*(add|support|allow|expose|option|flag|feature)", re.IGNORECASE)
CORE_ENTRYPOINT_RE = re.compile(r"(^|/)(cli|main|app|core|index|__init__)\.(py|ts|tsx|js)$", re.IGNORECASE)


# ── Data models ──────────────────────────────────────────────
@dataclass
class Opportunity:
    repo_full_name: str
    target_file: str
    pattern_type: str
    failure_mode: str
    evidence: str
    patch_scope: int
    test_target: str
    acceptance_score: int
    opportunity_kind: str = "bugfix"
    source_ref: str = ""
    maintainer_intent: bool = False
    state: str = "SCAN"
    evidence_lines: list[int] = field(default_factory=list)
    why_advanced: str = ""
    why_rejected: str = ""
    opportunity_id: int | None = None
    source_issue_number: int | None = None
    source_issue_url: str = ""
    issue_body_snippet: str = ""


@dataclass
class QualificationResult:
    accepted: bool
    reason_code: str = ""
    summary: str = ""
    score: int = 0


# ── Helpers ──────────────────────────────────────────────────
def count_repo_files(files: dict[str, str]) -> tuple[int, int, int]:
    py_count = sum(1 for path in files if path.endswith(".py"))
    ts_count = sum(1 for path in files if path.endswith((".ts", ".tsx")))
    test_count = sum(1 for path in files if TEST_FILE_RE.search(path.replace("\\", "/").lower()))
    return py_count, ts_count, test_count


def has_tests(files: dict[str, str]) -> bool:
    return any(TEST_FILE_RE.search(path.replace("\\", "/").lower()) for path in files)


def _test_roots(files: dict[str, str]) -> list[str]:
    roots: set[str] = set()
    for path in files:
        normalized = path.replace("\\", "/")
        if not TEST_FILE_RE.search(normalized.lower()):
            continue
        parts = normalized.split("/")
        for idx, part in enumerate(parts):
            lower = part.lower()
            if lower in {"test", "tests", "__tests__"}:
                roots.add("/".join(parts[: idx + 1]))
                break
        else:
            roots.add(str(Path(normalized).parent).replace("\\", "/"))
    return sorted(root for root in roots if root and root != ".")


def _shared_prefix_len(left: str, right: str) -> int:
    left_parts = [part for part in left.replace("\\", "/").split("/") if part]
    right_parts = [part for part in right.replace("\\", "/").split("/") if part]
    shared = 0
    for left_part, right_part in zip(left_parts, right_parts):
        if left_part != right_part:
            break
        shared += 1
    return shared


def _preferred_test_root(files: dict[str, str], target_file: str) -> str:
    roots = _test_roots(files)
    if not roots:
        return ""
    normalized = target_file.replace("\\", "/")
    ranked = sorted(
        roots,
        key=lambda root: (
            -_shared_prefix_len(root, normalized),
            abs(len(root.split("/")) - len(normalized.split("/"))),
            root,
        ),
    )
    return ranked[0]


def _test_root_for_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    for idx, part in enumerate(parts):
        if part.lower() in {"test", "tests", "__tests__"}:
            return "/".join(parts[: idx + 1])
    return str(Path(normalized).parent).replace("\\", "/")


def expected_test_root(files: dict[str, str], target_file: str) -> str:
    return _preferred_test_root(files, target_file)


def test_root_for_path(path: str) -> str:
    return _test_root_for_path(path)


def guess_test_target(files: dict[str, str], target_file: str) -> str:
    normalized = target_file.replace("\\", "/")
    stem = Path(normalized).stem
    for path in sorted(files):
        lower = path.replace("\\", "/").lower()
        if TEST_FILE_RE.search(lower) and stem in lower:
            return path
    preferred_root = _preferred_test_root(files, normalized)
    if preferred_root:
        return f"{preferred_root}/test_{stem}.py"
    return ""


def _line_count(content: str) -> int:
    return len(content.splitlines())


def _pattern_bonus(pattern_type: str) -> int:
    return PATTERN_PRIORITY.get(pattern_type, 0)


# ── Pattern scanner ──────────────────────────────────────────
class PatternScanner:
    def scan(self, candidate: RepoCandidate) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        files = candidate.files
        base_bonus = 8 if has_tests(files) else -4

        for path, content in files.items():
            if not path.endswith((".py", ".ts", ".tsx")):
                continue
            opportunities.extend(self._scan_missing_timeout(candidate, path, content, base_bonus, files))
            opportunities.extend(self._scan_unchecked_response_shape(candidate, path, content, base_bonus, files))
            opportunities.extend(self._scan_unsafe_file_write(candidate, path, content, base_bonus, files))
            opportunities.extend(self._scan_overbroad_exception(candidate, path, content, base_bonus, files))
            opportunities.extend(self._scan_missing_input_validation(candidate, path, content, base_bonus, files))
            opportunities.extend(self._scan_resource_cleanup_gap(candidate, path, content, base_bonus, files))
            opportunities.extend(self._scan_missing_regression_test(candidate, path, content, base_bonus, files))
            opportunities.extend(self._scan_feature_upgrade_todo(candidate, path, content, base_bonus, files))
            opportunities.extend(self._scan_missing_retry_backoff(candidate, path, content, base_bonus, files))
            opportunities.extend(self._scan_unsafe_subprocess(candidate, path, content, base_bonus, files))
            opportunities.extend(self._scan_temp_file_cleanup(candidate, path, content, base_bonus, files))
            opportunities.extend(self._scan_flaky_time_dependent_test(candidate, path, content, base_bonus, files))
        return self._dedupe(opportunities)

    def _dedupe(self, opportunities: list[Opportunity]) -> list[Opportunity]:
        deduped: dict[tuple[str, str], Opportunity] = {}
        for opportunity in opportunities:
            key = (opportunity.target_file, opportunity.pattern_type)
            existing = deduped.get(key)
            if existing is None or opportunity.acceptance_score > existing.acceptance_score:
                deduped[key] = opportunity
        return list(deduped.values())

    def _build_opportunity(
        self,
        candidate: RepoCandidate,
        path: str,
        pattern_type: str,
        failure_mode: str,
        evidence: str,
        line_no: int,
        base_score: int,
        files: dict[str, str],
    ) -> Opportunity:
        return Opportunity(
            repo_full_name=candidate.full_name,
            target_file=path,
            pattern_type=pattern_type,
            failure_mode=failure_mode,
            evidence=evidence,
            patch_scope=1,
            test_target=guess_test_target(files, path),
            acceptance_score=base_score + _pattern_bonus(pattern_type),
            opportunity_kind="bugfix",
            evidence_lines=[line_no],
        )

    def _scan_missing_timeout(
        self,
        candidate: RepoCandidate,
        path: str,
        content: str,
        base_bonus: int,
        files: dict[str, str],
    ) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return opportunities

        methods = {"get", "post", "put", "patch", "delete", "request"}
        clients = {"requests", "httpx"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in methods:
                continue
            if not isinstance(node.func.value, ast.Name) or node.func.value.id not in clients:
                continue
            if any(keyword.arg == "timeout" for keyword in node.keywords):
                continue
            evidence = f"Line {node.lineno} issues an HTTP call without an explicit timeout."
            failure_mode = "A slow or hanging upstream endpoint can block the command indefinitely instead of failing fast."
            opportunities.append(
                self._build_opportunity(candidate, path, "missing_timeout", failure_mode, evidence, node.lineno, 78 + base_bonus, files)
            )
            break
        return opportunities

    def _scan_unchecked_response_shape(
        self,
        candidate: RepoCandidate,
        path: str,
        content: str,
        base_bonus: int,
        files: dict[str, str],
    ) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        lines = content.splitlines()
        patterns = (
            re.compile(r"\.json\(\)\s*\[[^\]]+\]"),
            re.compile(r"json\.loads\([^)]+\)\s*\[[^\]]+\]"),
            re.compile(r"\[[\"'][^\"']+[\"']\]\s*\[[\"'][^\"']+[\"']\]"),
        )
        for idx, line in enumerate(lines):
            if any(pattern.search(line) for pattern in patterns):
                evidence = f"Line {idx + 1} indexes parsed response data before validating keys or shape."
                failure_mode = "A partial or malformed API response can raise KeyError or IndexError on a valid request path."
                opportunities.append(
                    self._build_opportunity(candidate, path, "unchecked_response_shape", failure_mode, evidence, idx + 1, 74 + base_bonus, files)
                )
                break
        return opportunities

    def _scan_unsafe_file_write(
        self,
        candidate: RepoCandidate,
        path: str,
        content: str,
        base_bonus: int,
        files: dict[str, str],
    ) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        lines = content.splitlines()
        sink = re.compile(r"(write_text\(|open\()")
        taint = re.compile(r"(sys\.argv|input\(|args\.[A-Za-z_]+|os\.getenv\(|environ\[)")
        has_parent_mkdir = "mkdir(" in content or ".parent" in content
        for idx, line in enumerate(lines):
            if not sink.search(line):
                continue
            if not taint.search(line):
                continue
            evidence = f"Line {idx + 1} writes to a path derived from external input without a clear validation step."
            failure_mode = "A malformed path input can write to an unintended location or fail with a filesystem error on a valid CLI invocation."
            score = 68 + base_bonus + (0 if has_parent_mkdir else 4)
            opportunities.append(
                self._build_opportunity(candidate, path, "unsafe_file_write_or_path", failure_mode, evidence, idx + 1, score, files)
            )
            break
        return opportunities

    def _scan_overbroad_exception(
        self,
        candidate: RepoCandidate,
        path: str,
        content: str,
        base_bonus: int,
        files: dict[str, str],
    ) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        lines = content.splitlines()
        for idx, line in enumerate(lines):
            if "except Exception" not in line and line.strip() != "except:":
                continue
            body = "\n".join(lines[idx + 1: min(idx + 4, len(lines))]).strip().lower()
            if not body:
                continue
            if any(marker in body for marker in ("pass", "return none", "return {}", "continue", "log.", "logging.", "print(")):
                evidence = f"Line {idx + 1} catches a broad exception and only logs/returns, which can hide a distinct failure."
                failure_mode = "A real upstream or parsing error can be silently swallowed, leaving operators with no actionable signal."
                opportunities.append(
                    self._build_opportunity(candidate, path, "overbroad_exception_handling", failure_mode, evidence, idx + 1, 70 + base_bonus, files)
                )
                break
        return opportunities

    def _scan_missing_input_validation(
        self,
        candidate: RepoCandidate,
        path: str,
        content: str,
        base_bonus: int,
        files: dict[str, str],
    ) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        lines = content.splitlines()
        patterns = (
            re.compile(r"sys\.argv\[\d+\]"),
            re.compile(r"int\(os\.getenv\("),
            re.compile(r"float\(os\.getenv\("),
        )
        for idx, line in enumerate(lines):
            if any(pattern.search(line) for pattern in patterns):
                evidence = f"Line {idx + 1} consumes external input directly without validating presence or format first."
                failure_mode = "A missing or malformed CLI/env input can raise IndexError or ValueError instead of producing a clear operator-facing error."
                opportunities.append(
                    self._build_opportunity(candidate, path, "missing_input_validation", failure_mode, evidence, idx + 1, 79 + base_bonus, files)
                )
                break
        return opportunities

    def _scan_resource_cleanup_gap(
        self,
        candidate: RepoCandidate,
        path: str,
        content: str,
        base_bonus: int,
        files: dict[str, str],
    ) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        lines = content.splitlines()
        assign_open = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*open\(")
        if "with open(" in content:
            return opportunities
        for idx, line in enumerate(lines):
            if not assign_open.search(line):
                continue
            var = line.split("=", 1)[0].strip()
            remainder = "\n".join(lines[idx + 1:])
            if f"{var}.close(" in remainder:
                continue
            evidence = f"Line {idx + 1} opens a file handle outside a context manager with no matching close call."
            failure_mode = "An exception on the write/read path can leave the file handle open and leak resources across repeated runs."
            opportunities.append(
                self._build_opportunity(candidate, path, "resource_cleanup_gap", failure_mode, evidence, idx + 1, 72 + base_bonus, files)
            )
            break
        return opportunities

    def _scan_missing_regression_test(
        self,
        candidate: RepoCandidate,
        path: str,
        content: str,
        base_bonus: int,
        files: dict[str, str],
    ) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        if TEST_FILE_RE.search(path.replace("\\", "/").lower()):
            return opportunities
        lines = content.splitlines()
        comment_re = re.compile(r"(fixme|todo|bug|regression)", re.IGNORECASE)
        for idx, line in enumerate(lines):
            if not comment_re.search(line):
                continue
            evidence = f"Line {idx + 1} already documents a bug or regression scenario, but no matching regression test anchor was found."
            failure_mode = "The documented broken path can regress silently because there is no focused regression coverage."
            opportunities.append(
                self._build_opportunity(candidate, path, "missing_regression_test_for_obvious_bugfix", failure_mode, evidence, idx + 1, 64 + base_bonus, files)
            )
            break
        return opportunities

    def _scan_feature_upgrade_todo(
        self,
        candidate: RepoCandidate,
        path: str,
        content: str,
        base_bonus: int,
        files: dict[str, str],
    ) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        lines = content.splitlines()
        for idx, line in enumerate(lines):
            if not FEATURE_TODO_RE.search(line):
                continue
            evidence = (
                f"Line {idx + 1} contains an in-code TODO/FIXME that explicitly points to a missing capability: "
                f"{line.strip()[:180]}"
            )
            failure_mode = (
                "The code documents an intended operator-visible capability that is still missing, so users cannot access "
                "the behavior the maintainers already signaled should exist."
            )
            opportunities.append(
                Opportunity(
                    repo_full_name=candidate.full_name,
                    target_file=path,
                    pattern_type="maintainer_todo_feature_upgrade",
                    failure_mode=failure_mode,
                    evidence=evidence,
                    patch_scope=1,
                    test_target=guess_test_target(files, path),
                    acceptance_score=76 + base_bonus,
                    opportunity_kind="feature_upgrade",
                    source_ref=f"code_comment:{path}:{idx + 1}",
                    maintainer_intent=True,
                    evidence_lines=[idx + 1],
                )
            )
            break
        return opportunities

    def _scan_missing_retry_backoff(
        self,
        candidate: RepoCandidate,
        path: str,
        content: str,
        base_bonus: int,
        files: dict[str, str],
    ) -> list[Opportunity]:
        http_pattern = re.compile(r"\b(requests|httpx)\.(get|post|put|patch|delete|request)\(")
        retry_markers = ("retry", "backoff", "tenacity", "Retry(", "max_retries", "retries=")
        if not any(http_pattern.search(line) for line in content.splitlines()):
            return []
        if any(marker in content for marker in retry_markers):
            return []
        lines = content.splitlines()
        for idx, line in enumerate(lines):
            if http_pattern.search(line):
                evidence = f"Line {idx + 1} issues an HTTP call with no retry or backoff strategy in this file."
                failure_mode = "A transient upstream failure will propagate as an uncaught exception instead of being retried with backoff."
                return [self._build_opportunity(candidate, path, "missing_retry_backoff", failure_mode, evidence, idx + 1, 70 + base_bonus, files)]
        return []

    def _scan_unsafe_subprocess(
        self,
        candidate: RepoCandidate,
        path: str,
        content: str,
        base_bonus: int,
        files: dict[str, str],
    ) -> list[Opportunity]:
        shell_true = re.compile(r"subprocess\.(run|call|check_output|Popen)\([^)]*shell\s*=\s*True")
        taint = re.compile(r"(sys\.argv|input\(|args\.[A-Za-z_]+|os\.getenv\(|environ\[)")
        lines = content.splitlines()
        for idx, line in enumerate(lines):
            if not shell_true.search(line):
                continue
            context = "\n".join(lines[max(0, idx - 3): min(idx + 4, len(lines))])
            if not taint.search(context):
                continue
            evidence = f"Line {idx + 1} calls subprocess with shell=True using externally-controlled input."
            failure_mode = "An unsanitized string from user input or environment can be injected as shell commands."
            return [self._build_opportunity(candidate, path, "unsafe_subprocess", failure_mode, evidence, idx + 1, 76 + base_bonus, files)]
        return []

    def _scan_temp_file_cleanup(
        self,
        candidate: RepoCandidate,
        path: str,
        content: str,
        base_bonus: int,
        files: dict[str, str],
    ) -> list[Opportunity]:
        unsafe_tempfile = re.compile(r"tempfile\.(mkstemp|mkdtemp)\(")
        lines = content.splitlines()
        for idx, line in enumerate(lines):
            if not unsafe_tempfile.search(line):
                continue
            remainder = "\n".join(lines[idx:min(idx + 30, len(lines))])
            if "os.unlink(" in remainder or "shutil.rmtree(" in remainder or "try:" in remainder:
                continue
            evidence = f"Line {idx + 1} creates a temporary file or directory without guaranteed cleanup on failure."
            failure_mode = "An exception before cleanup leaves orphaned temp files that accumulate across repeated runs."
            return [self._build_opportunity(candidate, path, "temp_file_cleanup_gap", failure_mode, evidence, idx + 1, 68 + base_bonus, files)]
        return []

    def _scan_flaky_time_dependent_test(
        self,
        candidate: RepoCandidate,
        path: str,
        content: str,
        base_bonus: int,
        files: dict[str, str],
    ) -> list[Opportunity]:
        if not TEST_FILE_RE.search(path.replace("\\", "/").lower()):
            return []
        sleep_re = re.compile(r"\btime\.sleep\(\s*\d")
        datetime_assert_re = re.compile(r"(assert|assertEqual).*datetime\.now|datetime\.now.*assert", re.IGNORECASE)
        lines = content.splitlines()
        for idx, line in enumerate(lines):
            if sleep_re.search(line):
                evidence = f"Line {idx + 1} uses a hardcoded sleep inside a test."
                failure_mode = "A hardcoded sleep causes non-deterministic failures under load or on slow CI runners."
                return [self._build_opportunity(candidate, path, "flaky_time_dependent_test", failure_mode, evidence, idx + 1, 66 + base_bonus, files)]
            if datetime_assert_re.search(line):
                evidence = f"Line {idx + 1} asserts against live datetime.now() which produces non-deterministic results."
                failure_mode = "A time-sensitive assertion can fail intermittently depending on execution speed or system clock precision."
                return [self._build_opportunity(candidate, path, "flaky_time_dependent_test", failure_mode, evidence, idx + 1, 66 + base_bonus, files)]
        return []


# ── Qualification ────────────────────────────────────────────
def _local_scope_lines(content: str, opportunity: Opportunity) -> int | None:
    """Lines spanned by the smallest function enclosing the evidence line.

    Returns None when the evidence is module-level (no enclosing function),
    unknown, or the file is unparseable — callers treat None as "cannot prove
    locality" and keep the broad-file rejection. A small returned value means the
    fix is tightly scoped even if the whole file is large.
    """
    if not opportunity.evidence_lines:
        return None
    line_no = opportunity.evidence_lines[0]
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return None
    best: int | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", None)
        if end is None:
            continue
        if node.lineno <= line_no <= end:
            span = end - node.lineno + 1
            if best is None or span < best:
                best = span
    return best


def _fix_is_localized(content: str, line_count: int, opportunity: Opportunity) -> bool:
    """True if the evidence sits in a small function inside a not-absurdly-large file."""
    if line_count > MAX_TARGET_FILE_LINES * LOCALITY_FILE_CAP_FACTOR:
        return False
    local_scope = _local_scope_lines(content, opportunity)
    return local_scope is not None and local_scope <= MAX_LOCAL_SCOPE_LINES


def qualify_opportunity(candidate: RepoCandidate, opportunity: Opportunity) -> QualificationResult:
    content = candidate.files.get(opportunity.target_file, "")
    if not content:
        return QualificationResult(False, "target_area_too_broad", "Target file is unavailable for qualification.", 0)

    if opportunity.patch_scope > 2:
        return QualificationResult(False, "patch_scope_too_wide", "Patch scope exceeds the engine limit of two files.", opportunity.acceptance_score)

    line_count = _line_count(content)
    if line_count > MAX_TARGET_FILE_LINES and not _fix_is_localized(content, line_count, opportunity):
        return QualificationResult(False, "target_area_too_broad", "Target file is too large for a narrow acceptance-first PR.", opportunity.acceptance_score)

    combined = f"{opportunity.failure_mode} {opportunity.evidence}".lower()
    if any(marker in combined for marker in VAGUE_MARKERS):
        return QualificationResult(False, "evidence_too_weak", "Opportunity rationale is vague rather than failure-mode driven.", opportunity.acceptance_score)

    if len(opportunity.failure_mode.split()) < 6:
        return QualificationResult(False, "failure_mode_not_concrete", "Failure mode is too short to be decision-complete.", opportunity.acceptance_score)

    score = opportunity.acceptance_score
    normalized_target = opportunity.target_file.replace("\\", "/")
    if (
        opportunity.opportunity_kind == "bugfix"
        and CORE_ENTRYPOINT_RE.search(normalized_target)
        and line_count > 180
        and not _fix_is_localized(content, line_count, opportunity)
    ):
        return QualificationResult(False, "target_area_too_broad", "Core entrypoint target is too large for a narrow acceptance-first PR.", score)
    preferred_test_root = _preferred_test_root(candidate.files, opportunity.target_file)
    if opportunity.test_target and preferred_test_root:
        proposed_root = _test_root_for_path(opportunity.test_target)
        if proposed_root != preferred_test_root and opportunity.test_target not in candidate.files:
            return QualificationResult(
                False,
                "invalid_test_target_layout",
                f"Suggested test target does not match the repository test layout (expected root near {preferred_test_root}).",
                score,
            )
    if opportunity.test_target:
        score += 8
    else:
        score -= 8
    if line_count > 220:
        score -= 16
    elif line_count > 160:
        score -= 8
    if opportunity.opportunity_kind == "bugfix" and CORE_ENTRYPOINT_RE.search(normalized_target):
        score -= 12
    if opportunity.patch_scope > 1:
        score -= 10
    if candidate.files.get("requirements.txt") or candidate.files.get("pyproject.toml"):
        score += 3

    if opportunity.opportunity_kind == "bugfix":
        if score < QUALIFY_MIN_SCORE:
            return QualificationResult(False, "low_acceptance_score", "Opportunity is too weak relative to current acceptance heuristics.", score)
        return QualificationResult(True, "", "Qualified narrow opportunity with concrete failure mode.", score)

    if opportunity.opportunity_kind in {"feature_upgrade", "feature_add"}:
        if not opportunity.maintainer_intent:
            return QualificationResult(False, "missing_maintainer_intent", "Feature work requires explicit maintainer intent.", score)
        if not opportunity.source_ref:
            return QualificationResult(False, "missing_source_reference", "Feature work must point to a concrete issue or code comment source.", score)
        if opportunity.opportunity_kind == "feature_add" and not opportunity.source_ref.startswith("issue:"):
            return QualificationResult(False, "feature_add_requires_issue", "New feature work must be backed by a narrow issue signal.", score)
        if score < FEATURE_QUALIFY_MIN_SCORE:
            return QualificationResult(False, "low_acceptance_score", "Feature opportunity is too weak for enhancement-level risk.", score)
        return QualificationResult(True, "", "Qualified maintainer-signaled feature opportunity with narrow scope.", score)

    return QualificationResult(False, "unknown_opportunity_kind", f"Unknown opportunity kind: {opportunity.opportunity_kind}", score)
