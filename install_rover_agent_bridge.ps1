Write-Host "[ROVER] installing agent bridge into current repo..."
New-Item -ItemType Directory -Force -Path ".agents\roles" | Out-Null
New-Item -ItemType Directory -Force -Path "scripts" | Out-Null
New-Item -ItemType Directory -Force -Path ".vscode" | Out-Null
New-Item -ItemType Directory -Force -Path "runs\agent_sessions" | Out-Null

python scripts/rover_agents.py config-check

Write-Host "[ROVER] done."
