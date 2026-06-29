from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.github.fork import (
    ForkError,
    _clone_repo_with_retry,
    _run,
    _run_repo_local_verification,
    _verify_dep_update_submission,
    _wait_for_fork_ready,
    get_current_github_login,
)


def _cp(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class ForkSubmissionTests(unittest.TestCase):
    def test_get_current_github_login_prefers_env_override(self) -> None:
        with patch.dict(os.environ, {"GITHUB_OWNER": "itsjawreal"}, clear=False):
            self.assertEqual(get_current_github_login(), "itsjawreal")

    def test_get_current_github_login_falls_back_to_auth_status_account_name(self) -> None:
        responses = [
            _cp(1, stderr="authentication failed"),
            _cp(
                1,
                stderr=(
                    "github.com\n"
                    "  X Failed to log in to github.com account itsjawreal (keyring)\n"
                    "  - Active account: true\n"
                    "  - The token in keyring is invalid.\n"
                ),
            ),
        ]

        with patch.dict(os.environ, {}, clear=True), \
                patch("dotenv.dotenv_values", return_value={}), \
                patch("src.github.fork._run", side_effect=responses):
            self.assertEqual(get_current_github_login(), "itsjawreal")

    def test_wait_for_fork_ready_retries_until_repo_is_visible(self) -> None:
        responses = [
            _cp(1, stderr="GraphQL: Could not resolve to a Repository with the name 'currentuser/free-claude-code'."),
            _cp(0, stdout='{"nameWithOwner":"currentuser/free-claude-code"}'),
        ]

        with patch("src.github.fork._run", side_effect=responses), patch("time.sleep") as mocked_sleep:
            _wait_for_fork_ready("currentuser/free-claude-code", logging.getLogger("test"), timeout_s=10, poll_interval_s=1)

        mocked_sleep.assert_called_once_with(1)

    def test_wait_for_fork_ready_raises_after_timeout(self) -> None:
        with patch(
            "src.github.fork._run",
            return_value=_cp(1, stderr="GraphQL: Could not resolve to a Repository with the name 'currentuser/free-claude-code'."),
        ), patch("time.sleep"):
            with self.assertRaises(ForkError):
                _wait_for_fork_ready("currentuser/free-claude-code", logging.getLogger("test"), timeout_s=1, poll_interval_s=1)

    def test_clone_repo_with_retry_recovers_after_eventual_consistency_delay(self) -> None:
        responses = [
            _cp(1, stderr="GraphQL: Could not resolve to a Repository with the name 'currentuser/free-claude-code'."),
            _cp(0, stdout="cloned"),
        ]

        with patch("src.github.fork._run", side_effect=responses), patch("time.sleep") as mocked_sleep:
            _clone_repo_with_retry("currentuser/free-claude-code", Path("/tmp/repo"), logging.getLogger("test"), attempts=2, delay_s=1)

        mocked_sleep.assert_called_once_with(1)

    def test_verify_dep_update_submission_rejects_lockfile_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / "package.json").write_text('{"scripts":{"typecheck":"tsc -b"}}', encoding="utf-8")
            (tmp_dir / "package-lock.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ForkError) as ctx:
                _verify_dep_update_submission(tmp_dir, {"package.json": "{}"}, logging.getLogger("test"))

        self.assertIn("package-lock.json", str(ctx.exception))

    def test_verify_dep_update_submission_requires_verification_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / "package.json").write_text('{"scripts":{"dev":"vite"}}', encoding="utf-8")
            with patch("src.github.fork.shutil.which", return_value="/usr/bin/npm"):
                with self.assertRaises(ForkError) as ctx:
                    _verify_dep_update_submission(tmp_dir, {"package.json": "{}"}, logging.getLogger("test"))

        self.assertIn("no typecheck/build/test script", str(ctx.exception))

    def test_local_verification_runs_pnpm_test_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / "package.json").write_text('{"scripts":{"test":"vitest run"}}', encoding="utf-8")
            (tmp_dir / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

            with patch("src.github.fork.shutil.which", return_value="/usr/bin/pnpm"), \
                 patch("src.github.fork._run", return_value=_cp(0)) as mocked_run:
                _run_repo_local_verification(tmp_dir, logging.getLogger("test"))

        self.assertEqual(mocked_run.call_args_list[0].args[0], ["pnpm", "install", "--frozen-lockfile"])
        self.assertEqual(mocked_run.call_args_list[1].args[0], ["pnpm", "test"])

    def test_run_raises_fork_error_on_timeout(self) -> None:
        with patch("src.github.fork.subprocess.run", side_effect=subprocess.TimeoutExpired(["gh"], 300)):
            with self.assertRaises(ForkError) as ctx:
                _run(["gh", "api", "user"])

        self.assertIn("timed out", str(ctx.exception))

    def test_local_verification_blocks_failed_test_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / "package.json").write_text('{"scripts":{"test":"vitest run"}}', encoding="utf-8")

            with patch("src.github.fork.shutil.which", return_value="/usr/bin/npm"), \
                 patch("src.github.fork._run", side_effect=[_cp(0), _cp(1, stderr="tests failed")]):
                with self.assertRaises(ForkError) as ctx:
                    _run_repo_local_verification(tmp_dir, logging.getLogger("test"))

        self.assertIn("local verification failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
