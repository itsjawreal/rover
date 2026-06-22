from __future__ import annotations

import io
import unittest
from unittest import mock

from rich.console import Console

from src.core.cli_ui import print_styled_doctor
from src.core.doctor import DoctorCheck


class DoctorUITests(unittest.TestCase):
    def test_default_doctor_view_does_not_repeat_backend_auth_notes(self) -> None:
        checks = [
            DoctorCheck("python", "ok", "running Python 3.10.12"),
            DoctorCheck("workspace", "ok", "root=/tmp/repo"),
            DoctorCheck("entrypoint", "ok", "argv0=/tmp/repo/.venv/bin/rover | python=/usr/bin/python3 | rover=/tmp/repo/.venv/bin/rover | root=/tmp/repo"),
            DoctorCheck("github-cli", "ok", "gh found on PATH"),
            DoctorCheck("github-auth", "ok", "github.com"),
            DoctorCheck("github-token", "ok", "GH_TOKEN is set"),
            DoctorCheck("storage-mode", "ok", "mode=persistent home=/tmp/.rover"),
            DoctorCheck("storage-state", "ok", "state=/tmp/.rover/state"),
            DoctorCheck("storage-cache", "ok", "cache=/tmp/.rover/cache"),
            DoctorCheck("storage-artifacts", "ok", "artifacts=/tmp/.rover/artifacts"),
            DoctorCheck("storage-config", "ok", "config=/tmp/.rover/config"),
            DoctorCheck("ai-backend", "ok", "configured backend=codex runtime=codex-cli"),
            DoctorCheck("codex-cli", "ok", "CODEX_CMD='codex' available"),
            DoctorCheck("codex-auth", "warn", "Codex CLI returned an unexpected auth error; run `codex login status` manually"),
            DoctorCheck("agent-runtime", "ok", "agent_tool=Codex backend=codex-cli support=tested"),
            DoctorCheck("selected-backend", "ok", "codex-cli available for the active runtime path"),
            DoctorCheck("selected-backend-auth", "warn", "Codex CLI returned an unexpected auth error; run `codex login status` manually"),
            DoctorCheck("openclaw-skill", "ok", "/tmp/.openclaw/skills/rover/SKILL.md"),
            DoctorCheck("openclaw-wrapper", "ok", "/tmp/.openclaw/tools/rover.py"),
            DoctorCheck("openclaw-mcp", "ok", "/tmp/.openclaw/openclaw.json → mcp.servers.rover"),
            DoctorCheck("hermes-mcp", "ok", "/tmp/.hermes/config.yaml → mcp_servers.rover"),
            DoctorCheck("notify-route", "ok", "transport=openclaw channel=telegram target=-100123 interval=60s progress=on"),
            DoctorCheck("notify-transport", "ok", "OPENCLAW_CMD='openclaw' available (resolved from PATH) | events=started,repo_selected"),
            DoctorCheck("openclaw-legacy", "ok", "no stale ~/openclaw install detected"),
        ]
        stream = io.StringIO()
        console = Console(file=stream, force_terminal=False, color_system=None, width=140)

        with mock.patch("src.core.cli_ui._console", console):
            print_styled_doctor(checks)

        output = stream.getvalue()
        self.assertIn("backend auth", output)
        self.assertIn("agents", output)
        self.assertNotIn("Additional notes", output)
        self.assertEqual(output.count("Codex CLI returned an unexpected auth error"), 1)


if __name__ == "__main__":
    unittest.main()
