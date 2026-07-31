#!/usr/bin/env bash

set -euo pipefail
SERVICE_NAME="terminal-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
STATE_FILE="/etc/${SERVICE_NAME}.install_dir"
DEFAULT_INSTALL_DIR="/opt/terminal-bot"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info(){ echo -e "${CYAN}i  $1${NC}"; }
ok(){ echo -e "${GREEN}OK $1${NC}"; }
warn(){ echo -e "${YELLOW}!  $1${NC}"; }
err(){ echo -e "${RED}X  $1${NC}"; }

require_root(){
  if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root (use sudo)."
    exit 1
  fi
}

load_install_dir(){
  if [[ -f "${STATE_FILE}" ]]; then
    INSTALL_DIR=$(cat "${STATE_FILE}")
  else
    INSTALL_DIR="${DEFAULT_INSTALL_DIR}"
  fi
}

stop_and_remove_service(){
  info "Stopping and disabling the ${SERVICE_NAME} service..."
  systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "${SERVICE_FILE}"
  systemctl daemon-reload
  ok "Service removed."
}

remove_files(){
  load_install_dir
  read -rp "Installation path to remove [${INSTALL_DIR}]: " DEL_DIR
  DEL_DIR=${DEL_DIR:-$INSTALL_DIR}

  if [[ ! -d "$DEL_DIR" ]]; then
    warn "Directory ${DEL_DIR} does not exist, nothing to remove there."
    return
  fi

  # server_manager_settings.json under ServerManager/ holds each user's
  # registered servers (SSH passwords encrypted with CRYPTO_SECRET from
  # .env). Point this out explicitly before deleting.
  if [[ -f "${DEL_DIR}/ServerManager/server_manager_settings.json" ]]; then
    warn "This directory contains saved server credentials (encrypted) at:"
    warn "  ${DEL_DIR}/ServerManager/server_manager_settings.json"
  fi

  read -rp "Delete ${DEL_DIR} and everything in it? (yes/no): " CONFIRM
  if [[ "$CONFIRM" == "yes" ]]; then
    rm -rf "$DEL_DIR"
    ok "Installation files removed."
  else
    info "Kept installation files at ${DEL_DIR}."
  fi
}

main(){
  require_root
  echo "======================================"
  echo " Terminal Bot - Uninstall"
  echo "======================================"
  read -rp "This will stop the bot and can delete its files. Continue? (yes/no): " GO
  if [[ "$GO" != "yes" ]]; then
    info "Cancelled."
    exit 0
  fi

  stop_and_remove_service
  remove_files
  rm -f "${STATE_FILE}"
  ok "Terminal Bot uninstallation complete."
}

main
