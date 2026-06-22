from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import call
from unittest.mock import patch

from src.core import ai


class AIBackendSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        ai.reset_usage()
        # These tests assert codex-default behavior; pin the active backend so they
        # are independent of the operator's configured AI_BACKEND (.env).
        ai._ACTIVE_BACKEND = "codex"

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
        switch_backend.assert_called_once_with("codex")

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
        self.assertEqual(switch_backend.call_args_list, [call("claude"), call("codex")])

    def test_fallback_restores_original_backend_after_success(self) -> None:
        responses = [
            ai.BackendConfigurationError("Codex CLI command was not found."),
            "Claude fallback response",
        ]

        with patch("src.core.ai._ACTIVE_BACKEND", "codex"), patch(
            "src.core.ai.ALLOW_BACKEND_FALLBACK", True
        ), patch("src.core.ai._has_usable_backend", return_value=True), patch(
            "src.core.ai._call_backend", side_effect=responses
        ):
            result = ai.call_ai("test prompt")

        self.assertEqual(result, "Claude fallback response")
        self.assertEqual(ai.get_backend_name(), "codex")

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
        self.assertEqual(switch_backend.call_args_list, [call("codex"), call("claude")])

    def test_usage_counts_failed_attempt_before_successful_fallback(self) -> None:
        ai.reset_usage()
        prompt = "test prompt"
        responses = [
            ai.BackendRuntimeError("Codex runtime unavailable."),
            "Claude fallback response",
        ]

        with patch("src.core.ai._ACTIVE_BACKEND", "codex"), patch(
            "src.core.ai.ALLOW_BACKEND_FALLBACK", True
        ), patch("src.core.ai._has_usable_backend", return_value=True), patch(
            "src.core.ai._call_backend", side_effect=responses
        ):
            result = ai.call_ai(prompt)

        usage = ai.get_usage()
        self.assertEqual(result, "Claude fallback response")
        self.assertEqual(usage["calls"], 0)

    def test_call_backend_counts_failed_cli_attempt_in_usage(self) -> None:
        ai.reset_usage()
        ai._ACTIVE_BACKEND = "codex"

        class _Proc:
            def __init__(self) -> None:
                self.stdin = Mock()
                self.stdout = []
                self.stderr = []
                self.returncode = 1

            def wait(self) -> int:
                return self.returncode

        with patch("src.core.ai.subprocess.Popen", return_value=_Proc()), patch(
            "src.core.ai._prepare_invocation", return_value=(["codex"], b"prompt")
        ):
            with self.assertRaises(RuntimeError):
                ai._call_backend("abc")

        usage = ai.get_usage()
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["prompt_chars"], 3)

    def test_call_backend_normalizes_broken_pipe_during_stdin_write(self) -> None:
        class _Reader:
            def __init__(self, text: bytes) -> None:
                self._text = text

            def read(self) -> bytes:
                return self._text

        class _BrokenPipeProc:
            def __init__(self) -> None:
                self.stdin = Mock()
                self.stdin.write.side_effect = BrokenPipeError()
                self.stdin.close = Mock()
                self.stdout = _Reader(b"")
                self.stderr = _Reader(b"backend refused stdin")
                self.returncode = 1

            def wait(self, timeout: int | None = None) -> int:
                return self.returncode

        with patch("src.core.ai.subprocess.Popen", return_value=_BrokenPipeProc()), patch(
            "src.core.ai._prepare_invocation", return_value=(["codex"], b"prompt")
        ):
            with self.assertRaises(ai.BackendRuntimeError) as raised:
                ai._call_backend("abc")

        self.assertIn("closed stdin early", str(raised.exception))

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


class OpenRouterBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        ai.reset_usage()

    def _resp(self, status: int, *, json_body: dict | None = None, text: str = "") -> Mock:
        resp = Mock()
        resp.status_code = status
        resp.text = text
        resp.json.return_value = json_body or {}
        return resp

    def test_openrouter_success_returns_message_content(self) -> None:
        body = {"choices": [{"message": {"content": "PATCH_TEXT"}}], "usage": {"total_tokens": 5}}
        with patch("src.core.ai._ACTIVE_BACKEND", "openrouter"), \
             patch("src.core.ai.OPENROUTER_API_KEY", "key-123"), \
             patch("src.core.ai.OPENROUTER_MODEL", "vendor/model"), \
             patch("src.core.ai.requests.post", return_value=self._resp(200, json_body=body)) as post:
            result = ai._call_backend("do the thing")

        self.assertEqual(result, "PATCH_TEXT")
        self.assertTrue(post.call_args.kwargs["json"]["model"] == "vendor/model")

    def test_openrouter_missing_key_raises_config_error(self) -> None:
        with patch("src.core.ai._ACTIVE_BACKEND", "openrouter"), \
             patch("src.core.ai.OPENROUTER_API_KEY", ""), \
             patch("src.core.ai.OPENROUTER_MODEL", "vendor/model"):
            with self.assertRaises(ai.BackendConfigurationError):
                ai._call_backend("prompt")

    def test_openrouter_rate_limit_raises_runtime_error(self) -> None:
        with patch("src.core.ai._ACTIVE_BACKEND", "openrouter"), \
             patch("src.core.ai.OPENROUTER_API_KEY", "key-123"), \
             patch("src.core.ai.OPENROUTER_MODEL", "vendor/model"), \
             patch("src.core.ai.requests.post", return_value=self._resp(429, text="rate limit")):
            with self.assertRaises(RuntimeError):
                ai._call_backend("prompt")

    def test_openrouter_credit_limit_raises_credit_error(self) -> None:
        with patch("src.core.ai._ACTIVE_BACKEND", "openrouter"), \
             patch("src.core.ai.OPENROUTER_API_KEY", "key-123"), \
             patch("src.core.ai.OPENROUTER_MODEL", "vendor/model"), \
             patch("src.core.ai.requests.post", return_value=self._resp(402, text="insufficient credits")):
            with self.assertRaises(ai.CreditLimitError):
                ai._call_backend("prompt")

    def test_openrouter_backend_reported_usable_with_key(self) -> None:
        with patch("src.core.ai.OPENROUTER_API_KEY", "key-123"):
            self.assertTrue(ai._has_usable_backend("openrouter"))
        with patch("src.core.ai.OPENROUTER_API_KEY", ""):
            self.assertFalse(ai._has_usable_backend("openrouter"))


class ParseJsonRobustnessTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(ai._parse_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self) -> None:
        self.assertEqual(ai._parse_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_json_wrapped_in_prose(self) -> None:
        raw = 'Sure, here is the patch:\n{"pr_title": "fix", "n": 2}\nLet me know if you need changes.'
        self.assertEqual(ai._parse_json(raw), {"pr_title": "fix", "n": 2})

    def test_braces_inside_strings_do_not_break_extraction(self) -> None:
        raw = 'reasoning...\n{"code": "if (x) { return; }", "ok": true}\ndone'
        self.assertEqual(ai._parse_json(raw), {"code": "if (x) { return; }", "ok": True})

    def test_unrecoverable_raises(self) -> None:
        with self.assertRaises(ValueError):
            ai._parse_json("no json here at all")


if __name__ == "__main__":
    unittest.main()
