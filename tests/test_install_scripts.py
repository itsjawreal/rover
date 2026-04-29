from __future__ import annotations

import unittest
from pathlib import Path


class InstallScriptTests(unittest.TestCase):
    def test_vps_install_script_exists_and_bootstraps_core_steps(self) -> None:
        script = Path("scripts/install_vps.sh")
        text = script.read_text(encoding="utf-8")

        self.assertTrue(script.exists())
        self.assertIn("python3 -m venv", text)
        self.assertIn("python -m pip install \"$ROOT_DIR\"", text)
        self.assertIn("github-contribution-engine --doctor", text)
        self.assertIn("gh auth login", text)
        self.assertIn("GITHUB_TOKEN", text)
        self.assertIn("OPENAI_API_KEY", text)
        self.assertIn("codex login", text)
        self.assertIn("Select your primary AI backend for this machine", text)
        self.assertIn("Claude CLI", text)
        self.assertIn("LLM API key only", text)
        self.assertIn("Use existing value from .env", text)
        self.assertIn("Replace with a new value", text)
        self.assertIn("Clear saved value and continue without it", text)
        self.assertIn("Choose how to handle existing Codex auth", text)
        self.assertIn("Install OpenClaw skill and wrapper now?", text)
        self.assertIn("src/openclaw_install.py", text)
        self.assertIn("venv_activate_script", text)
        self.assertIn("Use arrow keys, then press Enter.", text)

    def test_remote_bootstrap_script_exists(self) -> None:
        script = Path("scripts/bootstrap.sh")
        text = script.read_text(encoding="utf-8")

        self.assertTrue(script.exists())
        self.assertIn("github-contribution-engine.git", text)
        self.assertIn("git clone", text)
        self.assertIn("install_vps.sh", text)
        self.assertIn("git -C", text)
