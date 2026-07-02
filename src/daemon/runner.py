"""Rover background daemon — PR monitor + Telegram bot, no MCP client needed.

Works with any AI backend (codex, claude) configured in .env.
Notifications go through the configured transport (openclaw → Telegram, or
direct Telegram).

Usage:
    rover-daemon          # installed entry point
    python -m src.daemon  # direct
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.core.config import (  # noqa: E402
    PR_MONITOR_INTERVAL_SECONDS,
    MENISIK_NOTIFY_TRANSPORT,
    TELEGRAM_BOT_ENABLED,
    TELEGRAM_CHAT,
    TELEGRAM_TOKEN,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("rover.daemon")

_stop_event = threading.Event()


def _handle_signal(signum: int, _frame: object) -> None:
    log.info("Signal %d received — shutting down", signum)
    _stop_event.set()


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    ai_backend = os.getenv("AI_BACKEND", "codex")
    notify_transport = MENISIK_NOTIFY_TRANSPORT or "none"
    log.info("Rover daemon starting (AI_BACKEND=%s, notify=%s)", ai_backend, notify_transport)

    started: list[str] = []

    # ── PR monitor ───────────────────────────────────────────
    if PR_MONITOR_INTERVAL_SECONDS > 0:
        from src.core import pr_monitor
        pr_monitor.start(PR_MONITOR_INTERVAL_SECONDS)
        started.append(f"pr-monitor(every={PR_MONITOR_INTERVAL_SECONDS}s, ai={ai_backend})")
    else:
        log.info("PR monitor disabled — set PR_MONITOR_INTERVAL_SECONDS in .env to enable")

    # ── Telegram bot ─────────────────────────────────────────
    if TELEGRAM_BOT_ENABLED:
        if TELEGRAM_TOKEN and TELEGRAM_CHAT:
            from src.core.telegram_bot import TelegramCommandBot
            bot = TelegramCommandBot(TELEGRAM_TOKEN, TELEGRAM_CHAT, _ROOT)
            bot.start()
            started.append(f"telegram-bot(chat={TELEGRAM_CHAT})")
        else:
            log.warning(
                "TELEGRAM_BOT_ENABLED=true but TELEGRAM_BOT_TOKEN or "
                "TELEGRAM_CHAT_ID not configured in .env"
            )

    if not started:
        log.error(
            "No services started. Set PR_MONITOR_INTERVAL_SECONDS and/or "
            "TELEGRAM_BOT_ENABLED=true in .env"
        )
        sys.exit(1)

    log.info("Rover daemon running: %s", " | ".join(started))
    _stop_event.wait()
    log.info("Rover daemon stopped")
