"""Telegram command bot — receives messages and routes them to Menisik CLI actions.

Supported commands (send in the configured chat):
  /check                   poll open PRs + auto-respond to maintainer feedback
  /respond                 handle maintainer comments only (skip status poll)
  /prs [all|open|merged|closed]  list submitted PRs
  /run [owner/repo]        run one contribution (optional targeted repo)
  /inspect owner/repo      analyze repo without submitting
  /report                  show contribution run history
  /status                  engine status dashboard
  /doctor                  verify setup
  /monitor                 show PR monitor status
  /help                    show this reference

Any "menisik <subcommand>" text is also accepted as an alias
(the deprecated "rover <subcommand>" spelling still works).
Only messages from the configured TELEGRAM_CHAT_ID are processed.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_HELP_TEXT = """\
Menisik Bot — Commands
======================
/check                  poll PRs + auto-fix maintainer feedback
/respond                handle maintainer comments only
/prs [open|merged|closed]  list submitted PRs
/run [owner/repo]       submit one contribution
/inspect owner/repo     analyze repo (no submission)
/report                 run history + queued opportunities
/status                 engine status dashboard
/doctor                 verify setup
/monitor                PR monitor status
/help                   show this reference

Aliases: send "menisik check", "menisik run owner/repo", etc.
"""


class TelegramCommandBot:
    """Long-poll Telegram bot that routes incoming messages to Menisik CLI."""

    def __init__(self, token: str, chat_id: str, project_root: Path) -> None:
        self._token = token
        self._chat_id = str(chat_id).strip()
        self._project_root = project_root
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._offset = 0

    # ── Telegram API ─────────────────────────────────────────

    def _api(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        data = json.dumps(payload).encode() if payload else None
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=35) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            log.warning("Telegram API %s: HTTP %s", method, exc.code)
            return {"ok": False}
        except Exception as exc:
            log.debug("Telegram API %s failed: %s", method, exc)
            return {"ok": False}

    def send(self, text: str) -> None:
        """Send a message back to the configured chat, splitting if needed."""
        for chunk in [text[i:i + 4000] for i in range(0, max(len(text), 1), 4000)]:
            self._api("sendMessage", {"chat_id": self._chat_id, "text": chunk})

    def _poll(self) -> list[dict[str, Any]]:
        resp = self._api("getUpdates", {
            "offset": self._offset,
            "timeout": 30,
            "allowed_updates": ["message"],
        })
        if not resp.get("ok"):
            return []
        updates = list(resp.get("result") or [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    # ── Authorization ────────────────────────────────────────

    def _authorized(self, msg: dict[str, Any]) -> bool:
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        # accept both group ID (negative) and its stripped form
        return chat_id == self._chat_id or chat_id == self._chat_id.lstrip("-")

    # ── Menisik runner ───────────────────────────────────────

    def _menisik(self, *args: str) -> str:
        cmd = [sys.executable, "-m", "app.contribute", *args]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self._project_root),
                capture_output=True,
                text=True,
                timeout=300,
            )
            out = (result.stdout + result.stderr).strip()
            return out[-3500:] if len(out) > 3500 else out or "(no output)"
        except subprocess.TimeoutExpired:
            return "Timed out (300s). Use /status to check if a run is active."
        except Exception as exc:
            return f"Error: {exc}"

    # ── Command dispatch ─────────────────────────────────────

    def _dispatch(self, text: str) -> str:
        text = text.strip()
        # Strip "menisik " prefix so "menisik check" == "/check";
        # "rover " is the deprecated pre-rename alias.
        lower = text.lower()
        if lower.startswith("menisik "):
            text = text[8:].strip()
            lower = text.lower()
        elif lower.startswith("rover "):
            text = text[6:].strip()
            lower = text.lower()

        parts = text.split()
        cmd = parts[0].lstrip("/").lower() if parts else ""
        rest = parts[1:]

        if cmd in ("help", ""):
            return _HELP_TEXT

        if cmd == "check":
            self.send("Polling PRs and handling feedback…")
            return self._menisik("check", "--json")

        if cmd == "respond":
            self.send("Handling maintainer feedback…")
            return self._menisik("respond", "--json")

        if cmd == "prs":
            status = rest[0] if rest else "all"
            return self._menisik("list-prs", status)

        if cmd == "run":
            repo = rest[0] if rest else ""
            if repo:
                self.send(f"Starting contribution run for {repo}…")
                return self._menisik("run", repo)
            self.send("Starting search-mode contribution run…")
            return self._menisik("run")

        if cmd == "inspect":
            if not rest:
                return "Usage: /inspect owner/repo"
            self.send(f"Inspecting {rest[0]}…")
            return self._menisik("inspect", rest[0])

        if cmd == "report":
            return self._menisik("report")

        if cmd == "status":
            return self._menisik()  # no args → status dashboard

        if cmd == "doctor":
            return self._menisik("doctor")

        if cmd == "monitor":
            sub = rest[0].lower() if rest else ""
            if sub == "on":
                # start monitor via env-driven MCP config — advise user
                return (
                    "To start the PR monitor, set PR_MONITOR_INTERVAL_SECONDS=300 in .env\n"
                    "and restart menisik-mcp, or call start_pr_monitor via MCP."
                )
            if sub == "off":
                return (
                    "To stop the PR monitor, set PR_MONITOR_INTERVAL_SECONDS=0 in .env\n"
                    "and restart menisik-mcp, or call stop_pr_monitor via MCP."
                )
            # status — call the MCP server function directly via subprocess
            result = subprocess.run(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0,'.'); "
                 "from src.contribution_mcp.server import get_pr_monitor_status; "
                 "import json; print(json.dumps(get_pr_monitor_status()))"],
                cwd=str(self._project_root),
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                try:
                    s = json.loads(result.stdout.strip())
                    return (
                        f"PR Monitor\n"
                        f"Running : {s.get('running')}\n"
                        f"Interval: {s.get('interval_seconds')}s\n"
                        f"Last run: {s.get('last_run') or 'never'}\n"
                        f"Runs    : {s.get('run_count')}"
                    )
                except Exception:
                    pass
            return "Monitor status unavailable (MCP server not running in this process)."

        # Unknown input is NOT forwarded to the CLI: anyone in the configured
        # chat could otherwise smuggle arbitrary builder flags (e.g. a live
        # submission run with limits overridden). Only the explicit commands
        # above are executable from chat.
        return f"Unknown command: {parts[0][:60]}\n\n{_HELP_TEXT}" if parts else _HELP_TEXT

    # ── Main polling loop ────────────────────────────────────

    def _loop(self) -> None:
        log.info("Telegram bot started (chat_id=%s)", self._chat_id)
        self.send("Menisik bot online. Send /help for available commands.")
        while True:
            with self._lock:
                if not self._running:
                    break
            try:
                updates = self._poll()
            except Exception as exc:
                log.warning("Telegram poll error: %s", exc)
                time.sleep(5)
                continue
            for update in updates:
                msg = update.get("message") or {}
                if not self._authorized(msg):
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                log.info("Bot received: %s", text[:80])
                try:
                    reply = self._dispatch(text)
                    if reply:
                        self.send(reply)
                except Exception as exc:
                    log.warning("Bot handler error: %s", exc)
                    self.send(f"Error: {exc}")
        log.info("Telegram bot stopped")

    # ── Lifecycle ────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            # A previous loop may still be draining its long-poll (up to ~35 s
            # after stop()). Refuse to start a second poller: two getUpdates
            # loops fight over the offset and Telegram rejects one with 409.
            if self._thread is not None and self._thread.is_alive():
                log.warning("Telegram bot not restarted: previous polling loop is still winding down")
                return
            self._running = True
            t = threading.Thread(target=self._loop, daemon=True, name="telegram-bot")
            self._thread = t
        t.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            t = self._thread
        # Best-effort wait so an immediate restart doesn't race the old poller;
        # the loop exits at the next long-poll return (bounded by its timeout).
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running
