# Context - GitHub Contribution Engine

This repo is dedicated to one product: autonomous GitHub contribution engine.

## Mission

Build contribution engine that behaves like careful open-source contributor:

- find suitable repositories
- identify narrow evidence-backed opportunities
- generate minimal patches
- submit respectful PRs
- respond to maintainers
- learn from outcomes

## Current Architecture

- `app/builder.py`contribution-only CLI entrypoint.
- `src/contrib/contribution_engine.py`run orchestration and reporting.
- `src/contrib/contribution_store.py`SQLite state, memory, queue, PR lifecycle.
- `src/contrib/opportunity_engine.py`local pattern scanner and qualification.
- `src/analysis/repo_intelligence.py`repo scoring and memory adjustment.
- `src/contrib/pr_generator.py`target search, AI patch execution, PR status, feedback.
- `src/github/fork.py`GitHub fork/branch/PR operations.
- `src/core/`runtime config, AI backend, doctor, UI, auth, state.
- `src/platform/`machine-specific install and integration helpers.

Compatibility:

- new imports should target domain folders directly

Deprecation policy:

- `rover-engine` and legacy `--pr*` flags are deprecated now.
- They stay warning-only through `0.1.x`.
- Earliest planned removal: `0.2.0` or next deliberately breaking CLI release.
- `github-contribution-engine` OpenClaw compatibility paths remain supported until replacement integrations are proven stable.

## Contribution Quality Rules

- Prefer one-file or two-file patches.
- Start from concrete failure mode, not vague improvement.
- Reject style-only changes.
- Reject speculative "safer/cleaner/more consistent" PRs w/o proof.
- Keep one open PR per repo at time.
- Queue additional verified opportunities instead of spamming maintainers.
- Record every rejection w/ structured reason code.

## AI Backend

Use Codex CLI by default.

Claude CLI is allowed only as explicit fallback or local operator choice.

Do not introduce direct OpenAI SDK calls for contribution generation.

## Governance

`skill.md` is primary source of truth for agent behavior in this repo.

`.agent.md` should align to that policy as local execution persona.

Provider-specific operator notes should live under `docs/agents/` so tool differences stay documented w/o splitting root policy.

Legacy crypto-builder guidance must not be treated as active instruction for this repo.

## Required Verification

After changing contribution engine logic, run:

```powershell
python -m py_compile app\builder.py src\core\ai.py src\core\config.py src\core\doctor.py src\core\notify.py src\core\security.py src\core\state.py src\contrib\contribution_engine.py src\contrib\contribution_store.py src\contrib\opportunity_engine.py src\contrib\pr_engine.py src\contrib\pr_generator.py src\analysis\repo_intelligence.py src\github\fork.py src\github\scraper.py src\platform\mcp_install.py src\platform\openclaw_install.py src\contribution_mcp\server.py
python -m unittest discover -s tests -v
```

## Operator Commands

```powershell
python -m app.builder --contrib --1
python -m app.builder --contrib owner/repo --1
python -m app.builder --contrib-check
python -m app.builder --contrib-respond
python -m app.builder --contrib-report
python -m app.builder --repo-inspect owner/repo
```

