from __future__ import annotations

import io
import unittest
from unittest import mock

from app import builder
from src.core.command_router import parse_command_text


class CommandRouterTests(unittest.TestCase):
    def test_maps_indonesian_contribution_request_to_search_mode(self) -> None:
        request = parse_command_text("buat 1 kontribusi")

        self.assertEqual(request.action, "contrib_once")
        self.assertEqual(request.count, 1)
        self.assertTrue(request.dry_run)

    def test_maps_targeted_pull_request_phrase_to_repo_action(self) -> None:
        request = parse_command_text("buat satu pull request ke https://github.com/HKUDS/Vibe-Trading")

        self.assertEqual(request.action, "contrib_targeted")
        self.assertEqual(request.repo, "HKUDS/Vibe-Trading")
        self.assertTrue(request.dry_run)

    def test_maps_repo_inspect_phrase(self) -> None:
        request = parse_command_text("cek repo owner/repo dulu")

        self.assertEqual(request.action, "repo_inspect")
        self.assertEqual(request.repo, "owner/repo")

    def test_maps_repo_security_scan_phrase(self) -> None:
        request = parse_command_text("scan security owner/repo")

        self.assertEqual(request.action, "repo_scan")
        self.assertEqual(request.repo, "owner/repo")
        self.assertEqual(request.scan_kind, "security")

    def test_maps_repo_bug_scan_phrase(self) -> None:
        request = parse_command_text("scan bug owner/repo")

        self.assertEqual(request.action, "repo_scan")
        self.assertEqual(request.repo, "owner/repo")
        self.assertEqual(request.scan_kind, "bug")

    def test_maps_repo_trust_scan_phrase(self) -> None:
        request = parse_command_text("scan trust owner/repo")

        self.assertEqual(request.action, "repo_scan")
        self.assertEqual(request.repo, "owner/repo")
        self.assertEqual(request.scan_kind, "trust")

    def test_maps_repo_audit_scan_phrase(self) -> None:
        request = parse_command_text("scan audit owner/repo")

        self.assertEqual(request.action, "repo_scan")
        self.assertEqual(request.repo, "owner/repo")
        self.assertEqual(request.scan_kind, "audit")

    def test_maps_profile_phrase(self) -> None:
        request = parse_command_text("siapa login sekarang")

        self.assertEqual(request.action, "profile")

    def test_maps_doctor_phrase(self) -> None:
        request = parse_command_text("jalankan doctor")

        self.assertEqual(request.action, "doctor")

    def test_maps_feedback_response_phrase(self) -> None:
        request = parse_command_text("balas maintainer feedback")

        self.assertEqual(request.action, "contrib_respond")

    def test_enables_live_run_only_for_explicit_submit_words(self) -> None:
        request = parse_command_text("submit real pr ke owner/repo")

        self.assertEqual(request.action, "contrib_targeted")
        self.assertFalse(request.dry_run)

    def test_run_targeted_phrase_defaults_to_live_submission(self) -> None:
        request = parse_command_text("run owner/repo bugfix")

        self.assertEqual(request.action, "contrib_targeted")
        self.assertEqual(request.repo, "owner/repo")
        self.assertFalse(request.dry_run)

    def test_maps_fix_bug_shortcut_to_bugfix_lane(self) -> None:
        request = parse_command_text("Rover, fix bug di owner/repo")

        self.assertEqual(request.action, "contrib_targeted")
        self.assertEqual(request.repo, "owner/repo")
        self.assertEqual(request.goal, "bugfix")

    def test_maps_update_deps_shortcut_to_dep_update_lane(self) -> None:
        request = parse_command_text("Rover, update deps di owner/repo")

        self.assertEqual(request.action, "contrib_targeted")
        self.assertEqual(request.repo, "owner/repo")
        self.assertEqual(request.goal, "dep_update")

    def test_malformed_repo_shortcut_falls_back_to_safe_doctor(self) -> None:
        request = parse_command_text("Rover, fix bug di repo-abc")

        self.assertEqual(request.action, "doctor")
        self.assertEqual(request.confidence, "low")
        self.assertTrue(any("defaulting to a safe doctor action" in reason for reason in request.rationale))

    def test_builder_warns_when_rover_engine_alias_is_used(self) -> None:
        with mock.patch("sys.argv", ["rover-engine", "--doctor"]):
            with mock.patch("app.builder.build_doctor_report", return_value="DOCTOR_OK"):
                with mock.patch("sys.stdout", new_callable=io.StringIO):
                    with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                        builder.main()
        self.assertIn("`rover-engine` is a compatibility alias", stderr.getvalue())
        self.assertIn("Try: `rover doctor`", stderr.getvalue())

    def test_builder_warns_when_legacy_pr_flag_is_used(self) -> None:
        with mock.patch("app.builder.setup_logging"), mock.patch(
            "app.builder.cleanup_old_logs"
        ), mock.patch("app.builder.run_contribution_mode"):
            with mock.patch("sys.argv", ["rover", "--pr", "owner/repo"]):
                with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    builder.main()
        self.assertIn("`--pr` is deprecated. Prefer `--contrib`.", stderr.getvalue())
        self.assertIn("Equivalent command: `rover run owner/repo`.", stderr.getvalue())

    def test_builder_warns_when_rover_engine_alias_can_suggest_targeted_run(self) -> None:
        with mock.patch("sys.argv", ["rover-engine", "--contrib", "owner/repo", "--dry-run"]):
            with mock.patch("app.builder.setup_logging"), mock.patch(
                "app.builder.cleanup_old_logs"
            ), mock.patch("app.builder.run_contribution_mode"):
                with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    builder.main()
        self.assertIn("Try: `rover run owner/repo --dry-run`", stderr.getvalue())

    def test_builder_emits_single_deprecation_warning_for_alias_and_legacy_flag(self) -> None:
        with mock.patch("sys.argv", ["rover-engine", "--pr", "owner/repo"]):
            with mock.patch("app.builder.setup_logging"), mock.patch(
                "app.builder.cleanup_old_logs"
            ), mock.patch("app.builder.run_contribution_mode"):
                with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    builder.main()
        lines = [line for line in stderr.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertIn("`rover-engine` is a compatibility alias", lines[0])
        self.assertIn("`--pr` is deprecated", lines[0])

    def test_builder_command_text_doctor_prints_report(self) -> None:
        with mock.patch("app.builder.build_doctor_report", return_value="DOCTOR_OK"):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                builder.main(["--command-text", "cek kesehatan contribution engine"])
        self.assertIn("DOCTOR_OK", stdout.getvalue())

    def test_builder_command_text_report_prints_report(self) -> None:
        with mock.patch("src.contrib.pr_generator.get_contribution_report_data", return_value=([], [])):
            with mock.patch("src.core.cli_ui.print_styled_report") as mock_report:
                builder.main(["--command-text", "tampilkan report kontribusi terakhir"])
        mock_report.assert_called_once()

    def test_builder_doctor_json_prints_machine_readable_payload(self) -> None:
        from types import SimpleNamespace

        fake_check = SimpleNamespace(name="python", status="ok", detail="running Python 3.10")
        with mock.patch("app.builder.collect_doctor_checks", return_value=[fake_check]), mock.patch(
            "app.builder.build_doctor_report", return_value="DOCTOR_OK"
        ):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                builder.main(["--doctor", "--json"])
        self.assertIn('"action": "doctor"', stdout.getvalue())
        self.assertIn("DOCTOR_OK", stdout.getvalue())

    def test_builder_route_only_returns_mapping_without_execution(self) -> None:
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout, mock.patch(
            "app.builder.run_contribution_mode"
        ) as mocked_run:
            builder.main(["--command-text", "buat 1 kontribusi", "--route-only", "--json"])
        self.assertIn('"action": "route_command"', stdout.getvalue())
        self.assertIn('"mapped_action": "contrib_once"', stdout.getvalue())
        mocked_run.assert_not_called()

    def test_builder_scan_invalid_repo_prints_operator_error(self) -> None:
        with mock.patch("app.builder.setup_logging"), mock.patch("app.builder.cleanup_old_logs"), mock.patch(
            "src.contrib.repo_scan.build_scan_payload", side_effect=builder.ScraperError("Cannot parse repo URL/name: '...'")
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            builder.main(["--scan-repo", "...", "--scan-kind", "security"])

        self.assertIn("cannot scan repo: Cannot parse repo URL/name: '...'", stdout.getvalue())

    def test_builder_scan_invalid_repo_json_is_structured(self) -> None:
        with mock.patch("app.builder.setup_logging"), mock.patch("app.builder.cleanup_old_logs"), mock.patch(
            "src.contrib.repo_scan.build_scan_payload", side_effect=builder.ScraperError("Cannot parse repo URL/name: '...'")
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            builder.main(["--scan-repo", "...", "--scan-kind", "security", "--json"])

        self.assertIn('"action": "repo_scan"', stdout.getvalue())
        self.assertIn('"ok": false', stdout.getvalue())
        self.assertIn("Cannot parse repo URL/name", stdout.getvalue())

    def test_builder_profile_json_is_structured(self) -> None:
        with mock.patch("app.builder.get_current_github_login", return_value="nadira"), mock.patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout:
            builder.main(["--profile", "--json"])

        self.assertIn('"action": "profile"', stdout.getvalue())
        self.assertIn('"github_login": "nadira"', stdout.getvalue())
