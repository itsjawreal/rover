from __future__ import annotations

import contextlib
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.builder import inspect_repo
from src.contrib.contribution_store import PREngineStore
from src.contrib.pr_generator import get_repo_inspect_data
from src.github.scraper import RepoCandidate


class InspectCacheTests(unittest.TestCase):
    def _make_store(self) -> PREngineStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return PREngineStore(Path(tmp.name) / "engine.sqlite3")

    @staticmethod
    def _candidate() -> RepoCandidate:
        return RepoCandidate(
            name="tooling-repo",
            full_name="example/tooling-repo",
            description="A sample repo for inspect caching",
            stars=420,
            forks=33,
            license="MIT",
            url="https://github.com/example/tooling-repo",
            default_branch="main",
            pushed_days_ago=2,
            topics=["python", "tooling", "cli"],
            files={
                "src/app.py": "def run():\n    return 1\n",
                "tests/test_app.py": "def test_run():\n    assert True\n",
            },
        )

    def test_repo_inspect_snapshot_roundtrip(self) -> None:
        store = self._make_store()
        candidate = self._candidate()
        inspect_data = get_repo_inspect_data(candidate)

        store.save_repo_inspect_snapshot(
            candidate,
            inspect_data,
            source_pushed_at="2026-05-03T00:00:00Z",
            artifact_path="/tmp/inspect.md",
        )
        cached = store.get_repo_inspect_snapshot(candidate.full_name)

        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached["repo"], candidate.full_name)
        self.assertEqual(cached["source_pushed_at"], "2026-05-03T00:00:00Z")
        self.assertEqual(cached["topics"], ["python", "tooling", "cli"])
        self.assertEqual(cached["artifact_path"], "/tmp/inspect.md")

    def test_inspect_repo_uses_cached_snapshot_when_metadata_is_unchanged(self) -> None:
        store = self._make_store()
        candidate = self._candidate()
        inspect_data = get_repo_inspect_data(candidate)
        store.save_repo_inspect_snapshot(
            candidate,
            inspect_data,
            source_pushed_at="2026-05-03T00:00:00Z",
            artifact_path="/tmp/inspect.md",
        )

        with mock.patch("app.builder.PREngineStore", return_value=store), mock.patch(
            "app.builder.resolve_repo_full_name", return_value=candidate.full_name
        ), mock.patch(
            "app.builder.fetch_repo_metadata",
            return_value=(candidate.full_name, {"pushed_at": "2026-05-03T00:00:00Z"}),
        ), mock.patch(
            "app.builder.fetch_repo_candidate_with_scope"
        ) as mocked_fetch, mock.patch(
            "src.core.cli_ui._console.status", side_effect=lambda *args, **kwargs: contextlib.nullcontext()
        ):
            inspect_repo(candidate.full_name, logging.getLogger("test"))

        mocked_fetch.assert_not_called()

    def test_inspect_repo_cached_only_requires_existing_snapshot(self) -> None:
        store = self._make_store()

        with mock.patch("app.builder.PREngineStore", return_value=store), mock.patch(
            "app.builder.resolve_repo_full_name", return_value="example/missing-repo"
        ), mock.patch(
            "src.core.cli_ui._console.status", side_effect=lambda *args, **kwargs: contextlib.nullcontext()
        ), mock.patch("app.builder.fetch_repo_metadata") as mocked_metadata, mock.patch(
            "app.builder.fetch_repo_candidate_with_scope"
        ) as mocked_fetch:
            inspect_repo("example/missing-repo", logging.getLogger("test"), cached_only=True)

        mocked_metadata.assert_not_called()
        mocked_fetch.assert_not_called()

    def test_inspect_repo_refresh_bypasses_fresh_cache(self) -> None:
        store = self._make_store()
        candidate = self._candidate()
        inspect_data = get_repo_inspect_data(candidate)
        store.save_repo_inspect_snapshot(
            candidate,
            inspect_data,
            source_pushed_at="2026-05-03T00:00:00Z",
            artifact_path="/tmp/inspect.md",
        )

        with mock.patch("app.builder.PREngineStore", return_value=store), mock.patch(
            "app.builder.resolve_repo_full_name", return_value=candidate.full_name
        ), mock.patch(
            "app.builder.fetch_repo_metadata",
            return_value=(candidate.full_name, {"pushed_at": "2026-05-03T00:00:00Z"}),
        ), mock.patch(
            "app.builder.fetch_repo_candidate_with_scope", return_value=candidate
        ) as mocked_fetch, mock.patch(
            "app.builder.write_repo_inspect_artifact", return_value=Path("/tmp/inspect-fresh.md")
        ), mock.patch(
            "src.core.cli_ui._console.status", side_effect=lambda *args, **kwargs: contextlib.nullcontext()
        ):
            inspect_repo(candidate.full_name, logging.getLogger("test"), force_refresh=True)

        mocked_fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
