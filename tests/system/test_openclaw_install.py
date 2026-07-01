from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.platform.openclaw_install import install_openclaw_assets


class OpenClawInstallTests(unittest.TestCase):
    def test_install_writes_canonical_workspace_skill_wrapper_and_mcp_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_path, tool_path = install_openclaw_assets(
                rover_bin="/srv/engine/.venv/bin/rover",
                python_bin="/srv/engine/.venv/bin/python",
                rover_mcp_bin="/srv/engine/.venv/bin/rover-mcp",
                openclaw_root=str(root / ".openclaw"),
                openclaw_workspace=str(root / ".openclaw" / "workspace"),
            )

            self.assertEqual(skill_path, root / ".openclaw" / "workspace" / "skills" / "rover" / "SKILL.md")
            self.assertEqual(tool_path, root / ".openclaw" / "tools" / "rover.py")
            self.assertTrue(skill_path.exists())
            self.assertTrue(tool_path.exists())

            skill_text = skill_path.read_text(encoding="utf-8")
            tool_text = tool_path.read_text(encoding="utf-8")
            config = json.loads((root / ".openclaw" / "openclaw.json").read_text(encoding="utf-8"))

            self.assertIn("name: rover", skill_text)
            self.assertIn("Rover is the canonical integration name", skill_text)
            self.assertIn("Do not write files into `~/.openclaw/sandboxes/...`", skill_text)
            self.assertIn("If the user message starts with `rover `, treat it as an explicit Rover command surface", skill_text)
            self.assertIn("After `/new`, still treat `rover ...` as a built-in command prefix", skill_text)
            self.assertIn("Map exact Rover prefix commands directly", skill_text)
            self.assertIn("`rover profile` -> `profile`", skill_text)
            self.assertIn("For `rover profile`, return Rover profile output only", skill_text)
            self.assertIn("If the user message starts with `rover scan`, treat it as an exact scan command", skill_text)
            self.assertIn("For `rover scan ...`, return Rover scan output only", skill_text)
            self.assertIn("For `rover scan ...`, never fall back to repo inspection", skill_text)
            self.assertIn("For `rover scan trust ...` and `rover scan audit ...`, do not claim that Python or TypeScript files are required", skill_text)
            self.assertIn("Treat `run ...` contribution requests as live submission attempts", skill_text)
            self.assertIn("If a live Rover run is accepted, send one short acknowledgement with the `run_id`", skill_text)
            self.assertIn("Do not improvise with `gh`, manual issue browsing, direct GitHub checks", skill_text)
            self.assertIn("Do not emit multiple assistant progress messages for the same run", skill_text)
            self.assertIn("If Rover returns `accepted=false`, `status=blocked`, or `outcome_code=blocked_ineligible_repo`", skill_text)
            self.assertIn("Never invent in-progress activity for a blocked run", skill_text)
            self.assertIn("Never output placeholders such as `<work_in_progress>`", skill_text)
            self.assertIn("Do not suggest `override_limits`, forced targeted runs, or bypassing guardrails", skill_text)
            self.assertIn("live one targeted contribution: `contrib_targeted --repo owner/repo --count 1 --live`", skill_text)
            self.assertIn("`rover profile`", skill_text)
            self.assertIn("`rover scan security owner/repo`", skill_text)
            self.assertIn("audit scan: `scan --repo owner/repo --kind audit`", skill_text)
            self.assertIn("`rover run owner/repo bugfix`", skill_text)
            self.assertIn("wrapper invoke: `/tmp", skill_text.replace("\\", "/"))
            self.assertIn(".openclaw/tools/rover.py", skill_text.replace("\\", "/"))
            self.assertIn("ROVER_BIN_CANDIDATES", tool_text)
            self.assertIn("/srv/engine/.venv/bin/rover", tool_text)
            self.assertIn("resolve_rover_bin()", tool_text)
            self.assertIn('return run_rover(["doctor", "--json"])', tool_text)
            self.assertIn('return run_rover(["profile", "--json"])', tool_text)
            self.assertIn('scan = sub.add_parser("scan")', tool_text)
            self.assertIn('return run_rover(["scan", args.repo, "--kind", args.kind, "--json"])', tool_text)
            self.assertIn('return run_rover(["--command-text", args.text, "--route-only", "--json"])', tool_text)
            self.assertEqual(config["mcp"]["servers"]["rover"]["command"], "/srv/engine/.venv/bin/rover-mcp")
            self.assertTrue(config["skills"]["entries"]["rover"]["enabled"])

    def test_install_preserves_unrelated_openclaw_json_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".openclaw" / "openclaw.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps({"channels": {"whatsapp": {"enabled": True}}, "mcp": {"servers": {"other": {"command": "x"}}}}, indent=2),
                encoding="utf-8",
            )

            install_openclaw_assets(
                rover_bin="/srv/engine/.venv/bin/rover",
                python_bin="/srv/engine/.venv/bin/python",
                rover_mcp_bin="/srv/engine/.venv/bin/rover-mcp",
                openclaw_root=str(root / ".openclaw"),
            )

            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(config["channels"]["whatsapp"]["enabled"])
            self.assertEqual(config["mcp"]["servers"]["other"]["command"], "x")
            self.assertEqual(config["mcp"]["servers"]["rover"]["command"], "/srv/engine/.venv/bin/rover-mcp")

    def test_install_rejects_corrupt_openclaw_json_without_overwriting_it(self) -> None:
        # Regression: invalid JSON crashed with a raw JSONDecodeError; it must fail
        # with a clear message and must not clobber the operator's hand-edited file.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".openclaw" / "openclaw.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text('{"channels": {broken', encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                install_openclaw_assets(
                    rover_bin="/srv/engine/.venv/bin/rover",
                    python_bin="/srv/engine/.venv/bin/python",
                    rover_mcp_bin="/srv/engine/.venv/bin/rover-mcp",
                    openclaw_root=str(root / ".openclaw"),
                )

            self.assertIn("not valid JSON", str(ctx.exception))
            self.assertEqual(config_path.read_text(encoding="utf-8"), '{"channels": {broken')

    def test_install_writes_compatibility_skill_without_legacy_home_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_openclaw_assets(
                rover_bin="/srv/engine/.venv/bin/rover",
                python_bin="/srv/engine/.venv/bin/python",
                rover_mcp_bin="/srv/engine/.venv/bin/rover-mcp",
                openclaw_root=str(root / ".openclaw"),
            )

            self.assertTrue((root / ".openclaw" / "skills" / "github-contribution-engine" / "SKILL.md").exists())
            self.assertFalse((root / "openclaw").exists())
