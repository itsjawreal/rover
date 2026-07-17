from __future__ import annotations

import re
from dataclasses import dataclass, field


CANONICAL_ACTIONS = (
    "profile",
    "doctor",
    "contrib_once",
    "contrib_targeted",
    "contrib_check",
    "contrib_respond",
    "contrib_report",
    "repo_inspect",
    "repo_scan",
)

_COUNT_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "single": 1,
    "two": 2,
    "three": 3,
}

_REPO_URL_RE = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/.*)?", re.IGNORECASE)
_OWNER_REPO_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")


@dataclass(frozen=True)
class CommandRequest:
    action: str
    count: int = 1
    repo: str = ""
    goal: str = "bugfix"
    scan_kind: str = "security"
    dry_run: bool = True
    first_pr: bool = False
    confidence: str = "medium"
    rationale: list[str] = field(default_factory=list)

    def to_cli_args(self) -> list[str]:
        if self.action == "profile":
            return ["--profile"]
        if self.action == "doctor":
            return ["--doctor"]
        if self.action == "contrib_check":
            return ["--contrib-check"]
        if self.action == "contrib_respond":
            return ["--contrib-respond"]
        if self.action == "contrib_report":
            return ["--contrib-report"]
        if self.action == "repo_inspect":
            return ["--repo-inspect", self.repo]
        if self.action == "repo_scan":
            return ["--scan-repo", self.repo, "--scan-kind", self.scan_kind]

        args = ["--contrib"]
        if self.repo:
            args.append(self.repo)
        args.extend(["--count", str(self.count), "--goal", self.goal])
        if self.first_pr:
            args.append("--first-pr")
        if self.dry_run:
            args.append("--dry-run")
        return args


def parse_command_text(text: str) -> CommandRequest:
    raw = (text or "").strip()
    if not raw:
        return CommandRequest(
            action="doctor",
            confidence="low",
            rationale=["Empty command text; defaulting to a safe doctor action."],
        )

    normalized = " ".join(raw.casefold().split())
    repo = _extract_repo(raw)
    count = _extract_count(normalized)
    goal = _extract_goal(normalized)
    first_pr = any(token in normalized for token in ("first pr", "first contribution"))
    dry_run = not _wants_live_submission(normalized)

    if _matches_any(normalized, "profile", "who am i", "whoami", "github profile", "current login"):
        return CommandRequest(
            action="profile",
            confidence="high",
            rationale=["Matched operator identity/profile intent."],
        )

    if _matches_any(normalized, "doctor", "readiness", "health check", "check environment", "check machine"):
        return CommandRequest(
            action="doctor",
            confidence="high",
            rationale=["Matched environment/readiness intent."],
        )

    if _matches_any(normalized, "report", "summary", "status engine"):
        return CommandRequest(
            action="contrib_report",
            confidence="high",
            rationale=["Matched reporting intent."],
        )

    if _matches_any(normalized, "respond", "reply maintainer", "feedback maintainer", "maintainer feedback"):
        return CommandRequest(
            action="contrib_respond",
            confidence="high",
            rationale=["Matched maintainer-response intent."],
        )

    if _matches_any(normalized, "contrib-check", "pr check", "check pr", "check maintainer", "status pr"):
        return CommandRequest(
            action="contrib_check",
            confidence="high",
            rationale=["Matched PR status/feedback check intent."],
        )

    if _looks_like_repo_inspect(normalized):
        return CommandRequest(
            action="repo_inspect",
            repo=repo,
            confidence="high" if repo else "medium",
            rationale=["Matched repo-inspection intent.", "Repo target extracted." if repo else "Repo target still needed."],
        )

    if _looks_like_repo_scan(normalized):
        if _matches_any(normalized, "audit", "full scan", "full audit", "scan audit"):
            scan_kind = "audit"
        elif _matches_any(normalized, "trust", "reputation", "legit", "safe to use"):
            scan_kind = "trust"
        elif _matches_any(normalized, "security", "secure", "vulnerability", "vuln"):
            scan_kind = "security"
        else:
            scan_kind = "bug"
        return CommandRequest(
            action="repo_scan",
            repo=repo,
            scan_kind=scan_kind,
            confidence="high" if repo else "medium",
            rationale=[f"Matched repo-scan intent ({scan_kind}).", "Repo target extracted." if repo else "Repo target still needed."],
        )

    if _looks_like_contribution_request(normalized):
        action = "contrib_targeted" if repo else "contrib_once"
        rationale = ["Matched contribution/PR intent."]
        if repo:
            rationale.append("Repo target extracted from command text.")
        else:
            rationale.append("No repo target found, so search mode will be used.")
        if dry_run:
            rationale.append("Natural-language commands default to preview mode until explicit live submission words are used.")
        else:
            rationale.append("Live submission words detected; dry-run disabled.")
        return CommandRequest(
            action=action,
            count=count,
            repo=repo,
            goal=goal,
            dry_run=dry_run,
            first_pr=first_pr,
            confidence="high" if repo or count else "medium",
            rationale=rationale,
        )

    return CommandRequest(
        action="doctor",
        confidence="low",
        rationale=["No strong command intent matched; defaulting to a safe doctor action."],
    )


def _extract_repo(text: str) -> str:
    if match := _REPO_URL_RE.search(text):
        return match.group(1)
    for match in _OWNER_REPO_RE.finditer(text):
        candidate = match.group(1)
        if candidate.lower() != "python/m":
            return candidate
    return ""


def _extract_count(normalized: str) -> int:
    if match := re.search(r"\b(\d+)\b", normalized):
        return max(1, int(match.group(1)))
    for word, value in _COUNT_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", normalized):
            return value
    return 1


def _extract_goal(normalized: str) -> str:
    if _matches_any(
        normalized,
        "update deps",
        "update dependency",
        "update dependencies",
        "upgrade deps",
        "upgrade dependency",
        "upgrade dependencies",
        "bump deps",
        "bump dependency",
        "bump dependencies",
    ):
        return "dep_update"
    if _matches_any(normalized, "feature add", "new feature", "add feature"):
        return "feature_add"
    if _matches_any(normalized, "upgrade", "improve feature", "enhancement"):
        return "feature_upgrade"
    return "bugfix"


def _wants_live_submission(normalized: str) -> bool:
    if normalized.startswith("run "):
        return True
    # "submit" must be a whole word: "already submitted PRs" is a status
    # question, not a request to disable dry-run.
    if _has_word(normalized, "submit"):
        return True
    return _matches_any(
        normalized,
        "menisik run",
        "rover run",
        "open pr",
        "create pr now",
        "live run",
        "non dry run",
        "real pr",
    )


def _looks_like_repo_inspect(normalized: str) -> bool:
    return _matches_any(
        normalized,
        "inspect repo",
        "repo inspect",
        "check repo",
        "analyze repo",
        "review repo",
    )


def _looks_like_repo_scan(normalized: str) -> bool:
    return _matches_any(
        normalized,
        "scan repo",
        "audit scan",
        "scan audit",
        "full audit",
        "trust scan",
        "scan trust",
        "repo trust",
        "security scan",
        "scan security",
        "check security",
        "scan bug",
        "check bug",
    )


def _looks_like_contribution_request(normalized: str) -> bool:
    # Whole-word matching: bare substrings misfire badly here ("pr" is inside
    # "pretty"/"press", "open" inside "opened") and can turn casual text into
    # a contribution run.
    contribution_words = (
        "contrib",
        "contribution",
        "contributions",
        "pull request",
        "pr",
        "prs",
    )
    action_words = (
        "create",
        "run",
        "make",
        "find",
        "open",
        "submit",
    )
    if any(_has_word(normalized, word) for word in contribution_words) and any(
        _has_word(normalized, word) for word in action_words
    ):
        return True
    if _extract_repo(normalized) and _matches_any(
        normalized,
        "fix bug",
        "bugfix",
        "fix issue",
        "update deps",
        "update dependency",
        "update dependencies",
        "bump deps",
        "upgrade deps",
    ):
        return True
    return False


def _matches_any(text: str, *phrases: str) -> bool:
    return any(phrase in text for phrase in phrases)


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None
