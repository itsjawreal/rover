#!/usr/bin/env python3
"""Natural subcommand interface for the contribution engine.

  rover                     # status dashboard
  rover setup               # guided install for the current platform
  rover uninstall           # guided uninstall/reset for the current platform
  rover profile             # show active GitHub login identity
  rover doctor              # check setup
  rover doctor --verbose    # show full paths and raw checks
  rover run [N]             # search and submit N PRs (default 1)
  rover check               # poll open PR statuses
  rover report              # show run history
  rover respond             # handle maintainer comments
  rover list-prs            # all submitted PRs
  rover list-prs open       # filter: open | merged | closed
  rover inspect owner/repo  # analyze a repo without submitting
  rover scan owner/repo     # deterministic bug/security scan
  rover owner/repo          # target a specific repo
  rover --help              # full flag reference
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

def _looks_like_repo_root(path: Path) -> bool:
    return (
        (path / "app").is_dir()
        and (path / "src").is_dir()
        and (path / "scripts").is_dir()
    )


def _discover_root() -> Path:
    candidates: list[Path] = []

    try:
        cwd = Path.cwd().resolve()
        candidates.extend([cwd, *cwd.parents])
    except OSError:
        pass

    module_root = Path(__file__).resolve().parent.parent
    candidates.extend([module_root, *module_root.parents])

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _looks_like_repo_root(candidate):
            return candidate
    return module_root


ROOT = _discover_root()
sys.path.insert(0, str(ROOT))


def _wsl_unc_info(path: Path) -> tuple[str, str] | None:
    raw = str(path)
    if not raw.startswith("\\\\wsl.localhost\\"):
        return None
    parts = [part for part in raw.split("\\") if part]
    if len(parts) < 3 or parts[0].lower() != "wsl.localhost":
        return None
    distro = parts[1]
    linux_path = "/" + "/".join(parts[2:])
    return distro, linux_path

from src.core.cli_ui import (
    print_banner,
    print_section,
    print_ok,
    print_warn,
    print_err,
    print_item,
    print_blank,
    print_styled_doctor,
    print_styled_help,
    print_styled_prs,
    print_styled_report,
    print_status_dashboard,
)

_SUBCOMMAND_MAP: dict[str, str] = {
    "profile":  "--profile",
    "doctor":   "--doctor",
    "check":    "--contrib-check",
    "respond":  "--contrib-respond",
    "inspect":  "--repo-inspect",
    "scan":     "--scan-repo",
    "list-prs": "--list-prs",
}

_VALID_STATUSES = {"all", "open", "merged", "closed"}


def _normalize_scan_args(args: list[str]) -> list[str]:
    normalized: list[str] = []
    i = 0
    while i < len(args):
        part = args[i]
        if part == "--kind":
            normalized.append("--scan-kind")
        else:
            normalized.append(part)
        i += 1
    return normalized


def _normalize_repo_ref(value: str) -> str | None:
    value = value.strip()
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", value):
        return value
    try:
        parsed = urlparse(value)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != "github.com":
        return None
    path = parsed.path.strip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None
    candidate = f"{parts[0]}/{parts[1]}"
    if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", candidate):
        return None
    return candidate


def _run_builder(argv: list[str]) -> None:
    from app.builder import main as _main
    _main(argv)


def _run_in_wsl(argv: list[str], wsl_info: tuple[str, str]) -> None:
    distro, linux_root = wsl_info
    command = f"cd {shlex.quote(linux_root)} && python3 -m app.contribute"
    if argv:
        command = f"{command} {shlex.join(argv)}"
    try:
        subprocess.run(
            ["wsl.exe", "-d", distro, "bash", "-lc", command],
            check=False,
        )
    except KeyboardInterrupt:
        print_blank()
        print_warn("command interrupted by user")


def _run_platform_script(kind: str) -> None:
    wsl_info = _wsl_unc_info(ROOT)
    use_windows_script = os.name == "nt" and wsl_info is None
    script_name = f"{kind}_windows.ps1" if use_windows_script else f"{kind}_vps.sh"
    script = ROOT / "scripts" / script_name
    if not script.exists():
        print_banner()
        print_err(f"{kind} script not found: {script}")
        return

    if use_windows_script:
        try:
            subprocess.run(
                ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                check=False,
            )
        except KeyboardInterrupt:
            print_blank()
            print_warn(f"{kind} interrupted by user")
        return

    if os.name == "nt" and wsl_info is not None:
        distro, linux_root = wsl_info
        linux_script = f"{linux_root}/scripts/{script_name}"
        try:
            subprocess.run(["wsl.exe", "-d", distro, "bash", linux_script], check=False)
        except KeyboardInterrupt:
            print_blank()
            print_warn(f"{kind} interrupted by user")
        return

    os.execvp("bash", ["bash", str(script)])


def _cmd_doctor(verbose: bool = False) -> None:
    from src.core.doctor import collect_doctor_checks
    print_styled_doctor(collect_doctor_checks(), verbose=verbose)


def _cmd_list_prs(status_filter: str) -> None:
    if status_filter not in _VALID_STATUSES:
        print_err(f"Invalid status '{status_filter}'. Choose: all, open, merged, closed")
        return
    from src.contrib.contribution_store import ContributionStore
    rows = ContributionStore().list_pull_requests(
        limit=100,
        status_filter=None if status_filter == "all" else status_filter,
    )
    print_styled_prs(rows, status_filter)


def _cmd_report() -> None:
    from src.contrib.pr_generator import get_contribution_report_data
    summaries, queued = get_contribution_report_data(limit=5)
    print_styled_report(summaries, queued)


def main() -> None:
    argv = sys.argv[1:]
    wsl_info = _wsl_unc_info(ROOT)
    json_mode = "--json" in argv

    if os.name == "nt" and wsl_info is not None and (not argv or argv[0] not in {"setup", "uninstall"}):
        _run_in_wsl(argv, wsl_info)
        return

    # Styled help
    if argv and argv[0] in {"help", "--help", "-h"}:
        print_banner()
        print_styled_help()
        return

    # Pass-through: --version goes directly to builder
    if argv and argv[0] in {"--version", "-V"}:
        _run_builder(argv)
        return

    # No args → status dashboard
    if not argv:
        if json_mode:
            print_err("`rover --json` requires an explicit command")
            return
        print_banner()
        print_status_dashboard()
        return

    first = argv[0]

    if json_mode:
        if first == "profile":
            _run_builder(["--profile", "--json"])
            return
        if first == "doctor":
            _run_builder(["--doctor", "--json"])
            return
        if first == "report":
            _run_builder(["--contrib-report", "--json"])
            return
        if first == "check":
            _run_builder(["--contrib-check", "--json"])
            return
        if first == "respond":
            _run_builder(["--contrib-respond", "--json"])
            return
        if first == "inspect":
            _run_builder(["--repo-inspect", *argv[1:]])
            return
        if first == "scan":
            rest = _normalize_scan_args([arg for arg in argv[1:] if arg != "--json"])
            _run_builder(["--scan-repo", *rest, "--json"])
            return
        if first == "run":
            _GOAL_ALIASES = {"upgrade": "feature_upgrade", "add": "feature_add", "bug": "bugfix"}
            rest = [arg for arg in argv[1:] if arg != "--json"]
            n = "1"
            repo: str | None = None
            flags: list[str] = ["--json"]
            i = 0
            while i < len(rest):
                part = rest[i]
                if part.isdigit():
                    n = part
                elif part == "--goal" and i + 1 < len(rest):
                    goal = rest[i + 1]
                    flags += ["--goal", _GOAL_ALIASES.get(goal, goal)]
                    i += 1
                else:
                    normalized_repo = _normalize_repo_ref(part)
                    if normalized_repo:
                        repo = normalized_repo
                    else:
                        flags.append(part)
                i += 1
            contrib_arg = ["--contrib", repo] if repo else ["--contrib"]
            _run_builder([*contrib_arg, f"--{n}", *flags])
            return
        normalized_first = _normalize_repo_ref(first)
        if normalized_first:
            _run_builder(["--contrib", normalized_first, "--json", *[arg for arg in argv[1:] if arg != "--json"]])
            return

    # doctor → styled output
    if first == "doctor":
        print_banner()
        rest = argv[1:]
        verbose = False
        if rest:
            verbose_flags = {"--verbose", "-v"}
            if any(arg not in verbose_flags for arg in rest):
                print_err("Invalid doctor args. Use: rover doctor [--verbose]")
                return
            verbose = True
        _cmd_doctor(verbose=verbose)
        return

    # list-prs [status]
    if first == "list-prs":
        status = argv[1] if len(argv) > 1 else "all"
        print_banner()
        _cmd_list_prs(status)
        return

    # report → styled output
    if first == "report":
        print_banner()
        _cmd_report()
        return

    # setup → run install script
    if first == "setup":
        _run_platform_script("install")
        return

    # uninstall → run uninstall/reset script
    if first == "uninstall":
        _run_platform_script("uninstall")
        return

    # run [N] [owner/repo] [flags...] → search-mode or targeted contrib
    # e.g. rover run 3 --goal upgrade --dry-run
    #      rover run --goal add owner/repo
    if first == "run":
        _GOAL_ALIASES = {
            "upgrade": "feature_upgrade",
            "add":     "feature_add",
            "bug":     "bugfix",
        }
        rest = argv[1:]
        n = "1"
        repo: str | None = None
        flags: list[str] = []
        i = 0
        while i < len(rest):
            part = rest[i]
            if part.isdigit():
                n = part
            elif part == "--goal" and i + 1 < len(rest):
                goal = rest[i + 1]
                flags += ["--goal", _GOAL_ALIASES.get(goal, goal)]
                i += 1
            else:
                normalized_repo = _normalize_repo_ref(part)
                if normalized_repo:
                    repo = normalized_repo
                else:
                    flags.append(part)
            i += 1
        print_banner()
        contrib_arg = ["--contrib", repo] if repo else ["--contrib"]
        _run_builder([*contrib_arg, f"--{n}", *flags])
        return

    # check / respond / inspect → banner + delegate
    if first in _SUBCOMMAND_MAP:
        flag = _SUBCOMMAND_MAP[first]
        rest = _normalize_scan_args(argv[1:]) if first == "scan" else argv[1:]
        print_banner()
        _run_builder([flag, *rest])
        return

    # owner/repo shorthand → targeted contrib
    normalized_first = _normalize_repo_ref(first)
    if normalized_first:
        print_banner()
        _run_builder(["--contrib", normalized_first, *argv[1:]])
        return

    # fallthrough: pass everything as-is (supports --flags directly)
    _run_builder(argv)


if __name__ == "__main__":
    main()
