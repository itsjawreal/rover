from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.config import (
    _common_cli_fallback_candidates,
    _discover_root,
    _is_unusable_cross_os_cli_path,
    _looks_like_repo_root,
    _resolve_cli_command_details,
)


class ConfigRootTests(unittest.TestCase):
    def test_looks_like_repo_root_detects_expected_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# x\n", encoding="utf-8")
            (root / "app").mkdir()
            (root / "app" / "builder.py").write_text("print('x')\n", encoding="utf-8")
            (root / "src" / "core").mkdir(parents=True)
            (root / "src" / "core" / "config.py").write_text("ROOT='x'\n", encoding="utf-8")

            self.assertTrue(_looks_like_repo_root(root))

    def test_discover_root_prefers_repo_layout_from_cwd_parents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# x\n", encoding="utf-8")
            (root / "app").mkdir()
            (root / "app" / "builder.py").write_text("print('x')\n", encoding="utf-8")
            (root / "src" / "core").mkdir(parents=True)
            (root / "src" / "core" / "config.py").write_text("ROOT='x'\n", encoding="utf-8")
            nested = root / "nested" / "deeper"
            nested.mkdir(parents=True)

            previous = Path.cwd()
            try:
                import os
                os.chdir(nested)
                self.assertEqual(_discover_root(), root.resolve())
            finally:
                os.chdir(previous)

    def test_unusable_cross_os_cli_path_rejects_windows_appdata_path_on_posix(self) -> None:
        with patch("src.core.config.os.name", "posix"):
            self.assertTrue(
                _is_unusable_cross_os_cli_path("/mnt/c/Users/USER/AppData/Roaming/npm/codex")
            )

    def test_cli_resolution_uses_path_when_env_override_is_broken(self) -> None:
        with patch.dict(os.environ, {"CODEX_CMD": "/missing/codex"}, clear=False), patch(
            "src.core.config.shutil.which",
            side_effect=lambda name: "/usr/bin/codex" if name == "codex" else None,
        ):
            resolved = _resolve_cli_command_details("CODEX_CMD", "codex")

        self.assertEqual(resolved.command, "/usr/bin/codex")
        self.assertEqual(resolved.source, "path")
        self.assertIn("was set but not found", " ".join(resolved.notes))

    def test_cli_resolution_uses_common_fallback_when_path_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fallback = Path(tmp) / "codex"
            fallback.write_text("#!/bin/sh\n", encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_CMD": ""}, clear=False), patch(
                "src.core.config.shutil.which", return_value=None
            ), patch(
                "src.core.config._common_cli_fallback_candidates", return_value=[str(fallback)]
            ):
                resolved = _resolve_cli_command_details("CODEX_CMD", "codex")

        self.assertEqual(resolved.command, str(fallback))
        self.assertEqual(resolved.source, "fallback")

    def test_common_cli_fallback_candidates_include_user_local_bins(self) -> None:
        with patch("src.core.config.os.name", "posix"):
            candidates = _common_cli_fallback_candidates("codex")

        self.assertIn(str(Path.home() / ".local" / "bin" / "codex"), candidates)
        self.assertIn(str(Path.home() / ".local" / "npm" / "bin" / "codex"), candidates)
