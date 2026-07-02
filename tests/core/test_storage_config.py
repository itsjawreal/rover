from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.core.config as config
import src.contrib.contribution_store as contribution_store

_STORAGE_ENV_KEYS = (
    "MENISIK_HOME",
    "MENISIK_STATE_DIR",
    "MENISIK_CACHE_DIR",
    "MENISIK_ARTIFACT_DIR",
    "MENISIK_CONFIG_DIR",
    "ROVER_HOME",
    "ROVER_STATE_DIR",
    "ROVER_CACHE_DIR",
    "ROVER_ARTIFACT_DIR",
    "ROVER_CONFIG_DIR",
)


class StorageConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        importlib.reload(config)
        importlib.reload(contribution_store)

    def test_menisik_home_override_controls_storage_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            menisik_home = Path(tmp) / "menisik-home"
            with mock.patch.dict(
                os.environ,
                {
                    "MENISIK_HOME": str(menisik_home),
                    "MENISIK_STORAGE_MODE": "persistent",
                },
                clear=False,
            ):
                for key in _STORAGE_ENV_KEYS:
                    if key not in {"MENISIK_HOME"}:
                        os.environ.pop(key, None)
                reloaded_config = importlib.reload(config)
                reloaded_store = importlib.reload(contribution_store)

            self.assertEqual(reloaded_config.MENISIK_HOME, menisik_home.resolve())
            self.assertEqual(reloaded_config.MENISIK_STATE_DIR, menisik_home.resolve() / "state")
            self.assertEqual(reloaded_config.MENISIK_CACHE_DIR, menisik_home.resolve() / "cache")
            self.assertEqual(reloaded_config.MENISIK_ARTIFACT_DIR, menisik_home.resolve() / "artifacts")
            self.assertEqual(reloaded_config.PR_LOG_FILE, menisik_home.resolve() / "state" / "pr_log.json")
            self.assertEqual(
                reloaded_store.PR_ENGINE_DB_FILE,
                menisik_home.resolve() / "state" / "pr_engine.sqlite3",
            )

    def test_legacy_rover_home_env_still_works_as_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rover_home = Path(tmp) / "rover-home"
            with mock.patch.dict(
                os.environ,
                {
                    "ROVER_HOME": str(rover_home),
                    "MENISIK_STORAGE_MODE": "persistent",
                },
                clear=False,
            ):
                for key in _STORAGE_ENV_KEYS:
                    if key != "ROVER_HOME":
                        os.environ.pop(key, None)
                reloaded_config = importlib.reload(config)

            self.assertEqual(reloaded_config.MENISIK_HOME, rover_home.resolve())
            self.assertEqual(reloaded_config.MENISIK_STATE_DIR, rover_home.resolve() / "state")

    def test_workspace_storage_mode_uses_repo_local_dot_menisik(self) -> None:
        with mock.patch.dict(os.environ, {"MENISIK_STORAGE_MODE": "workspace"}, clear=False):
            for key in _STORAGE_ENV_KEYS:
                os.environ.pop(key, None)
            reloaded_config = importlib.reload(config)

        self.assertEqual(reloaded_config.MENISIK_STORAGE_MODE, "workspace")
        self.assertEqual(reloaded_config.MENISIK_HOME, (reloaded_config.ROOT / ".menisik").resolve())
        self.assertEqual(
            reloaded_config.MENISIK_ARTIFACT_DIR,
            (reloaded_config.ROOT / ".menisik" / "artifacts").resolve(),
        )


class LegacyStorageMigrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        importlib.reload(config)
        importlib.reload(contribution_store)

    def test_adopt_legacy_home_moves_rover_dir_when_menisik_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / ".rover"
            (legacy / "state").mkdir(parents=True)
            (legacy / "state" / "pr_log.json").write_text("{}", encoding="utf-8")
            new = Path(tmp) / ".menisik"

            result = config._adopt_legacy_home(new, legacy)

            self.assertEqual(result, new)
            self.assertFalse(legacy.exists())
            self.assertTrue((new / "state" / "pr_log.json").exists())

    def test_adopt_legacy_home_keeps_menisik_dir_when_both_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / ".rover"
            legacy.mkdir()
            (legacy / "old-marker").write_text("old", encoding="utf-8")
            new = Path(tmp) / ".menisik"
            new.mkdir()
            (new / "new-marker").write_text("new", encoding="utf-8")

            result = config._adopt_legacy_home(new, legacy)

            self.assertEqual(result, new)
            self.assertTrue(legacy.exists())
            self.assertTrue((new / "new-marker").exists())
            self.assertFalse((new / "old-marker").exists())

    def test_adopt_legacy_home_falls_back_to_legacy_when_move_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / ".rover"
            legacy.mkdir()
            new = Path(tmp) / ".menisik"
            with (
                mock.patch.object(Path, "rename", side_effect=OSError("locked")),
                mock.patch("src.core.config.shutil.move", side_effect=OSError("locked")),
            ):
                result = config._adopt_legacy_home(new, legacy)

            self.assertEqual(result, legacy)
            self.assertTrue(legacy.exists())

    def test_workspace_mode_migrates_repo_local_dot_rover(self) -> None:
        # ROOT/.rover should be adopted as ROOT/.menisik on startup.
        root = config.ROOT
        legacy = root / ".rover"
        new = root / ".menisik"
        if new.exists() or legacy.exists():
            self.skipTest("repo-local storage dirs already exist; skipping to avoid touching real state")
        legacy.mkdir()
        (legacy / "marker.txt").write_text("data", encoding="utf-8")
        try:
            with mock.patch.dict(os.environ, {"MENISIK_STORAGE_MODE": "workspace"}, clear=False):
                for key in _STORAGE_ENV_KEYS:
                    os.environ.pop(key, None)
                reloaded_config = importlib.reload(config)

            self.assertEqual(reloaded_config.MENISIK_HOME, new.resolve())
            self.assertTrue((new / "marker.txt").exists())
            self.assertFalse(legacy.exists())
        finally:
            import shutil as _shutil

            _shutil.rmtree(new, ignore_errors=True)
            _shutil.rmtree(legacy, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
