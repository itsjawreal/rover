from __future__ import annotations

import unittest
from pathlib import Path


class PackagingTests(unittest.TestCase):
    def test_pyproject_declares_cli_and_mcp_scripts(self) -> None:
        text = Path("pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('github-contribution-engine = "app.builder:main"', text)
        self.assertIn('contribution-mcp = "src.contribution_mcp.server:main"', text)
