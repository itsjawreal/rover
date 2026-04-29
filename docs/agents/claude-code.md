# Claude Code Notes

## Role

Claude Code is an allowed fallback or operator-selected backend.

## Use When

- the operator explicitly chooses Claude Code
- Codex is unavailable
- a local workflow needs comparison across tools

## Expectations

- preserve the same acceptance-first contribution behavior
- do not widen patch scope because the tool differs
- defer to [../../skill.md](../../skill.md) and [../../AGENTS.md](../../AGENTS.md) for core rules

## Operator Guidance

- keep prompts aligned with the contribution-engine mission
- record provider-specific quirks here, not in root policy files
- treat this file as notes for usage, not authority over product behavior

## Known Boundary

Claude Code support exists for operator flexibility. It is not the default product backend.
