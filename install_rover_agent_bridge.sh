#!/usr/bin/env bash
set -euo pipefail

echo "[ROVER] installing agent bridge into current repo..."

mkdir -p .agents/roles scripts .vscode runs/agent_sessions

# This installer is mainly a marker because the zip already contains files.
# Extract zip into repo root, then run:
python scripts/rover_agents.py config-check || true

echo "[ROVER] done."
