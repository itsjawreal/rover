#!/usr/bin/env python3
"""Scheduled-task entry point for the contribution engine.

Use this for cron jobs and one-shot runs:
  python run.py              # reads CONTRIB_AUTORUN_ARGS from .env, defaults to --contrib --1
  python run.py --contrib owner/repo --1

Use app.builder directly for interactive use with full flag support:
  python -m app.builder --help
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from app.builder import main as builder_main
from src.core.config import AI_BACKEND


def _default_args() -> list[str]:
    configured = (ROOT / ".env").read_text(encoding="utf-8") if (ROOT / ".env").exists() else ""
    autorun = "CONTRIB_AUTORUN_ARGS"
    for raw_line in configured.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == autorun and value.strip():
            return shlex.split(value.strip(), posix=False)
    return ["--contrib", "--1"]


_EXPLICIT_ACTIONS = {
    "--contrib", "--pr", "--contrib-check", "--pr-check",
    "--contrib-respond", "--pr-respond", "--contrib-report",
    "--repo-inspect", "--doctor",
}


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        argv = _default_args()

    if not any(arg in _EXPLICIT_ACTIONS for arg in argv):
        argv = ["--contrib", *argv]

    is_contrib_run = any(arg in {"--contrib", "--pr"} for arg in argv)

    backend = "Codex" if AI_BACKEND == "codex" else "Claude"
    print(f"[run.py] forwarding to contribution engine using {backend}: {' '.join(argv)}", flush=True)
    builder_main(argv)

    if is_contrib_run:
        print("[run.py] checking PR statuses...", flush=True)
        builder_main(["--contrib-check"])


if __name__ == "__main__":
    main()
