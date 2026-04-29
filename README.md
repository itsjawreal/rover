# GitHub Contribution Engine

> A careful open-source contributor agent, optimized for PR acceptance rather than raw code generation.

Autonomous contribution engine for finding, verifying, submitting, tracking, and learning from GitHub pull requests.

## What It Does

- Discovers active open-source repositories worth contributing to.
- Scans code locally for narrow, evidence-backed contribution opportunities.
- Qualifies opportunities before spending AI calls.
- Uses Codex CLI as the default tested backend and Claude CLI as a supported fallback to produce focused patches and PR bodies.
- Can present itself under different agent-tool and model-series labels for demos, grant forms, and operator environments.
- Submits PRs through GitHub CLI.
- Tracks PR lifecycle, maintainer feedback, rejections, queue state, and run summaries in SQLite.
- Learns from outcomes so repeated weak targets are deprioritized.

The engine optimizes for contribution quality over contribution count. It should behave like a careful contributor, not a PR spam bot.

## Main Commands

```powershell
python main.py --dry-run
python main.py --live --repo https://github.com/HKUDS/Vibe-Trading --issue https://github.com/HKUDS/Vibe-Trading/pull/60 --dry-run
python main.py --live --repo owner/repo --dry-run
python -m app.builder --contrib --1
python -m app.builder --contrib owner/repo --1
python -m app.builder --contrib owner/repo --goal feature_upgrade --1
python -m app.builder --contrib owner/repo --goal feature_add --1
python -m app.builder --contrib --goal bugfix --first-pr --1
python -m app.builder --contrib-check
python -m app.builder --contrib-respond
python -m app.builder --contrib-report
python -m app.builder --doctor
python -m app.builder --repo-inspect owner/repo
python -m app.builder --command-text "buat 1 kontribusi"
python -m app.builder --command-text "cek repo owner/repo dulu"
```

Compatibility aliases are still available:

```powershell
python -m app.builder --pr --1
python -m app.builder --pr-check
python -m app.builder --pr-respond
```

Natural-language routing is also available for channel-style inputs:

- `buat 1 kontribusi`
- `jalankan 1 pr ke owner/repo`
- `buat satu pull request ke https://github.com/owner/repo`
- `cek repo owner/repo dulu`

These phrases are mapped to canonical engine actions like `contrib_once`, `contrib_targeted`, `repo_inspect`, `contrib_check`, and `doctor`.
For safety, natural-language contribution requests default to preview mode unless the request explicitly asks for live submission.

## Submission-Ready Demo

Use the deterministic demo path for screenshots, grant forms, and quick proof:

```powershell
python main.py --dry-run
```

What it does:

- emits a stable contribution-agent run summary in JSON
- writes proof artifacts to `runs/` in both `.json` and `.md`
- shows selected repo, issue, planned fix, validation result, PR title, and PR body
- avoids live network or long-running AI calls
- if you pass `--repo` or `--issue` without `--live`, they are used only to customize the demo artifact

Use live mode only when you want the real GitHub + AI workflow:

```powershell
python main.py --live --repo owner/repo --dry-run
```

Current live-mode note:

- `--issue` is currently metadata-only in live mode; the engine does not yet fetch and reason over the GitHub issue thread directly.

## Engine Design

Core modules:

- `src/contribution_engine.py`: run orchestration, pacing, operator reports.
- `src/contribution_store.py`: SQLite schema and persistence.
- `src/opportunity_engine.py`: local pattern scanner and qualification policy.
- `src/repo_intelligence.py`: repo score adjustments from memory.
- `src/pr_generator.py`: GitHub target search, AI patch execution, PR tracking, and feedback handling.
- `src/fork.py`: fork, branch, push, and PR creation through `gh`.

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
- `feature_upgrade`: narrow enhancement work only when the code already contains explicit maintainer TODO/FIXME intent
- `feature_add`: stricter enhancement mode that expects issue-backed maintainer intent and a narrow target file match
- `--first-pr`: search-mode operator flag that biases repo discovery toward smaller, active, test-backed repos for a first real PR

V1 pattern classes:

- `missing_timeout`
- `unchecked_response_shape`
- `unsafe_file_write_or_path`
- `overbroad_exception_handling`
- `missing_regression_test_for_obvious_bugfix`
- `missing_input_validation`
- `resource_cleanup_gap`

## Setup

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Authenticate GitHub CLI:

```powershell
gh auth login
gh auth status
```

Optional `.env` values:

```env
AI_BACKEND=codex
AGENT_TOOL=Codex
MODEL_SERIES=GPT
GITHUB_TOKEN=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
CONTRIB_AUTORUN_ARGS=--contrib --1
CONTRIB_LANE=general
CONTRIB_TOPIC_KEYWORDS=
CONTRIB_SEARCH_QUERIES=
PR_MIN_STARS=300
PR_MAX_STARS=6000
PR_COUNT=1
PR_TARGETED_MAX_TOTAL_FILES=130
PR_TARGETED_MAX_PY_FILES=75
PR_TARGETED_ALLOW_BROAD=false
CONTRIB_FIRST_PR_MODE=false
```

Notes:

- `CODEX_CMD=codex` is already portable if Codex CLI is on `PATH`.
- `CLAUDE_CMD=claude` is preferred over a user-specific Windows path.
- `AGENT_TOOL` is the user-facing tool label for demos/forms. Defaults to `Codex` or `Claude Code` based on `AI_BACKEND`.
- `MODEL_SERIES` is the user-facing primary model family label. Defaults to `GPT` or `Claude` based on `AI_BACKEND`.
- Today only `Codex` and `Claude Code` map to real backend paths. Labels like `Aider`, `Cline`, `Cursor`, `OpenCode`, `Windsurf`, and `Other` are metadata-only until adapters are implemented.
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

```powershell
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
- Other agent-tool labels are documentation/demo metadata until a real backend adapter exists.
- API-key-only LLM operation is not implemented yet, so users without a supported CLI backend will currently be blocked.

## MCP Server

This repo now includes a minimal MCP server for agent integrations.

Current MCP tools:

- `doctor`
- `contrib_report`
- `route_command`
- `repo_inspect`
- `contrib_once`
- `contrib_targeted`
- `contrib_check`

Run it locally:

```powershell
python -m src.contribution_mcp
```

Or with streamable HTTP transport if your MCP client expects it:

```powershell
python -m src.contribution_mcp streamable-http
```

What this gives you:

- Telegram/OpenClaw/Claude/Desktop agents can call structured tools instead of shell commands
- natural-language requests can be normalized through `route_command`
- contribution runs stay behind the same engine safety rules

Example OpenClaw-style flow:

- user says `buat 1 kontribusi`
- client calls `route_command(text=...)`
- client receives canonical action like `contrib_once`
- client then calls `contrib_once(dry_run=true)`

Installable path:

```powershell
uv tool install git+https://github.com/BigNounce90/github-contribution-engine.git
```

After install, the MCP entrypoint becomes:

```powershell
contribution-mcp
```

And the main contribution CLI becomes:

```powershell
github-contribution-engine --doctor
```

Example Claude Desktop / OpenClaw MCP config:

```json
{
  "mcpServers": {
    "contribution-engine": {
      "command": "uv",
      "args": [
        "tool",
        "run",
        "--from",
        "git+https://github.com/BigNounce90/github-contribution-engine.git",
        "contribution-mcp"
      ]
    }
  }
}
```

## Verification

```powershell
python -m py_compile main.py app\builder.py src\agent_models.py src\ai.py src\config.py src\contribution_engine.py src\contribution_store.py src\fork.py src\notify.py src\opportunity_engine.py src\pr_engine.py src\pr_generator.py src\repo_intelligence.py src\repo_discovery.py src\issue_analyzer.py src\repo_cloner.py src\project_inspector.py src\fix_planner.py src\patch_generator.py src\validator.py src\pr_writer.py src\run_logger.py src\scraper.py src\security.py src\state.py
python -m unittest discover -s tests -v
```

## Grant Proof Package

For Xiaomi MiMo Orbit or similar builder submissions, the recommended proof bundle is:

- one screenshot of `python main.py --dry-run`
- one generated `runs/run_*.md` artifact
- one generated `runs/run_*.json` artifact
- one GitHub repository link
- one real PR/workflow example showing review-aware iteration, such as the Vibe-Trading validation CLI contribution story

## Active Docs

- [FEATURES.md](FEATURES.md): capability map for the current engine
- [PRODUCT_STATUS.md](PRODUCT_STATUS.md): current strengths, bottlenecks, and recommended usage
- [skill.md](skill.md): source of truth for contribution-engine behavior
- [AGENTS.md](AGENTS.md): repo mission, architecture, and verification rules
- [.agent.md](.agent.md): local agent execution persona
- [docs/agents/README.md](docs/agents/README.md): provider-specific operator notes
- [CONTRIBUTION_FLOW.md](CONTRIBUTION_FLOW.md): concise end-to-end contribution flow
