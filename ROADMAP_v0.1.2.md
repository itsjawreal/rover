# Roadmap V2

Practical roadmap for taking the GitHub contribution engine from operator-grade beta to a stronger open-source release.

## Phase 0 - Stabilize Current Beta

Goal:
- Reduce repeated operator pain in the current engine without changing the core product shape.

Deliverables:
- split submission retries from AI generation retries
- suppress duplicate queued opportunities for the same repo/file/pattern
- add clearer rejection explanations in report and inspect output
- keep contribution/fork/auth flows stable for the active GitHub user

Definition of done:
- a failed fork/push/PR create step no longer forces unnecessary AI regeneration
- queue/report noise from repeated identical opportunities is visibly lower
- operator can understand the top bottleneck from report output alone

## Phase 1 - First PR Success Rate

Goal:
- Increase the probability that a new operator gets one real PR merged or reviewed quickly.

Deliverables:
- `--first-pr` search mode
- repo suitability scoring in inspect output
- stronger preference for small repos with tests
- narrower target-file preference inside candidate repos

Definition of done:
- first-run search can consistently surface repos that are visibly smaller and more testable
- inspect output can say whether a repo is a strong or weak first-PR candidate
- `target_area_too_broad` becomes less dominant in first-PR workflows

## Phase 2 - Large Repo Narrowing

Goal:
- Make broad repositories less likely to dead-end at qualification time.

Deliverables:
- target-area narrowing fallback when the first opportunity is too broad
- file-level penalties for core or oversized files
- stronger preference for helper/config/smoke/test-adjacent files
- fallback search inside the same repo before abandoning it

Definition of done:
- a broad repo can still yield a narrow second-choice opportunity more often
- fewer search runs die after many `target_area_too_broad` rejections

## Phase 3 - Public Onboarding

Goal:
- Make the project understandable and runnable by users who did not build it.

Deliverables:
- publish-safe config preset
- guided setup / checklist command
- known limitations section kept current
- clearer docs for lanes, goals, and first-PR workflows

Definition of done:
- a new user can reach doctor, inspect, and dry-run contribution flows without manual debugging help
- README and support docs clearly separate current capabilities from planned ones

## Phase 4 - API-Key-Only Operation

Goal:
- Remove the CLI-only adoption bottleneck for public users.

Deliverables:
- generic provider adapter interface
- API-key-only LLM backend support
- doctor checks for backend readiness by adapter type
- docs for CLI mode vs API-key mode

Definition of done:
- a user without Codex CLI or Claude CLI can still run the engine with supported API credentials
- contribution generation path stays aligned with the same quality guardrails

## Phase 5 - Maintainer Intelligence

Goal:
- Improve acceptance quality through repo and maintainer memory.

Deliverables:
- maintainer style memory
- preferred PR-body shape by repo or maintainer
- stronger pacing decisions from past outcomes
- confidence scoring in reports

Definition of done:
- reports show which maintainers or repos are historically receptive
- repeated PRs to the same ecosystem get more context-aware over time

## Phase 6 - Portable Automation

Goal:
- Make recurring execution less tied to one Windows workstation.

Deliverables:
- portable scheduler examples
- cron/Linux runner guidance
- optional GitHub Actions or container runner profile
- safe non-interactive autorun defaults

Definition of done:
- the engine can be scheduled outside local Windows Task Scheduler with minimal adaptation

## Immediate Priority Order

1. submission retry split
2. duplicate-opportunity suppression
3. repo suitability + rejection explanation
4. target-area narrowing fallback
5. first-PR mode v2
6. onboarding and publish-safe preset
7. API-key-only backend adapter
8. maintainer-style memory

## Release Guidance

Current release posture:
- soft publish / experimental beta: yes
- polished public launch: not yet

Strongest current promise:
- operator-driven contribution engine for narrow Python/TypeScript PRs

Promise to avoid for now:
- works anywhere with no setup
- supports all LLM access modes
- reliably handles all large repos
