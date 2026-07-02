from __future__ import annotations

import logging
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.contrib.contribution_store import PREngineStore
from src.contrib.pr_generator import (
    PatchPlan,
    PRGeneratorError,
    Opportunity,
    PatchShape,
    _TARGETED_PATTERN_GENERATE_BUDGETS,
    _TARGETED_PATTERN_REVIEW_BUDGETS,
    _classify_patch_shape,
    _duplicate_patch_family_rejection,
    _discover_opportunities,
    _fetch_branch_files,
    _fetch_pr_comments,
    _fetch_pr_review_comments,
    _fetch_pr_reviews,
    _acceptance_score,
    _check_pr_evidence_quality,
    _delete_fork,
    _first_pr_repo_fit,
    _get_lane_preset,
    _matches_contribution_lane,
    _normalize_queries,
    _parse_pr_number,
    _recent_pr_recon,
    _self_review_test_layout,
    _self_review_diff,
    _targeted_execution_mode,
    _targeted_pattern_policy,
    _validate_candidate_scope,
    check_all_prs,
    check_pr_statuses,
    check_pr_feedback,
    fetch_repo_candidate_with_scope,
    generate_dep_update,
    generate_pr_improvement,
    get_followup_candidates,
    get_pr_submitted_repos,
    save_pr_log,
)
from src.contrib.pr_generator import generate_pr_response
from src.github.fork import ForkError
from src.github.scraper import RepoCandidate, ScraperError


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

        with patch("src.contrib.pr_generator._LANE_KEYWORDS", set()):
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

    def test_parse_pr_number_accepts_trailing_slash_query_and_fragment(self) -> None:
        self.assertEqual(_parse_pr_number("https://github.com/o/r/pull/123"), 123)
        self.assertEqual(_parse_pr_number("https://github.com/o/r/pull/123/"), 123)
        self.assertEqual(_parse_pr_number("https://github.com/o/r/pull/123?foo=bar"), 123)
        self.assertEqual(_parse_pr_number("https://github.com/o/r/pull/123#discussion"), 123)
        self.assertIsNone(_parse_pr_number("https://github.com/o/r/issues/123"))

    def test_fetch_branch_files_skips_missing_fork_or_branch(self) -> None:
        with patch("src.contrib.pr_generator.subprocess.run") as mocked_run:
            self.assertEqual(_fetch_branch_files("", "branch", ["a.py"]), {})
            self.assertEqual(_fetch_branch_files("owner/repo", "", ["a.py"]), {})
            self.assertEqual(_fetch_branch_files("owner/repo", "branch", []), {})

        mocked_run.assert_not_called()

    def test_pr_feedback_fetchers_skip_invalid_repo_name(self) -> None:
        with patch("src.contrib.pr_generator.subprocess.run") as mocked_run:
            self.assertEqual(_fetch_pr_comments("", 1), [])
            self.assertEqual(_fetch_pr_review_comments("owneronly", 1), [])
            self.assertEqual(_fetch_pr_reviews("", 1), [])

        mocked_run.assert_not_called()

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

        with patch("src.contrib.pr_generator._LANE_KEYWORDS", {"api", "cli"}):
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

        with patch("src.contrib.pr_generator._MAX_REPO_FILES", 120), \
             patch("src.contrib.pr_generator._PR_TARGETED_ALLOW_BROAD", False):
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

        with patch("src.contrib.pr_generator._PR_TARGETED_ALLOW_BROAD", True):
            py_count, ts_count, test_count = _validate_candidate_scope(candidate, targeted=True)

        self.assertEqual((py_count, ts_count, test_count), (120, 0, 0))

    def test_override_limits_bypasses_targeted_breadth_without_env(self) -> None:
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

        with patch("src.contrib.pr_generator._MAX_REPO_FILES", 120), \
             patch("src.contrib.pr_generator._PR_TARGETED_ALLOW_BROAD", False):
            py_count, ts_count, test_count = _validate_candidate_scope(
                candidate,
                targeted=True,
                override_limits=True,
            )

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

        with patch("src.contrib.pr_generator._gh_get", return_value=payload), \
             patch("src.contrib.pr_generator.download_repo_files", return_value=files):
            candidate = fetch_repo_candidate_with_scope(
                "https://github.com/example/large-repo",
                logging.getLogger("test"),
                enforce_scope=False,
            )

        self.assertEqual(candidate.full_name, "example/large-repo")
        self.assertEqual(len(candidate.files), 120)

    def test_fetch_repo_candidate_with_scope_rejects_archived_targeted_repo(self) -> None:
        payload = {
            "name": "archived-repo",
            "full_name": "example/archived-repo",
            "description": "Archived repository",
            "stargazers_count": 50,
            "forks_count": 5,
            "license": {"spdx_id": "MIT"},
            "html_url": "https://github.com/example/archived-repo",
            "default_branch": "main",
            "pushed_at": "2026-04-29T00:00:00Z",
            "topics": ["python"],
            "archived": True,
        }

        with patch("src.contrib.pr_generator._gh_get", return_value=payload):
            with self.assertRaises(ScraperError) as ctx:
                fetch_repo_candidate_with_scope(
                    "example/archived-repo",
                    logging.getLogger("test"),
                    enforce_scope=True,
                )

        self.assertIn("archived", str(ctx.exception))

    def test_recent_pr_recon_returns_unavailable_on_gh_timeout(self) -> None:
        candidate = RepoCandidate(
            name="sample",
            full_name="example/sample",
            description="Sample repo",
            stars=1000,
            forks=100,
            license="MIT",
            url="https://github.com/example/sample",
            default_branch="main",
            pushed_days_ago=1,
            topics=["python"],
            files={},
        )

        with patch("src.contrib.pr_generator.subprocess.run", side_effect=subprocess.TimeoutExpired(["gh"], 30)):
            result = _recent_pr_recon(candidate, logging.getLogger("test"))

        self.assertFalse(result.get("available"))
        self.assertEqual(result.get("reason"), "gh_timeout")

    def test_recent_pr_recon_rejects_repeated_negative_closed_prs(self) -> None:
        candidate = RepoCandidate(
            name="sample",
            full_name="example/sample",
            description="Sample repo",
            stars=1000,
            forks=100,
            license="MIT",
            url="https://github.com/example/sample",
            default_branch="main",
            pushed_days_ago=1,
            topics=["python"],
            files={},
        )
        merged = subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr="")
        closed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '[{"number":1,"title":"AI generated cleanup with no tests",'
                '"url":"https://github.com/example/sample/pull/1",'
                '"author":{"login":"contrib-bot"},"labels":[]},'
                '{"number":2,"title":"Too broad unsolicited bot PR",'
                '"url":"https://github.com/example/sample/pull/2",'
                '"author":{"login":"helper"},"labels":[]}]'
            ),
            stderr="",
        )

        with patch("src.contrib.pr_generator.subprocess.run", side_effect=[merged, closed]), \
             patch("src.contrib.pr_generator._ENGINE_STORE.record_repo_event"):
            with self.assertRaises(PRGeneratorError) as ctx:
                _recent_pr_recon(candidate, logging.getLogger("test"))

        self.assertIn("negative maintainer signals", str(ctx.exception))

    def test_fetch_repo_candidate_with_scope_rejects_inactive_targeted_repo(self) -> None:
        payload = {
            "name": "inactive-repo",
            "full_name": "example/inactive-repo",
            "description": "Inactive repository",
            "stargazers_count": 50,
            "forks_count": 5,
            "license": {"spdx_id": "MIT"},
            "html_url": "https://github.com/example/inactive-repo",
            "default_branch": "main",
            "pushed_at": "2024-01-01T00:00:00Z",
            "topics": ["python"],
        }

        with patch("src.contrib.pr_generator._gh_get", return_value=payload):
            with self.assertRaises(ScraperError) as ctx:
                fetch_repo_candidate_with_scope(
                    "example/inactive-repo",
                    logging.getLogger("test"),
                    enforce_scope=True,
                )

        self.assertIn("Use 'menisik inspect example/inactive-repo' instead.", str(ctx.exception))

    def test_fetch_repo_candidate_override_limits_allows_inactive_targeted_repo(self) -> None:
        payload = {
            "name": "inactive-repo",
            "full_name": "example/inactive-repo",
            "description": "Inactive repository",
            "stargazers_count": 50,
            "forks_count": 5,
            "license": {"spdx_id": "MIT"},
            "html_url": "https://github.com/example/inactive-repo",
            "default_branch": "main",
            "pushed_at": "2024-01-01T00:00:00Z",
            "topics": ["python"],
        }
        files = {"app.py": "def run():\n    return 1\n"}

        with patch("src.contrib.pr_generator._gh_get", return_value=payload), \
             patch("src.contrib.pr_generator.download_repo_files", return_value=files):
            candidate = fetch_repo_candidate_with_scope(
                "example/inactive-repo",
                logging.getLogger("test"),
                enforce_scope=True,
                override_limits=True,
            )

        self.assertEqual(candidate.full_name, "example/inactive-repo")

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
            "full_name": "itsjawreal/wallet-mcp",
            "fork_name": "itsjawreal/wallet-mcp",
        }

        with patch("src.github.fork.get_current_github_login", return_value="itsjawreal"), \
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

        with patch("src.contrib.pr_generator.load_pr_log", return_value=data), \
             patch("src.github.fork.get_current_github_login", return_value="currentuser"), \
             patch("pathlib.Path.write_text") as mocked_write, \
             patch("subprocess.run") as mocked_run:
            mocked_run.return_value.returncode = 0
            mocked_run.return_value.stdout = ""
            mocked_run.return_value.stderr = ""

            check_pr_statuses(logging.getLogger("test"))

        mocked_run.assert_called_once()
        self.assertTrue(data["submitted"][0]["fork_deleted"])
        mocked_write.assert_called_once()

    def test_check_pr_statuses_filters_entries_to_active_owner(self) -> None:
        data = {
            "submitted": [
                {
                    "full_name": "example/upstream-repo",
                    "owner_login": "otheruser",
                    "pr_url": "https://github.com/example/upstream-repo/pull/10",
                    "pr_title": "fix: sample",
                    "fork_name": "otheruser/upstream-repo",
                    "branch_name": "otheruser-patch-1",
                    "files_changed": ["src/foo.py"],
                    "submitted_at": "2026-04-28T10:00:00",
                    "status": "open",
                    "notified_merge": False,
                }
            ]
        }

        with patch("src.contrib.pr_generator.load_pr_log", return_value=data), \
             patch("src.github.fork.get_current_github_login", return_value="currentuser"), \
             patch("pathlib.Path.write_text") as mocked_write, \
             patch("subprocess.run") as mocked_run:
            check_pr_statuses(logging.getLogger("test"))

        mocked_run.assert_not_called()
        mocked_write.assert_not_called()

    def test_check_pr_feedback_filters_entries_to_active_owner(self) -> None:
        data = {
            "submitted": [
                {
                    "full_name": "example/upstream-repo",
                    "owner_login": "otheruser",
                    "pr_url": "https://github.com/example/upstream-repo/pull/10",
                    "status": "open",
                    "fork_name": "otheruser/upstream-repo",
                }
            ]
        }

        with patch("src.contrib.pr_generator.load_pr_log", return_value=data), \
             patch("src.github.fork.get_current_github_login", return_value="currentuser"), \
             patch("src.contrib.pr_generator._fetch_pr_comments") as mocked_issue, \
             patch("src.contrib.pr_generator._fetch_pr_review_comments") as mocked_review_comments, \
             patch("src.contrib.pr_generator._fetch_pr_reviews") as mocked_reviews:
            check_pr_feedback(logging.getLogger("test"))

        mocked_issue.assert_not_called()
        mocked_review_comments.assert_not_called()
        mocked_reviews.assert_not_called()

    def test_get_pr_submitted_repos_filters_legacy_entries_to_active_owner(self) -> None:
        payload = {
            "submitted": [
                {"full_name": "example/a", "owner_login": "currentuser", "fork_name": "currentuser/a"},
                {"full_name": "example/b", "owner_login": "otheruser", "fork_name": "otheruser/b"},
            ]
        }
        with patch("src.contrib.pr_generator._ENGINE_STORE.submitted_repos", return_value=set()), \
             patch("src.contrib.pr_generator.load_pr_log", return_value=payload), \
             patch("src.github.fork.get_current_github_login", return_value="currentuser"):
            repos = get_pr_submitted_repos()

        self.assertEqual(repos, {"example/a"})

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

        with patch("src.contrib.pr_generator.generate_dep_update", return_value=None), \
             patch("src.contrib.pr_generator._discover_opportunities", return_value=(opportunity, 123)), \
             patch("src.contrib.pr_generator.call_ai", side_effect=[ai_json, ai_json]), \
             patch("src.contrib.pr_generator._self_review_diff", side_effect=["behavior change", "behavior change"]):
            with self.assertRaises(PRGeneratorError) as ctx:
                generate_pr_improvement(candidate, logging.getLogger("test"))

        self.assertIn("Self-review rejected this target repeatedly", str(ctx.exception))

    def test_targeted_live_kill_switch_stops_after_first_self_review_rejection(self) -> None:
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
            '"pr_body":"## Summary\\nFix bug\\n## Why it matters\\nPrevents crash.\\n## Testing\\nAdded regression coverage.",'
            '"rationale":"Prevents crash.",'
            '"safety_proof":"The change only affects the failing path.",'
            '"changed_files":{"sample.py":"def run():\\n    return 2\\n","tests/test_sample.py":"def test_run():\\n    assert True\\n"}}'
        )
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="sample.py",
            pattern_type="missing_input_validation",
            failure_mode="callers receive stale data on a valid invocation.",
            evidence="the function body returns a hard-coded stale value",
            patch_scope=1,
            test_target="tests/test_sample.py",
            acceptance_score=90,
        )
        plan = (
            '{"target_file":"sample.py","failure_mode":"stale value on valid invocation",'
            '"expected_files":["sample.py","tests/test_sample.py"],'
            '"test_target":"tests/test_sample.py","why_narrow":"single function fix with one regression test",'
            '"proof_path":"assert valid invocation returns updated value"}'
        )

        with patch("src.contrib.pr_generator.generate_dep_update", return_value=None), \
             patch("src.contrib.pr_generator._discover_opportunities", return_value=(opportunity, 123)), \
             patch("src.contrib.pr_generator.call_ai", side_effect=[plan, ai_json]) as mocked_ai, \
             patch("src.contrib.pr_generator._self_review_diff", return_value="behavior change"):
            with self.assertRaises(PRGeneratorError) as ctx:
                generate_pr_improvement(candidate, logging.getLogger("test"), targeted_mode=True)

        self.assertIn("kill switch engaged", str(ctx.exception))
        self.assertEqual(mocked_ai.call_count, 2)

    def test_targeted_structural_retry_rejects_after_repeated_wrong_target_patch(self) -> None:
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
            '{"improvement_type":"bug_fix","pr_title":"fix: wrong file",'
            '"pr_body":"## Summary\\nFix bug\\n## Why it matters\\nPrevents crash.\\n## Testing\\nAdded regression coverage.",'
            '"rationale":"Prevents crash.",'
            '"safety_proof":"The change only affects the failing path.",'
            '"changed_files":{"other.py":"def run():\\n    return 2\\n"}}'
        )
        retry_ai_json = ai_json.replace("fix: wrong file", "fix: alternate wrong file")
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="sample.py",
            pattern_type="missing_input_validation",
            failure_mode="callers receive stale data on a valid invocation.",
            evidence="the function body returns a hard-coded stale value",
            patch_scope=1,
            test_target="tests/test_sample.py",
            acceptance_score=90,
        )
        plan = (
            '{"target_file":"sample.py","failure_mode":"stale value on valid invocation",'
            '"expected_files":["sample.py","other.py"],'
            '"test_target":"tests/test_sample.py","why_narrow":"single function fix in one local branch only",'
            '"proof_path":"assert valid invocation returns updated value"}'
        )

        with patch("src.contrib.pr_generator.generate_dep_update", return_value=None), \
             patch("src.contrib.pr_generator._recent_pr_recon", return_value={}), \
             patch("src.contrib.pr_generator.fetch_file_git_history", return_value=[]), \
             patch("src.contrib.pr_generator._discover_opportunities", return_value=(opportunity, 123)), \
             patch("src.contrib.pr_generator.call_ai", side_effect=[plan, ai_json, retry_ai_json]) as mocked_ai:
            with self.assertRaises(PRGeneratorError) as ctx:
                generate_pr_improvement(candidate, logging.getLogger("test"), targeted_mode=True)

        self.assertIn("Structural review rejected this target repeatedly", str(ctx.exception))
        self.assertEqual(mocked_ai.call_count, 3)

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

        with patch("src.contrib.pr_generator.generate_dep_update", return_value=None), \
             patch("src.contrib.pr_generator._discover_opportunities", return_value=(opportunity, 123)), \
             patch("src.contrib.pr_generator.call_ai", side_effect=[ai_json, ai_json]), \
             patch("src.contrib.pr_generator._self_review_diff") as mocked_review:
            with self.assertRaises(PRGeneratorError) as ctx:
                generate_pr_improvement(candidate, logging.getLogger("test"))

        self.assertIn("repository test layout", str(ctx.exception))
        mocked_review.assert_not_called()

    def test_targeted_plan_rejection_stops_before_codegen(self) -> None:
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
            files={"sample.py": "def run(value):\n    return int(value)\n"},
        )
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="sample.py",
            pattern_type="missing_input_validation",
            failure_mode="Invalid input can raise ValueError instead of returning a clear operator-facing error.",
            evidence="sample.py converts unvalidated input directly with int(value).",
            patch_scope=1,
            test_target="tests/test_sample.py",
            acceptance_score=90,
        )
        bad_plan = (
            '{"target_file":"sample.py","failure_mode":"invalid input blows up",'
            '"expected_files":["sample.py","tests/test_sample.py","docs/readme.md"],'
            '"test_target":"tests/test_sample.py","why_narrow":"small fix","proof_path":"assert"}'
        )

        with patch("src.contrib.pr_generator.generate_dep_update", return_value=None), \
             patch("src.contrib.pr_generator._discover_opportunities", return_value=(opportunity, 123)), \
             patch("src.contrib.pr_generator.call_ai", side_effect=[bad_plan, bad_plan]) as mocked_ai:
            with self.assertRaises(PRGeneratorError) as ctx:
                generate_pr_improvement(candidate, logging.getLogger("test"), targeted_mode=True)

        self.assertIn("allowed narrow file budget", str(ctx.exception))
        self.assertEqual(mocked_ai.call_count, 2)

    def test_targeted_plan_drift_kill_switch_stops_after_first_drift(self) -> None:
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
            files={"sample.py": "def run(value):\n    return int(value)\n"},
        )
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="sample.py",
            pattern_type="missing_input_validation",
            failure_mode="Invalid input can raise ValueError instead of returning a clear operator-facing error.",
            evidence="sample.py converts unvalidated input directly with int(value).",
            patch_scope=1,
            test_target="tests/test_sample.py",
            acceptance_score=90,
        )
        drift_plan = (
            '{"target_file":"other.py","failure_mode":"invalid input blows up",'
            '"expected_files":["other.py","tests/test_sample.py"],'
            '"test_target":"tests/test_sample.py","why_narrow":"single function fix with one regression test",'
            '"proof_path":"assert invalid input now returns clear domain error"}'
        )

        with patch("src.contrib.pr_generator.generate_dep_update", return_value=None), \
             patch("src.contrib.pr_generator._discover_opportunities", return_value=(opportunity, 123)), \
             patch("src.contrib.pr_generator.call_ai", return_value=drift_plan) as mocked_ai:
            with self.assertRaises(PRGeneratorError) as ctx:
                generate_pr_improvement(candidate, logging.getLogger("test"), targeted_mode=True)

        self.assertIn("Patch-plan drift kill switch engaged", str(ctx.exception))
        self.assertEqual(mocked_ai.call_count, 1)

    def test_semantic_self_review_prompt_includes_approved_patch_plan(self) -> None:
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
            files={"sample.py": "def run(value):\n    return int(value)\n"},
        )
        plan = PatchPlan(
            target_file="sample.py",
            failure_mode="Invalid input can raise ValueError instead of a clear error.",
            expected_files=["sample.py", "tests/test_sample.py"],
            test_target="tests/test_sample.py",
            why_narrow="Only the parse boundary changes.",
            proof_path="Assert invalid input now raises a clear domain error.",
        )
        captured_prompt: dict[str, str] = {}

        def fake_call_ai(prompt: str, timeout: int = 0) -> str:
            captured_prompt["text"] = prompt
            return '{"safe": true, "reason": "stays within the approved plan"}'

        with patch("src.contrib.pr_generator.call_ai", side_effect=fake_call_ai):
            rejection = _self_review_diff(
                candidate,
                {"sample.py": "def run(value):\n    return parse_value(value)\n"},
                {"improvement_type": "bug_fix", "pr_title": "fix: validate input", "safety_proof": "Only invalid input path changes."},
                logging.getLogger("test"),
                plan=plan,
            )

        self.assertIsNone(rejection)
        self.assertIn("Approved patch plan", captured_prompt["text"])
        self.assertIn("proof_path", captured_prompt["text"])

    def test_followup_candidates_excludes_already_attempted_repo(self) -> None:
        fake_log = {
            "submitted": [
                {
                    "full_name": "owner/repo-a",
                    "pr_url": "https://github.com/owner/repo-a/pull/1",
                    "status": "merged",
                    "submitted_at": "2020-01-01T00:00:00+00:00",
                },
                {
                    "full_name": "owner/repo-b",
                    "pr_url": "https://github.com/owner/repo-b/pull/1",
                    "status": "merged",
                    "submitted_at": "2020-01-01T00:00:00+00:00",
                },
            ]
        }
        with patch("src.contrib.pr_generator.load_pr_log", return_value=fake_log), \
             patch("src.github.fork.get_current_github_login", return_value=""):
            all_followups = get_followup_candidates(set(), set())
            self.assertIn("owner/repo-a", all_followups)

            # A repo already attempted this run must not be re-surfaced — otherwise
            # the run loop retries the same failing target every attempt.
            filtered = get_followup_candidates(set(), {"owner/repo-a"})
            self.assertNotIn("owner/repo-a", filtered)
            self.assertIn("owner/repo-b", filtered)

    def test_self_review_fails_closed_when_ai_unavailable(self) -> None:
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
            files={"sample.py": "def run(value):\n    return int(value)\n"},
        )
        calls = {"n": 0}

        def boom(*_args: object, **_kwargs: object) -> str:
            calls["n"] += 1
            raise RuntimeError("backend down")

        with patch("src.contrib.pr_generator.call_ai", side_effect=boom):
            rejection = _self_review_diff(
                candidate,
                # A real (non-style-only) change so it reaches the AI review gate.
                {"sample.py": "def run(value):\n    return parse_value(value)\n"},
                {"improvement_type": "bug_fix", "pr_title": "fix", "safety_proof": "narrow"},
                logging.getLogger("test"),
            )

        # Unverifiable patch must be rejected, not silently passed through.
        self.assertIsNotNone(rejection)
        self.assertIn("could not complete", rejection)
        self.assertEqual(calls["n"], 2)  # retried once before failing closed

    def test_self_review_rejects_style_only_change_without_ai(self) -> None:
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
            files={"sample.py": "def run(value):\n    return int(value)\n"},
        )

        def boom(*_args: object, **_kwargs: object) -> str:
            raise AssertionError("AI must not be called for a style-only change")

        with patch("src.contrib.pr_generator.call_ai", side_effect=boom):
            rejection = _self_review_diff(
                candidate,
                {"sample.py": "def run(value):\n    # added a comment only\n    return int(value)\n"},
                {"improvement_type": "bug_fix", "pr_title": "noop", "safety_proof": "no change"},
                logging.getLogger("test"),
            )

        self.assertIsNotNone(rejection)
        self.assertIn("style-only", rejection)

    def test_targeted_shortlist_rejects_weak_core_candidate_before_ai(self) -> None:
        store = self._make_store()
        run_id = store.start_run(mode="contrib", target_count=1)
        content = "\n".join([f"line_{idx} = {idx}" for idx in range(170)])
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
            files={"main.py": content},
        )
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="main.py",
            pattern_type="unchecked_response_shape",
            failure_mode="A malformed response can raise KeyError on a valid runtime path.",
            evidence="main.py indexes response fields directly without guarding shape first.",
            patch_scope=1,
            test_target="",
            acceptance_score=84,
        )

        with patch("src.contrib.pr_generator._ENGINE_STORE", store), \
             patch("src.contrib.pr_generator._ACTIVE_RUN_ID", run_id), \
             patch("src.contrib.pr_generator.fetch_maintainer_signals", return_value={}), \
             patch("src.contrib.pr_generator._discover_issue_backed_bugfixes", return_value=[]), \
             patch("src.contrib.pr_generator._PATTERN_SCANNER.scan", return_value=[opportunity]):
            with self.assertRaises(PRGeneratorError) as ctx:
                _discover_opportunities(candidate, logging.getLogger("test"), goal="bugfix", targeted_mode=True)

        # The targeted shortlist now rejects weak core candidates via the
        # patchability-threshold gate (before any AI call) rather than the older
        # per-pattern "target_area_too_broad" message.
        self.assertIn("patchability threshold", str(ctx.exception))

    def test_targeted_overbroad_exception_handling_rejects_policy_surface(self) -> None:
        store = self._make_store()
        run_id = store.start_run(mode="contrib", target_count=1)
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
            files={"src/context_system/claude_md.py": "def load():\n    try:\n        return read()\n    except Exception:\n        return None\n"},
        )
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="src/context_system/claude_md.py",
            pattern_type="overbroad_exception_handling",
            failure_mode="A real upstream or parsing error can be silently swallowed, leaving operators with no actionable signal.",
            evidence="Line 4 catches a broad exception and only logs/returns, which can hide a distinct failure.",
            patch_scope=1,
            test_target="tests/test_claude_md.py",
            acceptance_score=90,
        )

        with patch("src.contrib.pr_generator._ENGINE_STORE", store), \
             patch("src.contrib.pr_generator._ACTIVE_RUN_ID", run_id), \
             patch("src.contrib.pr_generator._PATTERN_SCANNER.scan", return_value=[opportunity]):
            with self.assertRaises(PRGeneratorError) as ctx:
                _discover_opportunities(candidate, logging.getLogger("test"), goal="bugfix", targeted_mode=True)

        self.assertIn("overbroad_exception_handling:target_area_too_broad", str(ctx.exception))

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

        with patch("src.contrib.pr_generator._check_npm_latest", return_value="0.5.4"):
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

        with patch("src.contrib.pr_generator._check_npm_latest", side_effect=lambda pkg: latest[pkg]):
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

        with patch("src.contrib.pr_generator._ENGINE_STORE", store), \
             patch("src.contrib.pr_generator._ACTIVE_RUN_ID", run_id), \
             patch("src.contrib.pr_generator._check_npm_latest", return_value="1.2.4"):
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

    def test_patch_shape_classifier_downgrades_behavior_routing_surfaces(self) -> None:
        opportunity = Opportunity(
            repo_full_name="example/repo",
            target_file="src/config/settings.py",
            pattern_type="missing_input_validation",
            failure_mode="Invalid config values can crash startup.",
            evidence="settings.py parses env directly.",
            patch_scope=1,
            test_target="tests/test_settings.py",
            acceptance_score=90,
        )

        shape = _classify_patch_shape(
            {"src/config/settings.py": "def load():\n    return None\n"},
            {"src/config/settings.py": "def load():\n    raise ValueError('bad')\n"},
            opportunity,
        )

        self.assertEqual(shape.risk, "high")
        self.assertIn("manual review", shape.reason)

    def test_patch_shape_classifier_flags_risky_filename_stem_not_just_directory(self) -> None:
        opportunity = Opportunity(
            repo_full_name="example/repo",
            target_file="src/auth.py",
            pattern_type="missing_timeout",
            failure_mode="No timeout on auth call.",
            evidence="requests.get omits timeout.",
            patch_scope=1,
            test_target="tests/test_auth.py",
            acceptance_score=90,
        )
        shape = _classify_patch_shape(
            {"src/auth.py": "def authenticate(token):\n    return token\n"},
            {"src/auth.py": "def authenticate(token):\n    return token + ' ok'\n"},
            opportunity,
        )
        self.assertEqual(shape.risk, "high")
        self.assertIn("manual review", shape.reason)

    def test_targeted_execution_mode_splits_live_safe_from_review(self) -> None:
        safe = Opportunity(
            repo_full_name="example/repo",
            target_file="client.py",
            pattern_type="missing_timeout",
            failure_mode="Slow requests can hang forever.",
            evidence="requests.get omits timeout.",
            patch_scope=1,
            test_target="",
            acceptance_score=90,
        )
        cleanup = Opportunity(
            repo_full_name="example/repo",
            target_file="client.py",
            pattern_type="resource_cleanup_gap",
            failure_mode="A file handle can leak on errors.",
            evidence="open() is not closed on exception.",
            patch_scope=1,
            test_target="tests/test_client.py",
            acceptance_score=90,
        )

        self.assertEqual(
            _targeted_execution_mode(_targeted_pattern_policy(safe), PatchShape("low", "")),
            "live-safe",
        )
        self.assertEqual(
            _targeted_execution_mode(_targeted_pattern_policy(cleanup), PatchShape("low", "")),
            "live-review",
        )

    def test_pattern_token_budgets_bias_compute_to_productive_families(self) -> None:
        self.assertEqual(_TARGETED_PATTERN_GENERATE_BUDGETS["overbroad_exception_handling"], 1)
        self.assertEqual(_TARGETED_PATTERN_REVIEW_BUDGETS["overbroad_exception_handling"], 1)
        self.assertEqual(_TARGETED_PATTERN_GENERATE_BUDGETS["missing_input_validation"], 2)
        self.assertEqual(_TARGETED_PATTERN_REVIEW_BUDGETS["unchecked_response_shape"], 2)

    def test_duplicate_patch_family_rejects_repeated_title_family(self) -> None:
        first = {
            "pr_title": "fix: add request timeout",
            "pr_body": "## Summary\nAdd timeout",
            "rationale": "A slow request can hang forever.",
            "safety_proof": "Only timeout handling changes.",
        }
        second = {
            "pr_title": "chore: add request timeout",
            "pr_body": "## Summary\nAdd timeout",
            "rationale": "Same patch family.",
            "safety_proof": "Same patch family.",
        }

        with patch.dict("src.contrib.pr_generator._ACTIVE_RUN_METRICS", {"seen_title_families": []}, clear=True):
            self.assertIsNone(_duplicate_patch_family_rejection(first))
            self.assertIn("Repeated PR title family", _duplicate_patch_family_rejection(second))

    def test_duplicate_patch_family_treats_scoped_prefix_same_as_plain_prefix(self) -> None:
        scoped = {
            "pr_title": "chore(deps): bump axios",
            "pr_body": "## Summary\nBump axios",
            "rationale": "Outdated dependency.",
            "safety_proof": "Only version number changes.",
        }
        plain = {
            "pr_title": "fix: bump axios",
            "pr_body": "## Summary\nBump axios",
            "rationale": "Outdated dependency.",
            "safety_proof": "Only version number changes.",
        }

        with patch.dict("src.contrib.pr_generator._ACTIVE_RUN_METRICS", {"seen_title_families": []}, clear=True):
            self.assertIsNone(_duplicate_patch_family_rejection(scoped))
            self.assertIn("Repeated PR title family", _duplicate_patch_family_rejection(plain))

    def test_duplicate_patch_family_rejects_exception_policy_wording(self) -> None:
        result = {
            "pr_title": "fix: surface errors",
            "pr_body": "Surface errors instead of swallow them.",
            "rationale": "Surface errors instead of swallow them.",
            "safety_proof": "Surface errors instead of swallow them.",
        }

        with patch.dict("src.contrib.pr_generator._ACTIVE_RUN_METRICS", {"seen_title_families": []}, clear=True):
            self.assertIsNone(_duplicate_patch_family_rejection(result))
            self.assertIn("exception-policy wording", _duplicate_patch_family_rejection(result))

    def test_check_all_prs_skips_reply_when_fix_push_fails(self) -> None:
        # Regression: check_all_prs posted the reply comment even when push_to_branch
        # failed, telling the maintainer a fix was applied when nothing was pushed.
        data = {
            "submitted": [
                {
                    "full_name": "example/upstream-repo",
                    "pr_url": "https://github.com/example/upstream-repo/pull/42",
                    "pr_title": "fix: sample",
                    "fork_name": "currentuser/upstream-repo",
                    "branch_name": "currentuser-patch-1",
                    "files_changed": ["src/foo.py"],
                    "submitted_at": "2026-04-28T10:00:00",
                    "status": "open",
                    "notified_merge": False,
                    "last_seen_comment_id": 0,
                }
            ]
        }
        action = SimpleNamespace(
            pr_url="https://github.com/example/upstream-repo/pull/42",
            full_name="example/upstream-repo",
            fork_name="currentuser/upstream-repo",
            branch_name="currentuser-patch-1",
            comment_id=0,
            comment_body="please guard the config parser against None",
            comment_author="maintainer",
            reply="Fixed — added the missing guard.",
            changed_files={"src/foo.py": "def foo():\n    return 1\n"},
            commit_msg="fix: guard config parser",
        )
        seen_cmds: list[list[str]] = []

        def fake_subprocess_run(cmd, **kwargs):
            seen_cmds.append(list(cmd))
            if "reviews" in " ".join(str(c) for c in cmd):
                return SimpleNamespace(returncode=0, stdout="[]", stderr="")
            return SimpleNamespace(
                returncode=0,
                stdout='{"state":"open","merged_at":null,"number":42}',
                stderr="",
            )

        with patch("src.contrib.pr_generator.load_pr_log", return_value=data), \
             patch("src.github.fork.get_current_github_login", return_value="currentuser"), \
             patch("src.contrib.pr_generator._ENGINE_STORE"), \
             patch("src.contrib.pr_generator._fetch_pr_comments", return_value=[
                 {"id": 5, "user": "maintainer", "body": "please guard the config parser", "created_at": "2026-04-29T00:00:00Z"},
             ]), \
             patch("src.contrib.pr_generator._fetch_pr_review_comments", return_value=[]), \
             patch("src.contrib.pr_generator._fetch_pr_reviews", return_value=[]), \
             patch("src.contrib.pr_generator._classify_maintainer_comment", return_value="needs_change"), \
             patch("src.contrib.pr_generator.generate_pr_response", return_value=action), \
             patch("src.github.fork.push_to_branch", side_effect=ForkError("push failed: network error")), \
             patch("src.core.notify.notify"), \
             patch("src.contrib.pr_generator.PR_LOG_FILE"), \
             patch("src.contrib.pr_generator.subprocess.run", side_effect=fake_subprocess_run):
            check_all_prs(logging.getLogger("test"))

        reply_cmds = [cmd for cmd in seen_cmds if "comment" in cmd]
        self.assertEqual(reply_cmds, [])
        # Feedback must be retried next run — last_seen_comment_id must not advance.
        self.assertEqual(data["submitted"][0]["last_seen_comment_id"], 0)

    def test_generate_pr_response_treats_null_reply_as_missing_field(self) -> None:
        # Regression: a JSON-null "reply" passed the keys-present check and crashed
        # with AttributeError on .strip() instead of raising PRGeneratorError.
        entry = {
            "full_name": "example/upstream-repo",
            "pr_url": "https://github.com/example/upstream-repo/pull/42",
            "pr_title": "fix: sample",
            "fork_name": "currentuser/upstream-repo",
            "branch_name": "currentuser-patch-1",
            "files_changed": ["src/foo.py"],
            "last_seen_comment_id": 0,
        }
        null_reply = '{"reply": null, "changed_files": {}, "commit_msg": ""}'

        with patch("src.contrib.pr_generator._fetch_branch_files", return_value={}), \
             patch("src.contrib.pr_generator.call_ai", side_effect=[null_reply, null_reply]), \
             patch("src.contrib.pr_generator.time.sleep"):
            with self.assertRaises(PRGeneratorError) as ctx:
                generate_pr_response(entry, "please fix", "maintainer", logging.getLogger("test"))

        self.assertIn("missing fields", str(ctx.exception))

    def test_generate_pr_improvement_treats_null_title_as_missing_field(self) -> None:
        # Regression: a JSON-null "pr_title"/"pr_body" passed the keys-present check
        # and crashed with AttributeError on .strip() instead of a clean retry/reject.
        store = self._make_store()
        run_id = store.start_run(mode="contrib", target_count=1)
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
        null_title_json = (
            '{"improvement_type":"bug_fix","pr_title":null,'
            '"pr_body":"## Summary\\nFix bug\\n## Why it matters\\nPrevents crash.\\n## Testing\\nAdded regression coverage.",'
            '"rationale":"Prevents crash.",'
            '"safety_proof":"The change only affects the failing path.",'
            '"changed_files":{"sample.py":"def run():\\n    return 2\\n"}}'
        )
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="sample.py",
            pattern_type="missing_input_validation",
            failure_mode="callers receive stale data on a valid invocation.",
            evidence="the function body returns a hard-coded stale value",
            patch_scope=1,
            test_target="tests/test_sample.py",
            acceptance_score=90,
        )

        with patch("src.contrib.pr_generator._ENGINE_STORE", store), \
             patch("src.contrib.pr_generator._ACTIVE_RUN_ID", run_id), \
             patch("src.contrib.pr_generator.generate_dep_update", return_value=None), \
             patch("src.contrib.pr_generator._discover_opportunities", return_value=(opportunity, 123)), \
             patch("src.contrib.pr_generator.call_ai", return_value=null_title_json), \
             patch("src.contrib.pr_generator.time.sleep"):
            with self.assertRaises(PRGeneratorError) as ctx:
                generate_pr_improvement(candidate, logging.getLogger("test"))

        self.assertIn("missing fields", str(ctx.exception))

    def test_generate_pr_improvement_treats_ai_reject_verdict_as_final_without_retry(self) -> None:
        # Regression: agentic CLI backends (Claude Code) refuse to fabricate a patch
        # when the scanner evidence is wrong, but had no JSON escape hatch — they
        # replied in prose, which failed _parse_json and burned all retries on the
        # same definitive verdict.
        store = self._make_store()
        run_id = store.start_run(mode="contrib", target_count=1)
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
        reject_json = (
            '{"decision":"reject",'
            '"reason":"line 36 is a class declaration, not an exception handler"}'
        )
        opportunity = Opportunity(
            repo_full_name=candidate.full_name,
            target_file="sample.py",
            pattern_type="overbroad_exception_handling",
            failure_mode="line 36 catches a broad exception and only logs/returns.",
            evidence="except Exception at line 36",
            patch_scope=1,
            test_target="tests/test_sample.py",
            acceptance_score=90,
        )

        mocked_call_ai = MagicMock(return_value=reject_json)
        with patch("src.contrib.pr_generator._ENGINE_STORE", store), \
             patch("src.contrib.pr_generator._ACTIVE_RUN_ID", run_id), \
             patch("src.contrib.pr_generator.generate_dep_update", return_value=None), \
             patch("src.contrib.pr_generator._discover_opportunities", return_value=(opportunity, 123)), \
             patch("src.contrib.pr_generator.call_ai", mocked_call_ai), \
             patch("src.contrib.pr_generator.time.sleep"):
            with self.assertRaises(PRGeneratorError) as ctx:
                generate_pr_improvement(candidate, logging.getLogger("test"))

        self.assertIn("AI rejected the bug target", str(ctx.exception))
        self.assertIn("class declaration", str(ctx.exception))
        self.assertEqual(mocked_call_ai.call_count, 1)

    def test_check_all_prs_reviews_timeout_does_not_propagate_or_lose_data(self) -> None:
        # Regression: if the reviews fetch timed out, TimeoutExpired previously escaped the
        # for-loop body, skipping the PR log write. Accumulated changes from prior iterations
        # (e.g. a closed PR) were silently lost.
        data = {
            "submitted": [
                {
                    "full_name": "example/upstream-repo",
                    "pr_url": "https://github.com/example/upstream-repo/pull/42",
                    "pr_title": "fix: sample",
                    "fork_name": "currentuser/upstream-repo",
                    "branch_name": "currentuser-patch-1",
                    "files_changed": ["src/foo.py"],
                    "submitted_at": "2026-04-28T10:00:00",
                    "status": "open",
                    "notified_merge": False,
                }
            ]
        }

        open_pr_response = SimpleNamespace(
            returncode=0,
            stdout='{"state":"open","merged_at":null,"number":42}',
            stderr="",
        )

        def fake_subprocess_run(cmd, **kwargs):
            if "reviews" in cmd:
                raise subprocess.TimeoutExpired(cmd, 20)
            return open_pr_response

        with patch("src.contrib.pr_generator.load_pr_log", return_value=data), \
             patch("src.github.fork.get_current_github_login", return_value="currentuser"), \
             patch("src.contrib.pr_generator._fetch_pr_comments", return_value=[]), \
             patch("src.contrib.pr_generator._fetch_pr_review_comments", return_value=[]), \
             patch("src.contrib.pr_generator._fetch_pr_reviews", return_value=[]), \
             patch("src.contrib.pr_generator.PR_LOG_FILE") as mock_log_file, \
             patch("src.contrib.pr_generator.subprocess.run", side_effect=fake_subprocess_run):
            check_all_prs(logging.getLogger("test"))

        # Function must complete without raising — no assert needed beyond reaching here.
        # The PR log write is not called when no changes occurred (open PR with no feedback),
        # but the key assertion is that no exception escaped the loop.


if __name__ == "__main__":
    unittest.main()
