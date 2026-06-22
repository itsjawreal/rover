from __future__ import annotations

from dataclasses import dataclass

from src.core.config import AGENT_TOOL, AI_BACKEND, MODEL_SERIES


SUPPORTED_AGENT_TOOLS = [
    "Codex",
    "Claude Code",
    "OpenRouter",
    "OpenCode",
    "Aider",
    "Cline",
    "Cursor",
    "Windsurf",
    "Other",
]

SUPPORTED_MODEL_SERIES = [
    "GPT",
    "Claude",
    "Gemini",
    "DeepSeek",
    "MiMo",
    "Doubao",
    "MiniMax",
    "Other",
]


@dataclass(frozen=True)
class AgentToolSupport:
    name: str
    support_level: str
    backend_family: str
    note: str


@dataclass(frozen=True)
class AgentRuntimeProfile:
    agent_tool: str
    model_series: str
    backend: str
    support_level: str
    support_note: str


AGENT_TOOL_SUPPORT = {
    "Codex": AgentToolSupport(
        name="Codex",
        support_level="tested",
        backend_family="codex-cli",
        note="Default tested path for contribution runs.",
    ),
    "Claude Code": AgentToolSupport(
        name="Claude Code",
        support_level="supported",
        backend_family="claude-cli",
        note="Supported fallback path for operators who choose Claude CLI.",
    ),
    "OpenRouter": AgentToolSupport(
        name="OpenRouter",
        support_level="supported",
        backend_family="openrouter-api",
        note="OpenAI-compatible HTTP backend (OpenRouter and similar); no CLI needed.",
    ),
    "OpenCode": AgentToolSupport(
        name="OpenCode",
        support_level="label-only",
        backend_family="external",
        note="User-facing label only until a real OpenCode adapter exists.",
    ),
    "Aider": AgentToolSupport(
        name="Aider",
        support_level="label-only",
        backend_family="external",
        note="User-facing label only until a real Aider adapter exists.",
    ),
    "Cline": AgentToolSupport(
        name="Cline",
        support_level="label-only",
        backend_family="external",
        note="User-facing label only until a real Cline adapter exists.",
    ),
    "Cursor": AgentToolSupport(
        name="Cursor",
        support_level="label-only",
        backend_family="external",
        note="User-facing label only until a real Cursor adapter exists.",
    ),
    "Windsurf": AgentToolSupport(
        name="Windsurf",
        support_level="label-only",
        backend_family="external",
        note="User-facing label only until a real Windsurf adapter exists.",
    ),
    "Other": AgentToolSupport(
        name="Other",
        support_level="label-only",
        backend_family="external",
        note="Custom label only; pair it with a real backend before claiming runtime support.",
    ),
}


def get_backend_label(ai_backend: str | None = None) -> str:
    selected = (ai_backend or AI_BACKEND or "codex").strip().lower()
    if selected == "claude":
        return "claude-cli"
    if selected == "openrouter":
        return "openrouter-api"
    return "codex-cli"


def get_agent_tool_support(name: str) -> AgentToolSupport:
    return AGENT_TOOL_SUPPORT.get(
        name,
        AgentToolSupport(
            name=name or "Other",
            support_level="label-only",
            backend_family="external",
            note="Unknown tool label; treated as user-facing metadata until a real adapter exists.",
        ),
    )


def get_runtime_profile() -> AgentRuntimeProfile:
    backend = get_backend_label()
    support = get_agent_tool_support(AGENT_TOOL)
    support_note = support.note
    if support.backend_family not in {backend, "external"}:
        support_note = f"{support.note} Active backend still resolves to {backend}."
    elif support.backend_family == "external":
        support_note = f"{support.note} Active backend currently resolves to {backend}."
    return AgentRuntimeProfile(
        agent_tool=AGENT_TOOL,
        model_series=MODEL_SERIES,
        backend=backend,
        support_level=support.support_level,
        support_note=support_note,
    )


def iter_agent_tool_support() -> list[AgentToolSupport]:
    return [AGENT_TOOL_SUPPORT[name] for name in SUPPORTED_AGENT_TOOLS]


def supports_agent_tool(name: str) -> bool:
    return name in SUPPORTED_AGENT_TOOLS


def supports_model_series(name: str) -> bool:
    return name in SUPPORTED_MODEL_SERIES
