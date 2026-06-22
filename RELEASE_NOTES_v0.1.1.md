# Rover v0.1.1 Release Notes

## Summary

Rover v0.1.1 focuses on PR trust, operator control, and safer contribution pacing.
The release keeps Rover centered as a careful GitHub contribution agent: narrow
patches, stronger pre-submit checks, clearer audit trails, and better operator
feedback before any PR reaches maintainers.

## Highlights

- Added human approval mode before PR submission.
- Added repo reconnaissance against recent merged/closed PRs.
- Added local repo verification before commit/push/PR creation.
- Added explicit `dep_update` contribution lane.
- Added natural-language shortcuts for targeted bugfix and dependency update runs.
- Added structured audit events for approval decisions.
- Added queue/reject notifications for operator feedback loops.
- Improved README and CONTRIBUTING guidance.

## Human Approval Mode

New flags:

```bash
python3 -m app.builder --contrib owner/repo --1 --human-approval
python3 -m app.builder --contrib owner/repo --1 --no-human-approval
```

Environment toggles:

```env
ROVER_HUMAN_APPROVAL=1
CONTRIB_HUMAN_APPROVAL=1
```

When enabled, Rover pauses before submission and shows:

- repo
- PR title
- improvement type
- changed files
- risk summary
- rationale

The operator can choose:

- submit PR
- queue for later
- reject patch

If human approval is required but no interactive TTY is available, Rover queues
the patch instead of submitting silently.

## Audit Trail

Human approval decisions are recorded in SQLite `repo_events` with structured
details including:

- actor
- decision
- reason
- opportunity id
- title
- improvement type
- changed files
- risk summary
- risk level

Operator rejection is also recorded as `operator_rejected` in structured
rejection memory.

## Repo Reconnaissance

Before AI patch generation, Rover can inspect recent PR history with `gh pr list`:

- last 10 merged PRs
- last 10 closed PRs

Repeated negative signals such as bot spam, AI-generated noise, missing tests,
failed CI, unsolicited changes, or overly broad PRs cause Rover to reject the
target before spending AI work.

## Local Verification

Before submitting or pushing a generated patch, Rover now detects and runs local
verification when possible.

Supported detection includes:

- `pnpm`, `npm`, `yarn`, and `bun` package managers
- package scripts such as `test`, `typecheck`, and `build`
- Python test layouts via `pytest` or `python -m unittest discover`

For example, a repo with `pnpm-lock.yaml` and a `test` script runs:

```bash
pnpm install --frozen-lockfile
pnpm test
```

## Dependency Update Lane

New explicit goal:

```bash
python3 -m app.builder --contrib owner/repo --goal dep_update --1
```

The lane only proceeds when Rover can find a safe dependency update and verify
it through the repository's local checks.

## Natural-Language Routing

New shortcuts:

```text
Rover, fix bug di owner/repo
Rover, update deps di owner/repo
```

These map to targeted contribution runs:

- `fix bug` -> `bugfix`
- `update deps` -> `dep_update`

Natural-language contribution commands still default to dry-run unless the
operator explicitly asks for live submission.

## Notifications

Queue and reject decisions now notify the operator through the existing
notification abstraction when credentials are configured.

Example:

```text
PR queued: owner/repo - fix: handle missing response field (Reason: Waiting for test fix)
PR rejected: owner/repo - feat: add broad dashboard mode (Reason: Too broad for maintainer review)
```

## Documentation

Updated docs include:

- CI badge in `README.md`
- `dep_update` usage
- human approval usage
- repo event audit trail example
- notification examples
- `CONTRIBUTING.md` guidance for adding new task lanes

## Verification

Local verification performed during development:

```bash
python3 -m py_compile app/builder.py src/core/ai.py src/core/config.py src/core/doctor.py src/core/notify.py src/core/security.py src/core/state.py src/contrib/contribution_engine.py src/contrib/contribution_store.py src/contrib/opportunity_engine.py src/contrib/pr_engine.py src/contrib/pr_generator.py src/analysis/repo_intelligence.py src/github/fork.py src/github/scraper.py src/platform/mcp_install.py src/platform/openclaw_install.py src/contribution_mcp/server.py
python3 -m unittest discover -s tests -v
```

Latest full suite result during this release work:

- 154 tests passed

## Staging Checklist

- Run `rover doctor` on staging.
- Run `rover inspect owner/repo` against a known small repo.
- Run targeted dry-run bugfix:

```bash
python3 -m app.builder --contrib owner/repo --goal bugfix --dry-run --1
```

- Run targeted human approval flow:

```bash
python3 -m app.builder --contrib owner/repo --goal bugfix --human-approval --1
```

- Verify queue decision creates a `human_approval_queue` repo event.
- Verify reject decision creates `operator_rejected`.
- Verify `--no-human-approval` overrides `ROVER_HUMAN_APPROVAL=1`.
- Run dependency update dry-run against a fixture or safe target:

```bash
python3 -m app.builder --contrib owner/repo --goal dep_update --dry-run --1
```

- Run `rover report` and confirm queued opportunities and rejection reasons are visible.
- Run `rover check` against existing open PR state.
- Run malformed natural-language command input and confirm Rover fails safe instead
  of guessing a repo:

```bash
python3 -m app.builder --command-text "Rover, fix bug di repo-abc"
```

- Inspect the active log file after failed repo fetch, queue, reject, and local
  verification failure paths. Confirm it includes enough detail to diagnose the
  repo, phase, reason, and attempted command.
- Test `rover check` with a local state containing open, queued, merged, and
  closed PR records so lifecycle reporting and feedback handling stay clear.

## Deferred

Async PR submission is intentionally deferred. It needs a deliberate design for:

- one-open-PR-per-repo pacing
- GitHub API pressure
- queue semantics
- SQLite state consistency
- operator approval ordering

This should be handled as a v0.2 design item, not a v0.1.1 patch.

Additional deferred items:

- A force/cooldown override needs an explicit safety design. It should not bypass
  one-open-PR-per-repo or maintainer-spam protections accidentally.
- Slack notification support should be added through the notification abstraction
  after the message format and credential model are defined.
