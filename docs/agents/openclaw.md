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

## AI Backend & Credentials — why Rover does not reuse OpenClaw's key

Rover reads its own AI backend and API key from `.env` (`AI_BACKEND`,
`OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `OPENROUTER_BASE_URL`). It deliberately
does **not** pull credentials from OpenClaw. To be honest about the reasons:

- **The key is not in `openclaw.json` to read.** OpenClaw keeps the real
  OpenRouter key in its own credential store and leaves only a profile reference
  in the config (`auth.profiles.openrouter:default` → `mode: api_key`, no key
  value). The exact storage location varies by install and version, and it is
  outside Rover's control — reaching into it would be brittle and undocumented.
- **Rover runs standalone.** It works as a CLI, MCP server, cron job, and daemon
  with no OpenClaw present. Coupling credentials to OpenClaw would break every
  non-OpenClaw mode.
- **Different roles, different models.** OpenClaw's model is its chat brain
  (often a cheap/fast model); Rover's backend generates patches (a stronger
  model). In practice the two use different OpenRouter models, so a shared config
  would force the wrong model on one of them.

This is a deliberate decoupling, not a missing feature. If you want a single key
without duplicating it, share it through the environment rather than through
OpenClaw's config:

```bash
export OPENROUTER_API_KEY=sk-or-...   # in ~/.bashrc / ~/.profile; both tools read it
```

## Install Path

Recommended install command:

```powershell
uv tool install git+https://github.com/itsjawreal/rover.git
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
