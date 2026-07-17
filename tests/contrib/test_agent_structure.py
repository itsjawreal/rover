from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.contrib.issue_analyzer import classify_issue_type
from src.analysis.project_inspector import inspect_project
from src.contrib.pr_writer import prepare_pr
from src.platform.run_logger import RunArtifact, save_run_log
from src.github.scraper import RepoCandidate
from src.contrib.fix_planner import FixPlan
from src.contrib.patch_generator import PatchPlan
from src.contrib.pr_generator import PRImprovement
from src.contrib.validator import ValidationResult


class AgentStructureTests(unittest.TestCase):
    def test_issue_type_classification_covers_bug_and_test_paths(self) -> None:
        self.assertEqual(classify_issue_type("missing_input_validation"), "bug")
        self.assertEqual(classify_issue_type("missing_regression_test_for_obvious_bugfix"), "test")

    def test_project_inspector_detects_python_repo(self) -> None:
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
                "pyproject.toml": "[project]\nname='sample'\n",
                "src/app.py": "def run():\n    return 1\n",
                "tests/test_app.py": "def test_run():\n    assert True\n",
            },
        )
        inspection = inspect_project(candidate)
        self.assertEqual(inspection.language, "python")
        self.assertEqual(inspection.package_manager, "python")
        self.assertIn("pytest", inspection.test_command)

    def test_pr_writer_rebuilds_sections_when_body_is_missing_structure(self) -> None:
        fix_plan = FixPlan(
            planned_fix="Patch src/app.py to validate CLI input.",
            risk_level="low",
            files_changed=["src/app.py", "tests/test_app.py"],
            validation_plan=["python -m py_compile <changed-files>"],
        )
        patch = PatchPlan(
            improvement=PRImprovement(
                title="fix: validate cli input",
                body="Plain body",
                improvement_type="bug_fix",
                changed_files={"src/app.py": "x=1\n"},
                rationale="Prevents malformed input crashes.",
            )
        )
        validation = ValidationResult(status="passed", summary="syntax checks passed", commands=["python -m py_compile <changed-files>"])
        prepared = prepare_pr(fix_plan, patch, validation)
        self.assertIn("## Summary", prepared.body)
        self.assertIn("## Validation result", prepared.body)

    def test_run_artifact_is_json_serializable(self) -> None:
        artifact = RunArtifact(
            selected_repo="example/sample",
            selected_issue="missing_input_validation in src/app.py",
            reason_for_selection="Small tested repo.",
            planned_fix="Patch src/app.py",
            changed_files=["src/app.py"],
            validation_result="passed",
            pr_title="fix: validate cli input",
            pr_body="## Summary\ntext",
            metadata={"dry_run": True},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.json"
            path.write_text(json.dumps(artifact.__dict__, indent=2), encoding="utf-8")
            self.assertTrue(path.exists())

    def test_save_run_log_writes_json_and_markdown(self) -> None:
        artifact = RunArtifact(
            selected_repo="example/sample",
            selected_issue="missing_input_validation in src/app.py",
            reason_for_selection="Small tested repo.",
            planned_fix="Patch src/app.py",
            changed_files=["src/app.py"],
            validation_result="passed",
            pr_title="fix: validate cli input",
            pr_body="## Summary\ntext",
            metadata={"dry_run": True, "mode": "test"},
        )
        paths = save_run_log(artifact)
        self.assertTrue(paths.json_path.exists())
        self.assertTrue(paths.markdown_path.exists())

    def test_clone_repository_uses_owner_repo_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with patch("src.github.repo_cloner.shutil.which", return_value="git"), \
                 patch("src.github.repo_cloner.subprocess.run") as mocked_run:
                mocked_run.return_value = SimpleNamespace(returncode=0)
                from src.github.repo_cloner import clone_repository

                result = clone_repository("https://github.com/HKUDS/Vibe-Trading.git", self._make_logger(), workspace)

        self.assertEqual(result.checkout_path.name, "HKUDS-Vibe-Trading")

    def test_clone_repository_returns_not_cloned_on_timeout(self) -> None:
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with patch("src.github.repo_cloner.shutil.which", return_value="git"), \
                 patch("src.github.repo_cloner.subprocess.run", side_effect=subprocess.TimeoutExpired(["git"], 120)):
                from src.github.repo_cloner import clone_repository

                result = clone_repository("https://github.com/owner/repo.git", self._make_logger(), workspace)

        self.assertFalse(result.cloned)
        self.assertIn("timed out", result.note.lower())

    def test_clone_repository_removes_partial_checkout_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            partial = workspace / "owner-repo"
            partial.mkdir(parents=True)
            (partial / "broken.txt").write_text("x", encoding="utf-8")
            with patch("src.github.repo_cloner.shutil.which", return_value="git"), \
                 patch("src.github.repo_cloner.subprocess.run") as mocked_run:
                mocked_run.return_value = SimpleNamespace(returncode=0)
                from src.github.repo_cloner import clone_repository

                result = clone_repository("https://github.com/owner/repo.git", self._make_logger(), workspace)

        self.assertFalse((result.checkout_path / "broken.txt").exists())

    def test_generate_patch_with_retry_falls_back_to_initial_when_retry_yields_empty_files(self) -> None:
        # Regression: if the retry patch generates no changed_files, run_sandbox_validation({})
        # returns sandbox_verified=True vacuously (nothing to compile → nothing fails). Without
        # the empty-files guard the empty retry patch gets returned as "sandbox_retry_success"
        # instead of falling back to the initial patch.
        import logging
        from unittest.mock import MagicMock
        from src.contrib.patch_generator import generate_patch_with_retry
        from src.contrib.pr_generator import PRImprovement
        from src.contrib.validator import ValidationResult

        initial_improvement = PRImprovement(
            title="fix: add timeout",
            body="## Summary\nAdds timeout to requests.get calls.",
            improvement_type="bug_fix",
            changed_files={"app.py": "import requests\nrequests.get(url, timeout=10)\n"},
            rationale="Prevents hanging requests.",
        )
        empty_improvement = PRImprovement(
            title="fix: add timeout",
            body="",
            improvement_type="bug_fix",
            changed_files={},
            rationale="",
        )

        call_count = 0

        def fake_generate_patch(candidate, log, goal="bugfix"):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return PatchPlan(improvement=initial_improvement)
            return PatchPlan(improvement=empty_improvement)

        fake_sandbox_fail = ValidationResult(
            status="failed", summary="compile error", sandbox_verified=False, sandbox_output="SyntaxError on line 1"
        )

        from src.github.scraper import RepoCandidate as RC
        candidate = RC(
            name="sample",
            full_name="example/sample",
            description="a test repo",
            stars=10,
            forks=2,
            license="MIT",
            url="https://github.com/example/sample",
            default_branch="main",
            pushed_days_ago=1,
            topics=[],
            files={"app.py": "import requests\nrequests.get(url)\n"},
        )

        with patch("src.contrib.patch_generator.generate_patch", side_effect=fake_generate_patch), patch(
            "src.contrib.validator.run_sandbox_validation", return_value=fake_sandbox_fail
        ):
            result = generate_patch_with_retry(candidate, logging.getLogger("test"))

        self.assertEqual(result.improvement.changed_files, initial_improvement.changed_files)
        self.assertEqual(result.sandbox_outcome, "sandbox_retry_failed")
        self.assertTrue(result.sandbox_retry_used)

    def test_run_sandbox_validation_compile_timeout_passes_without_verified_claim(self) -> None:
        # A compile timeout is an infra issue: the patch must not be blocked
        # (status stays "passed", sandbox_output empty → non-actionable path,
        # no repair retry), but code that never compiled must never be
        # reported as sandbox-verified.
        import subprocess
        from src.contrib.validator import run_sandbox_validation
        with patch("src.contrib.validator.subprocess.run", side_effect=subprocess.TimeoutExpired("py_compile", 15)):
            result = run_sandbox_validation({"app.py": "x = 1\n"})
        self.assertEqual(result.status, "passed")
        self.assertFalse(result.sandbox_verified)
        self.assertEqual(result.sandbox_output, "")

    def test_run_sandbox_validation_returns_verified_on_test_timeout(self) -> None:
        import subprocess
        from src.contrib.validator import run_sandbox_validation

        def fake_run(cmd, **kwargs):
            if "py_compile" in cmd:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise subprocess.TimeoutExpired(cmd, 15)

        with patch("src.contrib.validator.subprocess.run", side_effect=fake_run):
            result = run_sandbox_validation({"app.py": "x = 1\n"}, test_target="app.py")
        self.assertTrue(result.sandbox_verified)

    def _make_logger(self):
        import logging

        return logging.getLogger("test")
