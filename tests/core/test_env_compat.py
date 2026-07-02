from __future__ import annotations

import importlib
import os
import unittest
from unittest import mock

import src.core.config as config


class NotifyEnvCompatTests(unittest.TestCase):
    def tearDown(self) -> None:
        importlib.reload(config)

    def test_menisik_notify_env_wins_over_legacy_rover_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "MENISIK_NOTIFY_TRANSPORT": "telegram",
                "ROVER_NOTIFY_TRANSPORT": "openclaw",
            },
            clear=False,
        ):
            reloaded = importlib.reload(config)
            self.assertEqual(reloaded.MENISIK_NOTIFY_TRANSPORT, "telegram")

    def test_legacy_rover_notify_env_is_read_with_deprecation_warning(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ROVER_NOTIFY_TRANSPORT": "telegram",
                "ROVER_NOTIFY_INTERVAL_SECONDS": "45",
                "ROVER_NOTIFY_PROGRESS": "true",
            },
            clear=False,
        ):
            for key in (
                "MENISIK_NOTIFY_TRANSPORT",
                "MENISIK_NOTIFY_INTERVAL_SECONDS",
                "MENISIK_NOTIFY_PROGRESS",
            ):
                os.environ.pop(key, None)
            with self.assertLogs("src.core.config", level="WARNING") as captured:
                reloaded = importlib.reload(config)
            self.assertEqual(reloaded.MENISIK_NOTIFY_TRANSPORT, "telegram")
            self.assertEqual(reloaded.MENISIK_NOTIFY_INTERVAL_SECONDS, 45)
            self.assertTrue(reloaded.MENISIK_NOTIFY_PROGRESS)
        warning_text = "\n".join(captured.output)
        self.assertIn("ROVER_NOTIFY_TRANSPORT", warning_text)
        self.assertIn("MENISIK_NOTIFY_TRANSPORT", warning_text)

    def test_deprecation_warning_emitted_once_per_legacy_key(self) -> None:
        with mock.patch.dict(
            os.environ, {"ROVER_NOTIFY_STALL_SECONDS": "120"}, clear=False
        ):
            os.environ.pop("MENISIK_NOTIFY_STALL_SECONDS", None)
            reloaded = importlib.reload(config)
            self.assertEqual(reloaded.MENISIK_NOTIFY_STALL_SECONDS, 120)
            with mock.patch.object(reloaded.log, "warning") as warn:
                reloaded._env_raw("MENISIK_NOTIFY_STALL_SECONDS")
                reloaded._env_raw("MENISIK_NOTIFY_STALL_SECONDS")
        # already warned during reload; repeated reads stay silent
        warn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
