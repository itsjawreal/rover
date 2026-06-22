from __future__ import annotations

import os
import shutil
import subprocess


def _env_token(name: str) -> str:
    return os.getenv(name, "").strip()


def resolve_github_token() -> str:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = _env_token(name)
        if token:
            return token

    if shutil.which("gh") is None:
        return ""

    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return ""

    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def github_auth_mode() -> str:
    if _env_token("GH_TOKEN"):
        return "gh-token-env"
    if _env_token("GITHUB_TOKEN"):
        return "github-token-env"
    if resolve_github_token():
        return "gh-auth"
    return "none"
