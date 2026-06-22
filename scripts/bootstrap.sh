#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-git@github.com:BigNounce90/rover.git}"
HTTPS_REPO_URL="${HTTPS_REPO_URL:-https://github.com/BigNounce90/rover.git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/rover}"

log() {
  printf '[bootstrap] %s\n' "$1"
}

warn() {
  printf '[warn] %s\n' "$1"
}

clone_repo() {
  if command -v git >/dev/null 2>&1; then
    if [ -d "$INSTALL_DIR/.git" ]; then
      log "existing repo found at $INSTALL_DIR; pulling latest changes"
      git -C "$INSTALL_DIR" pull --ff-only
      return 0
    fi
    if [ -d "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR/.git" ]; then
      warn "$INSTALL_DIR exists but is not a git repo; reusing contents"
      return 0
    fi
    log "cloning repository into $INSTALL_DIR"
    if ! git clone "$REPO_URL" "$INSTALL_DIR"; then
      warn "SSH clone failed; trying HTTPS clone"
      git clone "$HTTPS_REPO_URL" "$INSTALL_DIR"
    fi
    return 0
  fi

  warn "git is not installed yet. Install git first, then rerun this bootstrap script."
  exit 1
}

main() {
  clone_repo
  exec bash "$INSTALL_DIR/scripts/install_vps.sh"
}

main "$@"
