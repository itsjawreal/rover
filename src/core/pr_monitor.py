"""Background PR monitor — polls open PRs and sends notifications via the
configured transport (Telegram, OpenClaw, etc.).

Runs as a daemon thread, completely independent of any AI backend or agent
shell (Claude Code, Codex, OpenClaw — all work the same way).
The AI backend used for auto-responding to maintainer feedback is determined
by AI_BACKEND in .env (default: codex).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

_log = logging.getLogger(__name__)

_lock = threading.Lock()
_running = False
_thread: threading.Thread | None = None
_interval: int = 0
_last_run: str = ""
_run_count: int = 0


def _loop(interval: int) -> None:
    global _running, _last_run, _run_count
    _log.info("PR monitor started (interval=%ds, AI_BACKEND=%s)", interval, _ai_backend())
    while True:
        with _lock:
            if not _running:
                break
        try:
            from src.contrib.pr_generator import check_all_prs
            from datetime import datetime, timezone
            check_all_prs(_log)
            with _lock:
                _last_run = datetime.now(timezone.utc).isoformat()
                _run_count += 1
        except Exception as exc:
            _log.warning("PR monitor check failed: %s", exc)
        # sleep in 1-second ticks so stop() is responsive
        for _ in range(interval):
            with _lock:
                if not _running:
                    break
            time.sleep(1)
        else:
            continue
        break
    _log.info("PR monitor stopped")


def _ai_backend() -> str:
    try:
        import os
        return os.getenv("AI_BACKEND", "codex")
    except Exception:
        return "unknown"


def start(interval: int) -> dict[str, Any]:
    """Start the PR monitor background thread.

    interval: seconds between polls (minimum 60, recommended 300).
    Safe to call multiple times — ignored if already running.
    Returns a status dict.
    """
    global _running, _thread, _interval
    interval = max(60, interval)
    with _lock:
        if _running:
            return {"status": "already_running", "interval_seconds": _interval,
                    "last_run": _last_run, "run_count": _run_count}
        _interval = interval
        _running = True
        t = threading.Thread(target=_loop, args=(interval,), daemon=True, name="pr-monitor")
        _thread = t
    t.start()
    return {"status": "started", "interval_seconds": interval,
            "last_run": _last_run, "run_count": _run_count}


def stop() -> dict[str, Any]:
    """Stop the PR monitor. Returns immediately; thread winds down within ~1 s."""
    global _running
    with _lock:
        if not _running:
            return {"status": "not_running"}
        _running = False
    return {"status": "stopping", "last_run": _last_run, "run_count": _run_count}


def status() -> dict[str, Any]:
    """Return current monitor state."""
    with _lock:
        return {
            "running": _running,
            "interval_seconds": _interval,
            "last_run": _last_run,
            "run_count": _run_count,
            "ai_backend": _ai_backend(),
        }
