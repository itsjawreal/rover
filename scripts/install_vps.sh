#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
ENV_FILE="$ROOT_DIR/.env"
EXAMPLE_ENV_FILE="$ROOT_DIR/.env.example"

log() {
  printf '[setup] %s\n' "$1"
}

warn() {
  printf '[warn] %s\n' "$1"
}

todo() {
  printf '[todo] %s\n' "$1"
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

python_has_venv() {
  python3 - <<'PY' >/dev/null 2>&1
import venv
print("ok")
PY
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
  local choice=""
  printf '%s\n' "$prompt" >&2
  local i=1
  for option in "${options[@]}"; do
    printf '  %d) %s\n' "$i" "$option" >&2
    i=$((i + 1))
  done
  while true; do
    read -r -p "Choice [1-${#options[@]}]: " choice >&2
    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#options[@]}" ]; then
      printf '%s' "$choice"
      return 0
    fi
    printf '[warn] Invalid choice. Please select a number between 1 and %d.\n' "${#options[@]}" >&2
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

  local auth_choice
  auth_choice="$(choose_option "Select how to prepare Codex:" \
    "Save OPENAI_API_KEY" \
    "Run interactive codex login" \
    "Skip Codex auth for now")"
  case "$auth_choice" in
    1)
      prompt_env_value "OPENAI_API_KEY" "Enter OPENAI_API_KEY (input hidden):" true
      ;;
    2)
      if has_cmd codex; then
        codex --login
      else
        warn "Codex CLI is not installed yet, so interactive login was skipped"
      fi
      ;;
    3)
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

main() {
  log "bootstrap starting in $ROOT_DIR"

  apt_install_if_missing git git
  apt_install_if_missing python3 python3
  if python_has_venv; then
    log "python3 venv module already available"
  else
    apt_install_if_missing python3-venv python3-venv
  fi
  apt_install_if_missing curl curl
  apt_install_if_missing gh gh
  install_uv_if_missing

  if [ ! -d "$VENV_DIR" ]; then
    log "creating virtual environment at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
  else
    log "virtual environment already exists at $VENV_DIR"
  fi

  # shellcheck disable=SC1090
  source "$VENV_DIR/bin/activate"
  log "upgrading pip"
  python -m pip install -U pip

  log "installing github-contribution-engine package"
  python -m pip install "$ROOT_DIR"

  if [ ! -f "$ENV_FILE" ] && [ -f "$EXAMPLE_ENV_FILE" ]; then
    log "creating .env from .env.example"
    cp "$EXAMPLE_ENV_FILE" "$ENV_FILE"
  else
    log ".env already present or .env.example missing"
  fi

  configure_github_auth

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

  log "running doctor"
  github-contribution-engine --doctor || true

  printf '\n'
  log "bootstrap complete"
  todo "Run: source \"$VENV_DIR/bin/activate\""
  if ! gh auth status >/dev/null 2>&1; then
    todo "Run: gh auth login"
  fi
  if ! has_cmd codex && ! has_cmd claude; then
    todo "Install Codex CLI or Claude CLI before running contribution generation."
  fi
  todo "Run: github-contribution-engine --doctor"
  todo "Run: contribution-mcp"
}

main "$@"
