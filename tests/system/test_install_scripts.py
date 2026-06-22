from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class InstallScriptTests(unittest.TestCase):
    def test_vps_install_script_exists_and_bootstraps_core_steps(self) -> None:
        script = REPO_ROOT / "scripts" / "install_vps.sh"
        text = script.read_text(encoding="utf-8")

        self.assertTrue(script.exists())
        self.assertIn("python3 -m venv", text)
        self.assertIn("python -m pip install -e \"$ROOT_DIR\"", text)
        self.assertIn("rover doctor", text)
        self.assertIn("gh auth login", text)
        self.assertIn("GITHUB_TOKEN", text)
        self.assertIn("Select GitHub auth mode for Rover:", text)
        self.assertIn("Token in .env only", text)
        self.assertIn("gh auth login only", text)
        self.assertIn("Both token + gh auth login", text)
        self.assertIn("OPENAI_API_KEY", text)
        self.assertIn("codex login", text)
        self.assertIn("Select your primary AI backend for this machine", text)
        self.assertIn("Claude CLI", text)
        self.assertIn("LLM API key only", text)
        self.assertIn("Use existing value from .env", text)
        self.assertIn("Replace with a new value", text)
        self.assertIn("Clear saved value and continue without it", text)
        self.assertIn("Choose how to handle existing Codex auth", text)
        self.assertIn("Found Codex CLI at $existing_cmd from a Windows-mounted PATH.", text)
        self.assertIn("That Codex binary cannot complete Linux/WSL auth here.", text)
        self.assertIn("No usable Linux/WSL Codex CLI was found, so device auth was skipped", text)
        self.assertIn("No usable Linux/WSL Codex CLI was found, so browser login was skipped", text)
        self.assertIn("update_env \"CODEX_CMD\" \"$usable_cmd\"", text)
        self.assertIn("ensure_user_npm_global()", text)
        self.assertIn("export npm_config_prefix=\"$desired_prefix\"", text)
        self.assertIn("export NPM_CONFIG_PREFIX=\"$desired_prefix\"", text)
        self.assertIn("using temporary npm global prefix at $desired_prefix", text)
        self.assertIn("Install Rover OpenClaw skill, wrapper, and mcp.servers.rover now?", text)
        self.assertIn("src/platform/openclaw_install.py", text)
        self.assertIn("venv_activate_script", text)
        self.assertIn("Use arrow keys to move. Press Enter to select.", text)
        self.assertIn("Prompt input was interrupted or the terminal is no longer interactive. Stopping setup.", text)
        self.assertIn("return 130", text)
        self.assertIn("trap on_interrupt INT", text)
        self.assertIn("Setup interrupted by user.", text)
        self.assertIn("Codex browser login was interrupted. Stopping setup.", text)
        self.assertIn("Codex device auth was interrupted. Stopping setup.", text)
        self.assertIn("printf '%s' \"${options[0]}\"", text)
        self.assertNotIn("printf '%s' \"1\"", text)

    def test_remote_bootstrap_script_exists(self) -> None:
        script = REPO_ROOT / "scripts" / "bootstrap.sh"
        text = script.read_text(encoding="utf-8")

        self.assertTrue(script.exists())
        self.assertIn("rover.git", text)
        self.assertIn("git clone", text)
        self.assertIn("install_vps.sh", text)
        self.assertIn("git -C", text)

    def test_uninstall_script_resets_local_rover_artifacts(self) -> None:
        script = REPO_ROOT / "scripts" / "uninstall_vps.sh"
        text = script.read_text(encoding="utf-8")

        self.assertTrue(script.exists())
        self.assertIn(".venv", text)
        self.assertIn(".mcp.json", text)
        self.assertIn("data", text)
        self.assertIn("logs", text)
        self.assertIn("runs", text)
        self.assertIn(".stream_partials", text)
        self.assertIn("skills/rover", text)
        self.assertIn("rover.py", text)
        self.assertIn("github-contribution-engine", text)
        self.assertIn("contribution.py", text)
        self.assertIn("gh auth logout -h github.com", text)
        self.assertIn("bash scripts/install_vps.sh", text)
        self.assertIn("Choosing Yes will permanently delete the selected local Rover files or directories.", text)
        self.assertIn("Continue uninstall/reset", text)
        self.assertIn("Cancel and keep everything", text)
        self.assertIn("No changes were made. Rover uninstall/reset was skipped.", text)
        self.assertIn("Use arrow keys to move. Press Enter to select.", text)
        self.assertIn('"Yes" "No"', text)

    def test_windows_install_script_offers_guided_auth_and_backend_setup(self) -> None:
        script = REPO_ROOT / "scripts" / "install_windows.ps1"
        text = script.read_text(encoding="utf-8")

        self.assertTrue(script.exists())
        self.assertIn("Select GitHub auth mode for Rover:", text)
        self.assertIn("-m pip install -e $RootDir", text)
        self.assertIn('Choose how to handle ${Key}:', text)
        self.assertIn("Token in .env only", text)
        self.assertIn("gh auth login only", text)
        self.assertIn("Both token + gh auth login", text)
        self.assertIn("Select your primary AI backend for this machine:", text)
        self.assertIn("Codex CLI", text)
        self.assertIn("Claude CLI", text)
        self.assertIn("LLM API key only", text)
        self.assertIn("Install Rover OpenClaw skill, wrapper, and mcp.servers.rover now?", text)
        self.assertIn("Generate local .mcp.json for this Windows workspace?", text)
        self.assertIn("Use arrow keys to move. Press Enter to select.", text)

    def test_windows_uninstall_script_resets_local_rover_artifacts(self) -> None:
        script = REPO_ROOT / "scripts" / "uninstall_windows.ps1"
        text = script.read_text(encoding="utf-8")

        self.assertTrue(script.exists())
        self.assertIn(".venv", text)
        self.assertIn(".mcp.json", text)
        self.assertIn("data", text)
        self.assertIn("logs", text)
        self.assertIn("runs", text)
        self.assertIn(".stream_partials", text)
        self.assertIn("skills\\rover", text)
        self.assertIn("rover.py", text)
        self.assertIn("github-contribution-engine", text)
        self.assertIn("contribution.py", text)
        self.assertIn("gh auth logout -h github.com", text)
        self.assertIn("install_windows.ps1", text)
        self.assertIn("Choosing Yes will permanently delete the selected local Rover files or directories.", text)
        self.assertIn("Continue uninstall/reset", text)
        self.assertIn("Cancel and keep everything", text)
        self.assertIn("No changes were made. Rover uninstall/reset was skipped.", text)
        self.assertIn("Use arrow keys to move. Press Enter to select.", text)
