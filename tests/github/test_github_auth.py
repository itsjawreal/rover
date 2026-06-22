from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from src.core.github_auth import github_auth_mode, resolve_github_token
from src.github.scraper import _gh_headers, _gh_get, ScraperError


class GitHubAuthTests(unittest.TestCase):
    def test_resolve_github_token_prefers_gh_token(self) -> None:
        with patch.dict("os.environ", {"GH_TOKEN": "ghu_123", "GITHUB_TOKEN": "ghp_456"}, clear=False):
            self.assertEqual(resolve_github_token(), "ghu_123")
            self.assertEqual(github_auth_mode(), "gh-token-env")

    def test_resolve_github_token_falls_back_to_github_token(self) -> None:
        with patch.dict("os.environ", {"GH_TOKEN": "", "GITHUB_TOKEN": "ghp_456"}, clear=False):
            self.assertEqual(resolve_github_token(), "ghp_456")
            self.assertEqual(github_auth_mode(), "github-token-env")

    def test_resolve_github_token_falls_back_to_gh_auth_token(self) -> None:
        with patch.dict("os.environ", {"GH_TOKEN": "", "GITHUB_TOKEN": ""}, clear=False), patch(
            "src.core.github_auth.shutil.which", return_value="/usr/bin/gh"
        ), patch("src.core.github_auth.subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = "gho_from_cli\n"
            self.assertEqual(resolve_github_token(), "gho_from_cli")
            self.assertEqual(github_auth_mode(), "gh-auth")

    def test_gh_headers_use_resolved_token(self) -> None:
        with patch("src.github.scraper.resolve_github_token", return_value="gho_header_token"):
            headers = _gh_headers()

        self.assertEqual(headers["Authorization"], "Bearer gho_header_token")

    def test_gh_get_uses_gh_cli_when_only_gh_auth_is_available(self) -> None:
        payload = {"full_name": "owner/repo"}
        with patch("src.github.scraper.resolve_github_token", return_value=""), patch(
            "src.github.scraper.shutil.which", return_value="/usr/bin/gh"
        ), patch("src.github.scraper.subprocess.run") as run_mock, patch(
            "src.github.scraper._http_get"
        ) as http_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(payload)
            data = _gh_get("https://api.github.com/repos/owner/repo", params={"ref": "main"})

        self.assertEqual(data["full_name"], "owner/repo")
        http_mock.assert_not_called()
        self.assertIn("repos/owner/repo?ref=main", run_mock.call_args.args[0])

    def test_gh_get_rate_limit_without_auth_fails_fast(self) -> None:
        class _Resp:
            status_code = 403
            text = "API rate limit exceeded"
            headers = {"X-RateLimit-Reset": "9999999999"}

            def raise_for_status(self) -> None:
                raise AssertionError("should not reach raise_for_status")

        with patch("src.github.scraper.resolve_github_token", return_value=""), patch(
            "src.github.scraper.shutil.which", return_value=None
        ), patch("src.github.scraper._http_get", return_value=_Resp()), patch(
            "src.github.scraper.time.sleep"
        ) as sleep_mock:
            with self.assertRaises(ScraperError) as ctx:
                _gh_get("https://api.github.com/repos/owner/repo")

        self.assertIn("without a usable token", str(ctx.exception))
        sleep_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
