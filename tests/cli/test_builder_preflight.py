from __future__ import annotations

import argparse
import logging
import unittest
from unittest import mock

from app import builder
from src.github.scraper import ScraperError


class _NoopStatus:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _NoopConsole:
    def status(self, *args, **kwargs):
        return _NoopStatus()

    def print(self, *args, **kwargs):
        return None


class BuilderTargetPreflightTests(unittest.TestCase):
    def test_targeted_repo_scope_failure_stops_before_run_section(self) -> None:
        args = argparse.Namespace(
            count=1,
            contrib="example/docs-only",
            dry_run=False,
            goal="bugfix",
            first_pr=False,
            override_limits=False,
        )

        with mock.patch("src.core.cli_ui._console", _NoopConsole()), mock.patch(
            "app.builder.get_current_github_login", return_value="nadira"
        ), mock.patch(
            "app.builder.fetch_repo_candidate",
            side_effect=ScraperError("No Python or TypeScript files found in example/docs-only"),
        ), mock.patch(
            "src.core.cli_ui.print_err"
        ) as mocked_error, mock.patch(
            "src.core.cli_ui.print_section"
        ) as mocked_section, mock.patch(
            "app.builder.start_pr_engine_run"
        ) as mocked_start:
            builder.run_contribution_mode(args, logging.getLogger("test"))

        mocked_error.assert_called_once_with(
            "cannot use target repo: No Python or TypeScript files found in example/docs-only"
        )
        mocked_section.assert_not_called()
        mocked_start.assert_not_called()

    def test_targeted_repo_preflight_passes_override_limits_flag(self) -> None:
        args = argparse.Namespace(
            count=1,
            contrib="example/project",
            dry_run=True,
            goal="bugfix",
            first_pr=False,
            override_limits=True,
        )
        candidate = mock.Mock()
        candidate.full_name = "example/project"
        candidate.stars = 1
        candidate.license = "MIT"
        candidate.pushed_days_ago = 999
        candidate.files = {"app.py": "def run():\n    return 1\n"}

        with mock.patch("src.core.cli_ui._console", _NoopConsole()), mock.patch(
            "app.builder.get_current_github_login", return_value="nadira"
        ), mock.patch(
            "app.builder.fetch_repo_candidate", return_value=candidate
        ) as mocked_fetch, mock.patch(
            "app.builder.start_pr_engine_run"
        ), mock.patch(
            "app.builder.get_pr_submitted_repos", return_value=set()
        ), mock.patch(
            "app.builder.get_security_blacklisted_sources", return_value=set()
        ), mock.patch(
            "app.builder.generate_pr_improvement",
            return_value=mock.Mock(
                title="Fix app",
                improvement_type="bug_fix",
                rationale="rationale",
                changed_files={"app.py": "def run():\n    return 2\n"},
                opportunity_id=1,
            ),
        ), mock.patch(
            "app.builder.can_submit_contribution_to_repo", return_value=True
        ), mock.patch(
            "src.core.cli_ui._choose_arrow", return_value="No - I'm done"
        ), mock.patch(
            "app.builder.finish_pr_engine_run", return_value={}
        ):
            builder.run_contribution_mode(args, logging.getLogger("test"))

        mocked_fetch.assert_called_once_with(
            "example/project", logging.getLogger("test"), override_limits=True, status_cb=mock.ANY
        )

    def test_human_approval_without_tty_queues_before_submit(self) -> None:
        args = argparse.Namespace(
            count=1,
            contrib="example/project",
            dry_run=False,
            goal="bugfix",
            first_pr=False,
            override_limits=False,
            human_approval=True,
        )
        candidate = mock.Mock()
        candidate.full_name = "example/project"
        candidate.stars = 10
        candidate.license = "MIT"
        candidate.pushed_days_ago = 1
        candidate.files = {"app.py": "def run():\n    return 1\n"}
        improvement = mock.Mock(
            title="fix: app bug",
            improvement_type="bug_fix",
            rationale="Prevents a concrete failure.",
            changed_files={"app.py": "def run():\n    return 2\n"},
            opportunity_id=123,
        )
        store = mock.Mock()

        with mock.patch("src.core.cli_ui._console", _NoopConsole()), mock.patch(
            "app.builder.get_current_github_login", return_value="nadira"
        ), mock.patch(
            "app.builder.fetch_repo_candidate", return_value=candidate
        ), mock.patch(
            "app.builder.start_pr_engine_run"
        ), mock.patch(
            "app.builder.get_pr_submitted_repos", return_value=set()
        ), mock.patch(
            "app.builder.get_security_blacklisted_sources", return_value=set()
        ), mock.patch(
            "app.builder.can_submit_contribution_to_repo", return_value=True
        ), mock.patch(
            "app.builder.generate_pr_improvement", return_value=improvement
        ), mock.patch(
            "app.builder.sys.stdin.isatty", return_value=False
        ), mock.patch(
            "app.builder.PREngineStore", return_value=store
        ), mock.patch(
            "app.builder.fork_and_submit_pr"
        ) as mocked_submit, mock.patch(
            "app.builder.finish_pr_engine_run", return_value={}
        ):
            builder.run_contribution_mode(args, logging.getLogger("test"))

        mocked_submit.assert_not_called()
        store.transition_opportunity.assert_called_once_with(
            123,
            "READY",
            why_advanced="Queued because human approval was requested without an interactive TTY.",
        )
        store.record_repo_event.assert_called_once()

    def test_no_human_approval_overrides_env_and_submits(self) -> None:
        args = argparse.Namespace(
            count=1,
            contrib="example/project",
            dry_run=False,
            goal="bugfix",
            first_pr=False,
            override_limits=False,
            human_approval=False,
        )
        candidate = mock.Mock()
        candidate.full_name = "example/project"
        candidate.default_branch = "main"
        candidate.stars = 10
        candidate.license = "MIT"
        candidate.pushed_days_ago = 1
        candidate.files = {"app.py": "def run():\n    return 1\n"}
        improvement = mock.Mock(
            title="fix: app bug",
            improvement_type="bug_fix",
            rationale="Prevents a concrete failure.",
            changed_files={"app.py": "def run():\n    return 2\n"},
            opportunity_id=123,
        )
        pr_result = mock.Mock()
        pr_result.pr_url = "https://github.com/example/project/pull/1"
        pr_result.pr_title = improvement.title

        with mock.patch.dict("os.environ", {"ROVER_HUMAN_APPROVAL": "1"}, clear=False), mock.patch(
            "src.core.cli_ui._console", _NoopConsole()
        ), mock.patch(
            "app.builder.get_current_github_login", return_value="nadira"
        ), mock.patch(
            "app.builder.fetch_repo_candidate", return_value=candidate
        ), mock.patch(
            "app.builder.start_pr_engine_run"
        ), mock.patch(
            "app.builder.get_pr_submitted_repos", return_value=set()
        ), mock.patch(
            "app.builder.get_security_blacklisted_sources", return_value=set()
        ), mock.patch(
            "app.builder.can_submit_contribution_to_repo", return_value=True
        ), mock.patch(
            "app.builder.generate_pr_improvement", return_value=improvement
        ), mock.patch(
            "app.builder.fork_and_submit_pr", return_value=pr_result
        ) as mocked_submit, mock.patch(
            "app.builder.save_pr_log"
        ), mock.patch(
            "app.builder.notify"
        ), mock.patch(
            "src.core.cli_ui._choose_arrow", return_value="No - I'm done"
        ), mock.patch(
            "app.builder.finish_pr_engine_run", return_value={}
        ):
            builder.run_contribution_mode(args, logging.getLogger("test"))

        mocked_submit.assert_called_once()

    def test_duplicate_pr_surfaces_known_existing_pr_url(self) -> None:
        args = argparse.Namespace(
            count=1,
            contrib="example/project",
            dry_run=False,
            goal="bugfix",
            first_pr=False,
            override_limits=False,
            human_approval=False,
        )
        candidate = mock.Mock()
        candidate.full_name = "example/project"
        candidate.default_branch = "main"
        candidate.stars = 10
        candidate.license = "MIT"
        candidate.pushed_days_ago = 1
        candidate.files = {"app.py": "def run():\n    return 1\n"}
        improvement = mock.Mock(
            title="fix: app bug",
            improvement_type="bug_fix",
            rationale="Prevents a concrete failure.",
            changed_files={"app.py": "def run():\n    return 2\n"},
            opportunity_id=123,
            body="body",
        )
        store = mock.Mock()

        with mock.patch("src.core.cli_ui._console", _NoopConsole()), mock.patch(
            "app.builder.get_current_github_login", return_value="nadira"
        ), mock.patch(
            "app.builder.fetch_repo_candidate", return_value=candidate
        ), mock.patch(
            "app.builder.start_pr_engine_run", return_value=55
        ), mock.patch(
            "app.builder.get_pr_submitted_repos", return_value=set()
        ), mock.patch(
            "app.builder.get_security_blacklisted_sources", return_value=set()
        ), mock.patch(
            "app.builder.can_submit_contribution_to_repo", return_value=True
        ), mock.patch(
            "app.builder.generate_pr_improvement", return_value=improvement
        ), mock.patch(
            "app.builder.fork_and_submit_pr", side_effect=builder.PRAlreadyExistsError("already open")
        ), mock.patch(
            "app.builder._known_open_pr",
            return_value={"pr_url": "https://github.com/example/project/pull/264", "pr_title": "fix: app bug"},
        ), mock.patch(
            "app.builder.PREngineStore", return_value=store
        ), mock.patch(
            "app.builder.finish_pr_engine_run", return_value={}
        ):
            builder.run_contribution_mode(args, logging.getLogger("test"))

        store.record_repo_event.assert_any_call(
            55,
            "example/project",
            "pr_already_open",
            "Existing PR already open: fix: app bug",
            {
                "pr_url": "https://github.com/example/project/pull/264",
                "pr_title": "fix: app bug",
                "source": "",
            },
        )

    def test_existing_open_pr_gate_records_known_pr_url(self) -> None:
        args = argparse.Namespace(
            count=1,
            contrib="example/project",
            dry_run=False,
            goal="bugfix",
            first_pr=False,
            override_limits=False,
            human_approval=False,
        )
        candidate = mock.Mock()
        candidate.full_name = "example/project"
        candidate.default_branch = "main"
        candidate.stars = 10
        candidate.license = "MIT"
        candidate.pushed_days_ago = 1
        candidate.files = {"app.py": "def run():\n    return 1\n"}
        store = mock.Mock()

        with mock.patch("src.core.cli_ui._console", _NoopConsole()), mock.patch(
            "app.builder.get_current_github_login", return_value="nadira"
        ), mock.patch(
            "app.builder.fetch_repo_candidate", return_value=candidate
        ), mock.patch(
            "app.builder.start_pr_engine_run", return_value=77
        ), mock.patch(
            "app.builder.get_pr_submitted_repos", return_value=set()
        ), mock.patch(
            "app.builder.get_security_blacklisted_sources", return_value=set()
        ), mock.patch(
            "app.builder.can_submit_contribution_to_repo", return_value=False
        ), mock.patch(
            "app.builder._known_open_pr",
            return_value={"pr_url": "https://github.com/example/project/pull/264", "pr_title": "fix: app bug"},
        ), mock.patch(
            "app.builder.PREngineStore", return_value=store
        ), mock.patch(
            "app.builder.finish_pr_engine_run", return_value={}
        ):
            builder.run_contribution_mode(args, logging.getLogger("test"))

        store.record_repo_event.assert_any_call(
            77,
            "example/project",
            "pr_already_open",
            "Existing PR already open: fix: app bug",
            {
                "pr_url": "https://github.com/example/project/pull/264",
                "pr_title": "fix: app bug",
                "source": "",
            },
        )

    def test_human_approval_queue_records_operator_reason_and_notifies(self) -> None:
        args = argparse.Namespace(human_approval=True)
        candidate = mock.Mock()
        candidate.full_name = "example/project"
        improvement = mock.Mock(
            title="fix: app bug",
            improvement_type="bug_fix",
            changed_files={"app.py": "def run():\n    return 2\n"},
            opportunity_id=123,
        )
        store = mock.Mock()

        with mock.patch("app.builder.sys.stdin.isatty", return_value=True), mock.patch(
            "src.core.cli_ui.print_blank"
        ), mock.patch(
            "src.core.cli_ui.print_section"
        ), mock.patch(
            "src.core.cli_ui.print_info"
        ), mock.patch(
            "src.core.cli_ui.print_item"
        ), mock.patch(
            "src.core.cli_ui._choose_arrow", return_value="Queue for later"
        ), mock.patch(
            "app.builder._read_operator_reason", return_value="test failed locally"
        ), mock.patch(
            "app.builder.PREngineStore", return_value=store
        ), mock.patch(
            "app.builder.notify"
        ) as mocked_notify:
            decision = builder._handle_human_approval(args, candidate, improvement, logging.getLogger("test"))

        self.assertEqual(decision, "queue")
        store.transition_opportunity.assert_called_once_with(
            123,
            "READY",
            why_advanced="Queued by operator during human approval: test failed locally",
        )
        event_details = store.record_repo_event.call_args.args[4]
        self.assertEqual(event_details["reason"], "test failed locally")
        self.assertEqual(event_details["risk_level"], "medium")
        mocked_notify.assert_called_once_with("PR queued: example/project - fix: app bug")


if __name__ == "__main__":
    unittest.main()
