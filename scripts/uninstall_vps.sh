#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
ENV_FILE="$ROOT_DIR/.env"
MCP_FILE="$ROOT_DIR/.mcp.json"
DATA_DIR="$ROOT_DIR/data"
LOG_DIR="$ROOT_DIR/logs"
RUNS_DIR="$ROOT_DIR/runs"
STREAM_DIR="$ROOT_DIR/.stream_partials"
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
ALT_OPENCLAW_HOME="$HOME/openclaw"

C_RESET=$'\033[0m'
C_BLUE=$'\033[1;34m'
C_GREEN=$'\033[1;32m'
C_YELLOW=$'\033[1;33m'
C_RED=$'\033[1;31m'
C_MAGENTA=$'\033[1;35m'

log()  { printf '%s[rover]%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
ok()   { printf '%s[ok]%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
todo() { printf '  - %s\n' "$*"; }
CHANGES_MADE=0

choose_option() {
  local prompt="$1"
  shift
  local options=("$@")
  local selected=0
  local tty_device="/dev/tty"
  if [ ! -r "$tty_device" ] || [ ! -w "$tty_device" ]; then
    printf '%s' "${options[0]}"
    return 0
  fi

  render_menu() {
    printf '\033[2J\033[H' >"$tty_device"
    printf '%s\n\n' "$prompt" >"$tty_device"
    local idx
    for idx in "${!options[@]}"; do
      if [ "$idx" -eq "$selected" ]; then
        printf '  %s> %s%s\n' "$C_GREEN" "${options[$idx]}" "$C_RESET" >"$tty_device"
      else
        printf '    %s\n' "${options[$idx]}" >"$tty_device"
      fi
    done
    printf '\n' >"$tty_device"
    printf '%s[hint]%s Use arrow keys to move. Press Enter to select.\n' "$C_MAGENTA" "$C_RESET" >"$tty_device"
  }

  local key
  while true; do
    render_menu
    IFS= read -rsn1 key <"$tty_device"
    if [ "$key" = $'\x1b' ]; then
      IFS= read -rsn1 -t 0.1 key <"$tty_device" || true
      if [ "$key" = "[" ]; then
        IFS= read -rsn1 -t 0.1 key <"$tty_device" || true
        case "$key" in
          A)
            if [ "$selected" -gt 0 ]; then
              selected=$((selected - 1))
            fi
            ;;
          B)
            if [ "$selected" -lt $((${#options[@]} - 1)) ]; then
              selected=$((selected + 1))
            fi
            ;;
        esac
      fi
    elif [ -z "$key" ] || [ "$key" = $'\n' ]; then
      printf '\033[2J\033[H' >"$tty_device"
      printf '%s' "${options[$selected]}"
      return 0
    fi
  done
}

confirm() {
  local prompt="${1:-Continue?}"
  local choice
  choice="$(choose_option "$prompt" "Yes" "No")"
  [ "$choice" = "Yes" ]
}

remove_path() {
  local target="$1"
  local label="$2"
  if [ -e "$target" ] || [ -L "$target" ]; then
    rm -rf -- "$target"
    CHANGES_MADE=1
    ok "removed $label: $target"
  else
    log "skip missing $label: $target"
  fi
}

remove_openclaw_assets() {
  local roots=("$OPENCLAW_HOME" "$ALT_OPENCLAW_HOME")
  local root
  for root in "${roots[@]}"; do
    remove_path "$root/workspace/skills/rover" "OpenClaw Rover workspace skill"
    remove_path "$root/skills/rover" "OpenClaw Rover fallback skill"
    remove_path "$root/workspace/skills/github-contribution-engine" "OpenClaw workspace skill"
    remove_path "$root/skills/github-contribution-engine" "OpenClaw fallback skill"
    remove_path "$root/tools/rover.py" "OpenClaw Rover wrapper"
    remove_path "$root/tools/contribution.py" "OpenClaw wrapper"
  done
}

printf '%srover uninstall/reset%s\n\n' "$C_BLUE" "$C_RESET"
warn "This script removes Rover-local install artifacts so you can test setup again from a clean slate."
warn "Review each prompt carefully. Choosing Yes will permanently delete the selected local Rover files or directories."

initial_choice="$(choose_option "Warning: this reset flow can permanently delete selected local Rover files, directories, and integration artifacts.

Choose how to proceed:" \
  "Continue uninstall/reset" \
  "Cancel and keep everything")"

if [ "$initial_choice" != "Continue uninstall/reset" ]; then
  warn "Cancelled uninstall/reset. No changes were made."
  exit 0
fi

if confirm "Remove Python virtualenv at $VENV_DIR?"; then
  remove_path "$VENV_DIR" "virtualenv"
fi

if confirm "Remove Rover runtime state (data, logs, runs, stream partials)?"; then
  remove_path "$DATA_DIR" "data dir"
  remove_path "$LOG_DIR" "logs dir"
  remove_path "$RUNS_DIR" "runs dir"
  remove_path "$STREAM_DIR" "stream partials dir"
fi

if confirm "Remove local MCP config at $MCP_FILE?"; then
  remove_path "$MCP_FILE" ".mcp.json"
fi

if confirm "Remove OpenClaw skill and wrapper installed by Rover?"; then
  remove_openclaw_assets
fi

if confirm "Remove local .env so setup can start from zero?"; then
  remove_path "$ENV_FILE" ".env"
fi

printf '\n'
if [ "$CHANGES_MADE" -eq 1 ]; then
  ok "Rover uninstall/reset complete"
  printf '%sNext steps%s\n' "$C_BLUE" "$C_RESET"
  todo "Optional: run 'gh auth logout -h github.com' if you also want to reset GitHub CLI auth"
  todo "Optional: run 'codex logout' or 'claude logout' if you want to re-test backend login flows"
  todo "Reinstall with: bash scripts/install_vps.sh"
else
  warn "No changes were made. Rover uninstall/reset was skipped."
fi
