$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir
$VenvDir = if ($env:VENV_DIR) { $env:VENV_DIR } else { Join-Path $RootDir '.venv' }
$EnvFile = Join-Path $RootDir '.env'
$McpFile = Join-Path $RootDir '.mcp.json'
$DataDir = Join-Path $RootDir 'data'
$LogDir = Join-Path $RootDir 'logs'
$RunsDir = Join-Path $RootDir 'runs'
$StreamDir = Join-Path $RootDir '.stream_partials'
$OpenClawHome = if ($env:OPENCLAW_HOME) { $env:OPENCLAW_HOME } else { Join-Path $HOME '.openclaw' }
$AltOpenClawHome = Join-Path $HOME 'openclaw'

function Write-Info($Message) { Write-Host "[rover] $Message" -ForegroundColor Cyan }
function Write-Ok($Message) { Write-Host "[ok] $Message" -ForegroundColor Green }
function Write-Warn($Message) { Write-Host "[warn] $Message" -ForegroundColor Yellow }
function Write-Todo($Message) { Write-Host "  - $Message" }
$ChangesMade = $false

function Choose-Option {
    param(
        [string]$Prompt,
        [string[]]$Options
    )

    if (-not [Console]::IsInputRedirected -and $Host.Name -ne 'ServerRemoteHost') {
        $selected = 0
        while ($true) {
            Clear-Host
            Write-Host "$Prompt`n"
            for ($i = 0; $i -lt $Options.Count; $i++) {
                if ($i -eq $selected) {
                    Write-Host "  > $($Options[$i])" -ForegroundColor Green
                } else {
                    Write-Host "    $($Options[$i])"
                }
            }
            Write-Host ""
            Write-Host "[hint] Use arrow keys to move. Press Enter to select." -ForegroundColor Magenta
            $key = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
            switch ($key.VirtualKeyCode) {
                38 { if ($selected -gt 0) { $selected-- } }
                40 { if ($selected -lt ($Options.Count - 1)) { $selected++ } }
                13 { Clear-Host; return $Options[$selected] }
            }
        }
    }

    return $Options[0]
}

function Confirm {
    param([string]$Prompt)
    return (Choose-Option -Prompt $Prompt -Options @('Yes', 'No')) -eq 'Yes'
}

function Remove-Target {
    param(
        [string]$Target,
        [string]$Label
    )
    if (Test-Path $Target) {
        Remove-Item -LiteralPath $Target -Recurse -Force
        $script:ChangesMade = $true
        Write-Ok "removed $Label: $Target"
    } else {
        Write-Info "skip missing $Label: $Target"
    }
}

function Remove-OpenClawAssets {
    foreach ($root in @($OpenClawHome, $AltOpenClawHome)) {
        Remove-Target -Target (Join-Path $root 'workspace\skills\rover') -Label 'OpenClaw Rover workspace skill'
        Remove-Target -Target (Join-Path $root 'skills\rover') -Label 'OpenClaw Rover fallback skill'
        Remove-Target -Target (Join-Path $root 'workspace\skills\github-contribution-engine') -Label 'OpenClaw workspace skill'
        Remove-Target -Target (Join-Path $root 'skills\github-contribution-engine') -Label 'OpenClaw fallback skill'
        Remove-Target -Target (Join-Path $root 'tools\rover.py') -Label 'OpenClaw Rover wrapper'
        Remove-Target -Target (Join-Path $root 'tools\contribution.py') -Label 'OpenClaw wrapper'
    }
}

Clear-Host
Write-Host "rover windows uninstall/reset`n" -ForegroundColor Cyan
Write-Warn 'This script removes Rover-local Windows artifacts so you can re-test setup from a clean slate.'
Write-Warn 'Review each prompt carefully. Choosing Yes will permanently delete the selected local Rover files or directories.'

if ((Choose-Option -Prompt "Warning: this reset flow can permanently delete selected local Rover files, directories, and integration artifacts.`n`nChoose how to proceed:" -Options @(
    'Continue uninstall/reset',
    'Cancel and keep everything'
)) -ne 'Continue uninstall/reset') {
    Write-Warn 'Cancelled uninstall/reset. No changes were made.'
    exit 0
}

if (Confirm "Remove Python virtualenv at $VenvDir?") {
    Remove-Target -Target $VenvDir -Label 'virtualenv'
}

if (Confirm 'Remove Rover runtime state (data, logs, runs, stream partials)?') {
    Remove-Target -Target $DataDir -Label 'data dir'
    Remove-Target -Target $LogDir -Label 'logs dir'
    Remove-Target -Target $RunsDir -Label 'runs dir'
    Remove-Target -Target $StreamDir -Label 'stream partials dir'
}

if (Confirm "Remove local MCP config at $McpFile?") {
    Remove-Target -Target $McpFile -Label '.mcp.json'
}

if (Confirm 'Remove OpenClaw skill and wrapper installed by Rover?') {
    Remove-OpenClawAssets
}

if (Confirm 'Remove local .env so setup can start from zero?') {
    Remove-Target -Target $EnvFile -Label '.env'
}

Write-Host ""
if ($ChangesMade) {
    Write-Ok 'Windows Rover uninstall/reset complete'
    Write-Host 'Next steps' -ForegroundColor Cyan
    Write-Todo 'Optional: run gh auth logout -h github.com if you also want to reset GitHub CLI auth'
    Write-Todo 'Optional: run codex logout or claude logout if you want to re-test backend login flows'
    Write-Todo 'Reinstall with: powershell -ExecutionPolicy Bypass -File scripts/install_windows.ps1'
} else {
    Write-Warn 'No changes were made. Rover uninstall/reset was skipped.'
}
