from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.github.scraper import RepoCandidate


@dataclass
class ProjectInspection:
    language: str
    package_manager: str
    test_command: str
    lint_command: str
    project_structure: str


def inspect_project(candidate: RepoCandidate, checkout_path: Path | None = None) -> ProjectInspection:
    files = candidate.files
    py_count = sum(1 for path in files if path.endswith(".py"))
    ts_count = sum(1 for path in files if path.endswith((".ts", ".tsx")))
    language = "python" if py_count >= ts_count else "typescript"

    if "poetry.lock" in files:
        package_manager = "poetry"
    elif "pyproject.toml" in files:
        package_manager = "python"
    elif "package-lock.json" in files:
        package_manager = "npm"
    elif "pnpm-lock.yaml" in files:
        package_manager = "pnpm"
    elif "yarn.lock" in files:
        package_manager = "yarn"
    else:
        package_manager = "unknown"

    if any(path.startswith("tests/") or "/tests/" in path for path in files):
        test_command = "python -m pytest -q" if language == "python" else "npm test -- --runInBand"
    elif language == "python":
        test_command = "python -m unittest discover -v"
    else:
        test_command = ""

    if language == "python":
        lint_command = "python -m py_compile <changed-files>"
    else:
        lint_command = "npm run lint"

    if checkout_path is not None and checkout_path.exists():
        structure = f"cloned checkout at {checkout_path}"
    else:
        structure = "remote source snapshot from GitHub API"

    return ProjectInspection(
        language=language,
        package_manager=package_manager,
        test_command=test_command,
        lint_command=lint_command,
        project_structure=structure,
    )
