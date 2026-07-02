# Menisik

[![CI](https://github.com/itsjawreal/menisik/actions/workflows/ci.yml/badge.svg)](https://github.com/itsjawreal/menisik/actions/workflows/ci.yml)

> A learning project: a GitHub contribution engine that scans repos, drafts focused patches, runs them through quality gates, and leaves the submit decision to a human.

Menisik is a personal learning project exploring whether an AI engine can find and prepare narrow open-source contributions. It is **not** a production-ready autonomous contributor — see [Honest status](#honest-status) below.

## Why the name?

*Menisik* is Indonesian for darning: mending fabric with small, careful, near-invisible stitches. That is the philosophy of this engine — it does not redesign the garment, it finds a small hole and closes it neatly, one stitch (one narrow patch) at a time.

This project was formerly named **rover**. The `rover`, `rover-engine`, `rover-mcp`, and `rover-daemon` commands remain as deprecated aliases through `0.1.x` (see [Deprecation Policy](#deprecation-policy)).

## What It Does

- Discovers active open-source repositories worth contributing to.
- Scans code locally for candidate contribution opportunities.
- Qualifies opportunities before spending AI calls.
- Uses an AI backend to draft focused patches and PR bodies: Codex or Claude CLI, or an OpenAI-compatible API (OpenRouter, etc.) — configurable via `AI_BACKEND` in `.env`.
- Can submit PRs through GitHub CLI (best used with `--dry-run` / human review first).
- Tracks PR lifecycle, maintainer feedback, rejections, queue state, and run summaries in SQLite.
- Adjusts repo scoring and cooldowns from past outcomes.

## Honest status

This README aims to describe what the engine actually does, not what it aspires to:

- ✅ The quality gates reliably **reject** unsafe, behavior-changing, or overly broad patches.
- ⚠️ A patch that **passes** the gates is not guaranteed to fix a real bug. The gates check safety, scope, and behavior-preservation — they cannot verify that the underlying problem is genuine, so the engine can produce plausible-but-speculative patches.
- ⚠️ It does **not** reliably produce merge-ready PRs unattended. Treat it as a drafting assistant: read every diff and confirm the bug is real before submitting.
- "Learns from outcomes" means simple repo scoring and cooldowns, not sophisticated learning.

The goal is contribution quality over volume — behave like a careful contributor, not a PR spam bot. That is the bar; reaching it consistently is still a work in progress.

## Main Commands

```bash
menisik                          # status dashboard
menisik doctor                   # check setup
menisik check                    # poll open PR statuses
menisik list-prs                 # all submitted PRs
menisik list-prs open            # filter by status
menisik report                   # run history + rejection analysis
menisik inspect owner/repo       # analyze a repo without submitting
menisik owner/repo               # target a specific repo
```

Advanced / low-level flags via `python -m app.builder`:

```bash
python -m app.builder --contrib --1
python -m app.builder --contrib owner/repo --goal feature_upgrade --1
python -m app.builder --contrib owner/repo --goal dep_update --1
python -m app.builder --contrib --goal bugfix --first-pr --1
python -m app.builder --contrib owner/repo --1 --human-approval
python -m app.builder --doctor
```

`run.py` is a thin scheduled-task wrapper that reads `CONTRIB_AUTORUN_ARGS` from `.env` and forwards to `python -m app.builder`. Use it for cron jobs.

Compatibility note:

- `rover`, `rover-engine`, `rover-mcp`, and `rover-daemon` still exist as deprecated aliases from the pre-rename era
- prefer `menisik ...` or `python -m app.builder ...` for new usage

## Deprecation Policy

Compatibility layers are kept only when they still protect active operator workflows.

Current policy:

- The project was renamed from **rover** to **menisik**. `rover`, `rover-engine`, `rover-mcp`, and `rover-daemon` are deprecated aliases of the `menisik*` entry points.
- `--pr`, `--pr-check`, and `--pr-respond` are deprecated now; prefer `--contrib`, `--contrib-check`, and `--contrib-respond`.
- Warning-only period: all `0.1.x` releases.
- Planned removal target: `0.2.0` or the next intentionally breaking CLI release.

Compatibility layers that remain supported without a removal date yet:

- OpenClaw compatibility skill paths under `github-contribution-engine` as aliases only.
- The MCP server still registers as `rover` and installers still write `rover`-named integration paths (`mcp.servers.rover`, skill/wrapper paths); renaming those would break existing MCP client configs, so they migrate to `menisik` at `0.2.0`.

These layers stay because they still protect real integrations, not just old command muscle memory.

Natural-language routing is also available for channel-style inputs:

- `make 1 contribution`
- `run 1 pr to owner/repo`
- `Menisik, fix bug in owner/repo`
- `Menisik, update deps in owner/repo`
- `create one pull request to https://github.com/owner/repo`
- `check repo owner/repo first`

(The routing is intent-based, so `Rover, fix bug in owner/repo` still works too.)

These phrases are mapped to canonical engine actions like `contrib_once`, `contrib_targeted`, `repo_inspect`, `contrib_check`, and `doctor`.
For safety, natural-language contribution requests default to preview mode unless the request explicitly asks for live submission.

When a prompt is ambiguous or the repo slug is not in `owner/repo` format, Menisik falls back to a safe `doctor` action at low confidence rather than guessing a target:

```
# Input: "Menisik, fix bug in repo-abc"
INFO  Natural-language command mapped to action=doctor repo=<search> count=1 dry_run=True confidence=low
INFO  [rationale] Repo token "repo-abc" does not match owner/repo format — skipping as target.
INFO  [rationale] No unambiguous action inferred from prompt — defaulting to a safe doctor action.
```

Prompts that do resolve cleanly log at normal confidence:

```
# Input: "Menisik, fix bug in owner/repo"
INFO  Natural-language command mapped to action=contrib_targeted repo=owner/repo count=1 dry_run=True confidence=high
INFO  [rationale] Explicit repo slug matched: owner/repo.
INFO  [rationale] Fix-intent keyword detected — mapped to contrib_targeted.
```

## Engine Design

Core modules:

- `src/contrib/contribution_engine.py`: run orchestration, pacing, operator reports.
- `src/contrib/contribution_store.py`: SQLite schema and persistence.
- `src/contrib/opportunity_engine.py`: local pattern scanner and qualification policy.
- `src/analysis/repo_intelligence.py`: repo score adjustments from memory.
- `src/contrib/pr_generator.py`: GitHub target search, AI patch execution, PR tracking, and feedback handling.
- `src/github/fork.py`: fork, branch, push, and PR creation through `gh`.

Module layout note:

- active code now lives under `src/core/*`, `src/contrib/*`, `src/github/*`, `src/platform/*`, and `src/analysis/*`
- new code should import those domain folders directly

The main state unit is an `Opportunity`, not a repo. Opportunities move through states like `SCAN`, `QUALIFY`, `EXECUTE`, `VERIFY`, `READY`, `SUBMIT`, and `REJECT`.

Behavioral policy is defined primarily in [skill.md](skill.md), with repo-specific operating guidance in [AGENTS.md](AGENTS.md).
Provider-specific operator notes live under [docs/agents/README.md](docs/agents/README.md).

## Quality Policy

The engine favors small, testable PRs:

- concrete failure mode required
- one clear target file preferred
- broad refactors rejected
- speculative "safer/cleaner" changes rejected
- one open PR per repo at a time
- queued opportunities are retained for later pacing

Every proposed patch should begin from a concrete failure mode and enough local evidence to explain the value without hand-waving.

Contribution goals:

- `bugfix`: default mode, sourced from local scanner evidence
- `dep_update`: updates pinned dependencies when a safe version bump and local verification path are available
- `feature_upgrade`: narrow enhancement work only when the code already contains explicit maintainer TODO/FIXME intent
- `feature_add`: stricter enhancement mode that expects issue-backed maintainer intent and a narrow target file match
- `--first-pr`: search-mode operator flag that biases repo discovery toward smaller, active, test-backed repos for a first real PR
- `--human-approval`: pauses before submission so the operator can submit, queue, or reject a generated patch

Human approval decisions are recorded in the `repo_events` audit trail. Example event details:

```json
{
  "event_type": "human_approval_queue",
  "repo_full_name": "owner/repo",
  "details_json": {
    "actor": "nadira",
    "decision": "queue",
    "reason": "test failed locally",
    "opportunity_id": 42,
    "title": "fix: handle missing response field",
    "improvement_type": "bug_fix",
    "files": ["src/client.py", "tests/test_client.py"],
    "risk": "narrow patch with test coverage signal",
    "risk_level": "medium"
  }
}
```

When notification credentials are configured, queue/reject decisions also send a short operator notification:

```text
PR queued: owner/repo - fix: handle missing response field (Reason: Waiting for test fix)
PR rejected: owner/repo - feat: add broad dashboard mode (Reason: Too broad for maintainer review)
```

Background MCP runs can also push progress updates while work is still in flight. Configure either direct Telegram delivery or OpenClaw relay delivery:

```text
MENISIK_NOTIFY_TRANSPORT=openclaw
OPENCLAW_NOTIFY_CHANNEL=telegram
OPENCLAW_NOTIFY_TARGET=-1001234567890
MENISIK_NOTIFY_PROGRESS=false
MENISIK_NOTIFY_INTERVAL_SECONDS=60
MENISIK_NOTIFY_STALL_SECONDS=300
MENISIK_NOTIFY_ONLY_ON_CHANGE=true
MENISIK_NOTIFY_ON_EVENT_TYPES=started,repo_selected,stage,patch_generated,pr_submitted,completed,failed,stalled
```

The deprecated `ROVER_NOTIFY_*` spellings are still read as a fallback and log a one-time deprecation warning per run.

Pattern classes (by priority):

| Pattern | Priority |
|---|---|
| `missing_timeout` | 10 |
| `missing_input_validation` | 9 |
| `unsafe_subprocess` | 9 |
| `unchecked_response_shape` | 8 |
| `resource_cleanup_gap` | 7 |
| `missing_retry_backoff` | 7 |
| `overbroad_exception_handling` | 6 |
| `unsafe_file_write_or_path` | 6 |
| `temp_file_cleanup_gap` | 6 |
| `flaky_time_dependent_test` | 5 |
| `missing_regression_test_for_obvious_bugfix` | 4 |
| `feature_upgrade_todo` | 3 |

## Setup

Bootstrap on a Linux VPS:

```bash
bash scripts/install_vps.sh
```

Reset the local Menisik install before re-testing setup flows:

```bash
bash scripts/uninstall_vps.sh
```

Guided local setup on Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_windows.ps1
```

Reset the local Windows install before re-testing setup flows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/uninstall_windows.ps1
```

One-line bootstrap from a fresh server:

```bash
curl -fsSL https://raw.githubusercontent.com/itsjawreal/menisik/master/scripts/bootstrap.sh | bash
```

The guided setup can also install a native OpenClaw skill + wrapper in one flow:

- `~/.openclaw/workspace/skills/rover/SKILL.md` when the OpenClaw workspace exists
- fallback: `~/.openclaw/skills/rover/SKILL.md`
- `~/.openclaw/tools/rover.py`
- `~/.openclaw/openclaw.json` with `mcp.servers.rover`

What the bootstrap script does:

- installs common system dependencies when possible (`git`, `python3`, `python3-venv`, `curl`, `gh`)
- installs `uv` when possible
- creates `.venv`
- installs this repo into the virtual environment
- creates `.env` from `.env.example` when missing
- can prompt for `GITHUB_TOKEN` and save it into `.env`
- can run `gh auth login` automatically, then continue to the next step after auth succeeds
- presents a backend setup wizard with these choices:
  - `Codex CLI`
  - `Claude CLI`
  - `LLM API key only`
  - `Skip for now`
- only asks the follow-up questions relevant to the selected backend
- can prompt for `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENROUTER_API_KEY` depending on the selected setup path
- can run `codex --login` when the user chooses the interactive Codex path
- runs `rover --doctor`
- prints the remaining manual steps such as `gh auth login` and AI CLI setup

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Authenticate GitHub CLI:

```bash
gh auth login
gh auth status
```

Key `.env` values:

```env
# Required
GITHUB_TOKEN=
GITHUB_OWNER=

# AI backend: claude | codex
AI_BACKEND=claude
CLAUDE_CMD=claude
CLAUDE_ARGS=-p -
CODEX_CMD=codex
CODEX_ARGS=exec --skip-git-repo-check -s read-only -

# Notifications (optional)
# (deprecated ROVER_NOTIFY_* spellings still work as fallback, with a warning)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
MENISIK_NOTIFY_TRANSPORT=openclaw
OPENCLAW_CMD=openclaw
OPENCLAW_NOTIFY_CHANNEL=telegram
OPENCLAW_NOTIFY_TARGET=
OPENCLAW_NOTIFY_ACCOUNT=
OPENCLAW_NOTIFY_THREAD_ID=
MENISIK_NOTIFY_PROGRESS=false
MENISIK_NOTIFY_INTERVAL_SECONDS=60
MENISIK_NOTIFY_STALL_SECONDS=300
MENISIK_NOTIFY_ONLY_ON_CHANGE=true
MENISIK_NOTIFY_ON_EVENT_TYPES=started,repo_selected,stage,patch_generated,pr_submitted,completed,failed,stalled

# Contribution targeting
CONTRIB_AUTORUN_ARGS=--contrib --1
CONTRIB_LANE=general
CONTRIB_TOPIC_KEYWORDS=
CONTRIB_SEARCH_QUERIES=

# Repo filters
PR_MIN_STARS=300
PR_MAX_STARS=6000
PR_COUNT=1
PR_TARGETED_MAX_TOTAL_FILES=500
PR_TARGETED_MAX_PY_FILES=300
PR_TARGETED_ALLOW_BROAD=false
```

Notes:

- `AI_BACKEND=claude` uses Claude CLI; `AI_BACKEND=codex` uses Codex CLI. The engine auto-falls back to Claude if Codex fails.
- `CONTRIB_AUTORUN_ARGS` controls what `python run.py` and scheduled tasks do by default.
- `CONTRIB_LANE` supports built-in presets: `general`, `crypto`, `devtools`, `frontend`, `data`, `infra`, `ml`, `docs`.
- `CONTRIB_TOPIC_KEYWORDS` and `CONTRIB_SEARCH_QUERIES` override the preset when you need custom targeting.
- `PR_TARGETED_MAX_TOTAL_FILES` and `PR_TARGETED_MAX_PY_FILES` only affect pinned/targeted repo runs.
- `PR_TARGETED_ALLOW_BROAD=true` disables targeted repo breadth guardrails entirely; use it only when you intentionally want to work on a large repo.
- `CONTRIB_FIRST_PR_MODE=true` makes search mode prefer smaller repos with tests and fresher activity for a first production PR attempt.
- You should keep secrets only in `.env`, not in docs or scripts.

Examples:

```env
# General-purpose developer tooling
CONTRIB_LANE=devtools

# Frontend ecosystem targeting
CONTRIB_LANE=frontend

# Custom niche override
CONTRIB_LANE=general
CONTRIB_TOPIC_KEYWORDS=observability,otel,tracing,metrics
CONTRIB_SEARCH_QUERIES=python:python observability library,typescript:typescript tracing sdk
```

## Portability

Before opening this project to wider users, run:

```bash
python -m app.builder --doctor
```

This checks:

- Python, `git`, and `gh` availability
- GitHub auth visibility
- configured AI backend and CLI presence
- whether only API keys are present without a supported CLI backend
- whether env settings contain machine-specific absolute paths

Current portability status:

- Codex CLI is the default tested path.
- Claude CLI is a supported fallback path.
- An OpenAI-compatible API backend (OpenRouter and similar) is supported via `AI_BACKEND=openrouter`, so a CLI is not required.
- Other agent-tool labels are documentation/demo metadata until a real backend adapter exists.

## MCP Server

This repo now includes a minimal MCP server for agent integrations.

MCP tools (17 total):

| Tool | Description |
|---|---|
| `get_status` | Engine stats: recent runs, queued opportunities, pattern rates |
| `list_opportunities` | Queued READY opportunities ranked by score |
| `list_prs` | Submitted PRs with status (open/merged/closed) |
| `contrib_report` | Formatted run summary with bottlenecks |
| `doctor` | Check all required tools are installed and configured |
| `inspect_repo` | Analyze a repo without submitting a PR |
| `route_command` | Map natural language to a canonical Menisik action |
| `run_contribution` | Start a contribution run in the background |
| `contrib_once` | Start one search-mode contribution run |
| `contrib_targeted` | Start one targeted contribution run |
| `stop_contribution` | Stop the active contribution process |
| `get_run_status` | Check if a contribution run is active |
| `contrib_check` | Poll open PRs for status changes and maintainer feedback |
| `contrib_respond` | Handle maintainer feedback without a status poll |
| `get_logs` | Last N lines from the most recent engine log |
| `get_config` | Read current `.env` settings (tokens masked) |
| `update_config` | Update a single key in `.env` |

Run it locally:

```bash
python -m src.contribution_mcp
```

**Claude Code auto-start** — the project includes `.mcp.json` at the root. Claude Code will auto-spawn the MCP server on session start, no manual config needed.

**OpenClaw** — install the native skill and wrapper:

```bash
python -m app.builder --install-openclaw
```

**Hermes and similar agent shells** — point them at the same `menisik-mcp` stdio server (or the deprecated `rover-mcp` alias) and start from `route_command` for natural-language chat requests. Operator notes live in [docs/agents/hermes.md](docs/agents/hermes.md).

Installable path:

```bash
uv tool install git+https://github.com/itsjawreal/menisik.git
```

After install, the MCP entrypoint becomes:

```bash
menisik-mcp
```

And the main contribution CLI becomes:

```bash
menisik --doctor
```

MCP config for Claude Desktop or other MCP clients (installed via `uv`):

```json
{
  "mcpServers": {
    "menisik": {
      "command": "uv",
      "args": [
        "tool", "run",
        "--from", "git+https://github.com/itsjawreal/menisik.git",
        "menisik-mcp"
      ]
    }
  }
}
```

MCP config for local WSL installs (already provided in `.mcp.json`):

```json
{
  "mcpServers": {
    "menisik": {
      "command": "wsl",
      "args": ["-d", "Ubuntu-20.04", "--", "bash", "-c",
               "cd /path/to/menisik && python3 -m src.contribution_mcp.server"],
      "type": "stdio"
    }
  }
}
```

## Verification

```bash
python -m py_compile app/builder.py src/ai.py src/config.py src/contribution_engine.py \
  src/contribution_store.py src/fork.py src/notify.py src/opportunity_engine.py \
  src/pr_engine.py src/pr_generator.py src/repo_intelligence.py src/validator.py \
  src/scraper.py src/security.py src/state.py src/contribution_mcp/server.py \
  src/core/ai.py src/core/config.py src/core/doctor.py src/core/github_auth.py \
  src/contrib/contribution_engine.py src/contrib/contribution_store.py src/contrib/opportunity_engine.py \
  src/contrib/pr_engine.py src/contrib/pr_generator.py src/github/fork.py src/github/scraper.py \
  src/analysis/repo_intelligence.py src/platform/mcp_install.py src/platform/openclaw_install.py
python -m unittest discover -s tests -v
```

## Active Docs

- [skill.md](skill.md): source of truth for contribution-engine behavior
- [AGENTS.md](AGENTS.md): repo mission, architecture, and verification rules
- [.agent.md](.agent.md): local agent execution persona
- [CONTRIBUTION_FLOW.md](CONTRIBUTION_FLOW.md): concise end-to-end contribution flow
- [ROADMAP_v0.1.2.md](ROADMAP_v0.1.2.md): active development roadmap
- [docs/agents/README.md](docs/agents/README.md): provider-specific operator notes
