from __future__ import annotations

import unittest
from unittest.mock import patch

from src.contribution_mcp.server import (
    contribution_once_payload,
    contribution_targeted_payload,
    route_command_payload,
)


class ContributionMCPTests(unittest.TestCase):
    def test_route_command_payload_exposes_canonical_action(self) -> None:
        payload = route_command_payload("buat 1 kontribusi")

        self.assertEqual(payload["action"], "contrib_once")
        self.assertEqual(payload["count"], 1)
        self.assertTrue(payload["dry_run"])
        self.assertIn("--contrib", payload["cli_args"])

    def test_contribution_once_payload_routes_through_builder(self) -> None:
        with patch("src.contribution_mcp.server._run_builder", return_value={"ok": True, "command": []}) as mocked:
            payload = contribution_once_payload(dry_run=True, first_pr=True)

        self.assertTrue(payload["ok"])
        mocked.assert_called_once_with(["--contrib", "--count", "1", "--goal", "bugfix", "--first-pr", "--dry-run"])

    def test_contribution_targeted_payload_routes_repo(self) -> None:
        with patch("src.contribution_mcp.server._run_builder", return_value={"ok": True, "command": []}) as mocked:
            payload = contribution_targeted_payload("owner/repo", dry_run=False)

        self.assertTrue(payload["ok"])
        mocked.assert_called_once_with(["--contrib", "owner/repo", "--count", "1", "--goal", "bugfix"])
