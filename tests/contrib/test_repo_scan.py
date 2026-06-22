from __future__ import annotations

import logging
import unittest
from unittest.mock import patch

from src.github.scraper import RepoCandidate


class RepoScanTests(unittest.TestCase):
    def test_security_scan_reports_high_confidence_findings(self) -> None:
        from src.contrib.repo_scan import build_scan_payload

        candidate = RepoCandidate(
            name="sample",
            full_name="example/sample",
            description="sample repo",
            stars=10,
            forks=2,
            license="mit",
            url="https://github.com/example/sample",
            default_branch="main",
            pushed_days_ago=1,
            files={
                "src/runner.py": 'subprocess.run(cmd, shell=True)\nconfig = yaml.load(raw)\nAPI_TOKEN = "sk_live_1234567890abcdef"\n',
            },
        )
        with (
            patch("src.contrib.repo_scan._fetch_scan_candidate", return_value=candidate),
            patch("src.contrib.repo_scan.get_repo_inspect_data", return_value={"targeted_scope": "targeted-ready", "scope_notes": []}),
        ):
            payload = build_scan_payload("example/sample", logging.getLogger("test"), kind="security")

        self.assertEqual(payload["action"], "repo_scan")
        self.assertEqual(payload["kind"], "security")
        self.assertGreaterEqual(payload["finding_count"], 2)
        self.assertEqual(payload["language_summary"]["py"], 1)
        self.assertGreaterEqual(payload["supported_file_count"], 1)
        self.assertTrue(any(item["rule_id"] == "shell_true_subprocess" for item in payload["findings"]))
        self.assertTrue(any(item["rule_id"] == "unsafe_yaml_load" for item in payload["findings"]))
        self.assertIn("Security Scan", payload["rendered"])

    def test_bug_scan_uses_pattern_scanner_and_qualification(self) -> None:
        from src.contrib.repo_scan import build_scan_payload

        candidate = RepoCandidate(
            name="sample",
            full_name="example/sample",
            description="sample repo",
            stars=10,
            forks=2,
            license="mit",
            url="https://github.com/example/sample",
            default_branch="main",
            pushed_days_ago=1,
            files={
                "client.py": "import requests\n\nrequests.get(url)\n",
                "tests/test_client.py": "def test_placeholder():\n    assert True\n",
                "requirements.txt": "requests==2.0.0\n",
            },
        )
        with (
            patch("src.contrib.repo_scan._fetch_scan_candidate", return_value=candidate),
            patch("src.contrib.repo_scan.get_repo_inspect_data", return_value={"targeted_scope": "targeted-ready", "scope_notes": []}),
        ):
            payload = build_scan_payload("example/sample", logging.getLogger("test"), kind="bug")

        self.assertEqual(payload["kind"], "bug")
        self.assertGreaterEqual(payload["finding_count"], 1)
        self.assertTrue(any(item["rule_id"] == "missing_timeout" for item in payload["findings"]))
        self.assertIn("Bug Scan", payload["rendered"])
        self.assertIn("user-facing reliability findings only", payload["rendered"])
        self.assertFalse(any(item["file"].startswith("tests/") for item in payload["findings"]))
        self.assertFalse(any(item["rule_id"] == "missing_regression_test_for_obvious_bugfix" for item in payload["findings"]))

    def test_security_scan_reports_no_supported_files_clearly(self) -> None:
        from src.contrib.repo_scan import build_scan_payload

        candidate = RepoCandidate(
            name="docs",
            full_name="example/docs",
            description="docs repo",
            stars=10,
            forks=2,
            license="mit",
            url="https://github.com/example/docs",
            default_branch="main",
            pushed_days_ago=1,
            files={"README.md": "# docs\n", "notes.txt": "hello\n"},
        )
        with (
            patch("src.contrib.repo_scan._fetch_scan_candidate", return_value=candidate),
            patch("src.contrib.repo_scan.get_repo_inspect_data", return_value={"targeted_scope": "targeted-ready", "scope_notes": []}),
        ):
            payload = build_scan_payload("example/docs", logging.getLogger("test"), kind="security")

        self.assertEqual(payload["supported_file_count"], 0)
        self.assertIn("no supported source files found", payload["coverage_note"])
        self.assertIn("Coverage note: no supported source files found", payload["rendered"])

    def test_security_scan_flags_archive_distribution_and_social_risk(self) -> None:
        from src.contrib.repo_scan import build_scan_payload

        candidate = RepoCandidate(
            name="payloads",
            full_name="example/payloads",
            description="payload repo",
            stars=10,
            forks=2,
            license="mit",
            url="https://github.com/example/payloads",
            default_branch="main",
            pushed_days_ago=1,
            files={
                "README.md": "Please disable antivirus first.\nThen run as admin.\n",
                "requirements.txt": "requests==2.0.0\n",
            },
        )
        fake_tree = {
            "tree": [
                {"type": "blob", "path": "payload.zip"},
                {"type": "blob", "path": "loader.exe"},
            ]
        }
        with (
            patch("src.contrib.repo_scan._fetch_scan_candidate", return_value=candidate),
            patch("src.contrib.repo_scan.get_repo_inspect_data", return_value={"targeted_scope": "targeted-ready", "scope_notes": []}),
            patch("src.contrib.repo_scan._gh_get", return_value=fake_tree),
        ):
            payload = build_scan_payload("example/payloads", logging.getLogger("test"), kind="security")

        rule_ids = {item["rule_id"] for item in payload["findings"]}
        self.assertIn("repository_distributes_archives", rule_ids)
        self.assertIn("repository_distributes_executables", rule_ids)
        self.assertIn("low_source_high_artifact_repo", rule_ids)
        self.assertIn("social_risk_instruction", rule_ids)
        self.assertIn("disable antivirus", payload["rendered"].lower())

    def test_trust_scan_reports_distribution_and_social_risk(self) -> None:
        from src.contrib.repo_scan import build_scan_payload

        candidate = RepoCandidate(
            name="payloads",
            full_name="example/payloads",
            description="payload repo",
            stars=10,
            forks=2,
            license="mit",
            url="https://github.com/example/payloads",
            default_branch="main",
            pushed_days_ago=1,
            files={
                "README.md": "Please disable antivirus first.\nThen run as admin.\n",
                "requirements.txt": "requests==2.0.0\n",
            },
        )
        fake_tree = {
            "tree": [
                {"type": "blob", "path": "payload.zip"},
                {"type": "blob", "path": "loader.exe"},
            ]
        }
        with (
            patch("src.contrib.repo_scan._fetch_trust_scan_candidate", return_value=candidate),
            patch("src.contrib.repo_scan._gh_get", return_value=fake_tree),
        ):
            payload = build_scan_payload("example/payloads", logging.getLogger("test"), kind="trust")

        rule_ids = {item["rule_id"] for item in payload["findings"]}
        self.assertEqual(payload["kind"], "trust")
        self.assertIn("repository_distributes_archives", rule_ids)
        self.assertIn("repository_distributes_executables", rule_ids)
        self.assertIn("social_risk_instruction", rule_ids)
        self.assertIn("Trust Scan", payload["rendered"])
        self.assertIn("trust and distribution signals", payload["rendered"])

    def test_audit_scan_combines_trust_and_security_findings(self) -> None:
        from src.contrib.repo_scan import build_scan_payload

        security_candidate = RepoCandidate(
            name="payloads",
            full_name="example/payloads",
            description="payload repo",
            stars=10,
            forks=2,
            license="mit",
            url="https://github.com/example/payloads",
            default_branch="main",
            pushed_days_ago=1,
            files={
                "src/runner.py": "import requests\nrequests.get(url, verify=False)\n",
            },
        )
        trust_candidate = RepoCandidate(
            name="payloads",
            full_name="example/payloads",
            description="payload repo",
            stars=10,
            forks=2,
            license="mit",
            url="https://github.com/example/payloads",
            default_branch="main",
            pushed_days_ago=1,
            files={
                "README.md": "Please disable antivirus first.\n",
            },
        )
        fake_tree = {"tree": [{"type": "blob", "path": "payload.zip"}]}
        with (
            patch("src.contrib.repo_scan._fetch_scan_candidate", return_value=security_candidate),
            patch("src.contrib.repo_scan._fetch_trust_scan_candidate", return_value=trust_candidate),
            patch("src.contrib.repo_scan.get_repo_inspect_data", return_value={"targeted_scope": "targeted-ready", "scope_notes": []}),
            patch("src.contrib.repo_scan._gh_get", return_value=fake_tree),
        ):
            payload = build_scan_payload("example/payloads", logging.getLogger("test"), kind="audit")

        self.assertEqual(payload["kind"], "audit")
        self.assertEqual(payload["finding_kind_counts"]["trust"], 3)
        self.assertEqual(payload["finding_kind_counts"]["security"], 1)
        self.assertIn("Audit Scan", payload["rendered"])
        self.assertIn("Finding types: trust=3 security=1", payload["rendered"])
        self.assertIn("[trust medium/medium]", payload["rendered"])
        self.assertIn("[security high/high]", payload["rendered"])

    def test_security_scan_does_not_flag_js_yaml_v4_load_as_unsafe(self) -> None:
        from src.contrib.repo_scan import build_scan_payload

        candidate = RepoCandidate(
            name="node-safe",
            full_name="example/node-safe",
            description="node repo",
            stars=10,
            forks=2,
            license="mit",
            url="https://github.com/example/node-safe",
            default_branch="main",
            pushed_days_ago=1,
            files={
                "package.json": '{"dependencies":{"js-yaml":"^4.1.0"}}',
                "src/loadBotConfig.ts": "const cfg = yaml.load(rawConfig)\\n",
            },
        )
        with (
            patch("src.contrib.repo_scan._fetch_scan_candidate", return_value=candidate),
            patch("src.contrib.repo_scan.get_repo_inspect_data", return_value={"targeted_scope": "targeted-ready", "scope_notes": []}),
        ):
            payload = build_scan_payload("example/node-safe", logging.getLogger("test"), kind="security")

        rule_ids = {item["rule_id"] for item in payload["findings"]}
        self.assertNotIn("unsafe_yaml_load", rule_ids)

    def test_security_scan_does_not_flag_plain_node_exec_usage(self) -> None:
        from src.contrib.repo_scan import build_scan_payload

        candidate = RepoCandidate(
            name="node-exec",
            full_name="example/node-exec",
            description="node repo",
            stars=10,
            forks=2,
            license="mit",
            url="https://github.com/example/node-exec",
            default_branch="main",
            pushed_days_ago=1,
            files={
                "src/runBot.ts": 'execSync("git rev-parse --short HEAD")\\n',
            },
        )
        with (
            patch("src.contrib.repo_scan._fetch_scan_candidate", return_value=candidate),
            patch("src.contrib.repo_scan.get_repo_inspect_data", return_value={"targeted_scope": "targeted-ready", "scope_notes": []}),
        ):
            payload = build_scan_payload("example/node-exec", logging.getLogger("test"), kind="security")

        rule_ids = {item["rule_id"] for item in payload["findings"]}
        self.assertNotIn("shell_command_exec", rule_ids)

    def test_security_scan_does_not_flag_privileged_script_repo_by_itself(self) -> None:
        from src.contrib.repo_scan import build_scan_payload

        candidate = RepoCandidate(
            name="mixed-source",
            full_name="example/mixed-source",
            description="mixed repo",
            stars=10,
            forks=2,
            license="mit",
            url="https://github.com/example/mixed-source",
            default_branch="main",
            pushed_days_ago=1,
            files={
                "src/main.py": "print('ok')\\n",
            },
        )
        fake_tree = {
            "tree": [
                {"type": "blob", "path": "scripts/publish-to-polypulse.ps1"},
                {"type": "blob", "path": "src/main.py"},
            ]
        }
        with (
            patch("src.contrib.repo_scan._fetch_scan_candidate", return_value=candidate),
            patch("src.contrib.repo_scan.get_repo_inspect_data", return_value={"targeted_scope": "targeted-ready", "scope_notes": []}),
            patch("src.contrib.repo_scan._gh_get", return_value=fake_tree),
        ):
            payload = build_scan_payload("example/mixed-source", logging.getLogger("test"), kind="security")

        rule_ids = {item["rule_id"] for item in payload["findings"]}
        self.assertNotIn("repository_distributes_executables", rule_ids)
        self.assertNotIn("low_source_high_artifact_repo", rule_ids)

    def test_security_scan_flags_disabled_tls_and_unverified_ssl(self) -> None:
        from src.contrib.repo_scan import build_scan_payload

        candidate = RepoCandidate(
            name="tls-bad",
            full_name="example/tls-bad",
            description="tls repo",
            stars=10,
            forks=2,
            license="mit",
            url="https://github.com/example/tls-bad",
            default_branch="main",
            pushed_days_ago=1,
            files={
                "client.py": (
                    "import requests\n"
                    "import ssl\n"
                    "requests.get(url, verify=False)\n"
                    "ctx = ssl._create_unverified_context()\n"
                ),
            },
        )
        with (
            patch("src.contrib.repo_scan._fetch_scan_candidate", return_value=candidate),
            patch("src.contrib.repo_scan.get_repo_inspect_data", return_value={"targeted_scope": "targeted-ready", "scope_notes": []}),
        ):
            payload = build_scan_payload("example/tls-bad", logging.getLogger("test"), kind="security")

        rule_ids = {item["rule_id"] for item in payload["findings"]}
        self.assertIn("tls_verification_disabled", rule_ids)
        self.assertIn("unverified_ssl_context", rule_ids)

    def test_security_scan_flags_archive_extractall_usage(self) -> None:
        from src.contrib.repo_scan import build_scan_payload

        candidate = RepoCandidate(
            name="archive-risk",
            full_name="example/archive-risk",
            description="archive repo",
            stars=10,
            forks=2,
            license="mit",
            url="https://github.com/example/archive-risk",
            default_branch="main",
            pushed_days_ago=1,
            files={
                "extractors.py": (
                    "import tarfile\n"
                    "import zipfile\n"
                    "tar = tarfile.open(path)\n"
                    "tar.extractall(dest)\n"
                    "zf = zipfile.ZipFile(path)\n"
                    "zf.extractall(dest)\n"
                ),
            },
        )
        with (
            patch("src.contrib.repo_scan._fetch_scan_candidate", return_value=candidate),
            patch("src.contrib.repo_scan.get_repo_inspect_data", return_value={"targeted_scope": "targeted-ready", "scope_notes": []}),
        ):
            payload = build_scan_payload("example/archive-risk", logging.getLogger("test"), kind="security")

        rule_ids = {item["rule_id"] for item in payload["findings"]}
        self.assertIn("tar_extractall_without_validation", rule_ids)
        self.assertIn("zip_extractall_without_validation", rule_ids)


if __name__ == "__main__":
    unittest.main()
