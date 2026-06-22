$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir
$VenvDir = if ($env:VENV_DIR) { $env:VENV_DIR } else { Join-Path $RootDir '.venv' }
$EnvFile = Join-Path $RootDir '.env'
$ExampleEnvFile = Join-Path $RootDir '.env.example'
$McpFile = Join-Path $RootDir '.mcp.json'

function Write-Info($Message) { Write-Host "[rover] $Message" -ForegroundColor Cyan }
function Write-Ok($Message) { Write-Host "[ok] $Message" -ForegroundColor Green }
function Write-Warn($Message) { Write-Host "[warn] $Message" -ForegroundColor Yellow }
function Write-Todo($Message) { Write-Host "  - $Message" }

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

function Get-PythonCommand {
    foreach ($candidate in @('py', 'python')) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            return $candidate
        }
    }
    throw 'Python launcher not found. Install Python 3 first.'
}

function Invoke-Python {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Args
    )
    $python = Get-PythonCommand
    if ($python -eq 'py') {
        & py -3 @Args
    } else {
        & python @Args
    }
}

function Ensure-EnvFile {
    if (-not (Test-Path $EnvFile) -and (Test-Path $ExampleEnvFile)) {
        Copy-Item $ExampleEnvFile $EnvFile
        Write-Ok "created $EnvFile from .env.example"
    }
}

function Update-EnvValue {
    param(
        [string]$Key,
        [string]$Value
    )

    $lines = @()
    if (Test-Path $EnvFile) {
        $lines = Get-Content $EnvFile
    }

    $updated = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^\Q$Key\E=") {
            $lines[$i] = "$Key=$Value"
            $updated = $true
            break
        }
    }
    if (-not $updated) {
        $lines += "$Key=$Value"
    }
    Set-Content -Path $EnvFile -Value $lines -Encoding UTF8
    Set-Item -Path "Env:$Key" -Value $Value
}

function Prompt-EnvValue {
    param(
        [string]$Key,
        [string]$Prompt,
        [switch]$Secret
    )

    if (Test-Path $EnvFile) {
        $current = Select-String -Path $EnvFile -Pattern "^\Q$Key\E=(.*)$" | Select-Object -First 1
        if ($current) {
            $choice = Choose-Option -Prompt "Choose how to handle ${Key}:" -Options @(
                'Use existing value from .env',
                'Replace with a new value',
                'Clear saved value and continue without it'
            )
            switch ($choice) {
                'Use existing value from .env' { return }
                'Clear saved value and continue without it' {
                    Update-EnvValue -Key $Key -Value ''
                    return
                }
            }
        }
    }

    if ($Secret) {
        $secure = Read-Host -Prompt $Prompt -AsSecureString
        $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try {
            $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
        }
    } else {
        $value = Read-Host -Prompt $Prompt
    }
    Update-EnvValue -Key $Key -Value $value
}

function Ensure-Venv {
    if (-not (Test-Path $VenvDir)) {
        Write-Info "creating virtualenv at $VenvDir"
        Invoke-Python -Args @('-m', 'venv', $VenvDir)
    }
}

function Get-VenvPython {
    return Join-Path $VenvDir 'Scripts\python.exe'
}

function Install-Rover {
    $venvPython = Get-VenvPython
    & $venvPython -m pip install -U pip
    & $venvPython -m pip install -e $RootDir
}

function Configure-GitHubAuth {
    $authChoice = Choose-Option -Prompt 'Select GitHub auth mode for Rover:' -Options @(
        'Token in .env only',
        'gh auth login only',
        'Both token + gh auth login',
        'Skip GitHub auth for now'
    )

    switch ($authChoice) {
        'Token in .env only' { Prompt-EnvValue -Key 'GITHUB_TOKEN' -Prompt 'Enter GITHUB_TOKEN' -Secret }
        'gh auth login only' { Write-Info 'Skipping token prompt; will rely on gh auth' }
        'Both token + gh auth login' { Prompt-EnvValue -Key 'GITHUB_TOKEN' -Prompt 'Enter GITHUB_TOKEN' -Secret }
        'Skip GitHub auth for now' { Write-Warn 'Skipping GitHub auth setup' }
    }

    if ($env:GITHUB_TOKEN -and -not $env:GH_TOKEN) {
        Set-Item -Path Env:GH_TOKEN -Value $env:GITHUB_TOKEN
    }

    if (Get-Command gh -ErrorAction SilentlyContinue) {
        & gh auth status *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Info 'gh auth already active'
        } elseif ($authChoice -eq 'Both token + gh auth login' -and $env:GITHUB_TOKEN) {
            $env:GITHUB_TOKEN | gh auth login --with-token
        } elseif ($authChoice -eq 'gh auth login only') {
            gh auth login
        }
    }
}

function Configure-CodexBackend {
    Update-EnvValue -Key 'AI_BACKEND' -Value 'codex'
    Update-EnvValue -Key 'CODEX_CMD' -Value 'codex'
    Update-EnvValue -Key 'AGENT_TOOL' -Value 'Codex'
    Update-EnvValue -Key 'MODEL_SERIES' -Value 'GPT'
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
            npm install -g @openai/codex
        }
    } else {
        Write-Warn 'npm not available; cannot install Codex CLI automatically'
    }

    $authChoice = Choose-Option -Prompt 'Select how to prepare Codex:' -Options @(
        'Run browser-based codex login',
        'Run device-auth codex login',
        'Skip Codex auth for now'
    )
    switch ($authChoice) {
        'Run browser-based codex login' { if (Get-Command codex -ErrorAction SilentlyContinue) { codex login } }
        'Run device-auth codex login' { if (Get-Command codex -ErrorAction SilentlyContinue) { codex login --device-auth } }
        'Skip Codex auth for now' { Write-Warn 'Skipping Codex auth for now' }
    }
}

function Configure-ClaudeBackend {
    Update-EnvValue -Key 'AI_BACKEND' -Value 'claude'
    Update-EnvValue -Key 'CLAUDE_CMD' -Value 'claude'
    Update-EnvValue -Key 'AGENT_TOOL' -Value 'Claude Code'
    Update-EnvValue -Key 'MODEL_SERIES' -Value 'Claude'
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
            npm install -g @anthropic-ai/claude-code
        }
    } else {
        Write-Warn 'npm not available; cannot install Claude CLI automatically'
    }

    $authChoice = Choose-Option -Prompt 'How do you want to authenticate Claude CLI?' -Options @(
        'Browser login (claude.ai account - no API key needed)',
        'Use ANTHROPIC_API_KEY from .env',
        'Skip Claude auth for now'
    )
    switch ($authChoice) {
        'Browser login (claude.ai account - no API key needed)' { Write-Todo "Run 'claude' in a new terminal to complete browser login" }
        'Use ANTHROPIC_API_KEY from .env' { Prompt-EnvValue -Key 'ANTHROPIC_API_KEY' -Prompt 'Enter ANTHROPIC_API_KEY' -Secret }
        'Skip Claude auth for now' { Write-Warn 'Skipping Claude auth for now' }
    }
}

function Configure-ApiKeyBackend {
    $providerChoice = Choose-Option -Prompt 'Select your API-key provider:' -Options @(
        'OpenAI',
        'Anthropic',
        'OpenRouter',
        'Skip API key setup for now'
    )
    switch ($providerChoice) {
        'OpenAI' { Prompt-EnvValue -Key 'OPENAI_API_KEY' -Prompt 'Enter OPENAI_API_KEY' -Secret }
        'Anthropic' { Prompt-EnvValue -Key 'ANTHROPIC_API_KEY' -Prompt 'Enter ANTHROPIC_API_KEY' -Secret }
        'OpenRouter' { Prompt-EnvValue -Key 'OPENROUTER_API_KEY' -Prompt 'Enter OPENROUTER_API_KEY' -Secret }
        'Skip API key setup for now' { Write-Warn 'Skipping API key setup' }
    }
}

function Install-OpenClawIntegration {
    if (-not (Confirm 'Install Rover OpenClaw skill, wrapper, and mcp.servers.rover now?')) {
        return
    }
    $venvPython = Get-VenvPython
    $roverBin = Join-Path $VenvDir 'Scripts\rover.exe'
    if (-not (Test-Path $roverBin)) { $roverBin = Join-Path $VenvDir 'Scripts\rover' }
    $roverMcpBin = Join-Path $VenvDir 'Scripts\rover-mcp.exe'
    if (-not (Test-Path $roverMcpBin)) { $roverMcpBin = Join-Path $VenvDir 'Scripts\rover-mcp' }
    & $venvPython (Join-Path $RootDir 'src\platform\openclaw_install.py') `
        --rover-bin $roverBin `
        --python-bin $venvPython `
        --rover-mcp-bin $roverMcpBin
}

Clear-Host
Write-Host "rover windows installer`n" -ForegroundColor Cyan
Write-Warn 'This setup prepares a local Windows Rover environment with the same guided choices as the VPS installer.'

Ensure-EnvFile
Ensure-Venv
Install-Rover

Write-Host "`nGitHub authentication" -ForegroundColor Cyan
Configure-GitHubAuth

Write-Host "`nAI backend selection" -ForegroundColor Cyan
$backendChoice = Choose-Option -Prompt 'Select your primary AI backend for this machine:' -Options @(
    'Codex CLI',
    'Claude CLI',
    'LLM API key only',
    'Skip backend setup for now'
)
switch ($backendChoice) {
    'Codex CLI' { Configure-CodexBackend }
    'Claude CLI' { Configure-ClaudeBackend }
    'LLM API key only' { Configure-ApiKeyBackend }
    'Skip backend setup for now' { Write-Warn 'Skipping backend setup for now' }
}

Write-Host "`nOpenClaw integration" -ForegroundColor Cyan
Install-OpenClawIntegration

if (Confirm 'Generate local .mcp.json for this Windows workspace?') {
    $venvPython = Get-VenvPython
    & $venvPython -m src.mcp_install
}

Write-Host "`nReadiness check" -ForegroundColor Cyan
$venvPython = Get-VenvPython
& $venvPython -m app.builder --doctor

Write-Host ""
Write-Ok 'Windows Rover setup complete'
Write-Host 'Next steps' -ForegroundColor Cyan
Write-Todo "Activate venv: $VenvDir\\Scripts\\Activate.ps1"
if (-not $env:GH_TOKEN -and -not $env:GITHUB_TOKEN) {
    Write-Todo 'Set GH_TOKEN/GITHUB_TOKEN or run gh auth login'
}
Write-Todo 'Run: rover doctor'
Write-Todo 'Run: rover run'
