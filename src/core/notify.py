from __future__ import annotations

import json
import logging
import subprocess
import urllib.request
from urllib.error import HTTPError
from dataclasses import dataclass

from src.core.config import (
    OPENCLAW_CMD,
    OPENCLAW_NOTIFY_ACCOUNT,
    OPENCLAW_NOTIFY_CHANNEL,
    OPENCLAW_NOTIFY_TARGET,
    OPENCLAW_NOTIFY_THREAD_ID,
    MENISIK_NOTIFY_MAX_MESSAGE_CHARS,
    MENISIK_NOTIFY_TRANSPORT,
    TELEGRAM_CHAT,
    TELEGRAM_TOKEN,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotificationRoute:
    transport: str
    channel: str = ""
    target: str = ""
    account: str = ""
    thread_id: str = ""


def _truncate_message(message: str) -> str:
    if len(message) <= MENISIK_NOTIFY_MAX_MESSAGE_CHARS:
        return message
    limit = max(50, MENISIK_NOTIFY_MAX_MESSAGE_CHARS - 3)
    return message[:limit].rstrip() + "..."


def default_notification_route() -> NotificationRoute | None:
    if MENISIK_NOTIFY_TRANSPORT == "openclaw" and OPENCLAW_NOTIFY_TARGET:
        return NotificationRoute(
            transport="openclaw",
            channel=OPENCLAW_NOTIFY_CHANNEL,
            target=OPENCLAW_NOTIFY_TARGET,
            account=OPENCLAW_NOTIFY_ACCOUNT,
            thread_id=OPENCLAW_NOTIFY_THREAD_ID,
        )
    if MENISIK_NOTIFY_TRANSPORT == "telegram" and TELEGRAM_TOKEN and TELEGRAM_CHAT:
        return NotificationRoute(transport="telegram", target=TELEGRAM_CHAT)
    return None


def _notify_telegram(message: str, route: NotificationRoute) -> bool:
    result = telegram_send_message(message, route=route)
    return result.get("ok", False)


def telegram_send_message(message: str, route: NotificationRoute | None = None) -> dict[str, object]:
    resolved = route or default_notification_route() or NotificationRoute(transport="telegram", target=TELEGRAM_CHAT)
    chat_id = resolved.target or TELEGRAM_CHAT
    token = TELEGRAM_TOKEN
    if not token or not chat_id:
        log.debug("Telegram notification skipped: credentials not configured")
        return {"ok": False, "message_id": None}
    try:
        payload: dict[str, str] = {"chat_id": chat_id, "text": _truncate_message(message)}
        if resolved.thread_id:
            payload["message_thread_id"] = resolved.thread_id
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        log.debug("Telegram notification sent successfully")
        result = body.get("result") or {}
        return {"ok": True, "message_id": result.get("message_id")}
    except urllib.error.HTTPError as e:
        log.warning(f"Telegram notification failed: HTTP {e.code} - {e.reason}")
        return {"ok": False, "message_id": None}
    except urllib.error.URLError as e:
        log.warning(f"Telegram notification failed: network error - {e.reason}")
        return {"ok": False, "message_id": None}
    except Exception as e:
        log.warning(f"Telegram notification failed: {e}")
        return {"ok": False, "message_id": None}


def telegram_edit_message(message: str, message_id: int, route: NotificationRoute | None = None) -> bool:
    resolved = route or default_notification_route() or NotificationRoute(transport="telegram", target=TELEGRAM_CHAT)
    chat_id = resolved.target or TELEGRAM_CHAT
    token = TELEGRAM_TOKEN
    if not token or not chat_id or not message_id:
        log.debug("Telegram edit skipped: credentials or message_id missing")
        return False
    try:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": _truncate_message(message),
        }
        if resolved.thread_id:
            payload["message_thread_id"] = resolved.thread_id
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/editMessageText",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        log.debug("Telegram message edited successfully")
        return True
    except HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
            body = json.loads(raw) if raw else {}
            description = str(body.get("description") or e.reason or "").lower()
            if "message is not modified" in description:
                log.debug("Telegram edit skipped: message is not modified")
                return True
        except Exception:
            pass
        log.warning(f"Telegram edit failed: HTTP {e.code} - {e.reason}")
        return False
    except urllib.error.URLError as e:
        log.warning(f"Telegram edit failed: network error - {e.reason}")
        return False
    except Exception as e:
        log.warning(f"Telegram edit failed: {e}")
        return False


def _notify_openclaw(message: str, route: NotificationRoute) -> bool:
    if not route.target:
        log.debug("OpenClaw notification skipped: target not configured")
        return False
    command = [
        OPENCLAW_CMD,
        "message",
        "send",
        "--channel",
        route.channel or "telegram",
        "--target",
        route.target,
        "--message",
        _truncate_message(message),
        "--json",
    ]
    if route.account:
        command.extend(["--account", route.account])
    if route.thread_id:
        command.extend(["--thread-id", route.thread_id])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        log.warning(f"OpenClaw notification failed: command not found - {OPENCLAW_CMD}")
        return False
    except subprocess.TimeoutExpired:
        log.warning("OpenClaw notification failed: command timed out")
        return False
    except Exception as e:
        log.warning(f"OpenClaw notification failed: {e}")
        return False
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "").strip()[-300:]
        log.warning(f"OpenClaw notification failed: exit {result.returncode} - {error}")
        return False
    log.debug("OpenClaw notification sent successfully")
    return True


def notify(message: str, route: NotificationRoute | None = None) -> bool:
    resolved = route or default_notification_route()
    if resolved is None:
        log.debug("Notification skipped: no route configured")
        return False
    if resolved.transport == "openclaw":
        return _notify_openclaw(message, resolved)
    if resolved.transport == "telegram":
        return _notify_telegram(message, resolved)
    log.debug(f"Notification skipped: unsupported transport {resolved.transport}")
    return False
