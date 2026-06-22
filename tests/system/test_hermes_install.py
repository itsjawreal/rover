from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.platform.hermes_install import install_hermes_config


class HermesInstallTests(unittest.TestCase):
    def test_install_writes_rover_mcp_server_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            written = install_hermes_config(
                rover_mcp_bin="/srv/engine/.venv/bin/rover-mcp",
                hermes_config_path=str(config_path),
            )

            self.assertEqual(written, config_path)
            text = config_path.read_text(encoding="utf-8")
            self.assertIn("mcp_servers:", text)
            self.assertIn("  rover:", text)
            self.assertIn('command: "/srv/engine/.venv/bin/rover-mcp"', text)

    def test_install_preserves_existing_servers_and_replaces_rover_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                "mcp_servers:\n"
                "  other:\n"
                '    command: "other-mcp"\n'
                "  rover:\n"
                '    command: "old-rover"\n'
                "    args: []\n",
                encoding="utf-8",
            )

            install_hermes_config(
                rover_mcp_bin="/srv/engine/.venv/bin/rover-mcp",
                hermes_config_path=str(config_path),
            )

            text = config_path.read_text(encoding="utf-8")
            self.assertIn('command: "other-mcp"', text)
            self.assertIn('command: "/srv/engine/.venv/bin/rover-mcp"', text)
            self.assertNotIn('command: "old-rover"', text)
