from __future__ import annotations

import base64
import io
import logging
import os
import json
import shutil
import subprocess
import time
import zipfile
from http.client import RemoteDisconnected
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

import requests

from src.core.github_auth import resolve_github_token

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────
_GITHUB_API = "https://api.github.com"
_ALLOWED_LICENSES = {"mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "unlicense"}
_MAX_REPO_FILES = int(os.getenv("CONTRIB_MAX_REPO_FILES", "120"))
_MAX_FILE_BYTES = int(os.getenv("CONTRIB_MAX_FILE_BYTES", "500000"))
_SKIP_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp4",
    ".zip",
    ".tar",
    ".gz",
    ".pyc",
    ".pyo",
    ".so",
    ".dll",
    ".exe",
}
_SKIP_DIRS = {
    "__pycache__",
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
    ".tox",
    "dist",
    "build",
    ".eggs",
}


# ── Data models ──────────────────────────────────────────────
@dataclass
class RepoCandidate:
    name: str
    full_name: str
    description: str
    stars: int
    forks: int
    license: str
    url: str
    default_branch: str
    pushed_days_ago: int
    topics: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    archived: bool = False
    disabled: bool = False
    maintainer_signals: dict = field(default_factory=dict)
    file_git_context: dict[str, str] = field(default_factory=dict)


class ScraperError(Exception):
    """Raised when GitHub API or source download fails."""


# ── GitHub API ───────────────────────────────────────────────
def _gh_headers() -> dict[str, str]:
    token = resolve_github_token()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _gh_cli_available() -> bool:
    return shutil.which("gh") is not None


def _request_proxies() -> dict[str, str]:
    blocked_proxy = "http://127.0.0.1:9"
    candidates = {
        "http": os.getenv("HTTP_PROXY", "").strip(),
        "https": os.getenv("HTTPS_PROXY", "").strip(),
    }
    return {
        scheme: value
        for scheme, value in candidates.items()
        if value and value.lower() != blocked_proxy
    }


def _http_get(url: str, **kwargs) -> requests.Response:
    session = requests.Session()
    session.trust_env = False
    try:
        return session.get(url, **kwargs)
    finally:
        session.close()


def _wait_for_rate_limit(resp: requests.Response, *, has_auth: bool) -> None:
    reset_ts = int(resp.headers.get("X-RateLimit-Reset", 0))
    now = int(time.time())
    wait_secs = max(10, (reset_ts - now) + 5) if reset_ts else 3600
    max_wait = int(os.getenv("GITHUB_RATE_LIMIT_MAX_WAIT_SECS", "30"))
    if not has_auth:
        raise ScraperError(
            "GitHub API rate limit hit without a usable token. Use GH_TOKEN/GITHUB_TOKEN or a working `gh auth login` session."
        )
    if wait_secs > max_wait:
        raise ScraperError(
            f"GitHub API rate limit would require waiting about {wait_secs}s, exceeding the interactive cap of {max_wait}s."
        )
    log.warning("GitHub rate limit hit; waiting %ds for reset", wait_secs)
    time.sleep(wait_secs)


def _gh_api_endpoint(url: str, params: dict | None = None) -> str:
    parsed = urlparse(url)
    path = parsed.path.lstrip("/")
    query_parts: list[tuple[str, str]] = []
    if parsed.query:
        # Preserve any query already present on the URL.
        query_parts.extend(
            [tuple(part.split("=", 1)) if "=" in part else (part, "") for part in parsed.query.split("&") if part]
        )
    if params:
        for key, value in params.items():
            query_parts.append((str(key), str(value)))
    if query_parts:
        return f"{path}?{urlencode(query_parts, doseq=True)}"
    return path


def _gh_get_via_cli(url: str, params: dict | None = None, timeout: int = 20) -> Any:
    endpoint = _gh_api_endpoint(url, params)
    result = subprocess.run(
        ["gh", "api", endpoint],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise ScraperError(f"GitHub CLI API error: {stderr[:200]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ScraperError(f"GitHub CLI returned invalid JSON for {endpoint}") from exc


def _retry_github_api(url: str, params: dict | None, timeout: int, retry: int, reason: str) -> Any:
    if retry < 2:
        wait = 5 * (2 ** retry)
        log.warning("GitHub API transient connection error; retry %d/2 in %ds: %s", retry + 1, wait, reason)
        time.sleep(wait)
        return _gh_get(url, params, min(timeout * 2, 60), retry + 1)
    raise ScraperError(f"GitHub API connection failed after retries: {reason}")


def _gh_get(url: str, params: dict | None = None, timeout: int = 15, _retry: int = 0) -> Any:
    token = resolve_github_token()
    if not token and _gh_cli_available():
        return _gh_get_via_cli(url, params=params, timeout=timeout)
    try:
        resp = _http_get(
            url,
            headers=_gh_headers(),
            params=params,
            timeout=timeout,
            proxies=_request_proxies(),
        )
    except requests.exceptions.Timeout as exc:
        return _retry_github_api(url, params, timeout, _retry, f"timeout: {exc}")
    except (
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
        RemoteDisconnected,
    ) as exc:
        return _retry_github_api(url, params, timeout, _retry, str(exc))

    if resp.status_code in (403, 429):
        body = resp.text.lower()
        if "rate limit" in body or "secondary rate" in body or resp.status_code == 429:
            _wait_for_rate_limit(resp, has_auth=bool(token))
            if _retry < 2:
                return _gh_get(url, params, timeout, _retry + 1)
        raise ScraperError(f"GitHub API auth error ({resp.status_code}): {resp.text[:200]}")
    if resp.status_code == 404:
        raise ScraperError(f"GitHub API 404: {url}")
    resp.raise_for_status()
    return resp.json()


def _get_license(repo: dict) -> str:
    lic = repo.get("license") or {}
    return (lic.get("spdx_id") or "").lower()


def _metadata_security_ok(candidate: RepoCandidate) -> bool:
    haystack = " ".join(
        [
            candidate.name,
            candidate.description,
            " ".join(candidate.topics),
        ]
    ).lower()
    suspicious = ("crack", "stealer", "malware", "phishing", "token grabber", "keylogger")
    return not any(marker in haystack for marker in suspicious)


# ── Source download ──────────────────────────────────────────
def _should_skip_path(path: str, size: int) -> bool:
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    if any(part in _SKIP_DIRS for part in parts):
        return True
    if Path(normalized).suffix.lower() in _SKIP_EXTENSIONS:
        return True
    return size > _MAX_FILE_BYTES


def _decode_blob(content: str) -> str:
    return base64.b64decode(content.encode("utf-8")).decode("utf-8", errors="replace")


# In-memory cache: (full_name, branch) → files dict.
# Survives across retry attempts within the same process — no re-download on failure.
_REPO_FILE_CACHE: dict[tuple[str, str], dict[str, str]] = {}


def _gh_download_zipball(full_name: str, ref: str, timeout: int = 90) -> bytes:
    """Download repo as a single ZIP via GitHub archive endpoint."""
    url = f"{_GITHUB_API}/repos/{full_name}/zipball/{ref}"
    token = resolve_github_token()
    resp = _http_get(
        url,
        headers=_gh_headers(),
        timeout=timeout,
        proxies=_request_proxies(),
        allow_redirects=True,
        stream=False,
    )
    if resp.status_code == 404:
        raise ScraperError(f"Zipball 404 for {full_name}@{ref}")
    resp.raise_for_status()
    return resp.content


def _extract_zipball(
    data: bytes,
    allowed_exts: tuple[str, ...],
    max_files: int,
) -> dict[str, str]:
    """Extract text files from a GitHub zipball (strips the top-level prefix dir)."""
    files: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for entry in zf.infolist():
            if entry.is_dir():
                continue
            # GitHub zips have a single top-level dir like "owner-repo-sha1234/"
            parts = entry.filename.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            path = parts[1]
            if not path.endswith(allowed_exts):
                continue
            if _should_skip_path(path, entry.file_size):
                continue
            if len(files) >= max_files:
                break
            try:
                files[path] = zf.read(entry).decode("utf-8", errors="replace")
            except Exception:
                continue
    return files


def download_repo_files(
    candidate: RepoCandidate,
    *,
    max_py: int | None = None,
    max_total: int | None = None,
    allowed_exts: tuple[str, ...] | None = None,
    tree: dict | None = None,
    status_cb: "Callable[[str], None] | None" = None,
) -> dict[str, str]:
    _ALLOWED_EXTS = allowed_exts or (".py", ".ts", ".tsx", ".json", ".toml", ".txt", ".md", ".yml", ".yaml")
    cache_key = (candidate.full_name, candidate.default_branch)

    # Return cached copy if available — retry attempts reuse the same files, no re-download.
    if cache_key in _REPO_FILE_CACHE and tree is None:
        log.info("Using cached files for %s (%d files)", candidate.full_name, len(_REPO_FILE_CACHE[cache_key]))
        cached = _REPO_FILE_CACHE[cache_key]
        # Still enforce max_py / max_total against cached counts
        if max_py is not None or max_total is not None:
            pre_py = sum(1 for p in cached if p.endswith(".py"))
            pre_total = len(cached)
            if max_py is not None and pre_py > max_py:
                raise ScraperError(f"Python surface too broad (py={pre_py} > {max_py} allowed).")
            if max_total is not None and pre_total > max_total:
                raise ScraperError(f"Repo too broad (total={pre_total} > {max_total} allowed).")
        return cached

    # Try zipball first: 1 HTTP request instead of N individual /contents calls.
    # Falls back to per-file API if zipball is unavailable or fails.
    if tree is None:
        try:
            if status_cb:
                status_cb(f"downloading {candidate.full_name} (zip)...")
            zip_data = _gh_download_zipball(candidate.full_name, candidate.default_branch)
            files = _extract_zipball(zip_data, _ALLOWED_EXTS, _MAX_REPO_FILES)

            if max_py is not None or max_total is not None:
                pre_py = sum(1 for p in files if p.endswith(".py"))
                pre_total = len(files)
                if max_py is not None and pre_py > max_py:
                    raise ScraperError(f"Python surface too broad (py={pre_py} > {max_py} allowed).")
                if max_total is not None and pre_total > max_total:
                    raise ScraperError(f"Repo too broad (total={pre_total} > {max_total} allowed).")

            log.info("Zipball download complete: %d files for %s", len(files), candidate.full_name)
            _REPO_FILE_CACHE[cache_key] = files
            return files
        except ScraperError:
            raise
        except Exception as exc:
            log.warning("Zipball failed for %s (%s) — falling back to per-file API", candidate.full_name, exc)

    # Per-file fallback: fetch tree then individual blobs
    if status_cb:
        status_cb(f"fetching file tree for {candidate.full_name}...")
    tree = tree or _gh_get(
        f"{_GITHUB_API}/repos/{candidate.full_name}/git/trees/{candidate.default_branch}",
        params={"recursive": "1"},
        timeout=30,
    )

    all_blobs = [
        item for item in tree.get("tree", [])
        if item.get("type") == "blob"
        and not _should_skip_path(item.get("path", ""), int(item.get("size") or 0))
        and item.get("path", "").endswith(_ALLOWED_EXTS)
    ]
    if max_py is not None or max_total is not None:
        pre_py = sum(1 for i in all_blobs if i.get("path", "").endswith(".py"))
        pre_total = len(all_blobs)
        if max_py is not None and pre_py > max_py:
            raise ScraperError(f"Python surface too broad — skipping download (py={pre_py} > {max_py} allowed).")
        if max_total is not None and pre_total > max_total:
            raise ScraperError(f"Repo too broad — skipping download (total={pre_total} > {max_total} allowed).")

    total_eligible = min(len(all_blobs), _MAX_REPO_FILES)
    if status_cb:
        status_cb(f"downloading files from {candidate.full_name} (0/{total_eligible})...")

    files = {}
    skipped = 0
    _STATUS_EVERY = 20
    for item in all_blobs:
        path = item.get("path", "")
        if len(files) >= _MAX_REPO_FILES:
            skipped += 1
            continue
        blob = _gh_get(f"{_GITHUB_API}/repos/{candidate.full_name}/contents/{path}", params={"ref": candidate.default_branch})
        encoded = blob.get("content", "")
        if blob.get("encoding") != "base64" or not encoded:
            skipped += 1
            continue
        files[path] = _decode_blob(encoded)
        if status_cb and len(files) % _STATUS_EVERY == 0:
            status_cb(f"downloading files from {candidate.full_name} ({len(files)}/{total_eligible})...")

    log.info("Download complete: %d files downloaded, %d skipped", len(files), skipped)
    _REPO_FILE_CACHE[cache_key] = files
    return files


_CONTRIBUTING_FILENAMES = ("CONTRIBUTING.md", "CONTRIBUTING.rst", "CONTRIBUTING")
_REQUIRES_TEST_MARKERS = ("test", "pytest", "unittest", "coverage", "spec")
_SMALL_DIFF_MARKERS = ("small", "minimal", "focused", "narrow", "single change", "one pr")


def fetch_maintainer_signals(candidate: RepoCandidate) -> dict:
    """Fetch CONTRIBUTING.md, issue labels, and open PR titles for a repo.

    Returns a dict with maintainer preference signals. Never raises — missing
    data results in empty/False values so the caller can proceed without it.
    """
    signals: dict = {
        "requires_tests": False,
        "prefers_small_diff": False,
        "active_community": False,
        "contributing_snippet": "",
        "open_pr_titles": [],
        "issue_labels": [],
    }

    for fname in _CONTRIBUTING_FILENAMES:
        try:
            data = _gh_get(
                f"{_GITHUB_API}/repos/{candidate.full_name}/contents/{fname}",
                timeout=10,
            )
            if data.get("encoding") == "base64" and data.get("content"):
                content = _decode_blob(data["content"])
                snippet = content[:3000]
                signals["contributing_snippet"] = snippet
                lower = snippet.lower()
                signals["requires_tests"] = any(m in lower for m in _REQUIRES_TEST_MARKERS)
                signals["prefers_small_diff"] = any(m in lower for m in _SMALL_DIFF_MARKERS)
                break
        except (ScraperError, Exception):
            continue

    try:
        labels = _gh_get(
            f"{_GITHUB_API}/repos/{candidate.full_name}/labels",
            params={"per_page": "30"},
            timeout=10,
        )
        if isinstance(labels, list):
            names = [label.get("name", "") for label in labels]
            signals["issue_labels"] = names
            signals["active_community"] = any(
                n in names for n in ("good first issue", "help wanted", "bug")
            )
    except (ScraperError, Exception):
        pass

    try:
        prs = _gh_get(
            f"{_GITHUB_API}/repos/{candidate.full_name}/pulls",
            params={"state": "open", "per_page": "20"},
            timeout=10,
        )
        if isinstance(prs, list):
            signals["open_pr_titles"] = [pr.get("title", "") for pr in prs]
    except (ScraperError, Exception):
        pass

    return signals


def fetch_file_git_history(
    candidate: RepoCandidate,
    file_path: str,
    max_commits: int = 10,
) -> list[dict]:
    """Fetch recent commit history for a specific file via GitHub API.

    Returns a list of raw commit dicts. Never raises — returns [] on any failure.
    """
    try:
        commits = _gh_get(
            f"{_GITHUB_API}/repos/{candidate.full_name}/commits",
            params={
                "path": file_path,
                "per_page": str(max_commits),
                "sha": candidate.default_branch,
            },
            timeout=15,
        )
        if isinstance(commits, list):
            return commits
        return []
    except (ScraperError, Exception):
        return []


def repo_from_api_payload(item: dict) -> RepoCandidate:
    pushed_days_ago = 999
    pushed_at = item.get("pushed_at", "")
    try:
        pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        pushed_days_ago = (datetime.now(timezone.utc) - pushed).days
    except Exception:
        pass

    return RepoCandidate(
        name=item["name"],
        full_name=item["full_name"],
        description=(item.get("description") or "")[:200],
        stars=item.get("stargazers_count", 0),
        forks=item.get("forks_count", 0),
        license=_get_license(item),
        url=item["html_url"],
        default_branch=item.get("default_branch", "main"),
        pushed_days_ago=pushed_days_ago,
        topics=item.get("topics", []),
        archived=bool(item.get("archived")),
        disabled=bool(item.get("disabled")),
    )
