# Context - GitHub Contribution Engine

This repository is dedicated to one product: an autonomous GitHub contribution engine.

## Mission

Build a contribution engine that behaves like a careful open-source contributor:

- find suitable repositories
- identify narrow evidence-backed opportunities
- generate minimal patches
- submit respectful PRs
- respond to maintainers
- learn from outcomes

## Current Architecture

- `app/builder.py`: contribution-only CLI entrypoint.
- `src/contribution_engine.py`: run orchestration and reporting.
- `src/contribution_store.py`: SQLite state, memory, queue, PR lifecycle.
- `src/opportunity_engine.py`: local pattern scanner and qualification.
- `src/repo_intelligence.py`: repo scoring and memory adjustment.
- `src/pr_generator.py`: target search, AI patch execution, PR status, feedback.
- `src/fork.py`: GitHub fork/branch/PR operations.

## Contribution Quality Rules

- Prefer one-file or two-file patches.
- Start from a concrete failure mode, not a vague improvement.
- Reject style-only changes.
- Reject speculative "safer/cleaner/more consistent" PRs without proof.
- Keep one open PR per repo at a time.
- Queue additional verified opportunities instead of spamming maintainers.
- Record every rejection with a structured reason code.

## AI Backend

Use Codex CLI by default.

Claude CLI is allowed only as an explicit fallback or local operator choice.

Do not introduce direct OpenAI SDK calls for contribution generation.

## Governance

`skill.md` is the primary source of truth for agent behavior in this repository.

`.agent.md` should align to that policy as the local execution persona.

Provider-specific operator notes should live under `docs/agents/` so tool differences stay documented without splitting the root policy.

Legacy crypto-builder guidance must not be treated as active instruction for this repo.

## Required Verification

After changing contribution engine logic, run:

```powershell
python -m py_compile app\builder.py src\ai.py src\config.py src\contribution_engine.py src\contribution_store.py src\fork.py src\notify.py src\opportunity_engine.py src\pr_engine.py src\pr_generator.py src\repo_intelligence.py src\scraper.py src\security.py src\state.py
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
