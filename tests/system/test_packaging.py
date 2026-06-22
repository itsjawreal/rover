from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class PackagingTests(unittest.TestCase):
    def test_pyproject_declares_cli_and_mcp_scripts(self) -> None:
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('rover = "app.contribute:main"', text)
        self.assertIn('rover-engine = "app.builder:main"', text)
        self.assertIn('rover-mcp = "src.contribution_mcp.server:main"', text)
