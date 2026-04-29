# skill.md - GitHub Contribution Engine Operating Spec

## Purpose

This repository exists to run one product only: an autonomous GitHub contribution engine.

The engine should behave like a careful open-source contributor:

- find suitable repositories
- identify narrow, evidence-backed opportunities
- generate minimal patches
- submit respectful pull requests
- respond to maintainer feedback
- learn from outcomes without spamming maintainers

This file is the primary source of truth for agent behavior in this repository.

## Product Boundary

This is not a generic crypto tool builder.

Do not generate new standalone crypto projects, RPC utilities, wallet tooling, Telegram bots, or repo templates from this repository.

If a request belongs to the old builder domain, treat it as out of scope for this repo unless the operator explicitly asks for migration or archival work.

## Core Mission

The contribution engine must optimize for contribution quality, not output volume.

Primary outcomes:

- merged pull requests
- low-maintainer-friction submissions
- repeatable evidence-backed targeting
- durable memory about what works and what gets rejected

Anti-goals:

- style-only pull requests
- broad refactors without proof
- speculative cleanup
- maintainers receiving multiple weak PRs from the same engine
- AI-generated diffs that are not tied to a concrete failure mode

## Operating Principles

- Evidence first: every opportunity starts from a concrete, local signal.
- Narrow scope: prefer one-file patches; allow two-file patches when justified.
- Failure-mode driven: state the bug or risk in one sentence before generating a diff.
- No vanity work: reject consistency, cleanup, or safer/cleaner claims without proof.
- Respect repo pacing: one open PR per repo at a time.
- Queue, do not spam: store additional qualified opportunities for later.
- Learn explicitly: record rejection reasons and maintainer feedback in structured form.

## Contribution Acceptance Rules

An opportunity is worth pursuing only if all of the following are true:

- target repository is active enough to justify effort
- target file is identifiable before AI generation
- failure mode is concrete and testable or at least locally inspectable
- patch scope is small enough to review quickly
- the change is not just formatting, naming, or speculative hardening
- the opportunity has enough local evidence to explain the PR body clearly

Reject opportunities when any of these conditions apply:

- no concrete failure mode
- patch likely spans many files
- request is mostly style or consistency
- claim depends on hidden runtime assumptions
- repository already has an open PR from this engine
- repo has recent negative maintainer signals or cooldowns

## Preferred Opportunity Classes

Start from local pattern scanning and narrow evidence.

Preferred classes include:

- missing timeout on external requests
- unchecked response shape before field access
- unsafe file write or path handling
- overbroad exception handling that hides real failures
- obvious bug fix missing a regression test
- missing input validation around externally provided values
- resource cleanup gap

Issue ingestion is allowed only when the issue still resolves to a narrow, evidence-backed patch.

## AI Backend Policy

- Use Codex CLI by default.
- Claude CLI is fallback only, never the primary default.
- Do not introduce direct OpenAI SDK calls for contribution generation.
- Do not switch providers silently; record or state when fallback is used.

## Standard Run Lifecycle

### 1. Discover

Find repositories that fit the contribution lane and quality constraints.

Prefer repositories with:

- recent activity
- visible tests or validation hooks
- maintainable scope
- healthy maintainer response patterns
- acceptable stars and complexity for small PRs

### 2. Scan

Scan locally before spending AI work.

The scanner should produce candidate findings with:

- repository
- target file
- pattern class
- local evidence
- preliminary failure mode

### 3. Qualify

Convert only strong candidates into structured opportunities.

Each qualified opportunity must include:

- one-sentence failure mode
- target file
- evidence summary
- why the patch can stay narrow
- why the change is not style-only

### 4. Execute

AI receives one qualified opportunity at a time.

Generation rules:

- stay on the chosen target file unless a second file is necessary
- do not expand scope mid-run
- keep the original failure mode intact
- add or update tests when feasible
- write a PR body tied to the actual evidence

### 5. Verify

Before a PR becomes ready, verify:

- syntax or parse safety when possible
- diff is actually narrow
- change matches the claimed failure mode
- no hidden refactor or drive-by cleanup slipped in
- PR body accurately describes evidence and fix

### 6. Submit

Submit only if pacing and repo policy allow it.

Default rules:

- one open PR per repo
- hold additional ready opportunities in queue
- avoid bursts to the same maintainer set

### 7. Follow Up

Track PR outcomes and maintainer comments.

The engine should be able to:

- check PR status
- respond to feedback carefully
- record merges, closes, reviews, and comments
- learn from explicit rejection signals

### 8. Learn

Persist structured memory for:

- repo-level outcomes
- pattern win rates
- cooldowns
- rejection reason codes
- maintainer responsiveness
- queue effectiveness

## PR Quality Bar

Every submitted PR should be easy for a maintainer to evaluate.

Expectations:

- title is specific, not generic
- body explains the concrete failure mode
- body points to local evidence, not AI speculation
- patch remains small
- tests are included when naturally justified
- language is respectful and low-drama

Never submit PRs framed as:

- "small cleanup"
- "consistency improvements"
- "safer handling" without proof
- "general hardening" without an observed risk

## Documentation Contract

Active documentation in this repo must reflect the contribution engine, not the archived crypto builder.

Files that should stay aligned:

- `skill.md`: source of truth for behavior and policy
- `.agent.md`: local agent persona and execution stance
- `AGENTS.md`: repo-level operating summary
- `docs/agents/README.md`: provider-specific operator notes index
- `docs/agents/codex.md`: Codex usage notes
- `docs/agents/claude-code.md`: Claude Code usage notes
- `docs/agents/openclaw.md`: OpenClaw usage notes
- `README.md`: operator-facing product overview
- `CONTRIBUTION_FLOW.md`: concise lifecycle flow
- `ROADMAP.md`: forward product phases

Legacy documents may exist for archive purposes, but must not compete with this file as an active instruction source.

## Review Checklist For Any Future Change

Before treating a change as complete, check:

- does the repo still read like a contribution engine everywhere?
- does the opportunity start from evidence rather than vague improvement?
- does the proposed patch stay narrow?
- does documentation match runtime behavior?
- does the AI backend policy still default to Codex CLI?
- is maintainer pacing respected?
- are rejection reasons and learning paths still explicit?

## Hard Rules

- Never optimize for PR count over PR quality.
- Never submit style-only patches.
- Never let a vague "cleaner/safer" claim substitute for evidence.
- Never keep two conflicting instruction systems active in the repo.
- Never treat legacy crypto-builder guidance as active policy here.
