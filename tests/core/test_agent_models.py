from __future__ import annotations

import unittest

from src.core.agent_models import (
    get_agent_tool_support,
    get_backend_label,
    SUPPORTED_AGENT_TOOLS,
    SUPPORTED_MODEL_SERIES,
    get_runtime_profile,
    supports_agent_tool,
    supports_model_series,
)


class AgentModelSupportTests(unittest.TestCase):
    def test_supported_agent_tools_include_form_targets(self) -> None:
        self.assertIn("Codex", SUPPORTED_AGENT_TOOLS)
        self.assertIn("Claude Code", SUPPORTED_AGENT_TOOLS)
        self.assertIn("Aider", SUPPORTED_AGENT_TOOLS)

    def test_supported_model_series_include_form_targets(self) -> None:
        self.assertIn("GPT", SUPPORTED_MODEL_SERIES)
        self.assertIn("Claude", SUPPORTED_MODEL_SERIES)
        self.assertIn("Gemini", SUPPORTED_MODEL_SERIES)

    def test_support_lookup_accepts_known_values(self) -> None:
        self.assertTrue(supports_agent_tool("Codex"))
        self.assertTrue(supports_model_series("GPT"))

    def test_runtime_profile_has_non_empty_labels(self) -> None:
        profile = get_runtime_profile()
        self.assertTrue(profile.agent_tool)
        self.assertTrue(profile.model_series)
        self.assertTrue(profile.backend)
        self.assertTrue(profile.support_level)

    def test_backend_label_matches_supported_defaults(self) -> None:
        self.assertIn(get_backend_label(), {"codex-cli", "claude-cli"})

    def test_agent_tool_support_levels_are_honest(self) -> None:
        self.assertEqual(get_agent_tool_support("Codex").support_level, "tested")
        self.assertEqual(get_agent_tool_support("Claude Code").support_level, "supported")
        self.assertEqual(get_agent_tool_support("Aider").support_level, "label-only")
