#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
ENV_FILE="$ROOT_DIR/.env"
EXAMPLE_ENV_FILE="$ROOT_DIR/.env.example"
SETUP_STEP=0

venv_bin_dir() {
  if [ -d "$VENV_DIR/Scripts" ]; then
    printf '%s' "$VENV_DIR/Scripts"
  else
    printf '%s' "$VENV_DIR/bin"
  fi
}

venv_python_bin() {
  local bin_dir
  bin_dir="$(venv_bin_dir)"
  if [ -x "$bin_dir/python.exe" ]; then
    printf '%s' "$bin_dir/python.exe"
  else
    printf '%s' "$bin_dir/python"
  fi
}

venv_activate_script() {
  local bin_dir
  bin_dir="$(venv_bin_dir)"
  printf '%s' "$bin_dir/activate"
}

venv_engine_bin() {
  local bin_dir
  bin_dir="$(venv_bin_dir)"
  if [ -x "$bin_dir/github-contribution-engine.exe" ]; then
    printf '%s' "$bin_dir/github-contribution-engine.exe"
  else
    printf '%s' "$bin_dir/github-contribution-engine"
  fi
}

if [ -t 1 ]; then
  C_RESET="$(printf '\033[0m')"
  C_BOLD="$(printf '\033[1m')"
  C_BLUE="$(printf '\033[34m')"
  C_GREEN="$(printf '\033[32m')"
  C_YELLOW="$(printf '\033[33m')"
  C_MAGENTA="$(printf '\033[35m')"
else
  C_RESET=""
  C_BOLD=""
  C_BLUE=""
  C_GREEN=""
  C_YELLOW=""
  C_MAGENTA=""
fi

banner() {
  printf '\n%s%sGitHub Contribution Engine Setup%s\n' "$C_BOLD" "$C_MAGENTA" "$C_RESET"
  printf '%sAcceptance-first installer for VPS and MCP usage%s\n\n' "$C_BLUE" "$C_RESET"
}

section() {
  SETUP_STEP=$((SETUP_STEP + 1))
  printf '\n%s[%02d]%s %s%s%s\n' "$C_MAGENTA" "$SETUP_STEP" "$C_RESET" "$C_BOLD" "$1" "$C_RESET"
}

log() {
  printf '%s[setup]%s %s\n' "$C_BLUE" "$C_RESET" "$1"
}

warn() {
  printf '%s[warn]%s %s\n' "$C_YELLOW" "$C_RESET" "$1"
}

todo() {
  printf '%s[todo]%s %s\n' "$C_MAGENTA" "$C_RESET" "$1"
}

ok() {
  printf '%s[ok]%s %s\n' "$C_GREEN" "$C_RESET" "$1"
}

hint() {
  printf '%s[hint]%s %s\n' "$C_MAGENTA" "$C_RESET" "$1"
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

is_headless_environment() {
  [ -n "${SSH_CONNECTION:-}" ] || [ -n "${SSH_TTY:-}" ] || [ -z "${DISPLAY:-}" ]
}

python_has_venv() {
  python3 - <<'PY' >/dev/null 2>&1
import venv
print("ok")
PY
}

codex_login_ready() {
  if ! has_cmd codex; then
    return 1
  fi
  if [ -n "${OPENAI_API_KEY:-}" ]; then
    return 0
  fi
  codex login status >/dev/null 2>&1
}

update_env() {
  local key="$1"
  local value="$2"
  touch "$ENV_FILE"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    python3 - "$ENV_FILE" "$key" "$value" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines()
updated = []
replaced = False
for line in lines:
    if line.startswith(f"{key}="):
        updated.append(f"{key}={value}")
        replaced = True
    else:
        updated.append(line)
if not replaced:
    updated.append(f"{key}={value}")
path.write_text("\n".join(updated) + "\n", encoding="utf-8")
PY
  else
    printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
  fi
}

clear_env_key() {
  local key="$1"
  if [ -f "$ENV_FILE" ]; then
    python3 - "$ENV_FILE" "$key" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines()
updated = [line for line in lines if not line.startswith(f"{key}=")]
path.write_text("\n".join(updated) + ("\n" if updated else ""), encoding="utf-8")
PY
  fi
  unset "$key" 2>/dev/null || true
}

get_env_file_value() {
  local key="$1"
  if [ ! -f "$ENV_FILE" ]; then
    return 0
  fi
  python3 - "$ENV_FILE" "$key" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
for line in path.read_text(encoding="utf-8").splitlines():
    if line.startswith(f"{key}="):
        print(line.split("=", 1)[1])
        break
PY
}

preview_secret() {
  local value="$1"
  local length="${#value}"
  if [ "$length" -le 8 ]; then
    printf 'saved'
  else
    printf '%s...%s' "${value:0:4}" "${value: -4}"
  fi
}

prompt_env_value() {
  local key="$1"
  local prompt="$2"
  local secret="${3:-false}"
  local current_value="${!key:-}"
  if [ -n "$current_value" ]; then
    log "$key already set in current shell"
    update_env "$key" "$current_value"
    return 0
  fi

  local env_file_value=""
  env_file_value="$(get_env_file_value "$key")"
  if [ -n "$env_file_value" ]; then
    local display_value="saved value"
    if [ "$secret" = "true" ]; then
      display_value="$(preview_secret "$env_file_value")"
    else
      display_value="$env_file_value"
    fi

    printf '\n'
    hint "$key already exists in .env: $display_value"
    local existing_choice
    existing_choice="$(choose_option "Choose how to handle $key:" \
      "Use existing value from .env" \
      "Replace with a new value" \
      "Clear saved value and continue without it")"
    case "$existing_choice" in
      1)
        export "$key=$env_file_value"
        ok "Using existing $key from .env"
        return 0
        ;;
      2)
        ;;
      3)
        clear_env_key "$key"
        warn "$key removed from .env"
        return 0
        ;;
    esac
  fi

  local value=""
  if [ "$secret" = "true" ]; then
    read -r -s -p "$prompt " value
    printf '\n'
  else
    read -r -p "$prompt " value
  fi
  if [ -n "$value" ]; then
    export "$key=$value"
    update_env "$key" "$value"
    log "$key saved to .env"
  else
    warn "$key left empty"
  fi
}

confirm() {
  local prompt="$1"
  local answer=""
  read -r -p "$prompt [Y/n] " answer
  answer="${answer:-Y}"
  case "${answer,,}" in
    y|yes) return 0 ;;
    *) return 1 ;;
  esac
}

choose_option() {
  local prompt="$1"
  shift
  local options=("$@")
  local selected=0
  local key=""
  local line_count="${#options[@]}"

  if [ ! -t 0 ] || [ ! -t 1 ] || [ -n "${CI:-}" ]; then
    printf '%s%s%s\n' "$C_BOLD" "$prompt" "$C_RESET" >&2
    for option in "${options[@]}"; do
      printf '  - %s\n' "$option" >&2
    done
    printf '%s' "1"
    return 0
  fi

  _render_option_picker() {
    local current="$1"
    printf '\r' >&2
    for _ in $(seq 1 "$line_count"); do
      printf '\033[1A\033[2K' >&2
    done
    printf '%s%s%s\n' "$C_BOLD" "$prompt" "$C_RESET" >&2
    local idx=0
    for option in "${options[@]}"; do
      if [ "$idx" -eq "$current" ]; then
        printf '  %s›%s %s%s%s\n' "$C_GREEN" "$C_RESET" "$C_BOLD" "$option" "$C_RESET" >&2
      else
        printf '   %s\n' "$option" >&2
      fi
      idx=$((idx + 1))
    done
    printf '%s[hint]%s Use arrow keys, then press Enter.\n' "$C_MAGENTA" "$C_RESET" >&2
  }

  line_count=$((line_count + 2))
  _render_option_picker "$selected"
  while true; do
    IFS= read -rsn1 key
    case "$key" in
      "")
        printf '%s' "$((selected + 1))"
        printf '\n' >&2
        return 0
        ;;
      $'\x1b')
        IFS= read -rsn2 key || true
        case "$key" in
          "[A")
            if [ "$selected" -gt 0 ]; then
              selected=$((selected - 1))
            else
              selected=$((${#options[@]} - 1))
            fi
            ;;
          "[B")
            if [ "$selected" -lt $((${#options[@]} - 1)) ]; then
              selected=$((selected + 1))
            else
              selected=0
            fi
            ;;
        esac
        _render_option_picker "$selected"
        ;;
    esac
  done
}

apt_install_if_missing() {
  local pkg="$1"
  local cmd="$2"
  if has_cmd "$cmd"; then
    log "$cmd already available"
    return 0
  fi
  if ! has_cmd apt-get; then
    warn "apt-get not available; cannot auto-install $pkg"
    return 0
  fi
  log "installing $pkg"
  if [ "$(id -u)" -eq 0 ]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg"
  elif has_cmd sudo; then
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg"
  else
    warn "sudo not available; install $pkg manually"
  fi
}

install_uv_if_missing() {
  if has_cmd uv; then
    log "uv already available"
    return 0
  fi
  if has_cmd curl; then
    log "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    return 0
  fi
  warn "curl not available; skipping uv install"
}

install_codex_if_requested() {
  if has_cmd codex; then
    log "codex already available"
    return 0
  fi
  if ! confirm "Install Codex CLI now?"; then
    warn "Skipping Codex CLI install"
    return 0
  fi
  apt_install_if_missing nodejs node
  apt_install_if_missing npm npm
  if has_cmd npm; then
    log "installing Codex CLI via npm"
    npm install -g @openai/codex
  else
    warn "npm not available; cannot install Codex CLI automatically"
  fi
}

configure_github_auth() {
  if confirm "Configure GitHub token now?"; then
    prompt_env_value "GITHUB_TOKEN" "Enter GITHUB_TOKEN (input hidden):" true
  fi
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    export GH_TOKEN="$GITHUB_TOKEN"
  fi

  if has_cmd gh; then
    if gh auth status >/dev/null 2>&1; then
      log "gh auth already active"
    elif [ -n "${GITHUB_TOKEN:-}" ]; then
      log "authenticating gh with provided token"
      printf '%s' "$GITHUB_TOKEN" | gh auth login --with-token
    elif confirm "Run interactive gh auth login now?"; then
      gh auth login
    else
      warn "Skipping gh auth login"
    fi
  fi
}

configure_codex_backend() {
  update_env "AI_BACKEND" "codex"
  update_env "AGENT_TOOL" "Codex"
  update_env "MODEL_SERIES" "GPT"
  install_codex_if_requested

  printf '\n'
  printf '%sCodex sign-in help%s\n' "$C_BOLD" "$C_RESET"
  if is_headless_environment; then
    hint "VPS / headless environment detected."
    hint "Recommended option: device auth."
    hint "Codex will show a URL and a short code. Open that URL from your local browser, complete sign-in, then return here."
    hint "Avoid localhost browser login on a remote VPS unless you have SSH port forwarding configured."
  else
    hint "Local machine detected."
    hint "Browser login is usually fine here. If it fails, rerun setup and choose device auth instead."
  fi

  if codex_login_ready; then
    ok "Existing Codex authentication detected"
    local existing_auth_choice
    existing_auth_choice="$(choose_option "Choose how to handle existing Codex auth:" \
      "Keep the existing Codex login" \
      "Save OPENAI_API_KEY instead" \
      "Re-run Codex login flow")"
    case "$existing_auth_choice" in
      1)
        return 0
        ;;
      2)
        prompt_env_value "OPENAI_API_KEY" "Enter OPENAI_API_KEY (input hidden):" true
        return 0
        ;;
      3)
        ;;
    esac
  fi

  local auth_choice
  auth_choice="$(choose_option "Select how to prepare Codex:" \
    "Save OPENAI_API_KEY" \
    "Run Codex device auth (recommended for VPS/headless)" \
    "Run browser-based codex login" \
    "Skip Codex auth for now")"
  case "$auth_choice" in
    1)
      prompt_env_value "OPENAI_API_KEY" "Enter OPENAI_API_KEY (input hidden):" true
      ;;
    2)
      if has_cmd codex; then
        printf '\n'
        hint "Starting Codex device auth."
        hint "You will get a verification URL and code."
        hint "Open the URL from your local browser, finish login, then come back to this VPS terminal."
        if codex login --device-auth; then
          ok "Codex device auth completed"
        else
          warn "Codex device auth did not complete."
          hint "If you pressed Ctrl+C or the flow failed, rerun setup and choose this option again."
          hint "You can also use OPENAI_API_KEY instead of interactive login."
        fi
      else
        warn "Codex CLI is not installed yet, so device auth was skipped"
      fi
      ;;
    3)
      if has_cmd codex; then
        printf '\n'
        hint "Starting browser-based Codex login."
        hint "This works best on a local machine with direct browser access."
        hint "If localhost fails on a VPS, rerun setup and choose device auth instead."
        if codex login; then
          ok "Codex browser login completed"
        else
          warn "Codex browser login did not complete."
          hint "On a VPS, choose device auth instead of browser login."
          hint "You can also use OPENAI_API_KEY instead of interactive login."
        fi
      else
        warn "Codex CLI is not installed yet, so browser login was skipped"
      fi
      ;;
    4)
      warn "Skipping Codex auth"
      ;;
  esac
}

configure_claude_backend() {
  update_env "AI_BACKEND" "claude"
  update_env "AGENT_TOOL" "Claude Code"
  update_env "MODEL_SERIES" "Claude"

  warn "Claude CLI install/auth is not fully automated in this script yet."
  if confirm "Save ANTHROPIC_API_KEY to .env now?"; then
    prompt_env_value "ANTHROPIC_API_KEY" "Enter ANTHROPIC_API_KEY (input hidden):" true
  fi
  if has_cmd claude; then
    log "claude CLI already available on PATH"
  else
    todo "Install Claude CLI manually, then rerun github-contribution-engine --doctor"
  fi
}

configure_api_key_backend() {
  local provider_choice
  provider_choice="$(choose_option "Select your API-key provider:" \
    "OpenAI / GPT" \
    "Anthropic / Claude" \
    "OpenRouter" \
    "Other / custom provider")"
  case "$provider_choice" in
    1)
      update_env "MODEL_SERIES" "GPT"
      prompt_env_value "OPENAI_API_KEY" "Enter OPENAI_API_KEY (input hidden):" true
      ;;
    2)
      update_env "MODEL_SERIES" "Claude"
      prompt_env_value "ANTHROPIC_API_KEY" "Enter ANTHROPIC_API_KEY (input hidden):" true
      ;;
    3)
      update_env "MODEL_SERIES" "Other"
      prompt_env_value "OPENROUTER_API_KEY" "Enter OPENROUTER_API_KEY (input hidden):" true
      ;;
    4)
      update_env "MODEL_SERIES" "Other"
      warn "Custom provider selected. Save provider-specific env vars manually if needed."
      ;;
  esac
  update_env "AGENT_TOOL" "Other"
  warn "API-key-only generation is still a partial path in this repo. Doctor will report remaining readiness gaps."
}

install_openclaw_integration() {
  if ! confirm "Install OpenClaw skill and wrapper now?"; then
    warn "Skipping OpenClaw native integration"
    return 0
  fi

  local python_bin
  local engine_bin
  python_bin="$(venv_python_bin)"
  engine_bin="$(venv_engine_bin)"

  if [ ! -x "$python_bin" ]; then
    warn "Python interpreter not found for OpenClaw asset install: $python_bin"
    return 0
  fi
  if [ ! -x "$engine_bin" ]; then
    warn "github-contribution-engine executable not found for OpenClaw asset install: $engine_bin"
    return 0
  fi

  log "installing OpenClaw skill and wrapper"
  "$python_bin" "$ROOT_DIR/src/openclaw_install.py" \
    --engine-bin "$engine_bin" \
    --python-bin "$python_bin"
  ok "OpenClaw skill and wrapper installed"
}

main() {
  banner
  log "bootstrap starting in $ROOT_DIR"

  section "System prerequisites"
  apt_install_if_missing git git
  apt_install_if_missing python3 python3
  if python_has_venv; then
    ok "python3 venv module already available"
  else
    apt_install_if_missing python3-venv python3-venv
  fi
  apt_install_if_missing curl curl
  apt_install_if_missing gh gh
  install_uv_if_missing

  section "Python environment"
  if [ ! -d "$VENV_DIR" ]; then
    log "creating virtual environment at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
  else
    log "virtual environment already exists at $VENV_DIR"
  fi

  # shellcheck disable=SC1090
  source "$(venv_activate_script)"
  log "upgrading pip"
  python -m pip install -U pip

  log "installing github-contribution-engine package"
  python -m pip install "$ROOT_DIR"

  section "Project configuration"
  if [ ! -f "$ENV_FILE" ] && [ -f "$EXAMPLE_ENV_FILE" ]; then
    log "creating .env from .env.example"
    cp "$EXAMPLE_ENV_FILE" "$ENV_FILE"
  else
    log ".env already present or .env.example missing"
  fi

  section "GitHub authentication"
  configure_github_auth

  section "AI backend selection"
  local backend_choice
  backend_choice="$(choose_option "Select your primary AI backend for this machine:" \
    "Codex CLI" \
    "Claude CLI" \
    "LLM API key only" \
    "Skip backend setup for now")"
  case "$backend_choice" in
    1) configure_codex_backend ;;
    2) configure_claude_backend ;;
    3) configure_api_key_backend ;;
    4) warn "Skipping backend setup for now" ;;
  esac

  section "OpenClaw integration"
  install_openclaw_integration

  section "Readiness check"
  log "running doctor"
  github-contribution-engine --doctor || true

  printf '\n'
  ok "bootstrap complete"
  printf '%sNext steps%s\n' "$C_BOLD" "$C_RESET"
  todo "Run: source \"$(venv_activate_script)\""
  if ! gh auth status >/dev/null 2>&1; then
    todo "Run: gh auth login"
  fi
  if ! has_cmd codex && ! has_cmd claude; then
    todo "Install Codex CLI or Claude CLI before running contribution generation."
  fi
  todo "Run: github-contribution-engine --doctor"
  todo "Run: contribution-mcp"
  todo "In OpenClaw, use the installed skill at ~/.openclaw/workspace/skills/github-contribution-engine/SKILL.md when the workspace exists."
}

main "$@"
