"""Generate a machine-specific .mcp.json for Claude Code and other MCP clients."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path


def _wsl_distro() -> str:
    try:
        result = subprocess.run(
            ["bash", "-c", "cat /etc/os-release | grep '^PRETTY_NAME' | cut -d= -f2 | tr -d '\"'"],
            capture_output=True, text=True, timeout=5,
        )
        name = result.stdout.strip()
        if name:
            return name
    except Exception:
        pass
    return os.environ.get("WSL_DISTRO_NAME", "Ubuntu")


def _is_wsl() -> bool:
    return "microsoft" in Path("/proc/version").read_text(errors="ignore").lower() if Path("/proc/version").exists() else False


def install_mcp(project_root: Path) -> Path:
    out = project_root / ".mcp.json"
    distro = os.environ.get("WSL_DISTRO_NAME") or _wsl_distro()
    linux_path = str(project_root).replace("\\", "/")

    if _is_wsl():
        config = {
            "mcpServers": {
                "rover": {
                    "command": "wsl",
                    "args": [
                        "-d", distro,
                        "--", "bash", "-c",
                        f"cd {shlex.quote(linux_path)} && python3 -m src.contribution_mcp.server",
                    ],
                    "type": "stdio",
                }
            }
        }
    else:
        import sys
        python = sys.executable
        config = {
            "mcpServers": {
                "rover": {
                    "command": python,
                    "args": ["-m", "src.contribution_mcp.server"],
                    "cwd": linux_path,
                    "type": "stdio",
                }
            }
        }

    out.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return out
