#!/usr/bin/env bash
# =============================================================
# Terminal Bot - Installer
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/Terminal_Bot/main/install.sh)
# =============================================================
set -euo pipefail

REPO_URL="https://github.com/Mahersaber2024/Terminal_Bot.git"
SERVICE_NAME="terminal-bot"
DEFAULT_INSTALL_DIR="/opt/terminal-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
STATE_FILE="/etc/${SERVICE_NAME}.install_dir"

REQUIRED_FILES=(
  "main.py"
  "handlers.py"
  "config.py"
  "crypto_utils.py"
  "sponsor_gate.py"
  "requirements.txt"
)

SERVERMANAGER_REQUIRED_FILES=(
  "__init__.py"
  "server_manager_handlers.py"
  "server_manager_engine.py"
  "server_manager_settings.py"
)

# ------------------ Colors ------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info(){ echo -e "${CYAN}ℹ️  $1${NC}"; }
ok(){ echo -e "${GREEN}✅ $1${NC}"; }
warn(){ echo -e "${YELLOW}⚠️  $1${NC}"; }
err(){ echo -e "${RED}❌ $1${NC}"; }
press_enter(){ read -rp "Press Enter to continue..." _ || true; }

require_root(){
  if [[ $EUID -ne 0 ]]; then
    err "This script must be run with root privileges (using sudo or as root user)."
    exit 1
  fi
}

save_install_dir(){ echo "${INSTALL_DIR}" > "${STATE_FILE}"; }
load_install_dir(){
  if [[ -f "${STATE_FILE}" ]]; then
    INSTALL_DIR=$(cat "${STATE_FILE}")
  else
    INSTALL_DIR="${DEFAULT_INSTALL_DIR}"
  fi
}

# ------------------ Steps ------------------
install_dir_prompt(){
  read -rp "Installation path [${DEFAULT_INSTALL_DIR}]: " INSTALL_DIR
  INSTALL_DIR=${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}
}

detect_python(){
  if command -v python3 &>/dev/null; then
    PY_BIN=python3
  else
    err "Python 3 not found on the server."
    exit 1
  fi
}

install_system_packages(){
  info "Installing system dependencies..."
  # iputils-ping: server_manager_engine.py's quick-ping feature shells out
  # to "ping" directly, without SSH credentials.
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip git iputils-ping
  ok "System dependencies installed."
}

clone_or_update_repo(){
  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    info "Project already exists, updating..."
    git -C "${INSTALL_DIR}" pull
    ok "Project code is ready."
    return
  fi

  local env_backup=""
  if [[ -d "${INSTALL_DIR}" ]] && [[ -n "$(ls -A "${INSTALL_DIR}" 2>/dev/null)" ]]; then
    warn "${INSTALL_DIR} already exists (from a previous run) but isn't a git checkout - clearing it first."
    if [[ -f "${INSTALL_DIR}/.env" ]]; then
      env_backup=$(mktemp)
      cp "${INSTALL_DIR}/.env" "$env_backup"
    fi
    # Fully removed (not recreated) - git clone needs a nonexistent or
    # empty target directory, so we let git clone create it fresh below.
    rm -rf "${INSTALL_DIR:?}"
  fi

  info "Cloning project from GitHub..."
  mkdir -p "$(dirname "${INSTALL_DIR}")"
  git clone "${REPO_URL}" "${INSTALL_DIR}"

  if [[ -n "$env_backup" ]]; then
    mv "$env_backup" "${INSTALL_DIR}/.env"
    info "Restored previous .env file."
  fi
  ok "Project code is ready."
}

verify_required_files(){
  info "Verifying required source files..."
  local missing=()

  for f in "${REQUIRED_FILES[@]}"; do
    [[ -f "${INSTALL_DIR}/${f}" ]] || missing+=("$f")
  done
  for f in "${SERVERMANAGER_REQUIRED_FILES[@]}"; do
    [[ -f "${INSTALL_DIR}/ServerManager/${f}" ]] || missing+=("ServerManager/$f")
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    err "The following required files are missing from the repository:"
    for f in "${missing[@]}"; do echo " - $f"; done
    err "Please make sure these files are committed and pushed to: ${REPO_URL}"
    err "Then run this installer again (option 2: Update bot)."
    exit 1
  fi

  if [[ ! -f "${INSTALL_DIR}/ServerManager/__init__.py" ]]; then
    warn "ServerManager/__init__.py not found — creating an empty one so 'ServerManager' is importable as a package."
    touch "${INSTALL_DIR}/ServerManager/__init__.py"
  fi

  ok "All required source files are present."
}

setup_venv(){
  info "Creating Python virtual environment and installing packages..."
  cd "${INSTALL_DIR}"
  ${PY_BIN} -m venv venv
  source venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
  deactivate
  ok "Python packages installed."
}

collect_bot_config(){
  echo
  echo "======================================"
  echo " Terminal Bot Configuration"
  echo "======================================"

  BOT_TOKEN=""
  while [[ -z "$BOT_TOKEN" ]]; do
    read -rp "Bot token (from @BotFather): " BOT_TOKEN
  done

  read -rp "Admin numeric Telegram user IDs (comma separated, optional): " ADMIN_IDS

  echo
  echo "Sponsor channel gate: require users to join channel(s) before using the bot."
  echo "Leave blank to disable this gate."
  read -rp "Sponsor channels, format 'id:title,id:title' (optional): " SPONSOR_CHANNELS
  read -rp "Sponsor channel invite links, format 'id:https://t.me/...' (optional): " SPONSOR_CHANNEL_LINKS

  echo
  CRYPTO_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  info "Generated a random CRYPTO_SECRET to encrypt stored SSH passwords at rest."
  warn "This secret is written to ${INSTALL_DIR}/.env - back it up. If it's lost or changed, previously saved server passwords can no longer be decrypted."
}

write_env_file(){
  local env_file="${INSTALL_DIR}/.env"
  if [[ -f "$env_file" ]]; then
    warn ".env already exists at ${env_file}, leaving it untouched."
    warn "Delete it first if you want this installer to regenerate it."
    return
  fi

  cat > "${env_file}" <<EOF
BOT_TOKEN=${BOT_TOKEN}
ADMIN_IDS=${ADMIN_IDS}
SPONSOR_CHANNELS=${SPONSOR_CHANNELS}
SPONSOR_CHANNEL_LINKS=${SPONSOR_CHANNEL_LINKS}
CRYPTO_SECRET=${CRYPTO_SECRET}
EOF
  chmod 600 "${env_file}"
  ok ".env file created (restricted access)."
}

create_systemd_service(){
  info "Creating systemd service..."
  cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Terminal Bot (Telegram SSH terminal bot)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/venv/bin/python3 ${INSTALL_DIR}/main.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}" >/dev/null
  systemctl restart "${SERVICE_NAME}"
  ok "Service created, enabled, and started."
}

show_summary(){
  echo
  echo "============================================================"
  echo " ✅ Terminal Bot installed"
  echo "============================================================"
  echo " Install directory : ${INSTALL_DIR}"
  echo " Service name       : ${SERVICE_NAME}"
  echo
  echo " Check status : systemctl status ${SERVICE_NAME}"
  echo " View logs    : journalctl -u ${SERVICE_NAME} -f"
  echo " Restart      : systemctl restart ${SERVICE_NAME}"
  echo
  echo " Config file (.env) : ${INSTALL_DIR}/.env"
  echo "============================================================"
}

# ============================================================
# ========== Main Functions ==========
# ============================================================
full_install(){
  require_root
  detect_python
  install_dir_prompt
  install_system_packages
  clone_or_update_repo
  verify_required_files
  setup_venv
  collect_bot_config
  write_env_file
  create_systemd_service
  save_install_dir
  ok "✅ Installation completed successfully! 🎉"
  show_summary
  press_enter
}

update_bot(){
  require_root
  load_install_dir
  if [[ ! -d "${INSTALL_DIR}" ]]; then
    read -rp "Enter current installation path: " INSTALL_DIR
  fi
  detect_python
  clone_or_update_repo
  verify_required_files
  setup_venv
  systemctl restart "${SERVICE_NAME}"
  save_install_dir
  ok "Update completed and service restarted."
}

restart_service(){
  require_root
  systemctl restart "${SERVICE_NAME}"
  ok "Service restarted."
}

view_logs(){
  journalctl -u "${SERVICE_NAME}" -f --no-pager -n 100
}

show_status(){
  systemctl status "${SERVICE_NAME}" --no-pager || true
  press_enter
}

uninstall_bot(){
  require_root
  warn "This will remove the service and bot files."
  read -rp "Are you sure? (yes/no): " CONFIRM
  if [[ "$CONFIRM" != "yes" ]]; then
    info "Cancelled."
    return
  fi

  systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "${SERVICE_FILE}"
  systemctl daemon-reload

  load_install_dir
  read -rp "Installation path to remove [${INSTALL_DIR}]: " DEL_DIR
  DEL_DIR=${DEL_DIR:-$INSTALL_DIR}

  if [[ -d "$DEL_DIR" ]]; then
    rm -rf "$DEL_DIR"
    ok "Installation files removed."
  fi

  rm -f "${STATE_FILE}"
  ok "Bot uninstallation completed."
}

main_menu(){
  while true; do
    echo
    echo "======================================"
    echo " 🚀 Terminal Bot - Installer"
    echo "======================================"
    echo "1) Full installation"
    echo "2) Update bot"
    echo "3) Restart service"
    echo "4) View logs"
    echo "5) Service status"
    echo "6) Complete uninstall"
    echo "0) Exit"
    echo "======================================"
    read -rp "Enter option number: " CHOICE
    case "$CHOICE" in
      1) full_install ;;
      2) update_bot; press_enter ;;
      3) restart_service; press_enter ;;
      4) view_logs ;;
      5) show_status ;;
      6) uninstall_bot; press_enter ;;
      0) exit 0 ;;
      *) warn "Invalid option." ;;
    esac
  done
}

main_menu
