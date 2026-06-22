# Contributing

## Setup

```bash
git clone https://github.com/BigNounce90/rover.git
cd rover
pip install -r requirements.txt
cp .env.example .env          # fill in GITHUB_TOKEN and GITHUB_OWNER
python -m app.builder --install-mcp
python -m app.builder --doctor
```

## Running tests

```bash
python -m unittest discover -s tests -v
```

All tests must pass before submitting a PR. CI runs automatically on push.

## Code style

- Python 3.11+, type hints on all public functions
- No comments explaining *what* the code does — only *why* when non-obvious
- Patches should be narrow: one clear target, one concrete failure mode
- No speculative hardening, no style-only changes

## Adding a task lane

A task lane is a deliberately narrow contribution mode such as `bugfix`,
`dep_update`, `feature_upgrade`, or `feature_add`.

When adding a lane:

- add the CLI goal alias in `app/builder.py`
- add natural-language mapping in `src/core/command_router.py` when appropriate
- keep generation inside `src/contrib/pr_generator.py`
- persist a structured reason when the lane rejects an opportunity
- add focused tests for routing, qualification, and failure behavior
- document the lane in `README.md`

Do not add broad lanes such as "cleanup" or "refactor". A lane should have a
clear acceptance policy and a predictable verification path.

## PR checklist

- [ ] Tests pass (`python -m unittest discover -s tests -v`)
- [ ] New behavior has a test
- [ ] No secrets or absolute paths committed
- [ ] `.env.example` updated if new env vars are added

## Project structure

```
app/builder.py          # CLI entry point
src/
  core/ai.py               # AI backend abstraction (Claude / Codex)
  contrib/contribution_engine.py  # run orchestration
  contrib/contribution_store.py   # SQLite persistence
  contrib/opportunity_engine.py   # local pattern scanner
  contrib/pr_generator.py         # GitHub search, patch execution, PR tracking
  github/fork.py                  # fork, branch, push via gh CLI
  contribution_mcp/               # MCP server
tests/                  # unittest suite
scripts/                # VPS installer and bootstrap
```

## Reporting bugs

Open an issue with:
- exact command run
- full error output
- output of `python -m app.builder --doctor`
