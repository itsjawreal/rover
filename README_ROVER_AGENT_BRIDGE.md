# ROVER Agent Bridge

Drop-in multi-agent CLI orchestration for a repo that already uses Claude and Codex in VS Code.

ROVER becomes the moderator:

```txt
User goal
  ↓
Claude = architect / planner
  ↓
Codex = builder / fixer
  ↓
ROVER = git guard + tests + transcript
  ↓
Claude = reviewer
  ↓
Codex = fixes if needed
  ↓
final report
```

## Install

Extract this zip into the root of your ROVER repo.

You should get:

```txt
.agents/
  commands.yml
  policy.yml
  roles/
scripts/
  rover_agents.py
.vscode/
  tasks.json
README_ROVER_AGENT_BRIDGE.md
```

## First check

From VS Code terminal:

```bash
python scripts/rover_agents.py config-check
```

If Claude/Codex command names are wrong, edit:

```txt
.agents/commands.yml
```

Example:

```yaml
claude:
  command: "claude -p"

codex:
  command: "codex"
```

If your Codex uses another syntax, change `codex.command`.

## Start a cycle

```bash
python scripts/rover_agents.py start "lanjutkan kodingan yang udah ada, cari bug kecil yang aman, implement, test, review"
```

## VS Code task

Open Command Palette:

```txt
Tasks: Run Task
→ ROVER: Agent Cycle
```

Then type your goal.

## Outputs

Each run is saved here:

```txt
runs/agent_sessions/YYYYMMDD-HHMMSS/
  task.md
  plan.md
  codex_result.md
  diff.patch
  tests.txt
  review.md
  final.md
  transcript.jsonl
```

Check latest:

```bash
python scripts/rover_agents.py status
```

List latest run files:

```bash
python scripts/rover_agents.py show
```

Abort and rollback working tree:

```bash
python scripts/rover_agents.py abort --rollback
```

## Safety

Default policy blocks edits to:

```txt
.env
.git/
.venv/
venv/
data/
logs/
runs/
secrets/
*.pem
*.key
id_*
```

It also stops if too many files changed or files were deleted.

Edit `.agents/policy.yml` only after the loop is stable.

## Recommended usage

Start narrow:

```bash
python scripts/rover_agents.py start "fix one obvious failing test, minimal diff only"
```

Then:

```bash
python scripts/rover_agents.py start "improve timeout handling in src/contrib only, add regression test"
```

Avoid vague goals at first:

```txt
"make everything better"
"refactor whole repo"
"fully automate all improvements"
```

Use narrow goals until you trust the loop.
