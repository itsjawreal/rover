from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import DATA_DIR
from src.opportunity_engine import Opportunity, count_repo_files
from src.scraper import RepoCandidate

# ── Constants ────────────────────────────────────────────────
PR_ENGINE_DB_FILE = DATA_DIR / "pr_engine.sqlite3"
REPO_COOLDOWN_DAYS = int(os.getenv("PR_REPO_COOLDOWN_DAYS", "3"))


# ── Time helpers ─────────────────────────────────────────────
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


# ── Persistence ──────────────────────────────────────────────
class ContributionStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or PR_ENGINE_DB_FILE
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    target_count INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    summary_json TEXT
                );
                CREATE TABLE IF NOT EXISTS campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_full_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    goal TEXT NOT NULL DEFAULT 'contribution',
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS maintainers (
                    login TEXT PRIMARY KEY,
                    style_profile_json TEXT NOT NULL DEFAULT '{}',
                    response_count INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT
                );
                CREATE TABLE IF NOT EXISTS repo_guidelines (
                    repo_full_name TEXT PRIMARY KEY,
                    contributing_path TEXT NOT NULL DEFAULT '',
                    pr_template_path TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_full_name TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    labels_json TEXT NOT NULL DEFAULT '[]',
                    state TEXT NOT NULL,
                    source_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    UNIQUE(repo_full_name, issue_number)
                );
                CREATE TABLE IF NOT EXISTS repos (
                    full_name TEXT PRIMARY KEY,
                    repo_profile_json TEXT NOT NULL DEFAULT '{}',
                    responsiveness_profile_json TEXT NOT NULL DEFAULT '{}',
                    pattern_history_json TEXT NOT NULL DEFAULT '{}',
                    file_hotspots_json TEXT NOT NULL DEFAULT '{}',
                    cooldown_until TEXT,
                    merged_count INTEGER NOT NULL DEFAULT 0,
                    closed_count INTEGER NOT NULL DEFAULT 0,
                    feedback_count INTEGER NOT NULL DEFAULT 0,
                    rejection_count INTEGER NOT NULL DEFAULT 0,
                    last_acceptance_score INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT
                );
                CREATE TABLE IF NOT EXISTS opportunities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    repo_full_name TEXT NOT NULL,
                    target_file TEXT NOT NULL,
                    pattern_type TEXT NOT NULL,
                    failure_mode TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    patch_scope INTEGER NOT NULL DEFAULT 1,
                    test_target TEXT,
                    acceptance_score INTEGER NOT NULL DEFAULT 0,
                    opportunity_kind TEXT NOT NULL DEFAULT 'bugfix',
                    source_ref TEXT NOT NULL DEFAULT '',
                    maintainer_intent INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'code_scan',
                    why_advanced TEXT NOT NULL DEFAULT '',
                    why_rejected TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opportunity_id INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pull_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opportunity_id INTEGER,
                    repo_full_name TEXT NOT NULL,
                    pr_url TEXT NOT NULL UNIQUE,
                    pr_title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lifecycle_state TEXT NOT NULL DEFAULT 'open',
                    fork_name TEXT NOT NULL DEFAULT '',
                    branch_name TEXT NOT NULL DEFAULT '',
                    improvement_type TEXT NOT NULL DEFAULT '',
                    submitted_at TEXT NOT NULL,
                    resolved_at TEXT,
                    maintainer_signal TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS pr_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pr_url TEXT NOT NULL,
                    comment_id INTEGER NOT NULL,
                    author TEXT NOT NULL,
                    body TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(pr_url, comment_id, source)
                );
                CREATE TABLE IF NOT EXISTS pr_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pr_url TEXT NOT NULL,
                    review_id INTEGER NOT NULL,
                    author TEXT NOT NULL,
                    state TEXT NOT NULL,
                    body TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    UNIQUE(pr_url, review_id)
                );
                CREATE TABLE IF NOT EXISTS rejections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    opportunity_id INTEGER,
                    repo_full_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    human_summary TEXT NOT NULL,
                    target_file TEXT NOT NULL DEFAULT '',
                    pattern_type TEXT NOT NULL DEFAULT '',
                    cooldown_until TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repo_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    repo_full_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_summaries (
                    run_id INTEGER PRIMARY KEY,
                    summary_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._migrate_existing_schema(conn)

    def _migrate_existing_schema(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(opportunities)").fetchall()}
        if "source" not in columns:
            conn.execute("ALTER TABLE opportunities ADD COLUMN source TEXT NOT NULL DEFAULT 'code_scan'")
        if "opportunity_kind" not in columns:
            conn.execute("ALTER TABLE opportunities ADD COLUMN opportunity_kind TEXT NOT NULL DEFAULT 'bugfix'")
        if "source_ref" not in columns:
            conn.execute("ALTER TABLE opportunities ADD COLUMN source_ref TEXT NOT NULL DEFAULT ''")
        if "maintainer_intent" not in columns:
            conn.execute("ALTER TABLE opportunities ADD COLUMN maintainer_intent INTEGER NOT NULL DEFAULT 0")
        pr_columns = {row["name"] for row in conn.execute("PRAGMA table_info(pull_requests)").fetchall()}
        if "lifecycle_state" not in pr_columns:
            conn.execute("ALTER TABLE pull_requests ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'open'")

    def start_run(self, mode: str, target_count: int) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs(mode, target_count, started_at) VALUES (?, ?, ?)",
                (mode, target_count, iso_now()),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, summary: dict) -> None:
        payload = json.dumps(summary, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, summary_json = ? WHERE id = ?",
                (iso_now(), payload, run_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO run_summaries(run_id, summary_json, created_at) VALUES (?, ?, ?)",
                (run_id, payload, iso_now()),
            )

    def upsert_repo_profile(self, candidate: RepoCandidate, acceptance_score: int) -> None:
        py_count, ts_count, test_count = count_repo_files(candidate.files)
        profile = {
            "stars": candidate.stars,
            "forks": candidate.forks,
            "license": candidate.license,
            "pushed_days_ago": candidate.pushed_days_ago,
            "py_count": py_count,
            "ts_count": ts_count,
            "test_count": test_count,
            "file_count": len(candidate.files),
        }
        with self._connect() as conn:
            row = conn.execute(
                "SELECT responsiveness_profile_json, pattern_history_json, file_hotspots_json FROM repos WHERE full_name = ?",
                (candidate.full_name,),
            ).fetchone()
            responsiveness_json = row["responsiveness_profile_json"] if row else "{}"
            pattern_history_json = row["pattern_history_json"] if row else "{}"
            file_hotspots_json = row["file_hotspots_json"] if row else "{}"
            conn.execute(
                """
                INSERT INTO repos(
                    full_name, repo_profile_json, responsiveness_profile_json,
                    pattern_history_json, file_hotspots_json, last_acceptance_score, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(full_name) DO UPDATE SET
                    repo_profile_json = excluded.repo_profile_json,
                    responsiveness_profile_json = excluded.responsiveness_profile_json,
                    pattern_history_json = excluded.pattern_history_json,
                    file_hotspots_json = excluded.file_hotspots_json,
                    last_acceptance_score = excluded.last_acceptance_score,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    candidate.full_name,
                    json.dumps(profile, ensure_ascii=False),
                    responsiveness_json,
                    pattern_history_json,
                    file_hotspots_json,
                    acceptance_score,
                    iso_now(),
                ),
            )

    def record_repo_event(self, run_id: int | None, repo_full_name: str, event_type: str, summary: str, details: dict | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO repo_events(run_id, repo_full_name, event_type, summary, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, repo_full_name, event_type, summary, json.dumps(details or {}, ensure_ascii=False), iso_now()),
            )

    def create_opportunity(self, run_id: int | None, opportunity: Opportunity, source: str = "code_scan") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO opportunities(
                    run_id, repo_full_name, target_file, pattern_type, failure_mode,
                    evidence, patch_scope, test_target, acceptance_score, opportunity_kind,
                    source_ref, maintainer_intent, state,
                    source, why_advanced, why_rejected, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    opportunity.repo_full_name,
                    opportunity.target_file,
                    opportunity.pattern_type,
                    opportunity.failure_mode,
                    opportunity.evidence,
                    opportunity.patch_scope,
                    opportunity.test_target,
                    opportunity.acceptance_score,
                    opportunity.opportunity_kind,
                    opportunity.source_ref,
                    1 if opportunity.maintainer_intent else 0,
                    opportunity.state,
                    source,
                    opportunity.why_advanced,
                    opportunity.why_rejected,
                    iso_now(),
                    iso_now(),
                ),
            )
            return int(cur.lastrowid)

    def transition_opportunity(self, opportunity_id: int, new_state: str, why_advanced: str = "", why_rejected: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE opportunities
                SET state = ?, why_advanced = ?, why_rejected = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_state, why_advanced, why_rejected, iso_now(), opportunity_id),
            )

    def record_attempt(self, opportunity_id: int, phase: str, attempt_no: int, outcome: str, summary: str, duration_ms: int = 0) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO attempts(opportunity_id, phase, attempt_no, outcome, summary, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (opportunity_id, phase, attempt_no, outcome, summary, duration_ms, iso_now()),
            )

    def reject_opportunity(
        self,
        run_id: int | None,
        opportunity: Opportunity,
        reason_code: str,
        human_summary: str,
        state: str,
        opportunity_id: int | None = None,
    ) -> None:
        cooldown_until = None
        with self._connect() as conn:
            if opportunity_id is not None:
                conn.execute(
                    "UPDATE opportunities SET state = 'REJECT', why_rejected = ?, updated_at = ? WHERE id = ?",
                    (human_summary, iso_now(), opportunity_id),
                )
            recent = conn.execute(
                """
                SELECT COUNT(*) AS n FROM rejections
                WHERE repo_full_name = ? AND reason_code = ? AND created_at >= ?
                """,
                (
                    opportunity.repo_full_name,
                    reason_code,
                    (utcnow() - timedelta(days=14)).isoformat(),
                ),
            ).fetchone()
            repeated = int(recent["n"]) >= 1
            if repeated:
                cooldown_until = (utcnow() + timedelta(days=REPO_COOLDOWN_DAYS)).isoformat()
            conn.execute(
                """
                INSERT INTO rejections(
                    run_id, opportunity_id, repo_full_name, state, reason_code, human_summary,
                    target_file, pattern_type, cooldown_until, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    opportunity_id,
                    opportunity.repo_full_name,
                    state,
                    reason_code,
                    human_summary,
                    opportunity.target_file,
                    opportunity.pattern_type,
                    cooldown_until,
                    iso_now(),
                ),
            )
            row = conn.execute(
                "SELECT file_hotspots_json, pattern_history_json, responsiveness_profile_json FROM repos WHERE full_name = ?",
                (opportunity.repo_full_name,),
            ).fetchone()
            hotspots = json.loads(row["file_hotspots_json"]) if row else {}
            pattern_history = json.loads(row["pattern_history_json"]) if row else {}
            responsiveness = json.loads(row["responsiveness_profile_json"]) if row else {}
            hotspots[opportunity.target_file] = hotspots.get(opportunity.target_file, 0) + 1
            pattern_stats = pattern_history.get(opportunity.pattern_type, {"qualified": 0, "rejected": 0, "submitted": 0})
            pattern_stats["rejected"] += 1
            pattern_history[opportunity.pattern_type] = pattern_stats
            responsiveness["last_rejection_reason"] = reason_code
            conn.execute(
                """
                INSERT INTO repos(
                    full_name, repo_profile_json, responsiveness_profile_json, pattern_history_json,
                    file_hotspots_json, cooldown_until, rejection_count, last_acceptance_score, last_seen_at
                ) VALUES (?, '{}', ?, ?, ?, ?, 1, 0, ?)
                ON CONFLICT(full_name) DO UPDATE SET
                    responsiveness_profile_json = ?,
                    pattern_history_json = ?,
                    file_hotspots_json = ?,
                    cooldown_until = COALESCE(?, repos.cooldown_until),
                    rejection_count = repos.rejection_count + 1,
                    last_seen_at = ?
                """,
                (
                    opportunity.repo_full_name,
                    json.dumps(responsiveness, ensure_ascii=False),
                    json.dumps(pattern_history, ensure_ascii=False),
                    json.dumps(hotspots, ensure_ascii=False),
                    cooldown_until,
                    iso_now(),
                    json.dumps(responsiveness, ensure_ascii=False),
                    json.dumps(pattern_history, ensure_ascii=False),
                    json.dumps(hotspots, ensure_ascii=False),
                    cooldown_until,
                    iso_now(),
                ),
            )

    def repo_score_adjustment(self, repo_full_name: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT cooldown_until, merged_count, feedback_count, rejection_count,
                       responsiveness_profile_json
                FROM repos WHERE full_name = ?
                """,
                (repo_full_name,),
            ).fetchone()
        if not row:
            return 0
        score = row["merged_count"] * 12 + row["feedback_count"] * 4 - row["rejection_count"] * 4
        cooldown_until = row["cooldown_until"]
        if cooldown_until:
            try:
                if datetime.fromisoformat(cooldown_until) > utcnow():
                    score -= 25
            except Exception:
                pass
        responsiveness = json.loads(row["responsiveness_profile_json"] or "{}")
        if responsiveness.get("last_signal") == "merged":
            score += 8
        if responsiveness.get("last_signal") == "closed":
            score -= 6
        return score

    def has_open_pr(self, repo_full_name: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM pull_requests WHERE repo_full_name = ? AND status = 'open' LIMIT 1",
                (repo_full_name,),
            ).fetchone()
        return row is not None

    def record_pull_request(
        self,
        opportunity_id: int | None,
        repo_full_name: str,
        pr_url: str,
        pr_title: str,
        fork_name: str,
        branch_name: str,
        improvement_type: str,
        status: str = "open",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pull_requests(
                    opportunity_id, repo_full_name, pr_url, pr_title, status,
                    lifecycle_state, fork_name, branch_name, improvement_type, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    opportunity_id,
                    repo_full_name,
                    pr_url,
                    pr_title,
                    status,
                    status,
                    fork_name,
                    branch_name,
                    improvement_type,
                    iso_now(),
                ),
            )
            conn.execute(
                """
                INSERT INTO repos(
                    full_name, repo_profile_json, responsiveness_profile_json, pattern_history_json,
                    file_hotspots_json, last_seen_at
                ) VALUES (?, '{}', '{}', '{}', '{}', ?)
                ON CONFLICT(full_name) DO UPDATE SET last_seen_at = ?
                """,
                (repo_full_name, iso_now(), iso_now()),
            )
            if opportunity_id is not None:
                conn.execute(
                    "UPDATE opportunities SET state = 'SUBMIT', why_advanced = ?, updated_at = ? WHERE id = ?",
                    ("PR submitted successfully", iso_now(), opportunity_id),
                )

    def update_pr_status(self, pr_url: str, status: str, maintainer_signal: str = "", resolved_at: str = "") -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT repo_full_name, opportunity_id FROM pull_requests WHERE pr_url = ?",
                (pr_url,),
            ).fetchone()
            conn.execute(
                """
                UPDATE pull_requests
                SET status = ?, lifecycle_state = ?, maintainer_signal = ?,
                    resolved_at = COALESCE(NULLIF(?, ''), resolved_at)
                WHERE pr_url = ?
                """,
                (status, status, maintainer_signal, resolved_at, pr_url),
            )
            if not row:
                return
            repo_full_name = row["repo_full_name"]
            opportunity_id = row["opportunity_id"]
            increments = {"merged": "merged_count", "closed": "closed_count"}
            if status in increments:
                conn.execute(
                    f"UPDATE repos SET {increments[status]} = {increments[status]} + 1, last_seen_at = ? WHERE full_name = ?",
                    (iso_now(), repo_full_name),
                )
            responsiveness = {"last_signal": status}
            if maintainer_signal:
                responsiveness["maintainer_signal"] = maintainer_signal
            existing = conn.execute(
                "SELECT responsiveness_profile_json FROM repos WHERE full_name = ?",
                (repo_full_name,),
            ).fetchone()
            existing_json = json.loads(existing["responsiveness_profile_json"] or "{}") if existing else {}
            existing_json.update(responsiveness)
            conn.execute(
                """
                INSERT INTO repos(
                    full_name, repo_profile_json, responsiveness_profile_json, pattern_history_json,
                    file_hotspots_json, last_seen_at
                ) VALUES (?, '{}', ?, '{}', '{}', ?)
                ON CONFLICT(full_name) DO UPDATE SET responsiveness_profile_json = ?, last_seen_at = ?
                """,
                (
                    repo_full_name,
                    json.dumps(existing_json, ensure_ascii=False),
                    iso_now(),
                    json.dumps(existing_json, ensure_ascii=False),
                    iso_now(),
                ),
            )
            if opportunity_id:
                pattern_row = conn.execute(
                    "SELECT pattern_type FROM opportunities WHERE id = ?",
                    (opportunity_id,),
                ).fetchone()
                repo_row = conn.execute(
                    "SELECT pattern_history_json FROM repos WHERE full_name = ?",
                    (repo_full_name,),
                ).fetchone()
                pattern_history = json.loads(repo_row["pattern_history_json"] or "{}") if repo_row else {}
                pattern_type = pattern_row["pattern_type"] if pattern_row else "unknown"
                stats = pattern_history.get(pattern_type, {"qualified": 0, "rejected": 0, "submitted": 0})
                if status == "merged":
                    stats["submitted"] += 1
                pattern_history[pattern_type] = stats
                conn.execute(
                    "UPDATE repos SET pattern_history_json = ? WHERE full_name = ?",
                    (json.dumps(pattern_history, ensure_ascii=False), repo_full_name),
                )

    def record_feedback_signal(self, repo_full_name: str, signal: str) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT responsiveness_profile_json FROM repos WHERE full_name = ?",
                (repo_full_name,),
            ).fetchone()
            responsiveness = json.loads(row["responsiveness_profile_json"] or "{}") if row else {}
            responsiveness["last_signal"] = signal
            conn.execute(
                """
                INSERT INTO repos(
                    full_name, repo_profile_json, responsiveness_profile_json, pattern_history_json,
                    file_hotspots_json, feedback_count, last_seen_at
                ) VALUES (?, '{}', ?, '{}', '{}', 1, ?)
                ON CONFLICT(full_name) DO UPDATE SET
                    responsiveness_profile_json = ?,
                    feedback_count = repos.feedback_count + 1,
                    last_seen_at = ?
                """,
                (
                    repo_full_name,
                    json.dumps(responsiveness, ensure_ascii=False),
                    iso_now(),
                    json.dumps(responsiveness, ensure_ascii=False),
                    iso_now(),
                ),
            )

    def summarize_run(self, run_id: int) -> dict:
        with self._connect() as conn:
            state_rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM opportunities WHERE run_id = ? GROUP BY state",
                (run_id,),
            ).fetchall()
            rejection_rows = conn.execute(
                "SELECT reason_code, COUNT(*) AS n FROM rejections WHERE run_id = ? GROUP BY reason_code ORDER BY n DESC, reason_code ASC",
                (run_id,),
            ).fetchall()
            queued_rows = conn.execute(
                """
                SELECT repo_full_name, target_file, pattern_type, failure_mode, acceptance_score, updated_at
                FROM opportunities
                WHERE run_id = ? AND state = 'READY'
                ORDER BY acceptance_score DESC, updated_at DESC
                """,
                (run_id,),
            ).fetchall()
            pr_rows = conn.execute(
                """
                SELECT pr.repo_full_name, pr.pr_url, pr.pr_title
                FROM pull_requests pr
                JOIN opportunities opp ON opp.id = pr.opportunity_id
                WHERE opp.run_id = ?
                ORDER BY pr.id ASC
                """,
                (run_id,),
            ).fetchall()
            discover_rows = conn.execute(
                "SELECT COUNT(*) AS n FROM repo_events WHERE run_id = ? AND event_type = 'discover_selected'",
                (run_id,),
            ).fetchone()
        state_counts = {row["state"]: int(row["n"]) for row in state_rows}
        top_rejections = [(row["reason_code"], int(row["n"])) for row in rejection_rows[:5]]
        queued = self._dedupe_ready_rows(queued_rows)
        submitted_prs = [dict(row) for row in pr_rows]
        bottleneck = ""
        if top_rejections:
            dominant = top_rejections[0]
            dominant_reason, dominant_count = dominant
            submitted_count = len(submitted_prs)
            if dominant_count > 0:
                if submitted_count > 0:
                    bottleneck = (
                        f"{submitted_count} PR submitted, but {dominant_reason} still dominated "
                        f"{dominant_count} rejected opportunity decisions"
                    )
                else:
                    bottleneck = f"0 PR because {dominant_reason} dominated {dominant_count} opportunity decisions"
        return {
            "discovered": int(discover_rows["n"]) if discover_rows else 0,
            "state_counts": state_counts,
            "top_rejections": top_rejections,
            "queued": queued,
            "submitted_prs": submitted_prs,
            "bottleneck": bottleneck,
        }

    def latest_run_summaries(self, limit: int = 5) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, summary_json, created_at
                FROM run_summaries
                ORDER BY run_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        summaries: list[dict] = []
        for row in rows:
            try:
                summary = json.loads(row["summary_json"])
            except Exception:
                summary = {}
            summary.setdefault("run_id", row["run_id"])
            summary.setdefault("created_at", row["created_at"])
            summaries.append(summary)
        return summaries

    def queued_opportunities(self, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, repo_full_name, target_file, pattern_type, failure_mode, acceptance_score, updated_at
                FROM opportunities
                WHERE state = 'READY'
                ORDER BY acceptance_score DESC, updated_at DESC
                """,
            ).fetchall()
        return self._dedupe_ready_rows(rows, limit=limit)

    def _dedupe_ready_rows(self, rows: list[sqlite3.Row], limit: int | None = None) -> list[dict]:
        unique: list[dict] = []
        seen: set[tuple[str, str, str, str]] = set()
        for row in rows:
            item = dict(row)
            signature = (
                item.get("repo_full_name", ""),
                item.get("target_file", ""),
                item.get("pattern_type", ""),
                item.get("failure_mode", ""),
            )
            if signature in seen:
                continue
            seen.add(signature)
            unique.append(item)
            if limit is not None and len(unique) >= limit:
                break
        return unique


PREngineStore = ContributionStore
