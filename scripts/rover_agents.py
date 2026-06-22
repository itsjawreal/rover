#!/usr/bin/env python3
"""
ROVER Agent Bridge

A safe deterministic CLI orchestrator that lets Claude and Codex "talk"
through ROVER using run folders, prompts, git diff, and test output.

Usage:
  python scripts/rover_agents.py start "continue existing code, fix the next safest issue"
  python scripts/rover_agents.py status
  python scripts/rover_agents.py show
  python scripts/rover_agents.py abort --rollback
  python scripts/rover_agents.py config-check

Design:
  - Claude = architect/reviewer
  - Codex = builder/fixer
  - ROVER = moderator, git guard, test runner, transcript keeper
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = ROOT / ".agents"
RUNS_DIR = ROOT / "runs" / "agent_sessions"
LATEST_FILE = RUNS_DIR / "LATEST"


# -----------------------------
# Small config loader
# -----------------------------

def _parse_scalar(value: str):
    v = value.strip()
    if v.lower() in ("true", "yes", "on"):
        return True
    if v.lower() in ("false", "no", "off"):
        return False
    if v.lower() in ("null", "none", "~"):
        return None
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    try:
        return int(v)
    except ValueError:
        return v


def tiny_yaml_load(path: Path) -> dict:
    """
    Tiny YAML subset reader so this script has no external dependencies.
    Supports:
      key: value
      key:
        child: value
        list:
          - item
    Good enough for .agents/*.yml included with this bridge.
    """
    if not path.exists():
        return {}

    root: dict = {}
    stack: List[Tuple[int, object]] = [(-1, root)]

    lines = path.read_text(encoding="utf-8").splitlines()
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue

        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if line.startswith("- "):
            item = _parse_scalar(line[2:])
            if isinstance(parent, list):
                parent.append(item)
            continue

        if ":" not in line:
            continue

        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()

        if val == "":
            # Determine whether this block should be a list by peeking ahead.
            # Simpler default: dict. If first child is list, later code converts
            # through explicit keys in included config.
            obj = {}
            if isinstance(parent, dict):
                parent[key] = obj
            stack.append((indent, obj))
        else:
            if isinstance(parent, dict):
                parent[key] = _parse_scalar(val)

    # Normalize known list blocks that tiny parser could not infer.
    # Re-parse protected_paths and default_tests manually.
    text = path.read_text(encoding="utf-8")
    for list_key in ("protected_paths", "default_tests", "allowed_extra_files"):
        m = re.search(rf"(?m)^{list_key}:\s*\n((?:\s+- .+\n?)*)", text)
        if m:
            root[list_key] = [line.strip()[2:].strip().strip('"').strip("'")
                              for line in m.group(1).splitlines()
                              if line.strip().startswith("- ")]
    return root


def read_file(path: Path, default: str = "") -> str:
    return path.read_text(encoding="utf-8") if path.exists() else default


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def now_id() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


# -----------------------------
# Command running
# -----------------------------

@dataclass
class CmdResult:
    code: int
    stdout: str
    stderr: str


def run_cmd(
    cmd: str,
    *,
    cwd: Path = ROOT,
    timeout: int = 900,
    input_text: Optional[str] = None,
) -> CmdResult:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")

    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd),
            shell=True,
            text=True,
            capture_output=True,
            input=input_text,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        return CmdResult(p.returncode, p.stdout or "", p.stderr or "")
    except subprocess.TimeoutExpired as e:
        return CmdResult(
            124,
            e.stdout or "",
            (e.stderr or "") + f"\n[TIMEOUT after {timeout}s]",
        )


def quote_arg(s: str) -> str:
    # Windows and POSIX safe enough when shell=True.
    return shlex.quote(s)


def shell_join(parts: List[str]) -> str:
    return " ".join(quote_arg(p) for p in parts)


# -----------------------------
# Git helpers
# -----------------------------

def git(args: str, timeout: int = 120) -> CmdResult:
    return run_cmd(f"git {args}", timeout=timeout)


def git_snapshot(run_dir: Path) -> None:
    write_file(run_dir / "git_status_initial.txt", git("status --short").stdout)
    write_file(run_dir / "git_branch.txt", git("branch --show-current").stdout.strip() + "\n")
    write_file(run_dir / "git_head.txt", git("rev-parse HEAD").stdout.strip() + "\n")


def git_diff() -> str:
    return git("diff --no-ext-diff", timeout=180).stdout


def git_status_short() -> str:
    return git("status --short", timeout=120).stdout


def list_changed_files() -> List[str]:
    out = git("diff --name-only", timeout=120).stdout
    return [x.strip() for x in out.splitlines() if x.strip()]


def list_deleted_files() -> List[str]:
    out = git("diff --name-status", timeout=120).stdout
    deleted = []
    for line in out.splitlines():
        if line.startswith("D\t"):
            deleted.append(line.split("\t", 1)[1])
    return deleted


# -----------------------------
# Policy / guard
# -----------------------------

@dataclass
class Policy:
    max_rounds: int = 3
    max_changed_files: int = 12
    max_deleted_files: int = 0
    max_runtime_minutes: int = 30
    require_tests: bool = True
    stop_on_large_diff: bool = True
    stop_on_env_change: bool = True
    protected_paths: Tuple[str, ...] = (
        ".git/",
        ".env",
        ".venv/",
        "venv/",
        "data/",
        "logs/",
        "runs/",
        "secrets/",
        "id_*",
        "*.pem",
        "*.key",
    )
    default_tests: Tuple[str, ...] = ("python -m pytest",)

    @classmethod
    def load(cls) -> "Policy":
        raw = tiny_yaml_load(AGENTS_DIR / "policy.yml")
        return cls(
            max_rounds=int(raw.get("max_rounds", cls.max_rounds)),
            max_changed_files=int(raw.get("max_changed_files", cls.max_changed_files)),
            max_deleted_files=int(raw.get("max_deleted_files", cls.max_deleted_files)),
            max_runtime_minutes=int(raw.get("max_runtime_minutes", cls.max_runtime_minutes)),
            require_tests=bool(raw.get("require_tests", cls.require_tests)),
            stop_on_large_diff=bool(raw.get("stop_on_large_diff", cls.stop_on_large_diff)),
            stop_on_env_change=bool(raw.get("stop_on_env_change", cls.stop_on_env_change)),
            protected_paths=tuple(raw.get("protected_paths", cls.protected_paths)),
            default_tests=tuple(raw.get("default_tests", cls.default_tests)),
        )


def path_matches(pattern: str, file_path: str) -> bool:
    from fnmatch import fnmatch
    p = file_path.replace("\\", "/")
    pat = pattern.replace("\\", "/")
    if pat.endswith("/"):
        return p.startswith(pat)
    return fnmatch(p, pat) or p == pat


def guard_check(policy: Policy) -> Tuple[bool, str]:
    changed = list_changed_files()
    deleted = list_deleted_files()
    problems = []

    if len(changed) > policy.max_changed_files:
        problems.append(f"Too many changed files: {len(changed)} > {policy.max_changed_files}")

    if len(deleted) > policy.max_deleted_files:
        problems.append(f"Deleted files detected: {len(deleted)} > {policy.max_deleted_files}: {deleted}")

    for f in changed:
        for pat in policy.protected_paths:
            if path_matches(pat, f):
                # Allow writing inside current run dir because ROVER itself does that,
                # but git normally ignores runs/ if configured.
                problems.append(f"Protected path touched: {f} matches {pat}")

    if problems:
        return False, "\n".join(f"- {p}" for p in problems)
    return True, "OK"


# -----------------------------
# Agent config
# -----------------------------

@dataclass
class AgentCommands:
    claude: str = "claude -p"
    codex: str = "codex"
    timeout_seconds: int = 900

    @classmethod
    def load(cls) -> "AgentCommands":
        raw = tiny_yaml_load(AGENTS_DIR / "commands.yml")
        claude = "claude -p"
        codex = "codex"
        timeout = 900
        if isinstance(raw.get("claude"), dict):
            claude = raw["claude"].get("command", claude)
        elif isinstance(raw.get("claude"), str):
            claude = raw.get("claude")
        if isinstance(raw.get("codex"), dict):
            codex = raw["codex"].get("command", codex)
        elif isinstance(raw.get("codex"), str):
            codex = raw.get("codex")
        timeout = int(raw.get("timeout_seconds", timeout))
        return cls(claude=claude, codex=codex, timeout_seconds=timeout)


def command_exists(cmdline: str) -> bool:
    first = shlex.split(cmdline, posix=os.name != "nt")[0] if cmdline.strip() else ""
    if not first:
        return False
    if os.name == "nt":
        r = run_cmd(f"where {first}", timeout=10)
    else:
        r = run_cmd(f"command -v {shlex.quote(first)}", timeout=10)
    return r.code == 0


# -----------------------------
# Prompt building
# -----------------------------

def role(name: str) -> str:
    return read_file(AGENTS_DIR / "roles" / name, "")


def compact_repo_context() -> str:
    tree_cmd = "git ls-files"
    files = git(tree_cmd, timeout=120).stdout.splitlines()
    selected = []
    for f in files:
        if f.startswith((".git/", ".venv/", "venv/", "data/", "logs/", "runs/")):
            continue
        selected.append(f)
    text = "\n".join(selected[:500])
    if len(selected) > 500:
        text += f"\n... truncated {len(selected) - 500} files"
    return text


def build_architect_prompt(goal: str, run_dir: Path, round_no: int) -> str:
    return f"""{role("claude_architect.md")}

# User goal
{goal}

# Round
{round_no}

# Repo file list
{compact_repo_context()}

# Existing notes
AGENTS.md:
{read_file(ROOT / "AGENTS.md")[:8000]}

CONTRIBUTING excerpt:
{read_file(ROOT / "CONTRIBUTING.md")[:4000]}

# Required output
Write a narrow plan only. Do not modify code.
Use exactly this format:

# PLAN

## Objective

## Files to modify
- path

## Steps
1.

## Tests
- command

## Stop conditions
-
"""


def build_codex_prompt(goal: str, plan: str, run_dir: Path, round_no: int) -> str:
    return f"""{role("codex_builder.md")}

# User goal
{goal}

# Round
{round_no}

# Claude plan
{plan}

# Hard rules
- Implement only the plan.
- Use minimal diffs.
- Do not modify .env, data, logs, runs, secrets, keys, or git metadata.
- Do not do broad refactors.
- Add or update tests when relevant.
- Run the tests listed in the plan if possible.
- At the end, summarize using the required format.

# Required final response format

# CODEX_RESULT

## Changed files
-

## What changed
-

## Tests run
-

## Result
PASS or FAIL

## Notes
-
"""


def build_review_prompt(goal: str, plan: str, codex_result: str, diff: str, tests: str, guard: str, round_no: int) -> str:
    # Limit diff size to avoid giant prompts; diff is also saved to file.
    max_diff = 40000
    shown_diff = diff[:max_diff]
    if len(diff) > max_diff:
        shown_diff += f"\n\n[DIFF TRUNCATED: total {len(diff)} chars. Review visible diff and request manual review if needed.]"

    return f"""{role("claude_reviewer.md")}

# User goal
{goal}

# Round
{round_no}

# Original plan
{plan}

# Codex result
{codex_result}

# Guard check
{guard}

# Test output
{tests[:20000]}

# Git diff
{shown_diff}

# Required output
Use exactly this format:

# REVIEW

Status: APPROVED or NEEDS_FIX

## Issues
-

## Required fixes
-

## Do not change
-
"""


def build_fix_prompt(goal: str, review: str, run_dir: Path, round_no: int) -> str:
    return f"""{role("codex_builder.md")}

# User goal
{goal}

# Claude review
{review}

# Task
Fix only the issues listed in Claude review.
Do not make unrelated changes.
Run relevant tests.

# Required final response format

# CODEX_FIX_RESULT

## Changed files
-

## Fixes
-

## Tests run
-

## Result
PASS or FAIL

## Notes
-
"""


# -----------------------------
# Agent calls
# -----------------------------

def call_agent(label: str, base_cmd: str, prompt: str, run_dir: Path, timeout: int) -> CmdResult:
    prompt_file = run_dir / f"{label}_prompt.md"
    write_file(prompt_file, prompt)

    # Prefer passing prompt as stdin when command ends with a flag that accepts prompt from stdin.
    # But most CLIs accept prompt as argv after command. Keep it simple/configurable.
    #
    # Examples:
    #   claude.command: "claude -p"
    #   codex.command: "codex"
    #
    cmd = f"{base_cmd} {quote_arg(prompt)}"
    append_jsonl(run_dir / "transcript.jsonl", {
        "ts": _dt.datetime.now().isoformat(),
        "event": "agent_start",
        "agent": label,
        "command": base_cmd,
        "prompt_file": str(prompt_file.relative_to(ROOT)),
    })
    result = run_cmd(cmd, timeout=timeout)
    append_jsonl(run_dir / "transcript.jsonl", {
        "ts": _dt.datetime.now().isoformat(),
        "event": "agent_done",
        "agent": label,
        "returncode": result.code,
        "stdout_chars": len(result.stdout),
        "stderr_chars": len(result.stderr),
    })
    write_file(run_dir / f"{label}_stdout.md", result.stdout)
    write_file(run_dir / f"{label}_stderr.txt", result.stderr)
    return result


def run_tests(policy: Policy, run_dir: Path, plan_text: str = "") -> str:
    commands: List[str] = []

    # Pull test commands from PLAN "## Tests" bullet lines.
    in_tests = False
    for line in plan_text.splitlines():
        if line.strip().lower().startswith("## tests"):
            in_tests = True
            continue
        if in_tests and line.strip().startswith("## "):
            in_tests = False
        if in_tests:
            s = line.strip()
            if s.startswith("- "):
                cmd = s[2:].strip()
                if cmd and cmd.lower() not in ("none", "n/a"):
                    commands.append(cmd)

    if not commands:
        commands = list(policy.default_tests)

    outputs = []
    for i, cmd in enumerate(commands, 1):
        outputs.append(f"$ {cmd}\n")
        r = run_cmd(cmd, timeout=900)
        outputs.append(r.stdout)
        if r.stderr:
            outputs.append("\n[stderr]\n" + r.stderr)
        outputs.append(f"\n[exit_code] {r.code}\n")
        if r.code != 0:
            # Stop after first failing test command; reviewer sees failure.
            break

    text = "\n".join(outputs)
    write_file(run_dir / "tests.txt", text)
    return text


def tests_passed(test_output: str) -> bool:
    codes = re.findall(r"\[exit_code\]\s+(\d+)", test_output)
    return bool(codes) and all(c == "0" for c in codes)


def review_approved(review: str) -> bool:
    return bool(re.search(r"Status:\s*APPROVED", review, flags=re.I))


# -----------------------------
# Main flow
# -----------------------------

def create_run(goal: str) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = RUNS_DIR / now_id()
    run_dir.mkdir(parents=True, exist_ok=False)
    write_file(run_dir / "task.md", goal + "\n")
    write_file(LATEST_FILE, str(run_dir.relative_to(ROOT)) + "\n")
    append_jsonl(run_dir / "transcript.jsonl", {
        "ts": _dt.datetime.now().isoformat(),
        "event": "run_created",
        "goal": goal,
    })
    return run_dir


def start(goal: str, *, dry_run: bool = False) -> int:
    policy = Policy.load()
    commands = AgentCommands.load()
    run_dir = create_run(goal)

    print(f"[ROVER] run: {run_dir.relative_to(ROOT)}")
    git_snapshot(run_dir)

    ok, msg = guard_check(policy)
    write_file(run_dir / "guard_initial.txt", msg + "\n")
    if not ok:
        print("[ROVER] initial guard failed:")
        print(msg)
        return 2

    if dry_run:
        print("[ROVER] dry run created. No agents called.")
        return 0

    for round_no in range(1, policy.max_rounds + 1):
        print(f"[ROVER] round {round_no}: Claude planning")
        plan_res = call_agent(
            "claude_plan",
            commands.claude,
            build_architect_prompt(goal, run_dir, round_no),
            run_dir,
            commands.timeout_seconds,
        )
        plan = plan_res.stdout.strip()
        write_file(run_dir / "plan.md", plan + "\n")

        if plan_res.code != 0:
            print("[ROVER] Claude planning failed. See run stderr.")
            write_file(run_dir / "final.md", "FAILED: Claude planning failed.\n")
            return 3

        print(f"[ROVER] round {round_no}: Codex building")
        codex_res = call_agent(
            "codex_build",
            commands.codex,
            build_codex_prompt(goal, plan, run_dir, round_no),
            run_dir,
            commands.timeout_seconds,
        )
        codex_result = codex_res.stdout.strip()
        write_file(run_dir / "codex_result.md", codex_result + "\n")

        diff = git_diff()
        write_file(run_dir / "diff.patch", diff)
        write_file(run_dir / "git_status_after_codex.txt", git_status_short())

        ok, guard_msg = guard_check(policy)
        write_file(run_dir / "guard_after_codex.txt", guard_msg + "\n")
        if not ok:
            print("[ROVER] guard failed after Codex. Stopping.")
            print(guard_msg)
            write_file(run_dir / "final.md", "STOPPED: Guard failed after Codex.\n\n" + guard_msg + "\n")
            return 4

        print(f"[ROVER] round {round_no}: running tests")
        test_output = run_tests(policy, run_dir, plan)

        print(f"[ROVER] round {round_no}: Claude reviewing")
        review_res = call_agent(
            "claude_review",
            commands.claude,
            build_review_prompt(goal, plan, codex_result, diff, test_output, guard_msg, round_no),
            run_dir,
            commands.timeout_seconds,
        )
        review = review_res.stdout.strip()
        write_file(run_dir / "review.md", review + "\n")

        if review_res.code != 0:
            print("[ROVER] Claude review failed. See run stderr.")
            write_file(run_dir / "final.md", "FAILED: Claude review failed.\n")
            return 5

        if review_approved(review) and (tests_passed(test_output) or not policy.require_tests):
            final = f"""# FINAL

Status: APPROVED

Run: {run_dir.relative_to(ROOT)}

Changed files:
{chr(10).join("- " + f for f in list_changed_files()) or "- none"}

Tests:
{'PASS' if tests_passed(test_output) else 'NOT REQUIRED'}

Review:
{review}
"""
            write_file(run_dir / "final.md", final)
            print("[ROVER] APPROVED.")
            print(f"[ROVER] final: {run_dir.relative_to(ROOT) / 'final.md'}")
            return 0

        print(f"[ROVER] round {round_no}: Codex fixing review")
        fix_res = call_agent(
            "codex_fix",
            commands.codex,
            build_fix_prompt(goal, review, run_dir, round_no),
            run_dir,
            commands.timeout_seconds,
        )
        write_file(run_dir / "codex_fix_result.md", fix_res.stdout.strip() + "\n")

        diff = git_diff()
        write_file(run_dir / "diff_after_fix.patch", diff)
        write_file(run_dir / "git_status_after_fix.txt", git_status_short())

        ok, guard_msg = guard_check(policy)
        write_file(run_dir / "guard_after_fix.txt", guard_msg + "\n")
        if not ok:
            print("[ROVER] guard failed after fix. Stopping.")
            print(guard_msg)
            write_file(run_dir / "final.md", "STOPPED: Guard failed after fix.\n\n" + guard_msg + "\n")
            return 6

        test_output = run_tests(policy, run_dir, plan)
        if tests_passed(test_output):
            final_review_res = call_agent(
                "claude_final_review",
                commands.claude,
                build_review_prompt(goal, plan, fix_res.stdout, git_diff(), test_output, guard_msg, round_no),
                run_dir,
                commands.timeout_seconds,
            )
            final_review = final_review_res.stdout.strip()
            write_file(run_dir / "final_review.md", final_review + "\n")
            if review_approved(final_review):
                final = f"""# FINAL

Status: APPROVED AFTER FIX

Run: {run_dir.relative_to(ROOT)}

Changed files:
{chr(10).join("- " + f for f in list_changed_files()) or "- none"}

Tests:
PASS

Review:
{final_review}
"""
                write_file(run_dir / "final.md", final)
                print("[ROVER] APPROVED after fix.")
                print(f"[ROVER] final: {run_dir.relative_to(ROOT) / 'final.md'}")
                return 0

        print(f"[ROVER] round {round_no}: not approved yet")

    final = f"""# FINAL

Status: STOPPED

Reason: max rounds reached.

Run: {run_dir.relative_to(ROOT)}

Changed files:
{chr(10).join("- " + f for f in list_changed_files()) or "- none"}

Next:
- Open review.md / final_review.md
- Decide manually whether to continue
"""
    write_file(run_dir / "final.md", final)
    print("[ROVER] stopped: max rounds reached.")
    return 7


def status() -> int:
    if not LATEST_FILE.exists():
        print("No agent session found.")
        return 1
    run_dir = ROOT / read_file(LATEST_FILE).strip()
    print(f"Latest run: {run_dir.relative_to(ROOT)}")
    for name in ("task.md", "final.md", "review.md", "tests.txt", "git_status_after_fix.txt", "git_status_after_codex.txt"):
        p = run_dir / name
        if p.exists():
            print(f"\n--- {name} ---")
            print(read_file(p)[-4000:])
    return 0


def show() -> int:
    if not LATEST_FILE.exists():
        print("No agent session found.")
        return 1
    run_dir = ROOT / read_file(LATEST_FILE).strip()
    print(f"Latest run folder: {run_dir}")
    print("\nFiles:")
    for p in sorted(run_dir.iterdir()):
        print(" -", p.name)
    return 0


def abort(rollback: bool = False) -> int:
    if not LATEST_FILE.exists():
        print("No agent session found.")
        return 1
    run_dir = ROOT / read_file(LATEST_FILE).strip()
    write_file(run_dir / "ABORTED", _dt.datetime.now().isoformat() + "\n")
    print(f"Marked aborted: {run_dir.relative_to(ROOT)}")
    if rollback:
        print("Rolling back working tree changes with: git checkout -- .")
        r = git("checkout -- .", timeout=180)
        if r.code != 0:
            print(r.stderr)
            return r.code
        print("Rollback done.")
    return 0


def config_check() -> int:
    policy = Policy.load()
    commands = AgentCommands.load()

    print("ROOT:", ROOT)
    print("Claude command:", commands.claude)
    print("Codex command:", commands.codex)
    print("Timeout:", commands.timeout_seconds)
    print("Claude exists:", command_exists(commands.claude))
    print("Codex exists:", command_exists(commands.codex))
    print("Policy:", policy)

    ok, msg = guard_check(policy)
    print("Guard:", msg)
    return 0 if ok else 2


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="rover_agents")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("goal", nargs="+")
    p_start.add_argument("--dry-run", action="store_true")

    sub.add_parser("status")
    sub.add_parser("show")

    p_abort = sub.add_parser("abort")
    p_abort.add_argument("--rollback", action="store_true")

    sub.add_parser("config-check")

    args = parser.parse_args(argv)

    if args.cmd == "start":
        return start(" ".join(args.goal), dry_run=args.dry_run)
    if args.cmd == "status":
        return status()
    if args.cmd == "show":
        return show()
    if args.cmd == "abort":
        return abort(rollback=args.rollback)
    if args.cmd == "config-check":
        return config_check()

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
