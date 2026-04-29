from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from main import _demo_artifact
from src.issue_analyzer import classify_issue_type
from src.project_inspector import inspect_project
from src.pr_writer import prepare_pr
from src.run_logger import RunArtifact, save_run_log
from src.scraper import RepoCandidate
from src.fix_planner import FixPlan
from src.patch_generator import PatchPlan
from src.pr_generator import PRImprovement
from src.validator import ValidationResult


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

    def test_demo_artifact_is_submission_ready(self) -> None:
        artifact = _demo_artifact("Codex", "GPT")
        self.assertEqual(artifact.selected_repo, "HKUDS/Vibe-Trading")
        self.assertIn("acceptance-first", artifact.reason_for_selection)
        self.assertEqual(artifact.metadata["mode"], "demo")
        self.assertIn("backend", artifact.metadata)
        self.assertIn("backend_support_level", artifact.metadata)

    def test_save_run_log_writes_json_and_markdown(self) -> None:
        artifact = _demo_artifact("Codex", "GPT")
        paths = save_run_log(artifact)
        self.assertTrue(paths.json_path.exists())
        self.assertTrue(paths.markdown_path.exists())

    def test_clone_repository_uses_owner_repo_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with patch("src.repo_cloner.shutil.which", return_value="git"), \
                 patch("src.repo_cloner.subprocess.run") as mocked_run:
                mocked_run.return_value = SimpleNamespace(returncode=0)
                from src.repo_cloner import clone_repository

                result = clone_repository("https://github.com/HKUDS/Vibe-Trading.git", self._make_logger(), workspace)

        self.assertEqual(result.checkout_path.name, "HKUDS-Vibe-Trading")

    def test_clone_repository_removes_partial_checkout_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            partial = workspace / "owner-repo"
            partial.mkdir(parents=True)
            (partial / "broken.txt").write_text("x", encoding="utf-8")
            with patch("src.repo_cloner.shutil.which", return_value="git"), \
                 patch("src.repo_cloner.subprocess.run") as mocked_run:
                mocked_run.return_value = SimpleNamespace(returncode=0)
                from src.repo_cloner import clone_repository

                result = clone_repository("https://github.com/owner/repo.git", self._make_logger(), workspace)

        self.assertFalse((result.checkout_path / "broken.txt").exists())

    def _make_logger(self):
        import logging

        return logging.getLogger("test")
