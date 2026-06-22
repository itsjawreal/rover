from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.core.config as config
import src.contrib.contribution_store as contribution_store


class StorageConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        importlib.reload(config)
        importlib.reload(contribution_store)

    def test_rover_home_override_controls_storage_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rover_home = Path(tmp) / "rover-home"
            with mock.patch.dict(
                os.environ,
                {
                    "ROVER_HOME": str(rover_home),
                    "ROVER_STORAGE_MODE": "persistent",
                },
                clear=False,
            ):
                for key in (
                    "ROVER_STATE_DIR",
                    "ROVER_CACHE_DIR",
                    "ROVER_ARTIFACT_DIR",
                    "ROVER_CONFIG_DIR",
                ):
                    os.environ.pop(key, None)
                reloaded_config = importlib.reload(config)
                reloaded_store = importlib.reload(contribution_store)

            self.assertEqual(reloaded_config.ROVER_HOME, rover_home.resolve())
            self.assertEqual(reloaded_config.ROVER_STATE_DIR, rover_home.resolve() / "state")
            self.assertEqual(reloaded_config.ROVER_CACHE_DIR, rover_home.resolve() / "cache")
            self.assertEqual(reloaded_config.ROVER_ARTIFACT_DIR, rover_home.resolve() / "artifacts")
            self.assertEqual(reloaded_config.PR_LOG_FILE, rover_home.resolve() / "state" / "pr_log.json")
            self.assertEqual(
                reloaded_store.PR_ENGINE_DB_FILE,
                rover_home.resolve() / "state" / "pr_engine.sqlite3",
            )

    def test_workspace_storage_mode_uses_repo_local_dot_rover(self) -> None:
        with mock.patch.dict(os.environ, {"ROVER_STORAGE_MODE": "workspace"}, clear=False):
            for key in (
                "ROVER_HOME",
                "ROVER_STATE_DIR",
                "ROVER_CACHE_DIR",
                "ROVER_ARTIFACT_DIR",
                "ROVER_CONFIG_DIR",
            ):
                os.environ.pop(key, None)
            reloaded_config = importlib.reload(config)

        self.assertEqual(reloaded_config.ROVER_STORAGE_MODE, "workspace")
        self.assertEqual(reloaded_config.ROVER_HOME, (reloaded_config.ROOT / ".rover").resolve())
        self.assertEqual(
            reloaded_config.ROVER_ARTIFACT_DIR,
            (reloaded_config.ROOT / ".rover" / "artifacts").resolve(),
        )


if __name__ == "__main__":
    unittest.main()
