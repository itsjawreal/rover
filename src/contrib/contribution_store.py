from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.config import PR_LOG_FILE, ROOT, MENISIK_STATE_DIR
from src.contrib.opportunity_engine import Opportunity, count_repo_files
from src.github.scraper import RepoCandidate

# ── Constants ────────────────────────────────────────────────


def _default_pr_engine_db_file() -> Path:
    if env_path := os.getenv("PR_ENGINE_DB_PATH", "").strip():
        return Path(env_path).expanduser()

    return MENISIK_STATE_DIR / "pr_engine.sqlite3"


PR_ENGINE_DB_FILE = _default_pr_engine_db_file()
REPO_COOLDOWN_DAYS = int(os.getenv("PR_REPO_COOLDOWN_DAYS", "3"))


def _active_owner_login() -> str:
    try:
        from src.github.fork import get_current_github_login

        return get_current_github_login().strip().lower()
    except Exception:
        return ""


def _legacy_pr_log_candidates() -> list[Path]:
    candidates = [PR_LOG_FILE, ROOT / "data" / "pr_log.json"]
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


# ── Time helpers ─────────────────────────────────────────────
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


def _target_dir(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    if "/" not in normalized:
        return "."
    return normalized.rsplit("/", 1)[0]


def _pattern_stats() -> dict:
    return {
        "qualified": 0,
        "rejected": 0,
        "submitted": 0,
        "closed_without_merge": 0,
        "target_dirs": {},
        "target_files": {},
        "had_test_target": {"yes": 0, "no": 0},
        "self_review_reasons": {},
        "maintainer_feedback_shapes": {},
    }


def _bump_counter(mapping: dict, key: str, amount: int = 1) -> None:
    clean_key = key or "unknown"
    mapping[clean_key] = int(mapping.get(clean_key, 0)) + amount


def _safe_json_dict(raw: object) -> dict:
    """Parse a JSON column that should hold an object; {} on corrupt/empty data."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ── Persistence ──────────────────────────────────────────────
class ContributionStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or PR_ENGINE_DB_FILE
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode = WAL")
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
                    external_run_id TEXT UNIQUE,
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
                    owner_login TEXT NOT NULL DEFAULT '',
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
                CREATE TABLE IF NOT EXISTS repo_inspections (
                    repo_full_name TEXT PRIMARY KEY,
                    repo_url TEXT NOT NULL,
                    inspected_at TEXT NOT NULL,
                    source_pushed_at TEXT NOT NULL DEFAULT '',
                    default_branch TEXT NOT NULL DEFAULT 'main',
                    stars_text TEXT NOT NULL DEFAULT '0',
                    forks_text TEXT NOT NULL DEFAULT '0',
                    license TEXT NOT NULL DEFAULT '',
                    pushed_days_ago INTEGER NOT NULL DEFAULT 0,
                    file_count INTEGER NOT NULL DEFAULT 0,
                    py_count INTEGER NOT NULL DEFAULT 0,
                    ts_count INTEGER NOT NULL DEFAULT 0,
                    test_count INTEGER NOT NULL DEFAULT 0,
                    lane_name TEXT NOT NULL DEFAULT '',
                    lane_match INTEGER NOT NULL DEFAULT 0,
                    first_pr_friendly INTEGER NOT NULL DEFAULT 0,
                    first_pr_reason TEXT NOT NULL DEFAULT '',
                    search_scope TEXT NOT NULL DEFAULT '',
                    targeted_scope TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    topics_text TEXT NOT NULL DEFAULT '',
                    scope_notes_text TEXT NOT NULL DEFAULT '',
                    next_steps_text TEXT NOT NULL DEFAULT '',
                    artifact_path TEXT NOT NULL DEFAULT '',
                    archived INTEGER NOT NULL DEFAULT 0,
                    disabled INTEGER NOT NULL DEFAULT 0
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
        if "owner_login" not in pr_columns:
            conn.execute("ALTER TABLE pull_requests ADD COLUMN owner_login TEXT NOT NULL DEFAULT ''")
        if "lifecycle_state" not in pr_columns:
            conn.execute("ALTER TABLE pull_requests ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'open'")
        run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "external_run_id" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN external_run_id TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_external_run_id ON runs(external_run_id)")

    def start_run(self, mode: str, target_count: int, external_run_id: str = "") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs(external_run_id, mode, target_count, started_at) VALUES (?, ?, ?, ?)",
                (external_run_id or None, mode, target_count, iso_now()),
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

    def get_run(self, run_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, external_run_id, mode, target_count, started_at, finished_at, summary_json FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        summary_raw = data.pop("summary_json", "") or ""
        try:
            data["summary"] = json.loads(summary_raw) if summary_raw else None
        except Exception:
            data["summary"] = None
        return data

    def get_run_by_external_id(self, external_run_id: str) -> dict | None:
        if not external_run_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, external_run_id, mode, target_count, started_at, finished_at, summary_json FROM runs WHERE external_run_id = ?",
                (external_run_id,),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        summary_raw = data.pop("summary_json", "") or ""
        try:
            data["summary"] = json.loads(summary_raw) if summary_raw else None
        except Exception:
            data["summary"] = None
        return data

    def get_repo_events_for_run(self, run_id: int, after_id: int = 0) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, repo_full_name, event_type, summary, details_json, created_at
                FROM repo_events
                WHERE run_id = ? AND id > ?
                ORDER BY id ASC
                """,
                (run_id, after_id),
            ).fetchall()
        events: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json", "") or "{}")
            except Exception:
                item["details"] = {}
            events.append(item)
        return events

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
            hotspots = _safe_json_dict(row["file_hotspots_json"]) if row else {}
            pattern_history = _safe_json_dict(row["pattern_history_json"]) if row else {}
            responsiveness = _safe_json_dict(row["responsiveness_profile_json"]) if row else {}
            hotspots[opportunity.target_file] = hotspots.get(opportunity.target_file, 0) + 1
            pattern_stats = pattern_history.get(opportunity.pattern_type, _pattern_stats())
            pattern_stats.setdefault("qualified", 0)
            pattern_stats.setdefault("submitted", 0)
            pattern_stats["rejected"] = int(pattern_stats.get("rejected", 0)) + 1
            _bump_counter(pattern_stats.setdefault("target_dirs", {}), _target_dir(opportunity.target_file))
            _bump_counter(pattern_stats.setdefault("target_files", {}), opportunity.target_file)
            test_bucket = "yes" if opportunity.test_target else "no"
            _bump_counter(pattern_stats.setdefault("had_test_target", {}), test_bucket)
            if reason_code == "self_review_rejected":
                _bump_counter(pattern_stats.setdefault("self_review_reasons", {}), human_summary[:160])
            reason_counts = pattern_stats.setdefault("reason_counts", {})
            reason_counts[reason_code] = int(reason_counts.get(reason_code, 0)) + 1
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

    def reject_opportunity_by_id(
        self,
        opportunity_id: int,
        reason_code: str,
        human_summary: str,
        state: str,
        run_id: int | None = None,
    ) -> None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT run_id, repo_full_name, target_file, pattern_type
                FROM opportunities
                WHERE id = ?
                """,
                (opportunity_id,),
            ).fetchone()
            if not row:
                return
            rejection_run_id = run_id if run_id is not None else row["run_id"]
            conn.execute(
                "UPDATE opportunities SET state = 'REJECT', why_rejected = ?, updated_at = ? WHERE id = ?",
                (human_summary, iso_now(), opportunity_id),
            )
            conn.execute(
                """
                INSERT INTO rejections(
                    run_id, opportunity_id, repo_full_name, state, reason_code, human_summary,
                    target_file, pattern_type, cooldown_until, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rejection_run_id,
                    opportunity_id,
                    row["repo_full_name"],
                    state,
                    reason_code,
                    human_summary,
                    row["target_file"],
                    row["pattern_type"],
                    None,
                    iso_now(),
                ),
            )
            conn.execute(
                """
                INSERT INTO repos(
                    full_name, repo_profile_json, responsiveness_profile_json, pattern_history_json,
                    file_hotspots_json, rejection_count, last_seen_at
                ) VALUES (?, '{}', ?, '{}', '{}', 1, ?)
                ON CONFLICT(full_name) DO UPDATE SET
                    responsiveness_profile_json = ?,
                    rejection_count = repos.rejection_count + 1,
                    last_seen_at = ?
                """,
                (
                    row["repo_full_name"],
                    json.dumps({"last_rejection_reason": reason_code}, ensure_ascii=False),
                    iso_now(),
                    json.dumps({"last_rejection_reason": reason_code}, ensure_ascii=False),
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
        responsiveness = _safe_json_dict(row["responsiveness_profile_json"])
        if responsiveness.get("last_signal") == "merged":
            score += 8
        if responsiveness.get("last_signal") == "closed":
            score -= 6
        return score

    def same_pattern_recent_rejections(
        self,
        repo_full_name: str,
        pattern_type: str,
        *,
        reason_code: str = "",
        days: int = 14,
    ) -> int:
        query = """
            SELECT COUNT(*) AS n FROM rejections
            WHERE repo_full_name = ? AND pattern_type = ? AND created_at >= ?
        """
        params: list[object] = [
            repo_full_name,
            pattern_type,
            (utcnow() - timedelta(days=days)).isoformat(),
        ]
        if reason_code:
            query += " AND reason_code = ?"
            params.append(reason_code)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["n"]) if row else 0

    def repo_live_fit(self, repo_full_name: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT repo_profile_json, responsiveness_profile_json, pattern_history_json,
                       merged_count, closed_count, feedback_count, rejection_count
                FROM repos WHERE full_name = ?
                """,
                (repo_full_name,),
            ).fetchone()
        if not row:
            return {"state": "dry-run-only", "score": 50, "reasons": ["no repo memory yet"]}

        profile = _safe_json_dict(row["repo_profile_json"])
        responsiveness = _safe_json_dict(row["responsiveness_profile_json"])
        history = _safe_json_dict(row["pattern_history_json"])
        score = 50
        reasons: list[str] = []

        test_count = int(profile.get("test_count", 0) or 0)
        file_count = int(profile.get("file_count", 0) or 0)
        if test_count:
            score += min(test_count, 12)
            reasons.append("nearby tests likely available")
        else:
            score -= 18
            reasons.append("no tests in repo profile")
        if file_count <= 80:
            score += 10
            reasons.append("small local surface")
        elif file_count > 160:
            score -= 15
            reasons.append("large repo surface")

        submitted = sum(int(v.get("submitted", 0) or 0) for v in history.values() if isinstance(v, dict))
        rejected = sum(int(v.get("rejected", 0) or 0) for v in history.values() if isinstance(v, dict))
        if submitted:
            score += min(submitted * 10, 20)
            reasons.append("prior accepted patch shape")
        if rejected:
            score -= min(rejected * 8, 24)
            reasons.append("prior rejected patch shape")
        if row["merged_count"]:
            score += min(int(row["merged_count"]) * 12, 24)
        if row["closed_count"] or row["rejection_count"]:
            score -= min((int(row["closed_count"]) + int(row["rejection_count"])) * 5, 30)
        if responsiveness.get("last_rejection_reason") == "self_review_rejected":
            score -= 15
            reasons.append("recent self-review rejection")
        scan_signals = responsiveness.get("scan_signals") or {}
        for kind, signal in scan_signals.items():
            if not isinstance(signal, dict):
                continue
            severity_counts = signal.get("severity_counts") or {}
            high_count = int(severity_counts.get("high", 0) or 0)
            supported_count = int(signal.get("supported_file_count", 0) or 0)
            if kind in {"security", "trust", "audit"} and high_count:
                score -= min(high_count * 10, 30)
                reasons.append(f"{kind} scan high-risk findings")
            if kind == "bug" and high_count and supported_count:
                score += min(high_count * 4, 12)
                reasons.append("bug scan found narrow high-signal candidates")
            if kind in {"security", "trust", "audit"} and supported_count == 0:
                score -= 12
                reasons.append(f"{kind} scan has low source coverage")

        if score >= 70:
            state = "live-targeted-ready"
        elif score >= 40:
            state = "dry-run-only"
        else:
            state = "inspect-only"
        return {"state": state, "score": score, "reasons": reasons}

    def is_on_cooldown(self, repo_full_name: str) -> tuple[bool, str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cooldown_until FROM repos WHERE full_name = ?",
                (repo_full_name,),
            ).fetchone()
        if not row or not row["cooldown_until"]:
            return False, ""
        try:
            if datetime.fromisoformat(row["cooldown_until"]) > utcnow():
                return True, row["cooldown_until"]
        except Exception:
            pass
        return False, ""

    def pattern_stats(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    pattern_type,
                    COUNT(*) AS total,
                    SUM(CASE WHEN state = 'SUBMIT' THEN 1 ELSE 0 END) AS submitted,
                    SUM(CASE WHEN state = 'REJECT' THEN 1 ELSE 0 END) AS rejected,
                    SUM(CASE WHEN state = 'READY' THEN 1 ELSE 0 END) AS queued
                FROM opportunities
                GROUP BY pattern_type
                ORDER BY submitted DESC, total DESC
                """,
            ).fetchall()
        return [dict(row) for row in rows]

    def has_open_pr(self, repo_full_name: str, owner_login: str | None = None) -> bool:
        return self.find_open_pr(repo_full_name, owner_login=owner_login) is not None

    def find_open_pr(self, repo_full_name: str, owner_login: str | None = None) -> dict | None:
        normalized_owner = (owner_login or _active_owner_login()).strip().lower()
        with self._connect() as conn:
            if normalized_owner:
                row = conn.execute(
                    """
                    SELECT repo_full_name, owner_login, pr_url, pr_title, status, improvement_type,
                           submitted_at, branch_name, fork_name
                    FROM pull_requests
                    WHERE repo_full_name = ? AND owner_login = ? AND status = 'open'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (repo_full_name, normalized_owner),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT repo_full_name, owner_login, pr_url, pr_title, status, improvement_type,
                           submitted_at, branch_name, fork_name
                    FROM pull_requests
                    WHERE repo_full_name = ? AND status = 'open'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (repo_full_name,),
                ).fetchone()
        if row is not None:
            result = dict(row)
            result["source"] = "sqlite"
            return result

        normalized = repo_full_name.strip().lower()
        for path in _legacy_pr_log_candidates():
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            entries = data.get("submitted", [])
            if not isinstance(entries, list):
                continue
            matches = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                full_name = str(entry.get("full_name") or entry.get("repo_full_name") or "").strip().lower()
                pr_url = str(entry.get("pr_url") or "").strip()
                status = str(entry.get("status") or "open").strip().lower()
                entry_owner = str(entry.get("owner_login") or "").strip().lower()
                if normalized_owner and entry_owner and entry_owner != normalized_owner:
                    continue
                if full_name != normalized or status != "open" or "/pull/" not in pr_url:
                    continue
                matches.append(entry)
            if matches:
                latest = max(matches, key=lambda item: str(item.get("submitted_at") or ""))
                return {
                    "repo_full_name": latest.get("full_name") or latest.get("repo_full_name") or repo_full_name,
                    "owner_login": latest.get("owner_login", ""),
                    "pr_url": latest.get("pr_url", ""),
                    "pr_title": latest.get("pr_title", ""),
                    "status": latest.get("status", "open"),
                    "improvement_type": latest.get("improvement_type", ""),
                    "submitted_at": latest.get("submitted_at", ""),
                    "branch_name": latest.get("branch_name", ""),
                    "fork_name": latest.get("fork_name", ""),
                    "source": f"legacy:{path}",
                }
        return None

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
        owner_login: str = "",
    ) -> None:
        normalized_owner = (owner_login or _active_owner_login()).strip().lower()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pull_requests(
                    opportunity_id, repo_full_name, owner_login, pr_url, pr_title, status,
                    lifecycle_state, fork_name, branch_name, improvement_type, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    opportunity_id,
                    repo_full_name,
                    normalized_owner,
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
                opportunity_row = conn.execute(
                    "SELECT pattern_type FROM opportunities WHERE id = ?",
                    (opportunity_id,),
                ).fetchone()
                if opportunity_row:
                    repo_row = conn.execute(
                        "SELECT pattern_history_json FROM repos WHERE full_name = ?",
                        (repo_full_name,),
                    ).fetchone()
                    pattern_history = _safe_json_dict(repo_row["pattern_history_json"]) if repo_row else {}
                    opportunity_detail = conn.execute(
                        "SELECT target_file, test_target FROM opportunities WHERE id = ?",
                        (opportunity_id,),
                    ).fetchone()
                    pattern_type = opportunity_row["pattern_type"] or "unknown"
                    stats = pattern_history.get(pattern_type, _pattern_stats())
                    stats["submitted"] = int(stats.get("submitted", 0)) + 1
                    if opportunity_detail:
                        target_file = opportunity_detail["target_file"] or ""
                        _bump_counter(stats.setdefault("target_dirs", {}), _target_dir(target_file))
                        _bump_counter(stats.setdefault("target_files", {}), target_file)
                        _bump_counter(
                            stats.setdefault("had_test_target", {}),
                            "yes" if opportunity_detail["test_target"] else "no",
                        )
                    pattern_history[pattern_type] = stats
                    conn.execute(
                        "UPDATE repos SET pattern_history_json = ? WHERE full_name = ?",
                        (json.dumps(pattern_history, ensure_ascii=False), repo_full_name),
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
            if status == "closed":
                cooldown_until = (utcnow() + timedelta(days=REPO_COOLDOWN_DAYS)).isoformat()
                conn.execute(
                    "UPDATE repos SET cooldown_until = ? WHERE full_name = ?",
                    (cooldown_until, repo_full_name),
                )
            responsiveness = {"last_signal": status}
            if maintainer_signal:
                responsiveness["maintainer_signal"] = maintainer_signal
            existing = conn.execute(
                "SELECT responsiveness_profile_json FROM repos WHERE full_name = ?",
                (repo_full_name,),
            ).fetchone()
            existing_json = _safe_json_dict(existing["responsiveness_profile_json"]) if existing else {}
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
                    "SELECT pattern_type, target_file, test_target FROM opportunities WHERE id = ?",
                    (opportunity_id,),
                ).fetchone()
                repo_row = conn.execute(
                    "SELECT pattern_history_json FROM repos WHERE full_name = ?",
                    (repo_full_name,),
                ).fetchone()
                pattern_history = _safe_json_dict(repo_row["pattern_history_json"]) if repo_row else {}
                pattern_type = pattern_row["pattern_type"] if pattern_row else "unknown"
                stats = pattern_history.get(pattern_type, _pattern_stats())
                if status == "merged":
                    stats["submitted"] = int(stats.get("submitted", 0)) + 1
                elif status == "closed":
                    stats["closed_without_merge"] = int(stats.get("closed_without_merge", 0)) + 1
                    if maintainer_signal:
                        _bump_counter(
                            stats.setdefault("maintainer_feedback_shapes", {}),
                            maintainer_signal[:160],
                        )
                if pattern_row:
                    target_file = pattern_row["target_file"] or ""
                    _bump_counter(stats.setdefault("target_dirs", {}), _target_dir(target_file))
                    _bump_counter(stats.setdefault("target_files", {}), target_file)
                    _bump_counter(
                        stats.setdefault("had_test_target", {}),
                        "yes" if pattern_row["test_target"] else "no",
                    )
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
            responsiveness = _safe_json_dict(row["responsiveness_profile_json"]) if row else {}
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

    def record_scan_summary(
        self,
        repo_full_name: str,
        *,
        kind: str,
        severity_counts: dict,
        finding_kind_counts: dict,
        supported_file_count: int,
    ) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT responsiveness_profile_json FROM repos WHERE full_name = ?",
                (repo_full_name,),
            ).fetchone()
            responsiveness = _safe_json_dict(row["responsiveness_profile_json"]) if row else {}
            scan_signals = responsiveness.setdefault("scan_signals", {})
            scan_signals[kind] = {
                "severity_counts": dict(severity_counts),
                "finding_kind_counts": dict(finding_kind_counts),
                "supported_file_count": int(supported_file_count),
                "updated_at": iso_now(),
            }
            conn.execute(
                """
                INSERT INTO repos(
                    full_name, repo_profile_json, responsiveness_profile_json,
                    pattern_history_json, file_hotspots_json, last_seen_at
                ) VALUES (?, '{}', ?, '{}', '{}', ?)
                ON CONFLICT(full_name) DO UPDATE SET
                    responsiveness_profile_json = ?,
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
        broad_rejected_early = next((count for reason, count in top_rejections if reason == "target_area_too_broad"), 0)
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
            "broad_rejected_early": broad_rejected_early,
            "shortlisted": 0,
            "planned": 0,
            "generated": 0,
            "self_review_rejected": 0,
            "shortlist_summary": [],
            "min_patchability_score": 0,
            "best_patchability_score": 0,
            "token_spend_by_stage": {"plan": 0, "generate": 0, "review": 0},
            "last_patch_plan": None,
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


    def list_pull_requests(self, limit: int = 50, status_filter: str | None = None) -> list[dict]:
        normalized_owner = _active_owner_login()
        with self._connect() as conn:
            if status_filter and normalized_owner:
                rows = conn.execute(
                    """
                    SELECT repo_full_name, owner_login, pr_url, pr_title, status, improvement_type,
                           submitted_at, resolved_at, maintainer_signal
                    FROM pull_requests
                    WHERE status = ? AND owner_login = ?
                    ORDER BY submitted_at DESC
                    LIMIT ?
                    """,
                    (status_filter, normalized_owner, limit),
                ).fetchall()
            elif status_filter:
                rows = conn.execute(
                    """
                    SELECT repo_full_name, owner_login, pr_url, pr_title, status, improvement_type,
                           submitted_at, resolved_at, maintainer_signal
                    FROM pull_requests
                    WHERE status = ?
                    ORDER BY submitted_at DESC
                    LIMIT ?
                    """,
                    (status_filter, limit),
                ).fetchall()
            elif normalized_owner:
                rows = conn.execute(
                    """
                    SELECT repo_full_name, owner_login, pr_url, pr_title, status, improvement_type,
                           submitted_at, resolved_at, maintainer_signal
                    FROM pull_requests
                    WHERE owner_login = ?
                    ORDER BY submitted_at DESC
                    LIMIT ?
                    """,
                    (normalized_owner, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT repo_full_name, owner_login, pr_url, pr_title, status, improvement_type,
                           submitted_at, resolved_at, maintainer_signal
                    FROM pull_requests
                    ORDER BY submitted_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def submitted_repos(self, owner_login: str | None = None) -> set[str]:
        normalized_owner = (owner_login or _active_owner_login()).strip().lower()
        with self._connect() as conn:
            if normalized_owner:
                rows = conn.execute(
                    "SELECT DISTINCT repo_full_name FROM pull_requests WHERE owner_login = ?",
                    (normalized_owner,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT DISTINCT repo_full_name FROM pull_requests").fetchall()
        return {str(row["repo_full_name"]).lower() for row in rows}

    def save_repo_inspect_snapshot(
        self,
        candidate: RepoCandidate,
        inspect_data: dict[str, object],
        *,
        source_pushed_at: str,
        artifact_path: str,
    ) -> None:
        topics_text = "\n".join(str(item) for item in inspect_data.get("topics", []) if str(item).strip())
        scope_notes_text = "\n".join(
            str(item) for item in inspect_data.get("scope_notes", []) if str(item).strip()
        )
        next_steps_text = "\n".join(
            str(item) for item in inspect_data.get("next_steps", []) if str(item).strip()
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO repo_inspections(
                    repo_full_name, repo_url, inspected_at, source_pushed_at, default_branch,
                    stars_text, forks_text, license, pushed_days_ago,
                    file_count, py_count, ts_count, test_count,
                    lane_name, lane_match, first_pr_friendly, first_pr_reason,
                    search_scope, targeted_scope, description,
                    topics_text, scope_notes_text, next_steps_text,
                    artifact_path, archived, disabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_full_name) DO UPDATE SET
                    repo_url=excluded.repo_url,
                    inspected_at=excluded.inspected_at,
                    source_pushed_at=excluded.source_pushed_at,
                    default_branch=excluded.default_branch,
                    stars_text=excluded.stars_text,
                    forks_text=excluded.forks_text,
                    license=excluded.license,
                    pushed_days_ago=excluded.pushed_days_ago,
                    file_count=excluded.file_count,
                    py_count=excluded.py_count,
                    ts_count=excluded.ts_count,
                    test_count=excluded.test_count,
                    lane_name=excluded.lane_name,
                    lane_match=excluded.lane_match,
                    first_pr_friendly=excluded.first_pr_friendly,
                    first_pr_reason=excluded.first_pr_reason,
                    search_scope=excluded.search_scope,
                    targeted_scope=excluded.targeted_scope,
                    description=excluded.description,
                    topics_text=excluded.topics_text,
                    scope_notes_text=excluded.scope_notes_text,
                    next_steps_text=excluded.next_steps_text,
                    artifact_path=excluded.artifact_path,
                    archived=excluded.archived,
                    disabled=excluded.disabled
                """,
                (
                    candidate.full_name,
                    candidate.url,
                    iso_now(),
                    source_pushed_at,
                    candidate.default_branch,
                    str(inspect_data.get("stars", "0")),
                    str(inspect_data.get("forks", "0")),
                    candidate.license,
                    int(candidate.pushed_days_ago),
                    int(inspect_data.get("files", 0)),
                    int(inspect_data.get("py", 0)),
                    int(inspect_data.get("ts", 0)),
                    int(inspect_data.get("tests", 0)),
                    str(inspect_data.get("lane_name", "")),
                    1 if inspect_data.get("lane_match") else 0,
                    1 if inspect_data.get("first_pr_friendly") else 0,
                    str(inspect_data.get("first_pr_reason", "")),
                    str(inspect_data.get("search_scope", "")),
                    str(inspect_data.get("targeted_scope", "")),
                    str(inspect_data.get("description", "")),
                    topics_text,
                    scope_notes_text,
                    next_steps_text,
                    artifact_path,
                    1 if candidate.archived else 0,
                    1 if candidate.disabled else 0,
                ),
            )

    def get_repo_inspect_snapshot(self, repo_full_name: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM repo_inspections
                WHERE repo_full_name = ?
                """,
                (repo_full_name,),
            ).fetchone()
        if not row:
            return None

        def _split_lines(value: str) -> list[str]:
            return [line for line in (value or "").splitlines() if line.strip()]

        item = dict(row)
        return {
            "repo": item["repo_full_name"],
            "url": item["repo_url"],
            "stars": item["stars_text"],
            "forks": item["forks_text"],
            "license": item["license"],
            "pushed_days_ago": item["pushed_days_ago"],
            "files": item["file_count"],
            "py": item["py_count"],
            "ts": item["ts_count"],
            "tests": item["test_count"],
            "lane_name": item["lane_name"],
            "lane_match": bool(item["lane_match"]),
            "first_pr_friendly": bool(item["first_pr_friendly"]),
            "first_pr_reason": item["first_pr_reason"],
            "first_pr_label": "good fit" if item["first_pr_friendly"] else "needs caution",
            "search_scope": item["search_scope"],
            "targeted_scope": item["targeted_scope"],
            "description": item["description"],
            "topics": _split_lines(item["topics_text"]),
            "scope_notes": _split_lines(item["scope_notes_text"]),
            "next_steps": _split_lines(item["next_steps_text"]),
            "cached": True,
            "inspected_at": item["inspected_at"],
            "source_pushed_at": item["source_pushed_at"],
            "artifact_path": item["artifact_path"],
            "default_branch": item["default_branch"],
            "archived": bool(item["archived"]),
            "disabled": bool(item["disabled"]),
        }


PREngineStore = ContributionStore
