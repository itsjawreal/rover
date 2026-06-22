from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.core.config import RUNS_DIR


@dataclass
class CloneResult:
    repo_url: str
    checkout_path: Path
    cloned: bool
    note: str


def _repo_dir_name(repo_url: str) -> str:
    cleaned = repo_url.rstrip("/")
    parts = [part for part in cleaned.split("/") if part]
    slug = "-".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    slug = re.sub(r"\.git$", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", slug).strip("-")
    return slug or "repo"


def _is_valid_checkout(path: Path) -> bool:
    return path.exists() and path.is_dir() and (path / ".git").exists()


def clone_repository(repo_url: str, log: logging.Logger, workspace: Path | None = None) -> CloneResult:
    workspace_root = workspace or (RUNS_DIR / "workspaces")
    workspace_root.mkdir(parents=True, exist_ok=True)
    checkout_path = workspace_root / _repo_dir_name(repo_url)
    if _is_valid_checkout(checkout_path):
        return CloneResult(repo_url=repo_url, checkout_path=checkout_path, cloned=False, note="Existing checkout reused.")
    if checkout_path.exists():
        shutil.rmtree(checkout_path, ignore_errors=True)

    if shutil.which("git") is None:
        return CloneResult(repo_url=repo_url, checkout_path=checkout_path, cloned=False, note="Git is unavailable on this machine.")

    command = ["git", "clone", "--depth", "1", repo_url, str(checkout_path)]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        log.info("Cloned %s into %s", repo_url, checkout_path)
        return CloneResult(repo_url=repo_url, checkout_path=checkout_path, cloned=True, note="Shallow clone completed.")
    except Exception as exc:
        if checkout_path.exists():
            shutil.rmtree(checkout_path, ignore_errors=True)
        log.warning("Clone skipped for %s: %s", repo_url, exc)
        return CloneResult(repo_url=repo_url, checkout_path=checkout_path, cloned=False, note=f"Clone failed: {exc}")
