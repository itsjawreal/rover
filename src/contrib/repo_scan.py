from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Iterable

from src.contrib.opportunity_engine import PatternScanner, qualify_opportunity
from src.contrib.pr_generator import fetch_repo_metadata, get_repo_inspect_data
from src.github.scraper import RepoCandidate, _gh_get, _get_license, download_repo_files

_BUG_SCAN_EXTENSIONS = (".py", ".ts", ".tsx")
_SECURITY_SCAN_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".jsx", ".yml", ".yaml")
_ARCHIVE_EXTENSIONS = (".zip", ".rar", ".7z", ".tar", ".gz")
_BINARY_ARTIFACT_EXTENSIONS = (".exe", ".dll", ".scr", ".apk", ".jar", ".bin", ".iso")
_PRIVILEGED_SCRIPT_EXTENSIONS = (".bat", ".cmd", ".ps1")


@dataclass(frozen=True)
class ScanFinding:
    kind: str
    rule_id: str
    severity: str
    confidence: str
    file: str
    line: int
    finding: str
    evidence: str
    why_it_matters: str
    recommendation: str


_SECRET_ASSIGN_RE = re.compile(
    r"""(?ix)
    \b(?:api[_-]?key|secret|token|access[_-]?token|client[_-]?secret|password)\b
    \s*[:=]\s*
    ["']([A-Za-z0-9_\-]{16,})["']
    """
)
_SAFE_YAML_RE = re.compile(r"\byaml\.safe_load\(")
_UNSAFE_YAML_RE = re.compile(r"\byaml\.load\(")
_SHELL_TRUE_RE = re.compile(r"subprocess\.(?:run|call|check_output|Popen)\([^)]*shell\s*=\s*True", re.IGNORECASE)
_REQUESTS_VERIFY_FALSE_RE = re.compile(r"\brequests\.(?:get|post|put|patch|delete|request|head|options)\([^)]*verify\s*=\s*False", re.IGNORECASE)
_URLLIB_VERIFY_FALSE_RE = re.compile(r"\burllib3\.(?:poolmanager|proxymanager)\([^)]*cert_reqs\s*=\s*[\"']?cert_none[\"']?", re.IGNORECASE)
_UNVERIFIED_SSL_CONTEXT_RE = re.compile(r"\bssl\._create_unverified_context\(")
_TARFILE_IMPORT_RE = re.compile(r"\bimport\s+tarfile\b|\bfrom\s+tarfile\s+import\b")
_ZIPFILE_IMPORT_RE = re.compile(r"\bimport\s+zipfile\b|\bfrom\s+zipfile\s+import\b")
_EXTRACTALL_RE = re.compile(r"\.extractall\(")
_TARFILE_ALIAS_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*tarfile\.")
_ZIPFILE_ALIAS_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*zipfile\.")
_EVAL_RE = re.compile(r"\beval\(")
_PYTHON_EXEC_RE = re.compile(r"\bexec\(")
_NODE_EXEC_RE = re.compile(r"\bexec(?:Sync)?\(")
_NODE_EXEC_INTERPOLATED_RE = re.compile(r"\bexec(?:Sync)?\(\s*(?:`[^`]*\$\{|[^)]*\+)")
_PICKLE_RE = re.compile(r"\bpickle\.(?:load|loads)\(")
_SOCIAL_RISK_PATTERNS = (
    (
        "disable antivirus",
        "high",
        "medium",
        "Instruction asks the user to disable antivirus or endpoint protection.",
        "This is a common social-engineering pattern used to bypass malware detection.",
        "Do not follow the instruction. Review the repo manually before trusting any artifact.",
    ),
    (
        "windows defender",
        "medium",
        "medium",
        "Instruction references Windows Defender in a way that suggests bypassing protection.",
        "Legitimate projects rarely need users to weaken endpoint protection to run.",
        "Treat the repo as untrusted until the executable path and contents are independently reviewed.",
    ),
    (
        "run as admin",
        "medium",
        "medium",
        "Instruction asks the user to execute payloads with elevated privileges.",
        "Admin-only execution increases impact if the artifact is malicious or overly broad.",
        "Avoid elevation unless the code path is reviewed and the privilege need is justified.",
    ),
    (
        "paste your token",
        "high",
        "high",
        "Instruction asks the user to paste a token or credential into the workflow.",
        "Credential harvesting is a direct account-compromise risk.",
        "Do not provide credentials. Prefer standard OAuth/device flows or audited secret handling.",
    ),
    (
        "paste your session",
        "high",
        "high",
        "Instruction asks the user to paste a session value.",
        "Session capture can directly hijack existing authenticated accounts.",
        "Do not share sessions or cookies with the repository workflow.",
    ),
    (
        "paste your cookie",
        "high",
        "high",
        "Instruction asks the user to provide browser cookies.",
        "Cookie exfiltration is a strong account-takeover signal.",
        "Do not provide browser cookies. Treat the repo as untrusted until proven otherwise.",
    ),
    (
        "smartscreen",
        "medium",
        "medium",
        "Instruction references bypassing SmartScreen or similar OS trust prompts.",
        "Bypass instructions are common when distributing unsigned or suspicious binaries.",
        "Verify signatures, hashes, and source provenance before running the artifact.",
    ),
)

_PLACEHOLDER_TOKENS = ("example", "changeme", "replace", "your_", "xxxxx", "sample", "dummy", "test")

_BUG_RECOMMENDATIONS = {
    "missing_timeout": "Add an explicit timeout and handle timeout failures cleanly.",
    "missing_input_validation": "Validate external input early and return a deterministic error path.",
    "unsafe_subprocess": "Replace shell execution with argv lists and strict input validation.",
    "unchecked_response_shape": "Validate response shape before indexing into dynamic payloads.",
    "resource_cleanup_gap": "Ensure files, sockets, or handles are closed on every exit path.",
    "missing_retry_backoff": "Add bounded retry logic with backoff for transient failures only.",
    "unsafe_file_write_or_path": "Normalize and constrain file paths before writes.",
    "overbroad_exception_handling": "Catch specific exception types and preserve actionable failures.",
    "temp_file_cleanup_gap": "Delete temporary artifacts on success and failure paths.",
    "flaky_time_dependent_test": "Remove timing sensitivity from tests and assert deterministic behavior.",
}
_BUG_SEVERITY = {
    "missing_timeout": "high",
    "unchecked_response_shape": "high",
    "missing_input_validation": "high",
    "unsafe_subprocess": "high",
    "resource_cleanup_gap": "medium",
    "missing_retry_backoff": "medium",
    "unsafe_file_write_or_path": "medium",
    "overbroad_exception_handling": "medium",
    "temp_file_cleanup_gap": "medium",
    "flaky_time_dependent_test": "low",
}


def _security_finding(
    *,
    rule_id: str,
    severity: str,
    confidence: str,
    file: str,
    line: int,
    finding: str,
    evidence: str,
    why_it_matters: str,
    recommendation: str,
) -> ScanFinding:
    return ScanFinding(
        kind="security",
        rule_id=rule_id,
        severity=severity,
        confidence=confidence,
        file=file,
        line=line,
        finding=finding,
        evidence=evidence,
        why_it_matters=why_it_matters,
        recommendation=recommendation,
    )


def _trust_finding(
    *,
    rule_id: str,
    severity: str,
    confidence: str,
    file: str,
    line: int,
    finding: str,
    evidence: str,
    why_it_matters: str,
    recommendation: str,
) -> ScanFinding:
    return ScanFinding(
        kind="trust",
        rule_id=rule_id,
        severity=severity,
        confidence=confidence,
        file=file,
        line=line,
        finding=finding,
        evidence=evidence,
        why_it_matters=why_it_matters,
        recommendation=recommendation,
    )


def _language_summary(candidate: RepoCandidate) -> dict[str, int]:
    summary = {"py": 0, "ts": 0, "js": 0, "yaml": 0, "other": 0}
    for path in candidate.files:
        lowered = path.lower()
        if lowered.endswith(".py"):
            summary["py"] += 1
        elif lowered.endswith((".ts", ".tsx")):
            summary["ts"] += 1
        elif lowered.endswith((".js", ".jsx")):
            summary["js"] += 1
        elif lowered.endswith((".yml", ".yaml")):
            summary["yaml"] += 1
        else:
            summary["other"] += 1
    return summary


def _language_summary_from_paths(paths: Iterable[str]) -> dict[str, int]:
    summary = {"py": 0, "ts": 0, "js": 0, "yaml": 0, "other": 0}
    for path in paths:
        lowered = path.lower()
        if lowered.endswith(".py"):
            summary["py"] += 1
        elif lowered.endswith((".ts", ".tsx")):
            summary["ts"] += 1
        elif lowered.endswith((".js", ".jsx")):
            summary["js"] += 1
        elif lowered.endswith((".yml", ".yaml")):
            summary["yaml"] += 1
        else:
            summary["other"] += 1
    return summary


def _supported_file_count(candidate: RepoCandidate, kind: str) -> int:
    extensions = _SECURITY_SCAN_EXTENSIONS if kind in {"security", "trust", "audit"} else _BUG_SCAN_EXTENSIONS
    return sum(1 for path in candidate.files if path.lower().endswith(extensions))


def _coverage_note(candidate: RepoCandidate, kind: str, supported_file_count: int) -> str:
    if supported_file_count == 0:
        return f"no supported source files found for v1 {kind} scan"
    language_summary = _language_summary(candidate)
    if kind == "bug":
        if language_summary["py"] == 0 and language_summary["ts"] == 0:
            return "bug scan coverage is limited in v1 because no Python or TypeScript source files were found"
        if language_summary["py"] == 0:
            return "bug scan coverage is limited in v1 because rules are currently strongest on Python-oriented patterns"
    if kind == "trust":
        if supported_file_count == 0:
            return "trust scan relies mostly on repo tree, README instructions, and visible distribution artifacts because no supported source files were found"
        return "trust scan focuses on distribution patterns, social-risk instructions, and visible artifact signals; source coverage is shown for context"
    if kind == "audit":
        return "audit scan combines trust/distribution signals with deterministic code-security rules; source coverage is shown for context"
    if kind == "security" and language_summary["py"] == 0 and language_summary["ts"] == 0 and language_summary["js"] == 0:
        return "security scan coverage is limited in v1 because only YAML-like config files were found"
    return ""


def _package_json(candidate: RepoCandidate) -> dict:
    raw = candidate.files.get("package.json")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _js_yaml_major_version(candidate: RepoCandidate) -> int | None:
    package_json = _package_json(candidate)
    dependency_sets = []
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        value = package_json.get(key)
        if isinstance(value, dict):
            dependency_sets.append(value)
    for deps in dependency_sets:
        version = deps.get("js-yaml")
        if not isinstance(version, str):
            continue
        match = re.search(r"(\d+)", version)
        if match:
            return int(match.group(1))
    return None


def _is_potentially_unsafe_yaml_load(path: str, line: str, candidate: RepoCandidate) -> bool:
    lowered_path = path.lower()
    if not _UNSAFE_YAML_RE.search(line) or _SAFE_YAML_RE.search(line):
        return False
    if lowered_path.endswith(".py"):
        return True
    if lowered_path.endswith((".ts", ".tsx", ".js", ".jsx")):
        major = _js_yaml_major_version(candidate)
        return major is not None and major < 4
    return False


def _is_node_exec_security_finding(path: str, line: str) -> bool:
    lowered_path = path.lower()
    if not lowered_path.endswith((".ts", ".tsx", ".js", ".jsx")):
        return False
    if not _NODE_EXEC_RE.search(line):
        return False
    return bool(_NODE_EXEC_INTERPOLATED_RE.search(line))


def _archive_extractall_kind(content: str, line: str) -> str | None:
    if not _EXTRACTALL_RE.search(line):
        return None
    lowered = line.lower()
    if "zip" in lowered:
        return "zip"
    if "tar" in lowered:
        return "tar"
    for alias in _ZIPFILE_ALIAS_RE.findall(content):
        if re.search(rf"\b{re.escape(alias)}\.extractall\(", line):
            return "zip"
    for alias in _TARFILE_ALIAS_RE.findall(content):
        if re.search(rf"\b{re.escape(alias)}\.extractall\(", line):
            return "tar"
    if _ZIPFILE_IMPORT_RE.search(content) and not _TARFILE_IMPORT_RE.search(content):
        return "zip"
    if _TARFILE_IMPORT_RE.search(content) and not _ZIPFILE_IMPORT_RE.search(content):
        return "tar"
    return None


def _scan_security_findings(candidate: RepoCandidate) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    findings.extend(_scan_tree_artifact_findings(candidate))
    findings.extend(_scan_social_risk_findings(candidate))
    for path, content in candidate.files.items():
        if not path.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".yml", ".yaml")):
            continue
        lines = content.splitlines()
        for idx, line in enumerate(lines, start=1):
            if _SHELL_TRUE_RE.search(line):
                findings.append(
                    _security_finding(
                        rule_id="shell_true_subprocess",
                        severity="high",
                        confidence="high",
                        file=path,
                        line=idx,
                        finding="subprocess shell=True detected",
                        evidence=f"Line {idx} uses subprocess with shell=True.",
                        why_it_matters="Shell execution increases command injection risk when inputs are not tightly controlled.",
                        recommendation="Use argv lists instead of shell=True and validate any externally influenced arguments.",
                    )
                )
            if _REQUESTS_VERIFY_FALSE_RE.search(line) or _URLLIB_VERIFY_FALSE_RE.search(line):
                findings.append(
                    _security_finding(
                        rule_id="tls_verification_disabled",
                        severity="high",
                        confidence="high",
                        file=path,
                        line=idx,
                        finding="TLS certificate verification disabled",
                        evidence=f"Line {idx} disables HTTPS certificate verification.",
                        why_it_matters="Disabling certificate verification makes HTTPS traffic vulnerable to man-in-the-middle interception.",
                        recommendation="Keep TLS verification enabled and install the right CA bundle instead of bypassing certificate checks.",
                    )
                )
            if _UNVERIFIED_SSL_CONTEXT_RE.search(line):
                findings.append(
                    _security_finding(
                        rule_id="unverified_ssl_context",
                        severity="high",
                        confidence="high",
                        file=path,
                        line=idx,
                        finding="unverified SSL context detected",
                        evidence=f"Line {idx} calls ssl._create_unverified_context().",
                        why_it_matters="Unverified SSL contexts disable certificate validation and can expose secure channels to interception.",
                        recommendation="Use the default SSL context or a properly configured CA bundle instead of bypassing certificate validation.",
                    )
                )
            archive_extract_kind = _archive_extractall_kind(content, line)
            if archive_extract_kind == "tar":
                findings.append(
                    _security_finding(
                        rule_id="tar_extractall_without_validation",
                        severity="high",
                        confidence="medium",
                        file=path,
                        line=idx,
                        finding="tarfile extractall() detected",
                        evidence=f"Line {idx} calls tarfile extractall().",
                        why_it_matters="Archive extraction without path validation can enable path traversal or overwrite files outside the intended directory.",
                        recommendation="Validate archive member paths before extraction, or use a safe extraction helper that rejects traversal entries.",
                    )
                )
            if archive_extract_kind == "zip":
                findings.append(
                    _security_finding(
                        rule_id="zip_extractall_without_validation",
                        severity="medium",
                        confidence="medium",
                        file=path,
                        line=idx,
                        finding="zipfile extractall() detected",
                        evidence=f"Line {idx} calls zipfile extractall().",
                        why_it_matters="Blind archive extraction can overwrite unexpected files when archive paths are not validated.",
                        recommendation="Validate destination paths for each archive member before extraction, especially for untrusted archives.",
                    )
                )
            if _EVAL_RE.search(line):
                findings.append(
                    _security_finding(
                        rule_id="dynamic_eval",
                        severity="high",
                        confidence="high",
                        file=path,
                        line=idx,
                        finding="eval() detected",
                        evidence=f"Line {idx} calls eval().",
                        why_it_matters="Dynamic evaluation can execute attacker-controlled code if inputs are not fixed constants.",
                        recommendation="Replace eval() with explicit parsing or a constrained dispatch table.",
                    )
                )
            if path.lower().endswith(".py") and _PYTHON_EXEC_RE.search(line):
                findings.append(
                    _security_finding(
                        rule_id="dynamic_exec",
                        severity="high",
                        confidence="high",
                        file=path,
                        line=idx,
                        finding="exec() detected",
                        evidence=f"Line {idx} calls exec().",
                        why_it_matters="Dynamic code execution can run untrusted content and is difficult to secure.",
                        recommendation="Remove exec() and replace it with explicit control flow or validated plugin boundaries.",
                    )
                )
            elif _is_node_exec_security_finding(path, line):
                findings.append(
                    _security_finding(
                        rule_id="shell_command_exec",
                        severity="high",
                        confidence="medium",
                        file=path,
                        line=idx,
                        finding="child_process exec() appears to interpolate command input",
                        evidence=f"Line {idx} builds an exec() command with interpolation or concatenation.",
                        why_it_matters="Interpolated shell commands can become command-injection sinks when any part of the command is externally influenced.",
                        recommendation="Prefer spawn/execFile with argv arrays, or validate and quote every externally influenced segment before invoking a shell.",
                    )
                )
            if _PICKLE_RE.search(line):
                findings.append(
                    _security_finding(
                        rule_id="pickle_deserialization",
                        severity="high",
                        confidence="high",
                        file=path,
                        line=idx,
                        finding="pickle deserialization detected",
                        evidence=f"Line {idx} calls pickle.load/loads.",
                        why_it_matters="Untrusted pickle payloads can execute arbitrary code during deserialization.",
                        recommendation="Do not deserialize untrusted pickle data; prefer safer formats such as JSON.",
                    )
                )
            if _is_potentially_unsafe_yaml_load(path, line, candidate):
                findings.append(
                    _security_finding(
                        rule_id="unsafe_yaml_load",
                        severity="high",
                        confidence="high",
                        file=path,
                        line=idx,
                        finding="yaml.load() without safe loader detected",
                        evidence=f"Line {idx} calls yaml.load().",
                        why_it_matters="Unsafe YAML loading can deserialize attacker-controlled payloads when the loader permits non-safe object construction.",
                        recommendation="Use the library's safe loader path, or pin usage to a version/schema that is safe for untrusted input.",
                    )
                )
            if match := _SECRET_ASSIGN_RE.search(line):
                secret_value = match.group(1)
                if any(token in secret_value.lower() for token in _PLACEHOLDER_TOKENS):
                    continue
                findings.append(
                    _security_finding(
                        rule_id="hardcoded_secret",
                        severity="high",
                        confidence="medium",
                        file=path,
                        line=idx,
                        finding="possible hardcoded secret detected",
                        evidence=f"Line {idx} assigns a credential-like value directly in source.",
                        why_it_matters="Hardcoded secrets are easy to leak and difficult to rotate safely once committed.",
                        recommendation="Move secrets to environment variables or a managed secret store and rotate exposed values.",
                    )
                )
    return _dedupe_findings(findings)


def _scan_tree_artifact_findings(candidate: RepoCandidate) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    try:
        tree = getattr(candidate, "_scan_tree", None) or _gh_get(
            f"https://api.github.com/repos/{candidate.full_name}/git/trees/{candidate.default_branch}",
            params={"recursive": "1"},
            timeout=30,
        )
    except Exception:
        return findings
    archive_hits: list[str] = []
    executable_hits: list[str] = []
    privileged_script_hits: list[str] = []
    for item in tree.get("tree", []):
        if item.get("type") != "blob":
            continue
        path = str(item.get("path") or "")
        lowered = path.lower()
        if lowered.endswith(_ARCHIVE_EXTENSIONS):
            archive_hits.append(path)
        if lowered.endswith(_BINARY_ARTIFACT_EXTENSIONS):
            executable_hits.append(path)
        if lowered.endswith(_PRIVILEGED_SCRIPT_EXTENSIONS):
            privileged_script_hits.append(path)
    if archive_hits:
        findings.append(
            _security_finding(
                rule_id="repository_distributes_archives",
                severity="medium" if not executable_hits else "high",
                confidence="high",
                file=archive_hits[0],
                line=0,
                finding="repository distributes archive payloads",
                evidence=f"Repository tree contains archive artifacts such as: {', '.join(archive_hits[:3])}",
                why_it_matters="Archive-heavy repos with little visible source increase the chance that the real behavior is hidden in packaged payloads.",
                recommendation="Inspect archive contents manually and verify hashes/provenance before using any packaged artifact.",
            )
        )
    if executable_hits:
        findings.append(
            _security_finding(
                rule_id="repository_distributes_executables",
                severity="high",
                confidence="high",
                file=executable_hits[0],
                line=0,
                finding="repository distributes executable payloads",
                evidence=f"Repository tree contains executable-like artifacts such as: {', '.join(executable_hits[:3])}",
                why_it_matters="Executable payloads can bypass source review entirely and require stronger provenance checks than normal source repositories.",
                recommendation="Do not run bundled executables until they are unpacked, reviewed, and verified against trusted provenance.",
            )
        )
    if (archive_hits or executable_hits or privileged_script_hits) and _supported_file_count(candidate, "security") == 0:
        findings.append(
            _security_finding(
                rule_id="low_source_high_artifact_repo",
                severity="high" if executable_hits else "medium",
                confidence="medium",
                file=archive_hits[0] if archive_hits else executable_hits[0] if executable_hits else privileged_script_hits[0],
                line=0,
                finding="repo has low visible source coverage but ships packaged artifacts",
                evidence="The downloaded source surface is minimal while the repository tree still includes packaged artifacts.",
                why_it_matters="When little readable source is present, trust decisions depend more on opaque payloads than on auditable code.",
                recommendation="Treat the repository as distribution-first. Prefer manual review and sandboxed inspection before use.",
            )
        )
    return findings


def _scan_social_risk_findings(candidate: RepoCandidate) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    for path, content in candidate.files.items():
        lowered_path = path.lower()
        if not lowered_path.endswith((".md", ".txt", ".yml", ".yaml")):
            continue
        lines = content.splitlines()
        for idx, line in enumerate(lines, start=1):
            lowered = line.lower()
            for needle, severity, confidence, finding, why, recommendation in _SOCIAL_RISK_PATTERNS:
                if needle in lowered:
                    findings.append(
                        _security_finding(
                            rule_id="social_risk_instruction",
                            severity=severity,
                            confidence=confidence,
                            file=path,
                            line=idx,
                            finding=finding,
                            evidence=f"Line {idx} contains instruction text matching `{needle}`.",
                            why_it_matters=why,
                            recommendation=recommendation,
                        )
                    )
    return findings


def _scan_trust_findings(candidate: RepoCandidate) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    security_findings = _scan_tree_artifact_findings(candidate) + _scan_social_risk_findings(candidate)
    for finding in security_findings:
        findings.append(
            _trust_finding(
                rule_id=finding.rule_id,
                severity=finding.severity,
                confidence=finding.confidence,
                file=finding.file,
                line=finding.line,
                finding=finding.finding,
                evidence=finding.evidence,
                why_it_matters=finding.why_it_matters,
                recommendation=finding.recommendation,
            )
        )
    if candidate.archived:
        findings.append(
            _trust_finding(
                rule_id="repo_archived",
                severity="medium",
                confidence="high",
                file="repo",
                line=0,
                finding="repository is archived",
                evidence="GitHub metadata marks this repository as archived.",
                why_it_matters="Archived repos are usually unmaintained, so security fixes and operator support may no longer arrive.",
                recommendation="Treat the repo as maintenance-frozen. Prefer maintained alternatives unless you can absorb full ownership risk.",
            )
        )
    if candidate.disabled:
        findings.append(
            _trust_finding(
                rule_id="repo_disabled",
                severity="high",
                confidence="high",
                file="repo",
                line=0,
                finding="repository is disabled",
                evidence="GitHub metadata marks this repository as disabled.",
                why_it_matters="Disabled repos are outside normal trust assumptions and may indicate policy or abuse problems.",
                recommendation="Do not trust or depend on the repo until the disablement reason is independently understood.",
            )
        )
    return findings


def _scan_bug_findings(candidate: RepoCandidate) -> list[ScanFinding]:
    scanner = PatternScanner()
    findings: list[ScanFinding] = []
    for opportunity in scanner.scan(candidate):
        normalized_path = opportunity.target_file.replace("\\", "/").lower()
        if opportunity.pattern_type == "missing_regression_test_for_obvious_bugfix":
            continue
        if normalized_path.startswith("tests/") or normalized_path.startswith("test/"):
            continue
        qualification = qualify_opportunity(candidate, opportunity)
        if not qualification.accepted:
            continue
        findings.append(
            ScanFinding(
                kind="bug",
                rule_id=opportunity.pattern_type,
                severity=_BUG_SEVERITY.get(opportunity.pattern_type, "medium"),
                confidence="high",
                file=opportunity.target_file,
                line=(opportunity.evidence_lines[0] if opportunity.evidence_lines else 0),
                finding=opportunity.failure_mode,
                evidence=opportunity.evidence,
                why_it_matters=qualification.summary,
                recommendation=_BUG_RECOMMENDATIONS.get(
                    opportunity.pattern_type,
                    "Keep the fix narrow, evidence-backed, and covered by the repository test layout.",
                ),
            )
        )
    return _dedupe_findings(findings)


def _build_audit_findings(repo: str, log) -> tuple[RepoCandidate, list[ScanFinding], dict[str, object]]:
    security_candidate = _fetch_scan_candidate(repo, log)
    trust_candidate = _fetch_trust_scan_candidate(repo, log)
    findings = _dedupe_findings([*_scan_trust_findings(trust_candidate), *_scan_security_findings(security_candidate)])
    inspect_data = get_repo_inspect_data(security_candidate)
    return security_candidate, findings, inspect_data


def _dedupe_findings(findings: Iterable[ScanFinding]) -> list[ScanFinding]:
    deduped: dict[tuple[str, str, int], ScanFinding] = {}
    for finding in findings:
        key = (finding.file, finding.rule_id, finding.line)
        deduped.setdefault(key, finding)
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        deduped.values(),
        key=lambda item: (severity_rank.get(item.severity, 9), item.file, item.line, item.rule_id),
    )


def _fetch_scan_candidate(repo: str, log) -> RepoCandidate:
    candidate = _fetch_scan_candidate_metadata(repo, log)
    candidate.files = download_repo_files(candidate)
    return candidate


def _fetch_trust_scan_candidate(repo: str, log) -> RepoCandidate:
    candidate = _fetch_scan_candidate_metadata(repo, log)
    tree = _gh_get(
        f"https://api.github.com/repos/{candidate.full_name}/git/trees/{candidate.default_branch}",
        params={"recursive": "1"},
        timeout=30,
    )
    candidate._scan_tree = tree
    tree_paths = [
        str(item.get("path") or "")
        for item in tree.get("tree", [])
        if item.get("type") == "blob" and str(item.get("path") or "")
    ]
    candidate._scan_language_summary = _language_summary_from_paths(tree_paths)
    candidate._scan_supported_file_count = sum(
        1 for path in tree_paths if path.lower().endswith(_SECURITY_SCAN_EXTENSIONS)
    )
    candidate.files = download_repo_files(
        candidate,
        allowed_exts=(".md", ".txt", ".yml", ".yaml"),
        tree=tree,
    )
    return candidate


def _fetch_scan_candidate_metadata(repo: str, log) -> RepoCandidate:
    full_name, data = fetch_repo_metadata(repo, log)
    pushed_at = data.get("pushed_at", "")
    try:
        from datetime import datetime, timezone

        pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        pushed_days_ago = (datetime.now(timezone.utc) - pushed).days
    except Exception:
        pushed_days_ago = 0
    return RepoCandidate(
        name=data["name"],
        full_name=full_name,
        description=(data.get("description") or "")[:200],
        stars=data.get("stargazers_count", 0),
        forks=data.get("forks_count", 0),
        license=_get_license(data),
        url=data["html_url"],
        default_branch=data.get("default_branch", "main"),
        pushed_days_ago=pushed_days_ago,
        topics=data.get("topics", []),
        archived=bool(data.get("archived")),
        disabled=bool(data.get("disabled")),
    )


def build_scan_payload(repo: str, log, *, kind: str = "security") -> dict[str, object]:
    normalized_kind = (kind or "security").strip().lower()
    if normalized_kind not in {"security", "bug", "trust", "audit"}:
        raise ValueError(f"unsupported scan kind: {kind}")
    if normalized_kind == "audit":
        candidate, findings, inspect_data = _build_audit_findings(repo, log)
    else:
        candidate = _fetch_trust_scan_candidate(repo, log) if normalized_kind == "trust" else _fetch_scan_candidate(repo, log)
        inspect_data = (
            {
                "targeted_scope": "not evaluated for trust scan",
                "scope_notes": ["trust scan skips full contribution-fit evaluation to keep repo-level checks fast"],
            }
            if normalized_kind == "trust"
            else get_repo_inspect_data(candidate)
        )
        if normalized_kind == "security":
            findings = _scan_security_findings(candidate)
        elif normalized_kind == "trust":
            findings = _scan_trust_findings(candidate)
        else:
            findings = _scan_bug_findings(candidate)
    language_summary = getattr(candidate, "_scan_language_summary", _language_summary(candidate))
    supported_file_count = getattr(candidate, "_scan_supported_file_count", _supported_file_count(candidate, normalized_kind))
    coverage_note = _coverage_note(candidate, normalized_kind, supported_file_count)
    severity_counts = {
        level: sum(1 for finding in findings if finding.severity == level) for level in ("high", "medium", "low")
    }
    payload = {
        "action": "repo_scan",
        "kind": normalized_kind,
        "repo": candidate.full_name,
        "url": candidate.url,
        "description": candidate.description.strip(),
        "targeted_scope": inspect_data.get("targeted_scope", ""),
        "scope_notes": list(inspect_data.get("scope_notes") or []),
        "language_summary": language_summary,
        "supported_file_count": supported_file_count,
        "coverage_note": coverage_note,
        "finding_count": len(findings),
        "severity_counts": severity_counts,
        "finding_kind_counts": {kind: sum(1 for finding in findings if finding.kind == kind) for kind in ("trust", "security", "bug")},
        "findings": [asdict(finding) for finding in findings],
    }
    try:
        from src.contrib.contribution_store import ContributionStore

        ContributionStore().record_scan_summary(
            candidate.full_name,
            kind=normalized_kind,
            severity_counts=severity_counts,
            finding_kind_counts=payload["finding_kind_counts"],
            supported_file_count=supported_file_count,
        )
    except Exception as exc:
        log.debug("Could not persist scan summary for %s: %s", candidate.full_name, exc)
    payload["rendered"] = build_scan_report(payload)
    return payload


def build_scan_report(payload: dict[str, object]) -> str:
    kind = str(payload.get("kind") or "security").title()
    repo = str(payload.get("repo") or "")
    url = str(payload.get("url") or "")
    targeted_scope = str(payload.get("targeted_scope") or "-")
    findings = list(payload.get("findings") or [])
    severity_counts = dict(payload.get("severity_counts") or {})
    lines = [
        f"{kind} Scan",
        "=" * (len(kind) + 5),
        "",
        f"Repo: {repo}",
        f"URL: {url}",
        f"Targeted scope: {targeted_scope}",
        f"Coverage: py={payload.get('language_summary', {}).get('py', 0)} ts={payload.get('language_summary', {}).get('ts', 0)} js={payload.get('language_summary', {}).get('js', 0)} yaml={payload.get('language_summary', {}).get('yaml', 0)} supported={payload.get('supported_file_count', 0)}",
        f"Findings: {len(findings)}",
        f"Severity: high={severity_counts.get('high', 0)} medium={severity_counts.get('medium', 0)} low={severity_counts.get('low', 0)}",
    ]
    payload_kind = str(payload.get("kind") or "")
    if payload_kind == "security":
        lines.append("Summary: high-confidence security rules only; no LLM judgment is used.")
    elif payload_kind == "trust":
        lines.append("Summary: repo-level trust and distribution signals only; this is not a code-vulnerability verdict.")
    elif payload_kind == "audit":
        lines.append("Summary: combined repo-level trust signals and deterministic code-security findings; this is not a formal audit.")
    else:
        lines.append("Summary: user-facing reliability findings only; PR-oriented scanner noise is filtered out.")
    if payload_kind == "audit":
        kind_counts = dict(payload.get("finding_kind_counts") or {})
        lines.append(
            f"Finding types: trust={kind_counts.get('trust', 0)} security={kind_counts.get('security', 0)}"
        )
    scope_notes = [str(note) for note in (payload.get("scope_notes") or []) if str(note).strip()]
    coverage_note = str(payload.get("coverage_note") or "")
    if coverage_note:
        lines.append(f"Coverage note: {coverage_note}")
    if scope_notes:
        lines.extend(["", "Scope notes:"])
        lines.extend(f"- {note}" for note in scope_notes[:3])
    if not findings:
        lines.extend(
            [
                "",
                "No high-confidence findings were detected by the deterministic scanner." if payload_kind != "audit" else "No high-confidence trust or security findings were detected by the deterministic scanner.",
                "This is not a full security audit or formal code review." if payload_kind != "trust" else "This is not a guarantee of safety or legitimacy.",
            ]
        )
        return "\n".join(lines)
    lines.append("")
    for idx, finding in enumerate(findings[:10], start=1):
        lines.extend(
            [
                f"{idx}. [{'{} '.format(finding['kind']) if payload_kind == 'audit' else ''}{finding['severity']}/{finding['confidence']}] {finding['rule_id']}",
                f"   File: {finding['file']}:{finding['line']}",
                f"   Finding: {finding['finding']}",
                f"   Evidence: {finding['evidence']}",
                f"   Why: {finding['why_it_matters']}",
                f"   Do: {finding['recommendation']}",
                "",
            ]
        )
    if len(findings) > 10:
        lines.append(f"... and {len(findings) - 10} more findings.")
    return "\n".join(lines).rstrip()
