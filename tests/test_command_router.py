from __future__ import annotations

import io
import unittest
from unittest import mock

from app import builder
from src.command_router import parse_command_text


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

    def test_builder_command_text_doctor_prints_report(self) -> None:
        with mock.patch("app.builder.build_doctor_report", return_value="DOCTOR_OK"):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                builder.main(["--command-text", "cek kesehatan contribution engine"])
        self.assertIn("DOCTOR_OK", stdout.getvalue())

    def test_builder_command_text_report_prints_report(self) -> None:
        with mock.patch("app.builder.build_contribution_report", return_value="REPORT_OK"):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                builder.main(["--command-text", "tampilkan report kontribusi terakhir"])
        self.assertIn("REPORT_OK", stdout.getvalue())
