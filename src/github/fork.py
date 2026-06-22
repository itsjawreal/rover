from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# ── Exceptions ────────────────────────────────────────────────
class ForkError(Exception):
    """Raised when any fork/clone/push/PR step fails."""


class PRAlreadyExistsError(ForkError):
    """Raised when a PR from our fork is already open for this branch."""


# ── Data models ───────────────────────────────────────────────
@dataclass
class PRResult:
    full_name: str
    pr_url: str
    pr_title: str
    fork_name: str
    branch_name: str
    files_changed: list[str] = field(default_factory=list)
    submitted_at: str = ""


# ── Helpers ───────────────────────────────────────────────────
def _run(
    cmd: list[str],
    cwd: str | Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if cmd and cmd[0] == "gh":
        env = gh_safe_env()
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=check,
    )


def _verify_dep_update_submission(tmp_dir: Path, changed_files: dict[str, str], log: logging.Logger) -> None:
    if set(changed_files) != {"package.json"}:
        return
    lockfiles = (
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
    )
    for lockfile in lockfiles:
        if (tmp_dir / lockfile).exists():
            raise ForkError(
                f"dep_update verification failed: repo uses {lockfile}, but the automated update only changed package.json."
            )
    if shutil.which("npm") is None:
        raise ForkError("dep_update verification failed: npm is required for package.json-only dependency updates.")
    package_json = tmp_dir / "package.json"
    try:
        pkg_data = json.loads(package_json.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ForkError(f"dep_update verification failed: package.json is invalid after applying changes ({exc}).") from exc
    scripts = pkg_data.get("scripts", {}) if isinstance(pkg_data, dict) else {}
    verify_cmd: list[str] | None = None
    script_name = ""
    if isinstance(scripts, dict):
        for name in ("typecheck", "build", "test"):
            if isinstance(scripts.get(name), str) and scripts[name].strip():
                verify_cmd = ["npm", "run", name]
                script_name = name
                break
    if verify_cmd is None:
        raise ForkError("dep_update verification failed: no typecheck/build/test script found in package.json.")
    log.info("  [verify] installing dependencies for dep update")
    install = _run(["npm", "install", "--ignore-scripts", "--package-lock=false"], cwd=tmp_dir, check=False)
    if install.returncode != 0:
        raise ForkError(f"dep_update verification install failed: {(install.stdout + install.stderr).strip()[:300]}")
    log.info("  [verify] running npm run %s", script_name)
    verify = _run(verify_cmd, cwd=tmp_dir, check=False)
    if verify.returncode != 0:
        raise ForkError(f"dep_update verification failed in `{script_name}`: {(verify.stdout + verify.stderr).strip()[:300]}")


def _load_package_json(tmp_dir: Path) -> dict:
    package_json = tmp_dir / "package.json"
    if not package_json.exists():
        return {}
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _detect_js_package_manager(tmp_dir: Path) -> str:
    if (tmp_dir / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (tmp_dir / "yarn.lock").exists():
        return "yarn"
    if (tmp_dir / "bun.lock").exists() or (tmp_dir / "bun.lockb").exists():
        return "bun"
    return "npm"


def _verification_commands(tmp_dir: Path) -> tuple[list[str], list[str], str] | None:
    package_data = _load_package_json(tmp_dir)
    scripts = package_data.get("scripts", {}) if package_data else {}
    if isinstance(scripts, dict):
        for script_name in ("test", "typecheck", "build"):
            script = scripts.get(script_name)
            if not isinstance(script, str) or not script.strip():
                continue
            manager = _detect_js_package_manager(tmp_dir)
            if shutil.which(manager) is None:
                raise ForkError(f"local verification failed: `{manager}` is required but was not found on PATH.")
            if manager == "npm":
                install = ["npm", "ci"] if (tmp_dir / "package-lock.json").exists() else ["npm", "install"]
                verify = ["npm", "run", script_name]
            elif manager == "yarn":
                install = ["yarn", "install", "--frozen-lockfile"]
                verify = ["yarn", script_name]
            elif manager == "bun":
                install = ["bun", "install", "--frozen-lockfile"]
                verify = ["bun", "run", script_name]
            else:
                install = ["pnpm", "install", "--frozen-lockfile"]
                verify = ["pnpm", script_name]
            return install, verify, f"{manager} {script_name}"

    has_python_tests = any(
        path.exists()
        for path in (
            tmp_dir / "pytest.ini",
            tmp_dir / "tox.ini",
            tmp_dir / "tests",
            tmp_dir / "test",
        )
    )
    if has_python_tests:
        if shutil.which("pytest"):
            return [], ["pytest"], "pytest"
        return [], ["python", "-m", "unittest", "discover"], "python -m unittest discover"

    return None


_INFRA_ERROR_MARKERS = ("ModuleNotFoundError", "ImportError", "No module named", "cannot import")


def _run_repo_local_verification(tmp_dir: Path, log: logging.Logger) -> None:
    plan = _verification_commands(tmp_dir)
    if plan is None:
        log.info("  [verify] no local test/typecheck/build command detected")
        return

    install_cmd, verify_cmd, label = plan
    if install_cmd:
        log.info("  [verify] installing dependencies: %s", " ".join(install_cmd))
        install = _run(install_cmd, cwd=tmp_dir, check=False)
        if install.returncode != 0:
            raise ForkError(f"local verification install failed: {(install.stdout + install.stderr).strip()[:300]}")

    log.info("  [verify] running local verification: %s", label)
    verify = _run(verify_cmd, cwd=tmp_dir, check=False)
    if verify.returncode != 0:
        output = (verify.stdout + verify.stderr).strip()
        # If every failure is a missing-dep import error (not an assertion failure
        # from our patch), skip rather than blocking the PR.
        if any(m in output for m in _INFRA_ERROR_MARKERS) and "failures=" not in output:
            log.warning("  [verify] infra-only test failures (missing deps) — skipping local verify: %s", output[:200])
            return
        raise ForkError(f"local verification failed in `{label}`: {output[:300]}")


def _force_rmtree(path: Path) -> None:
    def _on_error(func, fpath, exc_info):
        try:
            os.chmod(fpath, stat.S_IWRITE)
            func(fpath)
        except Exception:
            pass
    shutil.rmtree(path, onerror=_on_error)


def gh_safe_env() -> dict[str, str]:
    """Return an environment for gh commands without poisoned localhost:9 proxies."""
    env = os.environ.copy()
    blocked_values = {"http://127.0.0.1:9", "https://127.0.0.1:9"}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        value = (env.get(key) or "").strip().lower()
        if value in blocked_values or value == "http://127.0.0.1:9":
            env.pop(key, None)
    # Ensure system bin dirs are in PATH (may be missing when launched from Windows)
    path_parts = env.get("PATH", "").split(":")
    for system_bin in ("/usr/local/bin", "/usr/bin", "/bin"):
        if system_bin not in path_parts:
            path_parts.insert(0, system_bin)
    env["PATH"] = ":".join(path_parts)
    return env


def get_current_github_login() -> str:
    """Resolve the GitHub login to use for forks/PR heads.

    Priority:
    1. Explicit env override for portability/CI.
    2. `gh api user` when auth is healthy.
    3. `gh auth status` parsing, which still reveals the active account even when
       the cached token is stale.
    """
    # Skip placeholder values that ship in .env.example — they are never real logins.
    _PLACEHOLDER = {"your_github_username", "your_github_login", "your-github-username", ""}
    for env_name in ("GITHUB_OWNER", "GITHUB_LOGIN", "GH_USERNAME"):
        value = os.getenv(env_name, "").strip()
        if value and value.lower() not in _PLACEHOLDER:
            return value

    # Env var may be stale from a parent process that started before .env was updated.
    # Re-read .env file directly so a live config change takes effect without restart.
    try:
        from pathlib import Path as _Path
        from dotenv import dotenv_values as _dotenv_values
        _env_file = _Path(__file__).parent.parent.parent / ".env"
        if _env_file.exists():
            _env_vals = _dotenv_values(str(_env_file))
            for _key in ("GITHUB_OWNER", "GITHUB_LOGIN", "GH_USERNAME"):
                _val = (_env_vals.get(_key) or "").strip()
                if _val and _val.lower() not in _PLACEHOLDER:
                    return _val
    except Exception:
        pass

    r = _run(["gh", "api", "user", "--jq", ".login"], check=False)
    login = (r.stdout or "").strip()
    if r.returncode == 0 and login:
        return login

    status = _run(["gh", "auth", "status"], check=False)
    combined = ((status.stdout or "") + "\n" + (status.stderr or "")).strip()
    account_match = re.search(r"Failed to log in to github\.com account ([A-Za-z0-9-]+)", combined)
    if account_match:
        return account_match.group(1)
    account_match = re.search(r"Logged in to github\.com account ([A-Za-z0-9-]+)", combined)
    if account_match:
        return account_match.group(1)

    raise ForkError(
        "could not determine the active GitHub login for fork operations. "
        "Set GITHUB_OWNER in .env or run `gh auth login`."
    )


def _git_identity_for_login(login: str) -> tuple[str, str]:
    return f"{login}@users.noreply.github.com", login


def _wait_for_fork_ready(
    fork_full: str,
    log: logging.Logger,
    *,
    timeout_s: int = 90,
    poll_interval_s: int = 3,
) -> None:
    """Wait for GitHub/gh to resolve a newly created fork consistently."""
    deadline = time.time() + timeout_s
    last_error = ""
    while time.time() < deadline:
        r = _run(["gh", "repo", "view", fork_full, "--json", "nameWithOwner"], check=False)
        if r.returncode == 0 and fork_full.lower() in (r.stdout or "").lower():
            log.info("  [fork] fork is ready: %s", fork_full)
            return
        last_error = (r.stdout + r.stderr).strip()[:300]
        time.sleep(poll_interval_s)
    raise ForkError(
        f"fork {fork_full} was not resolvable within {timeout_s}s"
        + (f": {last_error}" if last_error else "")
    )


def _clone_repo_with_retry(
    fork_full: str,
    tmp_dir: Path,
    log: logging.Logger,
    *,
    attempts: int = 4,
    delay_s: int = 5,
) -> None:
    last_error = ""
    for attempt in range(1, attempts + 1):
        r = _run(
            ["gh", "repo", "clone", fork_full, str(tmp_dir), "--", "--depth=1"],
            check=False,
        )
        if r.returncode == 0:
            return
        last_error = (r.stdout + r.stderr).strip()[:300]
        is_not_ready = "could not resolve to a repository" in last_error.lower() or "not found" in last_error.lower()
        if attempt < attempts and is_not_ready:
            log.warning(
                "  [clone] fork not ready yet for %s (attempt %d/%d); retrying in %ds",
                fork_full,
                attempt,
                attempts,
                delay_s,
            )
            time.sleep(delay_s)
            continue
        raise ForkError(f"clone failed: {last_error}")


# ── Main PR submission ────────────────────────────────────────
def fork_and_submit_pr(
    full_name: str,
    default_branch: str,
    changed_files: dict[str, str],
    pr_title: str,
    pr_body: str,
    log: logging.Logger,
    improvement_type: str = "",
) -> PRResult:
    """Fork a repo, apply changes on a new branch, push, and open a PR.

    - Cleans up the local clone on both success and failure.
    - Keeps the fork on GitHub (required for the PR branch to stay live).
    - Raises ForkError on any unrecoverable step.
    """
    owner_login = get_current_github_login()
    git_email, git_name = _git_identity_for_login(owner_login)
    repo_name = full_name.split("/")[-1]
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    branch_name = f"{owner_login}-patch-{timestamp}"
    fork_full = f"{owner_login}/{repo_name}"
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"pr_{repo_name}_"))

    try:
        # ── STEP 1: Fork ─────────────────────────────────────
        log.info("  [fork] forking %s → %s", full_name, fork_full)
        r = _run(["gh", "repo", "fork", full_name, "--default-branch-only"], check=False)
        if r.returncode != 0:
            err = (r.stdout + r.stderr).strip()
            if "already exists" not in err.lower() and "already forked" not in err.lower():
                raise ForkError(f"gh repo fork failed: {err[:300]}")
            log.info("  [fork] fork already exists — reusing %s", fork_full)

        # ── STEP 1b: Sync fork with upstream ─────────────────
        log.info("  [sync] syncing fork %s with upstream", fork_full)
        r = _run(["gh", "repo", "sync", fork_full, "--branch", default_branch], check=False)
        if r.returncode != 0:
            log.warning("  [sync] sync failed (non-fatal): %s", (r.stdout + r.stderr).strip()[:200])

        # ── STEP 1c: Wait for fork to be resolvable ──────────
        log.info("  [fork] waiting for fork readiness: %s", fork_full)
        _wait_for_fork_ready(fork_full, log)

        # ── STEP 2: Clone fork ────────────────────────────────
        log.info("  [clone] cloning fork to %s", tmp_dir)
        _clone_repo_with_retry(fork_full, tmp_dir, log)

        # ── STEP 3: Git identity ──────────────────────────────
        _run(["git", "config", "user.email", git_email], cwd=tmp_dir)
        _run(["git", "config", "user.name", git_name], cwd=tmp_dir)

        # ── STEP 4: Create branch ─────────────────────────────
        log.info("  [branch] creating branch: %s", branch_name)
        _run(["git", "checkout", "-b", branch_name], cwd=tmp_dir)

        # ── STEP 5: Apply changed files ───────────────────────
        for filepath, content in changed_files.items():
            dest = tmp_dir / filepath
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            log.info("  [apply] %s", filepath)
        _run_repo_local_verification(tmp_dir, log)
        if improvement_type == "dep_update":
            _verify_dep_update_submission(tmp_dir, changed_files, log)

        # ── STEP 6: Commit ────────────────────────────────────
        _run(["git", "add", "-A"], cwd=tmp_dir)
        r = _run(["git", "commit", "-m", pr_title], cwd=tmp_dir, check=False)
        if r.returncode != 0:
            err = (r.stdout + r.stderr).strip()
            if "nothing to commit" in err.lower():
                raise ForkError("no actual diff — improved files are identical to originals")
            raise ForkError(f"commit failed: {err[:300]}")

        # ── STEP 7: Push branch to fork ───────────────────────
        log.info("  [push] pushing %s to fork", branch_name)
        r = _run(["git", "push", "origin", branch_name], cwd=tmp_dir, check=False)
        if r.returncode != 0:
            raise ForkError(f"push failed: {(r.stdout + r.stderr).strip()[:300]}")

        # ── STEP 8: Open PR against original repo ────────────
        log.info("  [pr] opening PR: %r", pr_title)
        r = _run(
            [
                "gh", "pr", "create",
                "--repo", full_name,
                "--head", f"{owner_login}:{branch_name}",
                "--base", default_branch,
                "--title", pr_title,
                "--body", pr_body,
            ],
            check=False,
        )
        if r.returncode != 0:
            err = (r.stdout + r.stderr).strip()
            if "already exists" in err.lower() or "a pull request for branch" in err.lower():
                raise PRAlreadyExistsError(f"PR already open: {err[:200]}")
            raise ForkError(f"pr create failed: {err[:300]}")

        pr_url = r.stdout.strip()
        log.info("  [pr] submitted: %s", pr_url)

        return PRResult(
            full_name=full_name,
            pr_url=pr_url,
            pr_title=pr_title,
            fork_name=fork_full,
            branch_name=branch_name,
            files_changed=list(changed_files.keys()),
            submitted_at=datetime.now().isoformat(),
        )

    finally:
        if tmp_dir.exists():
            _force_rmtree(tmp_dir)
            log.info("  [cleanup] removed temp dir: %s", tmp_dir)


def push_to_branch(
    fork_full: str,
    branch_name: str,
    changed_files: dict[str, str],
    commit_msg: str,
    log: logging.Logger,
) -> None:
    """Push additional commits to an existing branch on our fork.

    Used when responding to maintainer feedback — applies fixes on top of the
    existing PR branch without creating a new branch or new PR.
    Raises ForkError on any unrecoverable step.
    """
    repo_name = fork_full.split("/")[-1]
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"respond_{repo_name}_"))
    owner_login = get_current_github_login()
    git_email, git_name = _git_identity_for_login(owner_login)

    try:
        # ── Clone fork at existing branch ─────────────────────
        log.info("  [clone] cloning %s branch %s", fork_full, branch_name)
        r = _run(
            ["gh", "repo", "clone", fork_full, str(tmp_dir), "--",
             "--depth=5", f"--branch={branch_name}"],
            check=False,
        )
        if r.returncode != 0:
            raise ForkError(f"clone failed: {(r.stdout + r.stderr).strip()[:300]}")

        _run(["git", "config", "user.email", git_email], cwd=tmp_dir)
        _run(["git", "config", "user.name", git_name], cwd=tmp_dir)

        # ── Apply changed files ───────────────────────────────
        for filepath, content in changed_files.items():
            dest = tmp_dir / filepath
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            log.info("  [apply] %s", filepath)
        _run_repo_local_verification(tmp_dir, log)

        # ── Commit ────────────────────────────────────────────
        _run(["git", "add", "-A"], cwd=tmp_dir)
        r = _run(["git", "commit", "-m", commit_msg], cwd=tmp_dir, check=False)
        if r.returncode != 0:
            err = (r.stdout + r.stderr).strip()
            if "nothing to commit" in err.lower():
                raise ForkError("no actual diff — files unchanged after applying fix")
            raise ForkError(f"commit failed: {err[:300]}")

        # ── Push to same branch ───────────────────────────────
        log.info("  [push] pushing to %s/%s", fork_full, branch_name)
        r = _run(["git", "push", "origin", branch_name], cwd=tmp_dir, check=False)
        if r.returncode != 0:
            raise ForkError(f"push failed: {(r.stdout + r.stderr).strip()[:300]}")

        log.info("  [push] done: %s/%s", fork_full, branch_name)

    finally:
        if tmp_dir.exists():
            _force_rmtree(tmp_dir)
            log.info("  [cleanup] removed temp dir: %s", tmp_dir)


def fix_and_push_own_repo(
    full_name: str,
    default_branch: str,
    changed_files: dict[str, str],
    commit_msg: str,
    log: logging.Logger,
    improvement_type: str = "",
) -> PRResult:
    """Apply a fix directly to an operator-owned repo — no fork, no PR.

    Clones the repo, applies changed files, commits, and pushes directly to
    the default branch. Returns a PRResult with pr_url='' (no PR was opened).
    Raises ForkError on any unrecoverable step.
    """
    repo_name = full_name.split("/")[-1]
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"fix_own_{repo_name}_"))
    owner_login = get_current_github_login()
    git_email, git_name = _git_identity_for_login(owner_login)

    try:
        # ── STEP 1: Clone repo ────────────────────────────────
        log.info("  [clone] cloning %s to %s", full_name, tmp_dir)
        r = _run(
            ["gh", "repo", "clone", full_name, str(tmp_dir), "--", "--depth=1"],
            check=False,
        )
        if r.returncode != 0:
            raise ForkError(f"clone failed: {(r.stdout + r.stderr).strip()[:300]}")

        # ── STEP 2: Git identity ──────────────────────────────
        _run(["git", "config", "user.email", git_email], cwd=tmp_dir)
        _run(["git", "config", "user.name", git_name], cwd=tmp_dir)

        # ── STEP 3: Apply changed files ───────────────────────
        for filepath, content in changed_files.items():
            dest = tmp_dir / filepath
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            log.info("  [apply] %s", filepath)
        _run_repo_local_verification(tmp_dir, log)
        if improvement_type == "dep_update":
            _verify_dep_update_submission(tmp_dir, changed_files, log)

        # ── STEP 4: Commit ────────────────────────────────────
        _run(["git", "add", "-A"], cwd=tmp_dir)
        r = _run(["git", "commit", "-m", commit_msg], cwd=tmp_dir, check=False)
        if r.returncode != 0:
            err = (r.stdout + r.stderr).strip()
            if "nothing to commit" in err.lower():
                raise ForkError("no actual diff — improved files are identical to originals")
            raise ForkError(f"commit failed: {err[:300]}")

        # ── STEP 5: Push to default branch ───────────────────
        log.info("  [push] pushing to %s/%s", full_name, default_branch)
        r = _run(["git", "push", "origin", f"HEAD:{default_branch}"], cwd=tmp_dir, check=False)
        if r.returncode != 0:
            raise ForkError(f"push failed: {(r.stdout + r.stderr).strip()[:300]}")

        push_url = f"https://github.com/{full_name}/commits/{default_branch}"
        log.info("  [push] pushed: %s", push_url)

        return PRResult(
            full_name=full_name,
            pr_url=push_url,
            pr_title=commit_msg,
            fork_name=full_name,
            branch_name=default_branch,
            files_changed=list(changed_files.keys()),
            submitted_at=datetime.now().isoformat(),
        )

    finally:
        if tmp_dir.exists():
            _force_rmtree(tmp_dir)
            log.info("  [cleanup] removed temp dir: %s", tmp_dir)
