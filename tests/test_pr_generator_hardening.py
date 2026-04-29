from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.contribution_store import PREngineStore
from src.pr_generator import (
    PRGeneratorError,
    Opportunity,
    _acceptance_score,
    _check_pr_evidence_quality,
    _delete_fork,
    _first_pr_repo_fit,
    _get_lane_preset,
    _matches_contribution_lane,
    _normalize_queries,
    _self_review_test_layout,
    _validate_candidate_scope,
    check_pr_statuses,
    fetch_repo_candidate_with_scope,
    generate_dep_update,
    generate_pr_improvement,
    save_pr_log,
)
from src.scraper import RepoCandidate


class PRGeneratorHardeningTests(unittest.TestCase):
    def _make_store(self) -> PREngineStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "engine.sqlite3"
        return PREngineStore(db_path)

    def test_lane_match_defaults_open_when_no_keywords_configured(self) -> None:
        candidate = RepoCandidate(
            name="plain-tool",
            full_name="example/plain-tool",
            description="A general developer tool",
            stars=10,
            forks=2,
            license="MIT",
            url="https://github.com/example/plain-tool",
            default_branch="main",
            pushed_days_ago=1,
            topics=["python"],
            files={},
        )

        with patch("src.pr_generator._LANE_KEYWORDS", set()):
            self.assertTrue(_matches_contribution_lane(candidate))

    def test_lane_preset_contains_expected_frontend_keywords(self) -> None:
        preset = _get_lane_preset("frontend")

        self.assertIn("react", preset["keywords"])
        self.assertTrue(any(lang == "typescript" for lang, _query in preset["queries"]))

    def test_normalize_queries_parses_explicit_and_implicit_languages(self) -> None:
        queries = _normalize_queries([
            "python:python observability library",
            "typescript:react component library",
            "plain custom query",
        ])

        self.assertEqual(
            queries,
            [
                ("python", "python observability library"),
                ("typescript", "react component library"),
                ("python", "plain custom query"),
            ],
        )

    def test_lane_match_respects_configured_keywords(self) -> None:
        matching = RepoCandidate(
            name="api-helper",
            full_name="example/api-helper",
            description="CLI helper for API integrations",
            stars=10,
            forks=2,
            license="MIT",
            url="https://github.com/example/api-helper",
            default_branch="main",
            pushed_days_ago=1,
            topics=["python", "tooling"],
            files={},
        )
        non_matching = RepoCandidate(
            name="image-gallery",
            full_name="example/image-gallery",
            description="Photo gallery site",
            stars=10,
            forks=2,
            license="MIT",
            url="https://github.com/example/image-gallery",
            default_branch="main",
            pushed_days_ago=1,
            topics=["frontend"],
            files={},
        )

        with patch("src.pr_generator._LANE_KEYWORDS", {"api", "cli"}):
            self.assertTrue(_matches_contribution_lane(matching))
            self.assertFalse(_matches_contribution_lane(non_matching))

    def test_targeted_scope_rejects_capped_partial_repo(self) -> None:
        candidate = RepoCandidate(
            name="large-repo",
            full_name="example/large-repo",
            description="Large repository",
            stars=1000,
            forks=100,
            license="MIT",
            url="https://github.com/example/large-repo",
            default_branch="main",
            pushed_days_ago=1,
            topics=["python"],
            files={**{f"pkg/mod{i}.py": "def run():\n    return 1\n" for i in range(120)}},
        )

        with patch("src.pr_generator._MAX_REPO_FILES", 120), \
             patch("src.pr_generator._PR_TARGETED_ALLOW_BROAD", False):
            with self.assertRaisesRegex(Exception, "too broad|partially inspected"):
                _validate_candidate_scope(candidate, targeted=True)

    def test_targeted_scope_allows_small_repo(self) -> None:
        candidate = RepoCandidate(
            name="small-repo",
            full_name="example/small-repo",
            description="Small repository",
            stars=100,
            forks=20,
            license="MIT",
            url="https://github.com/example/small-repo",
            default_branch="main",
            pushed_days_ago=1,
            topics=["python"],
            files={
                "app.py": "def run():\n    return 1\n",
                "tests/test_app.py": "def test_run():\n    assert True\n",
            },
        )

        py_count, ts_count, test_count = _validate_candidate_scope(candidate, targeted=True)

        self.assertEqual((py_count, ts_count, test_count), (2, 0, 1))

    def test_targeted_scope_can_bypass_breadth_when_enabled(self) -> None:
        candidate = RepoCandidate(
            name="large-repo",
            full_name="example/large-repo",
            description="Large repository",
            stars=1000,
            forks=100,
            license="MIT",
            url="https://github.com/example/large-repo",
            default_branch="main",
            pushed_days_ago=1,
            topics=["python"],
            files={**{f"pkg/mod{i}.py": "def run():\n    return 1\n" for i in range(120)}},
        )

        with patch("src.pr_generator._PR_TARGETED_ALLOW_BROAD", True):
            py_count, ts_count, test_count = _validate_candidate_scope(candidate, targeted=True)

        self.assertEqual((py_count, ts_count, test_count), (120, 0, 0))

    def test_fetch_repo_candidate_with_scope_allows_inspect_on_large_repo(self) -> None:
        payload = {
            "name": "large-repo",
            "full_name": "example/large-repo",
            "description": "Large repository",
            "stargazers_count": 1000,
            "forks_count": 100,
            "license": {"spdx_id": "MIT"},
            "html_url": "https://github.com/example/large-repo",
            "default_branch": "main",
            "pushed_at": "2026-04-29T00:00:00Z",
            "topics": ["python"],
        }
        files = {f"pkg/mod{i}.py": "def run():\n    return 1\n" for i in range(120)}

        with patch("src.pr_generator._gh_get", return_value=payload), \
             patch("src.pr_generator.download_repo_files", return_value=files):
            candidate = fetch_repo_candidate_with_scope(
                "https://github.com/example/large-repo",
                logging.getLogger("test"),
                enforce_scope=False,
            )

        self.assertEqual(candidate.full_name, "example/large-repo")
        self.assertEqual(len(candidate.files), 120)

    def test_rejects_speculative_bugfix_without_tests_or_proof(self) -> None:
        result = {
            "improvement_type": "bug_fix",
            "pr_body": (
                "## Summary\n"
                "This change prevents between_pct() from sending a trailing None.\n"
                "## Why it matters\n"
                "Sending None can produce invalid or ambiguous API requests and makes the payload safer and more consistent.\n"
                "## Testing\n"
                "Verified by constructing filters with and without pct2 and checking the returned dictionaries."
            ),
            "rationale": "Prevents ambiguous API payloads for single-bound percentage filters.",
            "safety_proof": "The change is defensive rather than a fix to observed breakage.",
        }
        changed_files = {
            "src/tradingview_screener/column.py": "def between_pct():\n    return []\n",
        }

        rejection = _check_pr_evidence_quality(result, changed_files)

        self.assertIsNotNone(rejection)
        self.assertIn("speculative/defensive", rejection)

    def test_allows_bugfix_with_test_change_and_concrete_failure(self) -> None:
        result = {
            "improvement_type": "bug_fix",
            "pr_body": (
                "## Summary\n"
                "This change handles None before indexing the RPC response.\n"
                "## Why it matters\n"
                "A missing quote currently raises IndexError and crashes the request path.\n"
                "## Testing\n"
                "Added a regression test covering the missing-quote response and verified the handler now returns an empty result."
            ),
            "rationale": "Prevents IndexError when the upstream RPC omits quote data.",
            "safety_proof": "The fix only adds an early return for the previously crashing None response path.",
        }
        changed_files = {
            "src/example/quotes.py": "def parse_quote():\n    return None\n",
            "tests/test_quotes.py": "def test_missing_quote():\n    assert True\n",
        }

        rejection = _check_pr_evidence_quality(result, changed_files)

        self.assertIsNone(rejection)

    def test_delete_fork_skips_target_repo_itself(self) -> None:
        entry = {
            "full_name": "BigNounce90/wallet-mcp",
            "fork_name": "BigNounce90/wallet-mcp",
        }

        with patch("src.fork.get_current_github_login", return_value="BigNounce90"), \
             patch("subprocess.run") as mocked_run:
            _delete_fork(entry, logging.getLogger("test"))

        mocked_run.assert_not_called()
        self.assertFalse(entry["fork_deleted"])

    def test_check_pr_statuses_cleans_up_resolved_entries_missing_fork_deleted(self) -> None:
        data = {
            "submitted": [
                {
                    "full_name": "example/upstream-repo",
                    "pr_url": "https://github.com/example/upstream-repo/pull/10",
                    "pr_title": "fix: sample",
                    "fork_name": "currentuser/upstream-repo",
                    "branch_name": "currentuser-patch-1",
                    "files_changed": ["src/foo.py"],
                    "submitted_at": "2026-04-28T10:00:00",
                    "status": "closed",
                    "notified_merge": True,
                }
            ]
        }

        with patch("src.pr_generator.load_pr_log", return_value=data), \
             patch("src.fork.get_current_github_login", return_value="currentuser"), \
             patch("pathlib.Path.write_text") as mocked_write, \
             patch("subprocess.run") as mocked_run:
            mocked_run.return_value.returncode = 0
            mocked_run.return_value.stdout = ""
            mocked_run.return_value.stderr = ""

            check_pr_statuses(logging.getLogger("test"))

        mocked_run.assert_called_once()
        self.assertTrue(data["submitted"][0]["fork_deleted"])
        mocked_write.assert_called_once()

    def test_generate_pr_improvement_aborts_after_repeated_self_review_rejections(self) -> None:
        candidate = RepoCandidate(
            name="sample",
            full_name="example/sample",
            description="Sample repo",
            stars=10,
            forks=2,
            license="MIT",
            url="https://github.com/example/sample",
            default_branch="main",
            pushed_days_ago=1,
            topics=["python"],
            files={"sample.py": "def run():\n    return 1\n"},
        )
        ai_json = (
            '{"improvement_type":"bug_fix","pr_title":"fix: sample bug",'
            '"pr_body":"## Summary\\nFix bug\\n## Why it matters\\nPrevents IndexError crash.\\n## Testing\\nAdded regression coverage.",'
            '"rationale":"Prevents IndexError crash.",'
            '"safety_proof":"The change only affects the previously crashing input path.",'
            '"changed_files":{"sample.py":"def run():\\n    return 2\\n","tests/test_sample.py":"def test_run():\\n    assert True\\n"}}'
        )
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="sample.py",
            pattern_type="missing_input_validation",
            failure_mode="callers expecting 2 receive 1 from the stale code path on a valid invocation.",
            evidence="the function body returns a hard-coded stale value",
            patch_scope=1,
            test_target="tests/test_sample.py",
            acceptance_score=90,
        )

        with patch("src.pr_generator.generate_dep_update", return_value=None), \
             patch("src.pr_generator._discover_opportunities", return_value=(opportunity, 123)), \
             patch("src.pr_generator.call_ai", side_effect=[ai_json, ai_json]), \
             patch("src.pr_generator._self_review_diff", side_effect=["behavior change", "behavior change"]):
            with self.assertRaises(PRGeneratorError) as ctx:
                generate_pr_improvement(candidate, logging.getLogger("test"))

        self.assertIn("Self-review rejected this target repeatedly", str(ctx.exception))

    def test_self_review_layout_rejects_wrong_test_root(self) -> None:
        candidate = RepoCandidate(
            name="sample",
            full_name="example/sample",
            description="Sample repo",
            stars=10,
            forks=2,
            license="MIT",
            url="https://github.com/example/sample",
            default_branch="main",
            pushed_days_ago=1,
            topics=["python"],
            files={
                "agent/backtest/validation.py": "def main():\n    return 1\n",
                "agent/tests/test_validation.py": "def test_validation():\n    assert True\n",
            },
        )
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="agent/backtest/validation.py",
            pattern_type="missing_input_validation",
            failure_mode="A missing CLI input can crash instead of showing a clear operator-facing error.",
            evidence="validation.py reads argv directly and the repo keeps tests under agent/tests.",
            patch_scope=1,
            test_target="agent/tests/test_validation.py",
            acceptance_score=90,
        )

        rejection = _self_review_test_layout(
            candidate,
            opportunity,
            {
                "agent/backtest/validation.py": "def main():\n    return 2\n",
                "tests/test_validation.py": "def test_validation():\n    assert True\n",
            },
        )

        self.assertIn("does not match the repository test layout", rejection)

    def test_self_review_layout_rejects_import_style_mismatch(self) -> None:
        candidate = RepoCandidate(
            name="sample",
            full_name="example/sample",
            description="Sample repo",
            stars=10,
            forks=2,
            license="MIT",
            url="https://github.com/example/sample",
            default_branch="main",
            pushed_days_ago=1,
            topics=["python"],
            files={
                "agent/backtest/validation.py": "def main():\n    return 1\n",
                "agent/tests/test_validation.py": "from backtest import validation\n\ndef test_validation():\n    assert validation is not None\n",
            },
        )
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="agent/backtest/validation.py",
            pattern_type="missing_input_validation",
            failure_mode="A missing CLI input can crash instead of showing a clear operator-facing error.",
            evidence="validation.py reads argv directly and the repo keeps tests under agent/tests.",
            patch_scope=1,
            test_target="agent/tests/test_validation_cli.py",
            acceptance_score=90,
        )

        rejection = _self_review_test_layout(
            candidate,
            opportunity,
            {
                "agent/backtest/validation.py": "def main():\n    return 2\n",
                "agent/tests/test_validation_cli.py": "from agent.backtest import validation\n\ndef test_cli():\n    assert validation is not None\n",
            },
        )

        self.assertIn("uses agent.* imports", rejection)

    def test_generate_pr_improvement_rejects_wrong_test_layout_before_ai_review(self) -> None:
        candidate = RepoCandidate(
            name="sample",
            full_name="example/sample",
            description="Sample repo",
            stars=10,
            forks=2,
            license="MIT",
            url="https://github.com/example/sample",
            default_branch="main",
            pushed_days_ago=1,
            topics=["python"],
            files={
                "agent/backtest/validation.py": "def main():\n    return 1\n",
                "agent/tests/test_validation.py": "from backtest import validation\n\ndef test_validation():\n    assert validation is not None\n",
            },
        )
        ai_json = (
            '{"improvement_type":"bug_fix","pr_title":"fix: validation cli input",'
            '"pr_body":"## Summary\\nFix cli validation\\n## Why it matters\\nPrevents raw path errors.\\n## Testing\\nAdded regression coverage.",'
            '"rationale":"Prevents raw path errors.",'
            '"safety_proof":"The change only guards malformed input before existing valid paths continue.",'
            '"changed_files":{"agent/backtest/validation.py":"def main():\\n    return 2\\n","tests/test_validation.py":"def test_cli():\\n    assert True\\n"}}'
        )
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="agent/backtest/validation.py",
            pattern_type="missing_input_validation",
            failure_mode="A missing CLI input can crash instead of showing a clear operator-facing error.",
            evidence="validation.py reads argv directly and the repo keeps tests under agent/tests.",
            patch_scope=1,
            test_target="agent/tests/test_validation.py",
            acceptance_score=90,
        )

        with patch("src.pr_generator.generate_dep_update", return_value=None), \
             patch("src.pr_generator._discover_opportunities", return_value=(opportunity, 123)), \
             patch("src.pr_generator.call_ai", side_effect=[ai_json, ai_json]), \
             patch("src.pr_generator._self_review_diff") as mocked_review:
            with self.assertRaises(PRGeneratorError) as ctx:
                generate_pr_improvement(candidate, logging.getLogger("test"))

        self.assertIn("repository test layout", str(ctx.exception))
        mocked_review.assert_not_called()

    def test_acceptance_score_prefers_small_tested_repo(self) -> None:
        small = RepoCandidate(
            name="small-cli",
            full_name="example/small-cli",
            description="Small python cli client",
            stars=500,
            forks=60,
            license="MIT",
            url="https://github.com/example/small-cli",
            default_branch="main",
            pushed_days_ago=1,
            topics=["python", "cli"],
            files={
                "src/app.py": "def run():\n    return 1\n",
                "src/client.py": "def fetch():\n    return {}\n",
                "tests/test_app.py": "def test_run():\n    assert True\n",
            },
        )
        big = RepoCandidate(
            name="big-framework",
            full_name="example/big-framework",
            description="Large python framework platform",
            stars=1500,
            forks=200,
            license="MIT",
            url="https://github.com/example/big-framework",
            default_branch="main",
            pushed_days_ago=1,
            topics=["python", "framework"],
            files={**{f"pkg/mod{i}.py": "x=1\n" for i in range(90)}, **{"docs/readme.md": "# hi\n"}},
        )

        self.assertGreater(_acceptance_score(small, small.files), _acceptance_score(big, big.files))

    def test_first_pr_repo_fit_accepts_small_tested_recent_repo(self) -> None:
        candidate = RepoCandidate(
            name="small-cli",
            full_name="example/small-cli",
            description="Small python cli client",
            stars=600,
            forks=60,
            license="MIT",
            url="https://github.com/example/small-cli",
            default_branch="main",
            pushed_days_ago=2,
            topics=["python", "cli"],
            files={
                "src/app.py": "def run():\n    return 1\n",
                "tests/test_app.py": "def test_run():\n    assert True\n",
            },
        )

        accepted, reason = _first_pr_repo_fit(candidate, candidate.files)

        self.assertTrue(accepted)
        self.assertIn("small active repo", reason)

    def test_first_pr_repo_fit_rejects_large_or_untested_repo(self) -> None:
        candidate = RepoCandidate(
            name="big-framework",
            full_name="example/big-framework",
            description="Broad framework",
            stars=5000,
            forks=200,
            license="MIT",
            url="https://github.com/example/big-framework",
            default_branch="main",
            pushed_days_ago=3,
            topics=["python"],
            files={f"pkg/mod{i}.py": "x=1\n" for i in range(70)},
        )

        accepted, reason = _first_pr_repo_fit(candidate, candidate.files)

        self.assertFalse(accepted)
        self.assertTrue("stars" in reason or "files" in reason or "tests" in reason)

    def test_generate_dep_update_rejects_high_risk_pre_one_minor_bump(self) -> None:
        candidate = RepoCandidate(
            name="agentic-inbox",
            full_name="cloudflare/agentic-inbox",
            description="TypeScript app",
            stars=1800,
            forks=120,
            license="apache-2.0",
            url="https://github.com/cloudflare/agentic-inbox",
            default_branch="main",
            pushed_days_ago=5,
            topics=["typescript"],
            files={
                "package.json": '{\n\t"dependencies": {\n\t\t"@cloudflare/ai-chat": "^0.1.8"\n\t}\n}\n'
            },
        )

        with patch("src.pr_generator._check_npm_latest", return_value="0.5.4"):
            improvement = generate_dep_update(candidate, logging.getLogger("test"))

        self.assertIsNone(improvement)

    def test_generate_dep_update_preserves_package_json_format_and_exact_count(self) -> None:
        candidate = RepoCandidate(
            name="tooling",
            full_name="example/tooling",
            description="TypeScript app",
            stars=500,
            forks=50,
            license="mit",
            url="https://github.com/example/tooling",
            default_branch="main",
            pushed_days_ago=2,
            topics=["typescript"],
            files={
                "package.json": (
                    '{\n'
                    '\t"dependencies": {\n'
                    '\t\t"pkg-a": "^1.2.3",\n'
                    '\t\t"pkg-b": "^2.3.4",\n'
                    '\t\t"pkg-c": "^3.0.0"\n'
                    '\t},\n'
                    '\t"scripts": {\n'
                    '\t\t"typecheck": "tsc -b"\n'
                    '\t}\n'
                    '}\n'
                )
            },
        )
        latest = {"pkg-a": "1.2.4", "pkg-b": "2.3.5", "pkg-c": "3.0.0"}

        with patch("src.pr_generator._check_npm_latest", side_effect=lambda pkg: latest[pkg]):
            improvement = generate_dep_update(candidate, logging.getLogger("test"))

        self.assertIsNotNone(improvement)
        assert improvement is not None
        manifest = improvement.changed_files["package.json"]
        self.assertIn('\t"dependencies"', manifest)
        self.assertIn('\t\t"pkg-a": "^1.2.4"', manifest)
        self.assertIn('\t\t"pkg-b": "^2.3.5"', manifest)
        self.assertNotIn('    "dependencies"', manifest)
        self.assertEqual(improvement.title, "chore(deps): bump 2 outdated dependencies")
        self.assertIn("`pkg-a` 1.2.3->1.2.4", improvement.body)
        self.assertIn("`pkg-b` 2.3.4->2.3.5", improvement.body)
        self.assertNotIn("pkg-c", improvement.body)

    def test_generate_dep_update_records_opportunity_and_submit_state(self) -> None:
        store = self._make_store()
        run_id = store.start_run("targeted", 1)
        candidate = RepoCandidate(
            name="tooling",
            full_name="example/tooling",
            description="TypeScript app",
            stars=500,
            forks=50,
            license="mit",
            url="https://github.com/example/tooling",
            default_branch="main",
            pushed_days_ago=2,
            topics=["typescript"],
            files={
                "package.json": (
                    '{\n'
                    '\t"dependencies": {\n'
                    '\t\t"pkg-a": "^1.2.3"\n'
                    '\t},\n'
                    '\t"scripts": {\n'
                    '\t\t"typecheck": "tsc -b"\n'
                    '\t}\n'
                    '}\n'
                )
            },
        )

        with patch("src.pr_generator._ENGINE_STORE", store), \
             patch("src.pr_generator._ACTIVE_RUN_ID", run_id), \
             patch("src.pr_generator._check_npm_latest", return_value="1.2.4"):
            improvement = generate_dep_update(candidate, logging.getLogger("test"))
            assert improvement is not None
            self.assertIsNotNone(improvement.opportunity_id)
            ready_summary = store.summarize_run(run_id)
            self.assertEqual(ready_summary["state_counts"].get("READY"), 1)

            pr_result = SimpleNamespace(
                full_name=candidate.full_name,
                pr_url="https://github.com/example/tooling/pull/1",
                pr_title=improvement.title,
                fork_name="me/tooling",
                branch_name="branch",
                files_changed=list(improvement.changed_files.keys()),
                submitted_at="2026-04-29T00:00:00+00:00",
            )
            save_pr_log(
                pr_result,
                improvement_type=improvement.improvement_type,
                opportunity_id=improvement.opportunity_id,
            )

        submitted_summary = store.summarize_run(run_id)
        self.assertEqual(submitted_summary["state_counts"].get("SUBMIT"), 1)


if __name__ == "__main__":
    unittest.main()
