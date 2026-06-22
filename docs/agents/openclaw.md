# OpenClaw Notes

## Role

OpenClaw is a documented integration target for operator-specific use.

## Use When

- evaluating compatibility with another agent shell
- documenting how the contribution engine can be run outside the default backend
- preparing multi-provider operator guidance

## Expectations

- preserve the same repo selection, qualification, validation, and PR standards
- keep provider notes implementation-focused
- defer to [../../skill.md](../../skill.md) and [../../AGENTS.md](../../AGENTS.md) for policy

## Operator Guidance

- document command differences and environment assumptions here
- avoid duplicating general contribution rules
- add concrete setup notes only when they are verified
- prefer calling the MCP tools in `src.contribution_mcp.server` over sending raw shell commands
- use `route_command` first when the user speaks in natural language instead of canonical action names
- prefer `contrib_once` and `contrib_targeted` over the lower-level `run_contribution` tool when the action is already known

## Install Path

Recommended install command:

```powershell
uv tool install git+https://github.com/BigNounce90/rover.git
```

Verify the installed commands:

```powershell
rover --doctor
rover-mcp
```

If the commands are not found after install, make sure uv's bin directory is on `PATH`.

For one-step VPS onboarding plus native OpenClaw assets:

```bash
bash scripts/install_vps.sh
```

That setup flow can install:

- the Python environment
- `rover`
- `rover-mcp`
- `~/.openclaw/workspace/skills/rover/SKILL.md` when the OpenClaw workspace exists
- fallback: `~/.openclaw/skills/rover/SKILL.md`
- `~/.openclaw/tools/rover.py`
- `~/.openclaw/openclaw.json` with `mcp.servers.rover`

## OpenClaw MCP Config

If your OpenClaw environment supports standard MCP server registration, use:

```json
{
  "mcp": {
    "servers": {
      "rover": {
        "command": "/absolute/path/to/rover-mcp",
        "args": []
      }
    }
  }
}
```

## OpenClaw Native Skill

This repo also supports a native OpenClaw wrapper path for direct Rover workflows.

Installed files:

- preferred: `~/.openclaw/workspace/skills/rover/SKILL.md`
- fallback: `~/.openclaw/skills/rover/SKILL.md`
- `~/.openclaw/tools/rover.py`
- compatibility aliases under `github-contribution-engine`

The installed `SKILL.md` now uses the official OpenClaw YAML frontmatter format so gateway skill discovery matches the current OpenClaw docs.

The native wrapper is optional. The primary automation path is MCP via `mcp.servers.rover`. Use the wrapper only when your Telegram / Discord / OpenClaw agent behaves like a general chat assistant instead of calling MCP tools directly.

Preferred commands from that skill:

- `doctor`
- `contrib_report`
- `repo_inspect --repo owner/repo`
- `message --text "buat 1 kontribusi"`
- `message --text "tampilkan report kontribusi terakhir"`

For natural-language chat channels, `route --text "..."` is the safest first step. Use `message --text "..."` only when you intentionally want a synchronous direct Rover call instead of MCP background execution.

Local clone variant:

```json
{
  "mcp": {
    "servers": {
      "rover": {
        "command": "/path/to/rover/.venv/bin/rover-mcp",
        "args": []
      }
    }
  }
}
```

## Suggested Tool Flow

For natural-language channels such as Telegram or Discord routed through OpenClaw:

1. Call `route_command(text=...)`
2. Inspect the returned canonical action
3. Call the matching MCP tool:
   - `doctor`
   - `repo_inspect`
   - `contrib_once`
   - `contrib_targeted`
   - `contrib_check`
   - `contrib_report`

Example:

- user says `buat 1 kontribusi`
- OpenClaw calls `route_command(text="buat 1 kontribusi")`
- result maps to `contrib_once` with `dry_run=true`
- OpenClaw then calls `contrib_once(dry_run=true)`

## Safety Notes

- natural-language contribution requests default to preview mode unless the request explicitly asks for live submission
- `route_command` is safer than forwarding raw shell commands from chat
- keep GitHub auth and local CLI readiness healthy by checking `rover --doctor`
- do not write artifacts into `~/.openclaw/sandboxes/...` unless that path is explicitly known writable
- prefer plain text / JSON replies when no saved artifact is required
- if a file is required, choose a real writable repo or workspace path instead of an internal OpenClaw sandbox path

## Known Boundary

This file documents a supported path, not a separate product mode.
