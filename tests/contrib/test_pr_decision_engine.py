from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from src.contrib.contribution_engine import ContributionEngine
from src.core.doctor import ROOT, _codex_auth_ready, _command_exists, _entrypoint_check, _notification_route_check, build_doctor_report
from src.contrib.pr_engine import Opportunity, PREngineStore, PatternScanner, qualify_opportunity
from src.contrib.pr_generator import build_repo_inspect_report
from src.contrib.opportunity_engine import guess_test_target
from src.github.scraper import RepoCandidate


def _candidate(
    files: dict[str, str], full_name: str = "example/repo", **overrides: object
) -> RepoCandidate:
    base = dict(
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
    base.update(overrides)
    return RepoCandidate(**base)


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

    def test_excluded_patterns_skips_temp_file_cleanup_gap_by_exact_type_name(self) -> None:
        candidate = _candidate(
            {
                "worker.py": "import tempfile\n\ndef run():\n    path = tempfile.mkdtemp()\n    return path\n",
                "tests/test_worker.py": "def test_run():\n    assert True\n",
            }
        )
        all_patterns = {opp.pattern_type for opp in self.scanner.scan(candidate)}
        self.assertIn("temp_file_cleanup_gap", all_patterns)

        excluded = {opp.pattern_type for opp in self.scanner.scan(candidate, excluded_patterns={"temp_file_cleanup_gap"})}
        self.assertNotIn("temp_file_cleanup_gap", excluded)

    def test_excluded_patterns_skips_maintainer_todo_feature_upgrade_by_exact_type_name(self) -> None:
        candidate = _candidate(
            {
                "cli.py": "# TODO: add --format json support for output\n\ndef run():\n    return 1\n",
                "tests/test_cli.py": "def test_run():\n    assert True\n",
            }
        )
        all_patterns = {opp.pattern_type for opp in self.scanner.scan(candidate)}
        self.assertIn("maintainer_todo_feature_upgrade", all_patterns)

        excluded = {opp.pattern_type for opp in self.scanner.scan(candidate, excluded_patterns={"maintainer_todo_feature_upgrade"})}
        self.assertNotIn("maintainer_todo_feature_upgrade", excluded)

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

    def test_accepts_localized_fix_in_large_file(self) -> None:
        filler = "\n".join(f"CONST_{i} = {i}" for i in range(450))
        big = filler + "\n\ndef fetch():\n    import requests\n    return requests.get('https://api.example.com').json()\n"
        candidate = _candidate({"client_big.py": big})
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="client_big.py",
            pattern_type="missing_timeout",
            failure_mode="A slow upstream endpoint can block the command forever instead of failing fast on a valid request path.",
            evidence="The only HTTP call omits timeout= and sits in a small fetch() helper.",
            patch_scope=1,
            test_target="",
            acceptance_score=99,
            evidence_lines=[454],
        )

        result = qualify_opportunity(candidate, opportunity)

        # File is >400 lines, but the fix lives in a 3-line function — narrow.
        self.assertTrue(result.accepted)

    def test_rejects_localized_fix_when_file_exceeds_hard_cap(self) -> None:
        filler = "\n".join(f"CONST_{i} = {i}" for i in range(1300))
        huge = filler + "\n\ndef fetch():\n    import requests\n    return requests.get('https://api.example.com').json()\n"
        candidate = _candidate({"client_huge.py": huge})
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="client_huge.py",
            pattern_type="missing_timeout",
            failure_mode="A slow upstream endpoint can block the command forever instead of failing fast on a valid request path.",
            evidence="The only HTTP call omits timeout= and sits in a small fetch() helper.",
            patch_scope=1,
            test_target="",
            acceptance_score=99,
            evidence_lines=[1304],
        )

        result = qualify_opportunity(candidate, opportunity)

        # Localized, but the file is past the hard size cap — keep the rejection.
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

    def test_repo_reads_survive_corrupt_json_columns(self) -> None:
        # Regression: a corrupt repos-table JSON column crashed every subsequent
        # repo_live_fit / repo_score_adjustment read (shortlisting + inspect).
        store, db_path = self._make_store()
        candidate = _candidate({"client.py": "print('ok')\n"})
        store.upsert_repo_profile(candidate, 75)
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                """
                UPDATE repos SET repo_profile_json = 'not-json',
                    responsiveness_profile_json = '{corrupt',
                    pattern_history_json = '[1,2]'
                WHERE full_name = ?
                """,
                (candidate.full_name,),
            )
            conn.commit()

        fit = store.repo_live_fit(candidate.full_name)
        self.assertIn("state", fit)
        self.assertEqual(store.repo_score_adjustment(candidate.full_name), 0)

    def test_feedback_and_status_writes_survive_corrupt_json_columns(self) -> None:
        store, db_path = self._make_store()
        candidate = _candidate({"client.py": "print('ok')\n"})
        store.upsert_repo_profile(candidate, 75)
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "UPDATE repos SET responsiveness_profile_json = '{corrupt', pattern_history_json = 'not-json' WHERE full_name = ?",
                (candidate.full_name,),
            )
            conn.commit()

        store.record_feedback_signal(candidate.full_name, "maintainer_feedback")

        with closing(sqlite3.connect(db_path)) as conn:
            raw = conn.execute(
                "SELECT responsiveness_profile_json FROM repos WHERE full_name = ?",
                (candidate.full_name,),
            ).fetchone()[0]
        self.assertEqual(json.loads(raw).get("last_signal"), "maintainer_feedback")


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

    def test_repo_inspect_report_marks_archived_repo_as_inspect_only(self) -> None:
        candidate = _candidate(
            {"src/app.py": "def run():\n    return 1\n"},
            full_name="example/archived-repo",
            archived=True,
            pushed_days_ago=517,
        )

        report = build_repo_inspect_report(candidate)

        self.assertIn("Scope fit: search=inspect-only | targeted=inspect-only", report)
        self.assertIn("repo is archived on GitHub", report)
        self.assertIn("Keep this repo in inspect-only mode.", report)
        self.assertNotIn("--contrib example/archived-repo --1", report)

    def test_doctor_report_includes_summary_and_readiness(self) -> None:
        report = build_doctor_report()

        self.assertIn("Contribution Engine Doctor", report)
        self.assertIn("Summary:", report)
        self.assertIn("Operator readiness:", report)
        self.assertIn("Support matrix:", report)
        self.assertIn("OpenClaw integration note:", report)
        self.assertIn("storage-mode", report)
        self.assertIn("entrypoint", report)
        self.assertIn("notify-route", report)
        self.assertIn("notify-transport", report)

    def test_notification_route_check_reports_openclaw_transport(self) -> None:
        with mock.patch("src.core.doctor.ROVER_NOTIFY_TRANSPORT", "openclaw"), mock.patch(
            "src.core.doctor.OPENCLAW_NOTIFY_CHANNEL", "telegram"
        ), mock.patch("src.core.doctor.OPENCLAW_NOTIFY_TARGET", "-100123"), mock.patch(
            "src.core.doctor.OPENCLAW_NOTIFY_ACCOUNT", "default"
        ), mock.patch("src.core.doctor.OPENCLAW_NOTIFY_THREAD_ID", "7"), mock.patch(
            "src.core.doctor.ROVER_NOTIFY_INTERVAL_SECONDS", 45
        ), mock.patch("src.core.doctor.ROVER_NOTIFY_PROGRESS", True):
            check = _notification_route_check()

        self.assertEqual(check.status, "ok")
        self.assertIn("transport=openclaw", check.detail)
        self.assertIn("target=-100123", check.detail)
        self.assertIn("interval=45s", check.detail)
        self.assertIn("stall=", check.detail)

    def test_entrypoint_check_accepts_project_venv_wrapper_even_if_python_differs(self) -> None:
        argv0 = ROOT / ".venv" / "bin" / "rover"
        rover_on_path = str(argv0)
        with mock.patch("src.core.doctor.sys.argv", [str(argv0)]), mock.patch(
            "src.core.doctor.sys.executable", "/usr/bin/python3.10"
        ), mock.patch("src.core.doctor.shutil.which", return_value=rover_on_path):
            check = _entrypoint_check()

        self.assertEqual(check.status, "ok")
        self.assertIn("argv0=", check.detail)
        self.assertIn("rover=", check.detail)

    def test_codex_auth_ready_sanitizes_internal_cli_error(self) -> None:
        with mock.patch("src.core.doctor.os.getenv", return_value=""), mock.patch(
            "src.core.doctor._command_exists", return_value=True
        ), mock.patch(
            "src.core.doctor._run_command",
            return_value=(
                False,
                "file:///mnt/c/Users/USER/AppData/Roaming/npm/node_modules/@openai/codex/bin/codex.js:100",
            ),
        ):
            ok, detail = _codex_auth_ready()

        self.assertFalse(ok)
        self.assertEqual(
            detail,
            "Codex CLI returned an unexpected auth error; run `codex login status` manually",
        )

    def test_command_exists_ignores_permission_errors(self) -> None:
        with mock.patch("src.core.doctor.Path.exists", side_effect=PermissionError), mock.patch(
            "src.core.doctor.shutil.which", return_value=None
        ):
            self.assertFalse(_command_exists(r"\\mnt\\c\\Users\\USER\\AppData\\Roaming\\npm\\codex"))

    def test_command_exists_rejects_windows_mounted_cli_path_on_posix(self) -> None:
        self.assertFalse(_command_exists("/mnt/c/Users/USER/AppData/Roaming/npm/codex"))

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

    def test_open_pr_checks_are_partitioned_by_owner_login(self) -> None:
        store, _ = self._make_store()
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
        )
        opportunity_id = store.create_opportunity(1, opportunity)
        store.record_pull_request(
            opportunity_id,
            candidate.full_name,
            "https://github.com/example/repo/pull/1",
            "fix: add timeout",
            "currentuser/repo",
            "branch",
            "bug_fix",
            owner_login="currentuser",
        )

        self.assertTrue(store.has_open_pr(candidate.full_name, owner_login="currentuser"))
        self.assertFalse(store.has_open_pr(candidate.full_name, owner_login="otheruser"))
        found = store.find_open_pr(candidate.full_name, owner_login="currentuser")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["owner_login"], "currentuser")

    def test_find_open_pr_falls_back_to_legacy_pr_log(self) -> None:
        store, _ = self._make_store()
        candidate = _candidate({"client.py": "print('ok')\n"})
        legacy_payload = {
            "submitted": [
                {
                    "full_name": candidate.full_name,
                    "pr_url": "https://github.com/example/repo/pull/264",
                    "pr_title": "fix: validate config env inputs",
                    "status": "open",
                    "submitted_at": "2026-05-05T10:00:00",
                    "fork_name": "me/repo",
                    "branch_name": "me-patch-1",
                    "improvement_type": "error_handling",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmp:
            legacy_path = Path(tmp) / "pr_log.json"
            legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")
            with mock.patch("src.contrib.contribution_store._legacy_pr_log_candidates", return_value=[legacy_path]):
                found = store.find_open_pr(candidate.full_name)
                self.assertTrue(store.has_open_pr(candidate.full_name))

        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["pr_url"], "https://github.com/example/repo/pull/264")

    def test_find_open_pr_legacy_fallback_filters_by_owner_login(self) -> None:
        store, _ = self._make_store()
        candidate = _candidate({"client.py": "print('ok')\n"})
        legacy_payload = {
            "submitted": [
                {
                    "full_name": candidate.full_name,
                    "owner_login": "otheruser",
                    "pr_url": "https://github.com/example/repo/pull/264",
                    "pr_title": "fix: validate config env inputs",
                    "status": "open",
                    "submitted_at": "2026-05-05T10:00:00",
                    "fork_name": "otheruser/repo",
                    "branch_name": "otheruser-patch-1",
                    "improvement_type": "error_handling",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmp:
            legacy_path = Path(tmp) / "pr_log.json"
            legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")
            with mock.patch("src.contrib.contribution_store._legacy_pr_log_candidates", return_value=[legacy_path]):
                found = store.find_open_pr(candidate.full_name, owner_login="currentuser")

        self.assertIsNone(found)

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
        self.assertEqual(summary["broad_rejected_early"], 1)
        self.assertIn("shortlisted", summary)
        self.assertIn("token_spend_by_stage", summary)

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

    def test_contribution_report_dedupes_ready_queue_duplicates(self) -> None:
        store, _ = self._make_store()
        engine = ContributionEngine(store)
        run_id = engine.start_run("search", 1)
        candidate = _candidate({"client.py": "print('ok')\n"})
        for score in (80, 79, 78):
            opportunity = Opportunity(
                repo_full_name=candidate.full_name,
                target_file="client.py",
                pattern_type="missing_timeout",
                failure_mode="A slow upstream endpoint can block the command forever instead of failing fast on a valid request path.",
                evidence="The only HTTP call omits timeout=.",
                patch_scope=1,
                test_target="",
                acceptance_score=score,
                state="READY",
            )
            opportunity_id = store.create_opportunity(run_id, opportunity)
            store.transition_opportunity(opportunity_id, "READY", "Ready but waiting on pacing.")
        engine.finish_run(submitted=0, target=1, attempts=1, usage={"calls": 0, "est_tokens": 0}, log=__import__("logging").getLogger("test"))

        report = engine.build_operator_report()

        self.assertEqual(report.count("missing_timeout"), 1)
        self.assertIn("Ready queue (1 shown)", report)

    def test_benchmark_suite_tracks_submit_rate_per_token(self) -> None:
        store, _ = self._make_store()
        engine = ContributionEngine(store)
        engine.active_run_id = store.start_run("targeted", 1)
        engine.finish_run(
            submitted=1,
            target=1,
            attempts=2,
            usage={"calls": 4, "est_tokens": 2000},
            log=__import__("logging").getLogger("test"),
            extra_summary={
                "generated": 2,
                "discovered": 5,
                "self_review_rejected": 1,
                "broad_rejected_early": 2,
                "shape_rejected_early": 1,
            },
        )

        metrics = engine.benchmark_suite()

        self.assertEqual(metrics["submitted_prs_per_est_token"], 0.0005)
        self.assertEqual(metrics["tokens_per_submitted_pr"], 2000)
        self.assertEqual(metrics["ai_calls_per_submitted_pr"], 4)
        self.assertEqual(metrics["late_reject_ratio"], 0.5)
        self.assertEqual(metrics["early_reject_ratio"], 0.6)
        self.assertEqual(metrics["repos"][0]["repo"], "GPT-AGI/Clawd-Code")

    def test_finish_run_adds_operator_outcome_taxonomy(self) -> None:
        store, _ = self._make_store()
        engine = ContributionEngine(store)
        engine.active_run_id = store.start_run("targeted", 1)

        summary = engine.finish_run(
            submitted=0,
            target=1,
            attempts=1,
            usage={"calls": 0, "est_tokens": 0},
            log=__import__("logging").getLogger("test"),
            extra_summary={
                "current_stage": "qualify",
                "shortlisted": 0,
            },
        )

        self.assertEqual(summary["death_stage"], "qualify")
        self.assertEqual(summary["outcome_code"], "no_narrow_candidate")

    def test_repo_live_fit_uses_patch_shape_memory(self) -> None:
        store, _ = self._make_store()
        candidate = _candidate(
            {
                "client.py": "print('ok')\n",
                "tests/test_client.py": "def test_client():\n    assert True\n",
            }
        )
        store.upsert_repo_profile(candidate, 90)
        run_id = store.start_run("targeted", 1)
        submitted = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="client.py",
            pattern_type="missing_timeout",
            failure_mode="A slow upstream endpoint can block forever.",
            evidence="requests.get omits timeout.",
            patch_scope=1,
            test_target="tests/test_client.py",
            acceptance_score=90,
        )
        submitted_id = store.create_opportunity(run_id, submitted)
        store.record_pull_request(
            submitted_id,
            candidate.full_name,
            "https://github.com/example/repo/pull/3",
            "fix: add timeout",
            "example/repo",
            "branch",
            "bug_fix",
        )

        fit = store.repo_live_fit(candidate.full_name)

        self.assertEqual(fit["state"], "live-targeted-ready")
        self.assertGreaterEqual(fit["score"], 70)
        self.assertIn("prior accepted patch shape", fit["reasons"])

    def test_repo_live_fit_uses_scan_signals_for_ranking(self) -> None:
        store, _ = self._make_store()
        candidate = _candidate(
            {
                "README.md": "disable antivirus before running\n",
            },
            full_name="example/risky",
        )
        store.upsert_repo_profile(candidate, 30)
        store.record_scan_summary(
            candidate.full_name,
            kind="trust",
            severity_counts={"high": 2, "medium": 0, "low": 0},
            finding_kind_counts={"trust": 2, "security": 0, "bug": 0},
            supported_file_count=0,
        )

        fit = store.repo_live_fit(candidate.full_name)

        self.assertEqual(fit["state"], "inspect-only")
        self.assertIn("trust scan high-risk findings", fit["reasons"])
        self.assertIn("trust scan has low source coverage", fit["reasons"])

    def test_same_pattern_recent_rejections_blocks_bad_retry_family(self) -> None:
        store, _ = self._make_store()
        candidate = _candidate({"context_system/router.py": "def route():\n    return None\n"})
        run_id = store.start_run("targeted", 1)
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="context_system/router.py",
            pattern_type="overbroad_exception_handling",
            failure_mode="An exception is swallowed in a behavior-routing path.",
            evidence="except Exception returns None.",
            patch_scope=1,
            test_target="tests/test_router.py",
            acceptance_score=75,
        )
        opportunity_id = store.create_opportunity(run_id, opportunity)
        store.reject_opportunity(
            run_id,
            opportunity,
            "patch_shape_high_risk",
            "context_system patches require manual review.",
            "VERIFY",
            opportunity_id,
        )

        self.assertEqual(
            store.same_pattern_recent_rejections(candidate.full_name, "overbroad_exception_handling"),
            1,
        )
        self.assertEqual(
            store.same_pattern_recent_rejections(
                candidate.full_name,
                "overbroad_exception_handling",
                reason_code="patch_shape_high_risk",
            ),
            1,
        )

    def test_pattern_memory_records_shape_and_feedback_details(self) -> None:
        store, _ = self._make_store()
        candidate = _candidate({"src/client.py": "print('ok')\n", "tests/test_client.py": "def test_client():\n    assert True\n"})
        store.upsert_repo_profile(candidate, 90)
        run_id = store.start_run("targeted", 1)
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="src/client.py",
            pattern_type="missing_timeout",
            failure_mode="A slow upstream endpoint can block forever.",
            evidence="requests.get omits timeout.",
            patch_scope=1,
            test_target="tests/test_client.py",
            acceptance_score=90,
        )
        opportunity_id = store.create_opportunity(run_id, opportunity)
        store.reject_opportunity(
            run_id,
            opportunity,
            "self_review_rejected",
            "Valid input behavior changed.",
            "VERIFY",
            opportunity_id,
        )
        store.record_pull_request(
            opportunity_id,
            candidate.full_name,
            "https://github.com/example/repo/pull/4",
            "fix: add timeout",
            "example/repo",
            "branch",
            "bug_fix",
        )
        store.update_pr_status(
            "https://github.com/example/repo/pull/4",
            "closed",
            maintainer_signal="too broad for this module",
        )

        with store._connect() as conn:
            row = conn.execute(
                "SELECT pattern_history_json FROM repos WHERE full_name = ?",
                (candidate.full_name,),
            ).fetchone()
        history = json.loads(row["pattern_history_json"])
        stats = history["missing_timeout"]

        self.assertEqual(stats["target_dirs"]["src"], 3)
        self.assertEqual(stats["target_files"]["src/client.py"], 3)
        self.assertGreaterEqual(stats["had_test_target"]["yes"], 3)
        self.assertEqual(stats["self_review_reasons"]["Valid input behavior changed."], 1)
        self.assertEqual(stats["closed_without_merge"], 1)
        self.assertEqual(stats["maintainer_feedback_shapes"]["too broad for this module"], 1)


if __name__ == "__main__":
    unittest.main()
