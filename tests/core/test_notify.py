from __future__ import annotations

import io
import unittest
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch


class NotifyTests(unittest.TestCase):
    def test_notify_telegram_uses_bot_api(self) -> None:
        from src.core.notify import NotificationRoute, notify

        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok": true, "result": {"message_id": 42}}'
        with (
            patch("src.core.notify.TELEGRAM_TOKEN", "token"),
            patch("src.core.notify.TELEGRAM_CHAT", "-100123"),
            patch("src.core.notify.urllib.request.urlopen", return_value=response) as mocked_urlopen,
        ):
            result = notify("hello", route=NotificationRoute(transport="telegram", target="-100123", thread_id="9"))

        self.assertTrue(result)
        request = mocked_urlopen.call_args.args[0]
        self.assertIn("sendMessage", request.full_url)
        self.assertIn(b'"chat_id": "-100123"', request.data)
        self.assertIn(b'"message_thread_id": "9"', request.data)

    def test_telegram_edit_message_uses_edit_endpoint(self) -> None:
        from src.core.notify import NotificationRoute, telegram_edit_message

        response = MagicMock()
        with (
            patch("src.core.notify.TELEGRAM_TOKEN", "token"),
            patch("src.core.notify.TELEGRAM_CHAT", "-100123"),
            patch("src.core.notify.urllib.request.urlopen", return_value=response) as mocked_urlopen,
        ):
            result = telegram_edit_message("updated", 42, route=NotificationRoute(transport="telegram", target="-100123"))

        self.assertTrue(result)
        request = mocked_urlopen.call_args.args[0]
        self.assertIn("editMessageText", request.full_url)
        self.assertIn(b'"message_id": 42', request.data)

    def test_telegram_edit_message_treats_not_modified_as_success(self) -> None:
        from src.core.notify import NotificationRoute, telegram_edit_message

        error = HTTPError(
            url="https://api.telegram.org/bottoken/editMessageText",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'{"ok":false,"description":"Bad Request: message is not modified"}'),
        )
        with (
            patch("src.core.notify.TELEGRAM_TOKEN", "token"),
            patch("src.core.notify.TELEGRAM_CHAT", "-100123"),
            patch("src.core.notify.urllib.request.urlopen", side_effect=error),
        ):
            result = telegram_edit_message("updated", 42, route=NotificationRoute(transport="telegram", target="-100123"))

        self.assertTrue(result)

    def test_notify_openclaw_uses_message_send_cli(self) -> None:
        from src.core.notify import NotificationRoute, notify

        completed = MagicMock(returncode=0, stdout='{"ok":true}', stderr="")
        with (
            patch("src.core.notify.OPENCLAW_CMD", "openclaw"),
            patch("src.core.notify.subprocess.run", return_value=completed) as mocked_run,
        ):
            result = notify(
                "hello",
                route=NotificationRoute(
                    transport="openclaw",
                    channel="telegram",
                    target="-100123",
                    account="default",
                    thread_id="7",
                ),
            )

        self.assertTrue(result)
        command = mocked_run.call_args.args[0]
        self.assertEqual(command[:4], ["openclaw", "message", "send", "--channel"])
        self.assertIn("--target", command)
        self.assertIn("-100123", command)
        self.assertIn("--thread-id", command)
        self.assertIn("7", command)
