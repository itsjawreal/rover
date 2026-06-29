from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class MCPInstallTests(unittest.TestCase):
    def _install(self, *, is_wsl: bool, distro: str = "Ubuntu-20.04") -> dict:
        from src.platform.mcp_install import install_mcp
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {**os.environ, "WSL_DISTRO_NAME": distro}
            with patch("src.platform.mcp_install._is_wsl", return_value=is_wsl), \
                 patch.dict(os.environ, {"WSL_DISTRO_NAME": distro}):
                out = install_mcp(root)
            return json.loads(out.read_text(encoding="utf-8"))

    def test_wsl_config_uses_wsl_command(self) -> None:
        config = self._install(is_wsl=True, distro="Ubuntu-20.04")
        server = config["mcpServers"]["rover"]
        self.assertEqual(server["command"], "wsl")
        self.assertIn("-d", server["args"])
        self.assertIn("Ubuntu-20.04", server["args"])
        self.assertEqual(server["type"], "stdio")

    def test_wsl_config_contains_mcp_server_module(self) -> None:
        config = self._install(is_wsl=True)
        bash_cmd = config["mcpServers"]["rover"]["args"][-1]
        self.assertIn("src.contribution_mcp.server", bash_cmd)

    def test_non_wsl_config_uses_python_executable(self) -> None:
        config = self._install(is_wsl=False)
        server = config["mcpServers"]["rover"]
        self.assertEqual(server["command"], sys.executable)
        self.assertIn("-m", server["args"])
        self.assertIn("src.contribution_mcp.server", server["args"])
        self.assertIn("cwd", server)

    def test_output_file_is_mcp_json(self) -> None:
        from src.platform.mcp_install import install_mcp
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("src.platform.mcp_install._is_wsl", return_value=False):
                out = install_mcp(root)
            self.assertEqual(out.name, ".mcp.json")
            self.assertTrue(out.exists())

    def test_output_is_valid_json(self) -> None:
        from src.platform.mcp_install import install_mcp
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("src.platform.mcp_install._is_wsl", return_value=False):
                out = install_mcp(root)
            parsed = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("mcpServers", parsed)
            self.assertIn("rover", parsed["mcpServers"])

    def test_wsl_distro_name_from_env(self) -> None:
        config = self._install(is_wsl=True, distro="Debian")
        args = config["mcpServers"]["rover"]["args"]
        distro_index = args.index("-d") + 1
        self.assertEqual(args[distro_index], "Debian")

    def test_is_wsl_false_when_proc_version_missing(self) -> None:
        from src.platform.mcp_install import _is_wsl
        with patch("src.platform.mcp_install.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            result = _is_wsl()
        self.assertFalse(result)

    def test_wsl_bash_command_quotes_path_with_spaces(self) -> None:
        from src.platform.mcp_install import install_mcp
        import tempfile, shlex
        with tempfile.TemporaryDirectory() as tmp:
            spaced = Path(tmp) / "my project"
            spaced.mkdir()
            with patch("src.platform.mcp_install._is_wsl", return_value=True), \
                 patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu-20.04"}):
                out = install_mcp(spaced)
            config = json.loads(out.read_text(encoding="utf-8"))
        bash_cmd = config["mcpServers"]["rover"]["args"][-1]
        linux_path = str(spaced).replace("\\", "/")
        self.assertIn(shlex.quote(linux_path), bash_cmd)
