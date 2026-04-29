# Agent Notes

This folder holds provider-specific operator notes for the GitHub contribution engine.

Use these files for tool differences only:

- CLI invocation patterns
- environment or auth expectations
- fallback behavior
- known limitations
- operator tips for stable runs

Do not move core product policy here.

Core policy stays in:

- [../../skill.md](../../skill.md)
- [../../AGENTS.md](../../AGENTS.md)
- [../../.agent.md](../../.agent.md)

Available notes:

- [codex.md](codex.md)
- [claude-code.md](claude-code.md)
- [openclaw.md](openclaw.md)

Support level today:

- Codex: tested default path
- Claude Code: supported fallback path
- OpenClaw and other tool labels: document-first path until a real adapter exists
