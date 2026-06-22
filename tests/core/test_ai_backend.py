from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from src.core import ai


class AIBackendSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        ai.reset_usage()

    def test_codex_failure_does_not_fallback_to_claude_by_default(self) -> None:
        err = ai.BackendConfigurationError("Codex CLI command was not found.")

        with patch("src.core.ai.ALLOW_BACKEND_FALLBACK", False), patch(
            "src.core.ai._has_usable_backend", return_value=True
        ), patch("src.core.ai._call_backend", side_effect=err) as call_backend, patch(
            "src.core.ai._switch_backend"
        ) as switch_backend:
            with self.assertRaises(ai.BackendConfigurationError) as raised:
                ai.call_ai("test prompt", stream_path=Path("dummy.txt"))

        self.assertEqual(str(raised.exception), "Codex CLI command was not found.")
        self.assertEqual(call_backend.call_count, 1)
        switch_backend.assert_not_called()

    def test_codex_failure_can_fallback_to_claude_when_explicitly_enabled(self) -> None:
        responses = [
            ai.BackendConfigurationError("Codex CLI command was not found."),
            "Claude fallback response",
        ]

        with patch("src.core.ai.ALLOW_BACKEND_FALLBACK", True), patch(
            "src.core.ai._has_usable_backend", return_value=True
        ), patch("src.core.ai._call_backend", side_effect=responses) as call_backend, patch(
            "src.core.ai._switch_backend"
        ) as switch_backend:
            result = ai.call_ai("test prompt")

        self.assertEqual(result, "Claude fallback response")
        self.assertEqual(call_backend.call_count, 2)
        switch_backend.assert_called_once_with("claude")

    def test_claude_failure_can_fallback_to_codex_when_explicitly_enabled(self) -> None:
        responses = [
            ai.BackendConfigurationError("Claude CLI command was not found."),
            "Codex fallback response",
        ]

        with patch("src.core.ai._ACTIVE_BACKEND", "claude"), patch("src.core.ai.ALLOW_BACKEND_FALLBACK", True), patch(
            "src.core.ai._has_usable_backend", return_value=True
        ), patch("src.core.ai._call_backend", side_effect=responses) as call_backend, patch(
            "src.core.ai._switch_backend"
        ) as switch_backend:
            result = ai.call_ai("test prompt")

        self.assertEqual(result, "Codex fallback response")
        self.assertEqual(call_backend.call_count, 2)
        switch_backend.assert_called_once_with("codex")

    def test_build_command_uses_direct_exec_on_posix(self) -> None:
        with patch("src.core.ai.os.name", "posix"), patch("src.core.ai._ACTIVE_BACKEND", "codex"), patch(
            "src.core.ai.Path.cwd", return_value="/repo"
        ):
            result = ai._build_command(["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "-"])

        self.assertEqual(result[0], "codex")
        self.assertIn("-C", result)

    def test_build_command_wraps_with_pwsh_on_windows(self) -> None:
        with patch("src.core.ai.os.name", "nt"), patch("src.core.ai._ACTIVE_BACKEND", "codex"), patch(
            "src.core.ai.shutil.which", return_value="pwsh"
        ), patch(
            "src.core.ai.Path.cwd", return_value="C:/repo"
        ):
            result = ai._build_command(["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "-"])

        self.assertEqual(result[:2], ["pwsh", "-Command"])
        self.assertIn("codex", result[2])

    def test_unusable_windows_mounted_codex_path_is_rejected_on_posix(self) -> None:
        with patch("src.core.ai.CODEX_CMD", "/mnt/c/Users/USER/AppData/Roaming/npm/codex"), patch(
            "src.core.ai.shutil.which", return_value="/mnt/c/Users/USER/AppData/Roaming/npm/codex"
        ):
            self.assertFalse(ai._has_usable_backend("codex"))


if __name__ == "__main__":
    unittest.main()
