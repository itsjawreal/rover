from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.contribution_engine import ContributionEngine
from src.doctor import build_doctor_report
from src.pr_engine import Opportunity, PREngineStore, PatternScanner, qualify_opportunity
from src.pr_generator import build_repo_inspect_report
from src.opportunity_engine import guess_test_target
from src.scraper import RepoCandidate


def _candidate(files: dict[str, str], full_name: str = "example/repo") -> RepoCandidate:
    return RepoCandidate(
        name=full_name.split("/")[-1],
        full_name=full_name,
        description="Sample crypto python cli",
        stars=500,
        forks=80,
        license="mit",
        url=f"https://github.com/{full_name}",
        default_branch="main",
        pushed_days_ago=1,
        topics=["crypto", "python", "cli"],
        files=files,
    )


class PatternScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scanner = PatternScanner()

    def test_missing_timeout_positive_and_negative(self) -> None:
        positive = _candidate(
            {
                "client.py": "import requests\n\ndef run():\n    return requests.get('https://api.example.com').json()\n",
                "tests/test_client.py": "def test_run():\n    assert True\n",
            }
        )
        negative = _candidate(
            {
                "client.py": "import requests\n\ndef run():\n    return requests.get('https://api.example.com', timeout=10).json()\n",
            }
        )

        positive_patterns = {opp.pattern_type for opp in self.scanner.scan(positive)}
        negative_patterns = {opp.pattern_type for opp in self.scanner.scan(negative)}

        self.assertIn("missing_timeout", positive_patterns)
        self.assertNotIn("missing_timeout", negative_patterns)

    def test_scans_all_v1_patterns(self) -> None:
        candidate = _candidate(
            {
                "timeouts.py": "import requests\n\ndef fetch():\n    return requests.get('https://api.example.com')\n",
                "shape.py": "def parse(resp):\n    return resp.json()['data']['items']\n",
                "paths.py": "def dump(args):\n    open(args.output, 'w').write('x')\n",
                "exceptions.py": "def run():\n    try:\n        return risky()\n    except Exception:\n        return None\n",
                "inputs.py": "import os\n\ndef load_limit():\n    return int(os.getenv('LIMIT'))\n",
                "cleanup.py": "def dump(path):\n    handle = open(path)\n    return handle.read()\n",
                "regression.py": "# TODO regression: None payload path\n\ndef work():\n    return 1\n",
                "tests/test_any.py": "def test_any():\n    assert True\n",
            }
        )

        patterns = {opp.pattern_type for opp in self.scanner.scan(candidate)}

        self.assertIn("missing_timeout", patterns)
        self.assertIn("unchecked_response_shape", patterns)
        self.assertIn("unsafe_file_write_or_path", patterns)
        self.assertIn("overbroad_exception_handling", patterns)
        self.assertIn("missing_input_validation", patterns)
        self.assertIn("resource_cleanup_gap", patterns)
        self.assertIn("missing_regression_test_for_obvious_bugfix", patterns)

    def test_scans_maintainer_todo_feature_upgrade(self) -> None:
        candidate = _candidate(
            {
                "cli.py": "# TODO: add --format json support for report output\n\ndef run():\n    return 1\n",
                "tests/test_cli.py": "def test_cli():\n    assert True\n",
            }
        )

        opportunities = self.scanner.scan(candidate)
        feature_opps = [opp for opp in opportunities if opp.opportunity_kind == "feature_upgrade"]

        self.assertEqual(len(feature_opps), 1)
        self.assertEqual(feature_opps[0].pattern_type, "maintainer_todo_feature_upgrade")
        self.assertTrue(feature_opps[0].maintainer_intent)

    def test_guess_test_target_prefers_repo_layout_near_target_file(self) -> None:
        files = {
            "agent/backtest/validation.py": "def main():\n    return 1\n",
            "agent/tests/test_validation.py": "def test_validation():\n    assert True\n",
            "frontend/src/app.ts": "export const app = 1;\n",
            "tests/test_root_smoke.py": "def test_root_smoke():\n    assert True\n",
        }

        guessed = guess_test_target(files, "agent/backtest/validation.py")

        self.assertEqual(guessed, "agent/tests/test_validation.py")


class QualificationTests(unittest.TestCase):
    def test_accepts_concrete_broken_path(self) -> None:
        candidate = _candidate(
            {
                "client.py": "import requests\n\ndef fetch():\n    return requests.get('https://api.example.com').json()\n",
                "tests/test_client.py": "def test_fetch():\n    assert True\n",
            }
        )
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="client.py",
            pattern_type="missing_timeout",
            failure_mode="A slow upstream endpoint can block the command forever instead of failing fast on a valid request path.",
            evidence="The only HTTP call in client.py omits timeout= entirely.",
            patch_scope=1,
            test_target="tests/test_client.py",
            acceptance_score=80,
        )

        result = qualify_opportunity(candidate, opportunity)

        self.assertTrue(result.accepted)
        self.assertGreaterEqual(result.score, 80)

    def test_rejects_vague_cleanup_claim(self) -> None:
        candidate = _candidate({"client.py": "def run():\n    return 1\n"})
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="client.py",
            pattern_type="overbroad_exception_handling",
            failure_mode="This is safer and more consistent for future changes.",
            evidence="The cleanup could make the code more robust.",
            patch_scope=1,
            test_target="",
            acceptance_score=90,
        )

        result = qualify_opportunity(candidate, opportunity)

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason_code, "evidence_too_weak")

    def test_rejects_broad_core_file(self) -> None:
        candidate = _candidate({"core.py": "\n".join("x = 1" for _ in range(450))})
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="core.py",
            pattern_type="missing_timeout",
            failure_mode="A slow upstream endpoint can block the command forever instead of failing fast on a valid request path.",
            evidence="This file contains the only network request and it omits timeout=.",
            patch_scope=1,
            test_target="",
            acceptance_score=95,
        )

        result = qualify_opportunity(candidate, opportunity)

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason_code, "target_area_too_broad")

    def test_accepts_maintainer_signaled_feature_upgrade(self) -> None:
        candidate = _candidate(
            {
                "cli.py": "# TODO: add --format json support for report output\n\ndef run():\n    return 1\n",
                "tests/test_cli.py": "def test_cli():\n    assert True\n",
            }
        )
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="cli.py",
            pattern_type="maintainer_todo_feature_upgrade",
            failure_mode="The code documents an intended operator-visible capability that is still missing, so users cannot access the behavior the maintainers already signaled should exist.",
            evidence="Line 1 contains a TODO asking for --format json support in this command path.",
            patch_scope=1,
            test_target="tests/test_cli.py",
            acceptance_score=82,
            opportunity_kind="feature_upgrade",
            source_ref="code_comment:cli.py:1",
            maintainer_intent=True,
        )

        result = qualify_opportunity(candidate, opportunity)

        self.assertTrue(result.accepted)
        self.assertGreaterEqual(result.score, 82)

    def test_rejects_test_target_that_ignores_repo_test_layout(self) -> None:
        candidate = _candidate(
            {
                "agent/backtest/validation.py": "def main():\n    return 1\n",
                "agent/tests/test_validation.py": "def test_validation():\n    assert True\n",
            }
        )
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="agent/backtest/validation.py",
            pattern_type="missing_input_validation",
            failure_mode="A missing or malformed CLI argument can raise a low-level error instead of a clear operator-facing message.",
            evidence="The module reads argv directly and the repo keeps its focused tests under agent/tests.",
            patch_scope=1,
            test_target="tests/test_validation.py",
            acceptance_score=84,
        )

        result = qualify_opportunity(candidate, opportunity)

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason_code, "invalid_test_target_layout")

    def test_rejects_feature_add_without_issue_signal(self) -> None:
        candidate = _candidate({"cli.py": "def run():\n    return 1\n"})
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="cli.py",
            pattern_type="issue_backed_feature_add",
            failure_mode="An open maintainer-labeled enhancement request identifies a missing capability in this target area, and the repository does not yet expose that behavior.",
            evidence="Feature request exists somewhere, but source is missing.",
            patch_scope=1,
            test_target="",
            acceptance_score=90,
            opportunity_kind="feature_add",
            source_ref="code_comment:cli.py:1",
            maintainer_intent=True,
        )

        result = qualify_opportunity(candidate, opportunity)

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason_code, "feature_add_requires_issue")


class PREngineStoreTests(unittest.TestCase):
    def _make_store(self) -> tuple[PREngineStore, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "engine.sqlite3"
        return PREngineStore(db_path), db_path

    def test_persists_state_transitions_and_rejections(self) -> None:
        store, db_path = self._make_store()
        run_id = store.start_run("search", 1)
        candidate = _candidate({"client.py": "print('ok')\n"})
        store.upsert_repo_profile(candidate, 75)
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="client.py",
            pattern_type="missing_timeout",
            failure_mode="A slow upstream endpoint can block the command forever instead of failing fast on a valid request path.",
            evidence="The only HTTP call omits timeout=.",
            patch_scope=1,
            test_target="",
            acceptance_score=80,
        )
        opportunity_id = store.create_opportunity(run_id, opportunity)
        store.transition_opportunity(opportunity_id, "QUALIFY", why_advanced="Qualified narrowly.")
        store.reject_opportunity(run_id, opportunity, "low_acceptance_score", "Rejected for test.", "QUALIFY", opportunity_id)

        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute("SELECT state, why_advanced, why_rejected FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
            rejection = conn.execute("SELECT reason_code FROM rejections WHERE opportunity_id = ?", (opportunity_id,)).fetchone()

        self.assertEqual(row[0], "REJECT")
        self.assertEqual(row[1], "Qualified narrowly.")
        self.assertEqual(row[2], "Rejected for test.")
        self.assertEqual(rejection[0], "low_acceptance_score")

    def test_repeated_rejections_trigger_repo_cooldown(self) -> None:
        store, db_path = self._make_store()
        run_id = store.start_run("search", 1)
        candidate = _candidate({"client.py": "print('ok')\n"})
        store.upsert_repo_profile(candidate, 70)
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="client.py",
            pattern_type="missing_timeout",
            failure_mode="A slow upstream endpoint can block the command forever instead of failing fast on a valid request path.",
            evidence="The only HTTP call omits timeout=.",
            patch_scope=1,
            test_target="",
            acceptance_score=70,
        )

        for _ in range(2):
            opportunity_id = store.create_opportunity(run_id, opportunity)
            store.reject_opportunity(run_id, opportunity, "evidence_too_weak", "Too weak.", "QUALIFY", opportunity_id)

        with closing(sqlite3.connect(db_path)) as conn:
            cooldown = conn.execute("SELECT cooldown_until FROM repos WHERE full_name = ?", (candidate.full_name,)).fetchone()[0]

        self.assertTrue(cooldown)


class OperatorExperienceTests(unittest.TestCase):
    def _make_store(self) -> tuple[PREngineStore, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "engine.sqlite3"
        return PREngineStore(db_path), db_path

    def test_operator_report_includes_next_step_guidance(self) -> None:
        store, _db_path = self._make_store()
        engine = ContributionEngine(store)
        run_id = store.start_run("search", 1)
        summary = {
            "run_id": run_id,
            "submitted": 0,
            "target": 1,
            "attempts": 2,
            "ai_calls": 2,
            "state_counts": {"REJECT": 1},
            "top_rejections": [("evidence_too_weak", 1)],
            "queued": [],
            "submitted_prs": [],
            "bottleneck": "0 PR because evidence_too_weak dominated 1 opportunity decisions",
            "discovered": 1,
        }
        store.finish_run(run_id, summary)

        report = engine.build_operator_report(limit=3)

        self.assertIn("Contribution Engine Report", report)
        self.assertIn("Suggested next step:", report)
        self.assertIn("evidence_too_weak", report)

    def test_repo_inspect_report_surfaces_scope_and_next_action(self) -> None:
        candidate = _candidate(
            {
                "src/app.py": "def run():\n    return 1\n",
                "tests/test_app.py": "def test_run():\n    assert True\n",
            },
            full_name="example/tooling-repo",
        )

        report = build_repo_inspect_report(candidate)

        self.assertIn("Repo Inspect", report)
        self.assertIn("Scope fit:", report)
        self.assertIn("--contrib example/tooling-repo --1", report)

    def test_doctor_report_includes_summary_and_readiness(self) -> None:
        report = build_doctor_report()

        self.assertIn("Contribution Engine Doctor", report)
        self.assertIn("Summary:", report)
        self.assertIn("Operator readiness:", report)
        self.assertIn("Support matrix:", report)

    def test_queue_and_open_pr_pacing(self) -> None:
        store, _ = self._make_store()
        candidate = _candidate({"client.py": "print('ok')\n"})
        store.upsert_repo_profile(candidate, 75)
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="client.py",
            pattern_type="missing_timeout",
            failure_mode="A slow upstream endpoint can block the command forever instead of failing fast on a valid request path.",
            evidence="The only HTTP call omits timeout=.",
            patch_scope=1,
            test_target="",
            acceptance_score=80,
        )
        opportunity_id = store.create_opportunity(1, opportunity)
        store.record_pull_request(opportunity_id, candidate.full_name, "https://github.com/example/repo/pull/1", "fix: add timeout", "currentuser/repo", "branch", "bug_fix")

        self.assertTrue(store.has_open_pr(candidate.full_name))

    def test_run_summary_reports_bottleneck(self) -> None:
        store, _ = self._make_store()
        run_id = store.start_run("search", 1)
        candidate = _candidate({"client.py": "print('ok')\n"})
        store.record_repo_event(run_id, candidate.full_name, "discover_selected", "Selected candidate.")
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="client.py",
            pattern_type="missing_timeout",
            failure_mode="A slow upstream endpoint can block the command forever instead of failing fast on a valid request path.",
            evidence="The only HTTP call omits timeout=.",
            patch_scope=1,
            test_target="",
            acceptance_score=80,
        )
        opportunity_id = store.create_opportunity(run_id, opportunity)
        store.reject_opportunity(run_id, opportunity, "self_review_rejected", "Behavior changed.", "VERIFY", opportunity_id)

        summary = store.summarize_run(run_id)

        self.assertEqual(summary["discovered"], 1)
        self.assertIn(("self_review_rejected", 1), summary["top_rejections"])
        self.assertIn("self_review_rejected", summary["bottleneck"])

    def test_run_summary_bottleneck_mentions_submitted_prs_when_present(self) -> None:
        store, _ = self._make_store()
        run_id = store.start_run("search", 1)
        candidate = _candidate({"client.py": "print('ok')\n"})
        store.record_repo_event(run_id, candidate.full_name, "discover_selected", "Selected candidate.")
        submitted = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="client.py",
            pattern_type="missing_timeout",
            failure_mode="A slow upstream endpoint can block the command forever instead of failing fast on a valid request path.",
            evidence="The only HTTP call omits timeout=.",
            patch_scope=1,
            test_target="",
            acceptance_score=90,
        )
        rejected = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="core.py",
            pattern_type="missing_input_validation",
            failure_mode="An invalid env value can crash the command with an opaque error instead of surfacing a helpful message.",
            evidence="The code converts env input directly with int() and does not validate the value first.",
            patch_scope=1,
            test_target="",
            acceptance_score=82,
        )
        submitted_id = store.create_opportunity(run_id, submitted)
        rejected_id = store.create_opportunity(run_id, rejected)
        store.record_pull_request(
            submitted_id,
            candidate.full_name,
            "https://github.com/example/repo/pull/2",
            "fix: add timeout",
            "example/repo",
            "branch",
            "bug_fix",
        )
        store.reject_opportunity(run_id, rejected, "target_area_too_broad", "Core file too broad.", "QUALIFY", rejected_id)

        summary = store.summarize_run(run_id)

        self.assertIn("1 PR submitted", summary["bottleneck"])
        self.assertIn("target_area_too_broad", summary["bottleneck"])

    def test_contribution_report_includes_recent_runs_and_ready_queue(self) -> None:
        store, _ = self._make_store()
        engine = ContributionEngine(store)
        run_id = engine.start_run("search", 1)
        candidate = _candidate({"client.py": "print('ok')\n"})
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="client.py",
            pattern_type="missing_timeout",
            failure_mode="A slow upstream endpoint can block the command forever instead of failing fast on a valid request path.",
            evidence="The only HTTP call omits timeout=.",
            patch_scope=1,
            test_target="",
            acceptance_score=80,
            state="READY",
        )
        opportunity_id = store.create_opportunity(run_id, opportunity)
        store.transition_opportunity(opportunity_id, "READY", "Ready but waiting on pacing.")
        engine.finish_run(submitted=0, target=1, attempts=1, usage={"calls": 0, "est_tokens": 0}, log=__import__("logging").getLogger("test"))

        report = engine.build_operator_report()

        self.assertIn("Contribution Engine Report", report)
        self.assertIn("Recent runs", report)
        self.assertIn("Ready queue", report)
        self.assertIn("missing_timeout", report)


if __name__ == "__main__":
    unittest.main()
