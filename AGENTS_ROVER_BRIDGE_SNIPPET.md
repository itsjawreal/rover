# Optional AGENTS.md snippet

Add this to your main AGENTS.md if you want Claude/Codex to understand ROVER Agent Bridge.

## ROVER Agent Bridge

This repository may be operated by `scripts/rover_agents.py`.

Agent roles:
- Claude Architect: plans narrow changes only.
- Codex Builder: implements the plan with minimal diffs.
- Claude Reviewer: approves/rejects based on diff and tests.
- ROVER Orchestrator: enforces policy, stores transcripts, runs tests, and stops unsafe changes.

Rules:
- Do not modify `.env`, secrets, keys, `.git`, `data`, `logs`, `runs`, or virtualenv folders.
- Keep diffs small.
- Add or update tests for behavior changes.
- Prefer one qualified opportunity per cycle.
- Stop instead of guessing when the plan is unsafe or unclear.
