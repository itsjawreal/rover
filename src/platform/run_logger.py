from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.config import RUNS_DIR


@dataclass
class RunArtifact:
    selected_repo: str
    selected_issue: str
    reason_for_selection: str
    planned_fix: str
    changed_files: list[str]
    validation_result: str
    pr_title: str
    pr_body: str
    metadata: dict[str, Any]


@dataclass
class RunLogPaths:
    json_path: Path
    markdown_path: Path


def _render_markdown(artifact: RunArtifact) -> str:
    changed = "\n".join(f"- `{path}`" for path in artifact.changed_files) or "- none"
    metadata = "\n".join(
        f"- **{key}**: {value}"
        for key, value in artifact.metadata.items()
        if not isinstance(value, dict)
    )
    return (
        "# Open-Source Contributor Agent Run\n\n"
        f"## Selected Repo\n{artifact.selected_repo}\n\n"
        f"## Selected Issue\n{artifact.selected_issue}\n\n"
        f"## Reason for Selection\n{artifact.reason_for_selection}\n\n"
        f"## Planned Fix\n{artifact.planned_fix}\n\n"
        f"## Changed Files\n{changed}\n\n"
        f"## Validation Result\n{artifact.validation_result}\n\n"
        f"## PR Title\n{artifact.pr_title}\n\n"
        f"## PR Body\n{artifact.pr_body}\n\n"
        f"## Metadata\n{metadata}\n"
    )


def save_run_log(artifact: RunArtifact) -> RunLogPaths:
    RUNS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = RUNS_DIR / f"run_{timestamp}.json"
    markdown_path = RUNS_DIR / f"run_{timestamp}.md"
    json_path.write_text(json.dumps(asdict(artifact), indent=2, ensure_ascii=False), encoding="utf-8")
    markdown_path.write_text(_render_markdown(artifact), encoding="utf-8")
    return RunLogPaths(json_path=json_path, markdown_path=markdown_path)
