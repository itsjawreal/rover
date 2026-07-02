# Hermes Notes

## Role

Hermes is treated as an MCP-capable agent shell, not a separate Menisik backend.

## Use When

- connecting Menisik to a Hermes chat or orchestration surface
- evaluating another agent shell that can call MCP tools over stdio
- documenting cross-agent operator guidance outside Codex and Claude Code

## Expectations

- preserve the same repo selection, qualification, validation, and PR standards
- prefer MCP tool calls over raw shell execution
- defer to [../../skill.md](../../skill.md) and [../../AGENTS.md](../../AGENTS.md) for product policy

## Recommended Tool Flow

For chat-style Hermes requests:

1. Call `route_command(text=...)`
2. Inspect the canonical action and confidence
3. Call the matching MCP tool:
   - `doctor`
   - `inspect_repo`
   - `contrib_once`
   - `contrib_targeted`
   - `contrib_check`
   - `contrib_respond`
   - `contrib_report`

This keeps natural-language routing centralized in Menisik instead of duplicating intent logic in Hermes.

## MCP Config

Canonical Hermes integration target:

- `~/.hermes/config.yaml`
- root key: `mcp_servers`
- server name: `menisik` (installs before the 0.2.0 rename used `rover`; re-run `--install-hermes` to migrate)

Example:

```yaml
mcp_servers:
  menisik:
    command: "/absolute/path/to/menisik-mcp"
    args: []
    enabled: true
```

## Safety Notes

- natural-language contribution requests should start at `route_command`
- preview mode should remain the default unless the user explicitly asks for live submission
- use `doctor` and `get_config` when Hermes needs to diagnose local operator state

## Known Boundary

Hermes support is an MCP integration path. It is not a separate contribution engine mode.
