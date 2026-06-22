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
  if [ -x "$bin_dir/rover-engine.exe" ]; then
    printf '%s' "$bin_dir/rover-engine.exe"
  else
    printf '%s' "$bin_dir/rover-engine"
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

on_interrupt() {
  printf '\n%s[warn]%s Setup interrupted by user.\n' "$C_YELLOW" "$C_RESET" >&2
  exit 130
}

trap on_interrupt INT

banner() {
  printf '\n%s%s♦ rover setup%s\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
  printf '%sautonomous GitHub contribution agent — installer%s\n\n' "$C_BLUE" "$C_RESET"
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

codex_cmd_is_windows_path() {
  local cmd_path="${1:-}"
  case "$cmd_path" in
    /mnt/[a-zA-Z]/*|*.exe|*.cmd)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

linux_codex_cmd() {
  local env_cmd="${CODEX_CMD:-}"
  if [ -n "$env_cmd" ] && [ -x "$env_cmd" ] && ! codex_cmd_is_windows_path "$env_cmd"; then
    printf '%s' "$env_cmd"
    return 0
  fi

  local path_cmd
  path_cmd="$(command -v codex 2>/dev/null || true)"
  if [ -n "$path_cmd" ] && ! codex_cmd_is_windows_path "$path_cmd"; then
    printf '%s' "$path_cmd"
    return 0
  fi

  if has_cmd npm; then
    local npm_prefix
    npm_prefix="$(npm prefix -g 2>/dev/null || true)"
    if [ -n "$npm_prefix" ] && [ -x "$npm_prefix/bin/codex" ]; then
      printf '%s' "$npm_prefix/bin/codex"
      return 0
    fi
  fi

  return 1
}

codex_cli_usable() {
  local cmd_path
  cmd_path="$(linux_codex_cmd 2>/dev/null || true)"
  [ -n "$cmd_path" ] || return 1
  "$cmd_path" --help >/dev/null 2>&1
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
  local codex_cmd
  codex_cmd="$(linux_codex_cmd 2>/dev/null || true)"
  if [ -z "$codex_cmd" ]; then
    return 1
  fi
  if [ -n "${OPENAI_API_KEY:-}" ]; then
    return 0
  fi
  "$codex_cmd" login status >/dev/null 2>&1
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
      "Use existing value from .env")
        export "$key=$env_file_value"
        ok "Using existing $key from .env"
        return 0
        ;;
      "Replace with a new value")
        ;;
      "Clear saved value and continue without it")
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
  local choice
  choice="$(choose_option "$prompt" "Yes" "No")"
  [ "$choice" = "Yes" ]
}

choose_option() {
  local prompt="$1"
  shift
  local options=("$@")
  local selected=0
  local key=""
  local line_count="${#options[@]}"
  local tty_device="/dev/tty"
  local longest="${#prompt}"
  local option=""

  for option in "${options[@]}"; do
    if [ "${#option}" -gt "$longest" ]; then
      longest="${#option}"
    fi
  done
  local panel_width=$((longest + 8))
  local panel_border="+$(printf '%*s' "$panel_width" '' | tr ' ' '-')+"

  if [ -n "${CI:-}" ] || [ ! -r "$tty_device" ] || [ ! -w "$tty_device" ]; then
    printf '%s%s%s\n' "$C_BOLD" "$prompt" "$C_RESET" >&2
    for option in "${options[@]}"; do
      printf '  - %s\n' "$option" >&2
    done
    printf '%s' "${options[0]}"
    return 0
  fi

  _render_option_picker() {
    local current="$1"
    printf '\r' >"$tty_device"
    for _ in $(seq 1 "$line_count"); do
      printf '\033[1A\033[2K' >"$tty_device"
    done
    printf '%s%s%s\n' "$C_BOLD" "$prompt" "$C_RESET" >"$tty_device"
    printf '%s\n' "$panel_border" >"$tty_device"
    local idx=0
    for option in "${options[@]}"; do
      if [ "$idx" -eq "$current" ]; then
        printf '| %s›%s %s%s%s%*s |\n' \
          "$C_GREEN" "$C_RESET" "$C_BOLD" "$option" "$C_RESET" \
          $((panel_width - ${#option} - 4)) "" >"$tty_device"
      else
        printf '|   %s%*s |\n' \
          "$option" \
          $((panel_width - ${#option} - 4)) "" >"$tty_device"
      fi
      idx=$((idx + 1))
    done
    printf '%s\n' "$panel_border" >"$tty_device"
    printf '%s[hint]%s Use arrow keys to move. Press Enter to select.\n' "$C_MAGENTA" "$C_RESET" >"$tty_device"
  }

  line_count=$((line_count + 4))
  _render_option_picker "$selected"
  while true; do
    if ! IFS= read -rsn1 key <"$tty_device"; then
      printf '\n' >"$tty_device"
      printf '%s[warn]%s Prompt input was interrupted or the terminal is no longer interactive. Stopping setup.\n' \
        "$C_YELLOW" "$C_RESET" >"$tty_device"
      return 130
    fi
    case "$key" in
      "")
        printf '%s' "${options[$selected]}"
        printf '\n' >"$tty_device"
        return 0
        ;;
      $'\x1b')
        if ! IFS= read -rsn2 key <"$tty_device"; then
          printf '\n' >"$tty_device"
          printf '%s[warn]%s Prompt input was interrupted or the terminal is no longer interactive. Stopping setup.\n' \
            "$C_YELLOW" "$C_RESET" >"$tty_device"
          return 130
        fi
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

ensure_user_npm_global() {
  if ! has_cmd npm; then
    return 1
  fi

  local current_prefix
  current_prefix="$(npm config get prefix 2>/dev/null || true)"
  if [ -n "$current_prefix" ] && [ -w "$current_prefix" ]; then
    return 0
  fi

  local desired_prefix="$HOME/.local/npm"
  mkdir -p "$desired_prefix/bin" "$desired_prefix/lib"
  export npm_config_prefix="$desired_prefix"
  export NPM_CONFIG_PREFIX="$desired_prefix"
  export PATH="$desired_prefix/bin:$PATH"
  log "using temporary npm global prefix at $desired_prefix"
}

install_codex_if_requested() {
  local existing_cmd
  existing_cmd="$(command -v codex 2>/dev/null || true)"
  if codex_cli_usable; then
    local usable_cmd
    usable_cmd="$(linux_codex_cmd)"
    export CODEX_CMD="$usable_cmd"
    update_env "CODEX_CMD" "$usable_cmd"
    log "codex already available at $usable_cmd"
    return 0
  fi
  if [ -n "$existing_cmd" ]; then
    if codex_cmd_is_windows_path "$existing_cmd"; then
      warn "Found Codex CLI at $existing_cmd from a Windows-mounted PATH."
      hint "That Codex binary cannot complete Linux/WSL auth here."
      hint "Rover will install or prefer a Linux Codex CLI for this environment."
    else
      warn "Found Codex CLI at $existing_cmd but it is not usable in this Linux environment."
      hint "Rover will reinstall Codex CLI for Linux/WSL."
    fi
  fi
  if ! confirm "Install Codex CLI now?"; then
    warn "Skipping Codex CLI install"
    return 0
  fi
  apt_install_if_missing nodejs node
  apt_install_if_missing npm npm
  if has_cmd npm; then
    if ! ensure_user_npm_global; then
      warn "Could not switch npm global installs to a user-writable prefix"
      hint "Run 'export npm_config_prefix=~/.local/npm' and add ~/.local/npm/bin to PATH, then rerun rover setup."
      return 0
    fi
    log "installing Codex CLI via npm"
    npm install -g @openai/codex@latest
    local installed_cmd
    installed_cmd="$(linux_codex_cmd 2>/dev/null || true)"
    if [ -n "$installed_cmd" ]; then
      export CODEX_CMD="$installed_cmd"
      update_env "CODEX_CMD" "$installed_cmd"
      ok "Codex CLI ready at $installed_cmd"
    else
      warn "Codex CLI install finished, but no Linux/WSL codex binary was found"
      hint "Check npm global bin and PATH, then rerun rover setup."
    fi
  else
    warn "npm not available; cannot install Codex CLI automatically"
  fi
}

configure_github_auth() {
  local auth_choice
  auth_choice="$(choose_option "Select GitHub auth mode for Rover:" \
    "Token in .env only" \
    "gh auth login only" \
    "Both token + gh auth login" \
    "Skip GitHub auth for now")"

  case "$auth_choice" in
    "Token in .env only")
      prompt_env_value "GITHUB_TOKEN" "Enter GITHUB_TOKEN (input hidden):" true
      ;;
    "gh auth login only")
      log "Skipping token prompt; will rely on gh auth"
      ;;
    "Both token + gh auth login")
      prompt_env_value "GITHUB_TOKEN" "Enter GITHUB_TOKEN (input hidden):" true
      ;;
    "Skip GitHub auth for now")
      warn "Skipping GitHub auth setup"
      ;;
  esac

  if [ -n "${GITHUB_TOKEN:-}" ] && [ -z "${GH_TOKEN:-}" ]; then
    export GH_TOKEN="$GITHUB_TOKEN"
  fi

  if has_cmd gh; then
    if gh auth status >/dev/null 2>&1; then
      log "gh auth already active"
    elif [ "$auth_choice" = "gh auth login only" ] || [ "$auth_choice" = "Both token + gh auth login" ]; then
      gh auth login
    fi
  fi
}

configure_codex_backend() {
  update_env "AI_BACKEND" "codex"
  update_env "AGENT_TOOL" "Codex"
  update_env "MODEL_SERIES" "GPT"
  install_codex_if_requested
  if codex_cli_usable; then
    local usable_cmd
    usable_cmd="$(linux_codex_cmd)"
    export CODEX_CMD="$usable_cmd"
    update_env "CODEX_CMD" "$usable_cmd"
  fi

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
      "Keep the existing Codex login")
        return 0
        ;;
      "Save OPENAI_API_KEY instead")
        prompt_env_value "OPENAI_API_KEY" "Enter OPENAI_API_KEY (input hidden):" true
        return 0
        ;;
      "Re-run Codex login flow")
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
    "Save OPENAI_API_KEY")
      prompt_env_value "OPENAI_API_KEY" "Enter OPENAI_API_KEY (input hidden):" true
      ;;
    "Run Codex device auth (recommended for VPS/headless)")
      local codex_cmd
      codex_cmd="$(linux_codex_cmd 2>/dev/null || true)"
      if [ -n "$codex_cmd" ] && codex_cli_usable; then
        local codex_status=0
        printf '\n'
        hint "Starting Codex device auth."
        hint "You will get a verification URL and code."
        hint "Open the URL from your local browser, finish login, then come back to this VPS terminal."
        if "$codex_cmd" login --device-auth; then
          ok "Codex device auth completed"
        else
          codex_status=$?
          if [ "$codex_status" -eq 130 ]; then
            warn "Codex device auth was interrupted. Stopping setup."
            return 130
          fi
          warn "Codex device auth did not complete."
          hint "If you pressed Ctrl+C or the flow failed, rerun setup and choose this option again."
          hint "You can also use OPENAI_API_KEY instead of interactive login."
        fi
      else
        warn "No usable Linux/WSL Codex CLI was found, so device auth was skipped"
        hint "If command -v codex points into /mnt/c, install Codex inside Linux or rerun setup to let Rover do it."
      fi
      ;;
    "Run browser-based codex login")
      local codex_cmd
      codex_cmd="$(linux_codex_cmd 2>/dev/null || true)"
      if [ -n "$codex_cmd" ] && codex_cli_usable; then
        local codex_status=0
        printf '\n'
        hint "Starting browser-based Codex login."
        hint "This works best on a local machine with direct browser access."
        hint "If localhost fails on a VPS, rerun setup and choose device auth instead."
        if "$codex_cmd" login; then
          ok "Codex browser login completed"
        else
          codex_status=$?
          if [ "$codex_status" -eq 130 ]; then
            warn "Codex browser login was interrupted. Stopping setup."
            return 130
          fi
          warn "Codex browser login did not complete."
          hint "On a VPS, choose device auth instead of browser login."
          hint "You can also use OPENAI_API_KEY instead of interactive login."
        fi
      else
        warn "No usable Linux/WSL Codex CLI was found, so browser login was skipped"
        hint "If command -v codex points into /mnt/c, install Codex inside Linux or rerun setup to let Rover do it."
      fi
      ;;
    "Skip Codex auth for now")
      warn "Skipping Codex auth"
      ;;
  esac
}

configure_claude_backend() {
  update_env "AI_BACKEND" "claude"
  update_env "AGENT_TOOL" "Claude Code"
  update_env "MODEL_SERIES" "Claude"

  # Install claude CLI if missing
  if has_cmd claude; then
    ok "claude CLI already available on PATH"
  else
    log "installing Claude CLI via npm"
    apt_install_if_missing nodejs node
    apt_install_if_missing npm npm
    if has_cmd npm; then
      npm install -g @anthropic-ai/claude-code
      if has_cmd claude; then
        ok "claude CLI installed"
      else
        warn "claude CLI install may have failed — check npm output above"
      fi
    else
      warn "npm not available; cannot install claude CLI automatically"
    fi
  fi

  update_env "CLAUDE_CMD" "claude"
  update_env "CLAUDE_ARGS" "-p -"

  # Auth: browser login (no API key needed) or API key
  local auth_choice
  auth_choice="$(choose_option "How do you want to authenticate Claude CLI?" \
    "Browser login (claude.ai account — no API key needed)" \
    "Save ANTHROPIC_API_KEY to .env" \
    "Skip auth for now")"
  case "$auth_choice" in
    "Browser login (claude.ai account — no API key needed)")
      ok "Claude CLI installed. Login step:"
      todo "Run 'claude' in a new terminal to complete browser login"
      todo "After login, run 'rover doctor' to verify"
      ;;
    "Save ANTHROPIC_API_KEY to .env")
      prompt_env_value "ANTHROPIC_API_KEY" "Enter ANTHROPIC_API_KEY (input hidden):" true
      ;;
    "Skip auth for now")
      warn "Skipping Claude auth — run 'claude login' or set ANTHROPIC_API_KEY later"
      ;;
  esac
}

configure_api_key_backend() {
  local provider_choice
  provider_choice="$(choose_option "Select your API-key provider:" \
    "OpenAI / GPT" \
    "Anthropic / Claude" \
    "OpenRouter" \
    "Other / custom provider")"
  case "$provider_choice" in
    "OpenAI / GPT")
      update_env "MODEL_SERIES" "GPT"
      prompt_env_value "OPENAI_API_KEY" "Enter OPENAI_API_KEY (input hidden):" true
      ;;
    "Anthropic / Claude")
      update_env "MODEL_SERIES" "Claude"
      prompt_env_value "ANTHROPIC_API_KEY" "Enter ANTHROPIC_API_KEY (input hidden):" true
      ;;
    "OpenRouter")
      update_env "MODEL_SERIES" "Other"
      prompt_env_value "OPENROUTER_API_KEY" "Enter OPENROUTER_API_KEY (input hidden):" true
      ;;
    "Other / custom provider")
      update_env "MODEL_SERIES" "Other"
      warn "Custom provider selected. Save provider-specific env vars manually if needed."
      ;;
  esac
  update_env "AGENT_TOOL" "Other"
  warn "API-key-only generation is still a partial path in this repo. Doctor will report remaining readiness gaps."
}

install_openclaw_integration() {
  if ! confirm "Install Rover OpenClaw skill, wrapper, and mcp.servers.rover now?"; then
    warn "Skipping OpenClaw native integration"
    return 0
  fi

  local python_bin
  local rover_bin
  local rover_mcp_bin
  python_bin="$(venv_python_bin)"
  rover_bin="$(venv_bin_dir)/rover"
  rover_mcp_bin="$(venv_bin_dir)/rover-mcp"

  if [ ! -x "$python_bin" ]; then
    warn "Python interpreter not found for OpenClaw asset install: $python_bin"
    return 0
  fi
  if [ ! -x "$rover_bin" ] || [ ! -x "$rover_mcp_bin" ]; then
    warn "rover / rover-mcp executable not found for OpenClaw asset install"
    return 0
  fi

  log "installing Rover OpenClaw skill, wrapper, and MCP config"
  "$python_bin" "$ROOT_DIR/src/platform/openclaw_install.py" \
    --rover-bin "$rover_bin" \
    --python-bin "$python_bin" \
    --rover-mcp-bin "$rover_mcp_bin"
  ok "OpenClaw Rover integration installed"
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

  log "installing rover package in editable mode"
  python -m pip install -e "$ROOT_DIR"

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
    "Codex CLI") configure_codex_backend ;;
    "Claude CLI") configure_claude_backend ;;
    "LLM API key only") configure_api_key_backend ;;
    "Skip backend setup for now") warn "Skipping backend setup for now" ;;
  esac

  section "OpenClaw integration"
  install_openclaw_integration

  section "Readiness check"
  log "running rover doctor"
  rover doctor 2>/dev/null || rover-engine --doctor 2>/dev/null || true

  printf '\n'
  ok "setup complete"
  printf '%sNext steps%s\n' "$C_BOLD" "$C_RESET"
  todo "Run: source \"$(venv_activate_script)\""
  if ! gh auth status >/dev/null 2>&1 && [ -z "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]; then
    todo "Run: gh auth login or set GH_TOKEN/GITHUB_TOKEN"
  fi
  if ! has_cmd codex && ! has_cmd claude; then
    todo "Install Codex CLI or Claude CLI before running contributions."
  fi
  todo "Run: rover doctor"
  todo "Run: rover run       # submit your first PR"
}

main "$@"
