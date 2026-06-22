# Validasi Runtime Newplan

Dokumen ini adalah langkah lanjutan setelah semua item di `newplan.md` sudah:

- implemented
- tested

Tujuannya adalah membuktikan perilaku runtime nyata, terutama untuk:

- policy gate
- live-fit
- scan -> ranking
- outcome taxonomy
- benchmark KPI

## Prasyarat

Pastikan environment siap:

```bash
gh auth status
codex login status
python3 -m app.builder --doctor
```

Opsional, cek profil Rover:

```bash
python3 -m app.builder --profile
```

## Cara Mencatat Hasil

Untuk setiap repo benchmark, catat:

- `repo`
- `mode`
- `outcome_code`
- `death_stage`
- `ai_calls`
- `est_tokens`
- `shortlisted`
- `generated`
- `self_review_rejected`
- `state_counts`
- `scope_notes`
- `next_steps`

Sumber data utama:

```bash
python3 -m app.builder --contrib-report
python3 -m app.builder --contrib-check
```

## Benchmark 1: Hard Targeted Live

Repo:

- `GPT-AGI/Clawd-Code`

Command:

```bash
python3 -m app.builder --repo-inspect GPT-AGI/Clawd-Code
python3 -m app.builder --contrib GPT-AGI/Clawd-Code --1 --dry-run
python3 -m app.builder --contrib-report
python3 -m app.builder --contrib-check
```

Checklist lolos:

- tidak ada churn berulang `overbroad_exception_handling`
- `outcome_code` spesifik
- `death_stage` terisi benar
- `token_spend_by_stage` masuk akal
- kandidat risky tidak diarahkan ke auto-submit

## Benchmark 2: Open-PR Pacing

Repo:

- `Alishahryar1/free-claude-code`

Command:

```bash
python3 -m app.builder --repo-inspect Alishahryar1/free-claude-code
python3 -m app.builder --contrib Alishahryar1/free-claude-code --1 --dry-run
python3 -m app.builder --contrib-report
python3 -m app.builder --contrib-check
```

Checklist lolos:

- pacing PR open tetap owner-aware
- kalau sudah ada PR open, hasilnya eksplisit
- queue/report tetap konsisten

## Benchmark 3: Success Repo

Ganti `owner/repo-success` dengan repo kecil, aktif, dan punya test yang pernah cocok untuk Rover.

Command dry-run:

```bash
python3 -m app.builder --repo-inspect owner/repo-success
python3 -m app.builder --contrib owner/repo-success --1 --dry-run
python3 -m app.builder --contrib-report
```

Kalau dry-run sehat, lanjut live:

```bash
python3 -m app.builder --contrib owner/repo-success --1
python3 -m app.builder --contrib-report
python3 -m app.builder --contrib-check
```

Checklist lolos:

- submit rate minimal sama atau lebih baik
- `tokens_per_submitted_pr` turun atau stabil
- fix sempit seperti validation/timeout tetap lolos

## Benchmark 4: Inspect-Only Repo

Ganti `owner/repo-inspect-only` dengan repo yang diperkirakan inactive, terlalu broad, atau tidak cocok untuk targeted run.

Command:

```bash
python3 -m app.builder --repo-inspect owner/repo-inspect-only
python3 -m app.builder --contrib owner/repo-inspect-only --1 --dry-run
python3 -m app.builder --contrib-report
```

Checklist lolos:

- berhenti cepat
- `AI calls: 0` atau sangat rendah
- outcome eksplisit `inspect-only` atau blocked
- `scope_notes` dan `next_steps` membantu operator

## Benchmark 5: Low-Source / High-Artifact

Ganti `owner/repo-low-source-artifact` dengan repo distribusi/artifact-heavy.

Command:

```bash
python3 -m app.builder --repo-inspect owner/repo-low-source-artifact
python3 -m app.builder --scan owner/repo-low-source-artifact --kind trust
python3 -m app.builder --scan owner/repo-low-source-artifact --kind audit
python3 -m app.builder --contrib owner/repo-low-source-artifact --1 --dry-run
python3 -m app.builder --contrib-report
```

Checklist lolos:

- trust/audit scan menurunkan live-fit
- repo risky tidak diarahkan ke auto-submit
- guardrail trust/risk muncul di report dan outcome

## Urutan Eksekusi yang Disarankan

Jalankan urutan ini untuk tiap repo:

1. `--repo-inspect`
2. `--scan` bila repo trust/audit candidate
3. `--contrib ... --dry-run`
4. `--contrib-report`
5. `--contrib-check`

## Live Run yang Aman

Hanya lakukan live submit pada satu repo benchmark yang paling aman:

- repo kecil
- aktif
- punya test
- dry-run sudah terlihat sehat

Command:

```bash
python3 -m app.builder --contrib owner/repo-success --1
```

## Kriteria Kelulusan Global

Validasi runtime dianggap cukup baik jika:

- repo risky berhenti lebih cepat dibanding sebelum newplan
- repo sehat tetap bisa masuk jalur submit
- `manual_approval_required` muncul untuk surface yang semestinya tidak auto-live
- `outcome_code` dan `death_stage` konsisten dengan kejadian run
- KPI `submitted_prs / est_tokens` tidak memburuk pada repo sehat
