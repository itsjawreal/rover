"""MCP server — exposes the contribution engine as callable tools."""
from __future__ import annotations

import logging
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# Bootstrap sys.path so src.* imports work when launched standalone
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.core.command_router import parse_command_text  # noqa: E402
from src.core.config import (
    LOG_DIR,
    PR_MONITOR_INTERVAL_SECONDS,
    ROOT,
    ROVER_NOTIFY_INTERVAL_SECONDS,
    ROVER_NOTIFY_ON_EVENT_TYPES,
    ROVER_NOTIFY_ONLY_ON_CHANGE,
    ROVER_NOTIFY_PROGRESS,
    ROVER_NOTIFY_STALL_SECONDS,
    TELEGRAM_BOT_ENABLED,
    TELEGRAM_CHAT,
    TELEGRAM_TOKEN,
)  # noqa: E402
from src.core.notify import (  # noqa: E402
    NotificationRoute,
    default_notification_route,
    notify,
    telegram_edit_message,
    telegram_send_message,
)
from src.contrib.contribution_store import ContributionStore  # noqa: E402
from src.github.fork import get_current_github_login  # noqa: E402

_store = ContributionStore()
_SECRET_KEYS = {"GH_TOKEN", "GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
_RUNS: dict[str, "ManagedRun"] = {}
_RUNS_LOCK = threading.Lock()
_active_proc: subprocess.Popen[str] | None = None

# ── PR monitor (delegated to src.core.pr_monitor) ────────────
from src.core import pr_monitor as _pr_monitor  # noqa: E402

# ── Telegram bot state ───────────────────────────────────────
from src.core.telegram_bot import TelegramCommandBot  # noqa: E402
_tg_bot: TelegramCommandBot | None = None
_tg_bot_lock = threading.Lock()


@dataclass
class ManagedRun:
    run_id: str
    mode: str
    repo: str
    goal: str
    count: int
    dry_run: bool
    first_pr: bool
    override_limits: bool
    command: list[str]
    started_at: str
    process: subprocess.Popen[str] | None = None
    pid: int | None = None
    state: str = "queued"
    returncode: int | None = None
    engine_run_id: int | None = None
    engine_started_at: str = ""
    finished_at: str = ""
    logs: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    next_seq: int = 1
    last_repo_event_id: int = 0
    summary: dict[str, Any] | None = None
    error: str = ""
    notification_route: NotificationRoute | None = None
    last_notified_seq: int = 0
    last_progress_at: float = 0.0
    last_progress_key: str = ""
    last_stage_key: str = ""
    last_activity_at: float = field(default_factory=time.time)
    progress_message_id: int | None = None
    terminal_notified: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _register_run(run: ManagedRun) -> None:
    with _RUNS_LOCK:
        _RUNS[run.run_id] = run


def _latest_known_run() -> ManagedRun | None:
    with _RUNS_LOCK:
        if not _RUNS:
            return None
        return max(_RUNS.values(), key=lambda item: item.started_at)


def _latest_active_run() -> ManagedRun | None:
    with _RUNS_LOCK:
        active = [run for run in _RUNS.values() if run.state in {"queued", "started", "running"}]
    if not active:
        return None
    return max(active, key=lambda item: item.started_at)


def _append_event(run: ManagedRun, event_type: str, summary: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    with run.lock:
        event = {
            "seq": run.next_seq,
            "type": event_type,
            "summary": summary,
            "details": details or {},
            "created_at": _utc_now(),
        }
        run.next_seq += 1
        run.events.append(event)
        run.last_activity_at = time.time()
        return event


def _sync_engine_run(run: ManagedRun) -> None:
    if run.engine_run_id is not None:
        return
    record = _store.get_run_by_external_id(run.run_id)
    if not record:
        return
    run.engine_run_id = int(record["id"])
    run.engine_started_at = str(record.get("started_at") or "")


def _map_repo_event(event: dict[str, Any]) -> tuple[str, str]:
    mapping = {
        "discover_selected": "repo_selected",
        "scan_rejected": "opportunity_rejected",
        "qualify_rejected": "opportunity_rejected",
        "patch_generated": "patch_generated",
        "pr_submitted": "pr_submitted",
        "human_approval_queue": "opportunity_rejected",
        "human_approval_reject": "opportunity_rejected",
        "repo_recon": "repo_selected",
        "opportunity_selected": "repo_selected",
    }
    event_type = str(event.get("event_type") or "")
    return mapping.get(event_type, event_type or "repo_event"), str(event.get("summary") or event_type)


def _sync_repo_events(run: ManagedRun) -> None:
    _sync_engine_run(run)
    if run.engine_run_id is None:
        return
    for event in _store.get_repo_events_for_run(run.engine_run_id, after_id=run.last_repo_event_id):
        run.last_repo_event_id = int(event["id"])
        canonical_type, summary = _map_repo_event(event)
        details = dict(event.get("details") or {})
        details.setdefault("repo_full_name", event.get("repo_full_name", ""))
        details.setdefault("source_event_type", event.get("event_type", ""))
        _append_event(run, canonical_type, summary, details)


def _update_run_summary(run: ManagedRun) -> None:
    record = _store.get_run_by_external_id(run.run_id)
    if not record:
        return
    run.engine_run_id = int(record["id"])
    run.engine_started_at = str(record.get("started_at") or "")
    if record.get("summary"):
        run.summary = dict(record["summary"])
    if record.get("finished_at"):
        run.finished_at = str(record["finished_at"])


def _canonical_run_result(run: ManagedRun) -> dict[str, Any]:
    summary = dict(run.summary or {})
    try:
        owner_login = get_current_github_login().strip().lower()
    except Exception:
        owner_login = ""
    submitted_new_prs = list(summary.get("submitted_prs") or [])
    existing_open_prs: list[dict[str, Any]] = []
    if run.repo:
        existing = _store.find_open_pr(run.repo)
        if existing and existing.get("pr_url"):
            existing_open_prs.append(existing)
    outcome_code = "completed_no_submission"
    if run.state == "canceled":
        outcome_code = "canceled"
    elif run.returncode not in (None, 0) or run.state == "failed":
        outcome_code = "submission_failed"
    elif submitted_new_prs:
        outcome_code = "submitted_new_pr"
    elif existing_open_prs:
        outcome_code = "existing_pr_already_open"
    elif run.dry_run:
        outcome_code = "dry_run_complete"
    elif (
        int(summary.get("shortlisted", 0) or 0) > 0
        and int(summary.get("planned", 0) or 0) == 0
        and int(summary.get("generated", 0) or 0) == 0
        and int(summary.get("best_patchability_score", 0) or 0) > 0
        and int(summary.get("best_patchability_score", 0) or 0) < int(summary.get("min_patchability_score", 0) or 0)
    ):
        outcome_code = "shortlist_below_patchability_threshold"
    elif (
        int(summary.get("planned", 0) or 0) == 0
        and int(summary.get("generated", 0) or 0) == 0
        and int(summary.get("broad_rejected_early", 0) or 0) > 0
    ):
        outcome_code = "no_narrow_candidate"
    elif summary.get("queued"):
        outcome_code = "completed_no_submission"

    summary["submitted_new_prs"] = submitted_new_prs
    summary["existing_open_prs"] = existing_open_prs
    summary["outcome_code"] = outcome_code
    return {
        "run_id": run.run_id,
        "state": run.state,
        "returncode": run.returncode,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "engine_run_id": run.engine_run_id,
        "repo": run.repo,
        "goal": run.goal,
        "count": run.count,
        "dry_run": run.dry_run,
        "first_pr": run.first_pr,
        "override_limits": run.override_limits,
        "owner_login": owner_login,
        "outcome_code": outcome_code,
        "submitted_new_prs": submitted_new_prs,
        "existing_open_prs": existing_open_prs,
        "summary": summary,
    }


def _build_notification_route(
    *,
    notify_transport: str = "",
    notify_channel: str = "",
    notify_target: str = "",
    notify_account: str = "",
    notify_thread_id: str = "",
) -> NotificationRoute | None:
    if notify_transport or notify_channel or notify_target or notify_account or notify_thread_id:
        transport = (notify_transport or "openclaw").strip().lower()
        return NotificationRoute(
            transport=transport,
            channel=(notify_channel or "telegram").strip().lower(),
            target=notify_target.strip(),
            account=notify_account.strip(),
            thread_id=notify_thread_id.strip(),
        )
    return default_notification_route()


def _targeted_preflight(repo: str, override_limits: bool) -> dict[str, Any] | None:
    if not repo:
        return None
    from src.contrib.pr_generator import fetch_repo_candidate_with_scope, get_repo_inspect_data

    log = logging.getLogger("mcp.targeted_preflight")
    candidate = fetch_repo_candidate_with_scope(repo, log, enforce_scope=False, override_limits=override_limits)
    inspect_data = get_repo_inspect_data(candidate)
    targeted_scope = str(inspect_data.get("targeted_scope") or "")
    if targeted_scope == "targeted-ready":
        return None
    return {
        "accepted": False,
        "status": "blocked",
        "state": "blocked",
        "repo": str(inspect_data.get("repo") or repo),
        "goal": "",
        "count": 0,
        "dry_run": False,
        "first_pr": False,
        "override_limits": override_limits,
        "outcome_code": "blocked_ineligible_repo",
        "reason": targeted_scope or "inspect-only",
        "scope_notes": list(inspect_data.get("scope_notes") or []),
        "next_steps": list(inspect_data.get("next_steps") or []),
        "inspect": inspect_data,
    }


def _supports_telegram_progress_card(run: ManagedRun) -> bool:
    route = run.notification_route
    if route is None:
        return False
    if route.transport == "telegram":
        return True
    return route.transport == "openclaw" and (route.channel or "telegram") == "telegram"


def _notification_snapshot(run: ManagedRun) -> tuple[str, str]:
    last_event = run.events[-1] if run.events else None
    last_summary = str(last_event.get("summary") or "") if last_event else ""
    snapshot_key = "|".join(
        [
            run.state,
            str(run.returncode),
            str(len(run.events)),
            str(last_event.get("seq") if last_event else 0),
            last_summary,
        ]
    )
    return snapshot_key, last_summary


def _short_run_id(run_id: str) -> str:
    return run_id[:12] if len(run_id) > 12 else run_id


def _human_state_label(run: ManagedRun) -> str:
    mapping = {
        "queued": "Queued",
        "started": "Starting",
        "running": "Running",
        "completed": "Completed",
        "failed": "Failed",
        "canceled": "Canceled",
    }
    return mapping.get(run.state, run.state.title())


def _compact_summary(text: str, limit: int = 88) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _format_update_timestamp(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "-"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        return parsed.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return raw


def _progress_percent(run: ManagedRun) -> int:
    if run.state in {"failed", "canceled"}:
        return 100
    payload = _canonical_run_result(run)
    if payload["submitted_new_prs"] or payload["existing_open_prs"] or run.state == "completed":
        return 100
    summary = dict(payload.get("summary") or {})
    stage = str(summary.get("current_stage") or run.last_stage_key or "").lower()
    if stage == "submit":
        return 92
    if stage == "review":
        return 76
    if stage == "generate":
        return 62
    if stage == "plan":
        return 42
    if stage == "qualify":
        return 22
    if "opening pull request" in stage:
        return 92
    if "preparing fork and branch" in stage:
        return 82
    if "generating patch" in stage:
        return 68
    if "reviewing repository history" in stage:
        return 58
    if "reading target files" in stage:
        return 48
    if "scanning repository files" in stage:
        return 36
    if "discovering opportunities" in stage:
        return 22
    if any(event["type"] == "repo_selected" for event in run.events):
        return 28
    if run.state == "started":
        return 10
    return 5


def _render_progress_bar(percent: int) -> str:
    blocks = 12
    filled = max(0, min(blocks, round((percent / 100) * blocks)))
    return "[" + ("#" * filled) + ("-" * (blocks - filled)) + f"] {percent:>3d}%"


def _current_repo_label(run: "ManagedRun") -> str:
    """Return the most recently active owner/repo, or '(search mode)' if none seen yet."""
    if run.repo:
        return run.repo
    for event in reversed(run.events):
        repo = (event.get("details") or {}).get("repo_full_name")
        if repo:
            return repo
    return "(search mode)"


def _render_progress_card(run: ManagedRun) -> str:
    payload = _canonical_run_result(run)
    summary = dict(payload.get("summary") or {})
    last_event = run.events[-1] if run.events else None
    percent = _progress_percent(run)
    state_label = _human_state_label(run)
    current_stage = str(summary.get("current_stage") or "").lower()
    if current_stage:
        stage_label = current_stage.replace("_", " ").title()
        last_type = "Run Stage"
        last_summary = f"Run stage: {stage_label}"
    else:
        last_type = str(last_event["type"]).replace("_", " ").title() if last_event else "-"
        last_summary = _compact_summary(str(last_event.get("summary") or ""), limit=72) if last_event else "-"
        stage_label = str(run.last_stage_key or "").replace("_", " ").title() or "-"
    phase_label = "Patching" if current_stage in {"generate", "review", "submit"} else "Narrowing"
    target_file = str(summary.get("current_target_file") or "")
    pattern_type = str(summary.get("current_pattern_type") or "").replace("_", " ").title()
    candidate_label = target_file or pattern_type or "-"
    updated_at = _format_update_timestamp(str(last_event.get("created_at") or "") if last_event else run.started_at)
    lines = [
        "ROVER PROGRESS",
        _render_progress_bar(percent),
        "",
        f"🕒 Last update at : {updated_at}",
        "",
        f"🆔 Run    : {_short_run_id(run.run_id)}",
        f"📍 Repo   : {_current_repo_label(run)}",
        f"⚙️ State  : {state_label}",
        f"🎯 Goal   : {run.goal}",
        f"🚦 Mode   : {'dry-run' if run.dry_run else 'live'}",
        f"🧪 Phase  : {phase_label}",
        f"📊 Events : {len(run.events)} total",
        f"🛠️ Stage  : {stage_label}",
        f"🎯 Top narrowed candidate : {candidate_label}",
        f"🧭 Last   : {last_type}",
        f"📝 Step   : {last_summary}",
    ]
    return "\n".join(lines)


def _render_terminal_summary(run: ManagedRun) -> str:
    payload = _canonical_run_result(run)
    summary = dict(payload.get("summary") or {})
    status_title = {
        "completed": "ROVER COMPLETE",
        "failed": "ROVER FAILED",
        "canceled": "ROVER CANCELED",
    }.get(run.state, f"ROVER {run.state.upper()}")
    outcome_title = {
        "submitted_new_pr": "ROVER PR SUBMITTED",
        "existing_pr_already_open": "ROVER SKIPPED (OPEN PR)",
        "dry_run_complete": "ROVER DRY RUN COMPLETE",
        "completed_no_submission": "ROVER NO SUBMISSION",
        "shortlist_below_patchability_threshold": "ROVER NO NARROW CANDIDATE",
        "no_narrow_candidate": "ROVER NO NARROW CANDIDATE",
        "submission_failed": "ROVER FAILED",
        "canceled": "ROVER CANCELED",
    }.get(payload["outcome_code"], status_title)
    updated_at = _format_update_timestamp(run.finished_at or (run.events[-1]["created_at"] if run.events else run.started_at))
    phase_label = "Patching" if str(summary.get("current_stage") or "").lower() in {"generate", "review", "submit"} else "Narrowing"
    candidate_label = str(summary.get("current_target_file") or summary.get("current_pattern_type") or "-")
    lines = [
        outcome_title,
        "",
        f"🕒 Last update at : {updated_at}",
        "",
        f"🆔 Run      : {_short_run_id(run.run_id)}",
        f"📍 Repo     : {_current_repo_label(run)}",
        f"🚦 Mode     : {'dry-run' if run.dry_run else 'live'}",
        f"🧪 Phase    : {phase_label}",
        f"🎯 Top narrowed candidate: {candidate_label}",
        f"📌 Outcome  : {payload['outcome_code']}",
    ]
    if payload["submitted_new_prs"]:
        pr = payload["submitted_new_prs"][0]
        lines.extend(
            [
                "",
                "Pull Request",
                f"- Title: {pr.get('pr_title', '')}",
                f"- URL  : {pr.get('pr_url', '')}",
            ]
        )
    elif payload["existing_open_prs"]:
        pr = payload["existing_open_prs"][0]
        lines.extend(
            [
                "",
                "Existing Pull Request",
                f"- Title: {pr.get('pr_title', '')}",
                f"- URL  : {pr.get('pr_url', '')}",
            ]
        )
    patch_event = next((event for event in reversed(run.events) if event["type"] == "patch_generated"), None)
    if patch_event:
        details = dict(patch_event.get("details") or {})
        lines.extend(["", "Patch"])
        if details.get("title"):
            lines.append(f"- Title: {details['title']}")
        files = details.get("files") or []
        if files:
            lines.append(f"- Files: {', '.join(str(item) for item in files[:5])}")
            if len(files) > 5:
                lines.append(f"- More : +{len(files) - 5} files")
    lines.extend(["", "Usage"])
    if summary.get("ai_calls") is not None:
        lines.append(f"- AI calls: {summary.get('ai_calls', 0)}")
    if summary.get("est_tokens") is not None:
        lines.append(f"- Tokens  : ~{summary.get('est_tokens', 0)}")
    if summary.get("attempts") is not None:
        lines.append(f"- Attempts: {summary.get('attempts', 0)}")
    if summary.get("shortlisted") is not None:
        lines.append(f"- Shortlisted: {summary.get('shortlisted', 0)}")
    if summary.get("planned") is not None:
        lines.append(f"- Planned: {summary.get('planned', 0)}")
    if summary.get("generated") is not None:
        lines.append(f"- Generated: {summary.get('generated', 0)}")
    shortlist_summary = list(summary.get("shortlist_summary") or [])
    if shortlist_summary:
        lines.extend(["", "Shortlist"])
        for item in shortlist_summary[:2]:
            target_file = str(item.get("target_file") or "-")
            pattern_type = str(item.get("pattern_type") or "-")
            score = item.get("score")
            lines.append(f"- {target_file} | {pattern_type} | score={score}")
    best_patchability = int(summary.get("best_patchability_score", 0) or 0)
    min_patchability = int(summary.get("min_patchability_score", 0) or 0)
    if best_patchability and min_patchability and best_patchability < min_patchability:
        lines.extend(["", "Threshold miss"])
        lines.append(f"- best patchability {best_patchability} < required {min_patchability}")
    top_rejections = list(summary.get("top_rejections") or [])
    if top_rejections:
        lines.extend(["", "Why no PR"])
        for reason, count in top_rejections[:3]:
            lines.append(f"- {reason}: {count}")
    if summary.get("bottleneck"):
        lines.append(f"- Bottleneck: {summary['bottleneck']}")
    return "\n".join(lines)


def _upsert_progress_card(run: ManagedRun) -> None:
    if run.notification_route is None or not _supports_telegram_progress_card(run):
        return
    card = _render_progress_card(run)
    if run.progress_message_id:
        telegram_edit_message(card, run.progress_message_id, route=run.notification_route)
        return
    result = telegram_send_message(card, route=run.notification_route)
    if result.get("ok") and result.get("message_id"):
        run.progress_message_id = int(result["message_id"])


def _send_terminal_notification(run: ManagedRun) -> None:
    if run.notification_route is None or run.terminal_notified:
        return
    summary = _render_terminal_summary(run)
    if _supports_telegram_progress_card(run):
        if run.progress_message_id:
            telegram_edit_message(summary, run.progress_message_id, route=run.notification_route)
            run.terminal_notified = True
            return
        notify(summary, route=run.notification_route)
    else:
        notify(summary, route=run.notification_route)
    run.terminal_notified = True


def _render_event_message(run: ManagedRun, event: dict[str, Any]) -> str:
    payload = _canonical_run_result(run)
    lines = [
        f"ROVER {str(event.get('type') or 'update').upper()}",
        f"Run: {run.run_id}",
        f"Repo: {_current_repo_label(run)}",
        f"State: {run.state}",
        str(event.get("summary") or ""),
    ]
    details = dict(event.get("details") or {})
    if details.get("repo_full_name") and not run.repo:
        lines.append(f"Selected repo: {details['repo_full_name']}")
    if payload["submitted_new_prs"]:
        lines.append(f"PR: {payload['submitted_new_prs'][0].get('pr_url', '')}")
    elif payload["existing_open_prs"]:
        lines.append(f"Existing PR: {payload['existing_open_prs'][0].get('pr_url', '')}")
    elif payload["outcome_code"]:
        lines.append(f"Outcome: {payload['outcome_code']}")
    return "\n".join(part for part in lines if part)


def _infer_stage_event(line: str) -> tuple[str, dict[str, Any]] | None:
    lowered = line.strip().lower()
    if not lowered:
        return None
    patterns = [
        ("discovering opportunities", ("searching github", "discover_selected", "search mode")),
        ("scanning repository files", ("scanning", "pattern scanner", "opportunity scan")),
        ("reading target files", ("target:", "files:", "selected repo", "opportunity_selected")),
        ("reviewing repository history", ("repo recon", "merged /", "closed prs")),
        ("generating patch", ("patch_generated", "type:", "rationale:")),
        ("preparing fork and branch", ("gh repo fork", "[branch]", "creating branch", "fork/pr failed")),
        ("opening pull request", ("pr submitted", "pull request", "opening pr")),
        ("waiting for human approval", ("human approval", "queued patch instead of submitting")),
    ]
    for summary, needles in patterns:
        if any(needle in lowered for needle in needles):
            return summary, {"source": "stdout"}
    return None


def _append_stage_from_log(run: ManagedRun, line: str) -> None:
    inferred = _infer_stage_event(line)
    if inferred is None:
        return
    summary, details = inferred
    normalized = summary.lower()
    with run.lock:
        if normalized == run.last_stage_key:
            return
        run.last_stage_key = normalized
        if run.state in {"queued", "started"}:
            run.state = "running"
    _append_event(run, "stage", summary, details)


def _maybe_notify_run_events(run: ManagedRun) -> None:
    if run.notification_route is None:
        return
    if _supports_telegram_progress_card(run):
        _upsert_progress_card(run)
        with run.lock:
            if run.events:
                run.last_notified_seq = max(run.last_notified_seq, max(int(event["seq"]) for event in run.events))
        return
    pending: list[dict[str, Any]] = []
    with run.lock:
        for event in run.events:
            if int(event["seq"]) <= run.last_notified_seq:
                continue
            pending.append(dict(event))
    for event in pending:
        if event["type"] in ROVER_NOTIFY_ON_EVENT_TYPES:
            notify(_render_event_message(run, event), route=run.notification_route)
        with run.lock:
            run.last_notified_seq = max(run.last_notified_seq, int(event["seq"]))


def _maybe_notify_progress(run: ManagedRun) -> None:
    if run.notification_route is None or not ROVER_NOTIFY_PROGRESS:
        return
    if _supports_telegram_progress_card(run):
        _upsert_progress_card(run)
        return
    if run.state not in {"queued", "started", "running"}:
        return
    now = time.time()
    if now - run.last_progress_at < ROVER_NOTIFY_INTERVAL_SECONDS:
        return
    snapshot_key, _ = _notification_snapshot(run)
    if ROVER_NOTIFY_ONLY_ON_CHANGE and snapshot_key == run.last_progress_key:
        run.last_progress_at = now
        return
    if notify(_render_progress_message(run), route=run.notification_route):
        run.last_progress_at = now
        run.last_progress_key = snapshot_key


def _maybe_notify_stall(run: ManagedRun) -> None:
    if run.notification_route is None or ROVER_NOTIFY_STALL_SECONDS <= 0:
        return
    if run.state not in {"queued", "started", "running"}:
        return
    if _supports_telegram_progress_card(run):
        _upsert_progress_card(run)
        return
    now = time.time()
    with run.lock:
        stalled_for = now - run.last_activity_at
    if stalled_for < ROVER_NOTIFY_STALL_SECONDS:
        return
    snapshot_key, last_summary = _notification_snapshot(run)
    stall_key = f"stall|{snapshot_key}"
    if ROVER_NOTIFY_ONLY_ON_CHANGE and stall_key == run.last_progress_key:
        return
    message = "\n".join(
        [
            "ROVER STALLED",
            f"Run: {run.run_id}",
            f"Repo: {_current_repo_label(run)}",
            f"State: {run.state}",
            f"Idle: {int(stalled_for)}s",
            f"Last: {last_summary or 'no recent progress event'}",
        ]
    )
    if notify(message, route=run.notification_route):
        run.last_progress_at = now
        run.last_progress_key = stall_key


def _run_notification_loop(run: ManagedRun) -> None:
    if run.notification_route is None:
        return
    while True:
        _sync_repo_events(run)
        _update_run_summary(run)
        _maybe_notify_run_events(run)
        _maybe_notify_progress(run)
        _maybe_notify_stall(run)
        if run.state not in {"queued", "started", "running"}:
            _maybe_notify_run_events(run)
            _send_terminal_notification(run)
            break
        time.sleep(1.0)


def _consume_process_output(run: ManagedRun) -> None:
    proc = run.process
    if proc is None:
        return
    if proc.stdout is not None:
        for line in proc.stdout:
            with run.lock:
                run.logs.append(line.rstrip("\n"))
                run.last_activity_at = time.time()
            _append_stage_from_log(run, line.rstrip("\n"))
            _sync_repo_events(run)
    proc.wait()
    _sync_repo_events(run)
    _update_run_summary(run)
    run.returncode = proc.returncode
    run.finished_at = run.finished_at or _utc_now()
    if run.state != "canceled":
        run.state = "completed" if proc.returncode == 0 else "failed"
        _append_event(
            run,
            "completed" if proc.returncode == 0 else "failed",
            "Contribution run completed." if proc.returncode == 0 else "Contribution run failed.",
            {
                "returncode": proc.returncode,
                "engine_run_id": run.engine_run_id,
                "summary": run.summary or {},
            },
        )


def _spawn_run(
    *,
    repo: str,
    goal: str,
    count: int,
    dry_run: bool,
    first_pr: bool,
    override_limits: bool,
    notification_route: NotificationRoute | None = None,
) -> ManagedRun:
    global _active_proc

    run_id = uuid.uuid4().hex
    command = [sys.executable, "-m", "app.builder"]
    command += ["--contrib", repo] if repo else ["--contrib"]
    command += ["--goal", goal, f"--{count}", "--external-run-id", run_id]
    if dry_run:
        command.append("--dry-run")
    if first_pr:
        command.append("--first-pr")
    if override_limits:
        command.append("--override-limits")

    run = ManagedRun(
        run_id=run_id,
        mode="targeted" if repo else "search",
        repo=repo,
        goal=goal,
        count=count,
        dry_run=dry_run,
        first_pr=first_pr,
        override_limits=override_limits,
        command=command,
        started_at=_utc_now(),
        notification_route=notification_route,
    )
    _append_event(run, "queued", "Contribution run queued.", {"command": " ".join(command)})

    proc = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    run.process = proc
    run.pid = proc.pid
    run.state = "started"
    _append_event(run, "started", "Contribution run started in background.", {"pid": proc.pid})
    _register_run(run)
    _active_proc = proc

    watcher = threading.Thread(target=_consume_process_output, args=(run,), daemon=True)
    watcher.start()
    notifier = threading.Thread(target=_run_notification_loop, args=(run,), daemon=True)
    notifier.start()
    return run


def _status_payload(run: ManagedRun) -> dict[str, Any]:
    _sync_repo_events(run)
    _update_run_summary(run)
    return {
        "run_id": run.run_id,
        "running": run.state in {"queued", "started", "running"},
        "state": run.state,
        "mode": run.mode,
        "repo": run.repo,
        "goal": run.goal,
        "count": run.count,
        "dry_run": run.dry_run,
        "first_pr": run.first_pr,
        "override_limits": run.override_limits,
        "pid": run.pid,
        "returncode": run.returncode,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "engine_run_id": run.engine_run_id,
        "events": len(run.events),
        "summary": run.summary,
    }


def _find_run(run_id: str = "") -> ManagedRun | None:
    if run_id:
        with _RUNS_LOCK:
            return _RUNS.get(run_id)
    return _latest_active_run() or _latest_known_run()


mcp = FastMCP(
    name="rover",
    instructions=(
        "Rover contribution engine. "
        "Tools: get_status, list_opportunities, list_prs, contrib_report, doctor, inspect_repo, "
        "route_command, start_run, run_contribution, contrib_once, contrib_targeted, cancel_run, "
        "stop_contribution, get_run_status, get_run_events, get_run_result, get_logs, get_config, update_config."
    ),
)


@mcp.tool()
def get_status() -> dict[str, Any]:
    """Return engine stats: recent runs, queued opportunities, and pattern acceptance rates."""
    active = _latest_active_run()
    return {
        "recent_runs": _store.latest_run_summaries(limit=5),
        "queued_opportunities": _store.queued_opportunities(limit=10),
        "pattern_stats": _store.pattern_stats(),
        "active_run": _status_payload(active) if active else None,
    }


@mcp.tool()
def list_opportunities(limit: int = 20) -> list[dict]:
    """List queued READY opportunities ranked by acceptance score."""
    return _store.queued_opportunities(limit=limit)


@mcp.tool()
def list_prs(limit: int = 20) -> list[dict]:
    """List submitted pull requests with status (open/merged/closed)."""
    with _store._connect() as conn:
        rows = conn.execute(
            """
            SELECT repo_full_name, pr_url, pr_title, status,
                   improvement_type, submitted_at, resolved_at, maintainer_signal
            FROM pull_requests ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@mcp.tool()
def contrib_report(limit: int = 5) -> dict[str, Any]:
    """Return structured report data for recent contribution runs."""
    from src.contrib.pr_generator import build_contribution_report, get_contribution_report_data

    summaries, queued = get_contribution_report_data(limit=limit)
    return {
        "limit": limit,
        "summaries": summaries,
        "queued": queued,
        "rendered": build_contribution_report(limit=limit),
    }


@mcp.tool()
def doctor() -> dict[str, Any]:
    """Check that all required tools (gh, git, AI backend) are installed and configured."""
    from src.core.doctor import build_doctor_report, collect_doctor_checks

    checks = collect_doctor_checks()
    return {
        "checks": [check.__dict__ for check in checks],
        "rendered": build_doctor_report(),
    }


@mcp.tool()
def inspect_repo(repo: str) -> dict[str, Any]:
    """Fetch and analyze a GitHub repo without submitting a PR."""
    from src.contrib.pr_generator import build_repo_inspect_report_from_data, fetch_repo_candidate_with_scope, get_repo_inspect_data

    log = logging.getLogger("mcp.inspect")
    candidate = fetch_repo_candidate_with_scope(repo, log, enforce_scope=False)
    data = get_repo_inspect_data(candidate)
    return {"data": data, "rendered": build_repo_inspect_report_from_data(data)}


@mcp.tool()
def route_command(text: str) -> dict[str, Any]:
    """Map natural-language text to a canonical Rover action for chat-style agent shells."""
    request = parse_command_text(text)
    return {
        "action": request.action,
        "count": request.count,
        "repo": request.repo,
        "goal": request.goal,
        "scan_kind": request.scan_kind,
        "dry_run": request.dry_run,
        "first_pr": request.first_pr,
        "confidence": request.confidence,
        "rationale": request.rationale,
        "cli_args": request.to_cli_args(),
    }


@mcp.tool()
def start_run(
    repo: str = "",
    goal: str = "bugfix",
    count: int = 1,
    dry_run: bool = True,
    first_pr: bool = False,
    override_limits: bool = False,
    notify_transport: str = "",
    notify_channel: str = "",
    notify_target: str = "",
    notify_account: str = "",
    notify_thread_id: str = "",
) -> dict[str, Any]:
    """Start a contribution run in the background and return a stable run_id."""
    if repo:
        blocked = _targeted_preflight(repo, override_limits)
        if blocked is not None:
            blocked.update(
                {
                    "goal": goal,
                    "count": count,
                    "dry_run": dry_run,
                    "first_pr": first_pr,
                    "override_limits": override_limits,
                }
            )
            route = _build_notification_route(
                notify_transport=notify_transport,
                notify_channel=notify_channel,
                notify_target=notify_target,
                notify_account=notify_account,
                notify_thread_id=notify_thread_id,
            )
            if route is not None:
                blocked["notification"] = {
                    "transport": route.transport,
                    "channel": route.channel,
                    "target": route.target,
                    "account": route.account,
                    "thread_id": route.thread_id,
                }
            return blocked
    run = _spawn_run(
        repo=repo,
        goal=goal,
        count=count,
        dry_run=dry_run,
        first_pr=first_pr,
        override_limits=override_limits,
        notification_route=_build_notification_route(
            notify_transport=notify_transport,
            notify_channel=notify_channel,
            notify_target=notify_target,
            notify_account=notify_account,
            notify_thread_id=notify_thread_id,
        ),
    )
    payload = _status_payload(run)
    payload.update({"accepted": True, "command": " ".join(run.command)})
    if run.notification_route is not None:
        payload["notification"] = {
            "transport": run.notification_route.transport,
            "channel": run.notification_route.channel,
            "target": run.notification_route.target,
            "account": run.notification_route.account,
            "thread_id": run.notification_route.thread_id,
        }
    return payload


@mcp.tool()
def run_contribution(
    repo: str = "",
    goal: str = "bugfix",
    count: int = 1,
    dry_run: bool = False,
    first_pr: bool = False,
    override_limits: bool = False,
    notify_transport: str = "",
    notify_channel: str = "",
    notify_target: str = "",
    notify_account: str = "",
    notify_thread_id: str = "",
) -> dict[str, Any]:
    """Compatibility alias for starting a background contribution run."""
    payload = start_run(
        repo=repo,
        goal=goal,
        count=count,
        dry_run=dry_run,
        first_pr=first_pr,
        override_limits=override_limits,
        notify_transport=notify_transport,
        notify_channel=notify_channel,
        notify_target=notify_target,
        notify_account=notify_account,
        notify_thread_id=notify_thread_id,
    )
    payload["status"] = "started" if payload.get("accepted", True) else str(payload.get("status") or "blocked")
    return payload


@mcp.tool()
def contrib_once(
    count: int = 1,
    goal: str = "bugfix",
    dry_run: bool = True,
    first_pr: bool = False,
    override_limits: bool = False,
    notify_transport: str = "",
    notify_channel: str = "",
    notify_target: str = "",
    notify_account: str = "",
    notify_thread_id: str = "",
) -> dict[str, Any]:
    """Start one search-mode contribution run for agent shells that separate targeted and search actions."""
    return run_contribution(
        repo="",
        goal=goal,
        count=count,
        dry_run=dry_run,
        first_pr=first_pr,
        override_limits=override_limits,
        notify_transport=notify_transport,
        notify_channel=notify_channel,
        notify_target=notify_target,
        notify_account=notify_account,
        notify_thread_id=notify_thread_id,
    )


@mcp.tool()
def contrib_targeted(
    repo: str,
    count: int = 1,
    goal: str = "bugfix",
    dry_run: bool = True,
    first_pr: bool = False,
    override_limits: bool = False,
    notify_transport: str = "",
    notify_channel: str = "",
    notify_target: str = "",
    notify_account: str = "",
    notify_thread_id: str = "",
) -> dict[str, Any]:
    """Start one targeted contribution run for a specific repo."""
    return run_contribution(
        repo=repo,
        goal=goal,
        count=count,
        dry_run=dry_run,
        first_pr=first_pr,
        override_limits=override_limits,
        notify_transport=notify_transport,
        notify_channel=notify_channel,
        notify_target=notify_target,
        notify_account=notify_account,
        notify_thread_id=notify_thread_id,
    )


@mcp.tool()
def cancel_run(run_id: str) -> dict[str, Any]:
    """Stop a background contribution run by run_id."""
    run = _find_run(run_id)
    if run is None:
        return {"status": "not_found", "run_id": run_id}
    proc = run.process
    if proc is None or proc.poll() is not None:
        return {"status": "not_running", "run_id": run.run_id, "returncode": run.returncode}
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    run.returncode = proc.returncode
    run.finished_at = _utc_now()
    run.state = "canceled"
    _append_event(run, "canceled", "Contribution run canceled.", {"returncode": proc.returncode})
    return {"status": "stopped", "run_id": run.run_id, "pid": run.pid}


@mcp.tool()
def stop_contribution(run_id: str = "") -> dict[str, Any]:
    """Compatibility alias that stops the latest active contribution process when no run_id is supplied."""
    run = _find_run(run_id)
    if run is None or run.state not in {"queued", "started", "running"}:
        return {"status": "not_running"}
    return cancel_run(run.run_id)


@mcp.tool()
def get_run_status(run_id: str = "") -> dict[str, Any]:
    """Check whether a contribution run is currently active."""
    run = _find_run(run_id)
    if run is None:
        return {"running": False}
    return _status_payload(run)


@mcp.tool()
def get_run_events(run_id: str, after_seq: int = 0) -> dict[str, Any]:
    """Return structured lifecycle events for one background contribution run."""
    run = _find_run(run_id)
    if run is None:
        return {"run_id": run_id, "events": [], "next_after_seq": after_seq}
    _sync_repo_events(run)
    with run.lock:
        events = [event for event in run.events if int(event["seq"]) > after_seq]
    next_after_seq = events[-1]["seq"] if events else after_seq
    return {"run_id": run.run_id, "events": events, "next_after_seq": next_after_seq}


@mcp.tool()
def get_run_result(run_id: str) -> dict[str, Any]:
    """Return the final summary, logs, and status for one background contribution run."""
    run = _find_run(run_id)
    if run is None:
        return {"status": "not_found", "run_id": run_id}
    _sync_repo_events(run)
    _update_run_summary(run)
    with run.lock:
        logs_tail = run.logs[-50:]
    payload = _canonical_run_result(run)
    payload["logs_tail"] = logs_tail
    return payload


@mcp.tool()
def contrib_check() -> dict[str, Any]:
    """Poll all open PRs for status changes and fetch maintainer feedback."""
    result = subprocess.run(
        [sys.executable, "-m", "app.builder", "--contrib-check", "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    return {"ok": result.returncode == 0, "output": (result.stdout + result.stderr).strip()[-3000:]}


@mcp.tool()
def contrib_respond() -> dict[str, Any]:
    """Handle maintainer feedback without polling PR status first."""
    result = subprocess.run(
        [sys.executable, "-m", "app.builder", "--contrib-respond", "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    return {"ok": result.returncode == 0, "output": (result.stdout + result.stderr).strip()[-3000:]}


@mcp.tool()
def get_logs(run_id: str = "", lines: int = 50) -> dict[str, Any]:
    """Return recent in-memory logs for a run, or the latest engine log file when run_id is omitted."""
    run = _find_run(run_id)
    if run is not None:
        with run.lock:
            return {"run_id": run.run_id, "lines": run.logs[-lines:]}

    log_files = sorted(LOG_DIR.glob("contrib_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not log_files:
        return {"run_id": "", "lines": []}
    latest = log_files[0]
    content = latest.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"run_id": "", "path": str(latest), "lines": content[-lines:]}


@mcp.tool()
def get_config() -> dict[str, str]:
    """Read current .env settings. Token values are masked for security."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return {"error": ".env not found"}
    result: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        result[key] = (value[:4] + "****") if key in _SECRET_KEYS and len(value) > 4 else value
    return result


@mcp.tool()
def update_config(key: str, value: str) -> dict[str, str]:
    """Update a single key in .env. Secret keys (tokens) cannot be changed via MCP."""
    if key in _SECRET_KEYS:
        return {"status": "rejected", "reason": f"{key} cannot be updated via MCP."}
    env_file = ROOT / ".env"
    if not env_file.exists():
        return {"status": "error", "reason": ".env not found"}
    lines = env_file.read_text(encoding="utf-8").splitlines()
    pattern = re.compile(rf"^(#\s*)?{re.escape(key)}\s*=")
    updated = False
    new_lines: list[str] = []
    for line in lines:
        if pattern.match(line):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return {"status": "updated", "key": key, "value": value}




@mcp.tool()
def start_pr_monitor(interval_seconds: int = 300) -> dict[str, Any]:
    """Start background PR monitor that polls open PRs and sends Telegram notifications on comments, merges, or closes.

    interval_seconds: how often to poll (default 300 = 5 minutes, minimum 60).
    Returns immediately; polling runs in a daemon thread.
    """
    return _pr_monitor.start(interval_seconds)


@mcp.tool()
def stop_pr_monitor() -> dict[str, Any]:
    """Stop the background PR monitor thread."""
    return _pr_monitor.stop()


@mcp.tool()
def get_pr_monitor_status() -> dict[str, Any]:
    """Return current state of the background PR monitor."""
    return _pr_monitor.status()


@mcp.tool()
def start_telegram_bot() -> dict[str, Any]:
    """Start the Telegram command bot so users can send commands from Telegram.

    Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to be set in .env.
    Returns immediately; the bot runs in a daemon thread.
    """
    global _tg_bot
    with _tg_bot_lock:
        if _tg_bot is not None and _tg_bot.running:
            return {"status": "already_running", "chat_id": TELEGRAM_CHAT}
        if not TELEGRAM_TOKEN:
            return {"status": "error", "reason": "TELEGRAM_BOT_TOKEN not configured in .env"}
        if not TELEGRAM_CHAT:
            return {"status": "error", "reason": "TELEGRAM_CHAT_ID not configured in .env"}
        _tg_bot = TelegramCommandBot(TELEGRAM_TOKEN, TELEGRAM_CHAT, ROOT)
        _tg_bot.start()
    return {"status": "started", "chat_id": TELEGRAM_CHAT}


@mcp.tool()
def stop_telegram_bot() -> dict[str, Any]:
    """Stop the Telegram command bot."""
    global _tg_bot
    with _tg_bot_lock:
        if _tg_bot is None or not _tg_bot.running:
            return {"status": "not_running"}
        _tg_bot.stop()
    return {"status": "stopping"}


@mcp.tool()
def get_telegram_bot_status() -> dict[str, Any]:
    """Return current state of the Telegram command bot."""
    with _tg_bot_lock:
        running = _tg_bot is not None and _tg_bot.running
    return {
        "running": running,
        "chat_id": TELEGRAM_CHAT,
        "token_configured": bool(TELEGRAM_TOKEN),
    }


def main() -> None:
    if PR_MONITOR_INTERVAL_SECONDS > 0:
        _pr_monitor.start(PR_MONITOR_INTERVAL_SECONDS)
    if TELEGRAM_BOT_ENABLED:
        if TELEGRAM_TOKEN and TELEGRAM_CHAT:
            global _tg_bot
            with _tg_bot_lock:
                _tg_bot = TelegramCommandBot(TELEGRAM_TOKEN, TELEGRAM_CHAT, ROOT)
                _tg_bot.start()
        else:
            logging.getLogger("mcp").warning(
                "TELEGRAM_BOT_ENABLED=true but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"
            )
    mcp.run()


if __name__ == "__main__":
    main()
