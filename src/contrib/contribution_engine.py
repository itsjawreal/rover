from __future__ import annotations

import logging
from dataclasses import dataclass

from src.core.cli_ui import box_title, bullet_block, key_value_block, table
from src.contrib.contribution_store import ContributionStore

BENCHMARK_REPOS: tuple[dict[str, str], ...] = (
    {"repo": "GPT-AGI/Clawd-Code", "lane": "hard-targeted-live"},
    {"repo": "Alishahryar1/free-claude-code", "lane": "open-pr-pacing"},
    {"repo": "past-successful-small-tested", "lane": "success"},
    {"repo": "inspect-only-reference", "lane": "early-block"},
    {"repo": "low-source-high-artifact-reference", "lane": "scan-trust"},
)


@dataclass
class ContributionRunSummary:
    run_id: int
    submitted: int
    target: int
    attempts: int
    ai_calls: int
    est_tokens: int
    discovered: int
    state_counts: dict
    top_rejections: list
    bottleneck: str = ""


class ContributionEngine:
    """Thin orchestration layer around contribution state and run summaries."""

    def __init__(self, store: ContributionStore) -> None:
        self.store = store
        self.active_run_id: int | None = None

    def start_run(self, mode: str, target_count: int, external_run_id: str = "") -> int:
        self.active_run_id = self.store.start_run(
            mode=mode,
            target_count=target_count,
            external_run_id=external_run_id,
        )
        return self.active_run_id

    def finish_run(
        self,
        submitted: int,
        target: int,
        attempts: int,
        usage: dict[str, int],
        log: logging.Logger,
        extra_summary: dict | None = None,
    ) -> dict:
        if self.active_run_id is None:
            return {}

        summary = self.store.summarize_run(self.active_run_id)
        summary.update(
            {
                "run_id": self.active_run_id,
                "submitted": submitted,
                "target": target,
                "attempts": attempts,
                "ai_calls": usage.get("calls", 0),
                "est_tokens": usage.get("est_tokens", 0),
            }
        )
        if extra_summary:
            summary.update(extra_summary)
        summary.setdefault("death_stage", self._death_stage(summary, submitted))
        summary.setdefault("outcome_code", self._outcome_code(summary, submitted))
        self.store.finish_run(self.active_run_id, summary)
        self._log_summary(summary, log)
        return summary

    def can_submit_to_repo(self, repo_full_name: str) -> bool:
        return not self.store.has_open_pr(repo_full_name)

    def get_report_data(self, limit: int = 5) -> tuple[list[dict], list[dict]]:
        summaries = self.store.latest_run_summaries(limit=limit)
        queued = self.store.queued_opportunities(limit=10)
        return summaries, queued

    def benchmark_suite(self, limit: int = 20) -> dict:
        summaries = self.store.latest_run_summaries(limit=limit)
        submitted = sum(int(summary.get("submitted", 0) or 0) for summary in summaries)
        est_tokens = sum(int(summary.get("est_tokens", 0) or 0) for summary in summaries)
        ai_calls = sum(int(summary.get("ai_calls", 0) or 0) for summary in summaries)
        generated = sum(int(summary.get("generated", 0) or 0) for summary in summaries)
        discovered = sum(int(summary.get("discovered", 0) or 0) for summary in summaries)
        self_review_rejected = sum(int(summary.get("self_review_rejected", 0) or 0) for summary in summaries)
        broad_rejected = sum(int(summary.get("broad_rejected_early", 0) or 0) for summary in summaries)
        shape_rejected = sum(int(summary.get("shape_rejected_early", 0) or 0) for summary in summaries)
        late_rejects = self_review_rejected
        early_rejects = broad_rejected + shape_rejected
        return {
            "repos": list(BENCHMARK_REPOS),
            "submitted_prs_per_est_token": submitted / est_tokens if est_tokens else 0,
            "tokens_per_submitted_pr": est_tokens / submitted if submitted else 0,
            "ai_calls_per_submitted_pr": ai_calls / submitted if submitted else 0,
            "late_reject_ratio": late_rejects / generated if generated else 0,
            "early_reject_ratio": early_rejects / discovered if discovered else 0,
            "self_review_rejected_per_generated": self_review_rejected / generated if generated else 0,
            "broad_rejected_before_ai_per_discovered": broad_rejected / discovered if discovered else 0,
        }

    def build_operator_report(self, limit: int = 5) -> str:
        summaries = self.store.latest_run_summaries(limit=limit)
        queued = self.store.queued_opportunities(limit=10)
        if not summaries and not queued:
            return "No contribution engine runs recorded yet."

        latest = summaries[0] if summaries else {}
        lines = [box_title("Contribution Engine Report")]
        if latest:
            submitted = latest.get("submitted", 0)
            target = latest.get("target", 0)
            attempts = latest.get("attempts", 0)
            states = ", ".join(f"{key}={value}" for key, value in latest.get("state_counts", {}).items()) or "-"
            lines.extend(
                [
                    "",
                    key_value_block(
                        "Latest run",
                        [
                            ("Run", f"#{latest.get('run_id', '?')}"),
                            ("Submitted", f"{submitted}/{target}"),
                            ("Attempts", attempts),
                            ("AI calls", latest.get("ai_calls", 0)),
                            ("Estimated tokens", latest.get("est_tokens", 0)),
                            ("Outcome", latest.get("outcome_code", "-")),
                            ("Death stage", latest.get("death_stage", "-")),
                            ("States", states),
                        ],
                    ),
                ]
            )
            if latest.get("top_rejections"):
                pretty_rejections = ", ".join(
                    f"{reason} x{count}" for reason, count in latest["top_rejections"][:3]
                )
                lines.append(f"Top rejections: {pretty_rejections}")
            if latest.get("bottleneck"):
                lines.append(f"Bottleneck: {latest['bottleneck']}")

        if summaries:
            recent_rows: list[list[object]] = []
            for summary in summaries:
                recent_rows.append(
                    [
                        f"#{summary.get('run_id', '?')}",
                        f"{summary.get('submitted', 0)}/{summary.get('target', 0)}",
                        summary.get("attempts", 0),
                        summary.get("ai_calls", 0),
                        ", ".join(f"{k}={v}" for k, v in summary.get("state_counts", {}).items()) or "-",
                    ]
                )
            lines.extend(
                [
                    "",
                    table("Recent runs", ["Run", "Submitted", "Attempts", "AI calls", "States"], recent_rows),
                ]
            )
            bottlenecks = [
                f"run #{summary.get('run_id', '?')}: {summary['bottleneck']}"
                for summary in summaries
                if summary.get("bottleneck")
            ]
            if bottlenecks:
                lines.extend(["", bullet_block("Run bottlenecks", bottlenecks)])
        if queued:
            queue_rows = [
                [
                    f"#{opportunity.get('id')}",
                    opportunity.get("repo_full_name"),
                    opportunity.get("pattern_type"),
                    opportunity.get("target_file"),
                    opportunity.get("acceptance_score"),
                ]
                for opportunity in queued
            ]
            lines.extend(
                [
                    "",
                    table(
                        f"Ready queue ({len(queued)} shown)",
                        ["ID", "Repo", "Pattern", "File", "Score"],
                        queue_rows,
                    ),
                ]
            )
        lines.append("")
        lines.append("Suggested next step:")
        if queued:
            next_steps = ["Run `menisik run 1` to consume the strongest queued opportunity."]
        elif latest.get("top_rejections"):
            top_reason = latest["top_rejections"][0][0]
            next_steps = [f"Investigate rejection pattern `{top_reason}` before widening search or targeting larger repos."]
        else:
            next_steps = ["Run `menisik run 1` to start a new contribution cycle."]
        lines.append(bullet_block("Suggested next step", next_steps))
        return "\n".join(lines)

    def _log_summary(self, summary: dict, log: logging.Logger) -> None:
        log.info(
            "Contribution run summary: discovered=%d | states=%s | top_rejections=%s",
            summary.get("discovered", 0),
            summary.get("state_counts", {}),
            summary.get("top_rejections", []),
        )
        if summary.get("queued"):
            log.info("Queued-but-not-submitted opportunities: %d", len(summary["queued"]))
        if summary.get("bottleneck"):
            log.warning(summary["bottleneck"])

    def _death_stage(self, summary: dict, submitted: int) -> str:
        if submitted:
            return "submit"
        if summary.get("current_stage"):
            return str(summary["current_stage"])
        top_rejections = summary.get("top_rejections") or []
        if top_rejections:
            reason = top_rejections[0][0]
            if reason in {"target_area_too_broad", "shortlist_below_patchability_threshold"}:
                return "qualify"
            if reason in {"patch_not_minimal", "patch_shape_high_risk", "patch_shape_too_broad"}:
                return "verify"
            if reason in {"self_review_rejected", "semantic_review_rejected"}:
                return "review"
        return "discover"

    def _outcome_code(self, summary: dict, submitted: int) -> str:
        if submitted:
            return "submitted"
        if summary.get("manual_review_queued"):
            return "manual_approval_required"
        top_rejections = summary.get("top_rejections") or []
        if top_rejections:
            reason = str(top_rejections[0][0])
            mapping = {
                "target_area_too_broad": "shortlist_below_patchability_threshold",
                "patch_not_minimal": "structural_review_rejected",
                "patch_shape_high_risk": "structural_review_rejected",
                "patch_shape_too_broad": "structural_review_rejected",
                "self_review_rejected": "semantic_review_rejected",
            }
            return mapping.get(reason, reason)
        state_counts = summary.get("state_counts") or {}
        if state_counts.get("READY"):
            return "manual_approval_required"
        if not summary.get("shortlisted"):
            return "no_narrow_candidate"
        return "no_submit"
