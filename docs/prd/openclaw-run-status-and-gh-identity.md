# PRD: OpenClaw Run Status Integrity and GitHub Identity Isolation

## Summary

OpenClaw chat runs can report misleading Rover outcomes.

Observed failure:

- user asked Rover to run a targeted bugfix
- OpenClaw said run completed without a PR link
- real repo already had an open PR: `https://github.com/Alishahryar1/free-claude-code/pull/264`
- OpenClaw behavior read partial state, mixed sources, then improvised a narrative

This PRD defines the changes needed so:

- Rover is the single source of truth for run outcome
- OpenClaw does not free-style run completion state
- PR history is isolated by active GitHub identity, so switching `gh` accounts does not cross-contaminate PR state

## Problem

Current state has two integrity gaps.

### 1. Run outcome integrity gap

Rover stores PR/run state in more than one place:

- SQLite run store
- legacy `pr_log.json`

OpenClaw background summaries can end up reading:

- a run summary with `submitted_prs: []`
- an existing PR from another path
- or a stale/high-level report unrelated to the exact `run_id`

This creates user-visible contradictions:

- fork exists, but PR link missing
- run says complete, but no deterministic result object
- chat says “patch generated only” even when an existing PR is already open

### 2. GitHub identity isolation gap

PR history is not cleanly partitioned by active GitHub account.

Observed risk:

- user changes `gh auth` account
- Rover/OpenClaw can still read PR state created under a different login
- pacing, duplicate-PR checks, and reporting can collide across identities

This causes:

- wrong “existing PR” detection
- wrong maintainer feedback polling
- wrong PR status summaries
- mixed operator history across accounts

## Goals

1. Make `run_id` the only authoritative key for OpenClaw background run completion.
2. Ensure final run result always explains one of:
   - `submitted_new_pr`
   - `existing_pr_already_open`
   - `queued_no_submit`
   - `generation_failed`
   - `submission_failed`
3. Surface PR URL when one already exists for the same repo under the current GitHub identity.
4. Partition PR state by active GitHub login so account switching does not reuse another account’s PR history.
5. Remove OpenClaw need to infer run outcome from report summaries.

## Non-Goals

- redesigning Rover contribution scoring
- changing AI backend behavior
- adding multi-account UI
- removing all legacy compatibility paths in one step

## User Stories

### Story 1: Existing PR already open

As an OpenClaw user on Telegram,
when I run `rover run owner/repo bugfix`,
and my current GitHub account already has an open PR for that repo,
I should get:

- explicit status: `existing_pr_already_open`
- explicit PR URL
- no hallucinated “patch ready but not submitted” narrative

### Story 2: Fresh PR submission

As an OpenClaw user,
when Rover really submits a new PR,
I should get:

- final status: `submitted_new_pr`
- PR title
- PR URL
- repo name

### Story 3: Account switch

As an operator who changes `gh auth` login,
when I run `rover check` or another targeted contribution,
Rover should only consider PRs belonging to the active GitHub account for pacing, duplicate detection, and status polling.

### Story 4: Real-time monitoring

As an OpenClaw user,
when I ask “is it complete?”,
OpenClaw should answer from `get_run_result(run_id)` or `get_run_status(run_id)`,
not from `contrib_report` or a generic repo report.

## Current Failure Analysis

### Failure A: source split

Current behavior uses mixed persistence:

- `contrib_report` and run summaries rely on SQLite
- PR polling / legacy helpers still rely on `pr_log.json`
- OpenClaw can summarize one source while duplicate-PR truth lives in another

### Failure B: chat-layer inference

OpenClaw currently infers too much from:

- `contrib_report`
- generic queue state
- event snippets

Instead of using exact final run payload.

### Failure C: account ambiguity

Legacy PR state is repo-keyed, not account-keyed.

This means:

- open PR by account A can block account B
- maintainer check for account B can read account A’s PR history

## Proposed Product Behavior

### A. Canonical run outcome contract

Add a structured final outcome object to Rover background runs.

Required fields:

- `run_id`
- `engine_run_id`
- `state`
- `outcome_code`
- `repo`
- `goal`
- `submitted_new_prs`
- `existing_open_prs`
- `queued_opportunities`
- `rejections`
- `finished_at`

`outcome_code` enum:

- `submitted_new_pr`
- `existing_pr_already_open`
- `queued_no_submit`
- `generation_failed`
- `submission_failed`
- `canceled`

If `existing_pr_already_open`, include:

- `pr_url`
- `pr_title`
- `repo_full_name`
- `owner_login`
- `source` (`sqlite`, `legacy`, or future source)

### B. Exact OpenClaw run completion rule

OpenClaw must not use `contrib_report` to answer:

- “is it done?”
- “give me the PR link”
- “what happened to that run?”

For these intents, required tool order:

1. `get_run_status(run_id)`
2. if completed, `get_run_result(run_id)`
3. answer from returned fields only

Disallowed for final-status answers:

- guessing from queue state
- guessing from recent reports
- guessing from repo-level inspect output

### C. GitHub identity partitioning

All PR history and duplicate-detection state must be keyed by active GitHub identity.

Minimum identity field:

- `owner_login`

Applies to:

- SQLite `pull_requests`
- run summaries referencing submitted PRs
- legacy migration/import path
- duplicate PR detection
- `contrib_check`
- `contrib_respond`
- `get_pr_submitted_repos()`

Expected behavior:

- account A sees account A’s open PRs
- account B sees account B’s open PRs
- pacing and duplicate detection apply within the active account, not globally

### D. Legacy log migration behavior

Legacy `pr_log.json` should become migration-only, not a competing live source.

Required transition:

1. on startup or migration command, ingest legacy entries into SQLite with `owner_login`
2. mark legacy source as imported
3. stop using legacy log as a primary runtime store

Until removal:

- legacy reads may be used only as compatibility fallback
- final run result must declare when fallback data was used

## Functional Requirements

### FR-1 Final result payload

`get_run_result(run_id)` must return:

- exact `outcome_code`
- `submitted_new_prs`
- `existing_open_prs`
- `summary`
- `logs_tail`

If PR submission was skipped because a PR is already open:

- `submitted_new_prs` = `[]`
- `existing_open_prs` = `[ ... ]`
- `outcome_code` = `existing_pr_already_open`

### FR-2 Existing PR link recovery

When Rover detects an open PR during pacing or duplicate submission handling:

- recover the existing PR URL
- record a repo event with that URL
- expose it in final run result

### FR-3 Identity-aware PR storage

`pull_requests` records must include active GitHub login.

Duplicate checks must query:

- `repo_full_name`
- `owner_login`
- `status='open'`

### FR-4 Identity-aware PR check

`contrib_check` and `contrib_respond` must only operate on PRs for the active GitHub login unless explicitly requested otherwise.

### FR-5 OpenClaw safe answer policy

Docs/skill must instruct:

- use `get_run_result` for final answers
- use `get_run_status` for progress
- do not answer final run outcome from `contrib_report`

## Data Model Changes

### SQLite

Add to `pull_requests`:

- `owner_login TEXT NOT NULL DEFAULT ''`

Optional future additions:

- `source_store TEXT NOT NULL DEFAULT 'sqlite'`
- `source_imported_from TEXT NOT NULL DEFAULT ''`

Add to run summary schema or final result payload:

- `outcome_code`
- `existing_open_prs_json`

## Migration Plan

### Phase 1

- add `owner_login` column
- backfill from:
  - stored fork name when possible
  - active owner for imported single-user history
- keep compatibility fallback reads

### Phase 2

- import legacy `pr_log.json` entries into SQLite
- tag imported rows
- make SQLite canonical

### Phase 3

- remove runtime dependence on `pr_log.json`
- keep explicit export/import tool only if needed

## API Changes

### MCP

Update `get_run_result(run_id)` to return:

- `outcome_code`
- `submitted_new_prs`
- `existing_open_prs`
- `owner_login`

Update `get_run_status(run_id)` to optionally include:

- latest `outcome_hint`

### CLI / JSON

Contribution run JSON summary should include same fields for parity.

## Acceptance Criteria

1. If repo already has an open PR for current GitHub login:
   - Rover does not pretend a new PR was submitted
   - final result includes existing PR URL
   - OpenClaw can answer with that URL deterministically

2. If repo has open PR for a different GitHub login:
   - current account is not blocked by that record alone

3. `contrib_check` does not report PRs from another GitHub login by default.

4. `get_run_result(run_id)` is sufficient for OpenClaw to answer:
   - completed?
   - what happened?
   - what is the PR link?

5. No final-status OpenClaw answer requires reading `contrib_report`.

## Test Plan

### Unit tests

- existing PR recovered from SQLite
- existing PR recovered from legacy import fallback
- identity-aware duplicate check
- identity-aware `contrib_check`
- final result payload for:
  - new PR submitted
  - existing PR already open
  - queued no submit

### Integration tests

- start targeted run when same-account PR already open
- verify `get_run_result` returns `existing_pr_already_open` and PR URL
- switch mocked GitHub identity and verify PR isolation

### OpenClaw-facing tests

- natural-language run + follow-up “its complete?”
- response must quote exact `outcome_code`
- response must include PR URL only when present in tool payload

## Risks

- backfill owner identity may be imperfect for very old legacy rows
- mixed historical data may need one-time cleanup
- OpenClaw chat model can still hallucinate if skill instructions remain loose

## Recommended Delivery Order

1. Make SQLite canonical for final run result
2. Add `owner_login` to PR records
3. Update duplicate/pacing/query logic to be identity-aware
4. Tighten OpenClaw skill/docs to forbid report-based final run summaries
5. Migrate legacy `pr_log.json`

