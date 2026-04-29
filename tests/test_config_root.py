from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.config import _discover_root, _looks_like_repo_root


class ConfigRootTests(unittest.TestCase):
    def test_looks_like_repo_root_detects_expected_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# x\n", encoding="utf-8")
            (root / "app").mkdir()
            (root / "app" / "builder.py").write_text("print('x')\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "config.py").write_text("ROOT='x'\n", encoding="utf-8")

            self.assertTrue(_looks_like_repo_root(root))

    def test_discover_root_prefers_repo_layout_from_cwd_parents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# x\n", encoding="utf-8")
            (root / "app").mkdir()
            (root / "app" / "builder.py").write_text("print('x')\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "config.py").write_text("ROOT='x'\n", encoding="utf-8")
            nested = root / "nested" / "deeper"
            nested.mkdir(parents=True)

            previous = Path.cwd()
            try:
                import os
                os.chdir(nested)
                self.assertEqual(_discover_root(), root.resolve())
            finally:
                os.chdir(previous)
