# Contribution Engine Flow

## 1. Discover

Search GitHub for active repositories that match the configured contribution lane.

The engine favors:

- active repos
- allowed licenses
- reasonable repo size
- visible tests
- current contribution lane relevance
- prior positive maintainer signals

This step is for target selection only, not speculative idea generation.

## 2. Scan

Download source files and run local pattern scanners before calling AI.

V1 scanner patterns:

- missing timeout
- unchecked response shape
- unsafe path or file write
- overbroad exception handling
- missing regression test for an obvious bug
- missing input validation
- resource cleanup gap

## 3. Qualify

Turn scanner hits into structured opportunities.

Required:

- concrete one-sentence failure mode
- clear target file
- narrow patch scope
- enough local evidence

Rejected opportunities are persisted with reason codes.

If the engine cannot explain the failure mode in one sentence, the opportunity is not ready.

## 4. Execute

AI receives exactly one qualified opportunity.

It must:

- keep the same target file
- keep the same failure mode
- produce a minimal patch
- add or update tests when feasible
- write a PR body tied to the evidence

Codex CLI is the default backend for this step. Claude is fallback only.

## 5. Verify

The engine checks:

- syntax
- actual diff
- evidence quality
- diff safety
- self-review behavior analysis

Passing opportunities become `READY`.

## 6. Submit

Submit only when pacing allows it.

Default policy:

- one open PR per repo
- queued opportunities stay ready
- no burst PR spam to the same maintainer

The engine should prefer holding a good queued opportunity over sending a weak second PR.

## 7. Follow Up

`--contrib-check` and `--contrib-respond` handle PR lifecycle and feedback.

The engine records:

- approvals
- merges
- closes
- maintainer comments
- rejected assumptions

## 8. Learn

SQLite memory updates repo scoring, cooldowns, pattern outcomes, hotspots, and run summaries.

Use:

```powershell
python -m app.builder --contrib-report
```
