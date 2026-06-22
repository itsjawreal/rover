## Rover Builds - Find. Fix. Ship. Repeat.
### Submit Rate Per Token Recovery Plan

## Summary

Refocus Rover around one hard KPI:

- maximize `submitted_prs / est_tokens`
- minimize expensive no-submit runs
- spend AI only on candidates that are both narrow and submission-shaped

Current state is better than before, but still wasteful:

- some repos now fail early w/ `AI calls: 0`, which is good
- other repos still reach `plan` and `generate`, then die in semantic review
- certain pattern classes, such as `overbroad_exception_handling`, burn tokens while rarely producing safe PRs
- targeted live runs still treat too many candidates as “worth trying” when they should be rejected or downgraded earlier

This v2 plan optimizes for **submit rate per token**, not just “more attempts” or “more AI work”.

## Success Metric

Primary KPI:

- `submitted_prs / est_tokens`

Secondary KPIs:

- `tokens_per_submitted_pr`
- `ai_calls_per_submitted_pr`
- `late_reject_ratio`
- `early_reject_ratio`
- `self_review_rejected / generated`
- `broad_rejected_before_ai / discovered`

Target direction:

- more early `no`
- fewer expensive `maybe`
- more cheap, narrow `yes`

## Key Changes

Implementation status:

- [x] 1. Pattern-specific submit policy
- [x] 2. Repo-specific memory for accepted/rejected patch shapes
- [x] 3. Same-pattern retry kill switch
- [x] 4. Patch-shape classifier before semantic review
- [x] 5. `live-safe` / `live-review` execution split
- [x] 6. Repo live-fit score
- [x] 7. Scan output feeds contribution ranking
- [x] 8. Submit-rate-per-token benchmark suite
- [x] 9. Operator-visible outcome taxonomy
- [x] 10. Token-spend budget by pattern family

### [x] 1. Add pattern-specific submit policy

Not all bug patterns deserve the same live-run treatment.

Introduce policy bands per `pattern_type`:

- `auto_live_safe`
- `dry_run_first`
- `manual_only`
- `blocked_for_targeted_live`

Initial expectation:

- `missing_input_validation` -> `auto_live_safe`
- `unchecked_response_shape` -> `auto_live_safe`
- `missing_timeout` -> `auto_live_safe`
- `resource_cleanup_gap` -> `dry_run_first`
- `overbroad_exception_handling` -> `blocked_for_targeted_live` or `manual_only`

Each pattern policy should define:

- max changed files
- max diff lines
- required nearby test proof
- banned dirs / surfaces
- whether semantic review is enough for auto-submit

Goal:

- stop expensive late rejections on pattern classes that are structurally low-yield

### [x] 2. Add repo-specific memory for accepted and rejected patch shapes

Store repo-local memory about what works and what fails:

- `pattern_type`
- `target_dir`
- `target_file`
- `had_test_target`
- `self_review_reason`
- `submitted`
- `closed_without_merge`
- `maintainer_feedback_shape`

Use this memory to:

- down-rank patterns repeatedly rejected in that repo
- promote shapes repeatedly submitted or merged in that repo
- block retrying the same bad patch family on the same repo

Example:

- `GPT-AGI/Clawd-Code`
  - down-rank `overbroad_exception_handling`
  - down-rank `context_system/*` behavior-policy patches
  - keep preferring validation and response-shape fixes

Goal:

- convert failure history into ranking signal

### [x] 3. Add same-pattern retry kill switch

When targeted live run hits:

- same `repo`
- same `pattern_type`
- same `target_file`
- rejected in semantic self-review

then:

- do not retry that same patch family in the same run
- move to next shortlist candidate
- if no candidate remains, stop

Also add stronger no-repeat behavior for:

- same PR title family
- same “surface errors instead of swallow them” exception-policy wording

Goal:

- eliminate fake diversity retries that consume tokens without increasing submit odds

### [x] 4. Add patch-shape classifier before semantic review

Insert a cheap deterministic gate after code generation but before semantic AI review.

Classify patch as high-risk if it:

- changes exception policy
- swaps fallback behavior for raised errors
- broadens user-visible failure path
- changes more return-path semantics than plan claims
- touches behavior-routing files in `context`, `config`, `auth`, `loader`, `middleware`

If classifier says high-risk:

- reject early
- or downgrade to `manual approval only`
- do not spend semantic review tokens unless policy explicitly allows it

Goal:

- catch risky semantic shifts before expensive AI review

### [x] 5. Split live execution into `live-safe` and `live-review`

Current `live` is too coarse.

Add two execution modes internally:

- `live-safe`
  - auto-submit allowed
  - only for high-yield narrow patterns
- `live-review`
  - patch may be generated
  - requires human approval before PR submit

User-facing command may remain `run`, but engine policy decides whether candidate can:

- auto-submit
- queue for approval
- stop

Goal:

- keep autonomy where safe
- stop pretending all “live” candidates deserve equal trust

### [x] 6. Add repo live-fit score

Separate repo fit for:

- inspect
- dry-run
- live targeted auto-submit

New repo-level score should consider:

- nearby tests
- file locality
- likely bug pattern mix
- prior self-review rejection rate
- prior submit rate
- surface sensitivity (`context_system`, `config`, `auth`, etc.)

Possible states:

- `live-targeted-ready`
- `dry-run-only`
- `inspect-only`

Goal:

- reduce live runs on repos that consistently waste tokens

### [x] 7. Feed scan output back into contribution ranking

`scan` and `run` should not live independently.

Use:

- `scan bug`
- `scan security`
- `scan trust`
- `scan audit`

to bias contribution strategy:

- trust-risk signals can lower live-fit
- bug-scan narrow findings can seed shortlist candidates
- audit findings can suppress risky dirs

Goal:

- make scan output operational, not informational only

### [x] 8. Add benchmark suite for submit-rate-per-token

Adopt fixed benchmark repos:

1. [x] `GPT-AGI/Clawd-Code`
   - hard targeted live benchmark
   - reveals semantic-risk patterns
2. `Alishahryar1/free-claude-code`
   - open-PR pacing benchmark
3. one small, tested repo from past successful submissions
   - success benchmark
4. one inspect-only repo
   - early-block benchmark
5. one low-source / high-artifact repo
   - scan/trust benchmark

For each benchmark, track:

- shortlisted
- planned
- generated
- self_review_rejected
- submitted
- est_tokens
- ai_calls
- outcome_code

Goal:

- prevent overfitting to one repo

### [x] 9. Add operator-visible “where it died” outcome taxonomy

Expand no-submit terminal outcomes.

Examples:

- `no_narrow_candidate`
- `shortlist_below_patchability_threshold`
- `plan_rejected`
- `structural_review_rejected`
- `semantic_review_rejected`
- `manual_approval_required`
- `existing_pr_already_open`

For no-submit runs, terminal summary should show:

- top narrowed candidate
- shortlist summary
- threshold miss if any
- pattern family
- exact stage of death
- token spend by stage

Goal:

- make every failed run immediately tunable

### [x] 10. Add token-spend budget by pattern family

Not just per run. Also per candidate class.

Example:

- `overbroad_exception_handling`
  - max 1 generated patch per run
  - max 1 semantic review
- `missing_input_validation`
  - allow 2 generated attempts
- `unchecked_response_shape`
  - allow 2 generated attempts

Goal:

- bias compute toward historically productive pattern classes

## Public / Interface Changes

- [x] existing commands stay:
  - `run`
  - `check`
  - `report`
  - `scan`
- [x] summaries gain more specific `outcome_code`
- [x] targeted run payloads gain:
  - `live_fit`
  - `pattern_policy`
  - `death_stage`
  - `tokens_per_stage`
  - `candidate_history`
- [x] optional future operator flag:
  - `--live-review`

## Test Plan

### Core engine tests

- [x] same-pattern semantic rejection stops further retries in same run
- [x] blocked pattern family never reaches codegen in targeted live mode
- [x] risky patch-shape classifier rejects exception-policy drift before semantic AI review
- [x] repo memory down-ranks previously bad patch families
- [x] `live-safe` repo/path auto-submits only allowed pattern classes
- [x] `live-review` repo/path queues instead of auto-submitting

### Regression tests

- [x] narrow validation fix still submits
- [x] inspect-only repo still blocks
- [x] open-PR pacing still owner-aware
- [x] existing targeted shortlist and threshold reporting still works
- [x] progress cards keep formal stage precedence

### Benchmark acceptance scenarios

1. `GPT-AGI/Clawd-Code`
   - fewer `generated`
   - lower `est_tokens`
   - no repeated `overbroad_exception_handling` churn
2. [x] small tested repo
   - equal or better submit rate
   - lower `tokens_per_submitted_pr`
3. [x] inspect-only repo
   - fast stop
   - `AI calls: 0`
4. [x] risky repo with policy-surface candidates
   - no auto-submit
   - either `manual_approval_required` or early reject

## Devin-Inspired Additions

### [x] 11. Repo context gathering sebelum AI call (CONTRIBUTING.md + issue labels)

Sebelum shortlisting, fetch dan parse:

- `CONTRIBUTING.md` / `CONTRIBUTING.rst` dari repo target
- Issue labels aktif (via GitHub API): `good first issue`, `help wanted`, `bug`
- Open PR titles untuk detect duplikasi topik

Gunakan hasilnya untuk:

- Adjust pattern policy berdasarkan maintainer preference (misal: "we require tests" → naikkan `test_proximity` threshold)
- Suppress kandidat jika ada PR open dengan topik serupa
- Tag repo dengan `maintainer_signals`: `{ requires_tests, prefers_small_diff, active_community }`

Goal:

- Submit PR yang aligned dengan ekspektasi maintainer, bukan hanya yang lolos internal review

### [x] 12. Sandboxed patch validation sebelum submit

Setelah codegen, sebelum semantic AI review:

- Jalankan patch di isolated environment (subprocess/container per kandidat)
- Run existing tests yang colocated dengan file target
- Capture: exit code, test output, lint errors

Jika sandbox gagal:

- Reject tanpa semantic review (hemat token)
- Log: `sandbox_failed` + reason ke outcome taxonomy

Jika sandbox pass:

- Lanjut ke semantic review dengan test output sebagai additional context
- Flag patch sebagai `sandbox_verified: true` → boleh masuk `auto_live_safe`

Goal:

- Eliminasi patch yang gagal test sebelum buang token AI review

### [x] 13. Self-debugging retry loop dengan test feedback

Jika sandbox atau test gagal, izinkan **1 retry** sebelum reject:

- Feed error output kembali ke AI sebagai context
- AI generate patch revision yang targeted ke error spesifik
- Re-run sandbox

Batas:

- Max 1 retry per kandidat
- Hanya jika error output actionable (bukan timeout/infra failure)
- Tidak berlaku untuk `overbroad_exception_handling` atau pattern `blocked_for_targeted_live`

Outcome codes baru:

- `sandbox_retry_success` → lanjut ke review
- `sandbox_retry_failed` → reject, log dua iterasi

Goal:

- Recover dari patch yang "hampir benar" tanpa burn banyak token

## Phase 3 — Demand-Driven & Deep Execution

Adaptasi dari Devin, SWE-agent, AutoCodeRover, Agentless, dan Aider.
Fokus: dari **pattern-provider** menjadi **issue-solver** dengan execution loop yang nyata.

Priority tier:

```
TIER 1 — High impact, lower complexity (implement next)
  14. Issue-driven targeting
  15. Git history context sebelum patch
  16. AST-based scanner (ganti regex)

TIER 2 — High impact, higher complexity
  17. Real clone + pip install execution environment
  18. Multi-turn repair loop (hingga 3 iterasi)
  19. PR revision otomatis dari maintainer comment
```

---

### [x] 14. Issue-driven targeting (SWE-agent style)

**Problem sekarang:** Rover scan kode untuk pola yang sudah dikenal. Ini *provider-driven* — Rover yang memutuskan apa yang "perlu" diperbaiki. Maintainer tidak diminta.

**Adaptasi:** Fetch open issues dari repo target, solve apa yang maintainer sendiri minta.

Flow baru:

```
fetch open issues (label: bug, good first issue, help wanted)
  → filter: issues dengan body konkret (ada stacktrace / repro steps)
  → rank: recency × label priority × comment activity
  → feed issue body ke AI sebagai context utama
  → generate patch yang address issue tersebut
```

Signal yang di-extract dari issue:

- Error message / stacktrace → exact failure mode
- File/line yang disebutkan → target file langsung
- Linked PR yang pernah close → hindari duplikasi approach
- Maintainer label `good first issue` → high acceptance signal

Benefit vs pattern scan:

- PR dari issue: maintainer sudah minta → acceptance rate jauh lebih tinggi
- Tidak perlu convince bahwa ini bug — mereka sudah bilang
- Issue body = built-in test case / repro steps

New fields di `Opportunity`:

- `source_issue_number: int`
- `source_issue_url: str`
- `issue_body_snippet: str`
- `issue_labels: list[str]`

Policy:

- Issue-driven opportunity dapat `acceptance_score` bonus +15
- Issue dengan label `good first issue` → langsung `auto_live_safe`
- Issue tanpa repro steps → `dry_run_first`

Goal:

- Submit PR yang diminta, bukan PR yang ditemukan

---

### [x] 15. Git history context sebelum patch (AutoCodeRover style)

**Problem sekarang:** Rover hanya baca current file content. Tidak tahu kenapa kode ditulis begitu — bisa saja intentional, bisa sudah pernah dicoba lalu di-revert.

**Adaptasi:** Gunakan GitHub API untuk baca git history file target sebelum generate patch.

Data yang di-fetch:

```
GET /repos/{owner}/{repo}/commits?path={target_file}&per_page=10
```

Extract dari history:

- Commit messages yang menyebut `fix`, `revert`, `hotfix` pada file ini → tanda bahwa area ini sudah fragile
- Commit yang touch exact baris yang akan dipatch → author awareness
- Pernah ada `revert` di baris yang sama → high-risk signal, downgrade ke `live-review`

Signal ke AI:

- Sertakan snippet commit message relevan di prompt: "This file was last modified: [message] [author] [date]"
- Jika ada revert history → tambahkan warning di patch rationale

Goal:

- Avoid patching area yang sudah pernah di-revert (regression risk)
- Beri AI konteks historis untuk generate patch yang lebih informed

---

### [x] 16. AST-based scanner (ganti regex di PatternScanner)

**Problem sekarang:** `PatternScanner` di `opportunity_engine.py` pakai regex murni. Ini menyebabkan:

- False positive: match `except Exception` di dalam string atau docstring
- Tidak tahu apakah call ada di dalam try/except yang sudah ada
- Tidak bisa traverse call graph

**Adaptasi:** Ganti regex dengan Python `ast.walk()` untuk pattern detection yang akurat.

Contoh konkret:

```python
# Sekarang (regex — false positive risk):
if "except Exception" in line:
    ...

# Baru (AST — precise):
for node in ast.walk(tree):
    if isinstance(node, ast.ExceptHandler):
        if node.type is None or (isinstance(node.type, ast.Name) and node.type.id == "Exception"):
            ...
```

Pattern yang paling benefit dari AST:

- `overbroad_exception_handling` → ExceptHandler node
- `missing_timeout` → Call node untuk requests/httpx
- `resource_cleanup_gap` → Assign + Call(open) tanpa With node
- `unsafe_subprocess` → Call node dengan shell=True keyword

Benefit:

- Eliminasi false positive → lebih sedikit kandidat yang lolos tapi gagal di review
- Bisa detect nested context (apakah call sudah di dalam with statement?)
- Evidence lebih precise → baris exact dari AST node, bukan regex match

Goal:

- Tingkatkan precision shortlist → lebih sedikit token buang untuk false positive

---

### [ ] 17. Real clone + pip install execution environment

**Problem sekarang:** Sandbox kita (item 12) hanya `py_compile` karena tidak ada dependencies terinstall. Patch yang lolos syntax check bisa tetap broken di runtime.

**Adaptasi:** Clone repo ke tempdir, install deps, run actual test suite.

Flow:

```
git clone --depth=1 {repo_url} /tmp/rover_sandbox_{run_id}/
cd /tmp/rover_sandbox_{run_id}/
pip install -e . --quiet  (atau pip install -r requirements.txt)
apply patch (write changed files)
pytest {test_target} -x --tb=short -q --timeout=30
capture: returncode, stdout, stderr
cleanup: rm -rf /tmp/rover_sandbox_{run_id}/
```

Guardrails:

- Max clone size: 50MB (skip jika lebih besar)
- Max install time: 60 detik (timeout → fallback ke py_compile saja)
- Max test time: 30 detik per file
- Jalankan di subprocess dengan resource limits
- Skip jika repo butuh system deps (deteksi dari `setup.py` / `Makefile`)

Outcome codes baru:

- `real_sandbox_pass` → test actual pass → highest confidence untuk auto-submit
- `real_sandbox_dep_timeout` → fallback ke py_compile
- `real_sandbox_test_fail_actionable` → trigger retry loop (item 18)

Goal:

- `real_sandbox_pass` = strong signal bahwa PR tidak akan di-reject karena broken tests

---

### [x] 18. Multi-turn repair loop (Agentless / Devin style)

**Problem sekarang:** Max 1 retry (item 13). Devin bisa iterate 10+ kali. Agentless pakai loop: localize → patch → test → repair → test.

**Adaptasi:** Extend retry loop hingga max 3 iterasi untuk kandidat `auto_live_safe`.

Loop:

```
attempt = 0
while attempt < MAX_REPAIR_ATTEMPTS:
    patch = generate_patch(candidate, error_context)
    result = run_sandbox(patch)
    if result.pass:
        break
    if not result.actionable:
        break  # infra failure, tidak bisa diperbaiki
    error_context = result.error_output
    attempt += 1
```

Policy:

- `auto_live_safe` pattern: max 3 iterasi
- `dry_run_first` pattern: max 2 iterasi
- `blocked_for_targeted_live`: 0 iterasi (tidak boleh retry)
- Setiap iterasi burn token → hitung terhadap budget pattern family (item 10)

Outcome codes baru:

- `repair_loop_success_iter_{n}` → berhasil di iterasi ke-n
- `repair_loop_exhausted` → habis iterasi, submit patch terbaik atau reject

Goal:

- Recover dari patch yang "hampir benar" tanpa batas hard 1 retry

---

### [x] 19. PR revision otomatis dari maintainer comment

**Problem sekarang:** Setelah PR submitted dan maintainer comment, Rover bisa `contrib_check` dan `contrib_respond` — tapi hanya generate teks balasan, tidak push code revision. PR akhirnya di-close tanpa merge.

**Adaptasi:** Baca maintainer comment → generate code revision → push commit ke branch PR yang ada.

Flow:

```
contrib_check → detect PR dengan maintainer comment yang belum dibalas
  → classify comment:
      "needs test" → generate test file, push commit
      "wrong approach" → re-generate patch dengan comment sebagai context
      "style issue" → apply specific style fix, push commit
      "LGTM / approve" → no action needed
  → push commit ke existing fork branch
  → reply comment dengan link ke commit
```

Prerequisite:

- Fork branch masih hidup (tidak dihapus maintainer)
- Token punya write access ke fork

Signal dari comment untuk classify:

- Keywords: `test`, `spec`, `coverage` → butuh test
- Keywords: `revert`, `undo`, `wrong` → wrong approach, re-generate
- Keywords: `style`, `format`, `lint`, `pep8` → style fix
- Keywords: `LGTM`, `approved`, `looks good` → success, no action

Goal:

- Naikkan merge rate dari PR yang sudah submitted
- Convert "needs revision" dari dead-end menjadi iterasi

---

## Assumptions

- Primary business goal is **submit rate per token**, not raw run count.
- Targeted live mode should become stricter, not broader.
- Search mode may remain looser, but must not inherit unsafe targeted rules accidentally.
- Existing scan and inspect systems remain inputs; they should become stronger ranking signal sources.
- Issue-driven targeting (item 14) is the highest-leverage change for merge rate.
- Real execution environment (item 17) is the highest-leverage change for patch quality.
- Tagline remains: **Rover Builds - Find. Fix. Ship. Repeat.**
  Engine should earn that by shipping more real PRs per unit of compute.
