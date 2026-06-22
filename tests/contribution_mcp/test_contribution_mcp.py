from __future__ import annotations

import unittest
from unittest.mock import patch


class ContributionMCPToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        from src.contribution_mcp.server import mcp

        self.tools = {t.name: t for t in mcp._tool_manager.list_tools()}

    def test_all_expected_tools_registered(self) -> None:
        expected = {
            "get_status",
            "list_opportunities",
            "list_prs",
            "contrib_report",
            "doctor",
            "inspect_repo",
            "route_command",
            "start_run",
            "run_contribution",
            "contrib_once",
            "contrib_targeted",
            "cancel_run",
            "stop_contribution",
            "get_run_status",
            "get_run_events",
            "get_run_result",
            "contrib_check",
            "contrib_respond",
            "get_logs",
            "get_config",
            "update_config",
            "start_pr_monitor",
            "stop_pr_monitor",
            "get_pr_monitor_status",
            "start_telegram_bot",
            "stop_telegram_bot",
            "get_telegram_bot_status",
        }
        self.assertEqual(expected, set(self.tools))

    def test_get_run_status_not_running_when_no_run(self) -> None:
        from src.contribution_mcp.server import get_run_status

        result = get_run_status()
        self.assertFalse(result["running"])

    def test_get_run_events_returns_empty_for_unknown_run(self) -> None:
        from src.contribution_mcp.server import get_run_events

        result = get_run_events("missing")
        self.assertEqual(result["events"], [])

    def test_get_run_result_surfaces_existing_open_pr(self) -> None:
        from src.contribution_mcp.server import ManagedRun, get_run_result

        run = ManagedRun(
            run_id="run-1",
            mode="targeted",
            repo="example/project",
            goal="bugfix",
            count=1,
            dry_run=False,
            first_pr=False,
            override_limits=False,
            command=["python", "-m", "app.builder", "--contrib", "example/project", "--1"],
            started_at="2026-05-05T11:00:00+00:00",
            state="completed",
            returncode=0,
            summary={"submitted_prs": [], "queued": [{"repo": "example/project"}]},
        )

        with (
            patch("src.contribution_mcp.server._find_run", return_value=run),
            patch("src.contribution_mcp.server._sync_repo_events"),
            patch("src.contribution_mcp.server._update_run_summary"),
            patch(
                "src.contribution_mcp.server._store.find_open_pr",
                return_value={
                    "repo_full_name": "example/project",
                    "pr_url": "https://github.com/example/project/pull/264",
                    "pr_title": "fix: validate config env input",
                    "status": "open",
                    "source": "legacy:/tmp/pr_log.json",
                },
            ),
        ):
            result = get_run_result("run-1")

        self.assertEqual(result["outcome_code"], "existing_pr_already_open")
        self.assertEqual(result["existing_open_prs"][0]["pr_url"], "https://github.com/example/project/pull/264")
        self.assertEqual(result["summary"]["outcome_code"], "existing_pr_already_open")
        self.assertEqual(result["summary"]["existing_open_prs"][0]["pr_title"], "fix: validate config env input")

    def test_get_run_result_marks_dry_run_completion_explicitly(self) -> None:
        from src.contribution_mcp.server import ManagedRun, get_run_result

        run = ManagedRun(
            run_id="run-2",
            mode="targeted",
            repo="example/project",
            goal="bugfix",
            count=1,
            dry_run=True,
            first_pr=False,
            override_limits=False,
            command=["python", "-m", "app.builder", "--contrib", "example/project", "--1", "--dry-run"],
            started_at="2026-05-05T11:00:00+00:00",
            state="completed",
            returncode=0,
            summary={"submitted_prs": [], "queued": [{"repo": "example/project"}]},
        )

        with (
            patch("src.contribution_mcp.server._find_run", return_value=run),
            patch("src.contribution_mcp.server._sync_repo_events"),
            patch("src.contribution_mcp.server._update_run_summary"),
            patch("src.contribution_mcp.server._store.find_open_pr", return_value=None),
        ):
            result = get_run_result("run-2")

        self.assertEqual(result["outcome_code"], "dry_run_complete")
        self.assertEqual(result["summary"]["outcome_code"], "dry_run_complete")

    def test_start_run_includes_notification_route_when_explicit_target_is_passed(self) -> None:
        from src.contribution_mcp.server import ManagedRun, NotificationRoute, start_run

        run = ManagedRun(
            run_id="run-1",
            mode="targeted",
            repo="example/project",
            goal="bugfix",
            count=1,
            dry_run=False,
            first_pr=False,
            override_limits=False,
            command=["python", "-m", "app.builder"],
            started_at="2026-05-05T11:00:00+00:00",
            notification_route=NotificationRoute(
                transport="openclaw",
                channel="telegram",
                target="-100123",
                account="default",
                thread_id="7",
            ),
        )

        with (
            patch("src.contribution_mcp.server._targeted_preflight", return_value=None),
            patch("src.contribution_mcp.server._spawn_run", return_value=run),
            patch("src.contribution_mcp.server._status_payload", return_value={"run_id": "run-1", "state": "started"}),
        ):
            result = start_run(
                repo="example/project",
                dry_run=False,
                notify_transport="openclaw",
                notify_channel="telegram",
                notify_target="-100123",
                notify_account="default",
                notify_thread_id="7",
            )

        self.assertEqual(result["notification"]["transport"], "openclaw")
        self.assertEqual(result["notification"]["target"], "-100123")
        self.assertEqual(result["notification"]["thread_id"], "7")

    def test_start_run_blocks_targeted_repo_when_inspect_only(self) -> None:
        from src.contribution_mcp.server import start_run

        blocked = {
            "accepted": False,
            "status": "blocked",
            "state": "blocked",
            "repo": "jahwag/ClaudeSync",
            "outcome_code": "blocked_ineligible_repo",
            "reason": "inspect-only",
            "scope_notes": ["targeted mode: inactive repo (58d since last push; limit 45d)"],
            "next_steps": ["Keep this repo in inspect-only mode."],
            "inspect": {"targeted_scope": "inspect-only"},
        }
        with (
            patch("src.contribution_mcp.server._targeted_preflight", return_value=blocked),
            patch("src.contribution_mcp.server._spawn_run") as mocked_spawn,
        ):
            result = start_run(repo="jahwag/ClaudeSync", goal="bugfix", count=1, dry_run=False)

        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["outcome_code"], "blocked_ineligible_repo")
        self.assertIn("inactive repo", result["scope_notes"][0])
        mocked_spawn.assert_not_called()

    def test_get_config_masks_secrets(self) -> None:
        import os
        import tempfile
        from pathlib import Path
        from src.contribution_mcp import server as srv

        fake_env = "GITHUB_TOKEN=ghp_abc123\nGH_TOKEN=ghu_abc123\nCONTRIB_LANE=devtools\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(fake_env)
            tmp = Path(f.name)
        try:
            with patch("src.contribution_mcp.server.ROOT", tmp.parent):
                tmp.rename(tmp.parent / ".env")
                from src.contribution_mcp.server import get_config

                result = get_config()
                self.assertTrue(result["GITHUB_TOKEN"].endswith("****"))
                self.assertTrue(result["GH_TOKEN"].endswith("****"))
                self.assertEqual(result["CONTRIB_LANE"], "devtools")
        finally:
            env_path = tmp.parent / ".env"
            if env_path.exists():
                os.unlink(env_path)

    def test_update_config_blocks_secret_keys(self) -> None:
        from src.contribution_mcp.server import update_config

        result = update_config("GITHUB_TOKEN", "new_value")
        self.assertEqual(result["status"], "rejected")

    def test_stop_contribution_when_not_running(self) -> None:
        from src.contribution_mcp.server import stop_contribution

        result = stop_contribution()
        self.assertEqual(result["status"], "not_running")

    def test_route_command_maps_natural_language_request(self) -> None:
        from src.contribution_mcp.server import route_command

        result = route_command("buat satu pull request ke https://github.com/example/project")
        self.assertEqual(result["action"], "contrib_targeted")
        self.assertEqual(result["repo"], "example/project")
        self.assertTrue(result["dry_run"])

    def test_run_contribution_wraps_start_run(self) -> None:
        from src.contribution_mcp.server import run_contribution

        with patch(
            "src.contribution_mcp.server.start_run",
            return_value={"run_id": "abc", "state": "started", "running": True, "accepted": True},
        ) as mocked_start:
            result = run_contribution(repo="example/project", dry_run=True)

        self.assertEqual(result["status"], "started")
        mocked_start.assert_called_once_with(
            repo="example/project",
            goal="bugfix",
            count=1,
            dry_run=True,
            first_pr=False,
            override_limits=False,
            notify_transport="",
            notify_channel="",
            notify_target="",
            notify_account="",
            notify_thread_id="",
        )

    def test_run_contribution_preserves_blocked_targeted_result(self) -> None:
        from src.contribution_mcp.server import run_contribution

        with patch(
            "src.contribution_mcp.server.start_run",
            return_value={"accepted": False, "status": "blocked", "outcome_code": "blocked_ineligible_repo"},
        ):
            result = run_contribution(repo="jahwag/ClaudeSync", dry_run=False)

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["accepted"])

    def test_contrib_once_uses_run_contribution_helper(self) -> None:
        from src.contribution_mcp.server import contrib_once

        with patch("src.contribution_mcp.server.run_contribution", return_value={"status": "started"}) as mocked_run:
            result = contrib_once(count=2, goal="feature_upgrade", dry_run=False, first_pr=True, override_limits=True)

        self.assertEqual(result["status"], "started")
        mocked_run.assert_called_once_with(
            repo="",
            goal="feature_upgrade",
            count=2,
            dry_run=False,
            first_pr=True,
            override_limits=True,
            notify_transport="",
            notify_channel="",
            notify_target="",
            notify_account="",
            notify_thread_id="",
        )

    def test_contrib_targeted_uses_run_contribution_helper(self) -> None:
        from src.contribution_mcp.server import contrib_targeted

        with patch("src.contribution_mcp.server.run_contribution", return_value={"status": "started"}) as mocked_run:
            result = contrib_targeted("example/project", count=1, override_limits=True)

        self.assertEqual(result["status"], "started")
        mocked_run.assert_called_once_with(
            repo="example/project",
            goal="bugfix",
            count=1,
            dry_run=True,
            first_pr=False,
            override_limits=True,
            notify_transport="",
            notify_channel="",
            notify_target="",
            notify_account="",
            notify_thread_id="",
        )

    def test_run_notification_loop_uses_single_progress_card_and_terminal_summary(self) -> None:
        from src.contribution_mcp.server import ManagedRun, NotificationRoute, _run_notification_loop

        run = ManagedRun(
            run_id="run-1",
            mode="targeted",
            repo="example/project",
            goal="bugfix",
            count=1,
            dry_run=False,
            first_pr=False,
            override_limits=False,
            command=["python", "-m", "app.builder"],
            started_at="2026-05-05T11:00:00+00:00",
            state="started",
            notification_route=NotificationRoute(transport="openclaw", channel="telegram", target="-100123"),
            summary={"submitted_prs": [], "queued": [{"repo": "example/project"}], "ai_calls": 4, "est_tokens": 44500},
        )
        run.events = [
            {"seq": 1, "type": "started", "summary": "Contribution run started in background.", "details": {}, "created_at": "t1"},
            {"seq": 2, "type": "repo_selected", "summary": "Selected repo", "details": {}, "created_at": "t2"},
            {
                "seq": 3,
                "type": "patch_generated",
                "summary": "Patch generated: fix config validation",
                "details": {"title": "fix config validation", "files": ["smoke/lib/config.py"]},
                "created_at": "t3",
            },
        ]
        run.next_seq = 4
        run.last_activity_at = 0.0

        sent_cards: list[str] = []
        edited_cards: list[str] = []
        def fake_send(message: str, route=None) -> dict[str, object]:
            sent_cards.append(message)
            return {"ok": True, "message_id": 42}

        def fake_edit(message: str, message_id: int, route=None) -> bool:
            edited_cards.append(message)
            run.state = "completed"
            run.finished_at = "2026-05-05T11:01:00+00:00"
            return True

        with (
            patch("src.contribution_mcp.server.notify") as mocked_notify,
            patch("src.contribution_mcp.server.telegram_send_message", side_effect=fake_send),
            patch("src.contribution_mcp.server.telegram_edit_message", side_effect=fake_edit),
            patch("src.contribution_mcp.server._sync_repo_events"),
            patch("src.contribution_mcp.server._update_run_summary"),
            patch("src.contribution_mcp.server.time.sleep"),
            patch("src.contribution_mcp.server.time.time", return_value=200.0),
            patch("src.contribution_mcp.server.ROVER_NOTIFY_PROGRESS", True),
            patch("src.contribution_mcp.server.ROVER_NOTIFY_ONLY_ON_CHANGE", True),
            patch("src.contribution_mcp.server.ROVER_NOTIFY_STALL_SECONDS", 60),
            patch("src.contribution_mcp.server.ROVER_NOTIFY_ON_EVENT_TYPES", ("started", "repo_selected", "completed", "stalled")),
        ):
            _run_notification_loop(run)

        self.assertTrue(any("ROVER PROGRESS" in message for message in sent_cards))
        self.assertTrue(any("ROVER PROGRESS" in message for message in edited_cards))
        self.assertTrue(any("ROVER NO SUBMISSION" in message for message in edited_cards))
        self.assertTrue(any("🕒 Last update at :" in message for message in sent_cards))
        self.assertTrue(any("🕒 Last update at :" in message for message in edited_cards))
        self.assertTrue(any("🚦 Mode     : live" in message for message in edited_cards))
        self.assertTrue(any("🛠️ Stage  :" in message for message in sent_cards))
        self.assertTrue(any("🧪 Phase" in message for message in sent_cards))
        self.assertTrue(any("🎯 Top narrowed candidate" in message for message in edited_cards))
        self.assertTrue(any("- Tokens  : ~44500" in message for message in edited_cards))
        self.assertTrue(any("- Files: smoke/lib/config.py" in message for message in edited_cards))
        mocked_notify.assert_not_called()

    def test_terminal_summary_shows_shortlist_threshold_miss(self) -> None:
        from src.contribution_mcp.server import ManagedRun, _render_terminal_summary

        run = ManagedRun(
            run_id="run-1",
            mode="targeted",
            repo="example/project",
            goal="bugfix",
            count=1,
            dry_run=False,
            first_pr=False,
            override_limits=False,
            command=["python", "-m", "app.builder"],
            started_at="2026-05-05T11:00:00+00:00",
            finished_at="2026-05-05T11:05:00+00:00",
            state="completed",
            summary={
                "submitted_prs": [],
                "ai_calls": 0,
                "est_tokens": 0,
                "attempts": 3,
                "shortlisted": 2,
                "planned": 0,
                "generated": 0,
                "broad_rejected_early": 30,
                "best_patchability_score": 68,
                "min_patchability_score": 72,
                "shortlist_summary": [
                    {"target_file": "src/a.py", "pattern_type": "unchecked_response_shape", "score": 68},
                    {"target_file": "src/b.py", "pattern_type": "missing_timeout", "score": 64},
                ],
                "top_rejections": [("target_area_too_broad", 30)],
                "bottleneck": "0 PR because target_area_too_broad dominated 30 opportunity decisions",
                "current_target_file": "src/a.py",
                "current_stage": "qualify",
            },
        )

        rendered = _render_terminal_summary(run)

        self.assertIn("ROVER NO NARROW CANDIDATE", rendered)
        self.assertIn("🎯 Top narrowed candidate: src/a.py", rendered)
        self.assertIn("Shortlist", rendered)
        self.assertIn("src/a.py | unchecked_response_shape | score=68", rendered)
        self.assertIn("best patchability 68 < required 72", rendered)

    def test_progress_card_prefers_formal_run_stage_over_inferred_stage(self) -> None:
        from src.contribution_mcp.server import ManagedRun, _render_progress_card

        run = ManagedRun(
            run_id="run-1",
            mode="targeted",
            repo="example/project",
            goal="bugfix",
            count=1,
            dry_run=False,
            first_pr=False,
            override_limits=False,
            command=["python", "-m", "app.builder"],
            started_at="2026-05-05T11:00:00+00:00",
            state="running",
            summary={"current_stage": "generate", "current_target_file": "src/a.py"},
        )
        run.last_stage_key = "preparing fork and branch"
        run.events = [
            {"seq": 1, "type": "stage", "summary": "Preparing fork and branch", "details": {}, "created_at": "2026-05-05T11:00:10+00:00"}
        ]

        rendered = _render_progress_card(run)

        self.assertIn("🛠️ Stage  : Generate", rendered)
        self.assertIn("🧭 Last   : Run Stage", rendered)
        self.assertIn("📝 Step   : Run stage: Generate", rendered)
        self.assertNotIn("Preparing Fork And Branch", rendered)

    def test_upsert_progress_card_does_not_fallback_send_when_edit_fails(self) -> None:
        from src.contribution_mcp.server import ManagedRun, _upsert_progress_card
        from src.core.notify import NotificationRoute

        run = ManagedRun(
            run_id="run-1",
            mode="targeted",
            repo="example/project",
            goal="bugfix",
            count=1,
            dry_run=False,
            first_pr=False,
            override_limits=False,
            command=["python", "-m", "app.builder"],
            started_at="2026-05-05T11:00:00+00:00",
            state="running",
            notification_route=NotificationRoute(transport="telegram", target="-100123"),
        )
        run.progress_message_id = 42
        run.events = [
            {"seq": 1, "type": "started", "summary": "Contribution run started in background.", "details": {}, "created_at": "t1"}
        ]

        with (
            patch("src.contribution_mcp.server.telegram_edit_message", return_value=False) as mocked_edit,
            patch("src.contribution_mcp.server.telegram_send_message") as mocked_send,
        ):
            _upsert_progress_card(run)

        mocked_edit.assert_called_once()
        mocked_send.assert_not_called()

    def test_append_stage_from_log_emits_stage_once(self) -> None:
        from src.contribution_mcp.server import ManagedRun, _append_stage_from_log

        run = ManagedRun(
            run_id="run-1",
            mode="targeted",
            repo="example/project",
            goal="bugfix",
            count=1,
            dry_run=False,
            first_pr=False,
            override_limits=False,
            command=["python", "-m", "app.builder"],
            started_at="2026-05-05T11:00:00+00:00",
        )

        _append_stage_from_log(run, "Scanning repository files for opportunities")
        _append_stage_from_log(run, "Scanning repository files for opportunities")

        stage_events = [event for event in run.events if event["type"] == "stage"]
        self.assertEqual(len(stage_events), 1)
        self.assertEqual(stage_events[0]["summary"], "scanning repository files")
        self.assertEqual(run.state, "running")
